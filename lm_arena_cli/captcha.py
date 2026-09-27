"""
LM Arena CLI - 로봇 확인(reCAPTCHA) 감지 / "확인 UI만" 전면 표시

[배경]
LM Arena는 답변 도중 가끔 "Security Verification" 다이얼로그(reCAPTCHA v2
체크박스)를 띄우고, 사용자가 이를 통과시키지 않으면 답변을 계속 받을 수
없다. 그런데 자동화 브라우저(Test Chrome) 창은 평소 뒤쪽에 있거나 최소화되어
있고, 로그인 완료 후에는 "연결됨" 오버레이가 화면을 덮고 있어서 사용자가
확인 UI가 떴다는 사실 자체를 알 수 없다.

[이 모듈이 하는 일 - 기존 로직은 건드리지 않는 순수 추가 기능]
1) 감지: 매 tick마다 페이지에 로봇 확인 다이얼로그가 열려 있는지 검사한다.
2) 표시: 감지되면
   - reCAPTCHA(다이얼로그, iframe, 이미지 퀴즈 컨테이너)는 스타일/속성/위치
     어느 것도 건드리지 않는다. reCAPTCHA는 자신의 표시/클릭 영역이 변조되면
     상호작용이 막히므로, 읽기(감지)만 하고 절대 수정하지 않는다.
   - 대신 "다이얼로그 바로 아래 z-index"에 단색 불투명 커버(pointer-events:none)를
     깔아 그보다 아래에 있는 사이트 화면/반투명 배경을 가린다. 다이얼로그(z 110)와
     Google이 띄우는 퀴즈 컨테이너(z 20억)는 커버보다 위에 있으므로 원래
     그대로 보이고 클릭된다.
   - 우리가 직접 만든 "연결됨" 오버레이는 z-index가 최대라 확인 UI를 덮으므로,
     확인 UI가 떠 있는 동안에만 숨긴다.
   - 자동화 창이 최소화되어 있으면 복원하고, 맨 앞으로 가져온다.
     (Windows에서는 포커스 스틸 방지에 막히지 않도록 Win32 API로 한 번 더
     확실하게 최상단/포그라운드로 올린다.)
3) 복구: 다이얼로그가 사라지면(=통과) 숨김 스타일을 제거해 원래 화면(연결됨
   오버레이)을 되돌리고, 창을 원래 상태(최소화였으면 다시 최소화, 아니면
   직전에 사용 중이던 창으로 포커스 복귀)로 돌려놓는다.

[스레드 안전성]
Playwright sync API는 스레드 안전하지 않다. 그래서 이 모듈은 별도 스레드를
만들지 않고, LMArenaBrowser가 이미 돌리고 있는 대기 루프(iter_response_stream
/ wait_for_response_complete)에서 tick()을 호출하는 방식으로만 동작한다.
tick()은 절대 블로킹하지 않고(내부에서 sleep 없음) 매우 가볍게 반환한다.

[플랫폼]
- 숨김/복원/최소화 복원(CDP) 및 page.bring_to_front()는 모든 OS에서 동작.
- 포그라운드 강제 전환은 Windows에서만 추가로 수행한다(그 외 OS는 브라우저
  자체의 활성화 요청까지만). 추가 의존성은 없다(표준 라이브러리 ctypes만 사용).
- headless 모드에서는 사용자가 볼 창이 없으므로 아무 것도 하지 않는다.
"""
from __future__ import annotations

import sys
import time
import uuid
from typing import Any, Optional

from . import dom_selectors as sel

_IS_WINDOWS = sys.platform == "win32"

# 페이지 안에 심는 식별자(<style> id / 커버 요소 id). 사이트 자체 id와 겹치지
# 않도록 접두어를 붙였다. 둘 다 "우리가 만든" 요소이며 사이트/reCAPTCHA의
# 기존 요소에는 어떤 속성·스타일도 추가하지 않는다.
_CAPTCHA_STYLE_ID = "lmarena-cli-captcha-style"
_CAPTCHA_COVER_ID = "lmarena-cli-captcha-cover"
_CONNECTED_OVERLAY_ID = "lmarena-cli-connected-overlay"  # browser.py의 오버레이 id

# 주입하는 CSS는 "우리 자신의 연결됨 오버레이 숨기기" 한 줄뿐이다.
_CAPTCHA_ONLY_CSS = f"#{_CONNECTED_OVERLAY_ID} {{ display: none !important; }}"

# 브라우저 안에서 실행: 열려 있는 로봇 확인 다이얼로그를 "읽기만" 해서 찾고,
# 발견되면 (a) 숨김 스타일과 (b) 다이얼로그 바로 아래 z-index의 단색 커버를
# 보장한다(멱등). 발견 여부(bool)를 반환한다.
_SYNC_JS = """
(o) => {
    const re = new RegExp(o.textPattern, 'i');
    let found = null;
    for (const el of document.querySelectorAll(o.dialogSelector)) {
        if (el.getAttribute('data-state') === 'closed') continue;  // 닫히는 애니메이션 중
        let isCaptcha = false;
        for (const m of o.innerMarkers) {
            try { if (el.querySelector(m)) { isCaptcha = true; break; } } catch (e) { /* skip */ }
        }
        if (!isCaptcha && !re.test(el.textContent || '')) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) continue;
        found = el;
        break;
    }
    if (!found) return false;

    if (!document.getElementById(o.styleId)) {
        const style = document.createElement('style');
        style.id = o.styleId;
        style.textContent = o.css;
        document.head.appendChild(style);
    }

    // 커버의 z-index = 다이얼로그 z-index - 1 (같은 최상위 스택에 있는 경우).
    // 숫자로 읽을 수 없으면(auto 등) 사이트 기본 배경 위에 얹히는 정도의
    // 값으로 폴백한다.
    const dz = parseInt(getComputedStyle(found).zIndex, 10);
    const z = Number.isFinite(dz) ? dz - 1 : 100;
    let cover = document.getElementById(o.coverId);
    if (!cover) {
        cover = document.createElement('div');
        cover.id = o.coverId;
        document.body.appendChild(cover);
    }
    cover.setAttribute('style',
        'position:fixed;inset:0;background:#0b0b0f;pointer-events:none;z-index:' + z);
    return true;
}
"""

_CLEAR_JS = """
(o) => {
    const st = document.getElementById(o.styleId);
    if (st) st.remove();
    const cv = document.getElementById(o.coverId);
    if (cv) cv.remove();
}
"""


# ── Windows 전용 헬퍼 ────────────────────────────────────────────────
def _win_api():
    """user32/kernel32를 타입 지정과 함께 로드한다(64비트 핸들 안전)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetForegroundWindow.argtypes = []
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    return ctypes, wintypes, user32, kernel32


def _win_foreground() -> Optional[int]:
    try:
        _, _, user32, _ = _win_api()
        hwnd = user32.GetForegroundWindow()
        return int(hwnd) if hwnd else None
    except Exception:
        return None


def _win_is_window(hwnd: Optional[int]) -> bool:
    if not hwnd:
        return False
    try:
        _, _, user32, _ = _win_api()
        return bool(user32.IsWindow(hwnd))
    except Exception:
        return False


def _win_is_minimized(hwnd: int) -> bool:
    try:
        _, _, user32, _ = _win_api()
        return bool(user32.IsIconic(hwnd))
    except Exception:
        return False


def _win_find_by_title(token: str) -> Optional[int]:
    """최상위 창 중 제목에 token이 들어 있는 첫 창의 핸들을 반환한다."""
    ctypes, wintypes, user32, _ = _win_api()
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 2)
            user32.GetWindowTextW(hwnd, buf, n + 2)
            if token in buf.value:
                found.append(int(hwnd))
                return False
        except Exception:
            pass
        return True

    user32.EnumWindows(_cb, 0)
    return found[0] if found else None


def _win_force_foreground(hwnd: int) -> None:
    """
    Windows의 포커스 스틸 방지에 막히지 않고 창을 최상단/포그라운드로 올린다.
    (ALT 키 입력으로 잠금을 풀고 → 입력 스레드를 붙이고 → 잠깐 TOPMOST로
    올렸다가 되돌려 Z순서 맨 위로 보낸 뒤 → SetForegroundWindow)
    TOPMOST는 즉시 해제하므로 창이 계속 "항상 위"로 남지 않는다.
    """
    ctypes, wintypes, user32, kernel32 = _win_api()
    SW_RESTORE = 9
    SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0040
    HWND_TOPMOST, HWND_NOTOPMOST = wintypes.HWND(-1), wintypes.HWND(-2)

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)

    # ALT 눌렀다 떼기: 포그라운드 잠금을 일시적으로 해제하는 잘 알려진 우회.
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 0x0002, 0)

    cur_tid = kernel32.GetCurrentThreadId()
    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    if fg_tid and fg_tid != cur_tid:
        attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
    try:
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(cur_tid, fg_tid, False)


class CaptchaGuard:
    """
    LMArenaBrowser 하나에 붙어서 로봇 확인 UI를 감시한다.
    browser는 .page / .context / .headless 속성만 읽는다(호출 시점마다 다시
    읽으므로 page 교체에도 안전).
    """

    # DOM 조회 최소 간격(초). tick()이 더 자주 불려도 이 간격보다 촘촘히
    # 페이지를 조회하지 않는다.
    CHECK_INTERVAL = 0.3

    def __init__(self, browser: Any) -> None:
        self._b = browser
        self._active = False
        self._last_check = 0.0
        self._hwnd: Optional[int] = None          # Windows: 자동화 창 핸들(캐시)
        self._prev_fg: Optional[int] = None       # Windows: 확인 UI 직전 포그라운드 창
        self._was_minimized = False               # 확인 UI가 뜨기 전 창이 최소화였는지

    # ── 공개 API ────────────────────────────────────────────────
    @property
    def active(self) -> bool:
        return self._active

    def tick(self) -> bool:
        """
        로봇 확인 다이얼로그가 "지금" 화면에 떠 있으면 True.
        논블로킹. 새로 감지된 순간에는 숨김 스타일 주입 + 창 전면 표시를,
        사라진 순간에는 화면/창 복구를 수행한다. 어떤 예외도 밖으로 내보내지
        않는다(부가 기능이 본래 응답 대기를 절대 망가뜨리면 안 되므로).
        """
        page = getattr(self._b, "page", None)
        if page is None or getattr(self._b, "headless", False):
            return False

        now = time.monotonic()
        if now - self._last_check < self.CHECK_INTERVAL:
            return self._active
        self._last_check = now

        try:
            present = bool(page.evaluate(_SYNC_JS, self._js_args()))
        except Exception:
            # 페이지 이동/리로드 중 등 일시적 실패: 상태를 바꾸지 않고 다음
            # tick에 다시 본다.
            return self._active

        if present and not self._active:
            self._active = True
            self._on_appear()
        elif not present and self._active:
            self._active = False
            self._on_gone()
        return self._active

    def wait_until_clear(self, max_seconds: float = 300.0, poll: float = 0.3) -> bool:
        """
        (블로킹) 확인 UI가 없어질 때까지 기다린다. 프롬프트 전송 직전에 이전
        턴에서 남은 확인 UI가 있는지 정리하는 용도. 시간 안에 사라졌으면
        True, 그 사이에도 계속 떠 있으면 False.
        """
        deadline = time.monotonic() + max_seconds
        while self.tick():
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll)
        return True

    # ── 내부 ────────────────────────────────────────────────────
    def _js_args(self) -> dict:
        return {
            "dialogSelector": sel.CAPTCHA_DIALOG_CONTAINER,
            "innerMarkers": sel.CAPTCHA_DIALOG_INNER_MARKERS,
            "textPattern": sel.CAPTCHA_DIALOG_TEXT_PATTERN,
            "styleId": _CAPTCHA_STYLE_ID,
            "coverId": _CAPTCHA_COVER_ID,
            "css": _CAPTCHA_ONLY_CSS,
        }

    def _on_appear(self) -> None:
        # 배경 커버는 위 _SYNC_JS 안에서 이미 깔렸다(= 창을 앞으로
        # 가져오기 "전에" 배경이 먼저 가려진다). 여기서는 창만 올린다.
        if _IS_WINDOWS:
            self._prev_fg = _win_foreground()
        self._raise_window()

    def _on_gone(self) -> None:
        page = getattr(self._b, "page", None)
        if page is not None:
            try:
                page.evaluate(
                    _CLEAR_JS, {"styleId": _CAPTCHA_STYLE_ID, "coverId": _CAPTCHA_COVER_ID}
                )
            except Exception:
                pass
        self._restore_window()

    def _cdp_window_call(self, method: str, params: Optional[dict] = None) -> Optional[dict]:
        """Browser.* CDP 호출 한 번(세션은 매번 새로 열고 닫는다 - 드문 이벤트라 비용 무시)."""
        ctx = getattr(self._b, "context", None)
        page = getattr(self._b, "page", None)
        if ctx is None or page is None:
            return None
        session = ctx.new_cdp_session(page)
        try:
            return session.send(method, params or {})
        finally:
            try:
                session.detach()
            except Exception:
                pass

    def _window_id(self) -> tuple[Optional[int], Optional[str]]:
        info = self._cdp_window_call("Browser.getWindowForTarget") or {}
        state = (info.get("bounds") or {}).get("windowState")
        return info.get("windowId"), state

    def _raise_window(self) -> None:
        page = getattr(self._b, "page", None)

        # 1) 최소화되어 있으면 복원한다(CDP - 모든 OS 공통).
        self._was_minimized = False
        try:
            window_id, state = self._window_id()
            if window_id is not None and state == "minimized":
                self._was_minimized = True
                self._cdp_window_call(
                    "Browser.setWindowBounds",
                    {"windowId": window_id, "bounds": {"windowState": "normal"}},
                )
        except Exception:
            pass

        # 2) 탭/창 활성화 요청(모든 OS 공통).
        if page is not None:
            try:
                page.bring_to_front()
            except Exception:
                pass

        # 3) Windows: 포커스 스틸 방지를 넘어 확실하게 맨 앞으로.
        if _IS_WINDOWS:
            try:
                hwnd = self._locate_hwnd()
                if hwnd:
                    _win_force_foreground(hwnd)
            except Exception:
                pass

    def _restore_window(self) -> None:
        # 확인 UI가 뜨기 전 창이 최소화였으면 다시 최소화한다.
        if self._was_minimized:
            self._was_minimized = False
            try:
                window_id, _state = self._window_id()
                if window_id is not None:
                    self._cdp_window_call(
                        "Browser.setWindowBounds",
                        {"windowId": window_id, "bounds": {"windowState": "minimized"}},
                    )
            except Exception:
                pass
            return

        # Windows: 사용자가 아직 자동화 창에 머물러 있다면(=다른 창으로 이미
        # 옮겨가지 않았다면) 직전에 쓰던 창으로 포커스를 돌려준다. Work 모드처럼
        # 이후 다른 창을 조작하는 흐름이 자동화 창에 가로막히지 않게 하기 위함.
        if _IS_WINDOWS and self._hwnd and self._prev_fg and self._prev_fg != self._hwnd:
            try:
                if (
                    _win_foreground() == self._hwnd
                    and _win_is_window(self._prev_fg)
                    and not _win_is_minimized(self._prev_fg)
                ):
                    _win_force_foreground(self._prev_fg)
            except Exception:
                pass
        self._prev_fg = None

    def _locate_hwnd(self) -> Optional[int]:
        """
        자동화 창의 HWND를 찾는다. Playwright persistent context에는 브라우저
        PID를 얻는 공식 경로가 없어서, 탭 제목을 잠깐 고유 토큰으로 바꿔
        (Chromium 창 제목은 탭 제목을 그대로 반영한다) 최상위 창 목록에서
        찾은 뒤 원래 제목으로 되돌린다. 한 번 찾으면 캐시한다.
        """
        if self._hwnd and _win_is_window(self._hwnd):
            return self._hwnd
        page = getattr(self._b, "page", None)
        if page is None:
            return None

        token = "LMARENA-CLI-" + uuid.uuid4().hex[:8]
        try:
            old_title = page.title()
        except Exception:
            old_title = None
        try:
            page.evaluate("(t) => { document.title = t; }", token)
        except Exception:
            return None

        hwnd: Optional[int] = None
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            hwnd = _win_find_by_title(token)
            if hwnd:
                break
            time.sleep(0.1)

        if old_title is not None:
            try:
                page.evaluate("(t) => { document.title = t; }", old_title)
            except Exception:
                pass

        self._hwnd = hwnd
        return hwnd