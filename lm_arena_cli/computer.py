"""
LM Arena Work 모드 - 로컬 컴퓨터 조작 계층

LM Arena는 도구 호출이 없고 텍스트/코드 블록만 반환한다. 이 모듈은
모델이 코드 블록으로 지시한 마우스·키보드·창·셸·파일 동작을 이 PC에서
실제로 수행하고, 접근성 트리/창 목록을 텍스트 관측으로 돌려준다.

browser.py / DOM 셀렉터 / 기존 파일 저장 로직은 사용하지 않는다.
추가 패키지 없이 Windows Win32 + PowerShell UI Automation만 사용한다.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any


MAX_SHELL_CHARS = 8000
MAX_FILE_CHARS = 12000
MAX_OBS_CHARS = 14000
SHELL_TIMEOUT = 45
UIA_TIMEOUT = 14


def _is_windows() -> bool:
    return os.name == "nt"


class ComputerError(RuntimeError):
    pass


class Computer:
    def __init__(self) -> None:
        self._last_uia: dict[str, Any] | None = None
        if _is_windows():
            self._init_win32()

    def _init_win32(self) -> None:
        import ctypes
        from ctypes import wintypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self.KEYEVENTF_EXTENDEDKEY = 0x0001
        self.KEYEVENTF_KEYUP = 0x0002
        self.KEYEVENTF_UNICODE = 0x0004
        self.INPUT_MOUSE = 0
        self.INPUT_KEYBOARD = 1
        self.MOUSEEVENTF_MOVE = 0x0001
        self.MOUSEEVENTF_LEFTDOWN = 0x0002
        self.MOUSEEVENTF_LEFTUP = 0x0004
        self.MOUSEEVENTF_RIGHTDOWN = 0x0008
        self.MOUSEEVENTF_RIGHTUP = 0x0010
        self.MOUSEEVENTF_MIDDLEDOWN = 0x0020
        self.MOUSEEVENTF_MIDDLEUP = 0x0040
        self.MOUSEEVENTF_WHEEL = 0x0800
        self.MOUSEEVENTF_ABSOLUTE = 0x8000
        self.MOUSEEVENTF_VIRTUALDESK = 0x4000
        self.WHEEL_DELTA = 120
        self.SW_RESTORE = 9

        self.VK = {
            "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
            "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "cmd": 0x5B, "meta": 0x5B,
            "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
            "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
            "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21,
            "pagedown": 0x22, "pgdn": 0x22, "insert": 0x2D, "ins": 0x2D,
            "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
            "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91,
            "printscreen": 0x2C, "apps": 0x5D,
        }
        for i in range(1, 25):
            self.VK[f"f{i}"] = 0x70 + i - 1  # F1 = 0x70
        for i in range(10):
            self.VK[str(i)] = 0x30 + i
        for c in "abcdefghijklmnopqrstuvwxyz":
            self.VK[c] = ord(c.upper())

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [
                ("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD),
            ]

        class INPUTUNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("union", INPUTUNION)]

        self._MOUSEINPUT = MOUSEINPUT
        self._KEYBDINPUT = KEYBDINPUT
        self._INPUT = INPUT
        self._INPUTUNION = INPUTUNION
        def send_input(inputs: list) -> None:
            n = len(inputs)
            arr = (INPUT * n)(*inputs)
            sent = self.user32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(INPUT))
            if sent != n:
                raise ComputerError(f"SendInput가 {sent}/{n} 개만 전달했습니다.")

        self._send_input = send_input

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        self._POINT = POINT
        self._RECT = RECT

        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
        self.user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        self.user32.GetSystemMetrics.restype = ctypes.c_int

    # ── 관측 ──────────────────────────────────────────────────────
    def observe_text(self) -> str:
        parts = [
            f"os={os.name}",
            f"cwd={os.getcwd()}",
            f"user={os.environ.get('USERNAME') or os.environ.get('USER') or ''}",
            f"home={str(Path.home())}",
        ]
        if _is_windows():
            parts.append(self._win_screen_summary())
            parts.append(self._win_windows_summary())
            uia = self._uia_dump()
            if uia:
                parts.append("foreground_ui=" + uia)
        else:
            parts.append("note=non-windows: mouse/keyboard/UIA unavailable; shell/files still work")
        text = "\n".join(parts)
        if len(text) > MAX_OBS_CHARS:
            text = text[: MAX_OBS_CHARS - 20] + "\n...[truncated]"
        return text

    def _win_screen_summary(self) -> str:
        SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
        SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
        vx = self.user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = self.user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = self.user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = self.user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        pt = self._POINT()
        self.user32.GetCursorPos(ctypes_byref_point(pt))
        hwnd = self.user32.GetForegroundWindow()
        title = self._window_title(hwnd)
        rect = self._window_rect(hwnd)
        return (
            f"virtual_screen=x{vx},y{vy},w{vw},h{vh}\n"
            f"cursor=x{pt.x},y{pt.y}\n"
            f"foreground={title!r} hwnd={int(hwnd)} rect={rect}"
        )

    def _window_title(self, hwnd: int) -> str:
        import ctypes
        n = self.user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 2)
        self.user32.GetWindowTextW(hwnd, buf, n + 2)
        return buf.value.strip()

    def _window_rect(self, hwnd: int) -> str:
        r = self._RECT()
        if not self.user32.GetWindowRect(hwnd, r):
            return "?"
        return f"x{r.left},y{r.top},w{r.right - r.left},h{r.bottom - r.top}"

    def _win_windows_summary(self) -> str:
        import ctypes
        from ctypes import wintypes

        rows: list[str] = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lp):
            if not self.user32.IsWindowVisible(hwnd):
                return True
            title = self._window_title(hwnd)
            if not title:
                return True
            rows.append(f"- {title} | {self._window_rect(hwnd)}")
            return len(rows) < 30

        self._enum_windows_proc_type = WNDENUMPROC
        self._last_enum_cb = WNDENUMPROC(_cb)
        self.user32.EnumWindows(self._last_enum_cb, 0)
        return "top_windows:\n" + ("\n".join(rows) if rows else "(none)")

    def _uia_dump(self) -> str:
        script = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
function Dump($el, $depth, $maxDepth, $maxNodes, [ref]$count) {
  if ($null -eq $el) { return $null }
  if ($count.Value -ge $maxNodes) { return $null }
  $count.Value++
  $r = $el.Current.BoundingRectangle
  $name = [string]$el.Current.Name
  if ($name.Length -gt 80) { $name = $name.Substring(0,80) }
  $obj = @{
    n = $name
    t = [string]$el.Current.ControlType.ProgrammaticName
    x = [int]$r.X; y = [int]$r.Y; w = [int]$r.Width; h = [int]$r.Height
    e = [bool]$el.Current.IsEnabled
    id = [string]$el.Current.AutomationId
  }
  if ($depth -lt $maxDepth) {
    $children = @()
    try {
      foreach ($c in $el.FindAll([Windows.Automation.TreeScope]::Children, [Windows.Automation.Condition]::TrueCondition)) {
        if ($count.Value -ge $maxNodes) { break }
        $nm = [string]$c.Current.Name
        $ct = [string]$c.Current.ControlType.ProgrammaticName
        if (-not $nm -and $ct -match 'Image|Separator|Thumb|ScrollBar') { continue }
        $d = Dump $c ($depth+1) $maxDepth $maxNodes $count
        if ($null -ne $d) { $children += $d }
      }
    } catch {}
    if ($children.Count) { $obj.c = $children }
  }
  return $obj
}
$root = [Windows.Automation.AutomationElement]::RootElement
$fw = [Windows.Automation.AutomationElement]::FocusedElement
$count = 0
$fg = $null
if ($fw) {
  $win = $fw
  $walker = [Windows.Automation.TreeWalker]::ControlViewWalker
  for ($i=0; $i -lt 12; $i++) {
    $p = $walker.GetParent($win)
    if (-not $p) { break }
    if ($p.Equals($root)) { break }
    $win = $p
  }
  $fg = Dump $win 0 4 90 ([ref]$count)
}
$fg | ConvertTo-Json -Depth 10 -Compress
"""
        raw = _run_powershell(script, timeout=UIA_TIMEOUT)
        raw = (raw or "").strip()
        if not raw:
            return ""
        try:
            self._last_uia = json.loads(raw)
        except json.JSONDecodeError:
            self._last_uia = None
            return raw[:4000]
        return raw[:9000]

    # ── 실행 ──────────────────────────────────────────────────────
    def perform(self, action: dict[str, Any]) -> str:
        op = str(action.get("op") or action.get("action") or "").strip().lower()
        if not op:
            raise ComputerError("op 필드가 없습니다.")
        if op in {"observe", "screenshot", "look"}:
            return "observed"
        if op in {"wait", "sleep"}:
            ms = float(action.get("ms") or action.get("seconds", 0) * 1000 or 400)
            time.sleep(max(0.0, min(ms, 30000)) / 1000.0)
            return f"waited {ms}ms"
        if op in {"click", "left_click", "leftclick"}:
            return self._click(action, button="left", clicks=int(action.get("clicks") or 1))
        if op in {"dblclick", "double_click", "doubleclick"}:
            return self._click(action, button="left", clicks=2)
        if op in {"rightclick", "right_click", "contextclick"}:
            return self._click(action, button="right", clicks=int(action.get("clicks") or 1))
        if op in {"middleclick", "middle_click"}:
            return self._click(action, button="middle", clicks=1)
        if op in {"move", "mousemove", "hover"}:
            x, y = _xy(action)
            self._move(x, y)
            return f"moved {x},{y}"
        if op == "drag":
            return self._drag(action)
        if op == "scroll":
            return self._scroll(action)
        if op in {"type", "typewrite", "insert"}:
            return self._type_text(str(action.get("text") or action.get("value") or ""))
        if op in {"key", "hotkey", "press", "keys"}:
            keys = action.get("keys") or action.get("key") or action.get("combo")
            return self._hotkey(keys)
        if op == "shell":
            return self._shell(str(action.get("cmd") or action.get("command") or ""), action.get("cwd"))
        if op == "open":
            return self._open(str(action.get("target") or action.get("path") or action.get("url") or ""))
        if op in {"focus", "activate", "activate_window"}:
            return self._focus(str(action.get("title") or action.get("name") or ""))
        if op == "clipboard":
            return self._clipboard(action)
        if op in {"read_file", "read"}:
            return self._read_file(str(action.get("path") or ""))
        if op in {"write_file", "write"}:
            return self._write_file(str(action.get("path") or ""), str(action.get("content") or action.get("text") or ""))
        if op in {"list_dir", "ls"}:
            return self._list_dir(str(action.get("path") or "."))
        if op in {"click_name", "click_element", "uia_click"}:
            return self._click_name(
                str(action.get("name") or action.get("title") or ""),
                str(action.get("role") or action.get("type") or ""),
            )
        if op == "done":
            return str(action.get("summary") or "done")
        raise ComputerError(f"알 수 없는 op: {op}")

    def _require_windows(self) -> None:
        if not _is_windows():
            raise ComputerError("이 조작은 Windows에서만 지원됩니다.")

    def _click(self, action: dict[str, Any], button: str, clicks: int) -> str:
        self._require_windows()
        if action.get("name") and "x" not in action and "y" not in action:
            return self._click_name(str(action["name"]), str(action.get("role") or ""))
        x, y = _xy(action)
        self._move(x, y)
        time.sleep(0.05)
        down, up = self._button_flags(button)
        for _ in range(max(1, min(clicks, 3))):
            self._mouse_flags(down)
            time.sleep(0.04)
            self._mouse_flags(up)
            time.sleep(0.06)
        return f"{button} click x{x},y{y} x{clicks}"

    def _button_flags(self, button: str) -> tuple[int, int]:
        b = (button or "left").lower()
        if b == "right":
            return self.MOUSEEVENTF_RIGHTDOWN, self.MOUSEEVENTF_RIGHTUP
        if b == "middle":
            return self.MOUSEEVENTF_MIDDLEDOWN, self.MOUSEEVENTF_MIDDLEUP
        return self.MOUSEEVENTF_LEFTDOWN, self.MOUSEEVENTF_LEFTUP

    def _move(self, x: int, y: int) -> None:
        self._require_windows()
        if not self.user32.SetCursorPos(int(x), int(y)):
            raise ComputerError(f"커서를 {x},{y} 로 이동하지 못했습니다.")

    def _mouse_flags(self, flags: int, data: int = 0) -> None:
        import ctypes
        inp = self._INPUT()
        inp.type = self.INPUT_MOUSE
        inp.union.mi = self._MOUSEINPUT(0, 0, data, flags, 0, None)
        self._send_input([inp])

    def _drag(self, action: dict[str, Any]) -> str:
        self._require_windows()
        x1 = int(action.get("x1") if action.get("x1") is not None else action.get("x"))
        y1 = int(action.get("y1") if action.get("y1") is not None else action.get("y"))
        x2 = int(action.get("x2") if action.get("x2") is not None else action.get("to_x"))
        y2 = int(action.get("y2") if action.get("y2") is not None else action.get("to_y"))
        self._move(x1, y1)
        time.sleep(0.05)
        self._mouse_flags(self.MOUSEEVENTF_LEFTDOWN)
        steps = max(8, min(40, int(action.get("steps") or 20)))
        for i in range(1, steps + 1):
            nx = int(x1 + (x2 - x1) * i / steps)
            ny = int(y1 + (y2 - y1) * i / steps)
            self._move(nx, ny)
            time.sleep(0.01)
        time.sleep(0.05)
        self._mouse_flags(self.MOUSEEVENTF_LEFTUP)
        return f"drag {x1},{y1} -> {x2},{y2}"

    def _scroll(self, action: dict[str, Any]) -> str:
        self._require_windows()
        if action.get("x") is not None and action.get("y") is not None:
            self._move(int(action["x"]), int(action["y"]))
            time.sleep(0.04)
        dy = int(action.get("dy") if action.get("dy") is not None else action.get("clicks") or action.get("amount") or -3)
        self._mouse_flags(self.MOUSEEVENTF_WHEEL, int(dy * self.WHEEL_DELTA) & 0xFFFFFFFF)
        return f"scroll dy={dy}"

    def _type_text(self, text: str) -> str:
        self._require_windows()
        if not text:
            return "typed empty"
        import ctypes
        inputs = []
        for ch in text:
            if ch == "\n":
                self._flush_keys(inputs)
                inputs = []
                self._hotkey(["enter"])
                continue
            if ch == "\t":
                self._flush_keys(inputs)
                inputs = []
                self._hotkey(["tab"])
                continue
            code = ord(ch)
            for up in (0, self.KEYEVENTF_KEYUP):
                inp = self._INPUT()
                inp.type = self.INPUT_KEYBOARD
                inp.union.ki = self._KEYBDINPUT(0, code, self.KEYEVENTF_UNICODE | up, 0, None)
                inputs.append(inp)
            if len(inputs) >= 40:
                self._flush_keys(inputs)
                inputs = []
        self._flush_keys(inputs)
        return f"typed {len(text)} chars"

    def _flush_keys(self, inputs: list) -> None:
        if inputs:
            self._send_input(inputs)
            time.sleep(0.01)

    def _hotkey(self, keys: Any) -> str:
        self._require_windows()
        seq = _normalize_keys(keys)
        if not seq:
            raise ComputerError("keys가 비어 있습니다.")
        vks = [self._vk(k) for k in seq]
        import ctypes
        inputs = []
        for vk in vks:
            inp = self._INPUT()
            inp.type = self.INPUT_KEYBOARD
            inp.union.ki = self._KEYBDINPUT(vk, 0, 0, 0, None)
            inputs.append(inp)
        for vk in reversed(vks):
            inp = self._INPUT()
            inp.type = self.INPUT_KEYBOARD
            inp.union.ki = self._KEYBDINPUT(vk, 0, self.KEYEVENTF_KEYUP, 0, None)
            inputs.append(inp)
        self._send_input(inputs)
        return "key " + "+".join(seq)

    def _vk(self, name: str) -> int:
        key = name.strip().lower()
        if key in self.VK:
            return self.VK[key]
        if len(key) == 1:
            return ord(key.upper())
        raise ComputerError(f"알 수 없는 키: {name}")

    def _shell(self, cmd: str, cwd: Any) -> str:
        if not cmd.strip():
            raise ComputerError("빈 셸 명령입니다.")
        workdir = str(cwd) if cwd else None
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                timeout=SHELL_TIMEOUT,
                cwd=workdir,
            )
        except subprocess.TimeoutExpired:
            return f"TIMEOUT after {SHELL_TIMEOUT}s: {cmd}"
        out = (proc.stdout or b"").decode("utf-8", errors="replace")
        err = (proc.stderr or b"").decode("utf-8", errors="replace")
        if not out and err and os.name == "nt":
            out = (proc.stdout or b"").decode("cp949", errors="replace")
            err = (proc.stderr or b"").decode("cp949", errors="replace")
        text = (out + ("\n" + err if err else "")).strip()
        if len(text) > MAX_SHELL_CHARS:
            text = text[: MAX_SHELL_CHARS - 20] + "\n...[truncated]"
        return f"exit={proc.returncode}\n{text or '(no output)'}"

    def _open(self, target: str) -> str:
        if not target:
            raise ComputerError("open 대상이 없습니다.")
        if _is_windows():
            os.startfile(target)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", target])
        return f"opened {target}"

    def _focus(self, title: str) -> str:
        self._require_windows()
        if not title:
            raise ComputerError("창 제목이 없습니다.")
        import ctypes
        from ctypes import wintypes

        found = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        needle = title.lower()

        def _cb(hwnd, _lp):
            if not self.user32.IsWindowVisible(hwnd):
                return True
            t = self._window_title(hwnd)
            if needle in t.lower():
                found.append((hwnd, t))
                return False
            return True

        self._last_enum_cb = WNDENUMPROC(_cb)
        self.user32.EnumWindows(self._last_enum_cb, 0)
        if not found:
            raise ComputerError(f"창을 찾지 못했습니다: {title}")
        hwnd, t = found[0]
        self.user32.ShowWindow(hwnd, self.SW_RESTORE)
        self.user32.SetForegroundWindow(hwnd)
        time.sleep(0.15)
        return f"focused {t!r}"

    def _clipboard(self, action: dict[str, Any]) -> str:
        act = str(action.get("mode") or action.get("action") or "").lower()
        if act in {"set", "write", "copy"} or (action.get("text") is not None and act != "get"):
            text = str(action.get("text") or action.get("value") or "")
            _run_powershell(
                "Set-Clipboard -Value " + _ps_single_quote(text),
                timeout=8,
            )
            return f"clipboard set ({len(text)} chars)"
        raw = _run_powershell("Get-Clipboard -Raw", timeout=8)
        text = (raw or "").rstrip("\r\n")
        if len(text) > 4000:
            text = text[:4000] + "...[truncated]"
        return "clipboard:\n" + text

    def _read_file(self, path: str) -> str:
        p = Path(path).expanduser()
        if not p.exists():
            raise ComputerError(f"파일 없음: {p}")
        data = p.read_text(encoding="utf-8", errors="replace")
        if len(data) > MAX_FILE_CHARS:
            data = data[: MAX_FILE_CHARS] + "\n...[truncated]"
        return f"file {p} ({p.stat().st_size} bytes):\n{data}"

    def _write_file(self, path: str, content: str) -> str:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"wrote {p} ({len(content)} chars)"

    def _list_dir(self, path: str) -> str:
        p = Path(path).expanduser()
        if not p.exists():
            raise ComputerError(f"경로 없음: {p}")
        names = []
        for child in list(p.iterdir())[:80]:
            tag = "d" if child.is_dir() else "f"
            names.append(f"{tag} {child.name}")
        return f"{p}\n" + "\n".join(names)

    def _click_name(self, name: str, role: str) -> str:
        self._require_windows()
        if not name:
            raise ComputerError("click_name에 name이 없습니다.")
        script = f"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$name = {_ps_single_quote(name)}
$role = {_ps_single_quote(role)}
$root = [Windows.Automation.AutomationElement]::RootElement
$cond = New-Object Windows.Automation.PropertyCondition(
  [Windows.Automation.AutomationElement]::NameProperty, $name)
$found = $root.FindAll([Windows.Automation.TreeScope]::Descendants, $cond)
$pick = $null
foreach ($el in $found) {{
  $t = [string]$el.Current.ControlType.ProgrammaticName
  if ($role -and ($t -notmatch [regex]::Escape($role))) {{ continue }}
  $pick = $el; break
}}
if (-not $pick -and $found.Count -gt 0) {{ $pick = $found[0] }}
if (-not $pick) {{ 'NOTFOUND'; exit 0 }}
$r = $pick.Current.BoundingRectangle
$cx = [int]($r.X + $r.Width/2)
$cy = [int]($r.Y + $r.Height/2)
try {{
  $inv = $pick.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern)
  if ($inv) {{ $inv.Invoke(); "INVOKED $cx $cy $($pick.Current.ControlType.ProgrammaticName)"; exit 0 }}
}} catch {{}}
"CLICK $cx $cy $($pick.Current.ControlType.ProgrammaticName)"
"""
        raw = (_run_powershell(script, timeout=UIA_TIMEOUT) or "").strip()
        if raw.startswith("NOTFOUND") or not raw:
            raise ComputerError(f"요소를 찾지 못했습니다: {name}")
        parts = raw.split()
        if parts[0] == "CLICK" and len(parts) >= 3:
            self._click({"x": int(parts[1]), "y": int(parts[2])}, button="left", clicks=1)
            return f"clicked {name} at {parts[1]},{parts[2]} ({' '.join(parts[3:])})"
        return raw


def ctypes_byref_point(pt: Any) -> Any:
    import ctypes
    return ctypes.byref(pt)


def _xy(action: dict[str, Any]) -> tuple[int, int]:
    if action.get("x") is None or action.get("y") is None:
        raise ComputerError("x,y 좌표가 필요합니다.")
    return int(action["x"]), int(action["y"])


def _normalize_keys(keys: Any) -> list[str]:
    if keys is None:
        return []
    if isinstance(keys, str):
        if "+" in keys:
            return [p.strip() for p in keys.split("+") if p.strip()]
        if "," in keys:
            return [p.strip() for p in keys.split(",") if p.strip()]
        return [keys.strip()]
    if isinstance(keys, list):
        out: list[str] = []
        for k in keys:
            out.extend(_normalize_keys(k))
        return out
    return [str(keys)]


def _ps_single_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _run_powershell(script: str, timeout: float = 12) -> str:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-STA",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    out = proc.stdout.decode("utf-8", errors="replace")
    if not out:
        out = proc.stdout.decode("cp949", errors="replace")
    return out
