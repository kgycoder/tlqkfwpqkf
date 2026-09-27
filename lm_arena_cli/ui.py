"""
LM Arena CLI - 터미널 UI (Claude Code / Gemini CLI 스타일 전면 리디자인)

- 계층적 아스키 아트 워드마크 + 그라디언트 컬러링 + 리빌 애니메이션
- system dot(·) 기반 상태 표시 / 이모지 완전 제거
- 입력창을 감싸는 박스 UI (입력 전후 모두 유지)
- 추론 스트리밍: 타임라인 바 + 페이드인 미리보기 전용 패널
- 부드러운 스피너/펄스 애니메이션 전 구간 적용

⚠️  이 파일은 "시각적 스킨"만 담당한다. 로그인 판별, DOM 파싱, 응답 완료
판정, 코드 블록 추출/저장 등 모든 동작 로직은 browser.py / cli.py /
file_writer.py에 그대로 있으며 이 파일에서는 절대 건드리지 않는다.
stream_panel()의 budget 계산 로직 역시 화면이 터미널 높이를 넘지 않도록
보장하는 핵심 로직이므로, 색상·아이콘·박스 스타일만 바꾸고 계산식 자체는
원본 그대로 유지한다.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import time
import threading
from contextlib import contextmanager

from rich.align import Align
from rich.columns import Columns
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text
from rich import box

from rich.table import Table

from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import get_app
from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style

from . import __version__ as _VERSION

console = Console()

BRAND = "LM Arena CLI"

# ── 컬러 팔레트 (Claude Code 산호/앰버 + Gemini 차가운 블루/민트) ──
ACCENT      = "#d97757"   # 주 강조 (따뜻한 산호)
ACCENT_SOFT = "#f2b880"   # 그라디언트용 밝은 앰버
SECONDARY   = "#7aa2f7"   # 보조 강조 (소프트 블루)
TERTIARY    = "#73daca"   # 세 번째 강조 (민트/틸)
CODE_ACCENT = "#9d7cf0"   # 코드 블록 전용 (바이올렛)
SUCCESS     = "#5fd68d"
WARNING     = "#f5c451"
DANGER      = "#f26d6d"
MUTED       = "grey54"
DIM_MUTED   = "grey35"
CODE_THEME  = "dracula"
PASTE_BG    = "#404040"   # 붙여넣기 플레이스홀더 배경(회색 칩)
PASTE_FG    = "#e6e6e6"   # 위 배경 위에서 잘 보이는 밝은 전경색

# ── 그라디언트 유틸 ──────────────────────────────────────────────────
import math


def _ease_out_cubic(t: float) -> float:
    """감속형 이징: 초반에 빠르게 움직이다 끝에서 부드럽게 멈춘다."""
    t = min(max(t, 0.0), 1.0)
    return 1 - (1 - t) ** 3


def _ease_in_out_sine(t: float) -> float:
    """가속-감속형 이징: 시작과 끝이 모두 부드러운 S자 곡선."""
    t = min(max(t, 0.0), 1.0)
    return -(math.cos(math.pi * t) - 1) / 2


def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _gradient_hex(t: float, c1: str, c2: str) -> str:
    r1, g1, b1 = _hex_rgb(c1)
    r2, g2, b2 = _hex_rgb(c2)
    r = round(r1 + (r2 - r1) * t)
    g = round(g1 + (g2 - g1) * t)
    b = round(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _scale_hex(color: str, factor: float) -> str:
    """색을 검은색 쪽으로 factor만큼 스케일(0=검정, 1=원본)해 페이드 효과에 쓴다."""
    factor = min(max(factor, 0.0), 1.0)
    r, g, b = _hex_rgb(color)
    return f"#{round(r * factor):02x}{round(g * factor):02x}{round(b * factor):02x}"


def _gradient_line(line: str, width: int, c1: str, c2: str, brightness: float = 1.0) -> Text:
    text = Text()
    for i, ch in enumerate(line):
        if ch == " ":
            text.append(" ")
        else:
            frac = i / max(width - 1, 1)
            color = _gradient_hex(frac, c1, c2)
            if brightness < 1.0:
                color = _scale_hex(color, brightness)
            text.append(ch, style=f"bold {color}")
    return text


# ── 부드러운 점(브레일) 스피너 ────────────────────────────────────────
class _DotSpinner:
    """
    AI 추론 중 표시되는 점 모양(⠋⠙⠹...) 스피너.

    기존에는 rich의 내장 스피너("dots8", "arc")를 사용했는데, 프레임
    순서가 회전 궤적과 맞지 않아 재생 시 점이 "튀는" 것처럼 부자연스럽게
    보였다. 여기서는 점이 실제로 시계 방향으로 한 바퀴씩 도는 순서로
    프레임을 재배열하고, 재생 속도도 살짝 늦춰(약 11fps) 눈에 편안하게
    보이도록 했다. 점 모양 자체(브레일 캐릭터)는 기존 스타일을 그대로
    유지한다.
    """

    _FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    _INTERVAL = 0.075  # 프레임당 재생 시간(초) — 기존(0.09)보다 살짝 빠르게 해 회전이 더 매끄럽게 보이도록 조정

    def __init__(self, style: str):
        self.style = style
        self._t0 = time.time()

    def render(self, now: float | None = None) -> Text:
        now = time.time() if now is None else now
        idx = int((now - self._t0) / self._INTERVAL) % len(self._FRAMES)
        return Text(self._FRAMES[idx], style=self.style)


# ── 아스키 아트 워드마크 ─────────────────────────────────────────────
_FONT: dict[str, list[str]] = {
    "L": ["█    ", "█    ", "█    ", "█    ", "█████"],
    "M": ["█   █", "██ ██", "█ █ █", "█   █", "█   █"],
    "A": [" ███ ", "█   █", "█████", "█   █", "█   █"],
    "R": ["████ ", "█   █", "████ ", "█  █ ", "█   █"],
    "E": ["█████", "█    ", "████ ", "█    ", "█████"],
    "N": ["█   █", "██  █", "█ █ █", "█  ██", "█   █"],
    " ": ["   ", "   ", "   ", "   ", "   "],
}


def _ascii_wordmark(word: str) -> list[str]:
    glyphs = [_FONT.get(ch.upper(), _FONT[" "]) for ch in word]
    return [" ".join(glyph[row] for glyph in glyphs) for row in range(5)]


# ── 부팅 배너 ───────────────────────────────────────────────────────
def banner() -> None:
    """
    아스키 아트 워드마크를 산호→블루 그라디언트로 물들이며 한 줄씩 리빌한다.
    하단에는 버전 정보와 구분선을 배치한다.
    """
    rows = _ascii_wordmark("LM ARENA")
    width = max(len(r) for r in rows)
    colored_rows = [_gradient_line(r, width, ACCENT, SECONDARY) for r in rows]

    console.print()
    with Live(console=console, auto_refresh=False, transient=False) as live:
        revealed: list[Text] = []
        fade_steps = 6
        for row_idx, row in enumerate(colored_rows):
            # 각 줄을 한 번에 팍 띄우지 않고, 어두운 색 -> 원래 색으로
            # 부드럽게 밝아지는 페이드인을 적용한다(이징: ease-out-cubic).
            for step in range(1, fade_steps + 1):
                t = step / fade_steps
                brightness = _ease_out_cubic(t)
                fading_row = _gradient_line(rows[row_idx], width, ACCENT, SECONDARY, brightness=brightness)
                live.update(Align.center(Group(*revealed, fading_row)), refresh=True)
                time.sleep(0.014)
            revealed.append(row)
            live.update(Align.center(Group(*revealed)), refresh=True)
            time.sleep(0.02)

    console.print()

    # 버전 배지 + 태그라인
    version_badge = Text()
    version_badge.append("  v", style=f"dim {MUTED}")
    version_badge.append(_VERSION, style=f"bold {ACCENT_SOFT}")
    version_badge.append("  ", style="")

    dot_sep = Text("  ·  ", style=f"dim {DIM_MUTED}")

    tagline = Text()
    tagline.append("Playwright", style=f"bold {SECONDARY}")
    tagline.append(" · ", style=f"dim {DIM_MUTED}")
    tagline.append("LM Arena Direct", style=f"bold {TERTIARY}")
    tagline.append(" · ", style=f"dim {DIM_MUTED}")
    tagline.append("코딩 에이전트", style=f"{MUTED}")

    meta_line = Text()
    meta_line.append_text(version_badge)
    meta_line.append("  ")
    meta_line.append_text(tagline)

    console.print(Align.center(meta_line))
    console.print()
    console.print(Rule(style=f"dim {DIM_MUTED}"))
    console.print()


# ── 부팅 단계 스피너 ─────────────────────────────────────────────────
def boot_step(message: str):
    """부팅 단계(예: 'Playwright 초기화 중...')를 dots12 스피너와 함께 표시."""
    return console.status(
        f"[bold {SECONDARY}]  {message}[/]",
        spinner="dots12",
        spinner_style=SECONDARY,
        refresh_per_second=20,  # 기본값(12.5)보다 높여 스피너 회전을 더 매끄럽게
    )


# ── 상태 메시지 (system dot 기반, 이모지 없음) ─────────────────────
def info(msg: str) -> None:
    console.print(f"  [{SECONDARY}]·[/] {msg}")


def success(msg: str) -> None:
    console.print(f"  [bold {SUCCESS}]+[/] {msg}")


def warn(msg: str) -> None:
    console.print(f"  [bold {WARNING}]![/] {msg}")


def error(msg: str) -> None:
    console.print(f"  [bold {DANGER}]x[/] {msg}")


# ── 로그인 / 세션 상태 패널 ─────────────────────────────────────────
def login_status(logged_in: bool) -> None:
    if logged_in:
        body = Text()
        body.append("+ ", style=f"bold {SUCCESS}")
        body.append("로그인 상태 확인됨", style=f"bold {SUCCESS}")
        body.append("   ·   ", style=f"dim {DIM_MUTED}")
        body.append("저장된 세션 사용 중", style=MUTED)
        console.print(
            Panel(
                body,
                border_style=SUCCESS,
                box=box.ROUNDED,
                padding=(0, 2),
            )
        )
    else:
        body = Text()
        body.append("! ", style=f"bold {WARNING}")
        body.append("로그인이 필요합니다\n", style=f"bold {WARNING}")
        body.append("  브라우저 창에서 로그인을 완료해 주세요. ", style=MUTED)
        body.append("완료되면 자동으로 감지하여 다음 단계로 진행됩니다.", style=MUTED)
        console.print(
            Panel(
                body,
                border_style=WARNING,
                box=box.ROUNDED,
                padding=(0, 2),
            )
        )


def session_header(target_dir: str) -> None:
    """현재 세션의 상태(작업 폴더 등)를 패널로 보여준다."""
    # 짧은 "준비 중 -> 준비 완료" 펄스 애니메이션 (계단식 점 대신 연속 회전
    # 스피너를 사용해 더 매끄럽게 보이도록 함)
    pulse_spinner = _DotSpinner(SECONDARY)
    with Live(console=console, auto_refresh=False, transient=True) as live:
        t0 = time.time()
        duration = 0.42
        while time.time() - t0 < duration:
            t = Text()
            t.append("   ")
            t.append_text(pulse_spinner.render())
            t.append(" 세션 초기화 중...", style=SECONDARY)
            live.update(t, refresh=True)
            time.sleep(0.02)

    # 준비 완료 패널
    left = Text()
    left.append("  DIR  ", style=f"bold {SECONDARY} on #1a1a2e")
    left.append(f"  {target_dir}", style="")

    right = Text()
    right.append("  READY  ", style=f"bold {SUCCESS} on #0d1f0d")
    right.append("  프롬프트를 입력하세요", style=f"{SUCCESS}")

    body = Text()
    body.append("  DIR   ", style=f"bold {SECONDARY}")
    body.append(target_dir, style="")
    body.append("\n")
    body.append("  STATUS ", style=f"bold {SUCCESS}")
    body.append("Ready", style=f"bold {SUCCESS}")
    body.append("  ·  ", style=f"dim {DIM_MUTED}")
    body.append("프롬프트를 아래 입력창에 입력하세요", style=MUTED)

    console.print(
        Panel(
            body,
            border_style=MUTED,
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )
    console.print()


# ── 폴더 선택 프롬프트 ───────────────────────────────────────────────
def ask_target_dir(default: str = "") -> str:
    """
    작업 폴더 경로를 입력받는다.
    default가 주어지면(마지막 사용 폴더) Enter만 눌러도 그 폴더가 사용된다.
    """
    if default:
        info(f"마지막으로 사용한 폴더: [bold]{default}[/]")
        return Prompt.ask(
            f"[bold {SECONDARY}]  DIR[/] [dim](Enter = 마지막 폴더 유지)[/]",
            default=default,
            console=console,
        )
    return Prompt.ask(
        f"[bold {SECONDARY}]  DIR[/] [dim]LM Arena가 파일을 쓰고 수정할 폴더 경로[/]",
        console=console,
    )


# ── 입력창 박스 (prompt_toolkit 기반 — 줄바꿈에도 안전하게 늘어난다) ──
#
# [이전 방식의 문제]
# 이전 버전은 파이썬 내장 input()을 사용하면서, 박스의 상/하단 테두리를
# 미리 출력해 둔 뒤 "\x1b[2F\x1b[3C" 같은 ANSI 이스케이프로 커서를
# 고정된 줄 수(상단 1줄 + 입력 1줄 + 하단 1줄)만큼 이동시켜 그 위에서
# 타이핑을 받는 방식이었다. 이 계산은 프롬프트가 "터미널 폭을 넘지
# 않는 한 줄"이라는 전제에서만 맞는다. 한국어처럼 한 글자가 2칸을
# 차지하는 문자가 섞여 있거나 입력이 길어져 터미널이 자동으로 줄을
# 감싸면(wrap), 실제로 화면에 그려지는 줄 수가 예상보다 늘어나서:
#   - 커서 이동 거리가 어긋나 하단 테두리를 엉뚱한 자리에 덮어써 버리고
#     (하단 테두리가 "사라진 것처럼" 보임)
#   - 두 번째 줄부터는 왼쪽 세로 테두리("│  ")를 다시 그려줄 방법이
#     없어 테두리가 끊겨 버렸다(세로줄이 연장되지 않음).
#
# [새 방식]
# 커서를 손으로 이동시키는 대신, 화면에 그려지는 줄 수를 스스로 계산해
# 정확히 다시 그려주는 라이브러리(prompt_toolkit)에게 렌더링을 맡긴다.
#   - get_line_prefix / prompt_continuation: 입력이 터미널 폭을 넘어
#     자동으로 줄바꿈될 때마다(2번째, 3번째, ... 줄 모두) 매번 호출되어
#     왼쪽 세로 테두리("│  ")를 다시 붙여준다 -> 세로줄이 항상 연장됨.
#   - bottom_toolbar: 입력 영역 바로 "아래"에 붙어 함께 다시 그려지는
#     영역이라, 입력이 몇 줄로 늘어나든 하단 테두리가 항상 입력 바로
#     아래에 그대로 유지된다 -> 하단 테두리가 사라지지 않음.
#   - 한/영 여부와 무관하게 문자 폭(wide character)을 올바르게 계산해
#     주므로 한국어 입력에서도 줄바꿈 위치가 어긋나지 않는다.
#
# 로그인/DOM/전송 로직은 전혀 건드리지 않는다 — 이 함수는 여전히
# 사용자가 입력한 "문자열"만 돌려주며, cli.py가 그 문자열로 무엇을
# 하는지는 기존과 완전히 동일하다.
_BOX_COLOR = SECONDARY
_box_pending = False  # 방금 전송으로 이어질 입력을 받았는지 여부 (echo에서 사용)

# ── 슬래시 커맨드 (Claude Code / Gemini CLI 스타일) ─────────────────
# 이 목록이 /help 출력과 "/" 자동완성의 유일한 출처(single source of
# truth)다. 새 커맨드를 추가하려면 여기에 한 줄만 추가하면 되고,
# 실제 처리 로직은 cli.py의 _handle_slash_command()에서 담당한다
# (이 파일은 어디까지나 "시각적 스킨"만 담당한다는 원칙을 유지).
SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/help", "사용 가능한 명령어 목록을 봅니다"),
    ("/new", "새로운 채팅 세션을 시작합니다 (사이드바의 New Chat)"),
    ("/clear", "이전 대화 UI만 화면에서 치웁니다 (배너/세션은 유지)"),
    ("/status", "현재 로그인 상태를 다시 확인합니다"),
    ("/target", "작업 폴더를 확인하거나 변경합니다 (예: /target ~/project)"),
    ("/pwd", "현재 작업 폴더 경로를 출력합니다"),
    ("/filename", "코드 블록 첫 줄에 파일명 주석 표기를 요청하는 지침 전송 on/off (예: /filename off)"),
    ("/workspace", "작업 폴더 전체를 LM Arena에게 컨텍스트로 공유 on/off (예: /workspace on)"),
    ("/work", "LM Arena가 이 PC를 직접 조작하는 Work 에이전트 on/off (예: /work on)"),
    ("/mode", "채팅 모드를 바꿉니다 - direct 또는 agent (예: /mode agent)"),
    ("/review", "Agent 프로젝트 평가를 제출합니다 - yes / no / continue (예: /review yes)"),
    ("/download", "Agent workspace를 zip으로 받아 작업 폴더에 풉니다"),
    ("/logout", "저장된 브라우저 로그인 세션(쿠키)을 삭제합니다"),
    ("/version", "CLI 버전 정보를 표시합니다"),
    ("/exit", "세션을 종료합니다"),
    ("/quit", "세션을 종료합니다 (/exit 과 동일)"),
]


def _matching_slash_commands(text: str) -> list[str]:
    """현재 입력 텍스트로 시작하는 슬래시 커맨드 이름 목록(등록 순서 유지)."""
    if not text.startswith("/"):
        return []
    return [name for name, _ in SLASH_COMMANDS if name.startswith(text)]


class _SlashCommandSuggest(AutoSuggest):
    """
    입력이 '/'로 시작할 때만, 커서 뒤에 회색 이탤릭 "고스트 텍스트"로
    가장 가까운 커맨드의 나머지 부분을 미리 보여준다.

    prompt_toolkit의 completion 팝업(Completer)과 달리 화면 공간을 전혀
    추가로 차지하지 않는 한 줄짜리 인라인 힌트라서, 기존 박스 UI의 높이
    계산/테두리 렌더링과 절대 충돌하지 않는다. Tab 키로 수락한다.
    """

    def get_suggestion(self, buffer, document) -> Suggestion | None:
        text = document.text_before_cursor
        if "\n" in text or not text.startswith("/"):
            return None
        candidates = sorted(n for n in _matching_slash_commands(text) if n != text)
        if not candidates:
            return None
        remainder = candidates[0][len(text):]
        return Suggestion(remainder) if remainder else None


def print_help() -> None:
    """/help 명령어 처리 시 호출되는 명령어 목록 테이블 출력."""
    table = Table(
        show_header=True,
        header_style=f"bold {SECONDARY}",
        box=box.ROUNDED,
        border_style=DIM_MUTED,
        title=f"[bold {ACCENT}]사용 가능한 명령어[/]",
        title_justify="left",
    )
    table.add_column("명령어", style=f"bold {ACCENT_SOFT}", no_wrap=True)
    table.add_column("설명", style=MUTED)
    for name, desc in SLASH_COMMANDS:
        table.add_row(name, desc)
    console.print(table)
    console.print(
        f"  [dim {DIM_MUTED}]'/'를 입력하면 회색 글자로 다음 후보가 미리 보이고, "
        f"Tab 키로 완성할 수 있습니다.[/]"
    )


_input_session: PromptSession | None = None

# prompt_toolkit 기본 스타일에는 "bottom-toolbar" 클래스가 'reverse'
# (전경/배경 반전)로 정의되어 있다. 그래서 우리가 지정한 전경색(fg:)이
# 오히려 배경색처럼 칠해져, 입력창 하단 테두리에 원치 않는 파란
# 배경이 나타난다. 여기서 그 반전을 꺼서 우리가 지정한 색이 있는
# 그대로 글자색(전경색)으로만 적용되게 한다.
_PT_STYLE = Style.from_dict({
    "bottom-toolbar": "noreverse",
    # "/" 커맨드 입력 중 커서 뒤에 붙는 고스트 텍스트(자동완성 미리보기)
    # 색상. 팝업 메뉴 없이 한 줄 안에서만 표시되므로 박스 UI의 높이/
    # 테두리 계산에는 전혀 영향을 주지 않는다.
    # 주의: DIM_MUTED("grey35")는 rich 전용 색상 이름이라 prompt_toolkit이
    # 인식하지 못해 ValueError가 났었다("Wrong color format"). 여기서는
    # 반드시 hex 코드를 써야 한다(같은 회색조의 #565656로 지정).
    "auto-suggestion": "fg:#565656 italic",
})


# ── [버그 수정] 입력창이 커진 뒤 줄어들지 않는 문제 ──────────────────
#
# 원인: prompt_toolkit의 Renderer.render()는 매 프레임 높이를
#   height = max(min_available_height, 이전 프레임 높이, 이번 프레임에
#                 실제로 필요한 높이)
# 로 계산한다(공식 소스: prompt_toolkit/renderer.py Renderer.render).
# 즉 "이전 프레임 높이"가 항상 바닥값(최소값)으로 강제되기 때문에,
# 긴 텍스트를 입력/붙여넣어 입력창이 여러 줄로 늘어난 뒤 그 텍스트를
# 지워도 실제로 필요한 높이는 줄었는데 화면에는 예전의 큰 높이가 계속
# 유지되어(=빈 여백만 남아) 테두리 크기가 내용과 동기화되지 않는 것처럼
# 보인다. (일반적인 한 줄 프롬프트에서는 거의 드러나지 않지만, 우리처럼
# 늘었다 줄었다 하는 멀티라인 박스 UI에서는 바로 눈에 띈다.)
#
# 해결: "이전 프레임 높이"는 Renderer가 내부적으로 기억해 둔 이전 화면
# (_last_screen)의 높이에서 온다. 실제 내용이 줄어드는 시점에 이 캐시를
# 지워주면, 다음 렌더에서는 바닥값이 0이 되어 높이가 실제 내용 크기로
# 다시 줄어든다. Renderer.reset()을 통째로 호출하면 마우스 지원/브라켓
# 붙여넣기 비활성화, 커서 위치 초기화 등 타이핑 도중에는 부적절한
# 부작용이 함께 발생하므로, 높이 계산에만 관여하는 캐시 하나만 골라서
# 지운다(다른 상태는 전혀 건드리지 않음 -> 로그인/DOM/전송 로직과 무관).
_prev_wrapped_line_count = [1]


def _estimate_wrapped_line_count(text: str, width: int) -> int:
    """
    현재 입력 버퍼 텍스트가 (좌측 "│  " 테두리를 제외한) 실제 사용
    가능한 폭(width) 안에서 몇 줄로 시각적으로 줄바꿈될지 추정한다.
    Rich의 Text.wrap()을 그대로 사용해 한글 등 넓은 문자(wide character)
    셀 폭까지 정확히 반영한다(user_prompt_echo()에서 쓰는 방식과 동일).
    """
    total = 0
    for line in text.split("\n"):
        if not line:
            total += 1
            continue
        wrapped = Text(line).wrap(console, width) or [Text("")]
        total += len(wrapped)
    return max(total, 1)


def _shrink_guard_on_text_changed(buf) -> None:
    """
    버퍼 텍스트가 바뀔 때마다(타이핑/삭제/붙여넣기 모두 포함) 호출되어,
    실제로 필요한 줄 수가 "줄어들었을 때만" 렌더러의 높이 캐시를 지워
    다음 프레임에서 박스가 올바르게 작아지도록 만든다. 늘어나는
    경우에는 원래도 정상 동작하므로 아무것도 하지 않는다(불필요한
    전체 다시 그리기로 인한 깜빡임 방지).
    """
    global _prev_wrapped_line_count
    try:
        width = max(_border_width() - 3, 10)
        new_count = _estimate_wrapped_line_count(buf.text, width)
    except Exception:
        return

    if new_count < _prev_wrapped_line_count[0]:
        try:
            get_app().renderer._last_screen = None
        except Exception:
            # prompt_toolkit 내부 구현이 바뀌어 속성이 없어지는 등의 경우
            # 조용히 무시한다 - 최악의 경우 기존 동작(테두리 안 줄어듦)으로
            # 되돌아갈 뿐, 다른 기능에는 영향이 없다.
            pass

    _prev_wrapped_line_count[0] = new_count


# ── [디자인] 붙여넣기 플레이스홀더에 회색 배경 칩 표시 ──────────────
#
# 버퍼에 삽입되는 "[Pasted text +N lines]" / "[Pasted text #K +N lines]"
# 라벨은 그 자체가 일반 텍스트라서 원래는 배경색이 없었다. 이 Lexer는
# 화면에 그려질 때만 그 라벨 구간에 회색 배경(PASTE_BG)을 입혀 "칩"처럼
# 보이게 한다 - 버퍼의 실제 문자열은 전혀 건드리지 않는다.
#
# "박스가 제거되면 배경도 같이 제거되어야 한다"는 요구사항은 이 방식
# 자체로 항상 만족된다: 매 렌더마다 현재 버퍼 텍스트를 다시 스캔해서
# 라벨이 남아있는 구간에만 배경을 입히므로, 백스페이스/Delete로 라벨
# 전체가 지워지면(_remove_label 등 기존 로직 그대로) 다음 렌더에서는
# 그 구간 자체가 존재하지 않아 배경도 자동으로 사라진다.
class _PasteHighlightLexer(Lexer):
    def lex_document(self, document):
        lines = document.lines

        def get_line(lineno: int):
            line = lines[lineno] if 0 <= lineno < len(lines) else ""
            fragments = []
            pos = 0
            for m in _PASTE_LABEL_FULL_RE.finditer(line):
                if m.start() > pos:
                    fragments.append(("", line[pos:m.start()]))
                fragments.append((f"bg:{PASTE_BG} {PASTE_FG}", m.group(0)))
                pos = m.end()
            if pos < len(line):
                fragments.append(("", line[pos:]))
            if not fragments:
                fragments.append(("", line))
            return fragments

        return get_line


def _get_input_session() -> PromptSession:
    """PromptSession은 내부적으로 히스토리 등 상태를 갖고 있으므로 재사용한다.

    erase_when_done=True: 입력이 끝나면 prompt_toolkit이 방금 그렸던
    입력 영역(플레이스홀더가 보이던 줄들 포함)을 화면에서 지운다. 이렇게
    해야 user_prompt_echo()가 붙여넣기 플레이스홀더가 아닌 "실제 원문
    텍스트"를 깔끔하게 한 번만 다시 그릴 수 있다(지우지 않으면 화면에
    플레이스홀더가 남아있는 상태에서 원문이 추가로 출력되어 중복되어
    보인다).
    """
    global _input_session
    if _input_session is None:
        _input_session = PromptSession(
            erase_when_done=True,
            lexer=_PasteHighlightLexer(),
            auto_suggest=_SlashCommandSuggest(),
        )
        # 세션(=버퍼)은 여러 턴에 걸쳐 재사용되므로, 핸들러도 여기서 딱
        # 한 번만 등록한다(중복 등록 방지).
        _input_session.default_buffer.on_text_changed += _shrink_guard_on_text_changed
    return _input_session


def _border_width() -> int:
    # 터미널 폭보다 1칸 여유를 둬서, 폭 계산이 살짝 어긋나거나 폰트가
    # 넓은 문자를 만나도 절대 줄바꿈되지 않도록 안전 마진을 확보한다.
    return max(console.width - 1, 20)


def _top_border_line(title: str, color: str) -> Text:
    """
    상단 테두리 "╭─  Title ───...───╮"를 만든다. 전체 길이가 터미널
    폭과 정확히 일치하도록(1글자도 넘치지 않도록) 대시 개수를 역산한다:
        "╭─"(2) + label + dashes + "╮"(1) == width
    """
    width = _border_width()
    label = f"  {title} "
    dash_total = max(width - 2 - len(label) - 1, 4)
    line = Text()
    line.append("╭─", style=color)
    line.append(label, style=f"bold {color}")
    line.append("─" * dash_total, style=color)
    line.append("╮", style=color)
    return line


def _animate_open_box(title: str, color: str) -> None:
    """상단 테두리가 왼쪽에서 오른쪽으로 자라나는 짧은 리빌 애니메이션."""
    width = _border_width()
    label = f"  {title} "
    dash_total = max(width - 2 - len(label) - 1, 4)
    steps = 22

    with Live(console=console, auto_refresh=False, transient=False) as live:
        for i in range(1, steps + 1):
            t = i / steps
            eased = _ease_out_cubic(t)
            filled = round(dash_total * eased)
            line = Text()
            line.append("╭─", style=color)
            line.append(label, style=f"bold {color}")
            line.append("─" * filled, style=color)
            if i == steps:
                line.append("╮", style=color)
            live.update(line, refresh=True)
            time.sleep(0.007)


def _bottom_border_plain(color: str) -> FormattedText:
    """
    입력 "도중"에만 쓰인다. prompt_toolkit의 bottom_toolbar로 전달되어,
    입력이 몇 줄로 늘어나든(줄바꿈되든) 항상 입력 영역 바로 아래에
    다시 그려진다 — 이게 기존 방식과의 핵심 차이다.

    ⚠️ prompt_toolkit은 Enter가 눌려 세션이 끝나는 순간(is_done) 이
    bottom_toolbar를 자동으로 화면에서 지워버린다. 그래서 최종적으로
    보이는 닫힌 테두리는 이 함수가 아니라 세션 종료 "직후" 별도로
    한 번 출력하는 _bottom_border_line()이 담당한다.
    """
    width = _border_width()
    return FormattedText([(f"fg:{color}", f"╰{'─' * (width - 2)}╯")])


def _bottom_border_command_hint(matches: list[str], color: str) -> FormattedText:
    """
    '/' 커맨드 입력 중, 팝업 메뉴 대신 기존 닫힌 하단 테두리의 라벨
    자리에 일치하는 커맨드 이름들을 보여준다. 항상 딱 1줄만 차지하므로
    (팝업처럼 별도 공간을 예약하지 않음) 박스 UI의 높이 계산과 절대
    충돌하지 않는다. 커맨드 이름은 전부 ASCII라서(설명 문구는 넣지
    않음) 폭 계산이 한글 등 넓은 문자 걱정 없이 항상 정확하다.
    """
    width = _border_width()
    if matches:
        label = " " + "  ".join(matches[:5])
        if len(matches) > 5:
            label += f"  +{len(matches) - 5}"
        label += " "
        label_style = f"bold fg:{ACCENT_SOFT}"
    else:
        label = " 일치하는 명령어 없음 "
        label_style = f"fg:{DANGER}"

    max_label = max(width - 6, 4)
    if len(label) > max_label:
        label = label[: max_label - 2].rstrip() + "… "

    dash_total = max(width - 2 - len(label) - 2, 2)
    return FormattedText([
        (f"fg:{color}", "╰─"),
        (f"fg:{color}", "─" * dash_total),
        (label_style, label),
        (f"fg:{color}", "─╯"),
    ])


def _bottom_border_line(label: str, color: str, weight: str = "bold") -> Text:
    """
    입력이 끝난 "직후" 한 번만 출력하는, 라벨이 포함된 닫힌 하단 테두리
    "╰─────... label ──╯". prompt_toolkit 세션이 끝나면 커서가 이미
    입력 영역(줄바꿈 포함) 바로 다음 줄에 가 있으므로, 커서를 옮기는
    계산 없이 그 자리에 그대로 이어서 출력하기만 하면 된다. 전체 길이가
    터미널 폭과 정확히 일치하도록 대시 개수를 역산한다:
        "╰─"(2) + dashes + " label "(len(label)+2) + "─╯"(2) == width
    """
    width = _border_width()
    label_str = f" {label} " if label else ""
    dash_total = max(width - 2 - len(label_str) - 2, 4)
    line = Text()
    line.append("╰─", style=color)
    line.append("─" * dash_total, style=color)
    if label_str:
        line.append(label_str, style=f"{weight} {color}")
    line.append("─╯", style=color)
    return line


_PASTE_LABEL_RE = re.compile(r"\+(\d+) lines")
# Lexer가 배경을 입힐 범위를 찾기 위한, 라벨 전체("[Pasted text ...]")를
# 매칭하는 정규식. 위 _PASTE_LABEL_RE(숫자 부분만 매칭)와는 용도가 다르다.
_PASTE_LABEL_FULL_RE = re.compile(r"\[Pasted text(?: #\d+)? \+\d+ lines\]")

_last_display_text = ""  # 방금 전송된 입력의 "화면 표시용"(플레이스홀더 유지) 원문


def _relabel_buffer_text(buf, old_label: str, new_label: str) -> None:
    """
    이미 버퍼에 삽입된 플레이스홀더 라벨 문자열을 다른 라벨 문자열로
    치환한다(예: 두 번째 붙여넣기가 발생해 첫 번째 라벨을 "#1"이 붙은
    형태로 바꿔야 할 때). 커서 위치는 치환된 길이 차이만큼 보정한다.
    """
    text = buf.text
    idx = text.find(old_label)
    if idx == -1:
        return
    cursor_pos = buf.cursor_position
    new_text = text[:idx] + new_label + text[idx + len(old_label):]
    delta = len(new_label) - len(old_label)
    buf.text = new_text
    if cursor_pos >= idx + len(old_label):
        buf.cursor_position = min(cursor_pos + delta, len(new_text))
    else:
        buf.cursor_position = min(cursor_pos, len(new_text))


# Shift+Enter 지원을 위한 확장 키보드 프로토콜 활성화/비활성화 시퀀스.
#
# 표준 터미널은 Enter와 Shift+Enter를 완전히 같은 바이트("\r")로 보내기
# 때문에, 어떤 애플리케이션도 이 둘을 구분할 수 없다. 터미널이 modifier
# 정보를 실어 보내주는 확장 프로토콜(Kitty keyboard protocol 또는 xterm의
# modifyOtherKeys)을 먼저 요청해 둬야만 Shift+Enter가 "\x1b[13;2u" 같은
# 구분 가능한 시퀀스로 도착한다. 프로토콜을 모르는 터미널은 이 시퀀스들을
# 그냥 무시하므로 부작용이 없다.
_ENABLE_KEY_PROTOCOL = "\x1b[>1u" + "\x1b[>4;2m"
_DISABLE_KEY_PROTOCOL = "\x1b[<u" + "\x1b[>4;0m"


def _write_raw_to_terminal(data: str) -> None:
    """
    터미널로 이스케이프 시퀀스를 "즉시, 다른 어떤 버퍼링도 거치지 않고"
    내보낸다. sys.stdout.write()는 Rich Console이나 다른 레이어가 걸어둔
    버퍼링/래핑을 거칠 수 있어 타이밍이 어긋날 수 있으므로, 파일 디스크립터
    1(표준출력)에 직접 os.write()로 기록한다.
    """
    if sys.platform == "win32" or not sys.stdout.isatty():
        return
    try:
        os.write(1, data.encode("utf-8", errors="ignore"))
    except OSError:
        pass


def _enable_key_protocol(_app=None) -> None:
    """
    확장 키보드 프로토콜을 요청한다. prompt_toolkit의 ``pre_run`` 훅으로
    등록해 "터미널이 raw 모드로 전환된 직후" 시점에 실행되게 한다. 이
    시점이 중요한데, session.prompt() 호출 "이전"에 미리 보내면 이후
    prompt_toolkit이 터미널 모드를 raw로 전환하면서 일부 터미널이 상태를
    재설정해 버려 요청이 무시되는 경우가 있었다. pre_run 시점에 보내면
    이미 raw 모드가 적용된 뒤이므로 이런 문제가 없다.
    """
    _write_raw_to_terminal(_ENABLE_KEY_PROTOCOL)


def _disable_key_protocol() -> None:
    """세션이 끝나면(터미널이 cooked 모드로 돌아온 뒤) 원래 상태로 되돌린다."""
    _write_raw_to_terminal(_DISABLE_KEY_PROTOCOL)


def _patch_shift_enter_sequences() -> None:
    """
    Shift+Enter를 실제로 줄바꿈으로 동작시키기 위한 저수준 보정.

    prompt_toolkit에는 "Shift+Enter"라는 이름의 키가 아예 존재하지 않고,
    확장 키보드 프로토콜(Kitty CSI-u, xterm modifyOtherKeys)이 보내는
    Shift+Enter 시퀀스를 직접 다루려면 원시 이스케이프 시퀀스를 문자 단위로
    등록해야 하는데, 이 방식은 맨 앞 ESC 문자가 항상 Keys.Escape로 먼저
    해석되어 버려서 실질적으로 동작하지 않는다.

    더 나쁜 점은, prompt_toolkit이 기본 제공하는 시퀀스 테이블
    (ANSI_SEQUENCES)에는 xterm의 modifyOtherKeys 방식 Shift+Enter 시퀀스인
    "\x1b[27;2;13~"가 실수로(?) 일반 Enter와 동일한 Keys.ControlM으로
    매핑되어 있다는 것이다. 이 때문에 이 프로토콜을 지원하는 터미널에서
    Shift+Enter를 누르면 줄바꿈이 아니라 "그냥 즉시 전송"되어 버린다.

    해결책: 이 시퀀스들을 prompt_toolkit이 내부적으로 "특별한 동작이
    없는 키"로 이미 취급하는 Keys.Ignore로 재매핑한다. 그러면 이후
    KeyBindings에서 Keys.Ignore 하나에만 줄바꿈 핸들러를 걸어주는 것으로
    Kitty/xterm 두 프로토콜의 Shift+Enter를 모두 처리할 수 있다. 이
    프로토콜들을 아예 모르는 터미널에는 영향이 없다(원래부터 이 시퀀스를
    보내지 않으므로).
    """
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES

_patch_shift_enter_sequences()


def _patch_windows_shift_enter() -> None:
    """
    Windows 콘솔 환경에서 Shift+Enter가 줄바꿈이 아니라 항상 "즉시 전송"
    으로 이어지던 근본 원인을 고친다.

    처음엔 Windows Console API가 보내는 KEY_EVENT_RECORD의
    ControlKeyState 비트(Shift 눌림 여부)를 직접 읽는 ConsoleInputReader
    쪽만 패치했었다. 하지만 그것만으로는 부족했다: prompt_toolkit은
    Windows 10 이상 + Windows Terminal처럼 ENABLE_VIRTUAL_TERMINAL_INPUT
    콘솔 모드를 켤 수 있는 환경에서는 ConsoleInputReader 대신
    **Vt100ConsoleInputReader**를 사용하도록 자동으로 전환해 버린다
    (`Win32Input.__init__`의 `_is_win_vt100_input_enabled()` 검사).
    Vt100ConsoleInputReader는 KEY_EVENT_RECORD의 ControlKeyState를 아예
    보지 않고, 문자 하나하나를 유닉스와 동일한 Vt100Parser/ANSI_SEQUENCES
    경로로 흘려보낸다 — 즉 Shift가 눌렸는지 여부 자체가 반영될 방법이
    없다. 이 때문에 ConsoleInputReader 하나만 패치해서는 Windows
    Terminal에서 실제로 사용되는 클래스(Vt100ConsoleInputReader)에는
    전혀 영향을 주지 못했던 것이다 — 이번 문제가 계속 재발한 진짜 원인.

    해결: `_is_win_vt100_input_enabled()`가 항상 False를 반환하도록
    패치해서, prompt_toolkit이 (VT 이스케이프 시퀀스에 의존하는 불안정한
    경로 대신) 항상 ConsoleInputReader—Windows Console API가 보장하는
    진짜 Shift 비트를 직접 읽는 경로—를 쓰도록 강제한다. 이 모드 전환은
    "입력"에만 영향을 주고 색상/커서 등 "출력" 렌더링과는 무관하므로
    화면 표시에는 부작용이 없다. 이후 ConsoleInputReader의
    `_event_to_key_presses`를 감싸서, Shift가 눌린 상태에서 결과 키가
    Enter(Keys.ControlM, 드물게 Keys.ControlJ)일 때만 Keys.Ignore로
    바꿔치기한다. 유닉스 쪽과 동일하게 Keys.Ignore 하나에 줄바꿈
    핸들러를 걸어 두었으므로, 이렇게 하면 플랫폼과 무관하게 동일한 코드
    경로로 Shift+Enter가 줄바꿈으로 처리된다.
    """
    if sys.platform != "win32":
        return
    try:
        from prompt_toolkit.input import win32 as _pt_win32
    except Exception:
        return

    # 1) Vt100ConsoleInputReader로 전환되는 것을 막는다: 항상
    #    ConsoleInputReader(네이티브 Windows 콘솔 API 경로)를 쓰도록
    #    강제한다. Win32Input.__init__()이 이 함수의 반환값으로
    #    분기하므로, PromptSession/Application이 만들어지기 전인 지금
    #    (모듈 로드 시점)에 패치해 둬야 한다.
    if hasattr(_pt_win32, "_is_win_vt100_input_enabled"):
        _pt_win32._is_win_vt100_input_enabled = lambda: False

    # 2) ConsoleInputReader 자체의 Shift+Enter 처리 누락을 고친다.
    reader_cls = getattr(_pt_win32, "ConsoleInputReader", None)
    if reader_cls is None:
        return

    original = reader_cls._event_to_key_presses
    if getattr(original, "_shift_enter_patched", False):
        return  # 이미 패치됨 (재실행/재임포트 대비)

    shift_pressed_bit = getattr(reader_cls, "SHIFT_PRESSED", 0x0010)
    _enter_keys = {Keys.ControlM, Keys.ControlJ}

    def _patched_event_to_key_presses(self, ev):
        key_presses = original(self, ev)
        try:
            shift_pressed = bool(ev.ControlKeyState & shift_pressed_bit)
        except AttributeError:
            shift_pressed = False
        if shift_pressed:
            for kp in key_presses:
                if kp.key in _enter_keys:
                    kp.key = Keys.Ignore
        return key_presses

    _patched_event_to_key_presses._shift_enter_patched = True
    reader_cls._event_to_key_presses = _patched_event_to_key_presses


_patch_windows_shift_enter()


def prompt_input() -> str:
    """
    상단 테두리를 먼저 그린 뒤, prompt_toolkit 세션으로 실제 입력을
    받는다. 입력이 터미널 폭을 넘어 줄바꿈되어도:
      - 매 줄 앞에 "│  "가 다시 붙어 왼쪽 테두리가 끊기지 않고,
      - 하단 테두리("╰──...──╯")가 항상 입력 바로 아래에 유지된다.

    접근성/편의 기능(신규):
      - 텍스트 붙여넣기: 붙여넣은 내용을 입력창에 그대로 풀어놓지 않고
        "[Pasted text +n lines]" 형태의 한 덩어리 플레이스홀더로 표시한다.
        붙여넣기가 여러 번 발생하면 "[Pasted text #1 +n lines]",
        "[Pasted text #2 +n lines]"처럼 번호가 붙는다. 일반 텍스트와
        자유롭게 섞어 입력할 수 있고, 백스페이스/Delete로 지울 때는
        플레이스홀더 전체가 한 번에 지워진다(안의 실제 텍스트가 문자
        단위로 남지 않음). 실제 전송 시에는 플레이스홀더가 원본 붙여넣기
        내용으로 다시 펼쳐져서 LM Arena에는 전체 내용이 그대로 전달된다.
      - Shift+Enter: 전송하지 않고 줄바꿈만 추가한다(Enter는 그대로 전송).
        터미널이 Shift+Enter를 구분해서 보내주지 않는 환경을 위해
        Alt+Enter(Escape, Enter)도 동일하게 줄바꿈 폴백으로 둔다.

    반환값 규약은 기존과 동일하다:
      - Ctrl+C / EOF(Ctrl+D)  -> "__EXIT__" 를 반환
      - 빈 입력 / "exit"/"quit" -> 입력값을 그대로 반환 (실제 종료 판단은
        cli.py가 함)
      - 그 외(전송할 프롬프트)  -> (플레이스홀더가 원문으로 펼쳐진) 입력값을
        반환하고, 뒤이어 user_prompt_echo()가 "sent" 상태 줄을 덧붙이도록
        플래그를 켠다.
    """
    global _box_pending, _last_display_text

    console.print(Rule(style=f"dim {DIM_MUTED}"))
    _animate_open_box("You", _BOX_COLOR)

    # 이번 턴의 빈 입력 상태(1줄)를 기준선으로 다시 잡는다. 이전 턴이
    # 끝난 시점의 값이 남아있어도 정확성에는 문제가 없지만(어차피 감소를
    # 감지하기 위한 목적이므로), 매 턴을 독립적으로 시작한다는 의도를
    # 명확히 하기 위해 초기화한다.
    _prev_wrapped_line_count[0] = 1

    session = _get_input_session()

    # 이번 입력 세션에서만 유효한 붙여넣기 상태(호출마다 초기화된다).
    paste_map: dict[str, str] = {}
    paste_labels: list[str] = []

    def _renumber_after_removal(buf) -> None:
        """플레이스홀더가 하나만 남았을 때는 번호("#1")를 다시 떼어낸다."""
        if len(paste_labels) != 1:
            return
        only = paste_labels[0]
        m = _PASTE_LABEL_RE.search(only)
        n_lines = m.group(1) if m else "?"
        plain_label = f"[Pasted text +{n_lines} lines]"
        if plain_label != only:
            _relabel_buffer_text(buf, only, plain_label)
            paste_map[plain_label] = paste_map.pop(only)
            paste_labels[0] = plain_label

    def _remove_label(buf, label: str) -> None:
        paste_map.pop(label, None)
        if label in paste_labels:
            paste_labels.remove(label)
        _renumber_after_removal(buf)

    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event) -> None:
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")  # Alt+Enter: 어떤 터미널에서도 동작하는 줄바꿈 폴백
    def _newline_alt(event) -> None:
        event.current_buffer.insert_text("\n")

    # Shift+Enter: 모듈 로드 시점에 _patch_shift_enter_sequences()가
    # Kitty/xterm 확장 키보드 프로토콜의 Shift+Enter 시퀀스를 모두
    # Keys.Ignore로 재매핑해 두었으므로, 여기서는 Keys.Ignore 하나에만
    # 줄바꿈 핸들러를 걸면 된다. (원래 prompt_toolkit은 xterm의
    # modifyOtherKeys 방식 시퀀스 "\x1b[27;2;13~"를 실수로 일반 Enter와
    # 동일한 Keys.ControlM으로 매핑해 버려서, 이 재매핑 없이는 Shift+Enter를
    # 눌러도 그냥 전송돼 버리는 문제가 있었다.) 이 프로토콜 자체를 지원하지
    # 않는(구분해서 보내주지 않는) 구형 터미널에서는 위의 Alt+Enter 폴백을
    # 사용해야 한다.
    @kb.add(Keys.Ignore)
    def _newline_shift(event) -> None:
        event.current_buffer.insert_text("\n")

    @kb.add("backspace")
    def _backspace(event) -> None:
        buf = event.current_buffer
        text_before = buf.document.text_before_cursor
        for label in paste_labels:
            if text_before.endswith(label):
                buf.delete_before_cursor(count=len(label))
                _remove_label(buf, label)
                return
        buf.delete_before_cursor(count=1)

    @kb.add("delete")
    def _forward_delete(event) -> None:
        buf = event.current_buffer
        text_after = buf.document.text_after_cursor
        for label in paste_labels:
            if text_after.startswith(label):
                buf.delete(count=len(label))
                _remove_label(buf, label)
                return
        buf.delete(count=1)

    @kb.add(Keys.BracketedPaste)
    def _on_paste(event) -> None:
        data = event.data
        if not data:
            return
        buf = event.current_buffer
        # 터미널에 따라 붙여넣은 텍스트의 줄바꿈이 "\n"이 아니라 "\r" 또는
        # "\r\n"으로 도착하는 경우가 있다. data.count("\n")만 세면 이런
        # 경우 항상 1로 계산돼 버리므로, splitlines()로 개행 방식과 무관하게
        # 정확한 줄 수를 센다.
        n_lines = len(data.splitlines()) or 1
        count = len(paste_labels) + 1

        if count == 2:
            # 두 번째 붙여넣기가 생기면 첫 번째 라벨에 "#1"을 붙여준다.
            old_label = paste_labels[0]
            m = _PASTE_LABEL_RE.search(old_label)
            n1 = m.group(1) if m else "?"
            new_label0 = f"[Pasted text #1 +{n1} lines]"
            if new_label0 != old_label:
                _relabel_buffer_text(buf, old_label, new_label0)
                paste_map[new_label0] = paste_map.pop(old_label)
                paste_labels[0] = new_label0

        label = (
            f"[Pasted text +{n_lines} lines]"
            if count == 1
            else f"[Pasted text #{count} +{n_lines} lines]"
        )
        paste_map[label] = data
        paste_labels.append(label)
        buf.insert_text(label)

    @kb.add("tab")
    def _accept_suggestion(event) -> None:
        """
        '/' 커맨드 입력 중 표시되는 고스트 텍스트(자동완성 미리보기)를
        Tab으로 수락해 그대로 커서 위치에 삽입한다. 그 외 입력에서는
        고스트 텍스트 자체가 없으므로(빈 문자열) 아무 동작도 하지 않는다
        - 즉 일반 프롬프트 입력에는 전혀 영향을 주지 않는다.
        """
        buf = event.current_buffer
        suggestion = buf.suggestion
        if suggestion and suggestion.text:
            buf.insert_text(suggestion.text)

    def _continuation(width: int, line_number: int, wrap_count: int) -> FormattedText:
        # wrap_count > 0: 같은 입력이 폭을 넘어 자동으로 줄바꿈된 두 번째
        # 이후의 화면 줄. 여기서도 동일하게 왼쪽 테두리를 다시 붙여줘야
        # 세로줄이 끊기지 않는다.
        return FormattedText([(f"fg:{_BOX_COLOR}", "│  ")])

    def _bottom_toolbar() -> FormattedText:
        """
        평소에는 기존 그대로의 빈 닫힌 테두리(_bottom_border_plain)를
        보여준다. 입력이 '/'로 시작할 때만, 팝업 창을 새로 띄우는 대신
        같은 테두리 줄 오른쪽 라벨 자리에 일치하는 커맨드 이름들을
        보여준다 - 기존 "cancelled"/"closed" 라벨과 동일한 시각적 문법을
        재사용하므로 박스 UI의 높이 계산에 전혀 영향을 주지 않는다
        (항상 1줄).
        """
        text = session.default_buffer.text
        if text.startswith("/") and "\n" not in text:
            matches = _matching_slash_commands(text)
            return _bottom_border_command_hint(matches, _BOX_COLOR)
        return _bottom_border_plain(_BOX_COLOR)

    try:
        # in_thread=True: 이 프롬프트를 별도의 스레드에서 "새로운"
        # asyncio 이벤트 루프로 실행한다. Windows에서 Playwright(동기
        # API)가 내부적으로 이미 이벤트 루프를 돌리고 있는 등, 현재
        # 스레드에 이미 실행 중인 이벤트 루프가 있으면 prompt_toolkit이
        # 내부적으로 쓰는 asyncio.run()이
        # "asyncio.run() cannot be called from a running event loop"
        # 오류를 던진다. in_thread=True를 쓰면 항상 새 스레드 + 새
        # 이벤트 루프에서 실행되므로 이 충돌이 근본적으로 발생하지
        # 않는다(로그인/DOM/전송 로직과는 무관한, 입력창 자체의 실행
        # 방식에 대한 수정이다).
        #
        # multiline=True: Shift+Enter(및 Alt+Enter 폴백)로 줄바꿈을 넣을
        # 수 있어야 하므로 멀티라인 버퍼를 사용한다. 실제 "전송"은 위의
        # 커스텀 'enter' 바인딩이 담당하므로 일반 Enter 키 동작(줄바꿈)은
        # 덮어써진다.
        #
        # pre_run=_enable_key_protocol: 터미널이 raw 모드로 전환된 "직후"
        # 시점에 확장 키보드 프로토콜을 요청한다(session.prompt() 호출
        # 전에 미리 보내면 이후 raw 모드 전환 과정에서 일부 터미널이
        # 요청을 무시해 버리는 문제가 있었다).
        try:
            user_input = session.prompt(
                FormattedText([(f"fg:{_BOX_COLOR}", "│  ")]),
                prompt_continuation=_continuation,
                bottom_toolbar=_bottom_toolbar,
                wrap_lines=True,
                multiline=True,
                key_bindings=kb,
                style=_PT_STYLE,
                in_thread=True,
                pre_run=_enable_key_protocol,
            )
        finally:
            _disable_key_protocol()
    except (EOFError, KeyboardInterrupt):
        # 세션이 어떤 이유로 끝났든(예외 포함) 커서는 입력 영역(줄바꿈
        # 포함) 바로 다음 줄에 있으므로, 그 자리에 닫힌 테두리를 이어서
        # 출력하면 된다.
        console.print(_bottom_border_line("cancelled", DANGER))
        _box_pending = False
        return "__EXIT__"

    stripped = user_input.strip()
    if not stripped or stripped.lower() in {"exit", "quit"}:
        console.print(_bottom_border_line("closed", DIM_MUTED))
        _box_pending = False
        return user_input

    # 전송 직전: 화면에 보이던 붙여넣기 플레이스홀더를 실제 원본 텍스트로
    # 펼쳐서, LM Arena에는 붙여넣은 전체 내용이 그대로 전달되게 한다.
    # (터미널 화면상으로는 계속 요약된 라벨만 보였던 것과 무관하게, 여기서
    # 반환되는 문자열이 곧 cli.py가 전송에 사용하는 실제 프롬프트다.)
    final_text = user_input
    for label, original in paste_map.items():
        final_text = final_text.replace(label, original)

    # user_prompt_echo()가 "sent" 상태 줄과 함께 다시 그릴 내용을 저장해
    # 둔다. 화면에는(붙여넣기 플레이스홀더가 아니라) 실제로 전송되는 원문
    # 그대로("final_text")를 보여준다 — 요청 사항: sent 박스에는 붙여넣은
    # 텍스트의 원문이 나타나야 하며, 프롬프트 입력창은 erase_when_done으로
    # 이미 지워지므로 여기서 한 번만 그려도 중복되지 않는다.
    _last_display_text = final_text

    # 전송으로 이어질 입력: 실제 "sent" 상태 표시는 user_prompt_echo()가
    # 담당한다(입력 직후 곧바로 호출되므로 여기서는 플래그만 켜 둔다).
    _box_pending = True
    return final_text


# ── 사용자 프롬프트 에코 (입력창 바로 아래에 전송 상태를 표시) ────────
def user_prompt_echo(prompt: str) -> None:
    """
    prompt_input()에서 "전송 대기" 플래그가 켜져 있으면, 방금 입력했던
    영역을(prompt_toolkit이 erase_when_done으로 이미 화면에서 지운 뒤)
    실제로 전송된 원문 그대로(붙여넣은 텍스트는 플레이스홀더가 아니라
    원본 전체 내용으로) 딱 한 번 다시 그린 뒤, "sent" 라벨이 담긴 닫힌
    하단 테두리를 이어서 출력한다. 이 상태가 아니면(예외적으로
    prompt_input() 없이 직접 호출된 경우) 안전하게 독립된 패널로 대체
    표시한다.
    """
    global _box_pending, _last_display_text

    if _box_pending:
        display_text = _last_display_text
        if display_text.strip():
            # 붙여넣은 원문에는 터미널 폭보다 긴 줄이 섞여 있을 수 있다.
            # Rich의 자동 줄바꿈에 맡기면 "│  " 테두리 접두어가 없는
            # 상태로 다음 줄이 이어지면서 테두리가 끊겨 보이는(간헐적으로
            # 붕괴/누락되는) 문제가 있었다. 그래서 각 줄을 콘솔 폭에 맞춰
            # 우리가 직접 미리 줄바꿈한 뒤, 매 조각마다 "│  " 접두어를
            # 붙여 출력한다. Text.wrap()은 한글 등 넓은 문자(wide char)의
            # 셀 폭까지 고려해 계산하므로 폭 계산이 어긋나지 않는다.
            available_width = max(_border_width() - 3, 10)
            for line in display_text.splitlines() or [""]:
                wrapped_lines = Text(line).wrap(console, available_width) or [Text("")]
                for sub_line in wrapped_lines:
                    row = Text()
                    row.append("│  ", style=_BOX_COLOR)
                    row.append(sub_line)
                    console.print(row)
        console.print(_bottom_border_line("sent", _BOX_COLOR))
        _box_pending = False
        _last_display_text = ""
        return

    console.print(
        Panel(
            Text(prompt, style=""),
            title=f"[bold {SECONDARY}]  You[/]",
            title_align="left",
            subtitle=f"[{SUCCESS}]· sent[/]",
            subtitle_align="right",
            border_style=SECONDARY,
            box=box.ROUNDED,
            padding=(0, 2),
        )
    )


# ── 스트리밍 응답 패널 ───────────────────────────────────────────────
@contextmanager
def stream_panel():
    """
    일반 텍스트와 코드 블록을 모두 생성되는 대로 실시간으로 함께 보여주는
    뷰. 스트리밍 도중에는 마크다운 파싱 없이 빠르게 갱신하고(불완전한
    마크다운 문법이 깨져 보이지 않도록), with 블록을 벗어나면 화면에서
    사라진다(transient). 완료 후 최종 텍스트/코드 표시 및 파일 기록은
    호출부에서 assistant_text() / 파일 기록 로직으로 다시 그린다.

        with ui.stream_panel() as update:
            update(plain_text, code_blocks, done=False)
            ...
            update(plain_text, code_blocks, done=True)

    code_blocks는 .language / .code 속성을 가진 객체 리스트면 된다.

    ⚠️ 아래 budget/used/max_*_content 계산 로직은 터미널 높이를 넘겨서
    Live가 패널의 닫는 테두리를 잘라먹는 것을 막는 핵심 로직이다(원본과
    동일). box.ROUNDED는 top/bottom 각 1줄씩 총 2줄의 테두리를 차지하므로
    이 계산의 전제(+2)는 그대로 유효하다. 이 함수에서는 색상 팔레트,
    아이콘, 스피너 이름, 코드 하이라이팅 테마 등 "스킨"만 바꾼다.
    """
    start_time = time.time()
    thinking_spinner  = _DotSpinner(ACCENT)
    reasoning_spinner = _DotSpinner(SECONDARY)

    def _format_elapsed(seconds: float) -> str:
        total = max(int(seconds), 0)
        minutes, secs = divmod(total, 60)
        return f"{minutes}m {secs}s" if minutes else f"{secs}s"

    def _render_reasoning(reasoning) -> "tuple[list, int]":
        """
        추론(reasoning) 미리보기를 왼쪽 세로 타임라인 바(▏) 스타일로 그린다.
        헤더: 진행 중 -> 스피너 + "Thinking for Ns"
              완료   -> "+ Thought for Ns"
        """
        if reasoning is None:
            return [], 0

        header = Text()
        if reasoning.is_done:
            header.append("+ ", style=f"bold {SUCCESS}")
            header.append(
                f"Thought for {_format_elapsed(reasoning.elapsed_seconds)}",
                style=f"italic {MUTED}",
            )
        else:
            header.append_text(reasoning_spinner.render(time.time()))
            header.append(
                f"  Thinking for {_format_elapsed(reasoning.elapsed_seconds)}...",
                style=f"italic bold {SECONDARY}",
            )

        parts: list = [header]
        preview_line_count = 3
        if reasoning.text.strip():
            raw_lines = [ln for ln in reasoning.text.strip().splitlines() if ln.strip()]
            preview = raw_lines[-preview_line_count:]
            for line in preview:
                row = Text()
                row.append("  ▏ ", style=f"dim {SECONDARY}")
                row.append(line.strip(), style="dim italic")
                parts.append(row)

        return parts, len(parts)

    with Live(console=console, auto_refresh=False, transient=True) as live:

        def update(text: str, code_blocks=None, done: bool = False, reasoning=None) -> None:
            code_blocks = code_blocks or []
            renderables = []

            # 프롬프트 줄(You 패널)이나 셸 여백 등을 위해 여유를 좀 더 둔다.
            budget = max(console.size.height - 3, 10)
            used = 0  # 지금까지 확정된 renderable들이 차지하는 실제 줄 수

            # 추론 미리보기: 테두리 없는 세로선 스타일로 항상 최상단에
            reasoning_parts, reasoning_lines = _render_reasoning(reasoning)
            if reasoning_parts:
                # 추론 전용 패널 (얇은 세로선 + dim italic 텍스트)
                reasoning_body = Group(*reasoning_parts)
                renderables.append(
                    Panel(
                        reasoning_body,
                        title=f"[dim {SECONDARY}]  Reasoning[/]",
                        title_align="left",
                        border_style=DIM_MUTED,
                        box=box.MINIMAL,
                        padding=(0, 1),
                    )
                )
                renderables.append(Text(""))  # 답변 영역과의 시각적 여백
                used += reasoning_lines + 3   # 패널 테두리 + 여백 포함

            if text.strip():
                lines = text.splitlines() or [text]
                # 텍스트 패널에는 예산의 최대 1/3까지만 쓴다 (테두리 2줄 포함).
                max_text_content = max(budget // 3 - 2, 2)
                if len(lines) > max_text_content:
                    omitted = len(lines) - max_text_content
                    preview_lines = [f"  ... ({omitted} lines above)"] + lines[-max_text_content:]
                else:
                    preview_lines = lines
                preview = "\n".join(preview_lines)
                body = Text(preview + ("" if done else " ▌"))
                renderables.append(
                    Panel(
                        body,
                        title=f"[bold {ACCENT}]  {BRAND}[/]",
                        title_align="left",
                        border_style=ACCENT,
                        box=box.ROUNDED,
                        subtitle=(
                            None
                            if done
                            else f"[{SECONDARY}]· streaming[/]"
                        ),
                        subtitle_align="right",
                    )
                )
                used += len(preview_lines) + 2  # 테두리(top/bottom) 2줄 포함

            elif not code_blocks and reasoning is None:
                # 아직 어떤 텍스트/코드/추론 로그도 도착하지 않은 상태.
                # 세션이 멈춘 게 아니라 계속 정상적으로 대기 중임을
                # 알 수 있도록 애니메이션 스피너 + 경과 시간을 보여준다.
                elapsed = int(time.time() - start_time)
                minutes, seconds = divmod(elapsed, 60)
                elapsed_label = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"

                thinking_body = Text()
                thinking_body.append_text(thinking_spinner.render(time.time()))
                thinking_body.append(
                    f"  Reasoning...  ({elapsed_label})",
                    style=MUTED,
                )

                renderables.append(
                    Panel(
                        thinking_body,
                        title=f"[bold {ACCENT}]  {BRAND}[/]",
                        title_align="left",
                        border_style=ACCENT,
                        box=box.ROUNDED,
                        subtitle=(
                            f"[dim {DIM_MUTED}]"
                            "추론에는 시간이 오래 걸릴 수 있습니다 · 타임아웃 없이 대기 중"
                            "[/]"
                        ),
                        subtitle_align="right",
                    )
                )
                used += 3  # 내용 1줄 + 테두리 2줄

            # 코드 블록: 완료된 블록은 한 줄 요약, 마지막 블록만 실시간 미리보기
            last_idx = len(code_blocks)
            for idx, cb in enumerate(code_blocks, start=1):
                code_lines = cb.code.splitlines() or [""]
                is_actively_streaming = (idx == last_idx) and not done

                if not is_actively_streaming:
                    done_row = Text()
                    done_row.append("  + ", style=f"bold {SUCCESS}")
                    done_row.append(
                        f"Code block {idx} complete",
                        style=SUCCESS,
                    )
                    done_row.append("  ·  ", style=f"dim {DIM_MUTED}")
                    done_row.append(
                        f"{cb.language}  ·  {len(code_lines)} lines",
                        style=MUTED,
                    )
                    renderables.append(done_row)
                    used += 1
                    continue

                # 남은 예산에서 테두리 2줄을 뺀 만큼만 코드 내용에 쓴다.
                remaining = max(budget - used, 5)
                max_code_content = max(remaining - 2, 3)
                if len(code_lines) > max_code_content:
                    omitted = len(code_lines) - max_code_content
                    preview_lines = code_lines[-max_code_content:]
                    subtitle = (
                        f"[{MUTED}]· writing  ·  "
                        f"last {max_code_content} lines  ({omitted} above)[/]"
                    )
                    start_line = omitted + 1
                else:
                    preview_lines = code_lines
                    subtitle = f"[{MUTED}]· writing[/]"
                    start_line = 1
                preview_code = "\n".join(preview_lines)

                # word_wrap=False: 긴 한 줄이 화면 폭에 안 맞으면 Rich가
                # 옆으로 잘라서(...) 한 줄로 유지한다(줄바꿈으로 줄 수가
                # 늘어나지 않음). 이렇게 해야 위에서 센 "줄 수"와 실제
                # 렌더링 줄 수가 정확히 같아져서 예산을 넘기지 않는다.
                syntax = Syntax(
                    preview_code,
                    (cb.language or "text").lower(),
                    theme=CODE_THEME,
                    line_numbers=True,
                    start_line=start_line,
                    word_wrap=False,
                )
                renderables.append(
                    Panel(
                        syntax,
                        title=(
                            f"[bold {CODE_ACCENT}]  Block {idx}[/]"
                            f"  [dim]({cb.language})[/]"
                        ),
                        title_align="left",
                        border_style=CODE_ACCENT,
                        box=box.ROUNDED,
                        subtitle=subtitle,
                        subtitle_align="right",
                    )
                )
                used += len(preview_lines) + 2

            live.update(Group(*renderables), refresh=True)

        yield update


# ── 완료 후 최종 답변 출력 ───────────────────────────────────────────
def assistant_text(text: str) -> None:
    """마크다운(굵게/목록/인라인 코드 등)을 하이라이팅해서 답변을 표시한다."""
    console.print(
        Panel(
            Markdown(text),
            title=f"[bold {ACCENT}]  {BRAND}[/]",
            title_align="left",
            border_style=ACCENT,
            box=box.ROUNDED,
        )
    )


def assistant_code_block(language: str, filename: str, code: str) -> None:
    lexer = language.lower() if language else "text"
    syntax = Syntax(code, lexer, theme=CODE_THEME, line_numbers=True, word_wrap=True)
    console.print(
        Panel(
            syntax,
            title=(
                f"[bold {CODE_ACCENT}]  {filename}[/]"
                f"  [dim]({language})[/]"
            ),
            title_align="left",
            border_style=CODE_ACCENT,
            box=box.ROUNDED,
        )
    )


# ── 파일 쓰기 알림 ───────────────────────────────────────────────────
def work_note(message: str) -> None:
    """Work 모드 진행 한 줄. 기존 스트리밍/코드 패널 렌더링은 건드리지 않는다."""
    console.print(f"  [bold {ACCENT}]work[/]  {message}")


def file_written(path: str, mode: str = "created") -> None:
    if mode == "created":
        console.print(f"  [bold {SUCCESS}]+[/]  [bold]created[/]  {path}")
    else:
        console.print(f"  [bold {SECONDARY}]~[/]  [bold]modified[/] {path}")


# ── 공용 스피너 / 구분선 ─────────────────────────────────────────────
def spinner(message: str):
    """with 문으로 사용하는 로딩 스피너 컨텍스트 (browser.py에서 사용)."""
    return console.status(
        f"[bold {ACCENT}]  {message}[/]",
        spinner="point",
        spinner_style=ACCENT,
        refresh_per_second=20,  # 기본값(12.5)보다 높여 스피너 회전을 더 매끄럽게
    )


def divider() -> None:
    console.print()
    console.rule(style=f"dim {DIM_MUTED}")
    console.print()


# ── 터미널 화면 초기화 (/clear 명령어 전용) ──────────────────────────
def clear_screen() -> None:
    """
    이전 대화 UI(질문/답변 패널, 코드 블록, 구분선 등)만 화면 밖으로
    스크롤해 치운다. 로그인 배너 등은 지우지 않고 스크롤백에 그대로
    남아있으며(위로 스크롤하면 다시 볼 수 있음), 브라우저 세션이나
    LM Arena 대화 기록에도 전혀 영향을 주지 않는다.

    ⚠️ 화면을 실제로 지우고(cls / ANSI erase) 커서를 맨 위로 옮기는
    방식은, 이 앱이 닫힌 테두리를 그리는 데 쓰는 prompt_toolkit의
    bottom_toolbar가 "커서 기준으로 남은 화면 공간"을 매우 크게
    인식하게 만들어 입력창이 거대하게 깨지는 문제가 있었다. 그래서
    화면을 지우지 않고 스크롤로 밀어내는 방식을 쓴다.

    첫 시도(console.size.height만큼 console.print("\\n"*...))도 여전히
    문제가 있었는데, 원인은 rich의 Console이 내부적으로 캐시하는 크기와
    prompt_toolkit이 OS에서 직접 조회하는 실제 터미널 크기가 서로 어긋날
    수 있기 때문이다(둘이 서로 다른 값을 "진실"로 알고 있으면 커서 위치
    계산이 틀어진다). 이제는 rich를 거치지 않고 OS가 보고하는 실제
    터미널 행 수(shutil.get_terminal_size, prompt_toolkit도 내부적으로
    같은 계열의 API를 사용)를 직접 조회해, 그 수만큼(+여유분) 개행을
    표준출력에 그대로 써서 스크롤시킨다. 이렇게 하면 rich/prompt_toolkit
    양쪽이 보는 "터미널 크기"와 "커서가 화면 맨 아래 근처에 있다"는
    전제가 항상 일치하게 되어, 다음 입력창이 정상 크기로 그려진다.
    """
    global _box_pending
    rows = shutil.get_terminal_size(fallback=(80, 24)).lines
    sys.stdout.write("\n" * (rows + 2))
    sys.stdout.flush()
    _box_pending = False