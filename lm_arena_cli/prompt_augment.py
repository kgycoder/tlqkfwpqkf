"""
LM Arena CLI - 파일명 자동 표기 / 워크스페이스 경로 지침 프롬프트 보강

[배경]
LM Arena Direct 모드는 Anthropic/OpenAI API처럼 별도의 "system prompt"
채널이 없는 일반 채팅 웹 UI다. 그래서 코드 블록을 아무리 많이 받아도
LM Arena 쪽 모델이 파일명을 스스로 명시해주지 않으면, file_writer.py의
FILENAME_HINT_PATTERNS가 아무 힌트도 찾지 못해 "generated_1.py"처럼
의미 없는 이름으로 저장되는 문제가 생긴다.

[이 모듈이 하는 일]
API의 system prompt 자리를 대신할 수 있는 것은, 실제로 전송되는 사용자
메시지 문자열뿐이다. 그래서 이 모듈은 사용자가 입력한 프롬프트 앞에
"코드 블록 첫 줄에 파일명을 주석으로 남겨라"는 지침(및 워크스페이스 공유가
켜져 있다면 경로 지침까지 합쳐진 지침)을 붙인 최종 문자열을 만들어
반환하기만 한다.

이 지침이 요청하는 주석 형식(`# filename: x`, `// filename: x`,
`<!-- filename: x -->`, `/* filename: x */`)은 file_writer.py의
FILENAME_HINT_PATTERNS가 이미 인식하는 형식과 정확히 동일하다. 즉
file_writer.py / browser.py / dom_selectors.py / ui.py의 스트리밍·파싱·
DOM 판별 로직은 단 한 줄도 건드리지 않고, "무엇을 전송할 것인가"라는
지점 하나에만 새 레이어를 얹는다.

[지침 문구를 "system prompt처럼 읽히는 일반 텍스트"로 바꾼 이유 - 품질
저하 버그 수정]
과거 버전은 이 지침을 <rule>...</rule> 같은 XML 스타일 가짜 태그로 감싸
보냈다. Claude처럼 이런 메타 태그 관례에 맞춰 학습된 모델에게는 문제가
없지만, LM Arena는 그때그때 다른(때로는 이런 태그 관례를 전혀 모르는)
모델로 라우팅되는 일반 채팅 UI라서, 그 모델이 <rule> 태그 자체를 답변에
그대로 따라 쓰거나, 지침과 실제 질문의 경계를 헷갈려 하거나, 코드 블록
안의 실제 <, > 문자와 태그를 혼동해 형식이 깨진 답변을 내놓는 문제가
있었다. 그래서 태그 대신, 대부분의 대화형 모델이 파인튜닝 과정에서 실제
system prompt로 흔히 봤을 법한 평범한 자연어 문구("System instruction:
...")로 지침을 시작하고, 그 아래 실제 사용자 요청은 별도로
"User request:"라는 라벨을 붙여 명확히 구분한다. "Note:"처럼 약하게
들리는 안내문 대신, LM Arena가 실제로 지켜야 할 지침이라는 무게감이
드러나는 문구를 쓰되, 어떤 특정 모델의 태그 관례에도 의존하지 않는다.
이렇게 하면 LM Arena에서 어떤 모델이 선택되어 있든 지침을 안정적으로
system-prompt급으로 받아들이면서도, 태그를 그대로 따라 쓰는 등의
부작용은 생기지 않는다.

[지침을 최대한 짧게 유지하는 이유 - 응답 속도 문제 대응]
과거 버전은 언어별 예시를 5줄 넘게 나열하는 긴 지침이었는데, 이 블록이
매 프롬프트 앞에 통째로 다시 붙어 전송되다 보니 모델이 매번 읽어야 할
입력 토큰이 늘어나 체감 응답 속도가 느려지는 부작용이 있었다. 지금은
file_writer.py 쪽 파일명 힌트 인식 로직 자체가 다양한 주석 문법
(#, //, --, <!-- -->, /* */ 등)과 "filename"/"file" 두 키워드를 모두
이해하도록 넓어졌기 때문에, 지침 쪽에서 언어별 예시를 전부 나열할 필요가
없다. 예시는 1~2개만 남겨 모델이 나머지 언어에 유추 적용하게 한다.

[워크스페이스 지침이 "따로" 전달되던 문제 - 2개 rule 병합 버그 수정]
워크스페이스 공유 기능을 처음 추가했을 때는 "파일명 규칙"과 "경로 규칙"을
서로 다른 두 개의 지침 블록으로 따로 이어 붙여 보냈다. 지침이 두 덩어리로
나뉘어 있으면 그만큼 모델이 처리해야 할 메타 지침이 늘어나고, 서로 살짝
다른 문구로 "파일명을 이렇게 써라"는 같은 요구를 두 번 반복하는 모양이 돼
모델이 헷갈리기 쉽다. 그래서 워크스페이스 공유가 켜져 있을 때는 두 규칙을
하나의 자연스러운 문단으로 합친 MERGED_FILENAME_AND_WORKSPACE_RULE 하나만
보내도록 바꿨다(파일명 규칙이 꺼져 있는데 워크스페이스 공유만 켜져 있는
경우에는 WORKSPACE_ONLY_RULE 하나만 보낸다). 어느 경우에도 지침 블록은
항상 최대 1개만 전송된다.

[중요 - 호출부 계약]
build_outgoing_prompt()의 반환값은 반드시 다음 두 곳에 "동일하게" 넘겨야
한다:
  1) LMArenaBrowser.send_prompt(반환값)            <- 실제 전송
  2) iter_response_stream(..., sent_prompt=반환값)  <- 에코 판별
  3) parse_last_response(..., sent_prompt=반환값)   <- 에코 판별

browser.py의 에코(내가 보낸 메시지) 판별 로직은 "실제로 화면에 렌더링된
사용자 메시지 텍스트"와 sent_prompt를 비교하기 때문에, 화면에 그대로
보여줄 사용자 원문(ui.user_prompt_echo 등)과 실제 전송 문자열은 서로
달라도 되지만, 전송 문자열과 sent_prompt는 반드시 같아야 한다.
"""
from __future__ import annotations

# 지침(system instruction)과 실제 사용자 프롬프트(user request) 사이의
# 구분자. 이전에는 줄바꿈 하나("\n")만 쓰고 <rule> 태그로 경계를
# 표시했지만, 이제 태그가 없는 대신 (a) 지침은 "System instruction:"으로
# 시작하고 (b) 실제 요청 앞에는 "User request:" 라벨을 붙여, 빈 줄로
# 나눠진 두 섹션이 각각 무엇인지 텍스트만으로도 분명하게 드러나게 한다.
# 대부분의 대화형 모델은 파인튜닝 데이터에서 이런 "System: .../User: ..."
# 형태의 평범한 텍스트 구분을 흔히 봐왔기 때문에, 특정 모델의 태그 관례에
# 기대지 않고도 "이건 지침, 이건 실제 요청"이라는 구분을 안정적으로
# 전달할 수 있다.
_SEPARATOR = "\n\n"
_USER_REQUEST_LABEL = "User request:\n"

# ── (1) 파일명 규칙만 켜져 있을 때 (워크스페이스 공유는 꺼짐) ────────────
FILENAME_ONLY_RULE = (
    "System instruction: whenever your answer includes a code block, put a "
    "filename comment as its very first line, using that language's normal "
    "comment syntax - for example # filename: app.py, // filename: app.js, "
    "or <!-- filename: index.html -->. Give it a real, descriptive filename "
    'with the correct extension (never "generated" or "untitled"), and '
    "reuse the exact same filename if you're editing a file mentioned "
    "earlier in this chat. Follow this instruction for every code block in "
    "your reply, but don't mention it or explain it - just apply it "
    "silently while answering the user's request normally."
)

# 기존 호출부와의 호환을 위해 이전 이름도 그대로 유지한다(이전에는 여기에
# "{user_prompt}" 자리표시자까지 포함된 완성 템플릿이 들어 있었지만, 이제
# build_outgoing_prompt()가 조립을 전담하므로 규칙 본문만 담아둔다).
FILENAME_INSTRUCTION_BLOCK = FILENAME_ONLY_RULE

# ── (2) 워크스페이스 공유만 켜져 있을 때 (파일명 규칙 자체는 꺼짐) ───────
WORKSPACE_ONLY_RULE = (
    "System instruction: whenever you add or edit a file, put a filename "
    "comment as the first line of its code block. For a file inside a "
    "subfolder of the workspace shared with you below, use its full path "
    "relative to the workspace root - for example # filename: src/App.js "
    "- matching the folder structure you were shown. Reuse the exact same "
    "path when editing a file you were already shown. Follow this "
    "instruction for every code block in your reply, but don't mention it "
    "or explain it - just apply it silently while answering the user's "
    "request normally."
)

# ── (3) 파일명 규칙 + 워크스페이스 공유가 함께 켜져 있을 때 ──────────────
# 위 두 지침을 별개 블록 두 개로 이어 붙이지 않고, 하나의 자연스러운
# 문단으로 합친 것 - "rule이 2개로 나뉘어 전달되는" 문제의 수정 지점.
MERGED_FILENAME_AND_WORKSPACE_RULE = (
    "System instruction: whenever your answer includes a code block, put a "
    "filename comment as its very first line, using that language's normal "
    "comment syntax - for example # filename: app.py, // filename: "
    'app.js. Give it a real, descriptive filename with the correct '
    'extension (never "generated" or "untitled"). For a file inside a '
    "subfolder of the workspace shared with you below, use its full path "
    "instead - for example # filename: src/App.js - matching the folder "
    "structure you were shown. Reuse the exact same filename or path when "
    "editing a file you were already shown or discussed earlier in this "
    "chat. Follow this instruction for every code block in your reply, but "
    "don't mention it or explain it - just apply it silently while "
    "answering the user's request normally."
)

# 워크스페이스 폴더 구조/내용 컨텍스트를 감싸는 래퍼. {workspace_content}
# 자리에는 workspace.format_workspace_context_block()의 결과가 그대로
# 들어간다 - 그 문자열 안에 "{"나 "}"가 있어도(코드 파일 내용이라면 흔함)
# str.format()은 이 템플릿 문자열 쪽의 자리표시자만 해석하므로 안전하다.
# 이 래퍼 자체는 실제 데이터(폴더 구조/파일 내용)를 담는 컨텍스트이지
# "지침(rule)"이 아니므로, 위 두 지침 병합 수정과는 별개로 그대로 둔다.
WORKSPACE_CONTEXT_WRAPPER = (
    "<workspace_context>\n"
    "Full folder structure and file contents of the workspace folder the "
    "user shared with you, provided once for this chat session.\n\n"
    "{workspace_content}\n"
    "</workspace_context>"
)


def build_outgoing_prompt(
    user_prompt: str,
    enabled: bool,
    workspace_context: str | None = None,
    workspace_sharing_enabled: bool = False,
) -> str:
    """
    실제로 브라우저 입력창에 채워 넣을 최종 문자열을 만든다.

    - enabled=False, workspace_sharing_enabled=False: 사용자가 입력한
      프롬프트를 그대로 반환한다(지침 없음).
    - enabled=True, workspace_sharing_enabled=False: FILENAME_ONLY_RULE
      하나만 붙인다.
    - enabled=False, workspace_sharing_enabled=True: WORKSPACE_ONLY_RULE
      하나만 붙인다.
    - enabled=True, workspace_sharing_enabled=True: 두 규칙을 하나로 합친
      MERGED_FILENAME_AND_WORKSPACE_RULE 하나만 붙인다(2개로 따로
      전달되던 문제 수정).

    지침(system instruction) 블록이 하나라도 포함되면, 실제 사용자 프롬프트
    앞에는 "User request:" 라벨을 붙여 지침과 실제 요청의 경계를 텍스트로
    분명히 한다. 지침이 전혀 없는 경우(둘 다 꺼짐)에는 아무 라벨도 붙이지
    않고 사용자가 입력한 프롬프트를 그대로 반환한다(기능이 꺼져 있으면
    기존 동작과 동일).

    어느 경우든 지침(rule) 블록은 최대 1개만 전송된다.

    - workspace_context: workspace.format_workspace_context_block()의
      결과. 값이 있으면 <workspace_context> 블록으로 감싸 지침 뒤에 함께
      보낸다. 세션당 한 번만 값을 채워 넘기고, 그 이후 턴에는 None을
      넘기는 것은 호출부(cli.py / webapp.py)의 책임이다. workspace_context
      가 없어도 workspace_sharing_enabled가 True면 경로 지침만은 계속
      함께 보낸다(공유 토글이 켜져 있는 한, 경로 표기 요구 자체는 매 턴
      유효해야 하기 때문).
    """
    if workspace_sharing_enabled:
        rule_text = MERGED_FILENAME_AND_WORKSPACE_RULE if enabled else WORKSPACE_ONLY_RULE
    elif enabled:
        rule_text = FILENAME_ONLY_RULE
    else:
        rule_text = None

    blocks: list[str] = []
    if rule_text:
        blocks.append(rule_text)
    if workspace_context:
        blocks.append(WORKSPACE_CONTEXT_WRAPPER.format(workspace_content=workspace_context))

    if not blocks:
        return user_prompt
    return _SEPARATOR.join(blocks) + _SEPARATOR + _USER_REQUEST_LABEL + user_prompt