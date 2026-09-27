"""
LM Arena DOM 셀렉터 정의

LM Arena는 프론트엔드 빌드가 바뀌면 class 명이나 구조가 변경될 수 있으므로,
각 요소마다 "우선순위가 있는 셀렉터 리스트"를 두고, Playwright 쪽에서
순서대로 시도(폴백)하도록 설계한다.

각 항목은 Playwright locator 문법(css / text / role 등)을 그대로 사용할 수 있는
문자열 리스트다.
"""

# ── 로그인 여부 판별 ──────────────────────────────────────────────
# "Experience the frontier" 문구는 로그인 전 랜딩에만 존재한다.
# 이 문구가 "존재하지 않으면" 로그인된 것으로 간주한다 (역방향 판정).
#
# 주의: 로그인 후에도 같은 class(header-5xl 등)를 재사용하는 다른 헤드라인이
# 나타날 수 있으므로, class만으로 매칭하는 셀렉터(예: "h1.header-5xl")는
# 절대 사용하지 않는다. 실제 로그인 판별은 browser.py의
# LMArenaBrowser._landing_headline_visible()에서 텍스트 내용(정규식)만으로
# 수행하며, 아래 리스트는 참고용/보조 폴백일 뿐이다.
LOGGED_OUT_MARKERS = [
    "h1:has-text('Experience the frontier')",
    "text=Experience the frontier",
]

# 로그인 후에만 나타나는 요소(있으면 더 신뢰도 높게 로그인으로 판단).
# 구조가 바뀔 수 있으므로 여러 후보를 둔다.
# 주의: 아래 textarea 계열 셀렉터는 로그인 전 랜딩 페이지에도 동일하게
# 존재하는 경우가 있어(비로그인 사용자도 입력창을 볼 수 있는 UI), 이것만으로
# "로그인됨"을 단정해서는 안 된다. 반드시 SIGN_IN_MARKERS(로그인 버튼)가
# 보이지 않는다는 조건과 함께 사용해야 한다.
# [입력창이 textarea -> contenteditable(TipTap/ProseMirror)로 바뀐 것에 대한 수정]
# LM Arena가 입력창을 일반 <textarea>에서 TipTap(ProseMirror) 기반
# contenteditable <div>로 교체하면서, textarea 계열 셀렉터가 페이지 어디에도
# 매칭되지 않게 됐다. 그 결과 (a) 프롬프트 입력창을 찾지 못해 전송이
# 실패하고, (b) 로그인 판정의 마지막 단계(입력창이 있으면 로그인됨)까지 함께
# 무너져 "로그인했는데도 로그아웃으로 판정"되는 문제가 생긴다. 그래서
# contenteditable 셀렉터를 맨 앞에 추가한다. 기존 textarea 셀렉터는 예전
# 빌드/다른 화면을 위해 폴백으로 그대로 남겨둔다.
LOGGED_IN_MARKERS = [
    "div.ProseMirror[contenteditable='true']",
    "[contenteditable='true'][translate='no']",
    "div[contenteditable='true']",
    "textarea[name='message']",
    "textarea[placeholder]",
    "[data-testid='chat-input']",
]

# 로그아웃 상태에서만 보이는 "로그인/가입" 버튼류. 이 중 하나라도 화면에
# 보이면 무조건 로그아웃으로 판정한다(가장 신뢰도 높은 로그아웃 신호).
SIGN_IN_MARKERS = [
    "button:has-text('Log in')",
    "a:has-text('Log in')",
    "button:has-text('Sign in')",
    "a:has-text('Sign in')",
    "button:has-text('Sign up')",
    "a:has-text('Sign up')",
    "text=Log in",
    "text=Sign in",
    "[data-testid='login-button']",
    "[data-testid='sign-in-button']",
]

# 로그인 후에만 보이는 계정 관련 UI(아바타, 계정 메뉴 등). 존재하면 로그인
# 판정의 신뢰도를 높이는 보조 신호로 사용한다. (필수 조건은 아님)
ACCOUNT_MENU_MARKERS = [
    "[data-testid='user-avatar']",
    "[data-testid='account-menu']",
    "button[aria-label='Account']",
    "button[aria-label='User menu']",
]

# ── 프롬프트 입력창 ────────────────────────────────────────────────
# 실제 확인된 마크업(TipTap/ProseMirror):
#   <div contenteditable="true" aria-disabled="false" translate="no"
#        class="tiptap ProseMirror prose ... bg-surface-secondary max-h-[40vh]
#               min-h-[32px] overflow-y-auto p-1 md:min-h-[80px] md:p-3"
#        tabindex="0">
#     <p data-placeholder="Ask anything…" class="is-empty is-editor-empty">
#       <br class="ProseMirror-trailingBreak">
#     </p>
#   </div>
# placeholder는 바깥 div가 아니라 "안쪽 <p>의 data-placeholder"에 있으므로,
# 예전처럼 placeholder 속성으로 입력창 자체를 찾을 수 없다. 클래스 목록은
# 빌드마다 바뀌므로 가장 안정적인 신호(contenteditable + ProseMirror 클래스 +
# translate="no")만 기준으로 잡고, 아래로 갈수록 느슨한 폴백을 둔다.
# 이름은 기존 호출부(browser.py)와의 호환을 위해 그대로 유지한다.
PROMPT_TEXTAREA = [
    "div.ProseMirror[contenteditable='true']",
    "div[contenteditable='true'][translate='no']",
    "div[contenteditable='true']:has(p[data-placeholder])",
    "[role='textbox'][contenteditable='true']",
    "div[contenteditable='true']",
    "textarea[name='message']",
    "textarea[placeholder='Ask anything…']",
    "textarea[placeholder]",
    "textarea.body-base",
    "[data-testid='chat-input']",
]

# 전송 버튼 (Enter 키가 안 먹힐 경우의 폴백)
SEND_BUTTON = [
    "button[type='submit']",
    "button[aria-label='Send message']",
    "button[data-testid='send-button']",
    "form button:has(svg)",
]

# 응답 생성 중임을 나타내는 요소 (예: 정지/중단 버튼)
#
# 실제 확인된 마크업(아래 SVG, 사각형 아이콘)에는 aria-label이나
# data-testid, 텍스트('Stop')가 전혀 없다:
#   <svg viewBox="0 0 24 24" fill="currentColor" class="size-6">
#     <rect x="6" y="6" width="12" height="12" rx="1"></rect>
#   </svg>
# 그래서 기존 셀렉터들은 하나도 매칭되지 않아 is_generating()이 답변
# 생성 중에도 항상 False를 반환하는 문제가 있었다. 이 구조(24x24 뷰박스
# 안에 12x12 정사각형 <rect> 하나만 있는 정지 아이콘)를 최우선 셀렉터로
# 추가한다. 버튼을 감싸는 wrapper 태그가 button이 아닐 수도 있으므로,
# svg 자체를 기준으로 한 셀렉터도 함께 둔다(더 견고함). 기존 셀렉터들은
# 혹시 다른 UI 상태(다른 버전/레이아웃)에서 쓰일 수 있으니 폴백으로 유지한다.
GENERATING_INDICATOR = [
    "button:has(svg rect[x='6'][y='6'][width='12'][height='12'])",
    "svg:has(rect[x='6'][y='6'][width='12'][height='12'])",
    "button:has(svg rect[width='12'][height='12'])",
    "button[aria-label='Stop generating']",
    "button[data-testid='stop-button']",
    "button:has-text('Stop')",
]

# ── 답변 중단(Stop generation) 버튼 - 클릭 대상 ───────────────────────
# 위 GENERATING_INDICATOR는 "생성 중인지 여부"만 판별하는 용도이므로 그대로
# 재사용하지 않고, "그 버튼을 실제로 클릭"하는 용도의 별도 리스트를 둔다
# (판별 로직과 클릭 로직을 분리해두면, 이후 한쪽만 바뀌어도 서로 영향을
# 주지 않는다). 실제 확인된 마크업에 aria-label="Stop generation"이 있으므로
# 이를 최우선 셀렉터로 쓰고, GENERATING_INDICATOR와 동일한 SVG 구조 기반
# 폴백(아이콘 마크업만 바뀌어도 살아남도록)을 그대로 이어서 둔다.
STOP_GENERATION_BUTTON = [
    "button[aria-label='Stop generation']",
    "button:has(svg rect[x='6'][y='6'][width='12'][height='12'])",
    "svg:has(rect[x='6'][y='6'][width='12'][height='12'])",
    "button:has(svg rect[width='12'][height='12'])",
    "button[aria-label='Stop generating']",
    "button[data-testid='stop-button']",
]

# ── 답변 컨테이너 ─────────────────────────────────────────────────
# 일반 답변 + 코드블록을 모두 포함하는 최상위 prose 컨테이너
RESPONSE_CONTAINER = [
    "div.prose[class*='prose-base']",
    "div.prose",
    "[data-testid='message-content']",
]

# 코드 블록 컨테이너
CODE_BLOCK_CONTAINER = [
    "div[data-code-block='true']",
    "div.not-prose:has(pre)",
    "pre",
]

# 코드 블록 내 언어 라벨
CODE_BLOCK_LANGUAGE_LABEL = [
    "span.text-text-secondary.text-sm.font-medium",
    "span[class*='text-secondary']",
    "div > span:first-child",
]

# 코드 블록 내 실제 코드 텍스트(보통 <code> 또는 <pre>)
CODE_BLOCK_CODE = [
    "code",
    "pre code",
    "pre",
]

# 메시지(턴) 하나의 wrapper - 마지막 assistant 메시지를 찾기 위함
MESSAGE_TURN = [
    "[data-testid='message-turn']",
    "[data-role='assistant']",
    "div.prose",
]


# ── 추론(reasoning) 미리보기 컨테이너 ────────────────────────────────
# 실제 확인된 마크업:
#   <div class="relative pl-6 pr-2 pt-4" ...>
#     <div class="bg-sidebar-border absolute bottom-0 left-[6px] top-4 w-0.5 rounded-full"></div>
#     <div class="space-y-4 whitespace-normal size-full [&>*:first-child]:mt-0
#                 [&>*:last-child]:mb-0 font-mono text-xs leading-relaxed">
#       ... 추론 텍스트 노드들 ...
#     </div>
#   </div>
#
# 답변 컨테이너(RESPONSE_CONTAINER)와는 완전히 별개의 UI이며, 세로 타임라인
# 바(bg-sidebar-border)와 font-mono/text-xs 스타일로 구분되는 "추론 로그"
# 영역이다. 실제 추론 텍스트 노드가 들어있는 안쪽 div를 기준으로 텍스트를
# 추출한다 (바깥 wrapper는 세로줄 장식용 div까지 포함하므로 텍스트 추출에는
# 안쪽 div가 더 정확하다).
REASONING_TEXT = [
    "div.font-mono.text-xs.leading-relaxed",
    "div[class*='font-mono'][class*='text-xs'][class*='leading-relaxed']",
    "div.space-y-4.whitespace-normal.size-full",
]

# 안쪽 텍스트 div를 감싸는 바깥 wrapper (세로 타임라인 바 포함). 필요 시
# 폴백/보조 용도로만 사용하고, 텍스트 추출은 REASONING_TEXT를 우선한다.
REASONING_WRAPPER = [
    "div.relative.pl-6.pr-2.pt-4:has(div.font-mono.text-xs)",
    "div:has(> div.bg-sidebar-border)",
]


# ── 새 채팅(New Chat) 버튼 ────────────────────────────────────────
# 사이드바 상단의 "New Chat" 링크. 클릭하면 /text 로 이동하며 새로운
# 대화 세션이 시작된다. class 목록이 매우 길고 빌드마다 바뀔 수 있으므로,
# 가장 안정적인 속성(href, data-sidebar, 표시 텍스트)만 기준으로 잡는다.
NEW_CHAT_BUTTON = [
    "a[data-sidebar='menu-button'][href='/text']:has-text('New Chat')",
    "a[href='/text']:has-text('New Chat')",
    "[data-sidebar='menu-button']:has-text('New Chat')",
    "a:has-text('New Chat')",
]


def first_match_locator(page_or_frame, selectors: list[str]):
    """
    주어진 셀렉터 리스트를 순서대로 시도해 첫 번째로 매칭(count>0)되는
    Locator를 반환한다. 아무것도 매칭되지 않으면 마지막 셀렉터의 locator를
    그대로 반환한다(호출부에서 count 체크 후 처리하도록).
    """
    last_locator = None
    for sel in selectors:
        try:
            loc = page_or_frame.locator(sel)
            last_locator = loc
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return last_locator

# ── 모델 선택(Model selector) ─────────────────────────────────────
# 아래 셀렉터들은 모델 선택 기능을 위해 "새로 추가"된 것이며, 위의 기존
# 셀렉터/판정 로직과는 완전히 독립적이다(서로 참조하지 않는다).
#
# 실제 확인된 마크업 (프롬프트 입력창 하단의 모델 변경 버튼):
#   <button aria-haspopup="dialog" aria-expanded="false" data-state="closed" ...>
#     <svg class="size-4 flex-none">  <- 모델 lab 아이콘 (24x24 viewBox)
#     <span class="flex-1 truncate text-left text-sm font-normal">Max</span>
#     <svg class="... size-3 flex-none">  <- 꺾쇠(chevron)
#   </button>
# 클릭하면 Radix Popover(dialog) + cmdk 커맨드 팔레트가 열린다:
#   input[cmdk-input] (placeholder="Search models")  /  div[cmdk-group]  /  [cmdk-item]
# 모델을 고르면 URL이 https://arena.ai/text/direct?model_a=<모델이름> 으로 바뀐다.
MODEL_SELECT_BUTTON = [
    "button[aria-haspopup='dialog']:has(span.flex-1.truncate.text-left)",
    "button[aria-haspopup='dialog']:has(svg.size-4.flex-none):has(span.truncate)",
    "button[aria-haspopup='dialog'][aria-controls]:has(span.truncate)",
]

# 버튼 안에서 현재 모델 이름이 들어있는 요소 / lab 아이콘 요소
MODEL_BUTTON_LABEL = "span.truncate"
MODEL_BUTTON_ICON = "svg"  # 버튼 안 첫 번째 svg가 lab 아이콘(두 번째는 chevron)

# 모델 검색 입력창 (cmdk)
MODEL_SEARCH_INPUT = [
    "input[cmdk-input]",
    "input[placeholder='Search models']",
    "input[role='combobox'][placeholder*='Search']",
]

# 모델 목록 항목 (cmdk-item) - 목록 컨테이너는 스크롤 대상 탐색에만 사용한다.
MODEL_ITEM = [
    "[cmdk-item]",
    "[role='option'][data-value]",
    "[role='option']",
]
MODEL_LIST_CONTAINER = [
    "[cmdk-list]",
    "div[cmdk-group]",
]

# URL 쿼리에서 선택된 모델을 나타내는 파라미터 이름
MODEL_URL_PARAM = "model_a"


# ══════════════════════════════════════════════════════════════════════
# Agent 모드 전용 셀렉터 (완전히 새로 추가)
# ══════════════════════════════════════════════════════════════════════
# 위쪽의 기존 셀렉터/판정 로직은 단 한 줄도 건드리지 않는다. Agent 모드
# (https://arena.ai/agent/)에서는
#   - 일반 답변 컨테이너(RESPONSE_CONTAINER)와 추론(REASONING_TEXT)은
#     Direct 모드와 **동일한** DOM을 쓰므로 그대로 재사용한다.
#   - 코드 블록만은 "artifact 카드"라는 전혀 다른 DOM으로 나타난다.
#   - 화면에 workspace(폴더/파일 트리) 패널이 추가된다.
#   - 프로젝트가 끝나면 workspace 전체를 zip으로 받는 다운로드 버튼과,
#     "이 작업이 성공했습니까?" 평가 패널이 나타난다.
# 아래는 그 네 가지에 대한 셀렉터만 새로 정의한 것이다.
#
# [클래스 이름 이스케이프 주의]
# 실제 artifact 카드의 클래스에는 `group/artifact` 처럼 '/'가 들어 있다.
# CSS에서 '/'는 이스케이프(`.group\/artifact`)가 필요해 빌드가 바뀌면
# 깨지기 쉬우므로, 아래에서는 전부 `[class*='...']`(부분 문자열 매칭)만
# 사용해 이스케이프 자체를 피한다.

# ── Agent 코드 블록(artifact 카드) ─────────────────────────────────
# 실제 확인된 마크업(요약):
#   <div role="button" aria-pressed="false" class="group/artifact ... border ...">
#     <div class="flex items-center gap-1.5 px-3 py-2">
#       <svg .../>                                           <- 코드 아이콘
#       <span class="min-w-0 flex-1 truncate text-sm ...">style.css</span>   <- 파일명
#       <span class="... uppercase ...">CSS</span>                            <- 언어 배지
#     </div>
#     <div class="... border">
#       <div class="... whitespace-pre-wrap font-mono text-xs">/* 코드 내용 */</div>
#     </div>
#   </div>
AGENT_CODE_BLOCK_CONTAINER = [
    "div[role='button'][aria-pressed][class*='artifact']",
    "div[role='button'][aria-pressed]:has(div[class*='whitespace-pre-wrap'][class*='font-mono'])",
    "div[class*='artifact']:has(div[class*='whitespace-pre-wrap'])",
    # Agent 답변 안에 Direct 형식의 코드 블록이 섞여 나올 가능성에 대비한 폴백.
    "div[data-code-block='true']",
    "div.not-prose:has(pre)",
]

# 카드 헤더의 파일명(= file_writer.py가 그대로 쓸 파일명 힌트).
# 언어 배지(uppercase)와 겹치지 않도록 flex-1/min-w-0 쪽만 노린다.
AGENT_CODE_BLOCK_FILENAME = [
    "span[class*='flex-1'][class*='truncate']",
    "span[class*='min-w-0'][class*='truncate']",
    "span.truncate",
]

# 카드 헤더 오른쪽의 언어 배지(CSS / HTML / PYTHON ...).
AGENT_CODE_BLOCK_LANGUAGE_LABEL = [
    "span[class*='uppercase']",
    "span[class*='text-xs'][class*='border']",
]

# 카드 본문의 코드 미리보기 텍스트.
AGENT_CODE_BLOCK_CODE = [
    "div[class*='whitespace-pre-wrap'][class*='font-mono']",
    "div[class*='font-mono'][class*='text-xs']",
    "pre code",
    "pre",
]

# ── Agent workspace 패널(폴더/파일 트리) ───────────────────────────
# 실제 확인된 마크업(요약):
#   <div class="body-sm overflow-y-auto max-h-[60vh] flex-1 p-2 ...">
#     <button aria-label="Collapse test-site folder" style="padding-left: 8px;">…</button>
#     <button aria-label="Expand css folder"        style="padding-left: 24px;">…</button>
#     <div role="button" aria-label="about.html file" style="padding-left: 24px;">
#        … <a href="/api/chat/workspace/cas/user/…" download="about.html">…</a>
#     </div>
#   </div>
# 폴더/파일 모두 aria-label이 "<이름> folder" / "<이름> file"로 끝나는 것이
# 가장 안정적인 신호라서 이것만 기준으로 잡는다(클래스 이름 무관).
AGENT_WORKSPACE_FOLDER_ROW = [
    "button[aria-label$=' folder']",
    "button[aria-label*='folder']",
]
AGENT_WORKSPACE_FILE_ROW = [
    "[role='button'][aria-label$=' file']",
    "[role='button'][aria-label*=' file']",
]
AGENT_WORKSPACE_PANEL = [
    "div:has(> div > [role='button'][aria-label$=' file'])",
    "div:has([role='button'][aria-label$=' file'])",
    "div[class*='overflow-y-auto']:has(button[aria-label$=' folder'])",
]

# ── workspace 전체 다운로드 버튼 ───────────────────────────────────
# 파일 하나하나에도 동일한 아래 화살표 아이콘의 다운로드 링크가 붙어 있고
# (그쪽은 svg class="size-3.5", `[role='button'][aria-label$=" file"]` 안쪽),
# workspace 전체를 zip으로 받는 버튼은 그 바깥에 있으며 아이콘이
# svg class="size-3" 이다. 셀렉터만으로 둘을 확실히 구분하기는 어려우므로,
# agent_mode.find_workspace_download()가 "파일 행 안에 들어있지 않은 것"을
# 추가로 걸러낸다. 아래 리스트는 후보를 모으는 용도다.
AGENT_WORKSPACE_DOWNLOAD_BUTTON = [
    "a[download$='.zip']",
    "button[aria-label*='Download workspace' i]",
    "a[aria-label*='Download workspace' i]",
    "button[aria-label*='Download all' i]",
    "button:has(svg path[d='M12 4V16M12 16L15.5 12.5M12 16L8.5 12.5'])",
    "a:has(svg path[d='M12 4V16M12 16L15.5 12.5M12 16L8.5 12.5'])",
    "button[aria-label*='Download' i]",
    "a[aria-label*='Download' i]",
    "a[download]",
]

# ── 프로젝트 평가(리뷰) 패널 ───────────────────────────────────────
# 실제 확인된 마크업(요약):
#   <div class="border-border-medium bg-surface-secondary ...">
#     <span class="...">이 작업이 성공했습니까?</span>
#     <button aria-label="Close review panel">…</button>
#     <button …><span>예</span></button>
#     <button …><span>아니요</span></button>
#     <button …><span>계속 작업하기</span></button>
#   </div>
AGENT_REVIEW_PANEL = [
    "div:has(> div > div > button[aria-label='Close review panel'])",
    "div:has(button[aria-label='Close review panel'])",
    "div:has(> div > span:text-matches('이 작업이 성공|Did this task succeed', 'i'))",
]
AGENT_REVIEW_CLOSE_BUTTON = [
    "button[aria-label='Close review panel']",
    "button[aria-label*='Close review' i]",
]
AGENT_REVIEW_OPTION_BUTTON = [
    "button:has(span)",
    "button",
]

# 평가 선택지 -> 실제 버튼에 적힌 텍스트 후보들(한국어 UI 기준, 영어 폴백 포함).
# 표시 순서도 이 dict 순서를 따른다(예 / 아니요 / 계속 작업하기).
AGENT_REVIEW_CHOICES = {
    "yes": ["예", "Yes"],
    "no": ["아니요", "아니오", "No"],
    "continue": ["계속 작업하기", "계속 작업", "Keep working", "Continue working", "Continue"],
}

# ── Agent 모드의 New Chat ──────────────────────────────────────────
# Direct 모드의 NEW_CHAT_BUTTON(href='/text')과 달리 Agent 모드에서는
# 링크가 /agent 쪽을 가리킨다. 기존 리스트는 건드리지 않고 별도로 둔다.
AGENT_NEW_CHAT_BUTTON = [
    "a[data-sidebar='menu-button'][href^='/agent']:has-text('New Chat')",
    "a[href^='/agent']:has-text('New Chat')",
    "[data-sidebar='menu-button']:has-text('New Chat')",
    "a:has-text('New Chat')",
]

# ── 로그인 창만 보여주기(완전히 새로 추가) ────────────────────────────
# 로그아웃 상태일 때 LM Arena 사이트 콘텐츠가 그대로 노출되지 않도록,
# 같은 페이지 안에 뜨는 로그인 모달/다이얼로그를 식별하는 데 쓴다.
# SIGN_IN_MARKERS(버튼 자체)는 그대로 두고, "버튼을 눌렀을 때 나타나는
# 팝업/모달 컨테이너"만 새로 추가한다. 어떤 인증 제공자를 쓰는지 몰라도
# 폭넓게 잡히도록 흔한 모달/다이얼로그 패턴들을 후보로 둔다.
LOGIN_MODAL_MARKERS = [
    "[role='dialog']",
    "[data-radix-portal]",
    "dialog",
    ".cl-modalContent",       # Clerk
    ".cl-rootBox",            # Clerk
    "#auth0-lock-container",  # Auth0 Lock
    "[data-testid='login-modal']",
    "[data-testid='auth-modal']",
    "iframe[src*='accounts.google.com']",
]

# 로그인 모달 안의 "Continue with Google" 버튼(구글 로그인 팝업을 직접
# 여는 버튼). 실제 확인된 마크업의 class는 Tailwind 유틸리티 조합이라
# 빌드마다 바뀔 수 있으므로 신뢰하지 않고, 텍스트 매칭을 우선 후보로
# 두고 구글 4색 로고 SVG(path fill="#4285F4")를 보조 후보로 둔다.
GOOGLE_LOGIN_BUTTON = [
    "button:has-text('Continue with Google')",
    "button:has-text('Sign in with Google')",
    "a:has-text('Continue with Google')",
    "text=Continue with Google",
    "button:has(svg path[fill='#4285F4'])",
]

# [간헐적 버그 워크어라운드] 초기 렌더링 타이밍에 따라 메인 화면에 "Log
# in" 버튼(SIGN_IN_MARKERS)이 아직 나타나지 않는 상태가 간헐적으로
# 있는데, 이때 사이드바의 "Privacy and legal" 아이콘 버튼을 한 번
# 눌러주면 Login 버튼이 나타나는 현상이 확인되었다. class는 Tailwind
# 유틸리티 조합이라 신뢰하지 않고, 실제 확인된 aria-label을 우선
# 후보로 둔다.
PRIVACY_LEGAL_BUTTON = [
    "button[aria-label='Privacy and legal']",
    "button[aria-label*='Privacy and legal' i]",
    "li[data-sidebar='menu-item'] button[aria-label*='Privacy' i]",
]

# ══════════════════════════════════════════════════════════════════════
# 로봇 확인(reCAPTCHA "Security Verification") UI 전용 셀렉터 (완전히 새로 추가)
# ══════════════════════════════════════════════════════════════════════
# 위쪽의 기존 셀렉터/판정 로직은 단 한 줄도 건드리지 않는다. 아래 값들은
# captcha.py(CaptchaGuard)가 "지금 로봇 확인 다이얼로그가 떠 있는가"를
# 판별하고, 그 다이얼로그만 화면에 남기는 데에만 쓴다.
#
# 실제 확인된 마크업(요약):
#   <div role="dialog" id="radix-_r_h3_" data-state="open" class="... fixed left-[50%] top-[50%] z-[110] ...">
#     <h2 class="sr-only">Security Verification</h2>
#     <div ...>
#       <h2 ...>Security Verification</h2>
#       <div class="recaptcha-v2-container" id="recaptcha-v2-container">
#         <iframe title="reCAPTCHA" src="https://www.google.com/recaptcha/enterprise/anchor?..."></iframe>
#         <textarea id="g-recaptcha-response" class="g-recaptcha-response"></textarea>
#       </div>
#       <span>Protected by reCAPTCHA</span>
#     </div>
#   </div>
#
# [id를 쓰지 않는 이유] id="radix-_r_h3_" 는 렌더링마다 바뀌는 Radix 자동 생성
# 값이라 절대 셀렉터로 쓰지 않는다. 또한 이 셀렉터들은 브라우저 안에서
# document.querySelector로 직접 실행되므로 Playwright 전용 문법(:has-text 등)
# 없이 "순수 CSS"만 사용한다.

# 다이얼로그 후보(모든 Radix 다이얼로그가 대상이며, 아래 마커로 걸러낸다).
CAPTCHA_DIALOG_CONTAINER = "[role='dialog']"

# 다이얼로그 "안쪽"에 이 중 하나라도 있으면 로봇 확인 다이얼로그로 판정한다.
CAPTCHA_DIALOG_INNER_MARKERS = [
    ".recaptcha-v2-container",
    "#recaptcha-v2-container",
    "iframe[src*='recaptcha']",
    "iframe[title='reCAPTCHA']",
    "textarea.g-recaptcha-response",
]

# 위 마커가 아직 렌더링되기 전 순간을 위한 텍스트 폴백(다이얼로그 전체
# 텍스트에 대한 정규식, 대소문자 무시).
CAPTCHA_DIALOG_TEXT_PATTERN = r"Security Verification|Protected by reCAPTCHA"