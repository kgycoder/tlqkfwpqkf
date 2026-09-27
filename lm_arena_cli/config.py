"""
LM Arena CLI - 설정 및 상수
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "lm-arena-cli"

# ── 디버그 모드 ──────────────────────────────────────────────────────
# True : 로그인 완료 후 자동화 창(Test Chrome)에 "로그인 완료 · 연결됨" 커스텀
#        화면을 덮지 않고, 실제 LM Arena 화면이 그대로 보인다(테스트/디버그용).
# False: 기존 동작(커스텀 완료 화면만 표시). 배포 시에는 False로 되돌린다.
DEBUG_MODE = False

# ── 숨김 창 모드 (완전히 새로 추가) ──────────────────────────────────
# True : 자동화 브라우저(Test Chrome)를 "headless처럼" 사용한다. 진짜 headless는
#        실행 중에 창을 띄울 수 없고, 브라우저를 다시 열면 reCAPTCHA와 진행 중이던
#        대화 시작 요청이 사라진다. 그래서 브라우저는 한 번만 띄우고 창만
#        화면 밖으로 숨겨 두었다가(작업 표시줄에도 표시 안 됨) 아래 두 경우에만
#        꺼낸다(window_hider.py).
#          1) 로그아웃 상태 -> 구글 로그인 화면이 완전히 뜬 뒤 창 표시, 로그인 후 숨김
#          2) reCAPTCHA 감지 -> 창 표시, 통과 후 숨김
#        이 모드에서는 Settings.headless 값은 무시된다(진짜 headless는 reCAPTCHA를
#        처리할 수 없으므로).
# False: 기존 동작(Settings.headless 값대로). 환경변수 LMARENA_SHOW_BROWSER=1로
#        실행해도 꺼진다(진단용). DEBUG_MODE=True일 때도 자동으로 꺼진다.
HIDDEN_WINDOW = os.environ.get("LMARENA_SHOW_BROWSER", "").strip().lower() not in (
    "1", "true", "yes", "on",
)

# ~/.lm-arena-cli/ 아래에 세션(쿠키)과 설정을 저장한다.
HOME_DIR = Path.home() / ".lm-arena-cli"
USER_DATA_DIR = HOME_DIR / "browser_profile"   # Playwright persistent context 저장 위치
CONFIG_FILE = HOME_DIR / "config.json"

DIRECT_MODE_URL = "https://arena.ai/text/direct"

# ── Agent 모드(완전히 새로 추가) ─────────────────────────────────────
# LM Arena는 같은 사이트 안에서 URL만 바꾸면 Direct 채팅 <-> Agent 채팅이
# 전환된다. Direct 모드 URL(위)과 기존 로직은 전혀 건드리지 않고, Agent용
# URL과 "모드 이름 -> URL" 매핑만 새로 추가한다.
AGENT_MODE_URL = "https://arena.ai/agent/"

CHAT_MODE_DIRECT = "direct"
CHAT_MODE_AGENT = "agent"
CHAT_MODES = (CHAT_MODE_DIRECT, CHAT_MODE_AGENT)

MODE_URLS = {
    CHAT_MODE_DIRECT: DIRECT_MODE_URL,
    CHAT_MODE_AGENT: AGENT_MODE_URL,
}

# 화면(웹 UI / CLI)에 표시할 모드 이름.
MODE_LABELS = {
    CHAT_MODE_DIRECT: "Direct",
    CHAT_MODE_AGENT: "Agent",
}


def normalize_chat_mode(value: str | None) -> str:
    """알 수 없는 값이 들어와도 기존 동작(Direct)으로 안전하게 되돌린다."""
    mode = (value or "").strip().lower()
    return mode if mode in CHAT_MODES else CHAT_MODE_DIRECT

# 코드블록 언어 표시(LM Arena UI 상 표기) -> 실제 파일 확장자 매핑
LANGUAGE_EXTENSION_MAP = {
    "python": "py",
    "py": "py",
    "javascript": "js",
    "js": "js",
    "typescript": "ts",
    "ts": "ts",
    "jsx": "jsx",
    "tsx": "tsx",
    "json": "json",
    "html": "html",
    "css": "css",
    "scss": "scss",
    "bash": "sh",
    "shell": "sh",
    "sh": "sh",
    "zsh": "sh",
    "yaml": "yaml",
    "yml": "yaml",
    "markdown": "md",
    "md": "md",
    "sql": "sql",
    "java": "java",
    "c": "c",
    "cpp": "cpp",
    "c++": "cpp",
    "csharp": "cs",
    "c#": "cs",
    "go": "go",
    "rust": "rs",
    "ruby": "rb",
    "php": "php",
    "kotlin": "kt",
    "swift": "swift",
    "dockerfile": "Dockerfile",
    "toml": "toml",
    "xml": "xml",
    "plaintext": "txt",
    "text": "txt",
}


@dataclass
class Settings:
    """사용자별 영속 설정 (target 폴더 등)"""
    target_dir: str = ""
    headless: bool = False
    auto_write_files: bool = True
    last_used: str = ""
    # LM Arena에는 API처럼 별도의 "system prompt" 채널이 없으므로,
    # 대신 매 프롬프트 앞에 지침 블록을 덧붙여 보내는 방식(prompt_augment.py)
    # 으로 흉내낸다. 이 값이 True면 코드 블록 최상단에 파일명 주석을
    # 항상 표기하도록 요청하는 지침을 함께 전송한다. 기존 저장된
    # config.json에는 이 키가 없을 수 있으나, Settings.load()가
    # cls().__dict__(기본값)와 병합하므로 기존 설정 파일과도 안전하게
    # 호환된다.
    enforce_filename_comments: bool = True
    # workspace 공유 기능 (완전히 새로 추가된 설정). True면 target_dir(작업
    # 폴더)의 전체 구조/파일 내용을 LM Arena에게 컨텍스트로 공유한다. 실제
    # "세션당 한 번만 보낸다"는 로직은 이 값과 별개로 런타임 상태(session
    # state)에서 관리되며(workspace.py 참고), 이 값은 어디까지나 사용자가
    # 이 기능을 켜뒀는지 여부를 재실행 후에도 기억하기 위한 영속 설정이다.
    # 기존 config.json에는 이 키가 없을 수 있으나, 위 enforce_filename_comments
    # 케이스와 동일하게 Settings.load()가 기본값과 병합하므로 안전하다.
    share_workspace: bool = False
    # Work 모드(완전히 새로 추가). True면 LM Arena 답변의 work 코드 블록을
    # 로컬에서 실행해 마우스/키보드/창/셸을 조작하는 에이전트 루프를 돈다.
    # 기존 Direct 채팅 동작은 이 값이 False일 때 그대로 유지된다.
    work_mode: bool = False
    # ── Agent 모드 관련 설정 (완전히 새로 추가) ──────────────────────
    # chat_mode: "direct"(기존 동작 그대로) 또는 "agent". Agent일 때만
    # AGENT_MODE_URL로 접속하고, 모델 선택 UI가 숨겨지며, 답변을
    # Agent 전용 DOM(workspace / artifact 카드)으로 파싱한다. 기존
    # config.json에는 이 키가 없을 수 있으나 Settings.load()가 기본값과
    # 병합하므로 안전하다(위 enforce_filename_comments와 동일).
    chat_mode: str = CHAT_MODE_DIRECT
    # 프로젝트가 끝났을 때(평가 패널이 뜰 때) LM Arena workspace를 zip으로
    # 자동 다운로드해 작업 폴더에 풀어 넣을지 여부.
    agent_auto_download: bool = True
    # 다운로드된 zip의 최상위 폴더가 하나뿐이면(예: test-site/) 그 폴더를
    # 벗겨내고 내용물만 작업 폴더 바로 아래에 풀지 여부.
    agent_strip_root_folder: bool = True

    @classmethod
    def load(cls) -> "Settings":
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                return cls(**{**cls().__dict__, **data})
            except Exception:
                return cls()
        return cls()

    def save(self) -> None:
        HOME_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            json.dumps(self.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @property
    def target_path(self) -> Path | None:
        if not self.target_dir:
            return None
        return Path(self.target_dir).expanduser().resolve()


def ensure_dirs() -> None:
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)