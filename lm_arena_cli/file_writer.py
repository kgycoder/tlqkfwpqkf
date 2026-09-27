"""
LM Arena CLI - 코드 블록 -> 실제 파일 반영

- 코드 블록의 언어 라벨(Python, JavaScript 등)을 실제 확장자(.py, .js 등)로 변환.
- 코드 내부 앞부분에 파일명 힌트가 있는 경우(예: "# filename: app.py",
  "// filename: app.js", "<!-- filename: index.html -->",
  "/* filename: style.css */", "-- filename: query.sql") 이를 우선 사용.
- 힌트가 없으면 언어 + 순번으로 자동 이름 생성 (generated_1.py 등).
- 기존 파일과 동일한 이름이면 자동으로 "수정(diff 없이 덮어쓰기)" 처리.

[파일명 힌트 인식 - 버그 수정 내역]
과거 버전은 (a) "filename:" 키워드 뒤에 붙는 언어별 주석 기호가
#, //, <!-- --> 세 가지만 하드코딩되어 있어 CSS/SCSS(`/* */`)나
SQL/Lua(`--`) 같은 주석 문법으로 온 힌트는 아예 매칭되지 않았고,
(b) "코드의 맨 첫 줄"만 검사했기 때문에 shebang(`#!/usr/bin/env
python3`)이나 빈 줄이 먼저 오면 그 다음 줄에 있는 실제 힌트를
놓치는 문제가 있었다. 이는 LM Arena 쪽 문제가 아니라 이 파일(추출
로직) 자체의 버그였으므로, 아래 FILENAME_HINT_PATTERNS와
_extract_filename_hint()를 더 넓은 주석 문법을 인식하고 앞쪽 여러
줄까지 살펴보도록 고쳤다. 그 외 파일(브라우저 자동화, DOM 셀렉터,
UI 등)의 동작에는 영향이 없다.
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import LANGUAGE_EXTENSION_MAP
from .browser import CodeBlock
from . import ui

# 각 항목은 "한 줄"에 대해 match()로 검사된다(멀티라인 대상 아님).
# 키워드는 "filename" 또는 "file" 둘 다 허용해서, 모델이 조금 다르게
# 표현해도(`// file: app.js`, `// filename: app.js`) 잡아낸다.
FILENAME_HINT_PATTERNS = [
    # 1) 한 줄 주석 기호(#, //, --, ;, %, ::, REM) + filename/file 키워드.
    #    파이썬/셸/YAML(#), JS·TS·C계열(//), SQL·Lua(--), 어셈블리/리스프(;),
    #    LaTeX(%), 배치파일(:: 또는 REM)까지 한 패턴으로 커버한다.
    re.compile(
        r"^(?:#|//|--|;|%|::|REM)\s*(?:filename|file)\s*[:=]\s*(?P<name>[\w./-]+)",
        re.IGNORECASE,
    ),
    # 2) HTML/XML 블록 주석: <!-- filename: index.html -->
    re.compile(
        r"^<!--\s*(?:filename|file)\s*[:=]\s*(?P<name>[\w./-]+)\s*-->",
        re.IGNORECASE,
    ),
    # 3) CSS/SCSS 등 블록 주석: /* filename: style.css */  (기존에 누락되어 있던 형식)
    re.compile(
        r"^/\*\s*(?:filename|file)\s*[:=]\s*(?P<name>[\w./-]+)\s*\*/",
        re.IGNORECASE,
    ),
    # 4) 키워드 없이 확장자 있는 파일명만 덜렁 적은 폴백 형태 (# app.py 등)
    re.compile(r"^(?:#|//|--|;|%)\s*(?P<name>[\w./-]+\.\w+)\s*$"),
    re.compile(r"^/\*\s*(?P<name>[\w./-]+\.\w+)\s*\*/\s*$"),
    re.compile(r"^<!--\s*(?P<name>[\w./-]+\.\w+)\s*-->\s*$"),
]

# 힌트를 찾기 위해 앞에서부터 최대 몇 줄까지 살펴볼지. shebang 줄이나
# 라이선스 헤더, 빈 줄 한두 개 정도가 먼저 오는 경우를 커버하되, 코드
# 본문 깊숙한 곳의 문자열을 힌트로 오인하지 않도록 너무 크게 잡지는 않는다.
_HINT_SCAN_LINES = 5

# 힌트 후보로 "계속 검사할 만한" 주석/빈 줄로 간주하는 접두어들.
# 이걸로 시작하지 않는 줄(=실제 코드 본문)을 만나면 더 이상 아래로
# 내려가며 찾지 않는다 (오탐 방지: 코드 중간의 우연한 일치를 피함).
_COMMENT_PREFIXES = ("#", "//", "--", ";", "%", "::", "REM", "<!--", "/*")


def _extract_filename_hint(code: str) -> str | None:
    lines = code.splitlines()
    for line in lines[:_HINT_SCAN_LINES]:
        stripped = line.strip()
        if not stripped:
            continue  # 빈 줄은 건너뛰고 계속 다음 줄을 본다
        for pattern in FILENAME_HINT_PATTERNS:
            m = pattern.match(stripped)
            if m:
                return m.group("name")
        # 주석도 아니고 힌트도 아닌, 실제 코드로 보이는 줄을 만나면
        # 그 아래는 더 보지 않는다 (힌트는 항상 파일 맨 앞쪽에 온다는
        # 전제 하에, 코드 본문 깊은 곳을 잘못 매칭하는 것을 방지).
        if not stripped.upper().startswith(tuple(p.upper() for p in _COMMENT_PREFIXES)):
            break
    return None


def _extension_for_language(language: str) -> str:
    key = language.strip().lower()
    return LANGUAGE_EXTENSION_MAP.get(key, "txt")


def _sanitize_hint(hint: str | None) -> str | None:
    """
    파일명 힌트를 작업 폴더 밖으로 나갈 수 없는 상대 경로로 정리한다.

    Direct 모드에서 정규식(FILENAME_HINT_PATTERNS)으로 뽑은 힌트는 원래부터
    `[\\w./-]+`로 제한돼 있어 이 함수를 통과해도 결과가 달라지지 않는다.
    이 방어가 필요한 쪽은 Agent 모드다 - 거기서는 힌트가 LM Arena 화면의
    artifact 카드 헤더 텍스트에서 그대로 오기 때문에, 절대 경로나 '..'가
    섞여 들어올 가능성을 여기서 한 번 걸러야 안전하다.
    """
    if not hint:
        return None
    name = hint.strip().replace("\\", "/")
    if not name or name.endswith("/"):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".", "..")]
    if not parts:
        return None
    # 드라이브 문자(C:) 같은 절대 경로 조각 제거
    parts = [re.sub(r'^[A-Za-z]:', "", p) for p in parts]
    parts = [re.sub(r'[<>:"|?*\x00-\x1f]', "_", p).strip() for p in parts]
    parts = [p for p in parts if p]
    return "/".join(parts) if parts else None


def resolve_filename(code_block: CodeBlock, index: int, used_names: set[str]) -> str:
    hint = _sanitize_hint(code_block.filename_hint) or _extract_filename_hint(code_block.code)
    if hint:
        name = hint
    else:
        ext = _extension_for_language(code_block.language)
        base = f"generated_{index}"
        name = f"{base}.{ext}" if ext != "Dockerfile" else "Dockerfile"

    # 중복 방지
    original = name
    counter = 1
    while name in used_names:
        stem = Path(original).stem
        suffix = Path(original).suffix
        name = f"{stem}_{counter}{suffix}"
        counter += 1
    used_names.add(name)
    return name


def write_code_blocks(target_dir: Path, code_blocks: list[CodeBlock]) -> list[Path]:
    """
    code_blocks를 target_dir 하위에 파일로 기록한다.
    이미 존재하는 파일이면 덮어쓰기(=수정)로 처리하고, 신규면 생성으로 처리한다.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    used_names: set[str] = set()

    for idx, cb in enumerate(code_blocks, start=1):
        filename = resolve_filename(cb, idx, used_names)
        file_path = target_dir / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)

        existed = file_path.exists()
        file_path.write_text(cb.code, encoding="utf-8")
        written.append(file_path)

        ui.assistant_code_block(cb.language, filename, cb.code)
        ui.file_written(str(file_path), mode="modified" if existed else "created")

    return written