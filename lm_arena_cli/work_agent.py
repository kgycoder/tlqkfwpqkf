"""
LM Arena Work 모드 - 에이전트 루프 본체

한 번의 사용자 명령을 여러 Arena 턴으로 돌린다.
  1) 현재 화면/창/UI 트리를 텍스트로 관측
  2) 관측 + 명령을 LM Arena에 전송 (기존 browser.send_prompt 경로)
  3) 응답의 work JSON을 실행
  4) done 이거나 더 이상 액션이 없으면 종료, 아니면 관측을 다시 전송

전송/스트리밍/파싱은 호출부가 기존 LMArenaBrowser 메서드로 수행한다.
이 모듈은 그 위에 실행·관측만 얹는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .browser import ParsedResponse
from .computer import Computer
from .file_writer import write_code_blocks
from .work_protocol import parse_actions, partition_blocks, wrap_first_turn, wrap_followup_turn

MAX_STEPS = 28
MAX_ACTIONS_PER_TURN = 16


@dataclass
class WorkEvent:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


TurnFn = Callable[[str], ParsedResponse]
EventFn = Callable[[WorkEvent], None]


@dataclass
class WorkRunResult:
    steps: int
    done: bool
    summary: str


def run_work_task(
    *,
    user_prompt: str,
    enforce_filename_comments: bool,
    workspace_sharing_enabled: bool,
    workspace_context: str | None,
    target_path,
    auto_write_files: bool,
    turn: TurnFn,
    emit: Optional[EventFn] = None,
    computer: Optional[Computer] = None,
) -> WorkRunResult:
    pc = computer or Computer()
    emit = emit or (lambda _e: None)

    emit(WorkEvent("work_status", {"message": "화면과 창 상태를 읽는 중..."}))
    observation = pc.observe_text()
    outgoing = wrap_first_turn(
        user_prompt,
        enforce_filename_comments,
        workspace_context,
        workspace_sharing_enabled,
        observation,
    )

    done = False
    summary = ""
    steps = 0

    for steps in range(1, MAX_STEPS + 1):
        emit(WorkEvent("work_turn", {"step": steps, "max": MAX_STEPS}))
        parsed = turn(outgoing)
        work_blocks, other_blocks = partition_blocks(parsed.code_blocks)

        if other_blocks and auto_write_files and target_path:
            write_code_blocks(target_path, other_blocks)

        actions, parse_err = parse_actions(work_blocks, parsed.plain_text)
        if parse_err and not actions:
            emit(WorkEvent("work_status", {"message": parse_err}))
            observation = f"PARSE_ERROR: {parse_err}\nLast observation:\n{pc.observe_text()}"
            outgoing = wrap_followup_turn(
                user_prompt, observation, enforce_filename_comments, workspace_sharing_enabled
            )
            continue

        if not actions:
            # 모델이 일반 답변만 한 경우: 사용자 확인/완료로 보고 종료
            emit(WorkEvent("work_status", {"message": "Work 액션이 없어 이번 명령을 종료합니다."}))
            summary = parsed.plain_text.strip()[:500]
            break

        logs: list[str] = []
        stop_after_observe = False
        for raw in actions[:MAX_ACTIONS_PER_TURN]:
            op = str(raw.get("op") or raw.get("action") or "").lower()
            if op == "done":
                done = True
                summary = str(raw.get("summary") or parsed.plain_text.strip()[:500])
                emit(WorkEvent("work_action", {"op": "done", "ok": True, "detail": summary}))
                break
            if op in {"observe", "screenshot", "look"}:
                stop_after_observe = True
            try:
                detail = pc.perform(raw)
                ok = True
            except Exception as e:  # noqa: BLE001
                detail = str(e)
                ok = False
            logs.append(("OK" if ok else "FAIL") + f" {op}: {detail}")
            emit(WorkEvent("work_action", {"op": op, "ok": ok, "detail": _short(detail)}))
            if not ok and op in {"click", "type", "key", "shell"}:
                # 한 액션 실패 후에도 관측은 보내 모델이 복구하게 한다.
                pass
            if stop_after_observe and op in {"observe", "screenshot", "look"}:
                # observe만 있으면 나머지는 다음 턴에서.
                if len(actions) == 1:
                    break

        if done:
            break

        observation = "action_log:\n" + "\n".join(logs) + "\n\n" + pc.observe_text()
        emit(WorkEvent("work_status", {"message": f"Work {steps}/{MAX_STEPS} 관측을 다시 전송합니다."}))
        outgoing = wrap_followup_turn(
            user_prompt, observation, enforce_filename_comments, workspace_sharing_enabled
        )
    else:
        emit(WorkEvent("work_status", {"message": f"최대 스텝({MAX_STEPS})에 도달해 중단합니다."}))

    emit(WorkEvent("work_done", {"steps": steps, "done": done, "summary": summary}))
    return WorkRunResult(steps=steps, done=done, summary=summary)


def _short(text: str, n: int = 240) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 3] + "..."
