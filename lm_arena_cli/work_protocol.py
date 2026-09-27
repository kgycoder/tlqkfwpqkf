"""
LM Arena Work 모드 - 코드 블록 프로토콜

LM Arena는 도구 연동이 없고 일반 답변/코드 블록만 가능하다. 그래서
모델에게 "```work JSON" 코드 블록으로 컴퓨터 조작을 적게 한 뒤, 로컬에서
그 블록을 파싱·실행하고 관측 결과를 다음 사용자 메시지로 다시 보내는
방식으로 도구 호출을 우회한다.

지침은 기존 prompt_augment.build_outgoing_prompt() 결과를 감싸기만 하며
(파일명/워크스페이스 규칙은 그대로 유지) 그 함수 자체는 수정하지 않는다.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

from .browser import CodeBlock
from .prompt_augment import build_outgoing_prompt

WORK_LANGUAGES = {"work", "workjson", "actions", "computer", "lma-work"}

# 지침은 한 블록. 기존 filename/workspace System instruction 앞에 붙인다.
WORK_RULE = (
    "System instruction (work mode): you operate the user's Windows PC. "
    "If executing a user's command requires additional information or a search,"
    "that information must be gathered in the background,"
    "and only the organized final result should be displayed to the user;"
    "the information gathering and search processes must not be visible on the user's screen."
    "Please provide a brief description of the current task (one line or less)."
    "Do not leave traces of your work on the user's computer."
    "The work must proceed normally even while the user is performing other tasks on the PC,"
    "and it must not interfere with the user's activities."
    "Windows PC yourself. You cannot call APIs or tools; the local program "
    "executes only JSON inside a markdown code block whose language tag is "
    "exactly work. After each reply the program runs those actions and sends "
    "you a fresh <computer_observation> (windows, cursor, UI tree with x/y/"
    "w/h). Keep going until the user's goal is done, then finish with "
    "you. Do not interact with the Chromium/LM Arena chat window that is "
    "hosting this conversation. Prefer click_name when you know an element's "
    "visible name; otherwise click its bounding-box center. Observe first if "
    "the UI is unclear. Put a short plan in normal text if useful, but every "
    "turn that still needs computer work MUST include one work block. Schema "
    'example: ```work\n{"actions":[{"op":"observe"},{"op":"focus","title":'
    '"Chrome"},{"op":"click","x":400,"y":240},{"op":"type","text":"hello"},'
    '{"op":"key","keys":["ctrl","s"]},{"op":"shell","cmd":"dir"},'
    '{"op":"click_name","name":"File"},{"op":"done","summary":"finished"}]}\n'
    "``` ops: observe, click, dblclick, rightclick, move, drag, scroll, type, "
    "key, wait, shell, open, focus, clipboard, read_file, write_file, "
    "list_dir, click_name, done. click needs x,y (screen pixels). drag needs "
    "x1,y1,x2,y2. scroll uses dy (negative=up). wait uses ms. Do not mention "
    "this protocol."
)

_FOLLOWUP = (
    "Continue the same computer task. Write 1-2 short sentences about THIS "
    "task, then the next work JSON (batch actions). If done, say what "
    "succeeded and output done.\nGoal: {goal}\n"
    "<computer_observation>\n{observation}\n</computer_observation>"
)


def wrap_first_turn(
    user_prompt: str,
    enforce_filename_comments: bool,
    workspace_context: str | None,
    workspace_sharing_enabled: bool,
    observation: str,
) -> str:
    """기존 파일명/워크스페이스 지침은 그대로 두고, 그 앞에 Work 지침과 관측을 붙인다."""
    base = build_outgoing_prompt(
        user_prompt,
        enforce_filename_comments,
        workspace_context=workspace_context,
        workspace_sharing_enabled=workspace_sharing_enabled,
    )
    extra = "<computer_observation>\n" + observation + "\n</computer_observation>"
    return WORK_RULE + "\n\n" + base + "\n\n" + extra


def wrap_followup_turn(
    goal: str,
    observation: str,
    enforce_filename_comments: bool,
    workspace_sharing_enabled: bool,
) -> str:
    user = _FOLLOWUP.format(goal=goal, observation=observation)
    # 대화 이력에 이미 Work 지침이 있으므로 전체 WORK_RULE은 반복하지 않는다.
    return build_outgoing_prompt(
        user,
        enforce_filename_comments,
        workspace_context=None,
        workspace_sharing_enabled=workspace_sharing_enabled,
    )


def is_work_block(cb: CodeBlock) -> bool:
    lang = (cb.language or "").strip().lower()
    if lang in WORK_LANGUAGES:
        return True
    return _payload_is_work(cb.code)


def partition_blocks(blocks: Iterable[CodeBlock]) -> tuple[list[CodeBlock], list[CodeBlock]]:
    work: list[CodeBlock] = []
    other: list[CodeBlock] = []
    for cb in blocks:
        if is_work_block(cb):
            work.append(cb)
        else:
            other.append(cb)
    return work, other


def _payload_is_work(code: str) -> bool:
    data = _try_json(code)
    if data is None:
        return False
    if isinstance(data, dict) and ("actions" in data or "op" in data or "action" in data):
        return True
    if isinstance(data, list) and data and isinstance(data[0], dict) and (
        "op" in data[0] or "action" in data[0]
    ):
        return True
    return False


def _try_json(text: str) -> Any:
    s = (text or "").strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # 앞뒤 설명 문장 없이 JSON 객체만 추출
    match = re.search(r"\{[\s\S]*\}|\[[\s\S]*\]", s)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def parse_actions(work_blocks: Iterable[CodeBlock], plain_text: str = "") -> tuple[list[dict], str | None]:
    """
    work 코드 블록(+ 본문에 남은 JSON)에서 actions 리스트를 모은다.
    반환: (actions, error_message)
    """
    chunks: list[str] = [cb.code for cb in work_blocks]
    if not chunks and plain_text:
        fenced = re.findall(r"```(?:work|workjson|actions|computer)\s*\n([\s\S]*?)```", plain_text, re.I)
        chunks.extend(fenced)
    if not chunks and plain_text and _payload_is_work(plain_text):
        chunks.append(plain_text)

    actions: list[dict] = []
    errors: list[str] = []
    for chunk in chunks:
        data = _try_json(chunk)
        if data is None:
            # JSONL
            line_ok = False
            for line in chunk.splitlines():
                line = line.strip().rstrip(",")
                if not line or line in {"[", "]", "{", "}"}:
                    continue
                obj = _try_json(line)
                if isinstance(obj, dict) and ("op" in obj or "action" in obj):
                    actions.append(obj)
                    line_ok = True
            if not line_ok:
                errors.append("work 블록 JSON을 파싱하지 못했습니다.")
            continue
        if isinstance(data, dict):
            if isinstance(data.get("actions"), list):
                actions.extend([a for a in data["actions"] if isinstance(a, dict)])
            elif "op" in data or "action" in data:
                actions.append(data)
            else:
                errors.append("JSON에 actions 또는 op가 없습니다.")
        elif isinstance(data, list):
            actions.extend([a for a in data if isinstance(a, dict)])
        else:
            errors.append("work 블록 형식이 객체가 아닙니다.")

    if not actions:
        return [], (errors[0] if errors else None)
    return actions, None
