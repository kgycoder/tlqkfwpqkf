"""
LM Arena CLI - Playwright 브라우저 매니저

- 영속 컨텍스트(persistent context)로 쿠키/세션을 저장해 재실행 시에도
  로그인 상태를 유지한다.
- "Experience the frontier" 문구가 "실제 텍스트로" 존재하는지(클래스명이
  아니라 내용 자체)를 기준으로 로그인 상태를 판별한다. 로그인 후에는 같은
  class를 재사용하는 다른 헤드라인이 나타날 수 있으므로, class 기반 매칭은
  사용하지 않는다. 리다이렉트 자체는 로그인 성공의 근거로 사용하지 않는다.
- 응답 완료 여부는 (a) 프롬프트 전송 이전 대비 "새로운" 응답 컨테이너가
  실제로 추가되었는지, (b) 생성 중 표시가 사라졌는지, (c) 그 새 컨테이너의
  텍스트가 일정 시간 변하지 않는지를 모두 확인해 판정한다. 이렇게 해야
  방금 보낸 사용자 프롬프트 자체를 답변으로 착각하지 않는다.
"""
from __future__ import annotations

import difflib
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

from playwright.sync_api import (
    sync_playwright,
    Page,
    BrowserContext,
    TimeoutError as PWTimeoutError,
)

from . import dom_selectors as sel
from .config import (
    USER_DATA_DIR,
    DIRECT_MODE_URL,
    AGENT_MODE_URL,
    CHAT_MODE_DIRECT,
    CHAT_MODE_AGENT,
    MODE_URLS,
    normalize_chat_mode,
    ensure_dirs,
    DEBUG_MODE,
    HIDDEN_WINDOW,
)
from . import ui
from .captcha import CaptchaGuard
from .window_hider import WindowHider


class GenerationStopped(Exception):
    """
    사용자가 답변 중단 버튼을 눌러 현재 진행 중이던 LM Arena 답변 생성을
    중단시켰음을 호출부(webapp.py)에 알리기 위한 신호용 예외. DOM
    판별/파싱 로직과는 무관하며, 스트리밍 도중 중단 요청을 즉시 전파하는
    용도로만 쓰인다.
    """
    pass


@dataclass
class CodeBlock:
    language: str
    filename_hint: Optional[str]
    code: str


@dataclass
class ReasoningState:
    """
    추론(reasoning) 미리보기 스트리밍 상태 한 프레임.

    - text: 지금까지 감지된 추론 텍스트(미리보기용, 마지막 일부만 사용될 수 있음)
    - elapsed_seconds: 추론 시작~현재(또는 종료 시점)까지 경과 시간
    - is_done: 실제 답변 컨테이너가 나타나 추론 단계가 끝났다고 판단되면 True
      (True가 되는 순간의 elapsed_seconds로 이후 계속 고정된다 = "Thought for ~")
    """
    text: str
    elapsed_seconds: float
    is_done: bool


@dataclass
class ParsedResponse:
    text_parts: list[str]
    code_blocks: list[CodeBlock]

    @property
    def plain_text(self) -> str:
        return "\n\n".join(p for p in self.text_parts if p.strip())


# "Experience" ... "the" ... "frontier" 가 (줄바꿈/공백 무관하게) 순서대로
# 등장하는지를 검사한다. class 이름이 아니라 실제 텍스트 내용만으로 판별한다.
LANDING_HEADLINE_PATTERN = re.compile(r"experience\s+the\s+frontier", re.IGNORECASE)


class LMArenaBrowser:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self._pw = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        # 추론(reasoning) 미리보기 스트리밍용 상태. 매 턴(send_prompt)마다
        # 초기화된다. 기존 답변/코드 스트리밍 로직과는 완전히 분리된 별도
        # 트래킹이라 기존 로직에는 영향을 주지 않는다.
        self._reasoning_baseline: frozenset[str] = frozenset()
        self._reasoning_start_time: Optional[float] = None
        # 채팅 모드(완전히 새로 추가): "direct"(기존 동작) 또는 "agent".
        # 이 값은 "코드 블록을 어떤 DOM으로 읽을 것인가"에만 영향을 주며,
        # 로그인 판정 / 프롬프트 전송 / 응답 완료 판정 / 추론 추적 등
        # 기존 로직은 두 모드에서 완전히 동일하게 동작한다(일반 답변과
        # 추론 DOM이 두 모드에서 같기 때문).
        self.chat_mode: str = CHAT_MODE_DIRECT
        # 로그인 완료 후 "실제 LM Arena 화면 대신 커스텀 완료 화면만
        # 보여주기" 기능(완전히 새로 추가)의 상태. 페이지가 다시 로드될
        # 때마다(모드 전환/새 채팅 등) 오버레이를 자동으로 재주입하기 위한
        # 리스너를 한 번만 걸기 위해 별도로 추적한다.
        self._connected_overlay_active: bool = False
        self._connected_overlay_listener_attached: bool = False
        # 로봇 확인(reCAPTCHA) UI 감시기(완전히 새로 추가). start()에서
        # page가 준비된 뒤 생성한다. 아래 _captcha_tick()으로만 사용되며,
        # 기존 로그인/전송/스트리밍/DOM 판정 로직에는 관여하지 않는다.
        self._captcha: Optional[CaptchaGuard] = None

        # 숨김 창 모드(완전히 새로 추가): 브라우저는 한 번만 띄우고 창만 화면
        # 밖으로 숨긴다(window_hider.py 참고). 진짜 headless는 reCAPTCHA를 사용자가
        # 통과시킬 수 없으므로 이 모드에서는 항상 창이 있는 브라우저를 쓴다.
        self._hider: Optional[WindowHider] = None
        if HIDDEN_WINDOW and not DEBUG_MODE:
            self.headless = False
            self._hider = WindowHider(self)
        self._revealed_for_login: bool = False
        self._revealed_for_captcha: bool = False

    # ── 라이프사이클 ──────────────────────────────────────────────
    def start(self) -> None:
        ensure_dirs()
        self._pw = sync_playwright().start()
        launch_args = ["--disable-blink-features=AutomationControlled"]
        if self._hider is not None:
            self._hider.note_foreground()
            launch_args += self._hider.launch_args()  # 화면 밖 위치 + 성능 저하 방지 플래그
        self.context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=self.headless,
            viewport={"width": 1400, "height": 900},
            args=launch_args,
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self._captcha = CaptchaGuard(self)
        if self._hider is not None:
            self._hider.attach()  # 창을 화면 밖으로 옮기고 작업 표시줄에서 뺀다.

    # ── 로봇 확인(reCAPTCHA) 감지 (완전히 새로 추가) ─────────────────
    # LM Arena가 답변 도중 띄우는 "Security Verification" 다이얼로그를
    # 감지하면 captcha.py가 (1) 그 다이얼로그만 남기고 배경/오버레이를 숨긴
    # 뒤 (2) 자동화 창을 맨 앞으로 가져온다. 통과되어 다이얼로그가 사라지면
    # 화면과 창 상태를 원래대로 되돌린다. 아래 헬퍼는 응답 대기 루프에서
    # 논블로킹으로 호출된다(Playwright sync API는 스레드 안전하지 않으므로
    # 별도 감시 스레드를 두지 않는다).
    def _captcha_tick(self) -> bool:
        """확인 UI가 지금 떠 있으면 True. 어떤 경우에도 예외를 던지지 않는다."""
        if self._captcha is None:
            return False
        try:
            active = self._captcha.tick()
        except Exception:
            return False
        # 숨김 창 모드: 감지/커버/창 전면 표시는 위 기존 로직이 처리하고, 여기서는
        # 숨겨 둔 창을 화면으로 꺼내거나(감지) 다시 숨기기만(통과) 한다.
        self._sync_captcha_window(active)
        return active

    def _sync_captcha_window(self, active: bool) -> None:
        if self._hider is None:
            return
        try:
            if active and not self._revealed_for_captcha and self._hider.hidden:
                self._revealed_for_captcha = True
                self._hider.reveal()
            elif not active and self._revealed_for_captcha:
                self._revealed_for_captcha = False
                if not self._revealed_for_login:
                    self._hider.conceal()
        except Exception:
            pass

    def goto_direct_mode(self) -> None:
        assert self.page is not None
        self.page.goto(DIRECT_MODE_URL, wait_until="domcontentloaded")
        # SPA 특성상 domcontentloaded 시점엔 아직 로그인 상태 관련 요소들이
        # 렌더링되지 않았을 수 있으므로, 짧게 추가 대기해 오탐을 줄인다.
        try:
            self.page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        time.sleep(1.0)
        self.chat_mode = CHAT_MODE_DIRECT

    # ── 채팅 모드 전환 (Direct <-> Agent) : 완전히 새로 추가 ──────────
    # LM Arena는 같은 사이트 안에서 URL만 바꾸면 모드가 전환된다. 위
    # goto_direct_mode()와 동일한 이동/대기 절차를 그대로 쓰되, 목적지
    # URL만 모드에 따라 고른다. 로그인 판정/전송/스트리밍 로직은 두 모드가
    # 완전히 같은 코드를 공유한다.
    def goto_agent_mode(self) -> None:
        self.goto_mode(CHAT_MODE_AGENT)

    def goto_mode(self, mode: str) -> str:
        assert self.page is not None
        mode = normalize_chat_mode(mode)
        self.page.goto(MODE_URLS[mode], wait_until="domcontentloaded")
        try:
            self.page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        time.sleep(1.0)
        self.chat_mode = mode
        # 모드를 바꾸면 LM Arena 대화 세션도 새로 시작되므로, 이전 턴의
        # 추론 트래킹 상태를 new_chat()과 동일하게 초기화한다.
        self._reasoning_baseline = frozenset()
        self._reasoning_start_time = None
        return mode

    def detect_chat_mode(self) -> str:
        """현재 페이지 URL로 실제 모드를 확인해 self.chat_mode에 반영한다."""
        try:
            url = self.page.url if self.page is not None else ""
        except Exception:
            url = ""
        path = url.split("://", 1)[-1]
        path = path[path.find("/"):] if "/" in path else ""
        path = path.split("?", 1)[0]
        if path == "/agent" or path.startswith("/agent/"):
            self.chat_mode = CHAT_MODE_AGENT
        elif path.startswith("/text"):
            self.chat_mode = CHAT_MODE_DIRECT
        return self.chat_mode

    def new_chat_for_mode(self) -> bool:
        """
        현재 모드에 맞는 'New Chat'을 시작한다. 기존 new_chat()(Direct용
        NEW_CHAT_BUTTON 기준)은 전혀 건드리지 않고, Agent 모드일 때만 별도
        셀렉터를 먼저 시도하고 그래도 실패하면 모드 URL로 다시 이동한다.
        """
        assert self.page is not None
        if self.chat_mode != CHAT_MODE_AGENT:
            return self.new_chat()

        btn = sel.first_match_locator(self.page, sel.AGENT_NEW_CHAT_BUTTON)
        if btn is not None and btn.count() > 0:
            try:
                btn.first.click()
                self.page.wait_for_load_state("domcontentloaded", timeout=5000)
                time.sleep(0.5)
                self._reasoning_baseline = frozenset()
                self._reasoning_start_time = None
                return True
            except Exception:
                pass
        try:
            self.goto_mode(CHAT_MODE_AGENT)
            return True
        except Exception:
            return False

    # 코드 블록 추출에 쓸 셀렉터 묶음을 현재 모드에 따라 고른다.
    # (container, language, code, filename) 순서이며, Direct 모드에서는
    # 파일명 셀렉터가 비어 있어 기존 동작과 100% 동일하다.
    def _code_selector_set(self) -> "tuple[list[str], list[str], list[str], list[str]]":
        if self.chat_mode == CHAT_MODE_AGENT:
            return (
                sel.AGENT_CODE_BLOCK_CONTAINER,
                sel.AGENT_CODE_BLOCK_LANGUAGE_LABEL,
                sel.AGENT_CODE_BLOCK_CODE,
                sel.AGENT_CODE_BLOCK_FILENAME,
            )
        return (
            sel.CODE_BLOCK_CONTAINER,
            sel.CODE_BLOCK_LANGUAGE_LABEL,
            sel.CODE_BLOCK_CODE,
            [],
        )

    def close(self) -> None:
        """
        브라우저 컨텍스트와 Playwright 드라이버를 정리한다.

        사용자가 브라우저 창을 직접 닫아버렸거나, 드라이버 프로세스가 이미
        죽어 연결이 끊긴 상태에서 close()가 호출될 수 있다(예: 'Connection
        closed while reading from the driver'). 이런 경우 context.close()나
        pw.stop()이 예외를 던지는데, 이 시점은 프로그램이 정상 종료되는
        경로이므로 이미 닫혀 있는 것을 다시 닫으려다 실패한 것뿐이지 실제
        오류가 아니다. 따라서 각 정리 단계를 개별적으로 try/except로 감싸서,
        하나가 실패해도(이미 닫혀 있어도) 나머지 정리 단계는 계속 진행하고
        예외가 프로그램 종료를 방해하며 그대로 튀어나오지 않도록 한다.
        """
        try:
            if self.context:
                self.context.close()
        except Exception:
            # 이미 닫혀 있거나(사용자가 브라우저 창을 직접 닫음) 드라이버와의
            # 연결이 끊긴 경우. 정상 종료 흐름이므로 조용히 무시한다.
            pass

        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            # 위와 동일한 이유로 무시한다.
            pass

    # ── 로그인 ────────────────────────────────────────────────────
    def _landing_headline_visible(self) -> bool:
        """
        페이지에 "Experience the frontier" 텍스트가 실제로(내용 기준으로)
        보이는 h1이 있는지 검사한다. class 이름만으로는 판단하지 않는다.
        로그인 후 같은 class(header-5xl 등)를 다른 문구의 헤드라인이
        재사용할 수 있기 때문에, class 매칭은 절대 신뢰하지 않는다.
        """
        assert self.page is not None
        try:
            headlines = self.page.locator("h1")
            count = headlines.count()
            for i in range(count):
                el = headlines.nth(i)
                try:
                    if not el.is_visible():
                        continue
                    raw_text = el.inner_text()
                except Exception:
                    continue
                normalized = " ".join(raw_text.split())
                if LANDING_HEADLINE_PATTERN.search(normalized):
                    return True
            return False
        except Exception:
            # 판단 불가 시, 로그아웃으로 단정하지 않도록 False 반환
            # (호출부에서 입력창 존재 여부로 추가 검증한다)
            return False

    def _sign_in_button_visible(self) -> bool:
        """
        "Log in" / "Sign in" 등 로그인 유도 버튼이 화면에 보이는지 검사한다.
        이 버튼이 보인다는 것은 아직 로그아웃 상태라는 가장 확실한 신호이며,
        입력창(textarea) 존재 여부보다 우선한다. 랜딩 페이지에서도 비로그인
        사용자에게 동일한 입력창이 보이는 경우가 있어, 입력창 존재만으로는
        로그인 여부를 판단할 수 없기 때문이다.
        """
        assert self.page is not None
        for marker in sel.SIGN_IN_MARKERS:
            try:
                loc = self.page.locator(marker)
                if loc.count() > 0 and loc.first.is_visible():
                    return True
            except Exception:
                continue
        return False

    def is_logged_in(self) -> bool:
        """
        판정 순서(우선순위):
        1) "Experience the frontier" 헤드라인이 실제로 보이면 -> 로그아웃
        2) "Log in" / "Sign in" 류 버튼이 보이면 -> 로그아웃
           (입력창이 이미 존재하더라도 이 신호가 우선한다. 비로그인 사용자도
            같은 입력창을 볼 수 있는 UI이기 때문)
        3) 위 두 로그아웃 신호가 전혀 없고, 계정 메뉴 또는 입력창 등
           로그인 후 요소가 존재하면 -> 로그인
        4) 그 외 애매한 경우는 보수적으로 로그아웃으로 판단한다.
        페이지 리다이렉트 자체는 로그인 성공의 근거로 사용하지 않는다.
        """
        assert self.page is not None
        try:
            if self._landing_headline_visible():
                return False

            if self._sign_in_button_visible():
                return False

            # 계정 메뉴가 보이면 가장 신뢰도 높은 로그인 신호
            for marker in sel.ACCOUNT_MENU_MARKERS:
                try:
                    loc = self.page.locator(marker)
                    if loc.count() > 0 and loc.first.is_visible():
                        return True
                except Exception:
                    continue

            # 로그아웃 신호가 전혀 없는 상태에서 입력창이 있으면 로그인으로 판단
            for marker in sel.LOGGED_IN_MARKERS:
                try:
                    loc = self.page.locator(marker)
                    if loc.count() > 0:
                        return True
                except Exception:
                    continue

            # 판단 불가 시 보수적으로 False 반환 (폴백)
            return False
        except Exception:
            return False

    def wait_for_login(self, timeout_seconds: int = 600, poll_interval: float = 2.0) -> bool:
        """
        사용자가 브라우저 창에서 직접 로그인할 때까지 대기한다.
        (구글/이메일 로그인 등 리다이렉트가 여러 번 발생할 수 있으므로,
        매 폴링마다 DOM 마커를 다시 검사한다.)
        """
        assert self.page is not None
        deadline = time.time() + timeout_seconds
        with ui.spinner("브라우저에서 로그인을 진행해주세요..."):
            while time.time() < deadline:
                try:
                    self.page.wait_for_load_state("domcontentloaded", timeout=poll_interval * 1000)
                except PWTimeoutError:
                    pass
                except Exception:
                    pass

                if self.is_logged_in():
                    return True
                time.sleep(poll_interval)
        return False

    # ── 로그인 창만 보여주기 (완전히 새로 추가) ─────────────────────
    # 배경: 로그아웃 상태에서 자동화 브라우저 창을 그대로 두면, 사용자가
    # 로그인하기 전까지 LM Arena 사이트 화면(랜딩 페이지 등) 전체가 그대로
    # 노출된다. 아래 두 메서드는 is_logged_in() / wait_for_login() /
    # SIGN_IN_MARKERS 등 기존 로그인 판별 로직은 전혀 건드리지 않고, 그
    # 위에서 "무엇이 화면에 보이는가"만 바꾸는 순수 추가 기능이다.
    #
    # 동작 방식:
    # 1) SIGN_IN_MARKERS(기존 셀렉터)로 로그인 버튼을 자동으로 클릭해
    #    로그인 흐름을 사용자 대신 시작시킨다.
    # 2) 클릭 결과 별도 팝업 창(구글 로그인 등 OAuth 리다이렉트)이 뜨면,
    #    그 팝업을 최상단으로 올리고 원래 창은 about:blank로 바꿔
    #    사이트 내용이 뒤에 노출되지 않게 한 뒤, 팝업이 닫힐 때까지
    #    기다렸다가 원래 창을 원래 URL로 되돌린다(이후 wait_for_login이
    #    정상적으로 로그인 여부를 다시 판별할 수 있도록).
    # 3) 팝업이 아니라 같은 페이지 안 모달로 뜨는 경우에는, 모달을 제외한
    #    배경 요소를 CSS로 숨겨 로그인 창만 보이게 한다.
    _LOGIN_HIDE_STYLE_ID = "lmarena-cli-login-hide-style"

    def enter_login_only_view(self, popup_wait_seconds: int = 300) -> bool:
        """
        로그아웃 상태일 때 호출한다. "Sign in" 버튼을 자동으로 눌러 로그인
        모달을 띄운 뒤, 그 안의 "Continue with Google" 버튼까지 자동으로
        이어서 눌러 실제 구글 로그인 팝업이 곧바로 뜨도록 만든다. 그 결과
        화면에는 LM Arena 사이트 대신 구글 로그인 팝업(구글 버튼을 못 찾은
        경우에는 최소한 로그인 모달)만 보이게 된다. 로그인 버튼 자체를
        찾지 못했으면 False를 반환한다(이 경우 호출부는 기존
        wait_for_login()만으로 폴백하면 된다).
        """
        assert self.page is not None
        btn = sel.first_match_locator(self.page, sel.SIGN_IN_MARKERS)
        if btn is None or btn.count() == 0:
            # [간헐적 버그 워크어라운드] 초기 렌더링 타이밍에 따라 Login
            # 버튼이 아직 화면에 나타나지 않는 경우가 있다. 이때 사이드바의
            # "Privacy and legal" 아이콘 버튼을 한 번 눌러주면 Login
            # 버튼이 나타나는 현상이 확인되어, 같은 동작을 자동으로
            # 수행한 뒤 SIGN_IN_MARKERS를 다시 조회한다.
            self._nudge_privacy_legal_button()
            btn = sel.first_match_locator(self.page, sel.SIGN_IN_MARKERS)
        if btn is None or btn.count() == 0:
            self._reveal_for_login()  # 숨긴 채로는 로그인할 수 없다.
            return False

        original_url = self.page.url
        try:
            btn.first.click()
        except Exception:
            self._reveal_for_login()
            return False

        # 로그인 모달이 뜨면 그 안의 "Continue with Google" 버튼이 나타날
        # 때까지 잠깐 기다린다. 못 찾으면 아래에서 배경 숨김으로 폴백한다.
        google_btn = self._wait_for_locator(sel.GOOGLE_LOGIN_BUTTON, timeout_ms=4000)

        popup: Optional[Page] = None
        navigated_away = False
        if google_btn is not None:
            pre_click_url = self.page.url
            pre_pages = list(self.context.pages)
            try:
                google_btn.click()
            except Exception:
                google_btn = None

            if google_btn is not None:
                # 구글 버튼 클릭이 (a) 새 팝업 창을 여는지, (b) LM Arena
                # 창이 그 자리에서 그대로 구글 로그인 페이지로 이동(같은 탭
                # 내 리다이렉트)하는지를 잠깐 동안 관찰해 구분한다. 이 판별
                # 없이 무조건 "팝업이 안 떴으니 모달만 숨긴다"고 가정하면,
                # (b)처럼 같은 탭이 실제 구글 로그인 화면으로 이미 바뀐
                # 상태에서 배경 숨김 CSS를 씌우게 되어 그 구글 로그인 화면
                # 자체가 통째로 가려져 화면이 검게 보이는 문제가 생긴다.
                deadline = time.time() + 6.0
                while time.time() < deadline:
                    new_pages = [p for p in self.context.pages if p not in pre_pages]
                    if new_pages:
                        popup = new_pages[0]
                        break
                    try:
                        current_url = self.page.url
                    except Exception:
                        current_url = pre_click_url
                    if current_url != pre_click_url:
                        navigated_away = True
                        break
                    self._pause(0.2)

        if popup is not None:
            # 구글 로그인 팝업이 실제로 떴다: 팝업을 앞으로 가져오고
            # 원래 창(자동화 창)은 빈 화면으로 바꿔 사이트가 보이지 않게 한다.
            if self._hider is not None:
                self._hider.park(popup)  # 로드가 끝날 때까지 팝업도 화면 밖에 둔다.
            try:
                popup.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            if self._hider is not None:
                # 구글 로그인 화면이 완전히 그려진 뒤에야 팝업 창을 꺼낸다.
                self._hider.reveal_when_google_ready(popup)
            try:
                popup.bring_to_front()
            except Exception:
                pass
            try:
                self.page.goto("about:blank")
            except Exception:
                pass
            try:
                popup.wait_for_event("close", timeout=popup_wait_seconds * 1000)
            except Exception:
                # 타임아웃되거나 팝업이 이미 닫혔어도 아래에서 원래 창을
                # 복원하고 정상적으로 계속 진행한다(로그인 성공 여부는
                # 이후 wait_for_login()이 다시 판별한다).
                pass
            try:
                self.page.goto(original_url, wait_until="domcontentloaded")
                self.page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            return True

        if navigated_away:
            # 팝업이 아니라 같은 탭에서 곧바로 구글 로그인 페이지로 이동한
            # 경우: 그 페이지 자체가 이미 화면 전체를 채운 로그인 화면이므로
            # 더 숨길 배경이 없다(오히려 여기서 배경 숨김 CSS를 씌우면 이
            # 구글 로그인 화면 자체가 가려져 검은 화면이 된다). 아무 것도
            # 하지 않고 그대로 둔다 - 이후 wait_for_login()이 기존 방식대로
            # 리다이렉트를 따라가며 로그인 완료 여부를 판별한다.
            if self._hider is not None:
                # 구글 로그인 화면이 완전히 그려진 뒤에야 숨겨 둔 창을 꺼낸다.
                self._revealed_for_login = True
                self._hider.reveal_when_google_ready(self.page)
            return True

        # 팝업도, 같은 탭 이동도 없었던 경우(즉 여전히 LM Arena 페이지 위에
        # 로그인 모달만 떠 있는 상태): 모달을 제외한 배경 요소를 숨겨
        # 최소한 사이트 콘텐츠라도 노출되지 않게 한다.
        if self._hider is not None:
            # 숨김 창 모드 안전장치: 배경 숨김 CSS는 "로그인 모달이 실제로 있을 때만"
            # 적용한다. 모달이 없거나(이미 구글 로그인 화면으로 이동한 경우 포함)
            # 그대로 적용하면 화면 전체가 검게 가려진다.
            try:
                on_google = urlparse(self.page.url).hostname == "accounts.google.com"
            except Exception:
                on_google = False
            if on_google:
                self._revealed_for_login = True
                self._hider.reveal_when_google_ready(self.page)
                return True
            if not self._login_modal_visible():
                self._reveal_for_login()
                return True
        self._hide_background_for_login_modal()
        self._reveal_for_login()
        return True

    def _pause(self, seconds: float) -> None:
        """대기. Playwright 이벤트(팝업/URL 변경)가 처리되도록 가능하면 Playwright 대기를 쓴다."""
        try:
            self.page.wait_for_timeout(int(seconds * 1000))
        except Exception:
            time.sleep(seconds)

    def _login_modal_visible(self) -> bool:
        assert self.page is not None
        for marker in sel.LOGIN_MODAL_MARKERS:
            try:
                loc = self.page.locator(marker)
                if loc.count() > 0 and loc.first.is_visible():
                    return True
            except Exception:
                continue
        return False

    def _reveal_for_login(self) -> None:
        """로그인 화면(모달 등)을 사용자에게 보여야 할 때 숨긴 창을 꺼낸다."""
        if self._hider is None:
            return
        self._revealed_for_login = True
        self._hider.reveal()

    def _wait_for_locator(self, selectors: list[str], timeout_ms: int = 4000):
        """
        selectors 후보 중 실제로 화면에 보이는 첫 요소를 timeout_ms 동안
        폴링해 찾는다. 못 찾으면 None.
        """
        assert self.page is not None
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            loc = sel.first_match_locator(self.page, selectors)
            if loc is not None and loc.count() > 0:
                try:
                    if loc.first.is_visible():
                        return loc.first
                except Exception:
                    pass
            time.sleep(0.2)
        return None

    def _nudge_privacy_legal_button(self) -> None:
        """
        [간헐적 버그 워크어라운드] Login 버튼(SIGN_IN_MARKERS)이 아직
        화면에 나타나지 않는 간헐적인 상태에서, 사이드바의 "Privacy and
        legal" 아이콘 버튼을 클릭하면 Login 버튼이 나타나는 현상에 대한
        대응이다. 이 버튼 자체를 못 찾거나 클릭이 실패해도 조용히
        넘어간다(호출부인 enter_login_only_view()가 이어서
        SIGN_IN_MARKERS를 다시 조회해 성공 여부를 판단하므로, 여기서
        예외를 던지거나 반환값으로 성패를 알릴 필요가 없다).
        """
        assert self.page is not None
        nudge = self._wait_for_locator(sel.PRIVACY_LEGAL_BUTTON, timeout_ms=2000)
        if nudge is None:
            return
        try:
            nudge.click()
        except Exception:
            return
        # 클릭 직후 Login 버튼이 나타날 시간을 짧게 기다려준다.
        self._wait_for_locator(sel.SIGN_IN_MARKERS, timeout_ms=3000)

    def _hide_background_for_login_modal(self) -> None:
        assert self.page is not None
        modal_selector = ", ".join(sel.LOGIN_MODAL_MARKERS)
        try:
            self.page.evaluate(
                """([styleId, modalSelector]) => {
                    if (document.getElementById(styleId)) return;
                    const style = document.createElement('style');
                    style.id = styleId;
                    style.textContent = `
                        body > *:not(script):not(style) {
                            visibility: hidden !important;
                        }
                        ${modalSelector} {
                            visibility: visible !important;
                        }
                        ${modalSelector} * {
                            visibility: visible !important;
                        }
                        body { background: #0b0b0f !important; }
                    `;
                    document.head.appendChild(style);
                }""",
                [self._LOGIN_HIDE_STYLE_ID, modal_selector],
            )
        except Exception:
            pass

    def exit_login_only_view(self) -> None:
        """
        enter_login_only_view()가 같은 페이지에 주입했을 수 있는 숨김
        스타일을 제거해 정상 화면으로 되돌린다. 팝업 경로였거나 아무것도
        주입되지 않았어도 조용히 아무 일도 하지 않는다(안전한 no-op).
        """
        if self.page is None:
            return
        try:
            self.page.evaluate(
                "(styleId) => { const el = document.getElementById(styleId); if (el) el.remove(); }",
                self._LOGIN_HIDE_STYLE_ID,
            )
        except Exception:
            pass
        # 숨김 창 모드: 로그인이 확인되면 창을 다시 숨긴다(실패/타임아웃이면 그대로 둔다).
        if self._hider is not None and self._revealed_for_login:
            self._revealed_for_login = False
            try:
                if self.is_logged_in():
                    self._hider.conceal()
            except Exception:
                pass

    # ── 로그인 완료 후 커스텀 화면 표시 (완전히 새로 추가) ────────────
    # 배경: 로그인에 성공한 뒤에도 자동화 창을 그대로 두면, 화면에 실제
    # LM Arena 채팅 UI(대화 내용 등)가 계속 노출된다. 아래 두 메서드는
    # send_prompt / iter_response_stream / parse_last_response / new_chat /
    # 모델 선택 등 실제 LM Arena DOM과 통신하는 기존 로직은 단 하나도
    # 건드리지 않고, 그 위에 "무엇이 화면에 보이는가"만 바꾸는 순수 추가
    # 기능이다.
    #
    # 핵심 아이디어: 오버레이 최상위 요소에 pointer-events:none을 줘서
    # 시각적으로는 실제 화면을 완전히 덮으면서도, 클릭/입력 대상을 찾는
    # 히트테스트(Playwright가 "이 요소가 실제로 이벤트를 받는가"를 판정할
    # 때 쓰는 document.elementFromPoint)는 오버레이를 건너뛰고 그 아래에
    # 있는 진짜 LM Arena 요소로 그대로 통과한다. 즉 화면에는 커스텀
    # "로그인 완료" 페이지만 보이지만, Playwright는 여전히 실제 입력창/
    # 전송 버튼/응답 컨테이너를 정상적으로 찾고 조작할 수 있다.
    _CONNECTED_OVERLAY_ID = "lmarena-cli-connected-overlay"
    _CONNECTED_OVERLAY_STYLE_ID = "lmarena-cli-connected-overlay-style"

    # 브랜드 톤(web_static/styles.css의 --ink #141413 / --success #2c8f4f /
    # 순수 블랙&화이트 팔레트, "LM" 박스 로고)을 그대로 재사용해 CLI/웹앱과
    # 통일된 느낌을 주는 자체 디자인 완료 화면.
    # [디자인 전면 개편] 기존 화이트 테마/브랜드 톤(#141413, #2c8f4f)과
    # 오버레이 주입 방식(pointer-events:none 히트테스트 통과, 동일한
    # overlayId/styleId, show_connected_overlay/_inject_connected_overlay의
    # 호출 계약)은 그대로 두고, 화면 내용만 다시 그린다.
    # - 불필요했던 2줄 설명 문구는 제거하고 로고 / 체크 아이콘 / "로그인
    #   완료" / "연결됨" 상태만 남겨 최대한 심플하게 정리.
    # - 배경에는 <canvas>로 그린, 천천히 회전하는 3D 네트워크(구 표면에
    #   분포한 점들을 가까운 점끼리 선으로 연결)를 그려 "연결되었다"는
    #   느낌을 시각적으로 표현.
    # - 중심부는 선명하게, 가장자리로 갈수록 backdrop-filter blur +
    #   radial-gradient 마스크로 흐려지는 포커스 블러 효과를 줘서 과하지
    #   않게 깔끔한 분위기를 연출.
    _CONNECTED_OVERLAY_JS = r"""
    ([overlayId, styleId]) => {
        if (document.getElementById(overlayId)) return;

        const style = document.createElement('style');
        style.id = styleId;
        style.textContent = `
            @keyframes lmarena-cli-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }
            @keyframes lmarena-cli-fade { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
            #${overlayId} .lmarena-cli-dot { animation: lmarena-cli-pulse 1.6s ease-in-out infinite; }
            #${overlayId} .lmarena-cli-card { animation: lmarena-cli-fade .5s ease-out; }
        `;
        document.head.appendChild(style);

        const overlay = document.createElement('div');
        overlay.id = overlayId;
        overlay.setAttribute('style', [
            'position:fixed', 'inset:0', 'z-index:2147483647', 'pointer-events:none',
            'background:#ffffff', 'overflow:hidden',
            "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Pretendard','Apple SD Gothic Neo',sans-serif",
        ].join(';'));
        overlay.innerHTML = `
            <canvas id="${overlayId}-net" style="position:absolute;inset:0;width:100%;height:100%;display:block;"></canvas>
            <div style="position:absolute;inset:0;
                 backdrop-filter:blur(9px);-webkit-backdrop-filter:blur(9px);
                 -webkit-mask-image:radial-gradient(circle at 50% 46%, transparent 0%, transparent 26%, #000 62%);
                 mask-image:radial-gradient(circle at 50% 46%, transparent 0%, transparent 26%, #000 62%);"></div>
            <div style="position:absolute;inset:0;
                 background:radial-gradient(circle at 50% 46%, rgba(255,255,255,0) 0%, rgba(255,255,255,.5) 76%, rgba(255,255,255,.9) 100%);"></div>
            <div class="lmarena-cli-card" style="position:relative;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px;text-align:center;">
              <div style="width:52px;height:52px;border-radius:50%;background:#e9f6ee;display:flex;align-items:center;justify-content:center;">
                <svg width="26" height="26" viewBox="0 0 24 24" fill="none">
                  <path d="M5 13l4 4L19 7" stroke="#2c8f4f" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
              </div>
              <div style="font-size:18px;font-weight:600;color:#141413;letter-spacing:-.01em;">로그인 완료</div>
              <div style="display:flex;align-items:center;gap:6px;">
                <span class="lmarena-cli-dot" style="width:6px;height:6px;border-radius:50%;background:#2c8f4f;display:inline-block;"></span>
                <span style="font-size:11.5px;color:#8f8f8a;letter-spacing:.02em;">연결됨</span>
              </div>
            </div>
        `;
        document.body.appendChild(overlay);

        // ── 3D 네트워크 배경 애니메이션 ───────────────────────────
        // 점을 구 표면이 아니라 화면 전체를 채우는 3D 공간(가로/세로/깊이
        // 방향 큐브 볼륨) 안에 흩뿌려 놓고, 매 프레임 Y/X축으로 살짝씩
        // 회전시켜 원근 투영한 뒤, 화면상 거리가 가까운 점끼리만 선으로
        // 이어 "네트워크가 연결된" 느낌을 낸다. 선/점 모두 기존 브랜드
        // 잉크 블랙(#141413)만 낮은 알파로 사용해 화이트 테마를 해치지
        // 않는다.
        const canvas = overlay.querySelector(`#${overlayId}-net`);
        const ctx = canvas.getContext('2d');
        let W = 0, H = 0;
        const DPR = Math.min(window.devicePixelRatio || 1, 2);

        function resize() {
            W = window.innerWidth;
            H = window.innerHeight;
            canvas.width = W * DPR;
            canvas.height = H * DPR;
            canvas.style.width = W + 'px';
            canvas.style.height = H + 'px';
            ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
        }
        resize();
        window.addEventListener('resize', resize);

        // 화면 전체(가로/세로)에 고르게 퍼지도록 -1..1 범위의 큐브 볼륨에
        // 점을 랜덤 배치한다(구 표면 제약 없음 = 배경 전체를 채우는 3D 필드).
        const N = 90;
        const pts = [];
        for (let i = 0; i < N; i++) {
            pts.push({
                x: (Math.random() * 2 - 1),
                y: (Math.random() * 2 - 1),
                z: (Math.random() * 2 - 1),
            });
        }

        let angleY = 0;
        let raf = null;

        function project(p, angleX, angleY) {
            const cosY = Math.cos(angleY), sinY = Math.sin(angleY);
            const x1 = p.x * cosY - p.z * sinY;
            const z1 = p.x * sinY + p.z * cosY;
            const cosX = Math.cos(angleX), sinX = Math.sin(angleX);
            const y1 = p.y * cosX - z1 * sinX;
            const z2 = p.y * sinX + z1 * cosX;
            const persp = 2.2 / (2.2 - z2 * 0.9);
            const spreadX = window.innerWidth * 0.62;
            const spreadY = window.innerHeight * 0.62;
            return {
                sx: window.innerWidth / 2 + x1 * spreadX * persp,
                sy: window.innerHeight / 2 + y1 * spreadY * persp,
                z: z2,
                persp: persp,
            };
        }

        function draw() {
            ctx.clearRect(0, 0, W, H);
            const angleX = 0.18 + Math.sin(angleY * 0.5) * 0.06;
            const proj = pts.map((p) => project(p, angleX, angleY));
            const linkDist = Math.min(window.innerWidth, window.innerHeight) * 0.22;

            for (let i = 0; i < proj.length; i++) {
                for (let j = i + 1; j < proj.length; j++) {
                    const dx = proj[i].sx - proj[j].sx, dy = proj[i].sy - proj[j].sy;
                    const dist = Math.hypot(dx, dy);
                    if (dist < linkDist) {
                        const depth = (proj[i].z + proj[j].z) / 2;
                        const alpha = Math.max(0, 0.16 - dist / (linkDist * 6)) * (0.6 + depth * 0.4);
                        ctx.strokeStyle = `rgba(20,20,19,${Math.max(0, Math.min(0.14, alpha))})`;
                        ctx.lineWidth = 1;
                        ctx.beginPath();
                        ctx.moveTo(proj[i].sx, proj[i].sy);
                        ctx.lineTo(proj[j].sx, proj[j].sy);
                        ctx.stroke();
                    }
                }
            }
            for (const p of proj) {
                const size = 1.5 * p.persp;
                const depthAlpha = 0.25 + (p.z + 1) / 2 * 0.5;
                ctx.beginPath();
                ctx.fillStyle = `rgba(20,20,19,${depthAlpha * 0.5})`;
                ctx.arc(p.sx, p.sy, size, 0, Math.PI * 2);
                ctx.fill();
            }

            angleY += 0.0016;
            raf = requestAnimationFrame(draw);
        }
        draw();

        // 문서가 교체되며 overlay 노드가 DOM에서 사라지면 애니메이션과
        // 리스너를 정리한다(메모리/CPU 누수 방지). 새 문서 로드 시에는
        // show_connected_overlay()가 건 'load' 리스너가 다시 주입한다.
        const mo = new MutationObserver(() => {
            if (!document.body.contains(overlay)) {
                if (raf) cancelAnimationFrame(raf);
                window.removeEventListener('resize', resize);
                mo.disconnect();
            }
        });
        mo.observe(document.body, { childList: true });
    }
    """

    def show_connected_overlay(self) -> None:
        """
        로그인 성공 직후(이미 로그인되어 있던 시작 시점 포함) 호출한다.
        이후 자동화 창에는 실제 LM Arena 화면 대신 직접 디자인한 "로그인
        완료" 커스텀 화면만 계속 보이게 된다.

        모드 전환(goto_mode)이나 새 채팅처럼 페이지가 다시 로드될 때도
        오버레이가 사라지지 않도록, self.page의 'load' 이벤트에 한 번만
        리스너를 걸어 로드될 때마다 자동으로 다시 주입한다. 실제 DOM
        요소(입력창/전송 버튼/응답 컨테이너 등)는 전혀 손대지 않으므로,
        그 요소들을 찾아 조작하는 기존 메서드들은 이 오버레이와 무관하게
        평소와 동일하게 동작한다.
        """
        assert self.page is not None
        # 디버그 모드(config.DEBUG_MODE=True)에서는 오버레이를 아예 주입하지
        # 않는다. _connected_overlay_active가 False로 남아 있으므로
        # 페이지 로드 시 재주입 리스너도 아무 일도 하지 않는다.
        if DEBUG_MODE:
            return
        self._connected_overlay_active = True
        self._inject_connected_overlay()
        if not self._connected_overlay_listener_attached:
            self.page.on("load", lambda _p=None: self._inject_connected_overlay())
            self._connected_overlay_listener_attached = True

    def _inject_connected_overlay(self) -> None:
        if not self._connected_overlay_active or self.page is None:
            return
        if self._hider is not None and self._hider.hidden:
            return  # 숨겨진 창에는 볼 사람이 없다: 캔버스 애니메이션으로 CPU를 쓰지 않는다.
        try:
            self.page.evaluate(
                self._CONNECTED_OVERLAY_JS,
                [self._CONNECTED_OVERLAY_ID, self._CONNECTED_OVERLAY_STYLE_ID],
            )
        except Exception:
            pass

    # ── 슬래시 커맨드 지원 (/new, /clear) ────────────────────────────
    # 아래 두 메서드는 기존 로그인/전송/스트리밍 판정 로직에는 전혀
    # 관여하지 않는 순수 추가 기능이다. 기존 메서드/셀렉터는 건드리지
    # 않고, 새로 정의된 NEW_CHAT_BUTTON 셀렉터와 기존 goto_direct_mode()만
    # 재사용한다.
    def new_chat(self) -> bool:
        """
        사이드바의 'New Chat' 버튼을 클릭해 새로운 채팅 세션을 시작한다.
        버튼을 찾지 못하면 False를 반환한다(호출부에서 경고 처리).
        """
        assert self.page is not None
        btn = sel.first_match_locator(self.page, sel.NEW_CHAT_BUTTON)
        if btn is None or btn.count() == 0:
            return False
        try:
            btn.first.click()
            self.page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        time.sleep(0.5)
        # 새 세션이므로 이전 턴의 추론 트래킹 상태를 초기화한다.
        self._reasoning_baseline = frozenset()
        self._reasoning_start_time = None
        return True

    # ── 프롬프트 전송 ─────────────────────────────────────────────
    def _snapshot_container_texts(self) -> frozenset[str]:
        """
        현재 페이지에 존재하는 모든 답변(prose) 컨테이너의 '정규화된 텍스트'
        집합을 스냅샷으로 찍는다.

        인덱스/개수가 아니라 텍스트 내용 자체로 비교하는 이유: LM Arena가
        새 메시지를 리스트 뒤쪽(끝)이 아니라 앞쪽(맨 위)에 추가하는 방식일
        수도 있어서, "개수가 늘어난 만큼 뒤쪽 인덱스를 본다"는 가정이
        깨지면 이전 답변을 새 답변으로 잘못 집어오는 문제가 생긴다.
        텍스트 집합 비교는 삽입 순서(앞/뒤)에 관계없이 항상 정확하다.
        """
        assert self.page is not None
        loc = sel.first_match_locator(self.page, sel.RESPONSE_CONTAINER)
        if loc is None:
            return frozenset()
        try:
            count = loc.count()
        except Exception:
            return frozenset()

        texts = set()
        for i in range(count):
            try:
                raw = loc.nth(i).inner_text()
            except Exception:
                continue
            # 아래 _find_new_answer_container에서 후보 텍스트를 비교할 때와
            # 동일한 정규화(_normalize_for_echo_compare)를 반드시 써야 한다.
            # 정규화 방식이 서로 다르면 "전송 전부터 있던 컨테이너"조차
            # baseline_texts에 없는 것으로 오판되어 이전 턴의 답변을 새
            # 답변으로 잘못 집어오는 문제가 생긴다.
            normalized = self._normalize_for_echo_compare(raw)
            if normalized:
                texts.add(normalized)
        return frozenset(texts)

    # ── 입력창 채우기 (textarea / contenteditable 모두 지원) ──────────
    # LM Arena가 입력창을 <textarea>에서 TipTap(ProseMirror) contenteditable로
    # 교체하면서 기존 fill() 경로만으로는 부족해졌다. 두 가지를 모두 다룬다.
    #
    # [왜 contenteditable에 paste 이벤트를 쓰는가 - 속도 문제]
    # Playwright의 fill()은 contenteditable에서도 동작하기는 하지만, 글자 수가
    # 많아지면 급격히 느려진다(실측: 약 5천 자 0.1초, 5만 자 6.3초, 20만 자는
    # 30초 타임아웃으로 아예 실패). 워크스페이스 공유를 켜면 프롬프트가
    # 수십만 자에 이를 수 있으므로(workspace.py의 상한이 50만 자), fill()로는
    # 전송 자체가 불가능해진다. 반면 ProseMirror가 원래 붙여넣기를 처리하는
    # 경로(paste 이벤트에 text/plain을 실어 보내기)를 쓰면 같은 내용이
    # 5만 자 0.15초, 180만 자 1.5초에 들어간다(실측). 그래서 기본 경로를
    # paste로 두고, 혹시 실패하면 기존 fill()로 폴백한다.
    #
    # 붙여넣기 전 Ctrl+A로 기존 내용을 선택해 두는 이유: paste는 "선택 영역을
    # 대체"하는 동작이라, 선택하지 않으면 이전 턴에 남아 있던 텍스트 뒤에
    # 덧붙어 버린다(실측 확인). Ctrl+A는 포커스가 편집기 안에 있을 때
    # ProseMirror가 자체 selectAll로 처리하므로 편집기 내부로만 적용된다.
    _PASTE_TEXT_JS = """
    (el, text) => {
        el.focus();
        const dt = new DataTransfer();
        dt.setData('text/plain', text);
        el.dispatchEvent(new ClipboardEvent('paste', {
            clipboardData: dt, bubbles: true, cancelable: true,
        }));
    }
    """

    @staticmethod
    def _is_content_editable(locator) -> bool:
        try:
            return bool(locator.evaluate("(el) => el.isContentEditable === true"))
        except Exception:
            return False

    def _fill_prompt_input(self, locator, prompt: str) -> None:
        """입력창에 prompt를 채운다(기존 내용은 모두 대체된다)."""
        if not self._is_content_editable(locator):
            # 기존 textarea 경로 - 동작/호출 순서 모두 그대로 유지한다.
            locator.click()
            locator.fill(prompt)
            return

        locator.click()
        try:
            # 편집기 안의 기존 내용을 모두 선택해 paste가 "대체"가 되게 한다.
            self.page.keyboard.press("Control+A")
        except Exception:
            pass
        try:
            locator.evaluate(self._PASTE_TEXT_JS, prompt)
        except Exception:
            locator.fill(prompt)  # paste가 막힌 환경을 위한 폴백
            return

        # paste가 실제로 반영됐는지 확인하고, 비어 있으면 fill()로 폴백한다.
        try:
            landed = (locator.inner_text() or "").strip()
        except Exception:
            landed = ""
        if not landed and prompt.strip():
            locator.fill(prompt)

    def send_prompt(self, prompt: str) -> frozenset[str]:
        """
        프롬프트를 전송하고, 전송 '직전' 시점의 모든 답변 컨테이너 텍스트
        스냅샷(baseline_texts)을 반환한다. 이 값을 wait_for_response_complete
        / parse_last_response에 그대로 넘겨야 새/구 답변을 정확히 구분할 수
        있다.
        """
        assert self.page is not None
        # 이전 턴에서 통과되지 않고 남아 있는 로봇 확인 UI가 있으면 입력창이
        # 가려져 전송이 실패하므로, 먼저 통과될 때까지 기다린다(없으면 즉시 통과).
        if self._captcha is not None:
            try:
                # 숨김 창 모드: 남아 있는 확인 UI가 있으면 먼저 창을 꺼낸 뒤 기다린다.
                self._captcha_tick()
                self._captcha.wait_until_clear(max_seconds=300.0)
            except Exception:
                pass
            self._sync_captcha_window(self._captcha.active)
        baseline_texts = self._snapshot_container_texts()
        # 추론 미리보기용 베이스라인도 함께 리셋한다 (이전 턴의 추론 로그를
        # 새 턴의 추론으로 착각하지 않도록). 기존 answer 관련 로직/반환값은
        # 그대로 유지한 채 별도로만 추가한다.
        self._reasoning_baseline = self._snapshot_reasoning_texts()
        self._reasoning_start_time = None

        textarea = sel.first_match_locator(self.page, sel.PROMPT_TEXTAREA)
        if textarea is None or textarea.count() == 0:
            raise RuntimeError("프롬프트 입력창을 찾을 수 없습니다 (DOM 변경 가능성).")

        # textarea든 contenteditable(TipTap/ProseMirror)든 동일하게 처리한다.
        self._fill_prompt_input(textarea.first, prompt)

        # Enter로 우선 전송 시도, 실패하면 전송 버튼 폴백
        try:
            textarea.first.press("Enter")
        except Exception:
            send_btn = sel.first_match_locator(self.page, sel.SEND_BUTTON)
            if send_btn and send_btn.count() > 0:
                send_btn.first.click()
            else:
                raise RuntimeError("전송 버튼도 찾을 수 없습니다 (DOM 변경 가능성).")

        return baseline_texts

    # ── 응답 대기 및 파싱 ─────────────────────────────────────────
    def is_generating(self) -> bool:
        assert self.page is not None
        for marker in sel.GENERATING_INDICATOR:
            try:
                loc = self.page.locator(marker)
                if loc.count() > 0 and loc.first.is_visible():
                    return True
            except Exception:
                continue
        return False

    def stop_generation(self) -> bool:
        """
        현재 생성 중인 LM Arena 답변을 즉시 중단시킨다 ("Stop generation"
        버튼 클릭). 버튼이 화면에 없거나(이미 생성이 끝났거나 DOM 변경)
        클릭에 실패하면 False를 반환한다. 기존 is_generating()/
        GENERATING_INDICATOR 판정 로직은 전혀 건드리지 않는다.
        """
        assert self.page is not None
        btn = sel.first_match_locator(self.page, sel.STOP_GENERATION_BUTTON)
        if btn is None or btn.count() == 0:
            return False
        try:
            if not btn.first.is_visible():
                return False
            btn.first.click()
            return True
        except Exception:
            return False

    def _find_new_answer_container(self, baseline_texts: frozenset[str], sent_prompt: str):
        """
        현재 페이지의 모든 답변 컨테이너를 순서(인덱스)에 상관없이 전부
        훑어서, baseline_texts(전송 전 스냅샷)에 없던 '새로운 텍스트'를 가진
        컨테이너를 찾는다. 그중에서도 방금 내가 보낸 프롬프트와 내용이
        동일한 컨테이너(사용자 메시지 에코)는 답변이 아니므로 제외한다.

        인덱스 기반이 아니라 텍스트 집합 기반으로 비교하므로, 새 메시지가
        리스트의 앞/뒤 어느 쪽에 추가되든 상관없이 항상 정확하게 새 답변만
        찾아낸다.

        판정은 "완전히 동일한 문자열인가"라는 단순 비교를 쓰되, 이 조건에
        걸리는 컨테이너 중 **가장 먼저 발견된 것 하나만** 사용자 메시지
        에코로 간주해 제외하고, 그 이후에 나오는(=완전히 같은 내용을 가진)
        컨테이너는 실제 답변 후보로 그대로 남겨둔다.

        왜 이렇게 해야 하는가: 예를 들어 "지금부터 내가 하는 말을 토씨
        하나 틀리지 않고 그대로 따라 해"처럼, AI의 진짜 답변이 프롬프트와
        완전히 동일한 문자열이 되는 경우가 실제로 있다. 이때 화면에는
        (1) 내가 보낸 메시지의 에코 컨테이너와 (2) AI가 그대로 따라 한
        진짜 답변 컨테이너, 이렇게 프롬프트와 내용이 동일한 컨테이너가
        "새로 생긴 것" 중에 두 개 존재하게 된다. 만약 "프롬프트와 같은
        텍스트는 전부 제외"해버리면 진짜 답변까지 함께 사라져 버린다.
        반대로 "에코 컨테이너는 정확히 1개만 존재한다"는 사실을 이용해
        가장 먼저 만난 것 하나만 에코로 치고 넘어가면, 그 다음에 나오는
        (내용이 같은) 두 번째 컨테이너는 정상적으로 답변 후보에 포함된다.

        (유사도/접두어 기반의 느슨한 비교는 쓰지 않는다 — 그렇게 하면
        AI의 실제 답변이 프롬프트와 거의 비슷하기만 해도 에코로 오인해
        숨겨버리는 부작용이 있었다.) 비교 대상인 sent_prompt는
        `_prompt_text_for_echo_compare()`로 한 번 정규화해서 넘겨받는다 —
        RESPONSE_CONTAINER(div.prose)가 마크다운 렌더러를 통해 그려지기
        때문에, 사용자 메시지도 같은 방식으로 렌더링되면서 "#", "**",
        "```" 같은 마크다운 기호가 화면에서는 사라진다. 그 결과 원본
        프롬프트 문자열과 화면에 실제로 렌더링된 에코 텍스트가 (내용은
        같아도) 글자 단위로는 달라져서 exact-match가 실패하는 문제가
        있었다. 자세한 설명은 `_prompt_text_for_echo_compare()` 참고.
        """
        assert self.page is not None
        loc = sel.first_match_locator(self.page, sel.RESPONSE_CONTAINER)
        if loc is None:
            return None
        try:
            count = loc.count()
        except Exception:
            return None

        normalized_prompt = self._prompt_text_for_echo_compare(sent_prompt)
        candidates = []
        prompt_echo_already_skipped = False
        for i in range(count):
            candidate = loc.nth(i)
            try:
                text = candidate.inner_text()
            except Exception:
                continue
            normalized_text = self._normalize_for_echo_compare(text)
            if not normalized_text:
                continue
            if normalized_text in baseline_texts:
                continue  # 이미 전송 전부터 있던(=이전 턴의) 내용

            # 1차: 정규화 후 완전 일치. 2차(폴백): 긴 붙여넣기 텍스트에서
            # exact-match가 실패하는 경우(축약 렌더링/잔여 문자 차이)를
            # 대비한 접두어·고유사도 기반 판정. _looks_like_prompt_echo는
            # 충분히 긴 텍스트에 대해서만 동작하므로, 우연히 프롬프트
            # 일부와 겹치는 짧은 실제 답변을 에코로 오인하지 않는다.
            is_prompt_echo = normalized_text == normalized_prompt
            if not is_prompt_echo:
                is_prompt_echo = self._looks_like_prompt_echo(normalized_text, normalized_prompt)

            if is_prompt_echo:
                if not prompt_echo_already_skipped:
                    prompt_echo_already_skipped = True
                    continue  # 내가 보낸 프롬프트의 에코 (딱 1개만 이렇게 넘어간다)
                # 두 번째부터는 "내용이 우연히/의도적으로 프롬프트와 같은
                # 진짜 답변"일 수 있으므로 후보에서 제외하지 않는다.
            candidates.append((len(normalized_text), candidate))

        if not candidates:
            return None

        # 여러 개가 후보로 잡히면(드문 경우) 가장 긴 텍스트를 답변으로 간주한다.
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        return candidates[0][1]

    # 마크다운 문법 기호를 대략적으로 제거하기 위한 정규식들.
    # RESPONSE_CONTAINER(div.prose)는 마크다운을 렌더링하는 컨테이너이고,
    # LM Arena는 사용자 메시지도 같은 컨테이너/렌더러로 그린다. 그래서
    # 사용자가 보낸 원문에 마크다운 문법이 섞여 있으면(제목 #, 굵게 **,
    # 코드블록 ``` , 목록 -/*, 인용 >, 구분선 --- 등), 화면에 실제로
    # 렌더링된 텍스트에서는 이 기호들이 사라진 채로 나타난다. 짧은 문장을
    # 직접 타이핑할 때는 이런 마크다운 문법을 쓰는 경우가 거의 없지만,
    # 문서/코드/노트 등을 붙여넣을 때는 흔히 섞여 있어서, 유독 "붙여넣은
    # 긴 텍스트"에서만 프롬프트 원문과 화면 표시 내용이 달라져 에코 판정이
    # 실패하는 문제가 있었다 — 이번 문제의 진짜 원인이다.
    _MD_CODE_FENCE_RE = re.compile(r"```[^\n`]*\n?|```")
    _MD_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
    _MD_IMAGE_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
    _MD_HEADER_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*", re.MULTILINE)
    _MD_BLOCKQUOTE_RE = re.compile(r"^[ \t]{0,3}>[ \t]?", re.MULTILINE)
    _MD_LIST_MARKER_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+", re.MULTILINE)
    _MD_HR_RE = re.compile(r"^[ \t]{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$", re.MULTILINE)
    _MD_EMPHASIS_RE = re.compile(r"(\*\*\*|\*\*|\*|___|__|_)")

    @classmethod
    def _strip_markdown_syntax(cls, text: str) -> str:
        """
        마크다운 소스 문자열에서 렌더링 시 사라지는 문법 기호만 제거해,
        '화면에 실제로 보일 텍스트'에 가깝게 만든다. 완벽한 마크다운
        파서는 아니지만, 에코 비교 목적으로는 이 정도로 충분하다.
        """
        text = cls._MD_CODE_FENCE_RE.sub("", text)
        text = cls._MD_IMAGE_LINK_RE.sub(r"\1", text)
        text = cls._MD_INLINE_CODE_RE.sub(r"\1", text)
        text = cls._MD_HEADER_RE.sub("", text)
        text = cls._MD_BLOCKQUOTE_RE.sub("", text)
        text = cls._MD_LIST_MARKER_RE.sub("", text)
        text = cls._MD_HR_RE.sub("", text)
        text = cls._MD_EMPHASIS_RE.sub("", text)
        return text

    # ── 에코 비교 정밀화 (긴 붙여넣기 텍스트 대응) ───────────────────
    # 문제: 짧은 텍스트를 직접 타이핑할 때는 원문과 화면 렌더링 결과가
    # 글자 단위로 정확히 일치해 exact-match 비교(위 _strip_markdown_syntax +
    # 단순 whitespace 정규화)로 충분했다. 그런데 "긴 텍스트를 붙여넣어"
    # 전송하면, 그 텍스트가 외부 문서/웹페이지 등에서 온 경우가 많아
    # 스마트 따옴표(" "), 특수 대시(– —), 말줄임표(…), 줄바꿈 없는 공백,
    # zero-width 문자, 유니코드 결합 문자 등이 흔히 섞여 있다. 이런
    # 문자는 브라우저가 화면에 그대로(혹은 미세하게 다르게) 표시하는데,
    # 우리 쪽 비교 문자열은 이를 정규화하지 않아 글자 단위로 어긋나
    # exact-match가 실패했다. 그 결과 "내가 보낸 프롬프트의 에코"가
    # 에코로 인식되지 못하고 그대로 "새로운 답변 후보"로 남아, 아직 실제
    # 답변이 짧거나 비어있는 동안에는 (candidates 중 가장 긴 텍스트를
    # 채택하는 로직 때문에) 에코 자체가 답변으로 오인되어 화면에
    # 나타났다 — 이것이 "답변 박스에 보낸 프롬프트가 그대로 나타나는"
    # 치명적 버그의 원인이다. 실제 답변이 자라나 에코보다 길어지면
    # 후보 정렬에서 역전되어 정상 답변으로 교체되므로, "답변이 시작되면
    # 저절로 해결된다"는 증상과도 정확히 일치한다.
    #
    # 해결: exact-match 비교 자체는 그대로 유지하되(기존 로직/셀렉터
    # 변경 없음), 비교 직전 양쪽 문자열에 공통으로 적용하는 정규화
    # 단계를 추가해 위와 같은 문자 단위 불일치를 흡수한다. 그래도 남는
    # 경우(예: 매우 긴 메시지를 UI가 앞부분만 보여주는 축약 렌더링)를
    # 대비해 접두어/고유사도 기반 폴백도 함께 둔다.
    _ECHO_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")
    _ECHO_TYPOGRAPHIC_MAP = str.maketrans({
        "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u2032": "'",
        "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
        "\u2013": "-", "\u2014": "-", "\u2212": "-",
        "\u2026": "...",
        "\u00a0": " ",
    })

    @classmethod
    def _normalize_for_echo_compare(cls, text: str) -> str:
        """
        화면에 렌더링된 텍스트와 우리가 보낸 원문을 비교하기 직전에,
        양쪽 모두에 공통으로 적용하는 마지막 정규화 단계.

        - 유니코드 NFKC 정규화: 조합 문자/호환 문자 형태 차이를 없앤다.
        - zero-width 문자 제거: 붙여넣기 출처에 흔히 섞여 있으나 화면에는
          보이지 않는다.
        - 스마트 따옴표/대시/말줄임표/줄바꿈 없는 공백을 일반 문자로
          치환: 원본 문서나 브라우저의 자동 타이포그래피 치환으로 인해
          원문과 화면 표시가 문자 단위로 달라지는 것을 방지한다.
        """
        normalized = unicodedata.normalize("NFKC", text)
        normalized = cls._ECHO_ZERO_WIDTH_RE.sub("", normalized)
        normalized = normalized.translate(cls._ECHO_TYPOGRAPHIC_MAP)
        return " ".join(normalized.split()).strip()

    @classmethod
    def _looks_like_prompt_echo(cls, candidate_text: str, prompt_text: str) -> bool:
        """
        정규화 이후에도 화면 텍스트가 원문과 "완전히 동일"하지는 않지만
        사실상 내가 보낸 프롬프트의 에코라고 볼 수 있는 두 가지 경우를
        추가로 흡수한다 (exact-match가 실패했을 때만 호출되는 폴백).

        1) 접두어(미리보기/축약 렌더링) 매칭: 일부 UI는 매우 긴 사용자
           메시지를 전체 그대로 그리지 않고 앞부분만 보여줄 수 있다.
           이 경우 화면 텍스트는 원문의 정확한 접두어가 된다.
        2) 고유사도 매칭: 정규화로 흡수되지 않는 미세한 문자 차이가
           남는 경우, 매우 높은 유사도(0.95 이상)를 기준으로 흡수한다.

        두 경우 모두 "충분히 긴(=의미 있는 분량의)" 텍스트에 대해서만
        적용해, 우연히 프롬프트 일부와 비슷한 짧은 실제 답변까지 에코로
        오인하는 일이 없도록 한다.
        """
        if not candidate_text or not prompt_text:
            return False

        if len(candidate_text) >= 40 and prompt_text.startswith(candidate_text):
            return True

        if len(candidate_text) >= 40 or len(prompt_text) >= 40:
            ratio = difflib.SequenceMatcher(None, candidate_text, prompt_text).quick_ratio()
            if ratio >= 0.95:
                return True

        return False

    @classmethod
    def _prompt_text_for_echo_compare(cls, sent_prompt: str) -> str:
        """
        sent_prompt(내가 실제로 전송한 원문 문자열)를, 화면에 렌더링된
        사용자 메시지 에코와 비교하기 좋은 형태로 정규화한다: 마크다운
        문법 기호를 제거한 뒤 _normalize_for_echo_compare로 유니코드/
        타이포그래피/공백 차이까지 흡수한다.
        """
        stripped = cls._strip_markdown_syntax(sent_prompt)
        return cls._normalize_for_echo_compare(stripped)

    # ── 추론(reasoning) 미리보기 ─────────────────────────────────────
    # 일반 텍스트/코드 블록 스트리밍(RESPONSE_CONTAINER 기반)과는 완전히
    # 별개의 트래킹이다. 기존 답변 파싱/완료 판정 로직은 전혀 건드리지
    # 않고, 순수하게 "추론 로그 미리보기 + 경과 시간"만 별도로 관찰한다.
    def _snapshot_reasoning_texts(self) -> frozenset[str]:
        assert self.page is not None
        loc = sel.first_match_locator(self.page, sel.REASONING_TEXT)
        if loc is None:
            return frozenset()
        try:
            count = loc.count()
        except Exception:
            return frozenset()

        texts = set()
        for i in range(count):
            try:
                raw = loc.nth(i).inner_text()
            except Exception:
                continue
            normalized = " ".join(raw.split()).strip()
            if normalized:
                texts.add(normalized)
        return frozenset(texts)

    def _find_new_reasoning_container(self):
        """
        베이스라인(전송 전 스냅샷)에 없던 새로운 추론 텍스트 컨테이너를
        찾는다. _find_new_answer_container와 동일한 텍스트 집합 비교
        방식을 사용하되, 완전히 별도의 셀렉터/베이스라인을 사용한다.
        """
        assert self.page is not None
        loc = sel.first_match_locator(self.page, sel.REASONING_TEXT)
        if loc is None:
            return None
        try:
            count = loc.count()
        except Exception:
            return None

        candidates = []
        for i in range(count):
            candidate = loc.nth(i)
            try:
                text = candidate.inner_text()
            except Exception:
                continue
            normalized = " ".join(text.split()).strip()
            if not normalized:
                continue
            if normalized in self._reasoning_baseline:
                continue
            candidates.append((len(normalized), candidate))

        if not candidates:
            return None

        candidates.sort(key=lambda pair: pair[0], reverse=True)
        return candidates[0][1]

    def _extract_reasoning_text(self, node) -> str:
        try:
            return node.inner_text().strip()
        except Exception:
            return ""

    def wait_for_response_complete(
        self,
        baseline_count: int,
        sent_prompt: str,
        timeout_seconds: int = 300,
        stable_checks: int = 3,
        poll_interval: float = 1.0,
        new_message_timeout: float = 60.0,
    ) -> bool:
        """
        1) baseline_count 이후 생긴 컨테이너 중 '내 프롬프트 에코가 아닌'
           실제 답변 컨테이너가 나타날 때까지 먼저 기다리고
        2) '생성 중' 인디케이터가 사라지고
        3) 그 답변의 텍스트 길이가 연속 stable_checks회 동일하게 유지되면
           완료로 판단한다.
        """
        deadline = time.time() + timeout_seconds
        new_message_deadline = time.time() + new_message_timeout

        with ui.spinner("LM Arena 응답 대기 중..."):
            # 1단계: 프롬프트 에코가 아닌 실제 답변 컨테이너가 생길 때까지 대기
            while time.time() < new_message_deadline:
                if self._captcha_tick():
                    # 로봇 확인 대기 시간은 응답 타임아웃에서 제외한다.
                    new_message_deadline = time.time() + new_message_timeout
                    deadline = time.time() + timeout_seconds
                    time.sleep(poll_interval)
                    continue
                if self._find_new_answer_container(baseline_count, sent_prompt) is not None:
                    break
                time.sleep(poll_interval)
            else:
                return False  # 새 답변이 아예 시작되지 않음

            # 2단계: 생성 중 표시가 사라지고 텍스트가 안정될 때까지 대기
            last_len = -1
            stable_count = 0
            while time.time() < deadline:
                if self._captcha_tick():
                    deadline = time.time() + timeout_seconds
                    stable_count = 0
                    time.sleep(poll_interval)
                    continue
                generating = self.is_generating()
                container = self._find_new_answer_container(baseline_count, sent_prompt)
                text_len = 0
                if container is not None:
                    try:
                        text_len = len(container.inner_text())
                    except Exception:
                        text_len = last_len

                if not generating and text_len == last_len and text_len > 0:
                    stable_count += 1
                    if stable_count >= stable_checks:
                        return True
                else:
                    stable_count = 0

                last_len = text_len
                time.sleep(poll_interval)
        return False

    # ── 실시간 스트리밍 (일반 텍스트 + 코드 블록 모두) ─────────────────
    # 컨테이너 안에서 (전체 텍스트 / 코드 제외 일반 텍스트 / 코드 블록들)을
    # 브라우저 쪽에서 "한 번의" JS 실행으로 원자적으로 계산해서 반환한다.
    # 예전에는 Python 쪽에서 컨테이너 전체 텍스트를 가져온 뒤 각 코드
    # 블록 텍스트를 "따로" 다시 조회해서 문자열 replace()로 빼는 방식이었는데,
    # 이 두 번의 조회 사이에도 실시간 스트리밍으로 DOM이 계속 바뀌면서 서로
    # 다른 시점의 스냅샷이 섞여 replace()가 실패 -> 코드 텍스트가 일반 텍스트
    # 안에 잠깐 노출되는 깜빡임이 발생했다. 아래처럼 한 번의 evaluate 호출
    # 안에서 전부 계산하면 이 레이스 컨디션이 사라진다. 사용하는 셀렉터는
    # dom_selectors.py에 정의된 것을 그대로 전달만 하며, 판정 로직/셀렉터
    # 자체는 변경하지 않는다.
    _STREAM_EXTRACT_JS = """
    (node, args) => {
        const { containerSelectors, langSelectors, codeSelectors } = args;
        // nameSelectors는 Agent 모드에서만 채워진다(artifact 카드 헤더의
        // 파일명). Direct 모드에서는 빈 배열이라 아래 루프가 즉시 끝나고
        // filename은 항상 null이므로 기존 동작과 완전히 동일하다.
        const nameSelectors = args.nameSelectors || [];
        // Agent 모드의 코드 미리보기는 whitespace-pre-wrap 블록이라 코드의
        // 줄바꿈/들여쓰기 자체가 내용이다. innerText는 렌더링 결과를 따르므로
        // (숨겨지거나 클립된 영역에서) 공백이 접힐 여지가 있어, Agent 모드에서는
        // textContent로 원문 그대로 읽는다. Direct 모드는 기존과 동일하게
        // innerText를 쓴다(값이 false로 전달됨).
        const preferTextContent = !!args.preferTextContent;

        function firstMatchAll(root, selectors) {
            for (const s of selectors) {
                try {
                    const els = root.querySelectorAll(s);
                    if (els.length > 0) return Array.from(els);
                } catch (e) { /* 이 환경에서 지원 안 되는 셀렉터는 건너뜀 */ }
            }
            return [];
        }

        const full = node.innerText;

        const codeContainers = firstMatchAll(node, containerSelectors);
        const codeBlocks = codeContainers.map((block) => {
            let language = 'text';
            for (const s of langSelectors) {
                try {
                    const el = block.querySelector(s);
                    if (el && el.innerText && el.innerText.trim()) {
                        language = el.innerText.trim();
                        break;
                    }
                } catch (e) { /* skip */ }
            }
            let code = '';
            for (const s of codeSelectors) {
                try {
                    const el = block.querySelector(s);
                    if (el) {
                        code = preferTextContent ? el.textContent : el.innerText;
                        break;
                    }
                } catch (e) { /* skip */ }
            }
            let filename = null;
            for (const s of nameSelectors) {
                try {
                    const el = block.querySelector(s);
                    if (el && el.innerText && el.innerText.trim()) {
                        filename = el.innerText.trim();
                        break;
                    }
                } catch (e) { /* skip */ }
            }
            return { language, code, filename };
        });

        // 코드 블록 요소들을 "복제본"에서만 제거하고 남은 텍스트를 계산한다.
        // (원본 DOM은 건드리지 않음 - 화면에 실제로 보이는 답변에는 영향 없음)
        const clone = node.cloneNode(true);
        for (const s of containerSelectors) {
            try {
                clone.querySelectorAll(s).forEach((el) => el.remove());
            } catch (e) { /* skip */ }
        }
        const plain = clone.innerText;

        return { full, plain, codeBlocks };
    }
    """

    def _stream_extract(self, node) -> "tuple[str, str, list[CodeBlock]]":
        """
        (전체 텍스트, 코드 제외 일반 텍스트, 코드 블록 목록)을 위 JS로
        원자적으로 추출한다. 실패 시 안전하게 빈 값을 반환한다.
        """
        containers, langs, codes, names = self._code_selector_set()
        try:
            result = node.evaluate(
                self._STREAM_EXTRACT_JS,
                {
                    "containerSelectors": containers,
                    "langSelectors": langs,
                    "codeSelectors": codes,
                    "nameSelectors": names,
                    "preferTextContent": self.chat_mode == CHAT_MODE_AGENT,
                },
            )
        except Exception:
            return "", "", []

        full_text = result.get("full") or ""
        plain_text = (result.get("plain") or "").strip()
        code_blocks = [
            CodeBlock(
                language=(cb.get("language") or "text"),
                # Agent 모드에서만 artifact 카드의 파일명이 채워진다.
                # file_writer.resolve_filename()이 filename_hint를 최우선으로
                # 쓰므로, 별도 수정 없이 그대로 올바른 파일명으로 저장된다.
                filename_hint=(cb.get("filename") or None),
                code=cb.get("code") or "",
            )
            for cb in (result.get("codeBlocks") or [])
        ]
        return full_text, plain_text, code_blocks

    def iter_response_stream(
        self,
        baseline_count: "frozenset[str]",
        sent_prompt: str,
        timeout_seconds: int = 600,
        poll_interval: float = 0.4,
        new_message_timeout: float = 60.0,
        stable_checks: int = 3,
        confirm_delay: "float | None" = None,
        spinner_tick_seconds: float = 0.1,
        on_tick: "Optional[Callable[[], None]]" = None,
    ):
        """
        새 답변의 일반 텍스트와 코드 블록(들)을 모두 생성되는 대로 실시간으로
        보여주기 위한 제너레이터. (plain_text, code_blocks, reasoning, done)
        튜플을 순차 yield한다. reasoning은 ReasoningState | None이며, 추론
        로그가 감지된 적이 없으면 계속 None이다. 답변 컨테이너가 나타나기
        전까지는 reasoning.is_done=False로 경과 시간이 계속 갱신되고,
        답변 컨테이너가 나타난 시점부터는 reasoning.is_done=True로 고정된
        채(elapsed_seconds도 그 시점 값으로 고정) 이후 모든 프레임에 함께
        yield된다. 기존 plain_text/code_blocks/done 판정 로직 및 그 기준이

        on_tick(완전히 새로 추가된 선택적 콜백, 기본값 None - 기존 호출부는
        전혀 영향받지 않는다): 답변 텍스트/코드가 실제로 바뀌어 이 제너레이터가
        "yield"하는 순간이 아니라, 내부 대기 루프가 도는 매 tick마다(1단계는
        spinner_tick_seconds 간격, 2단계는 poll_interval 간격) 무조건 호출된다.
        Agent 모드의 workspace 패널처럼 "답변 텍스트와는 별개의 DOM 영역"을
        실시간으로 함께 관찰하고 싶을 때를 위한 훅이다 - 답변 텍스트가 한동안
        바뀌지 않으면 이 제너레이터 자체가 그 사이 아무것도 yield하지 않으므로,
        루프 밖(for ... in iter_response_stream())에서 매 순간을 관찰하는 것은
        불가능하다. on_tick은 제너레이터 안쪽 타이머에 직접 물리므로 텍스트
        변화와 무관하게 항상 실행된다. 콜백 내부 예외는 이 메서드가 삼켜서,
        관찰용 부가 기능 하나가 실패해도 본래의 스트리밍 자체는 절대 멈추지
        않게 한다.
        되는 셀렉터는 전혀 변경되지 않았다.

        완료 판정 기준은 wait_for_response_complete와 완전히 동일하게
        (a) '생성 중' 표시가 사라지고 (b) 코드 블록 내용까지 포함한 전체
        텍스트 길이가 연속 stable_checks회 변하지 않는 것으로 판단한다.
        (화면 표시용 plain_text만 기준으로 삼으면, 서두 문장이 먼저 안정된
        사이 코드가 아직 스트리밍 중인데 완료로 오판해 코드가 잘리는 문제가
        생기므로 반드시 전체 텍스트 기준을 유지한다.)

        [1단계 타임아웃 제거 - 추론 대기]
        LM Arena는 첫 답변 텍스트가 화면에 나타나기 전까지 추론(reasoning)에
        매우 긴 시간을 쓸 수 있다. 과거에는 이 1단계에 별도의
        new_message_timeout(기본 60초)을 두어, 그 안에 답변 컨테이너가 전혀
        나타나지 않으면 곧바로 포기하고 done=True로 빈 결과를 반환했다. 이
        때문에 추론이 60초를 넘기기만 해도 "응답 내용을 감지하지 못했습니다"
        라는 오판 경고와 함께 세션이 사실상 끊기는 문제가 있었다.
        이제 1단계는 타임아웃 없이 답변 컨테이너가 나타날 때까지 계속
        기다린다. 대신 아직 컨테이너가 없는 동안 spinner_tick_seconds(기본
        0.1초) 간격으로 ("", [], False)를 yield해서, 호출부(ui.stream_panel)가
        이를 감지해 "추론 중" 스피너를 부드럽게(약 초당 10프레임) 계속
        갱신할 수 있게 한다. 실제 DOM 조회는 이보다 느슨한 poll_interval
        간격으로만 수행되므로 부하는 늘지 않는다. new_message_timeout
        파라미터는 기존 호출부와의 시그니처 호환을 위해 남겨두되 더 이상
        강제 종료 용도로는 쓰이지 않는다(무시됨).
        """
        assert self.page is not None
        # 완료 확정 전 재검증 대기 시간. poll_interval보다 넉넉하게 잡아,
        # 리렌더링으로 인한 "생성 중" 표시의 순간적인 깜빡임 구간을
        # 확실히 벗어난 뒤에 다시 확인하도록 한다.
        if confirm_delay is None:
            confirm_delay = max(poll_interval * 3, 1.2)

        # 1단계: 프롬프트 에코가 아닌 실제 답변 컨테이너가 생길 때까지 대기.
        # 타임아웃 없이(=LM Arena가 아무리 오래 추론해도) 계속 기다린다.
        #
        # DOM 조회(_find_new_answer_container)는 poll_interval(기본 0.4초)
        # 간격이면 충분하지만, 화면의 스피너 애니메이션까지 이 주기에
        # 그대로 묶어버리면 초당 2~3프레임밖에 안 나와 뚝뚝 끊겨 보인다.
        # 그래서 DOM 조회 주기는 그대로 유지하되(부하 증가 없음), 스피너가
        # 부드럽게 돌아가도록 훨씬 더 짧은 간격(spinner_tick_seconds, 기본
        # 0.1초 = 초당 10프레임)으로 "아직 도착 안 함" 신호만 자주 yield한다.
        container = None
        last_dom_check = 0.0
        last_reasoning_text = ""

        def _fire_on_tick() -> None:
            # on_tick 콜백 실행 중 발생하는 예외는 절대 본 스트리밍을
            # 방해해서는 안 되므로 여기서 삼킨다.
            if on_tick is None:
                return
            try:
                on_tick()
            except Exception:
                pass

        while container is None:
            _fire_on_tick()
            # 로봇 확인 UI 감지(논블로킹). 이 1단계는 원래 타임아웃 없이
            # 계속 대기하므로 여기서는 감지/표시만 하면 되고, 아래 스피너
            # yield가 계속 돌아 중단 버튼도 그대로 반응한다.
            self._captcha_tick()
            now = time.time()
            if now - last_dom_check >= poll_interval:
                container = self._find_new_answer_container(baseline_count, sent_prompt)
                if container is None:
                    # 답변 컨테이너가 아직 없는 동안에만 추론 로그를 관찰한다.
                    # 이 별도 트래킹은 기존 answer 완료 판정에는 전혀 관여하지
                    # 않는다.
                    reasoning_container = self._find_new_reasoning_container()
                    if reasoning_container is not None:
                        if self._reasoning_start_time is None:
                            self._reasoning_start_time = time.time()
                        last_reasoning_text = self._extract_reasoning_text(reasoning_container)
                last_dom_check = now
                if container is not None:
                    break

            reasoning_state = None
            if self._reasoning_start_time is not None:
                reasoning_state = ReasoningState(
                    text=last_reasoning_text,
                    elapsed_seconds=time.time() - self._reasoning_start_time,
                    is_done=False,
                )
            yield "", [], reasoning_state, False
            time.sleep(spinner_tick_seconds)

        # 답변 컨테이너가 나타난 시점 = 추론 단계 종료 시점으로 간주한다.
        # 이 이후로는 elapsed_seconds를 더 이상 갱신하지 않고 고정해
        # "Thought for ~"(추론 완료) 형태로 계속 함께 표시한다.
        reasoning_final_state = None
        if self._reasoning_start_time is not None:
            reasoning_final_state = ReasoningState(
                text=last_reasoning_text,
                elapsed_seconds=time.time() - self._reasoning_start_time,
                is_done=True,
            )

        # 2단계(실제 스트리밍) 타임아웃은 여기서부터 계산한다. 1단계 추론
        # 대기가 아무리 길어졌어도 그로 인해 2단계 예산이 미리 깎이지
        # 않도록, 컨테이너를 찾은 시점을 기준으로 새로 잡는다.
        deadline = time.time() + timeout_seconds

        last_plain_text = ""
        last_code_blocks: list[CodeBlock] = []
        last_full_len = -1
        stable_count = 0

        while time.time() < deadline:
            _fire_on_tick()
            if self._captcha_tick():
                # 로봇 확인이 떠 있는 동안에는 "생성 중 표시 없음 + 텍스트
                # 정지"가 완료로 오판되지 않도록 안정 카운트를 리셋하고,
                # 사용자가 확인하는 시간은 응답 타임아웃에서 제외한다. 같은
                # 상태를 다시 yield해 호출부의 중단 버튼 확인(_check_stop)이
                # 이 대기 중에도 계속 반응하게 한다.
                stable_count = 0
                deadline = time.time() + timeout_seconds
                yield last_plain_text, last_code_blocks, reasoning_final_state, False
                time.sleep(poll_interval)
                continue
            container = self._find_new_answer_container(baseline_count, sent_prompt)
            generating = self.is_generating()

            full_text, plain_text, code_blocks = "", last_plain_text, last_code_blocks
            if container is not None:
                full_text, plain_text, code_blocks = self._stream_extract(container)

            code_signature = tuple((cb.language, cb.code) for cb in code_blocks)
            last_code_signature = tuple((cb.language, cb.code) for cb in last_code_blocks)
            if plain_text != last_plain_text or code_signature != last_code_signature:
                last_plain_text = plain_text
                last_code_blocks = code_blocks
                yield plain_text, code_blocks, reasoning_final_state, False

            full_len = len(full_text)
            # 코드 블록 내용까지 포함한 전체 텍스트가 더 이상 자라지 않을 때만
            # (그리고 생성 중 표시가 사라졌을 때만) 완료로 판정한다.
            if not generating and full_len == last_full_len and full_len > 0:
                stable_count += 1
                if stable_count >= stable_checks:
                    # 즉시 확정하지 않고, 한 번 더 대기 후 재검증한다
                    # (허위 완료로 인한 조기 종료 방지).
                    time.sleep(confirm_delay)

                    recheck_container = self._find_new_answer_container(baseline_count, sent_prompt)
                    recheck_generating = self.is_generating()
                    recheck_full_text = ""
                    recheck_plain_text, recheck_code_blocks = last_plain_text, last_code_blocks
                    if recheck_container is not None:
                        recheck_full_text, recheck_plain_text, recheck_code_blocks = self._stream_extract(
                            recheck_container
                        )
                    recheck_full_len = len(recheck_full_text)

                    if not recheck_generating and recheck_full_len == full_len:
                        # 재검증까지 통과 -> 그 사이에도 변화가 없었으므로 진짜 완료.
                        yield recheck_plain_text, recheck_code_blocks, reasoning_final_state, True
                        return

                    # 재검증 실패 -> 사실은 아직 생성 중이었던 것(허위 완료).
                    # 그 사이 새로 늘어난 내용을 반영하고 안정 카운트를 초기화한
                    # 뒤 계속 관찰한다. (완료로 확정하지 않음)
                    recheck_code_signature = tuple(
                        (cb.language, cb.code) for cb in recheck_code_blocks
                    )
                    if (
                        recheck_plain_text != last_plain_text
                        or recheck_code_signature != last_code_signature
                    ):
                        last_plain_text = recheck_plain_text
                        last_code_blocks = recheck_code_blocks
                        yield recheck_plain_text, recheck_code_blocks, reasoning_final_state, False

                    last_full_len = recheck_full_len
                    stable_count = 0
                    continue
            else:
                stable_count = 0

            last_full_len = full_len
            time.sleep(poll_interval)

        yield last_plain_text, last_code_blocks, reasoning_final_state, False  # 타임아웃 (완료로 확정되지 않음)

    def parse_last_response(self, baseline_count: int, sent_prompt: str) -> ParsedResponse:
        """
        baseline_count 이후 새로 생긴 컨테이너 중, 보낸 프롬프트의 에코가
        아닌 실제 답변 컨테이너에서 일반 텍스트 조각과 코드 블록들을 분리
        추출한다. 코드 블록 컨테이너 내부의 텍스트는 일반 텍스트에서 제외한다.
        """
        assert self.page is not None
        container = self._find_new_answer_container(baseline_count, sent_prompt)
        text_parts: list[str] = []
        code_blocks: list[CodeBlock] = []

        if container is None:
            return ParsedResponse(text_parts, code_blocks)

        # 코드 블록 추출 (현재 모드에 맞는 셀렉터 묶음을 사용한다. Direct
        # 모드에서는 name_selectors가 비어 있어 기존과 완전히 동일하다.)
        container_selectors, lang_selectors, code_selectors, name_selectors = (
            self._code_selector_set()
        )
        code_container_loc = sel.first_match_locator(container, container_selectors)
        code_count = code_container_loc.count() if code_container_loc else 0

        for i in range(code_count):
            block = code_container_loc.nth(i)
            language = "text"
            for lang_sel in lang_selectors:
                try:
                    lbl = block.locator(lang_sel)
                    if lbl.count() > 0:
                        raw = lbl.first.inner_text().strip()
                        if raw:
                            language = raw
                            break
                except Exception:
                    continue

            code_text = ""
            prefer_text_content = self.chat_mode == CHAT_MODE_AGENT
            for code_sel in code_selectors:
                try:
                    code_loc = block.locator(code_sel)
                    if code_loc.count() > 0:
                        # Agent 모드는 위 _stream_extract와 동일한 이유로
                        # textContent를 써서 코드 원문을 그대로 읽는다.
                        code_text = (
                            code_loc.first.text_content()
                            if prefer_text_content
                            else code_loc.first.inner_text()
                        ) or ""
                        break
                except Exception:
                    continue

            filename_hint = None
            for name_sel in name_selectors:
                try:
                    name_loc = block.locator(name_sel)
                    if name_loc.count() > 0:
                        raw_name = name_loc.first.inner_text().strip()
                        if raw_name:
                            filename_hint = raw_name
                            break
                except Exception:
                    continue

            if code_text.strip():
                code_blocks.append(
                    CodeBlock(language=language, filename_hint=filename_hint, code=code_text)
                )

        # 전체 텍스트에서 코드블록 텍스트를 제외한 순수 설명 텍스트 추출
        if self.chat_mode == CHAT_MODE_AGENT:
            # Agent 모드의 artifact 카드는 코드뿐 아니라 파일명/언어 배지까지
            # 한 카드 안에 들어 있어서, 아래 Direct 방식(코드 문자열만
            # replace)으로는 "style.cssCSS" 같은 헤더 텍스트가 설명문에 남는다.
            # 그래서 이미 검증된 _stream_extract의 방식(복제본에서 카드 전체를
            # 제거하고 남은 텍스트를 읽기)을 그대로 재사용한다. Direct 모드의
            # 아래 로직은 전혀 건드리지 않는다.
            _, plain_text, _ = self._stream_extract(container)
            if plain_text.strip():
                text_parts.append(plain_text.strip())
            return ParsedResponse(text_parts, code_blocks)

        try:
            full_text = container.inner_text()
        except Exception:
            full_text = ""

        remaining = full_text
        for cb in code_blocks:
            remaining = remaining.replace(cb.code, "")
        if remaining.strip():
            text_parts.append(remaining.strip())

        return ParsedResponse(text_parts, code_blocks)