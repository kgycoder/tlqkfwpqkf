"""
LM Arena CLI - 브라우저(웹) UI 백엔드

이 파일은 새로 추가되는 기능이다. browser.py / ui.py / cli.py / config.py /
dom_selectors.py / file_writer.py 에 있는 기존 로직, DOM 셀렉터, 로그인 판정,
응답 완료 판정, 코드 블록 저장 방식은 단 한 줄도 수정하지 않는다. 이 모듈은
그 위에 얇은 HTTP/SSE 계층 하나만 새로 얹어서, 기존과 동일한 LMArenaBrowser를
그대로 재사용해 브라우저 기반 UI에 데이터를 공급한다.

중요한 구분:
  - "자동화 브라우저"(automation browser): Playwright가 제어하는 Chromium.
    기존 코드 그대로(headless=False면 창이 뜨고, 로그아웃 상태면 사용자가
    여기서 직접 로그인한다). 절대 UI를 그리는 용도가 아니다.
  - "UI 브라우저": 사용자의 PC 기본 브라우저(크롬/엣지/사파리 등, 이미
    창이 열려 있다면 새 탭)에 열리는 로컬 웹페이지(http://127.0.0.1:<port>).
    모든 조작(프롬프트 전송, 새 채팅, 폴더 변경, 로그아웃 등)은 여기서
    이루어진다.

Playwright의 sync API는 그것을 만든 스레드에서만 호출해야 안전하므로,
전용 워커 스레드 하나가 LMArenaBrowser 인스턴스를 소유하고, 모든 브라우저
조작은 잡(Job) 큐를 통해 그 스레드에서만 실행되도록 한다. Flask(개발 서버)는
여러 스레드에서 HTTP 요청을 처리하지만, 실제 Playwright 호출은 항상 이
워커 스레드로 위임된다.
"""
from __future__ import annotations

import json
import queue
import re
import shutil
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from flask import Flask, jsonify, request, Response, send_from_directory

from .config import (
    Settings,
    USER_DATA_DIR,
    CHAT_MODE_AGENT,
    CHAT_MODE_DIRECT,
    normalize_chat_mode,
)
from . import model_selector as msel
from . import agent_mode
from .browser import LMArenaBrowser, CodeBlock, ReasoningState, GenerationStopped
from .file_writer import write_code_blocks
from .prompt_augment import build_outgoing_prompt
from .work_agent import WorkEvent, run_work_task
from .workspace import build_workspace_snapshot, format_workspace_context_block

STATIC_DIR = Path(__file__).parent / "web_static"


# ── 직렬화 헬퍼 (기존 dataclass -> JSON) ─────────────────────────────
def _code_block_to_dict(cb: CodeBlock) -> dict:
    return {
        "language": cb.language,
        "filename_hint": cb.filename_hint,
        "code": cb.code,
    }


def _reasoning_to_dict(r: Optional[ReasoningState]) -> Optional[dict]:
    if r is None:
        return None
    return {
        "text": r.text,
        "elapsed_seconds": round(r.elapsed_seconds, 1),
        "is_done": r.is_done,
    }


@dataclass
class Job:
    kind: str
    payload: dict = field(default_factory=dict)
    out: "queue.Queue" = field(default_factory=queue.Queue)


class AutomationWorker:
    """
    기존 LMArenaBrowser를 소유하고 실제 Playwright 호출을 전담하는 워커 스레드.
    HTTP 핸들러 쪽에서는 이 클래스의 submit()으로 작업을 큐에 넣고,
    반환된 Job.out 큐에서 결과(또는 스트리밍 이벤트)를 받아간다.
    """

    def __init__(self) -> None:
        self.settings = Settings.load()
        self.browser: Optional[LMArenaBrowser] = None
        self._jobs: "queue.Queue[Job]" = queue.Queue()
        self._state_lock = threading.Lock()
        self.state: dict[str, Any] = {
            # starting -> starting_browser -> connecting -> waiting_login
            # -> ready -> (login_timeout | error | closed)
            "stage": "starting",
            "logged_in": False,
            "target_dir": self.settings.target_dir,
            "auto_write_files": self.settings.auto_write_files,
            "enforce_filename_comments": self.settings.enforce_filename_comments,
            "share_workspace": self.settings.share_workspace,
            "work_mode": self.settings.work_mode,
            # 채팅 모드(완전히 새로 추가): "direct"(기존 동작) 또는 "agent".
            # Agent일 때 프런트엔드는 모델 선택 UI를 숨기고, 답변을 Agent
            # 전용(artifact 카드 / workspace / 평가 패널) UI로 그린다.
            "chat_mode": normalize_chat_mode(self.settings.chat_mode),
            "agent_auto_download": self.settings.agent_auto_download,
            # 평가("이 작업이 성공했습니까?") 응답을 기다리는 중인지.
            "agent_review_pending": False,
            # 모델 선택(완전히 새로 추가): 모델 버튼에 표시된 현재 모델.
            # {"name", "icon"(아이콘 ID 또는 None), "slug"} 또는 None.
            "current_model": None,
            "error": None,
        }
        # 화면 새로고침 시 대화 이력을 복원하기 위한 서버 측 기록.
        # (LM Arena 자체 대화 기록과는 별개의, UI 표시 전용 사본)
        self.history: list[dict] = []
        self._history_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        # 워크스페이스 공유(완전히 새로 추가): "이번 LM Arena 대화 세션에
        # 이미 폴더 전체를 보냈는지"를 나타내는 런타임 전용 플래그. Settings
        # 와 달리 저장되지 않으며, 새 채팅을 시작하거나 작업 폴더를 바꾸거나
        # 공유 토글을 새로 켤 때마다 False로 리셋된다.
        self._workspace_shared_this_session = False
        # 답변 중단(Stop) 요청 플래그(완전히 새로 추가). Flask 요청 스레드
        # (POST /api/stop을 처리하는, 워커와는 다른 스레드)에서 set()하고,
        # Playwright를 소유한 워커 스레드가 스트리밍 루프 도중 이를 감지해
        # 실제 클릭을 수행한다 - Playwright sync API는 그것을 생성한
        # 스레드에서만 호출해야 하므로, 요청 스레드가 브라우저를 직접
        # 조작하지 않고 이 threading.Event만 건드린다.
        self._stop_event = threading.Event()
        # 모델 선택(완전히 새로 추가). 모델 목록 카탈로그와 lab 아이콘 저장소.
        # 아이콘은 내용 해시를 ID로 삼아 /api/model-icon/<ID> 로 내려준다.
        # 디스크 캐시(model_selector.MODELS_CACHE_FILE)가 있으면 재시작 직후에도
        # 목록/아이콘을 바로 보여줄 수 있다.
        self._icons: dict[str, str] = {}
        self._icons_lock = threading.Lock()
        self._catalog: Optional[dict] = None
        cache = msel.load_cache()
        if cache:
            self._icons.update({k: v for k, v in cache["icons"].items() if isinstance(v, str)})
            self._catalog = {"fetched_at": cache.get("fetched_at", 0), "models": cache["models"]}

    # ── 상태 조회/갱신 ────────────────────────────────────────────
    def _set_state(self, **kwargs) -> None:
        with self._state_lock:
            self.state.update(kwargs)

    def get_state(self) -> dict:
        with self._state_lock:
            return dict(self.state)

    def get_history(self) -> list[dict]:
        with self._history_lock:
            return list(self.history)

    def _append_history(self, entry: dict) -> None:
        with self._history_lock:
            self.history.append(entry)

    def _clear_history(self) -> None:
        with self._history_lock:
            self.history.clear()

    # ── 시작 ──────────────────────────────────────────────────────
    def start(self) -> None:
        self._thread.start()

    def submit(self, kind: str, payload: Optional[dict] = None) -> Job:
        job = Job(kind=kind, payload=payload or {})
        self._jobs.put(job)
        return job

    def request_stop(self) -> None:
        """
        현재 진행 중인 LM Arena 답변 생성을 즉시 중단하도록 요청한다. 잡
        큐에 넣지 않는 이유: send_prompt 잡을 처리 중인 워커 스레드는
        스트리밍 루프(iter_response_stream)에 갇혀 있어 큐를 비우지
        못하므로, 큐에 넣으면 답변이 끝날 때까지 처리되지 않는다. 대신
        스레드 안전한 플래그만 세워두면, 그 루프가 매 델타마다 직접
        확인(_check_stop)해 즉시 반응한다.
        """
        self._stop_event.set()

    # ── 워커 루프 (Playwright 호출은 전부 이 스레드 안에서만 일어난다) ──
    def _run(self) -> None:
        try:
            self.browser = LMArenaBrowser(headless=self.settings.headless)
            self._set_state(stage="starting_browser")
            self.browser.start()

            self._set_state(stage="connecting")
            # 마지막으로 사용하던 채팅 모드로 접속한다. Direct면
            # 기존과 동일하게 goto_direct_mode()가 그대로 호출된다.
            startup_mode = normalize_chat_mode(self.settings.chat_mode)
            if startup_mode == CHAT_MODE_AGENT:
                self.browser.goto_agent_mode()
            else:
                self.browser.goto_direct_mode()
            self._set_state(chat_mode=self.browser.chat_mode)

            if self.browser.is_logged_in():
                self._set_state(logged_in=True, stage="ready")
            else:
                self._set_state(logged_in=False, stage="waiting_login")
                # LM Arena 사이트 화면이 자동화 창에 그대로 노출되지 않도록,
                # 로그인 버튼을 자동으로 눌러 로그인 창(팝업/모달)만 보이게
                # 한다. 로그인 판정(wait_for_login/is_logged_in) 자체는
                # 전혀 바뀌지 않고, 그 사이에 화면에 무엇이 보이는지만 바뀐다.
                self.browser.enter_login_only_view()
                try:
                    ok = self.browser.wait_for_login(timeout_seconds=600)
                finally:
                    self.browser.exit_login_only_view()
                if ok:
                    self._set_state(logged_in=True, stage="ready")
                else:
                    self._set_state(
                        stage="login_timeout",
                        error="로그인 대기 시간이 초과되었습니다. 자동화 브라우저 창에서 로그인 후 앱을 다시 시작해주세요.",
                    )

            # 로그인 완료(또는 이미 로그인된 상태) 이후로는 자동화 창에
            # 실제 LM Arena 화면 대신 직접 디자인한 "로그인 완료" 커스텀
            # 화면만 보이게 한다. send_prompt 등 실제 DOM 통신 로직에는
            # 영향을 주지 않는다.
            if self.get_state().get("logged_in"):
                self.browser.show_connected_overlay()
        except Exception as e:  # noqa: BLE001
            self._set_state(stage="error", error=str(e))

        # 모델 선택: 준비가 끝났으면 현재 모델(이름/아이콘)만 읽어 둔다. 창을 열지
        # 않는 DOM 읽기뿐이라 화면에 아무 영향도 주지 않는다.
        # Agent 모드에는 모델 선택 UI 자체가 없으므로 건너뛴다.
        if self.get_state().get("logged_in") and self._mode() != CHAT_MODE_AGENT:
            self._refresh_current_model(wait_seconds=5.0)

        while True:
            job = self._jobs.get()
            try:
                self._handle_job(job)
            except Exception as e:  # noqa: BLE001
                job.out.put({"type": "error", "message": str(e)})
                job.out.put(None)

    def _handle_job(self, job: Job) -> None:
        if self.browser is None:
            job.out.put({"type": "error", "message": "브라우저가 아직 준비되지 않았습니다."})
            job.out.put(None)
            return

        if job.kind == "send_prompt":
            self._handle_send_prompt(job)
        elif job.kind == "new_chat":
            # Agent 모드에서는 'New Chat' 링크가 /agent 쪽을 가리키므로,
            # 모드에 맞는 새 채팅 진입점을 쓴다(Direct에서는 기존
            # browser.new_chat()이 그대로 호출된다).
            ok = self.browser.new_chat_for_mode()
            if ok:
                self._clear_history()
                # 새 LM Arena 대화 세션이 시작됐으므로, 워크스페이스도
                # 다음 메시지에서 다시 한 번 공유되게 한다.
                self._workspace_shared_this_session = False
                self._set_state(agent_review_pending=False)
                if self._mode() != CHAT_MODE_AGENT:
                    self._refresh_current_model(wait_seconds=3.0)
            job.out.put({"type": "new_chat_result", "ok": ok})
            job.out.put(None)
        elif job.kind == "set_chat_mode":
            self._handle_set_chat_mode(job)
        elif job.kind == "agent_review":
            self._handle_agent_review(job)
        elif job.kind == "agent_download":
            self._handle_agent_download(job)
        elif job.kind == "agent_workspace":
            tree = [e.to_dict() for e in agent_mode.read_workspace_tree(self.browser.page)]
            job.out.put({"type": "agent_workspace", "tree": tree})
            job.out.put(None)
        elif job.kind == "set_target":
            raw = job.payload.get("target_dir", "")
            path = Path(raw).expanduser().resolve()
            path.mkdir(parents=True, exist_ok=True)
            self.settings.target_dir = str(path)
            self.settings.save()
            self._set_state(target_dir=str(path))
            # 작업 폴더(=워크스페이스 공유 대상)가 바뀌었으므로, 다음
            # 메시지에서 새 폴더 기준으로 다시 한 번 공유되게 한다.
            self._workspace_shared_this_session = False
            job.out.put({"type": "target_result", "target_dir": str(path)})
            job.out.put(None)
        elif job.kind == "set_share_workspace":
            # 웹 UI의 토글에서 워크스페이스 공유를 켜고 끄는 잡. 기존
            # set_auto_write와 완전히 동일한 패턴.
            was_enabled = self.settings.share_workspace
            self.settings.share_workspace = bool(job.payload.get("value", False))
            self.settings.save()
            if self.settings.share_workspace and not was_enabled:
                # 꺼져 있다가 새로 켜졌으니, 다음 메시지에서 처음부터 새로
                # 공유되게 한다.
                self._workspace_shared_this_session = False
            self._set_state(share_workspace=self.settings.share_workspace)
            job.out.put(
                {"type": "share_workspace_result", "value": self.settings.share_workspace}
            )
            job.out.put(None)
        elif job.kind == "set_work_mode":
            self.settings.work_mode = bool(job.payload.get("value", False))
            self.settings.save()
            self._set_state(work_mode=self.settings.work_mode)
            job.out.put({"type": "work_mode_result", "value": self.settings.work_mode})
            job.out.put(None)
        elif job.kind == "set_auto_write":
            self.settings.auto_write_files = bool(job.payload.get("value", True))
            self.settings.save()
            self._set_state(auto_write_files=self.settings.auto_write_files)
            job.out.put({"type": "auto_write_result", "value": self.settings.auto_write_files})
            job.out.put(None)
        elif job.kind == "set_filename_enforcement":
            # CLI의 '/filename' 명령과 동일한 설정(enforce_filename_comments)을
            # 웹 UI에서도 토글할 수 있도록 하는 잡. 기존 잡 종류/핸들러는
            # 그대로 두고 새 분기만 추가한다.
            self.settings.enforce_filename_comments = bool(job.payload.get("value", True))
            self.settings.save()
            self._set_state(enforce_filename_comments=self.settings.enforce_filename_comments)
            job.out.put(
                {
                    "type": "filename_enforcement_result",
                    "value": self.settings.enforce_filename_comments,
                }
            )
            job.out.put(None)
        elif job.kind == "list_models":
            self._handle_list_models(job)
        elif job.kind == "select_model":
            self._handle_select_model(job)
        elif job.kind == "refresh_login":
            job.out.put({"type": "login_result", "logged_in": self.browser.is_logged_in()})
            job.out.put(None)
        elif job.kind == "logout":
            self.browser.close()
            shutil.rmtree(USER_DATA_DIR, ignore_errors=True)
            self._set_state(stage="closed", logged_in=False)
            job.out.put({"type": "logout_result", "ok": True})
            job.out.put(None)
        else:
            job.out.put({"type": "error", "message": f"알 수 없는 작업: {job.kind}"})
            job.out.put(None)

    def _check_stop(self, job: Job) -> None:
        """
        스트리밍 루프(정상 Direct 응답 / Work 모드의 각 턴) 안에서 델타를
        받을 때마다 호출한다. 중단 요청이 없으면 아무 일도 하지 않는다.
        요청이 있었으면 실제 LM Arena의 'Stop generation' 버튼을 클릭하고,
        호출부에게 즉시 중단을 알리기 위해 GenerationStopped를 발생시킨다
        (호출부가 for 루프를 도중에 빠져나오게 한다).
        """
        if not self._stop_event.is_set():
            return
        self._stop_event.clear()
        self.browser.stop_generation()
        job.out.put({"type": "stopped"})
        raise GenerationStopped()

    # ── Agent 모드 (완전히 새로 추가) ─────────────────────────────
    # 실제 LM Arena 조작은 전부 agent_mode.py가 기존 LMArenaBrowser의 page를
    # 받아서 수행한다. 여기서는 잡/상태/SSE 이벤트로 감싸기만 한다.
    def _mode(self) -> str:
        return normalize_chat_mode(self.get_state().get("chat_mode"))

    def _handle_set_chat_mode(self, job: Job) -> None:
        assert self.browser is not None
        mode = normalize_chat_mode(job.payload.get("mode"))
        try:
            actual = self.browser.goto_mode(mode)
        except Exception as e:  # noqa: BLE001
            job.out.put({"type": "chat_mode_result", "ok": False, "chat_mode": self._mode(), "error": str(e)})
            job.out.put(None)
            return

        self.settings.chat_mode = actual
        self.settings.save()
        # 모드를 바꾸면 LM Arena 대화가 새로 시작되므로, 새 채팅과 동일하게
        # UI 이력과 워크스페이스 공유 상태를 정리한다.
        self._clear_history()
        self._workspace_shared_this_session = False
        self._set_state(chat_mode=actual, agent_review_pending=False)

        if actual == CHAT_MODE_AGENT:
            # Agent 모드에는 모델 선택 UI가 없다 -> 프런트엔드가 버튼을 숨길
            # 수 있도록 현재 모델 표시를 비운다.
            self._set_state(current_model=None)
        else:
            self._refresh_current_model(wait_seconds=3.0)

        job.out.put(
            {
                "type": "chat_mode_result",
                "ok": True,
                "chat_mode": actual,
                "current": self.get_state().get("current_model"),
                "error": None,
            }
        )
        job.out.put(None)

    def _agent_download_workspace(self) -> dict:
        """workspace zip을 받아 작업 폴더에 푼다. 결과를 dict로 반환."""
        assert self.browser is not None
        target = self.settings.target_path
        if target is None:
            return {
                "ok": False,
                "files": [],
                "archive_name": "",
                "error": "작업 폴더가 설정되어 있지 않아 workspace를 저장할 수 없습니다.",
            }
        result = agent_mode.download_workspace(
            self.browser.page,
            target,
            strip_root=self.settings.agent_strip_root_folder,
        )
        return result.to_dict()

    def _handle_agent_download(self, job: Job) -> None:
        job.out.put({"type": "agent_download", **self._agent_download_workspace()})
        job.out.put(None)

    def _handle_agent_review(self, job: Job) -> None:
        assert self.browser is not None
        choice = str(job.payload.get("choice", ""))
        if choice == "dismiss":
            ok = agent_mode.close_review(self.browser.page)
            error = None if ok else "평가 패널을 닫지 못했습니다."
        else:
            ok, error = agent_mode.submit_review(self.browser.page, choice)
        if ok:
            self._set_state(agent_review_pending=False)
        job.out.put({"type": "agent_review_result", "ok": ok, "choice": choice, "error": error})
        job.out.put(None)

    # ── 모델 선택 (완전히 새로 추가) ──────────────────────────────
    # 실제 LM Arena 조작은 전부 model_selector.py가 기존 LMArenaBrowser의 page를
    # 받아서 수행한다. 여기서는 결과를 캐시/직렬화해 HTTP 계층에 넘길 뿐이다.
    def _register_icon(self, svg: Optional[str]) -> Optional[str]:
        if not svg:
            return None
        key = msel.icon_id(svg)
        with self._icons_lock:
            self._icons[key] = svg
        return key

    def get_model_icon(self, key: str) -> Optional[str]:
        with self._icons_lock:
            return self._icons.get(key)

    def _current_model_payload(self, info: Optional[dict]) -> Optional[dict]:
        if not info:
            return None
        return {
            "name": info["name"],
            "icon": self._register_icon(info.get("icon_svg")),
            "slug": info.get("slug"),
        }

    def _refresh_current_model(self, wait_seconds: float = 0.0) -> Optional[dict]:
        """모델 버튼에서 현재 모델을 읽어 state에 반영한다(읽지 못하면 이전 값 유지)."""
        if self.browser is None or self.browser.page is None:
            return None
        deadline = time.time() + wait_seconds
        while True:
            try:
                payload = self._current_model_payload(msel.read_current_model(self.browser.page))
            except Exception:  # noqa: BLE001
                payload = None
            if payload is not None:
                self._set_state(current_model=payload)
                return payload
            if time.time() >= deadline:
                return None
            time.sleep(0.4)

    def _models_payload(self, ok: bool = True, **extra) -> dict:
        catalog = self._catalog or {}
        return {
            "type": "models_result",
            "ok": ok,
            "models": catalog.get("models", []),
            "fetched_at": catalog.get("fetched_at"),
            "current": self.get_state().get("current_model"),
            **extra,
        }

    def _handle_list_models(self, job: Job) -> None:
        assert self.browser is not None and self.browser.page is not None
        force = bool(job.payload.get("force", False))
        if self._catalog and not force and msel.cache_is_fresh(self._catalog):
            job.out.put(self._models_payload(cached=True))
            job.out.put(None)
            return
        try:
            models = msel.scrape_models(self.browser.page)
            rows = [
                {"name": m.name, "search": m.search, "icon": self._register_icon(m.icon_svg)}
                for m in models
            ]
            fetched_at = time.time()
            self._catalog = {"fetched_at": fetched_at, "models": rows}
            with self._icons_lock:
                icons_snapshot = dict(self._icons)
            msel.save_cache(rows, icons_snapshot, fetched_at)
            self._refresh_current_model()
            job.out.put(self._models_payload(cached=False))
        except Exception as e:  # noqa: BLE001
            # 실패해도 이전(오래된) 목록이 있으면 함께 내려줘서 UI가 계속 쓸 수 있게 한다.
            job.out.put(self._models_payload(ok=False, error=str(e)))
        job.out.put(None)

    def _handle_select_model(self, job: Job) -> None:
        assert self.browser is not None and self.browser.page is not None
        name = str(job.payload.get("name", ""))
        try:
            result = msel.select_model(self.browser.page, name)
        except Exception as e:  # noqa: BLE001
            job.out.put({"type": "model_result", "ok": False, "current": None, "reset": False, "error": str(e)})
            job.out.put(None)
            return
        if result.reset:
            # URL 이동 폴백으로 LM Arena 대화가 새로 시작됐다 -> new_chat과 동일하게 정리.
            self._clear_history()
            self._workspace_shared_this_session = False
        payload = self._current_model_payload(result.current)
        if payload is not None:
            self._set_state(current_model=payload)
        job.out.put(
            {
                "type": "model_result",
                "ok": result.ok,
                "current": payload,
                "reset": result.reset,
                "error": result.error,
            }
        )
        job.out.put(None)

    def _handle_send_prompt(self, job: Job) -> None:
        assert self.browser is not None
        prompt = job.payload["prompt"]

        self._append_history({"role": "user", "content": prompt})
        job.out.put({"type": "user_echo", "prompt": prompt})
        # 새 프롬프트를 시작하므로, 이전 턴에서 미처 소모되지 않은 중단
        # 요청이 있었다면 여기서 리셋한다(항상 "이번 턴"에 대해서만 유효).
        self._stop_event.clear()

        # Agent 모드가 켜져 있으면 Work 모드보다 우선한다 - Agent 모드에서는
        # LM Arena가 자체 workspace에서 직접 프로젝트를 만들기 때문에, 이
        # PC를 조작하는 Work 루프와 동시에 돌릴 이유가 없다.
        if self._mode() == CHAT_MODE_AGENT:
            self._handle_agent_prompt(job, prompt)
            return

        if self.settings.work_mode:
            self._handle_work_prompt(job, prompt)
            return

        # 워크스페이스 공유(완전히 새로 추가된 기능): 켜져 있고 이번 세션에
        # 아직 보내지 않았다면 지금 딱 한 번만 작업 폴더 전체를 스캔해서
        # 함께 보낸다. cli.py의 _build_workspace_context_or_warn과 동일한
        # 이유로, 폴더가 없거나 스캔이 실패해도 프롬프트 전송 자체는 막지
        # 않고 이번 턴은 공유 없이 그대로 진행한다.
        workspace_context = None
        if self.settings.share_workspace and not self._workspace_shared_this_session:
            if self.settings.target_path:
                try:
                    snapshot = build_workspace_snapshot(self.settings.target_path)
                    workspace_context = format_workspace_context_block(snapshot)
                    self._workspace_shared_this_session = True
                    job.out.put(
                        {
                            "type": "workspace_shared",
                            "included_file_count": snapshot.included_file_count,
                        }
                    )
                except Exception as e:  # noqa: BLE001
                    job.out.put(
                        {
                            "type": "workspace_share_warning",
                            "message": f"작업 폴더를 읽는 중 오류가 발생해 이번 턴은 공유 없이 진행합니다: {e}",
                        }
                    )
            else:
                job.out.put(
                    {
                        "type": "workspace_share_warning",
                        "message": "작업 폴더가 설정되어 있지 않아 워크스페이스를 공유할 수 없습니다.",
                    }
                )

        # cli.py의 _handle_one_turn과 동일한 이유로, 실제 전송 문자열에는
        # (설정이 켜져 있으면) 파일명 주석 지침을 덧붙인다. 화면/이력에는
        # 위에서 이미 사용자 원문을 그대로 기록했으므로 변화가 없다.
        # send_prompt / iter_response_stream / parse_last_response에는
        # 반드시 이 outgoing_prompt를 동일하게 넘겨야 에코 판별이 맞는다.
        outgoing_prompt = build_outgoing_prompt(
            prompt,
            self.settings.enforce_filename_comments,
            workspace_context=workspace_context,
            workspace_sharing_enabled=self.settings.share_workspace,
        )

        baseline = self.browser.send_prompt(outgoing_prompt)

        final_plain = ""
        final_blocks: list[CodeBlock] = []
        try:
            for plain_text, code_blocks, reasoning, done in self.browser.iter_response_stream(
                baseline_count=baseline, sent_prompt=outgoing_prompt, timeout_seconds=600
            ):
                final_plain = plain_text
                final_blocks = code_blocks
                job.out.put(
                    {
                        "type": "delta",
                        "plain_text": plain_text,
                        "code_blocks": [_code_block_to_dict(cb) for cb in code_blocks],
                        "reasoning": _reasoning_to_dict(reasoning),
                        "done": done,
                    }
                )
                self._check_stop(job)
        except GenerationStopped:
            # 사용자가 답변 중단 버튼을 눌렀다. "stopped" 이벤트는 이미
            # _check_stop에서 보냈으므로, 지금까지 감지된 내용(final_plain/
            # final_blocks)으로 정상 종료 경로(아래 parse_last_response 등)를
            # 그대로 이어간다 - 에러가 아니라 정상적인 조기 종료로 취급한다.
            pass
        except Exception as e:  # noqa: BLE001
            job.out.put({"type": "error", "message": str(e)})
            job.out.put(None)
            return

        parsed = self.browser.parse_last_response(baseline_count=baseline, sent_prompt=outgoing_prompt)
        written_files: list[str] = []
        if parsed.code_blocks and self.settings.auto_write_files and self.settings.target_path:
            paths = write_code_blocks(self.settings.target_path, parsed.code_blocks)
            written_files = [str(p) for p in paths]

        self._append_history(
            {
                "role": "assistant",
                "content": final_plain,
                "code_blocks": [_code_block_to_dict(cb) for cb in final_blocks],
                "written_files": written_files,
            }
        )

        job.out.put(
            {
                "type": "final",
                "plain_text": final_plain,
                "code_blocks": [_code_block_to_dict(cb) for cb in final_blocks],
                "written_files": written_files,
            }
        )
        job.out.put(None)


    def _share_workspace_if_needed(self, job: Job) -> Optional[str]:
        """
        워크스페이스 공유가 켜져 있고 이번 세션에 아직 보내지 않았다면 한
        번만 작업 폴더를 스캔해 컨텍스트 블록을 만든다. 기존
        _handle_send_prompt / _handle_work_prompt 안에 있던 것과 동일한
        절차를, Agent 모드에서도 그대로 쓰기 위해 헬퍼로만 꺼낸 것이다
        (기존 두 함수의 코드는 그대로 두었다).
        """
        if not (self.settings.share_workspace and not self._workspace_shared_this_session):
            return None
        if not self.settings.target_path:
            job.out.put(
                {
                    "type": "workspace_share_warning",
                    "message": "작업 폴더가 설정되어 있지 않아 워크스페이스를 공유할 수 없습니다.",
                }
            )
            return None
        try:
            snapshot = build_workspace_snapshot(self.settings.target_path)
            self._workspace_shared_this_session = True
            job.out.put(
                {
                    "type": "workspace_shared",
                    "included_file_count": snapshot.included_file_count,
                }
            )
            return format_workspace_context_block(snapshot)
        except Exception as e:  # noqa: BLE001
            job.out.put(
                {
                    "type": "workspace_share_warning",
                    "message": f"작업 폴더를 읽는 중 오류가 발생해 이번 턴은 공유 없이 진행합니다: {e}",
                }
            )
            return None

    def _handle_agent_prompt(self, job: Job, prompt: str) -> None:
        """
        Agent 모드 한 턴.

        전송/스트리밍/완료 판정은 Direct 모드와 완전히 동일한 기존 경로
        (send_prompt -> iter_response_stream -> parse_last_response)를 그대로
        쓴다. 일반 답변과 추론 DOM이 두 모드에서 같기 때문이다. 달라지는
        것은 (a) 코드 블록이 artifact 카드 DOM으로 파싱된다는 점
        (browser._code_selector_set()이 모드를 보고 알아서 고른다), (b) 답변이
        스트리밍되는 동안에도 workspace 트리를 주기적으로 함께 읽어 폴더/
        파일이 생기는 대로 실시간으로 화면에 보여준다는 점(과거에는 답변이
        완전히 끝난 뒤에야 한 번만 읽어서, 프로젝트가 다 끝난 다음에야
        workspace UI가 갑자기 나타나는 것처럼 보였다), (c) 프로젝트가
        끝났으면(=평가 패널이 뜨면) workspace zip을 받아 작업 폴더에 풀고
        평가 선택지를 프런트엔드에 넘긴다는 점이다.
        """
        assert self.browser is not None

        workspace_context = self._share_workspace_if_needed(job)
        outgoing_prompt = build_outgoing_prompt(
            prompt,
            self.settings.enforce_filename_comments,
            workspace_context=workspace_context,
            workspace_sharing_enabled=self.settings.share_workspace,
        )

        baseline = self.browser.send_prompt(outgoing_prompt)

        # ── workspace 실시간 스트리밍 (완전히 새로 추가) ────────────
        # [1차 시도의 한계 - 왜 답변이 끝나야만 나타났는가]
        # 처음에는 "for ... in iter_response_stream(...)" 루프 몸통 안에서만
        # workspace를 다시 읽었다. 하지만 iter_response_stream은 답변 텍스트/
        # 코드가 실제로 바뀔 때만 값을 yield하고, 그 사이(예: LM Arena가
        # 화면에 아무 말도 추가하지 않은 채 한동안 조용히 파일만 만들고 있는
        # 구간)에는 제너레이터가 내부적으로 계속 돌고는 있어도 "for" 루프
        # 쪽으로는 전혀 제어가 넘어오지 않는다. 그래서 그 구간에는 workspace
        # 폴링 코드 자체가 실행되지 못했고, 결국 텍스트가 다시 바뀌는
        # 시점(대개 "완료했습니다" 같은 마지막 메시지)에야 한꺼번에 반영되어
        # "완료 후에야 나타나는" 것처럼 보였다.
        #
        # [해결 - on_tick]
        # browser.iter_response_stream()에 on_tick 콜백을 추가했다. 이 콜백은
        # 텍스트가 바뀌었는지와 무관하게, 제너레이터 내부 타이머가 도는 매
        # 순간(1단계는 0.1초, 2단계는 0.4초 간격)마다 항상 호출된다. 이
        # 콜백 안에서 시간 기반으로(WORKSPACE_POLL_INTERVAL) workspace를
        # 다시 읽고, 내용이 실제로 바뀌었을 때만 SSE로 즉시 내보낸다 - 이제는
        # 답변 텍스트가 조용한 구간에도 workspace만 따로 실시간으로 갱신된다.
        WORKSPACE_POLL_INTERVAL = 0.8
        last_tree_sig: Optional[tuple] = None
        last_tree_check = 0.0

        def _agent_workspace_tick() -> None:
            nonlocal last_tree_sig, last_tree_check
            now = time.time()
            if now - last_tree_check < WORKSPACE_POLL_INTERVAL:
                return
            last_tree_check = now
            entries = agent_mode.read_workspace_tree(self.browser.page)
            sig = tuple((e.kind, e.name, e.depth) for e in entries)
            if sig == last_tree_sig:
                return
            last_tree_sig = sig
            job.out.put({"type": "agent_workspace", "tree": [e.to_dict() for e in entries]})

        final_plain = ""
        final_blocks: list[CodeBlock] = []
        try:
            for plain_text, code_blocks, reasoning, done in self.browser.iter_response_stream(
                baseline_count=baseline,
                sent_prompt=outgoing_prompt,
                timeout_seconds=1800,
                on_tick=_agent_workspace_tick,
            ):
                final_plain = plain_text
                final_blocks = code_blocks
                job.out.put(
                    {
                        "type": "delta",
                        "plain_text": plain_text,
                        "code_blocks": [_code_block_to_dict(cb) for cb in code_blocks],
                        "reasoning": _reasoning_to_dict(reasoning),
                        "done": done,
                    }
                )
                self._check_stop(job)
        except GenerationStopped:
            pass
        except Exception as e:  # noqa: BLE001
            job.out.put({"type": "error", "message": str(e)})
            job.out.put(None)
            return

        parsed = self.browser.parse_last_response(
            baseline_count=baseline, sent_prompt=outgoing_prompt
        )

        # artifact 카드에서 읽은 코드도 Direct 모드와 동일하게 작업 폴더에
        # 반영해 둔다(카드 헤더의 파일명이 filename_hint로 들어와 정확한
        # 이름으로 저장된다). 프로젝트 최종본은 아래 workspace zip이 다시
        # 덮어쓰므로, 이건 진행 중에도 파일을 보고 싶을 때를 위한 것이다.
        written_files: list[str] = []
        if parsed.code_blocks and self.settings.auto_write_files and self.settings.target_path:
            try:
                paths = write_code_blocks(self.settings.target_path, parsed.code_blocks)
                written_files = [str(p) for p in paths]
            except Exception as e:  # noqa: BLE001
                job.out.put({"type": "agent_status", "message": f"파일 저장 중 오류: {e}"})

        # 스트리밍 도중 마지막으로 반영된 시점 이후 변화가 있었을 수 있으므로,
        # 답변이 끝난 시점에 한 번 더(강제로) 최종 상태를 읽어 맞춘다. 대화
        # 이력 복원(history)에도 이 최종 트리를 그대로 저장한다.
        try:
            final_tree_entries = agent_mode.read_workspace_tree(self.browser.page)
        except Exception:
            final_tree_entries = []
        tree = [e.to_dict() for e in final_tree_entries]
        final_sig = tuple((e.kind, e.name, e.depth) for e in final_tree_entries)
        if tree and final_sig != last_tree_sig:
            job.out.put({"type": "agent_workspace", "tree": tree})

        self._append_history(
            {
                "role": "assistant",
                "content": final_plain,
                "code_blocks": [_code_block_to_dict(cb) for cb in final_blocks],
                "written_files": written_files,
                "agent_tree": tree,
            }
        )
        job.out.put(
            {
                "type": "final",
                "plain_text": final_plain,
                "code_blocks": [_code_block_to_dict(cb) for cb in final_blocks],
                "written_files": written_files,
            }
        )

        # ── 프로젝트 종료 판정 ─────────────────────────────────────
        # LM Arena Agent는 프로젝트가 끝나면 "이 작업이 성공했습니까?"
        # 평가 패널을 띄운다. 이 패널이 곧 "프로젝트 종료" 신호이므로,
        # 이때 workspace 전체를 zip으로 받아 작업 폴더에 반영하고 평가
        # 선택지를 사용자에게 보여준다.
        finished = False
        try:
            finished = agent_mode.wait_for_review(self.browser.page, timeout_seconds=12.0)
        except Exception:
            finished = False

        if finished:
            if self.settings.agent_auto_download:
                job.out.put(
                    {"type": "agent_status", "message": "프로젝트가 끝났습니다. workspace를 내려받는 중..."}
                )
                job.out.put({"type": "agent_download", **self._agent_download_workspace()})
            try:
                options = agent_mode.read_review_options(self.browser.page)
            except Exception:
                options = []
            self._set_state(agent_review_pending=True)
            job.out.put(
                {
                    "type": "agent_review",
                    "question": "이 작업이 성공했습니까?",
                    "options": options,
                }
            )

        job.out.put(None)

    def _handle_work_prompt(self, job: Job, prompt: str) -> None:
        assert self.browser is not None
        job.out.put(
            {
                "type": "work_status",
                "message": "Work 모드: LM Arena가 이 PC를 조작합니다. 채팅 창은 건드리지 마세요.",
            }
        )

        workspace_context = None
        if self.settings.share_workspace and not self._workspace_shared_this_session:
            if self.settings.target_path:
                try:
                    snapshot = build_workspace_snapshot(self.settings.target_path)
                    workspace_context = format_workspace_context_block(snapshot)
                    self._workspace_shared_this_session = True
                    job.out.put(
                        {
                            "type": "workspace_shared",
                            "included_file_count": snapshot.included_file_count,
                        }
                    )
                except Exception as e:  # noqa: BLE001
                    job.out.put(
                        {
                            "type": "workspace_share_warning",
                            "message": f"작업 폴더를 읽는 중 오류가 발생해 이번 턴은 공유 없이 진행합니다: {e}",
                        }
                    )
            else:
                job.out.put(
                    {
                        "type": "workspace_share_warning",
                        "message": "작업 폴더가 설정되어 있지 않아 워크스페이스를 공유할 수 없습니다.",
                    }
                )

        def turn(outgoing_prompt: str):
            baseline = self.browser.send_prompt(outgoing_prompt)
            final_plain = ""
            final_blocks: list[CodeBlock] = []
            for plain_text, code_blocks, reasoning, done in self.browser.iter_response_stream(
                baseline_count=baseline, sent_prompt=outgoing_prompt, timeout_seconds=600
            ):
                final_plain = plain_text
                final_blocks = code_blocks
                job.out.put(
                    {
                        "type": "delta",
                        "plain_text": plain_text,
                        "code_blocks": [_code_block_to_dict(cb) for cb in code_blocks],
                        "reasoning": _reasoning_to_dict(reasoning),
                        "done": done,
                    }
                )
                # 중단 요청 시 GenerationStopped가 여기서 발생해 이 턴을
                # 즉시 빠져나가고, 그대로 run_work_task 밖(아래 except
                # GenerationStopped)까지 전파되어 Work 루프 전체가 멈춘다 -
                # "이번 턴만" 멈추고 다음 스텝으로 계속 진행되는 것을 방지한다.
                self._check_stop(job)
            parsed = self.browser.parse_last_response(
                baseline_count=baseline, sent_prompt=outgoing_prompt
            )
            self._append_history(
                {
                    "role": "assistant",
                    "content": final_plain,
                    "code_blocks": [_code_block_to_dict(cb) for cb in final_blocks],
                    "written_files": [],
                }
            )
            job.out.put(
                {
                    "type": "final",
                    "plain_text": final_plain,
                    "code_blocks": [_code_block_to_dict(cb) for cb in final_blocks],
                    "written_files": [],
                }
            )
            return parsed

        def emit(event: WorkEvent) -> None:
            job.out.put({"type": event.kind, **event.payload})

        try:
            run_work_task(
                user_prompt=prompt,
                enforce_filename_comments=self.settings.enforce_filename_comments,
                workspace_sharing_enabled=self.settings.share_workspace,
                workspace_context=workspace_context,
                target_path=self.settings.target_path,
                auto_write_files=self.settings.auto_write_files,
                turn=turn,
                emit=emit,
            )
        except GenerationStopped:
            # "stopped" 이벤트는 이미 _check_stop(turn 내부)에서 보냈다.
            # Work 에이전트 루프 전체를 여기서 종료시켜, 중단 이후 다음
            # 스텝으로 계속 진행되지 않게 한다.
            job.out.put({"type": "work_status", "message": "사용자 요청으로 작업을 중단했습니다."})
        except Exception as e:  # noqa: BLE001
            job.out.put({"type": "error", "message": str(e)})
        job.out.put(None)


# ── Flask 앱 ──────────────────────────────────────────────────────
def create_app(worker: AutomationWorker) -> Flask:
    app = Flask(__name__, static_folder=None)

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:filename>")
    def static_files(filename):
        return send_from_directory(STATIC_DIR, filename)

    @app.get("/api/state")
    def api_state():
        return jsonify(worker.get_state())

    @app.get("/api/history")
    def api_history():
        return jsonify(worker.get_history())

    @app.post("/api/target")
    def api_target():
        data = request.get_json(force=True, silent=True) or {}
        job = worker.submit("set_target", {"target_dir": data.get("target_dir", "")})
        return jsonify(job.out.get(timeout=30) or {})

    @app.post("/api/auto-write")
    def api_auto_write():
        data = request.get_json(force=True, silent=True) or {}
        job = worker.submit("set_auto_write", {"value": data.get("value", True)})
        return jsonify(job.out.get(timeout=30) or {})

    @app.post("/api/share-workspace")
    def api_share_workspace():
        # 기존 /api/auto-write와 동일한 패턴의, 완전히 새로운 엔드포인트.
        # 작업 폴더(target_dir) 전체를 LM Arena에게 컨텍스트로 공유할지
        # on/off 한다.
        data = request.get_json(force=True, silent=True) or {}
        job = worker.submit("set_share_workspace", {"value": data.get("value", False)})
        return jsonify(job.out.get(timeout=30) or {})

    @app.post("/api/chat-mode")
    def api_chat_mode():
        # Direct <-> Agent 모드 전환(완전히 새로운 엔드포인트).
        # URL 이동 + 대화 세션 재시작이 일어나므로 넉넉한 타임아웃을 둔다.
        data = request.get_json(force=True, silent=True) or {}
        job = worker.submit("set_chat_mode", {"mode": data.get("mode", "direct")})
        try:
            return jsonify(job.out.get(timeout=90) or {})
        except queue.Empty:
            return jsonify({"ok": False, "error": "모드를 바꾸는 시간이 초과되었습니다."}), 504

    @app.post("/api/agent-review")
    def api_agent_review():
        # "이 작업이 성공했습니까?" 평가 제출 (yes / no / continue / dismiss).
        data = request.get_json(force=True, silent=True) or {}
        job = worker.submit("agent_review", {"choice": data.get("choice", "")})
        try:
            return jsonify(job.out.get(timeout=60) or {})
        except queue.Empty:
            return jsonify({"ok": False, "error": "평가를 제출하는 시간이 초과되었습니다."}), 504

    @app.post("/api/agent-download")
    def api_agent_download():
        # workspace zip을 지금 즉시 내려받아 작업 폴더에 푼다(수동 실행용).
        job = worker.submit("agent_download")
        try:
            return jsonify(job.out.get(timeout=180) or {})
        except queue.Empty:
            return jsonify({"ok": False, "error": "workspace 다운로드 시간이 초과되었습니다."}), 504

    @app.get("/api/agent-workspace")
    def api_agent_workspace():
        job = worker.submit("agent_workspace")
        try:
            return jsonify(job.out.get(timeout=30) or {})
        except queue.Empty:
            return jsonify({"tree": []}), 504

    @app.post("/api/work-mode")
    def api_work_mode():
        data = request.get_json(force=True, silent=True) or {}
        job = worker.submit("set_work_mode", {"value": data.get("value", False)})
        return jsonify(job.out.get(timeout=30) or {})

    @app.post("/api/filename-enforcement")
    def api_filename_enforcement():
        # 기존 /api/auto-write와 동일한 패턴의, 완전히 새로운 엔드포인트.
        # (프런트엔드에서 아직 이 엔드포인트를 호출하지 않아도, /filename
        # CLI 명령이나 향후 UI 추가를 위해 백엔드 지원만 먼저 열어둔다.)
        data = request.get_json(force=True, silent=True) or {}
        job = worker.submit("set_filename_enforcement", {"value": data.get("value", True)})
        return jsonify(job.out.get(timeout=30) or {})

    @app.get("/api/models")
    def api_models():
        # 모델 목록. 캐시가 신선하면 즉시, 아니면(또는 ?refresh=1) LM Arena의 모델
        # 선택 창을 열어 새로 읽어온다(수 초 걸릴 수 있음).
        force = request.args.get("refresh", "") in ("1", "true")
        job = worker.submit("list_models", {"force": force})
        try:
            return jsonify(job.out.get(timeout=120) or {})
        except queue.Empty:
            return jsonify({"ok": False, "error": "모델 목록을 불러오는 시간이 초과되었습니다."}), 504

    @app.post("/api/model")
    def api_model():
        data = request.get_json(force=True, silent=True) or {}
        job = worker.submit("select_model", {"name": data.get("name", "")})
        try:
            return jsonify(job.out.get(timeout=90) or {})
        except queue.Empty:
            return jsonify({"ok": False, "error": "모델을 바꾸는 시간이 초과되었습니다."}), 504

    @app.get("/api/model-icon/<icon_id>")
    def api_model_icon(icon_id):
        # lab 아이콘 SVG. ID가 내용 해시라 영구 캐시해도 안전하다. <img>로만 쓰이지만,
        # 주소로 직접 열어도 스크립트가 실행되지 않도록 CSP를 붙인다.
        svg = worker.get_model_icon(icon_id) if re.fullmatch(r"[0-9a-f]{12}", icon_id) else None
        if not svg:
            return Response("", status=404)
        return Response(
            svg,
            mimetype="image/svg+xml",
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/new-chat")
    def api_new_chat():
        job = worker.submit("new_chat")
        return jsonify(job.out.get(timeout=30) or {})

    @app.post("/api/refresh-login")
    def api_refresh_login():
        job = worker.submit("refresh_login")
        return jsonify(job.out.get(timeout=30) or {})

    @app.post("/api/logout")
    def api_logout():
        job = worker.submit("logout")
        return jsonify(job.out.get(timeout=30) or {})

    @app.post("/api/stop")
    def api_stop():
        # send_prompt 잡을 처리 중인 워커 스레드는 스트리밍 루프에 갇혀
        # 있으므로 잡 큐를 거치지 않고 바로 플래그만 세운다(worker.
        # request_stop 참고). 그래서 이 핸들러는 워커 스레드를 기다리지
        # 않고 즉시 응답한다.
        worker.request_stop()
        return jsonify({"ok": True})

    @app.post("/api/send")
    def api_send():
        data = request.get_json(force=True, silent=True) or {}
        prompt = (data.get("prompt") or "").strip()
        if not prompt:
            return jsonify({"error": "빈 프롬프트입니다."}), 400

        job = worker.submit("send_prompt", {"prompt": prompt})

        def stream():
            while True:
                item = job.out.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

        return Response(
            stream(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def run_browser_ui(port: int = 8765, open_browser: bool = True) -> None:
    """
    웹 UI 백엔드(로컬 HTTP 서버)를 기동한다. 서버가 뜨면 사용자의 PC 기본
    브라우저(자동화에 쓰이는 Playwright 창이 아니라)에 새 탭으로 채팅 UI를
    연다. 이미 기본 브라우저가 열려 있다면 그 창에 새 탭으로 추가된다
    (webbrowser 모듈이 OS 기본 브라우저 핸들러에 위임하는 표준 동작).
    """
    worker = AutomationWorker()
    worker.start()
    app = create_app(worker)

    if open_browser:

        def _open_when_ready() -> None:
            time.sleep(0.8)
            webbrowser.open(f"http://127.0.0.1:{port}", new=2)

        threading.Thread(target=_open_when_ready, daemon=True).start()

    app.run(host="127.0.0.1", port=port, threaded=True)