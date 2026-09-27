"""
LM Arena CLI - 엔트리포인트

사용 예:
    lmarena                       # 설정 없이 바로 실행: 자동 로그인 + 폴더 선택 + 대화형 세션
    lmarena chat                  # 위와 동일 (명시적으로 대화형 세션 시작)
    lmarena run "프롬프트"         # 단발성 프롬프트 실행 (역시 자동 로그인 + 폴더 선택)
    lmarena init                  # 대상 폴더만 재설정하고 싶을 때 (평소엔 필요 없음)
    lmarena logout                # 저장된 브라우저 세션 삭제

대화형 세션 중 '/filename' 명령으로 "코드 블록 첫 줄에 파일명 주석을
남겨라"는 지침을 함께 전송할지 켜고 끌 수 있다 (기본값: 켜짐).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import ui
from . import __version__ as CLI_VERSION
from .config import (
    Settings,
    USER_DATA_DIR,
    CHAT_MODE_AGENT,
    CHAT_MODE_DIRECT,
    MODE_LABELS,
    normalize_chat_mode,
)
from . import agent_mode
from .browser import LMArenaBrowser
from .file_writer import write_code_blocks
from .prompt_augment import build_outgoing_prompt
from .work_agent import WorkEvent, run_work_task
from .workspace import build_workspace_snapshot, format_workspace_context_block


# ── 공통 부트스트랩 (로그인 확인/자동 로그인 + 폴더 선택) ────────────
def _resolve_target_dir(settings: Settings, override: str | None = None) -> Path:
    """
    작업 폴더를 결정한다.
    - override(예: `init --target`)가 주어지면 그대로 사용.
    - 없으면 마지막으로 사용한 폴더(settings.target_dir)를 기본값으로 제시하고,
      사용자가 Enter만 누르면 그 폴더가 그대로 기본 폴더로 사용된다.
    """
    raw = override or ui.ask_target_dir(default=settings.target_dir)

    target_path = Path(raw).expanduser().resolve()
    target_path.mkdir(parents=True, exist_ok=True)

    settings.target_dir = str(target_path)
    settings.last_used = datetime.now().isoformat(timespec="seconds")
    settings.save()
    return target_path


def _bootstrap(settings: Settings, target_override: str | None = None) -> tuple[LMArenaBrowser, Path]:
    """
    브라우저 시작 -> LM Arena Direct 모드 접속 -> 로그인 확인
    (로그아웃 상태면 자동으로 로그인 대기) -> 작업 폴더 선택
    (기본값 = 마지막 사용 폴더)까지 한 번에 처리하는 공통 진입 로직.

    'lmarena' 명령 하나만으로 별도 설정 없이 바로 쓸 수 있도록 하는 핵심 함수다.
    """
    browser = LMArenaBrowser(headless=settings.headless)

    with ui.boot_step("Playwright 초기화 중..."):
        browser.start()
    ui.success("Playwright 초기화 완료")

    # 마지막으로 사용하던 채팅 모드로 접속한다(기본값 direct = 기존 동작).
    startup_mode = normalize_chat_mode(settings.chat_mode)
    with ui.boot_step(f"LM Arena {MODE_LABELS[startup_mode]} 모드로 접속 중..."):
        if startup_mode == CHAT_MODE_AGENT:
            browser.goto_agent_mode()
        else:
            browser.goto_direct_mode()
    ui.success(f"LM Arena 접속 완료 ({MODE_LABELS[startup_mode]} 모드)")

    if browser.is_logged_in():
        ui.login_status(True)
    else:
        ui.login_status(False)
        ui.warn(
            "로그인 페이지로의 리다이렉트만으로는 성공으로 처리하지 않습니다. "
            "실제 Direct 모드 화면(입력창)이 나타나야 완료됩니다."
        )
        # LM Arena 사이트 화면이 그대로 노출되지 않도록, 로그인 버튼을
        # 자동으로 눌러 로그인 창(팝업/모달)만 보이게 한다. 로그인 버튼을
        # 찾지 못했다면(False) 기존과 동일하게 wait_for_login()만으로
        # 진행한다 - 화면 표시만 바뀔 뿐, 로그인 판정 로직 자체는 동일하다.
        browser.enter_login_only_view()
        try:
            if not browser.wait_for_login(timeout_seconds=600):
                ui.error("로그인 대기 시간이 초과되었습니다. 다시 시도해주세요.")
                browser.close()
                sys.exit(1)
        finally:
            browser.exit_login_only_view()
        ui.success("로그인 성공! 세션이 저장되었습니다.")

    # 로그인 완료(또는 이미 로그인된 상태) 이후로는 자동화 창에 실제 LM
    # Arena 화면 대신 직접 디자인한 "로그인 완료" 커스텀 화면만 보이게
    # 한다. send_prompt 등 실제 DOM 통신 로직에는 영향을 주지 않는다.
    browser.show_connected_overlay()

    target_path = _resolve_target_dir(settings, override=target_override)
    ui.session_header(str(target_path))

    return browser, target_path


# ── 서브커맨드 ────────────────────────────────────────────────────
def cmd_init(args: argparse.Namespace) -> None:
    """대상 폴더만 재설정하고 싶을 때 쓰는 명시적 커맨드 (평소엔 필요 없음)."""
    ui.banner()
    settings = Settings.load()
    browser, target_path = _bootstrap(settings, target_override=args.target)
    browser.close()
    ui.success(f"설정 완료: {target_path}")


def cmd_browser_ui(args: argparse.Namespace) -> None:
    """
    'lmarena browser' 전용 진입점 (신규 추가).

    CLI 화면을 그리는 대신, 로컬 웹 서버를 띄우고 PC의 기본 브라우저에
    새 탭으로 채팅 UI를 연다. 로그인/전송/스트리밍/코드 저장 등 실제 동작
    로직은 기존 LMArenaBrowser(browser.py)를 그대로 재사용하며(webapp.py
    참고), 이 함수는 그 위에 새로운 실행 경로만 추가한다. 기존 cmd_chat /
    cmd_run 등 다른 커맨드의 동작에는 전혀 영향을 주지 않는다.
    """
    # 지연 임포트: Flask는 이 커맨드를 쓸 때만 필요하므로, 순수 CLI 모드만
    # 쓰는 사용자가 불필요한 의존성 임포트 비용을 겪지 않도록 한다.
    from . import webapp

    print(f"LM Arena 웹 UI를 준비하고 있습니다... (http://127.0.0.1:{args.port})")
    print("종료하려면 이 터미널에서 Ctrl+C 를 누르세요.")
    try:
        webapp.run_browser_ui(port=args.port, open_browser=not args.no_open)
    except KeyboardInterrupt:
        pass


def cmd_logout(args: argparse.Namespace) -> None:
    if USER_DATA_DIR.exists():
        shutil.rmtree(USER_DATA_DIR, ignore_errors=True)
        ui.success("저장된 브라우저 세션(쿠키)을 삭제했습니다.")
    else:
        ui.info("삭제할 세션이 없습니다.")


def _new_session_state() -> dict:
    """
    '/new'로 새 채팅을 시작하거나, 워크스페이스 공유를 새로 켜거나, 작업
    폴더를 바꿀 때마다 리셋되는 런타임 전용 상태. Settings와 달리 디스크에
    저장되지 않는다 - "이번 LM Arena 대화 세션에 워크스페이스를 이미
    보냈는지"는 재실행할 때마다 다시 초기화되어야 하기 때문이다.
    """
    return {"workspace_shared": False}


def _build_workspace_context_or_warn(settings: Settings) -> str | None:
    """
    워크스페이스 공유가 켜져 있고 아직 이번 세션에 보내지 않았을 때, 작업
    폴더를 스캔해 컨텍스트 블록 문자열을 만든다. 폴더가 설정되지 않았거나
    스캔 중 오류가 나도 전체 대화 흐름(프롬프트 전송)은 막지 않고, 경고만
    보여준 뒤 이번 턴은 공유 없이 진행한다.
    """
    if not settings.target_path:
        ui.warn(
            "작업 폴더가 설정되어 있지 않아 워크스페이스를 공유할 수 없습니다. "
            "'/target'으로 폴더를 먼저 지정하세요."
        )
        return None
    try:
        with ui.boot_step("작업 폴더를 읽는 중..."):
            snapshot = build_workspace_snapshot(settings.target_path)
        ui.success(f"작업 폴더를 공유했습니다 (파일 {snapshot.included_file_count}개 내용 포함).")
        return format_workspace_context_block(snapshot)
    except Exception as e:  # noqa: BLE001
        ui.warn(f"작업 폴더를 읽는 중 오류가 발생해 이번 턴은 공유 없이 진행합니다: {e}")
        return None


def _handle_one_turn(
    browser: LMArenaBrowser, prompt: str, settings: Settings, session_state: dict
) -> None:
    ui.user_prompt_echo(prompt)

    # 워크스페이스 공유(완전히 새로 추가된 기능): 켜져 있고 이번 세션에
    # 아직 보내지 않았다면 지금 딱 한 번만 폴더 전체를 스캔해서 함께
    # 보낸다. 이후 턴에는 workspace_context를 None으로 넘겨 다시 보내지
    # 않는다(경로 지정 규칙만은 매 턴 함께 전달됨 - prompt_augment.py 참고).
    workspace_context = None
    if settings.share_workspace and not session_state.get("workspace_shared"):
        workspace_context = _build_workspace_context_or_warn(settings)
        if workspace_context is not None:
            session_state["workspace_shared"] = True

    # LM Arena에는 API 같은 별도의 system prompt 채널이 없으므로,
    # 파일명 자동 표기 지침이 켜져 있으면(기본값) 실제로 전송할 문자열
    # 앞에 그 지침 블록을 덧붙인다(prompt_augment.py 참고). 화면에는
    # 방금 위에서 원본 prompt를 그대로 에코했으므로 사용자 눈에는 아무
    # 변화가 없다. 이후 send_prompt / iter_response_stream /
    # parse_last_response에는 반드시 이 outgoing_prompt를 동일하게
    # 넘겨야 에코(내가 보낸 메시지) 판별이 정확하게 유지된다.
    outgoing_prompt = build_outgoing_prompt(
        prompt,
        settings.enforce_filename_comments,
        workspace_context=workspace_context,
        workspace_sharing_enabled=settings.share_workspace,
    )

    # 전송 '직전' 답변 개수(텍스트 스냅샷)를 기준선으로 저장 -> 이후 새로 생긴
    # 컨테이너만 답변으로 취급해, 방금 보낸 내 프롬프트를 답변으로 오인하지 않는다.
    baseline_count = browser.send_prompt(outgoing_prompt)

    # 일반 텍스트와 코드 블록을 모두 생성되는 대로 실시간으로 보여준다.
    final_text = ""
    completed = False
    with ui.stream_panel() as update:
        for plain_text, code_blocks, reasoning, done in browser.iter_response_stream(
            baseline_count=baseline_count, sent_prompt=outgoing_prompt, timeout_seconds=600
        ):
            final_text = plain_text
            update(plain_text, code_blocks, done=done, reasoning=reasoning)
            if done:
                completed = True

    if not completed:
        ui.warn("응답 완료 감지 시간이 초과되었습니다. 현재까지의 내용을 표시합니다.")

    if final_text.strip():
        ui.assistant_text(final_text)

    # 답변이 완전히 끝난 뒤에만, 최종 DOM 기준으로 코드 블록을 다시 정확하게
    # 추출해서 파일을 쓰거나 기존 파일을 수정한다 (기존 로직/함수 그대로).
    parsed = browser.parse_last_response(baseline_count=baseline_count, sent_prompt=outgoing_prompt)

    if parsed.code_blocks:
        if settings.auto_write_files and settings.target_path:
            write_code_blocks(settings.target_path, parsed.code_blocks)
        else:
            for cb in parsed.code_blocks:
                ui.assistant_code_block(cb.language, "(미저장)", cb.code)

    if not final_text.strip() and not parsed.code_blocks:
        ui.warn("응답 내용을 감지하지 못했습니다 (DOM 구조가 변경되었을 수 있습니다).")


def _handle_work_task(
    browser: LMArenaBrowser, prompt: str, settings: Settings, session_state: dict
) -> None:
    """
    Work 모드 전용 경로. 기존 _handle_one_turn은 그대로 두고, 켜져 있을
    때만 이 함수가 에이전트 루프를 돈다. send_prompt / iter_response_stream
    / parse_last_response 계약은 기존과 동일하다.
    """
    ui.user_prompt_echo(prompt)
    ui.work_note("Work 모드: LM Arena가 이 PC를 조작합니다. 채팅 창은 건드리지 마세요.")

    workspace_context = None
    if settings.share_workspace and not session_state.get("workspace_shared"):
        workspace_context = _build_workspace_context_or_warn(settings)
        if workspace_context is not None:
            session_state["workspace_shared"] = True

    def turn(outgoing_prompt: str):
        baseline_count = browser.send_prompt(outgoing_prompt)
        final_text = ""
        completed = False
        with ui.stream_panel() as update:
            for plain_text, code_blocks, reasoning, done in browser.iter_response_stream(
                baseline_count=baseline_count, sent_prompt=outgoing_prompt, timeout_seconds=600
            ):
                final_text = plain_text
                update(plain_text, code_blocks, done=done, reasoning=reasoning)
                if done:
                    completed = True
        if not completed:
            ui.warn("응답 완료 감지 시간이 초과되었습니다. 현재까지의 내용을 표시합니다.")
        if final_text.strip():
            ui.assistant_text(final_text)
        return browser.parse_last_response(
            baseline_count=baseline_count, sent_prompt=outgoing_prompt
        )

    def emit(event: WorkEvent) -> None:
        if event.kind in ("work_status", "work_done"):
            ui.work_note(event.payload.get("message") or event.payload.get("summary") or "")
        elif event.kind == "work_action":
            mark = "ok" if event.payload.get("ok") else "fail"
            ui.work_note(f"{mark} {event.payload.get('op')} — {event.payload.get('detail')}")

    run_work_task(
        user_prompt=prompt,
        enforce_filename_comments=settings.enforce_filename_comments,
        workspace_sharing_enabled=settings.share_workspace,
        workspace_context=workspace_context,
        target_path=settings.target_path,
        auto_write_files=settings.auto_write_files,
        turn=turn,
        emit=emit,
    )


def _handle_agent_turn(
    browser: LMArenaBrowser, prompt: str, settings: Settings, session_state: dict
) -> None:
    """
    Agent 모드 전용 경로 (완전히 새로 추가).

    전송/스트리밍/완료 판정은 Direct 모드와 완전히 동일한 기존 경로를 그대로
    쓴다(일반 답변/추론 DOM이 두 모드에서 같기 때문). 달라지는 것은 코드
    블록이 artifact 카드 DOM으로 파싱된다는 점(browser 쪽이 모드를 보고
    알아서 고른다)과, 프로젝트가 끝나면(=평가 패널이 뜨면) workspace zip을
    받아 작업 폴더에 풀고 평가를 남기도록 안내한다는 점뿐이다.
    """
    ui.user_prompt_echo(prompt)

    workspace_context = None
    if settings.share_workspace and not session_state.get("workspace_shared"):
        workspace_context = _build_workspace_context_or_warn(settings)
        if workspace_context is not None:
            session_state["workspace_shared"] = True

    outgoing_prompt = build_outgoing_prompt(
        prompt,
        settings.enforce_filename_comments,
        workspace_context=workspace_context,
        workspace_sharing_enabled=settings.share_workspace,
    )

    baseline_count = browser.send_prompt(outgoing_prompt)

    final_text = ""
    completed = False
    with ui.stream_panel() as update:
        for plain_text, code_blocks, reasoning, done in browser.iter_response_stream(
            baseline_count=baseline_count, sent_prompt=outgoing_prompt, timeout_seconds=1800
        ):
            final_text = plain_text
            update(plain_text, code_blocks, done=done, reasoning=reasoning)
            if done:
                completed = True

    if not completed:
        ui.warn("응답 완료 감지 시간이 초과되었습니다. 현재까지의 내용을 표시합니다.")
    if final_text.strip():
        ui.assistant_text(final_text)

    parsed = browser.parse_last_response(
        baseline_count=baseline_count, sent_prompt=outgoing_prompt
    )
    if parsed.code_blocks:
        if settings.auto_write_files and settings.target_path:
            write_code_blocks(settings.target_path, parsed.code_blocks)
        else:
            for cb in parsed.code_blocks:
                ui.assistant_code_block(cb.language, cb.filename_hint or "(미저장)", cb.code)

    # LM Arena workspace 패널의 폴더/파일 트리를 그대로 보여준다.
    tree = agent_mode.read_workspace_tree(browser.page)
    if tree:
        lines = [
            ("  " * entry.depth) + ("- " + entry.name + ("/" if entry.kind == "dir" else ""))
            for entry in tree
        ]
        ui.info("workspace:\n" + "\n".join(lines))

    # 프로젝트 종료 판정: 평가 패널이 뜨면 끝난 것으로 본다.
    if not agent_mode.wait_for_review(browser.page, timeout_seconds=12.0):
        return

    ui.success("프로젝트가 끝났습니다.")
    if settings.agent_auto_download:
        if settings.target_path is None:
            ui.warn("작업 폴더가 설정되어 있지 않아 workspace를 저장하지 못했습니다 ('/target'으로 지정하세요).")
        else:
            with ui.boot_step("workspace를 내려받아 압축을 푸는 중..."):
                result = agent_mode.download_workspace(
                    browser.page,
                    settings.target_path,
                    strip_root=settings.agent_strip_root_folder,
                )
            if result.ok:
                ui.success(
                    f"workspace를 작업 폴더에 반영했습니다 ({result.archive_name}) - 파일 {len(result.files)}개"
                )
            else:
                ui.warn(result.error or "workspace를 내려받지 못했습니다.")

    options = agent_mode.read_review_options(browser.page)
    if options:
        labels = " / ".join(f"{o['label']}({o['choice']})" for o in options)
        ui.info(f"이 작업이 성공했습니까? {labels}")
    ui.info("'/review yes', '/review no', '/review continue' 로 평가를 남겨주세요.")


# ── 슬래시 커맨드 처리 ──────────────────────────────────────────────
# Claude Code / Gemini CLI 스타일의 "/" 명령어들. 기존 대화형 루프
# (cmd_chat)의 기존 분기(빈 입력, exit/quit)는 그대로 두고, 그 뒤에만
# 새로 추가한다. 명령어 목록/설명은 ui.SLASH_COMMANDS 하나에서만
# 관리하며(자동완성과 /help가 같은 소스를 공유), 여기서는 실제 동작만
# 담당한다.
def _handle_slash_command(
    raw: str, browser: LMArenaBrowser, settings: Settings, session_state: dict
) -> str:
    """
    반환값:
      - "break"    : 대화형 세션을 종료해야 함 (cmd_chat의 while 루프 탈출)
      - "continue" : 처리 완료, 다음 턴으로 넘어감
    """
    parts = raw.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/exit", "/quit"):
        return "break"

    if cmd == "/help":
        ui.print_help()
        return "continue"

    if cmd == "/new":
        with ui.boot_step("새 채팅 세션 시작 중..."):
            ok = browser.new_chat()
        if ok:
            # 새 LM Arena 대화 세션이 시작됐으므로, 워크스페이스도 "새
            # 세션"으로 취급해 다음 메시지에서 다시 한 번 공유되게 한다.
            session_state["workspace_shared"] = False
            ui.success("새로운 채팅 세션을 시작했습니다.")
        else:
            ui.warn("'New Chat' 버튼을 찾을 수 없습니다 (DOM 변경 가능성).")
        return "continue"

    if cmd == "/clear":
        ui.clear_screen()
        ui.success("이전 대화 UI를 화면에서 치웠습니다 (배너/세션은 스크롤백에 남아있고, 브라우저 세션·대화 기록은 그대로 유지됩니다).")
        return "continue"

    if cmd == "/status":
        ui.login_status(browser.is_logged_in())
        return "continue"

    if cmd == "/pwd":
        ui.info(f"현재 작업 폴더: {settings.target_dir or '(미설정)'}")
        return "continue"

    if cmd == "/target":
        target_path = _resolve_target_dir(settings, override=(arg or None))
        # 작업 폴더(=워크스페이스 공유 대상 폴더이기도 함)가 바뀌었으므로,
        # 다음 메시지에서 새 폴더 기준으로 다시 한 번 공유되게 한다.
        session_state["workspace_shared"] = False
        ui.success(f"작업 폴더를 변경했습니다: {target_path}")
        return "continue"

    if cmd == "/logout":
        if USER_DATA_DIR.exists():
            shutil.rmtree(USER_DATA_DIR, ignore_errors=True)
            ui.success("저장된 브라우저 세션(쿠키)을 삭제했습니다. (다음 실행 시 재로그인 필요)")
        else:
            ui.info("삭제할 세션이 없습니다.")
        return "continue"

    if cmd == "/version":
        ui.info(f"lm-arena-cli v{CLI_VERSION}")
        return "continue"

    if cmd == "/filename":
        # 파일명 자동 표기 지침 전송 on/off. 인자 없이 입력하면 토글된다.
        normalized_arg = arg.strip().lower()
        if normalized_arg in ("on", "true", "1", "켜기", "켬"):
            settings.enforce_filename_comments = True
        elif normalized_arg in ("off", "false", "0", "끄기", "끔"):
            settings.enforce_filename_comments = False
        else:
            settings.enforce_filename_comments = not settings.enforce_filename_comments
        settings.save()
        state_label = "켜짐 (파일명 주석 지침을 함께 전송합니다)" if settings.enforce_filename_comments else "꺼짐 (지침 없이 원문 그대로 전송합니다)"
        ui.success(f"파일명 자동 표기 지침: {state_label}")
        return "continue"

    if cmd == "/workspace":
        # 작업 폴더(target_dir) 전체를 LM Arena에게 컨텍스트로 공유할지
        # on/off. 인자 없이 입력하면 토글된다 ('/filename'과 동일한 패턴).
        normalized_arg = arg.strip().lower()
        was_enabled = settings.share_workspace
        if normalized_arg in ("on", "true", "1", "켜기", "켬"):
            settings.share_workspace = True
        elif normalized_arg in ("off", "false", "0", "끄기", "끔"):
            settings.share_workspace = False
        else:
            settings.share_workspace = not settings.share_workspace
        settings.save()

        if settings.share_workspace and not was_enabled:
            # 꺼져 있다가 새로 켜졌으니, 다음 메시지에서 처음부터 새로
            # 공유되게 한다.
            session_state["workspace_shared"] = False

        if settings.share_workspace:
            state_label = "켜짐 (다음 메시지부터 작업 폴더 전체를 컨텍스트로 공유합니다)"
        else:
            state_label = "꺼짐"
        ui.success(f"워크스페이스 공유: {state_label}")
        return "continue"

    if cmd == "/work":
        normalized_arg = arg.strip().lower()
        if normalized_arg in ("on", "true", "1", "켜기", "켬"):
            settings.work_mode = True
        elif normalized_arg in ("off", "false", "0", "끄기", "끔"):
            settings.work_mode = False
        else:
            settings.work_mode = not settings.work_mode
        settings.save()
        if settings.work_mode:
            state_label = "켜짐 (다음 메시지부터 LM Arena가 이 PC를 직접 조작합니다)"
        else:
            state_label = "꺼짐"
        ui.success(f"Work 모드: {state_label}")
        return "continue"

    if cmd == "/mode":
        # 채팅 모드 전환 (direct <-> agent). 인자 없이 입력하면 토글된다.
        normalized_arg = arg.strip().lower()
        current = normalize_chat_mode(settings.chat_mode)
        if normalized_arg in ("agent", "a"):
            target = CHAT_MODE_AGENT
        elif normalized_arg in ("direct", "d", "chat"):
            target = CHAT_MODE_DIRECT
        elif not normalized_arg:
            target = CHAT_MODE_DIRECT if current == CHAT_MODE_AGENT else CHAT_MODE_AGENT
        else:
            ui.warn("모드는 'direct' 또는 'agent' 만 사용할 수 있습니다 (예: /mode agent)")
            return "continue"

        if target == current and browser.chat_mode == target:
            ui.info(f"이미 {MODE_LABELS[target]} 모드입니다.")
            return "continue"

        with ui.boot_step(f"{MODE_LABELS[target]} 모드로 전환 중..."):
            actual = browser.goto_mode(target)
        settings.chat_mode = actual
        settings.save()
        # 모드를 바꾸면 LM Arena 대화도 새로 시작되므로, 새 채팅과 동일하게
        # 워크스페이스 공유 상태를 초기화한다.
        session_state["workspace_shared"] = False
        if actual == CHAT_MODE_AGENT:
            ui.success("Agent 모드로 전환했습니다 (모델 직접 선택은 사용할 수 없습니다).")
        else:
            ui.success("Direct 모드로 전환했습니다.")
        return "continue"

    if cmd == "/review":
        # Agent 프로젝트 평가 제출 (yes / no / continue).
        if normalize_chat_mode(settings.chat_mode) != CHAT_MODE_AGENT:
            ui.warn("평가는 Agent 모드에서만 사용할 수 있습니다 ('/mode agent').")
            return "continue"
        choice_map = {
            "yes": "yes", "y": "yes", "예": "yes",
            "no": "no", "n": "no", "아니요": "no", "아니오": "no",
            "continue": "continue", "c": "continue", "계속": "continue",
            "close": "dismiss", "dismiss": "dismiss",
        }
        choice = choice_map.get(arg.strip().lower())
        if not choice:
            ui.warn("사용법: /review yes | no | continue  (닫기만 하려면 /review close)")
            return "continue"
        if choice == "dismiss":
            ok = agent_mode.close_review(browser.page)
            error = None if ok else "평가 패널을 닫지 못했습니다."
        else:
            ok, error = agent_mode.submit_review(browser.page, choice)
        if ok:
            ui.success(f"평가를 제출했습니다: {choice}")
        else:
            ui.warn(error or "평가를 제출하지 못했습니다.")
        return "continue"

    if cmd == "/download":
        # Agent workspace를 지금 즉시 받아 작업 폴더에 푼다.
        if normalize_chat_mode(settings.chat_mode) != CHAT_MODE_AGENT:
            ui.warn("workspace 다운로드는 Agent 모드에서만 사용할 수 있습니다 ('/mode agent').")
            return "continue"
        if settings.target_path is None:
            ui.warn("작업 폴더가 설정되어 있지 않습니다 ('/target'으로 먼저 지정하세요).")
            return "continue"
        with ui.boot_step("workspace를 내려받아 압축을 푸는 중..."):
            result = agent_mode.download_workspace(
                browser.page, settings.target_path, strip_root=settings.agent_strip_root_folder
            )
        if result.ok:
            ui.success(f"workspace를 작업 폴더에 반영했습니다 - 파일 {len(result.files)}개")
        else:
            ui.warn(result.error or "workspace를 내려받지 못했습니다.")
        return "continue"

    ui.warn(f"알 수 없는 명령어입니다: {cmd}  ('/help'로 사용 가능한 명령어를 확인하세요)")
    return "continue"


def cmd_run(args: argparse.Namespace) -> None:
    ui.banner()
    settings = Settings.load()
    browser, _ = _bootstrap(settings)
    session_state = _new_session_state()
    try:
        if normalize_chat_mode(settings.chat_mode) == CHAT_MODE_AGENT:
            _handle_agent_turn(browser, args.prompt, settings, session_state)
        elif settings.work_mode:
            _handle_work_task(browser, args.prompt, settings, session_state)
        else:
            _handle_one_turn(browser, args.prompt, settings, session_state)
    finally:
        browser.close()


def cmd_chat(args: argparse.Namespace) -> None:
    """
    기본 진입점. 별도 설정 없이 `lmarena`만 실행해도 이 함수가 호출된다.
    자동 로그인 확인 -> 작업 폴더 선택(기본값 = 마지막 폴더) -> 대화형 세션 순으로 진행한다.
    """
    ui.banner()
    settings = Settings.load()
    browser, _ = _bootstrap(settings)
    ui.info("종료하려면 'exit' 또는 'quit' 입력, Ctrl+C 로도 종료됩니다.")
    ui.info("'/help'를 입력하면 사용 가능한 명령어를 볼 수 있습니다 ('/'만 입력해도 자동완성됩니다).")

    session_state = _new_session_state()
    try:
        while True:
            # ── 입력창 박스 UI (항상 렌더링) ─────────────────────────
            prompt = ui.prompt_input()

            # 종료 신호 처리
            if prompt == "__EXIT__":
                break

            if not prompt.strip():
                continue
            if prompt.strip().lower() in {"exit", "quit"}:
                break

            # "/"로 시작하면 LM Arena로 전송하지 않고 로컬 슬래시 커맨드로
            # 처리한다. 기존 일반 프롬프트 흐름(_handle_one_turn)은 전혀
            # 건드리지 않고, 그 앞단에서만 분기한다.
            if prompt.strip().startswith("/"):
                ui.user_prompt_echo(prompt)
                try:
                    action = _handle_slash_command(prompt, browser, settings, session_state)
                except Exception as e:  # noqa: BLE001
                    ui.error(f"명령어 처리 중 오류 발생: {e}")
                    action = "continue"
                ui.divider()
                if action == "break":
                    break
                continue

            try:
                if normalize_chat_mode(settings.chat_mode) == CHAT_MODE_AGENT:
                    # Agent 모드가 Work 모드보다 우선한다 - Agent는 LM Arena
                    # 자체 workspace에서 작업하므로 이 PC를 조작하지 않는다.
                    _handle_agent_turn(browser, prompt, settings, session_state)
                elif settings.work_mode:
                    _handle_work_task(browser, prompt, settings, session_state)
                else:
                    _handle_one_turn(browser, prompt, settings, session_state)
            except Exception as e:  # noqa: BLE001
                ui.error(f"오류 발생: {e}")

            ui.divider()
    finally:
        browser.close()
        ui.success("세션을 종료했습니다.")


# ── 파서 / 메인 ───────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lmarena", description="LM Arena CLI")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser(
        "init", help="대상 폴더만 재설정 (평소엔 필요 없음 - 'lmarena'만 실행해도 자동으로 물어봅니다)"
    )
    p_init.add_argument("--target", "-t", help="LM Arena가 파일을 쓸 폴더 경로")
    p_init.set_defaults(func=cmd_init)

    p_run = sub.add_parser("run", help="단발성 프롬프트 실행")
    p_run.add_argument("prompt", help="LM Arena에 전달할 프롬프트")
    p_run.set_defaults(func=cmd_run)

    p_chat = sub.add_parser("chat", help="대화형 세션 시작 ('lmarena'만 입력한 것과 동일)")
    p_chat.set_defaults(func=cmd_chat)

    p_logout = sub.add_parser("logout", help="저장된 브라우저 세션 삭제")
    p_logout.set_defaults(func=cmd_logout)

    p_browser = sub.add_parser(
        "browser", help="브라우저(웹) UI로 실행 - PC 기본 브라우저에서 명령어 없이 모든 조작 가능"
    )
    p_browser.add_argument("--port", type=int, default=8765, help="로컬 웹 서버 포트 (기본값: 8765)")
    p_browser.add_argument(
        "--no-open", action="store_true", help="기본 브라우저 탭을 자동으로 열지 않음"
    )
    p_browser.set_defaults(func=cmd_browser_ui)

    # 서브커맨드 없이 'lmarena'만 입력해도 대화형 세션이 바로 시작되도록
    # 기본 핸들러를 지정한다. (=> 설정 없이 바로 실행 가능)
    parser.set_defaults(func=cmd_chat)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()