"""
LM Arena - Agent 모드 지원 (완전히 새로 추가되는 모듈)

이 모듈은 browser.py / ui.py / cli.py / webapp.py / file_writer.py /
prompt_augment.py / workspace.py 에 있던 기존 로직(로그인 판정, 프롬프트
전송, 스트리밍/응답 완료 판정, Direct 모드 코드 블록 파싱, 파일명 힌트
인식)을 단 한 줄도 바꾸지 않는다. model_selector.py와 동일하게
"LMArenaBrowser가 이미 들고 있는 Playwright Page를 받아서" Agent 모드에만
존재하는 UI 요소를 다루는 일만 한다.

이 모듈이 담당하는 것:
  1) read_workspace_tree(page)        Agent workspace 패널의 폴더/파일 트리 읽기
  2) find_workspace_download(page)    workspace 전체 zip 다운로드 버튼 찾기
  3) download_workspace(page, dest)   그 버튼을 눌러 zip을 받아 작업 폴더에 풀기
  4) review_pending(page)             "이 작업이 성공했습니까?" 평가 패널 감지
  5) submit_review(page, choice)      예 / 아니요 / 계속 작업하기 버튼 클릭

[왜 workspace를 zip으로 받아야 하는가]
Agent 모드의 artifact 카드는 코드 "미리보기"이고, 실제 프로젝트 파일의
정본은 LM Arena 서버의 workspace에 있다. 그래서 프로젝트가 끝난 시점
(=평가 패널이 뜨는 시점)에 workspace 전체를 zip으로 내려받아 압축을 풀어
사용자 작업 폴더(settings.target_dir)에 반영하는 것이 가장 정확하다.

Playwright sync API는 만든 스레드에서만 써야 하므로, 이 모듈의 모든
함수는 webapp.AutomationWorker의 워커 스레드(또는 CLI의 메인 스레드)에서만
호출해야 한다 - model_selector.py와 동일한 제약이다.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from playwright.sync_api import Page

from . import dom_selectors as sel
from .config import AGENT_MODE_URL


class AgentModeError(Exception):
    """Agent 모드 UI 조작 중 발생한 예상 가능한 오류(DOM 변경 포함)."""


@dataclass
class WorkspaceEntry:
    """workspace 패널의 한 줄(폴더 또는 파일)."""
    kind: str        # "dir" | "file"
    name: str
    depth: int       # padding-left로 추정한 트리 깊이 (0이 최상위)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "name": self.name, "depth": self.depth}


@dataclass
class DownloadResult:
    ok: bool
    files: list[str] = field(default_factory=list)
    archive_name: str = ""
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "files": self.files,
            "archive_name": self.archive_name,
            "error": self.error,
        }


# ── 모드 판별 ───────────────────────────────────────────────────────
def is_agent_url(url: str | None) -> bool:
    """현재 URL이 Agent 모드인지. (Direct 판별 로직은 건드리지 않는다)"""
    try:
        path = urlparse(url or "").path or ""
    except Exception:
        return False
    return path == "/agent" or path.startswith("/agent/")


def agent_url() -> str:
    return AGENT_MODE_URL


# ── 1) workspace 트리 읽기 ──────────────────────────────────────────
# 폴더/파일 행은 aria-label이 "<이름> folder" / "<이름> file"로 끝나고,
# 들여쓰기는 인라인 style의 padding-left(px)로 표현된다. 클래스 이름에
# 전혀 의존하지 않고 이 두 가지만으로 트리를 복원한다.
_READ_TREE_JS = r"""(args) => {
  const rows = [];
  const seen = new Set();

  function pad(el) {
    const raw = (el.style && el.style.paddingLeft) || "";
    const n = parseFloat(raw);
    return Number.isFinite(n) ? n : 0;
  }

  function push(el, kind, name) {
    if (!name || seen.has(el)) return;
    seen.add(el);
    rows.push({ kind: kind, name: name, pad: pad(el), top: el.getBoundingClientRect().top });
  }

  for (const s of args.folderSelectors) {
    let els = [];
    try { els = Array.from(document.querySelectorAll(s)); } catch (e) { continue; }
    if (!els.length) continue;
    for (const el of els) {
      const label = el.getAttribute("aria-label") || "";
      const m = label.match(/^(?:Expand|Collapse)\s+(.*?)\s+folder$/i) || label.match(/^(.*?)\s+folder$/i);
      push(el, "dir", (m ? m[1] : label).trim());
    }
    break;
  }

  for (const s of args.fileSelectors) {
    let els = [];
    try { els = Array.from(document.querySelectorAll(s)); } catch (e) { continue; }
    if (!els.length) continue;
    for (const el of els) {
      const label = el.getAttribute("aria-label") || "";
      const m = label.match(/^(.*?)\s+file$/i);
      push(el, "file", (m ? m[1] : label).trim());
    }
    break;
  }

  // 화면에 그려진 순서(위->아래)가 곧 트리 순서다.
  rows.sort((a, b) => a.top - b.top);

  // padding-left 값들을 작은 것부터 모아 depth(0,1,2...)로 환산한다.
  const pads = Array.from(new Set(rows.map((r) => r.pad))).sort((a, b) => a - b);
  return rows.map((r) => ({ kind: r.kind, name: r.name, depth: Math.max(0, pads.indexOf(r.pad)) }));
}"""


def read_workspace_tree(page: Page) -> list[WorkspaceEntry]:
    """
    Agent workspace 패널에 보이는 폴더/파일 목록을 읽는다. 패널이 없거나
    아직 아무 파일도 없으면 빈 리스트를 반환한다(오류가 아니다 - 프로젝트
    초반에는 workspace가 비어 있는 것이 정상이다).
    """
    try:
        rows = page.evaluate(
            _READ_TREE_JS,
            {
                "folderSelectors": sel.AGENT_WORKSPACE_FOLDER_ROW,
                "fileSelectors": sel.AGENT_WORKSPACE_FILE_ROW,
            },
        )
    except Exception:
        return []
    out: list[WorkspaceEntry] = []
    for row in rows or []:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        out.append(
            WorkspaceEntry(
                kind="dir" if row.get("kind") == "dir" else "file",
                name=name,
                depth=int(row.get("depth") or 0),
            )
        )
    return out


def workspace_has_files(page: Page) -> bool:
    return any(e.kind == "file" for e in read_workspace_tree(page))


# ── 2) workspace 전체 다운로드 버튼 찾기 ────────────────────────────
# 개별 파일에도 같은 모양의 다운로드 링크가 붙어 있으므로,
# "파일 행(role=button, aria-label이 ' file'로 끝남) 안에 들어있지 않은 것"
# 만 workspace 전체 다운로드로 인정한다.
_IS_INSIDE_FILE_ROW_JS = r"""(el) => {
  try {
    return !!el.closest("[role='button'][aria-label$=' file']");
  } catch (e) {
    return false;
  }
}"""


def find_workspace_download(page: Page):
    """
    workspace 전체를 zip으로 받는 버튼/링크의 Locator를 반환한다.
    찾지 못하면 None.
    """
    for selector in sel.AGENT_WORKSPACE_DOWNLOAD_BUTTON:
        try:
            loc = page.locator(selector)
            count = min(loc.count(), 30)
        except Exception:
            continue
        for i in range(count):
            el = loc.nth(i)
            try:
                if not el.is_visible():
                    continue
                if el.evaluate(_IS_INSIDE_FILE_ROW_JS):
                    continue  # 개별 파일 다운로드 링크는 제외
            except Exception:
                continue
            return el
    return None


# ── 3) zip 다운로드 + 압축 해제 ─────────────────────────────────────
def _safe_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """
    zip-slip(../ 경로로 폴더 밖에 파일을 쓰는 공격) 방지를 위해, 상대 경로가
    바깥으로 벗어나거나 절대 경로/드라이브 문자를 포함한 항목은 제외한다.
    """
    safe: list[zipfile.ZipInfo] = []
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if not name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
            continue
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            continue
        safe.append(info)
    return safe


def _common_root(members: list[zipfile.ZipInfo]) -> Optional[str]:
    """모든 항목이 같은 최상위 폴더 하나 아래에 있으면 그 폴더 이름을 반환."""
    roots = set()
    for info in members:
        name = info.filename.replace("\\", "/").strip("/")
        if not name:
            continue
        head = name.split("/", 1)
        if len(head) == 1 and not info.is_dir():
            return None  # 최상위에 파일이 바로 있으면 공통 루트가 아니다
        roots.add(head[0])
        if len(roots) > 1:
            return None
    return next(iter(roots)) if len(roots) == 1 else None


def extract_archive(archive: Path, dest_dir: Path, strip_root: bool = True) -> list[Path]:
    """
    zip을 dest_dir 아래에 푼다. 같은 이름의 파일이 이미 있으면 덮어쓴다
    (=수정). zip이 아니면 파일 하나를 그대로 복사한다.
    반환값은 실제로 기록된 파일 경로 목록.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    if not zipfile.is_zipfile(archive):
        target = dest_dir / archive.name
        shutil.copy2(archive, target)
        return [target]

    with zipfile.ZipFile(archive) as zf:
        members = _safe_members(zf)
        root = _common_root(members) if strip_root else None
        for info in members:
            name = info.filename.replace("\\", "/").strip("/")
            if root:
                if name == root:
                    continue
                if name.startswith(root + "/"):
                    name = name[len(root) + 1:]
            if not name:
                continue
            target = (dest_dir / name).resolve()
            try:
                target.relative_to(dest_dir.resolve())
            except ValueError:
                continue  # 방어적: 정규화 후에도 폴더 밖이면 건너뛴다
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            written.append(target)
    return written


def download_workspace(
    page: Page,
    dest_dir: Path,
    strip_root: bool = True,
    timeout_ms: int = 120_000,
) -> DownloadResult:
    """
    workspace 전체 다운로드 버튼을 눌러 zip을 받고, dest_dir(사용자 작업
    폴더)에 압축을 풀어 반영한다.

    strip_root=True면 zip 안에 최상위 폴더가 하나뿐일 때(예: test-site/)
    그 폴더를 벗겨내고 내용물만 작업 폴더 바로 아래에 푼다 - 사용자의
    작업 폴더 자체가 곧 프로젝트 폴더이기 때문이다.
    """
    if dest_dir is None:
        return DownloadResult(False, error="작업 폴더가 설정되어 있지 않습니다.")

    button = find_workspace_download(page)
    if button is None:
        return DownloadResult(False, error="workspace 다운로드 버튼을 찾지 못했습니다 (DOM 변경 가능성).")

    tmp_dir = Path(tempfile.mkdtemp(prefix="lmarena-workspace-"))
    try:
        with page.expect_download(timeout=timeout_ms) as info:
            button.click()
        download = info.value
        suggested = download.suggested_filename or "workspace.zip"
        archive = tmp_dir / os.path.basename(suggested)
        download.save_as(str(archive))

        written = extract_archive(archive, Path(dest_dir), strip_root=strip_root)
        return DownloadResult(
            ok=True,
            files=[str(p) for p in written],
            archive_name=archive.name,
        )
    except Exception as e:  # noqa: BLE001
        return DownloadResult(False, error=f"workspace 다운로드/압축 해제 실패: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── 4) 프로젝트 평가(리뷰) 패널 ─────────────────────────────────────
def _find_review_panel(page: Page):
    for selector in sel.AGENT_REVIEW_PANEL:
        try:
            loc = page.locator(selector)
            if loc.count() > 0 and loc.first.is_visible():
                return loc.first
        except Exception:
            continue
    return None


def review_pending(page: Page) -> bool:
    """
    "이 작업이 성공했습니까?" 평가 패널이 화면에 떠 있는가 = 프로젝트가
    끝났는가. Agent 모드에서 workspace 다운로드 시점을 정하는 신호로도 쓴다.
    """
    return _find_review_panel(page) is not None


def wait_for_review(page: Page, timeout_seconds: float = 20.0, poll: float = 0.5) -> bool:
    """답변이 끝난 직후 평가 패널이 조금 늦게 뜨는 경우를 위한 짧은 대기."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if review_pending(page):
            return True
        time.sleep(poll)
    return False


def _norm(text: str) -> str:
    return " ".join((text or "").split()).strip().casefold()


def read_review_options(page: Page) -> list[dict]:
    """
    평가 패널에 실제로 보이는 선택지들을 [{"choice","label"}] 형태로
    반환한다. 패널이 없으면 빈 리스트.
    """
    panel = _find_review_panel(page)
    if panel is None:
        return []

    labels: list[str] = []
    for selector in sel.AGENT_REVIEW_OPTION_BUTTON:
        try:
            loc = panel.locator(selector)
            count = loc.count()
        except Exception:
            continue
        if count == 0:
            continue
        for i in range(count):
            try:
                btn = loc.nth(i)
                if btn.get_attribute("aria-label"):
                    continue  # 닫기 버튼 등 아이콘 전용 버튼은 제외
                text = " ".join((btn.inner_text() or "").split()).strip()
            except Exception:
                continue
            if text and text not in labels:
                labels.append(text)
        break

    out: list[dict] = []
    for choice, candidates in sel.AGENT_REVIEW_CHOICES.items():
        match = next(
            (lbl for lbl in labels if any(_norm(lbl) == _norm(c) for c in candidates)),
            None,
        ) or next(
            (lbl for lbl in labels if any(_norm(c) in _norm(lbl) for c in candidates)),
            None,
        )
        if match:
            out.append({"choice": choice, "label": match})
    return out


def submit_review(page: Page, choice: str) -> tuple[bool, Optional[str]]:
    """
    평가 버튼(예 / 아니요 / 계속 작업하기)을 클릭한다.
    반환: (성공 여부, 오류 메시지)
    """
    key = (choice or "").strip().lower()
    candidates = sel.AGENT_REVIEW_CHOICES.get(key)
    if not candidates:
        return False, f"알 수 없는 평가 선택지입니다: {choice}"

    panel = _find_review_panel(page)
    if panel is None:
        return False, "평가 패널이 화면에 없습니다 (이미 처리되었을 수 있습니다)."

    for selector in sel.AGENT_REVIEW_OPTION_BUTTON:
        try:
            loc = panel.locator(selector)
            count = loc.count()
        except Exception:
            continue
        if count == 0:
            continue
        # 1차: 텍스트 완전 일치 -> 2차: 포함 일치
        for exact in (True, False):
            for i in range(count):
                btn = loc.nth(i)
                try:
                    if btn.get_attribute("aria-label"):
                        continue
                    text = _norm(btn.inner_text())
                except Exception:
                    continue
                if not text:
                    continue
                hit = any(_norm(c) == text for c in candidates) if exact else any(
                    _norm(c) in text for c in candidates
                )
                if hit:
                    try:
                        btn.click()
                        time.sleep(0.4)
                        return True, None
                    except Exception as e:  # noqa: BLE001
                        return False, f"평가 버튼 클릭에 실패했습니다: {e}"
        break

    return False, "평가 버튼을 찾지 못했습니다 (DOM 변경 가능성)."


def close_review(page: Page) -> bool:
    """평가 패널을 답하지 않고 닫는다(Esc와 동일)."""
    panel = _find_review_panel(page)
    if panel is None:
        return False
    for selector in sel.AGENT_REVIEW_CLOSE_BUTTON:
        try:
            loc = panel.locator(selector)
            if loc.count() > 0:
                loc.first.click()
                return True
        except Exception:
            continue
    return False
