"""
LM Arena CLI - 작업 폴더(workspace) 공유

이 모듈은 완전히 새로 추가되는 기능이다. browser.py / ui.py / cli.py /
webapp.py / config.py / dom_selectors.py / file_writer.py / prompt_augment.py에
있던 기존 로직, DOM 셀렉터, 로그인/응답 완료 판정, 파일명 힌트 인식, "파일명
주석 지침" 문자열은 단 한 글자도 건드리지 않는다. 여기서는 그 위에 "작업
폴더 전체를 LM Arena에게 컨텍스트로 보여준다"는 완전히 별개의 기능만 새로
얹는다.

[기능 요약]
- 사용자가 지정한 작업 폴더(settings.target_dir - 기존에 "코드 블록 저장
  폴더"로 쓰이던 것과 동일한 폴더를 그대로 재사용한다) 아래의 전체 폴더/파일
  구조를 트리 형태로 만들고, 그중 텍스트로 읽을 수 있는 파일은 내용까지
  포함해 하나의 프롬프트 삽입용 블록 문자열로 만든다.
- 폴더/파일의 "이름과 경로"는 상한 없이 항상 트리 전체에 포함된다(중첩된
  하위 폴더도 전부 포함). 다만 내용까지 함께 보내는 것은 아래 상한선
  안에서만 이뤄지며, 상한을 넘는 파일은 트리에는 남아있되 내용만 생략된다.
- 이 블록을 "세션(LM Arena 대화 세션)당 한 번만" 프롬프트에 포함시키는 것은
  이 모듈의 책임이 아니다. 이 모듈은 순수하게 "폴더를 읽어서 블록 문자열을
  만드는" 역할만 하고, "이번 세션에 이미 보냈는지" 여부는 호출부(cli.py의
  session_state / webapp.py의 AutomationWorker._workspace_shared_this_session)
  가 관리한다.

[파일 구분 표기 방식]
개별 파일은 "=====" 같은 모호한 구분 문자 대신, LLM이 프롬프트에서 가장
흔하게 접하고 정확히 구분해내는 형태인 <file path="..."> ... </file> 태그로
감싼다. 왜 이렇게 바꿨는지:
- 태그 하나가 "여기서부터 파일 시작/여기서 파일 끝"이라는 의미를 명확히
  전달하며, 파일마다 반복되는 설명 문장("바이너리라 생략함" 등) 없이도
  skip="binary" / skip="sensitive" / truncated="true" 같은 속성 하나로
  같은 정보를 훨씬 짧게 전달할 수 있다(첨부 텍스트 총량 감소).
- 그 의미를 매 파일마다 다시 설명하지 않고, prompt_augment.py의
  WORKSPACE_CONTEXT_WRAPPER에서 이 표기 규칙을 딱 한 번만 안내한다.
- 코드 내용 자체는 이스케이프하거나 잘라내지 않고 원문 그대로 태그 사이에
  넣는다(아래 _try_read_text가 바이트를 자르기 전에 먼저 전체를 디코드한
  뒤 "문자" 단위로만 길이를 제한하므로, 멀티바이트 UTF-8 문자 중간이 잘려
  디코딩이 깨지는 일이 없다 - 코드가 부정확하게 전달되던 문제를 함께
  고쳤다).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# 트리 순회 시 건너뛸 디렉터리 이름들 (의존성/빌드 산출물/버전관리 내부 등,
# 공유해봐야 노이즈만 되고 토큰만 낭비하는 폴더).
EXCLUDED_DIR_NAMES = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".idea", ".vscode", "dist", "build", ".next", ".nuxt",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "target",
    ".tox", ".cache", ".parcel-cache", "coverage",
}

# 이름이 이걸로 "끝나면" 제외 (예: foo.egg-info).
EXCLUDED_DIR_SUFFIXES = (".egg-info",)

# 스캔 시 아예 건너뛰는(트리에도 안 보이는) 잡파일들.
SKIP_FILENAME_EXACT = {".DS_Store"}

# 트리/경로에는 정상적으로 표시하되, 내용은 절대 함께 보내지 않는 "자격증명
# 류로 보이는" 파일명 패턴. 사용자가 지정한 폴더를 통째로 외부 서비스(LM
# Arena)에 넘기는 기능이라, 이 정도의 최소한의 안전장치는 유지한다.
SENSITIVE_FILENAME_PATTERNS = (
    ".env", ".pem", ".key", ".p12", ".pfx", "id_rsa", "id_ed25519",
    "credentials.json", ".npmrc", ".netrc",
)

DEFAULT_MAX_FILES_WITH_CONTENT = 200
DEFAULT_MAX_FILE_CHARS = 40_000            # 파일 1개당 포함할 최대 문자 수
DEFAULT_MAX_TOTAL_CONTENT_CHARS = 500_000  # 전체 내용 합계 상한(프롬프트 폭주 방지)


@dataclass
class WorkspaceSnapshot:
    root: Path
    tree_text: str
    content_block: str
    included_file_count: int
    truncated: bool  # 상한에 걸려 일부 파일 내용이 생략되었는지


def _is_excluded_dir(name: str) -> bool:
    if name in EXCLUDED_DIR_NAMES:
        return True
    if name.startswith("."):
        # 숨김 디렉터리는 기본적으로 제외한다(버전관리/IDE 설정류가 대부분).
        return True
    return any(name.endswith(suf) for suf in EXCLUDED_DIR_SUFFIXES)


def _is_sensitive_filename(name: str) -> bool:
    lowered = name.lower()
    return any(pat in lowered for pat in SENSITIVE_FILENAME_PATTERNS)


def _try_read_text(path: Path, max_chars: int) -> tuple[str | None, bool]:
    """
    텍스트로 읽을 수 있으면 (내용, 잘렸는지)를, 바이너리로 판단되면
    (None, False)를 반환한다.

    [정확성 관련] 바이트를 먼저 자른 뒤 디코드하면, 멀티바이트 UTF-8 문자
    (한글, 이모지 등) 중간에서 잘려 디코딩 자체가 실패할 수 있다 - 그러면
    실제로는 멀쩡한 텍스트 파일이 통째로 "바이너리/읽을 수 없음"으로 잘못
    분류되어 코드가 아예 전달되지 않는 문제가 생긴다. 그래서 전체 바이트를
    먼저 정상적으로 디코드한 뒤에만(=완전한 문자열이 된 뒤에만) 문자 단위로
    길이를 제한한다. 이렇게 하면 잘림이 일어나도 항상 완전한 문자 경계에서만
    잘리고, 잘리지 않은 나머지 내용은 원문 그대로 정확하게 전달된다.
    """
    try:
        raw = path.read_bytes()
    except Exception:
        return None, False

    if b"\x00" in raw[:8000]:
        return None, False  # NUL 바이트 존재 -> 바이너리로 간주

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, False

    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return text, truncated


def _walk_sorted(root: Path) -> list[tuple[str, Path]]:
    """
    재귀적으로 순회하면서 각 단계마다 폴더 먼저, 그 다음 파일을 이름순으로
    정렬해 결정적인(항상 같은 순서의) 트리를 만든다. 반환값은
    (kind, path) 쌍의 리스트이며 kind는 "dir" 또는 "file"이다.
    """
    entries: list[tuple[str, Path]] = []
    try:
        children = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except Exception:
        return entries
    for child in children:
        try:
            is_dir = child.is_dir()
        except Exception:
            continue
        if is_dir:
            if _is_excluded_dir(child.name):
                continue
            entries.append(("dir", child))
            entries.extend(_walk_sorted(child))
        else:
            if child.name in SKIP_FILENAME_EXACT:
                continue
            entries.append(("file", child))
    return entries


def _build_tree_text(root: Path, entries: list[tuple[str, Path]]) -> str:
    lines = [f"{root.name}/"]
    for kind, path in entries:
        rel_parts = path.relative_to(root).parts
        depth = len(rel_parts) - 1
        indent = "  " * depth
        label = f"{path.name}/" if kind == "dir" else path.name
        lines.append(f"{indent}- {label}")
    return "\n".join(lines)


def _xml_attr(value: str) -> str:
    """path 속성값 안의 큰따옴표/역슬래시만 최소한으로 이스케이프한다
    (파일 내용 자체는 손대지 않고, 속성값 하나만 안전하게 만든다)."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _open_file_tag(rel_path: str, *, skip: str | None = None, truncated: bool = False) -> str:
    attrs = [f'path="{_xml_attr(rel_path)}"']
    if skip:
        attrs.append(f'skip="{skip}"')
    if truncated:
        attrs.append('truncated="true"')
    return f"<file {' '.join(attrs)}>"


def build_workspace_snapshot(
    root: Path,
    max_files_with_content: int = DEFAULT_MAX_FILES_WITH_CONTENT,
    max_file_chars: int = DEFAULT_MAX_FILE_CHARS,
    max_total_content_chars: int = DEFAULT_MAX_TOTAL_CONTENT_CHARS,
) -> WorkspaceSnapshot:
    """
    root 아래 전체 구조(트리)와, 읽을 수 있는 텍스트 파일들의 내용을 모아
    하나의 WorkspaceSnapshot으로 만든다. 폴더/파일 이름과 경로는 상한 없이
    전부 트리에 포함하지만(하위 폴더 안의 하위 폴더/파일도 이름·경로 형태로
    정확히 전달), 내용까지 포함하는 것은 위 상한들 안에서만 이뤄진다.
    """
    entries = _walk_sorted(root)
    tree_text = _build_tree_text(root, entries)

    content_chunks: list[str] = []
    included_count = 0
    total_chars = 0
    truncated = False

    for kind, path in entries:
        if kind != "file":
            continue

        if included_count >= max_files_with_content or total_chars >= max_total_content_chars:
            truncated = True
            continue

        rel_path = path.relative_to(root).as_posix()

        if _is_sensitive_filename(path.name):
            # 자기 닫힘 태그(내용 없음) - 별도 설명 문장 없이 skip 속성
            # 하나로 "왜 내용이 없는지"까지 전달한다.
            content_chunks.append(f'<file path="{_xml_attr(rel_path)}" skip="sensitive"/>')
            included_count += 1
            continue

        text, file_truncated = _try_read_text(path, max_file_chars)
        if text is None:
            content_chunks.append(f'<file path="{_xml_attr(rel_path)}" skip="binary"/>')
            included_count += 1
            continue

        remaining_budget = max_total_content_chars - total_chars
        if len(text) > remaining_budget:
            text = text[:remaining_budget]
            file_truncated = True
            truncated = True

        content_chunks.append(
            f"{_open_file_tag(rel_path, truncated=file_truncated)}\n{text}\n</file>"
        )
        included_count += 1
        total_chars += len(text)

    content_block = "\n".join(content_chunks)
    return WorkspaceSnapshot(
        root=root,
        tree_text=tree_text,
        content_block=content_block,
        included_file_count=included_count,
        truncated=truncated,
    )


def format_workspace_context_block(snapshot: WorkspaceSnapshot) -> str:
    """
    prompt_augment.py가 <workspace_context> 블록 안에 그대로 삽입할 수 있는
    하나의 문자열로 스냅샷을 직렬화한다. 부가 설명 문장은 최소화하고,
    <tree>/<file> 태그 자체가 구조를 전달하게 한다(각 태그의 의미는
    prompt_augment.py의 WORKSPACE_CONTEXT_WRAPPER에서 한 번만 안내됨).
    """
    parts = [f'<tree root="{_xml_attr(str(snapshot.root))}">', snapshot.tree_text, "</tree>"]
    if snapshot.content_block:
        parts.append(snapshot.content_block)
    if snapshot.truncated:
        parts.append(
            '<note>Some file contents were omitted due to size limits; '
            "the <tree> above still lists every folder and file.</note>"
        )
    return "\n".join(parts)