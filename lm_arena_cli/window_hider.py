"""
LM Arena CLI - "headless처럼 보이는" 숨김 창 관리

[왜 진짜 headless를 쓰지 않는가]
Chromium은 실행 중에 headless 여부를 바꿀 수 없다. 브라우저를 다시 띄우면
진행 중이던 페이지 상태가 사라지는데, reCAPTCHA(로봇 확인)와 그에 딸린
대화 시작 요청도 그 상태에 포함되어 함께 사라진다. 그래서 브라우저는 처음부터
끝까지 "창이 있는 일반 Chrome" 하나만 띄워 두고, 사용자 눈에만 headless처럼
보이도록 창을 숨긴다. 페이지, 쿠키, 진행 중인 요청은 전혀 건드리지 않는다.

[숨기는 방법]
- Windows: 창을 "화면 안의 원래 위치"에 그대로 둔 채 완전 투명(알파 0) +
  마우스 통과(WS_EX_TRANSPARENT) + 작업 표시줄/Alt+Tab 제외(TOOLWINDOW) +
  활성화 금지(NOACTIVATE)로 만든다. 창이 항상 화면 안에 있고 Chromium 입장에서는
  계속 "보이는 창"이므로 렌더링이 멈추거나 창이 검게 그려지는 문제가 없고,
  기존 captcha.py의 창 탐색(_locate_hwnd)도 그대로 동작한다. 최소화/화면 밖
  이동/SW_HIDE로 숨기지 않는 이유: 그런 창은 Chromium이 렌더링을 멈추거나
  다시 보였을 때 검은 화면으로 남을 수 있다.
- 알파 방식이 지원되지 않는 환경과 Windows 외 OS: CDP(Browser.setWindowBounds)로
  화면 밖으로 옮기고, 꺼낼 때 창 크기를 1px 흔들어 다시 그리게 한다
  (macOS는 OS가 창을 화면 안으로 끌어올 수 있어 완전히 숨겨지지 않을 수 있다).

[창을 꺼내는 경우]
browser.py가 이 모듈의 reveal()/conceal()을 호출한다.
  1) 로그아웃 상태: 구글 로그인 화면이 완전히 뜬 뒤 reveal, 로그인 후 conceal
  2) reCAPTCHA 감지: reveal, 통과 후 conceal (감지/커버/전면 표시/복구는
     기존 captcha.py가 그대로 담당하고, 이 모듈은 창의 위치/작업표시줄만 맡는다)
"""
from __future__ import annotations

import sys
import time
from typing import Any, Optional

_IS_WINDOWS = sys.platform == "win32"

OFFSCREEN_X, OFFSCREEN_Y = -24000, -24000
ONSCREEN_X, ONSCREEN_Y = 80, 50

# Chromium이 보이지 않는 창의 렌더링/타이머를 늦추지 않도록 하는 플래그들
# (창을 숨겨도 성능이 떨어지지 않게 한다).
_ANTI_THROTTLE_ARGS = [
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-features=CalculateNativeWinOcclusion",
]

GOOGLE_IDENTIFIER_SELECTOR = "input[type='email'], #identifierId, input[name='identifier']"


# ── Windows 전용 헬퍼 (captcha.py의 검증된 ctypes 로더/포그라운드 로직 재사용) ──
def _win_move(hwnd: int, x: int, y: int) -> None:
    from .captcha import _win_api

    ctypes, wintypes, user32, _ = _win_api()
    SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
    user32.SetWindowPos(hwnd, None, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)


def _win_set_hidden(hwnd: int, hidden: bool) -> bool:
    """
    hidden=True : 투명 + 마우스 통과 + 작업 표시줄/Alt+Tab 제외 + 활성화 금지.
    hidden=False: 위 속성을 모두 되돌려 평범한 창으로 복구.
    (작업 표시줄 반영을 위해 스타일 변경 전후로 창을 잠깐 숨겼다 보이는데, 창이
    투명하거나 방금 만든 상태에서만 수행한다.)
    투명 처리(SetLayeredWindowAttributes)가 지원되지 않으면 레이어드 속성을 되돌리고
    False를 반환한다 -> 호출부가 화면 밖 이동 방식으로 폴백한다.
    """
    from .captcha import _win_api

    ctypes, wintypes, user32, _ = _win_api()
    GWL_EXSTYLE = -20
    WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x00000020, 0x00000080
    WS_EX_APPWINDOW, WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x00040000, 0x00080000, 0x08000000
    LWA_ALPHA = 0x2
    SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER = 0x0001, 0x0002, 0x0004
    SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x0010, 0x0020

    getter = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
    setter = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
    getter.argtypes = [wintypes.HWND, ctypes.c_int]
    getter.restype = ctypes.c_ssize_t
    setter.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    setter.restype = ctypes.c_ssize_t
    user32.SetLayeredWindowAttributes.argtypes = [
        wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD,
    ]
    user32.SetLayeredWindowAttributes.restype = wintypes.BOOL

    ex = getter(hwnd, GWL_EXSTYLE)
    if hidden:
        new = (ex | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) & ~WS_EX_APPWINDOW
    else:
        new = (ex | WS_EX_APPWINDOW | WS_EX_LAYERED) & ~(
            WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
        )

    user32.ShowWindow(hwnd, SW_HIDE)
    setter(hwnd, GWL_EXSTYLE, new)
    ok = bool(user32.SetLayeredWindowAttributes(hwnd, 0, 0 if hidden else 255, LWA_ALPHA))
    if not ok:
        # 레이어드 창이 속성 없이 남으면 아무 것도 그려지지 않는다: 반드시 되돌린다.
        setter(hwnd, GWL_EXSTYLE, new & ~(WS_EX_LAYERED | WS_EX_TRANSPARENT))
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    if ok and not hidden:
        setter(hwnd, GWL_EXSTYLE, new & ~WS_EX_LAYERED)
        user32.SetWindowPos(
            hwnd, None, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )
    return ok


def _win_nudge(hwnd: int) -> None:
    """창 크기를 1px 늘렸다 되돌려 Chromium이 화면을 다시 그리게 한다."""
    from .captcha import _win_api

    ctypes, wintypes, user32, _ = _win_api()
    SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0002, 0x0004, 0x0010
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return
    w, h = rect.right - rect.left, rect.bottom - rect.top
    flags = SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE
    user32.SetWindowPos(hwnd, None, 0, 0, w + 1, h, flags)
    user32.SetWindowPos(hwnd, None, 0, 0, w, h, flags)


class WindowHider:
    """LMArenaBrowser 하나에 붙어 자동화 창을 숨기고/꺼내는 역할만 한다."""

    def __init__(self, browser: Any) -> None:
        self._b = browser
        self._hwnd: Optional[int] = None
        self._prev_fg: Optional[int] = None
        self._enabled = False   # attach()에 성공해 창을 숨겨 두었는가
        self._alpha_mode = False  # Windows: 화면 안 투명 처리 중인가(아니면 화면 밖)
        self.hidden = False     # 지금 창이 숨겨진(화면 밖) 상태인가

    # ── 실행 준비 ────────────────────────────────────────────────
    @staticmethod
    def launch_args() -> list[str]:
        return [
            f"--window-position={OFFSCREEN_X},{OFFSCREEN_Y}",
            "--window-size=1400,900",
            *_ANTI_THROTTLE_ARGS,
        ]

    def note_foreground(self) -> None:
        """브라우저를 띄우기 전에 사용자가 쓰던 창을 기억한다(포커스 복귀용)."""
        if not _IS_WINDOWS:
            return
        try:
            from .captcha import _win_foreground

            self._prev_fg = _win_foreground()
        except Exception:
            self._prev_fg = None

    def attach(self) -> None:
        """브라우저 실행 직후 호출: 창을 숨긴다(Windows는 투명 처리, 그 외는 화면 밖)."""
        page = getattr(self._b, "page", None)
        if page is None:
            return
        hidden_ok = False
        if _IS_WINDOWS:
            try:
                guard = getattr(self._b, "_captcha", None)
                hwnd = guard._locate_hwnd() if guard is not None else None
                if hwnd:
                    self._hwnd = hwnd
                    if _win_set_hidden(hwnd, True):
                        # 화면 밖에서 만들어진 창을 원래 위치로 되돌리고(투명한 상태라
                        # 보이지 않는다) 다시 그리게 해 렌더링이 정상 재개되게 한다.
                        _win_move(hwnd, ONSCREEN_X, ONSCREEN_Y)
                        _win_nudge(hwnd)
                        self._alpha_mode = True
                        hidden_ok = True
                    else:
                        hidden_ok = True  # 투명 처리 불가: 실행 시의 화면 밖 위치 유지
            except Exception:
                hidden_ok = False
        if not hidden_ok:
            try:
                self._cdp_move(page, OFFSCREEN_X, OFFSCREEN_Y)
                hidden_ok = True
            except Exception:
                hidden_ok = False
        self._enabled = hidden_ok
        self.hidden = hidden_ok
        if hidden_ok:
            self._restore_previous_focus()

    # ── 표시 / 숨김 ──────────────────────────────────────────────
    def reveal(self, page: Any = None) -> None:
        """
        창을 보이게 하고 맨 앞으로 가져온다. page가 자동화 메인 페이지가 아닌
        경우(구글 로그인 팝업)에는 그 팝업 창만 화면 안으로 옮긴다.
        """
        b_page = getattr(self._b, "page", None)
        target = page if page is not None else b_page
        if target is None:
            return
        is_main = target is b_page
        if is_main and not self._enabled:
            return  # 숨긴 적이 없으면 이미 보이는 창이다.
        try:
            if is_main and _IS_WINDOWS and self._hwnd:
                from .captcha import _win_force_foreground

                _win_set_hidden(self._hwnd, False)
                _win_move(self._hwnd, ONSCREEN_X, ONSCREEN_Y)
                _win_nudge(self._hwnd)
                self._alpha_mode = False
                self.hidden = False
                _win_force_foreground(self._hwnd)
            else:
                self._cdp_move(target, ONSCREEN_X, ONSCREEN_Y, nudge=True)
                if is_main:
                    self.hidden = False
        except Exception:
            if is_main:
                self.hidden = False
        try:
            target.bring_to_front()
        except Exception:
            pass

    def conceal(self) -> None:
        """다시 숨긴다."""
        page = getattr(self._b, "page", None)
        if page is None or not self._enabled:
            return
        try:
            if _IS_WINDOWS and self._hwnd:
                if _win_set_hidden(self._hwnd, True):
                    self._alpha_mode = True
                else:
                    _win_move(self._hwnd, OFFSCREEN_X, OFFSCREEN_Y)
            else:
                self._cdp_move(page, OFFSCREEN_X, OFFSCREEN_Y)
            self.hidden = True
        except Exception:
            pass

    def park(self, page: Any) -> None:
        """새로 열린 팝업 창을 로드가 끝날 때까지 화면 밖에 둔다."""
        try:
            self._cdp_move(page, OFFSCREEN_X, OFFSCREEN_Y)
        except Exception:
            pass

    def reveal_when_google_ready(self, page: Any, timeout_ms: int = 10000) -> None:
        """구글 로그인 화면(이메일 입력창)이 실제로 그려진 뒤에 창을 꺼낸다."""
        try:
            page.wait_for_load_state("load", timeout=timeout_ms)
        except Exception:
            pass
        try:
            page.wait_for_selector(GOOGLE_IDENTIFIER_SELECTOR, timeout=timeout_ms)
        except Exception:
            pass
        self.reveal(page)

    # ── 내부 ────────────────────────────────────────────────────
    def _cdp_move(self, page: Any, left: int, top: int, nudge: bool = False) -> None:
        session = self._b.context.new_cdp_session(page)
        try:
            info = session.send("Browser.getWindowForTarget")
            window_id = info["windowId"]
            session.send(
                "Browser.setWindowBounds",
                {
                    "windowId": window_id,
                    "bounds": {"left": left, "top": top, "windowState": "normal"},
                },
            )
            if nudge:
                # 화면 밖에 있던 창이 검게 남지 않도록 크기를 1px 흔들어 다시 그리게 한다.
                bounds = (info.get("bounds") or {})
                width, height = bounds.get("width"), bounds.get("height")
                if width and height:
                    session.send(
                        "Browser.setWindowBounds",
                        {"windowId": window_id, "bounds": {"width": width + 1, "height": height}},
                    )
                    session.send(
                        "Browser.setWindowBounds",
                        {"windowId": window_id, "bounds": {"width": width, "height": height}},
                    )
        finally:
            try:
                session.detach()
            except Exception:
                pass

    def _restore_previous_focus(self) -> None:
        """브라우저가 뜨면서 가져간 포커스를 사용자가 쓰던 창으로 돌려준다."""
        if not (_IS_WINDOWS and self._hwnd and self._prev_fg and self._prev_fg != self._hwnd):
            return
        try:
            from .captcha import (
                _win_foreground,
                _win_force_foreground,
                _win_is_minimized,
                _win_is_window,
            )

            time.sleep(0.2)
            if (
                _win_foreground() == self._hwnd
                and _win_is_window(self._prev_fg)
                and not _win_is_minimized(self._prev_fg)
            ):
                _win_force_foreground(self._prev_fg)
        except Exception:
            pass