/* LM Arena - 브라우저 UI 프론트엔드
 *
 * 백엔드(webapp.py)와는 순수 JSON/SSE로만 통신하며, 여기서는 화면 렌더링과
 * 사용자 입력 처리만 담당한다. LM Arena와의 실제 통신/DOM 파싱은 전부 서버
 * 쪽 기존 browser.py 로직이 수행한다.
 *
 * 통신 로직(엔드포인트, SSE payload 형태, 폴링 주기)은 기존과 동일하게
 * 유지되며, 이 파일에서 바뀐 부분은 오직 렌더링/애니메이션/UX 계층이다.
 */
(() => {
  const chatScroll = document.getElementById("chatScroll");
  const chatInner = document.getElementById("chatInner");
  const emptyState = document.getElementById("emptyState");
  const promptInput = document.getElementById("promptInput");
  const sendBtn = document.getElementById("sendBtn");
  const newChatBtn = document.getElementById("newChatBtn");
  const targetInput = document.getElementById("targetInput");
  const targetSaveBtn = document.getElementById("targetSaveBtn");
  const targetHint = document.getElementById("targetHint");
  const autoWriteToggle = document.getElementById("autoWriteToggle");
  const shareWorkspaceToggle = document.getElementById("shareWorkspaceToggle");
  const shareWorkspaceHint = document.getElementById("shareWorkspaceHint");
  const workModeToggle = document.getElementById("workModeToggle");
  const workModeHint = document.getElementById("workModeHint");
  const statusDot = document.getElementById("statusDot");
  const statusText = document.getElementById("statusText");
  const logoutBtn = document.getElementById("logoutBtn");
  const collapseBtn = document.getElementById("collapseBtn");
  const expandBtn = document.getElementById("expandBtn");
  const appEl = document.getElementById("app");
  const loginOverlay = document.getElementById("loginOverlay");
  const overlayTitle = document.getElementById("overlayTitle");

  const topbarTitle = document.querySelector(".topbar-title");

  let isSending = false;
  let currentAssistantEls = null; // 스트리밍 중인 어시스턴트 메시지의 DOM 참조 묶음

  // 채팅 모드(완전히 새로 추가): "direct"(기존 동작) 또는 "agent".
  // Agent 모드에서는 (a) 모델 선택 UI가 숨겨지고, (b) 코드 블록이 LM Arena
  // Agent의 artifact 카드 형태로 그려지며, (c) workspace 트리 / 프로젝트
  // 평가 UI가 답변 아래에 추가로 나타난다. Direct 모드의 렌더링 경로는
  // 전혀 건드리지 않고, 이 값이 "agent"일 때만 새 분기가 동작한다.
  let chatMode = "direct";
  function isAgentMode() { return chatMode === "agent"; }

  // ── 아이콘 ──────────────────────────────────────────────────────
  const ICON_COPY =
    '<svg viewBox="0 0 24 24" width="14" height="14"><rect x="8" y="8" width="12" height="12" rx="2.4" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M5 15.5V5.6A2.6 2.6 0 0 1 7.6 3H16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';
  const ICON_CHECK =
    '<svg viewBox="0 0 24 24" width="14" height="14"><path d="M5 12.5l4.5 4.5L19 7" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const ICON_FILE =
    '<svg viewBox="0 0 24 24" width="12" height="12"><path d="M5 12.5l4.5 4.5L19 7" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  // 전송 버튼(위쪽 화살표)과, 답변 생성 중일 때 같은 버튼 자리에 대신
  // 표시할 중단 아이콘(사각형). LM Arena 자체 "Stop generation" 버튼의
  // 아이콘(24 뷰박스 안 12x12 정사각형)과 동일한 비율로 맞췄다.
  const ICON_SEND =
    '<svg viewBox="0 0 24 24" width="18" height="18"><path d="M12 19V5M5 12l7-7 7 7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>';
  // rect에 fill을 명시하지 않으면 SVG 기본값(검은색)이 적용돼, 전송
  // 버튼처럼 어두운 배경(var(--ink)) 위에서는 아이콘이 거의 보이지 않는
  // 문제가 있었다. 기존 ICON_SEND의 화살표가 stroke="currentColor"로
  // 버튼의 글자색(color: var(--bg), 흰색 계열)을 상속받는 것과 동일하게,
  // 이 사각형도 fill="currentColor"로 버튼 색을 그대로 상속받게 한다.
  const ICON_STOP =
    '<svg viewBox="0 0 24 24" width="18" height="18"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"></rect></svg>';

  // Agent 모드 전용 아이콘들 (artifact 카드 / workspace 트리 / 평가 패널).
  const ICON_CODE =
    '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M13.5 6L10 18.5"/><path d="M6.5 8.5L3 12l3.5 3.5"/><path d="M17.5 8.5L21 12l-3.5 3.5"/></svg>';
  const ICON_CHEV_RIGHT =
    '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>';
  const ICON_FOLDER =
    '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 11V4.6c0-.33.27-.6.6-.6h6.18c.14 0 .28.05.39.14l3.16 2.71c.11.09.25.15.39.15H21.4c.33 0 .6.27.6.6V11M2 11v8.4c0 .33.27.6.6.6h18.8c.33 0 .6-.27.6-.6V11M2 11h20"/></svg>';
  const ICON_DOC =
    '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V7l-4-4z"/><path d="M14 3v4h4"/></svg>';
  const ICON_DOWNLOAD =
    '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 20h12"/><path d="M12 4v12m0 0l3.5-3.5M12 16l-3.5-3.5"/></svg>';
  const ICON_RV_YES =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M7 12.5l3 3 7-7"/><circle cx="12" cy="12" r="10"/></svg>';
  const ICON_RV_NO =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9.2 14.8L14.8 9.2M14.8 14.8L9.2 9.2"/><circle cx="12" cy="12" r="10"/></svg>';
  const ICON_RV_CONTINUE =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 21h18"/><path d="M12.2 5.8L15 3l5 5-2.8 2.8M12.2 5.8L6.6 11.4a.6.6 0 0 0-.3.5v4.6h4.6c.16 0 .3-.06.4-.17l5.6-5.6M12.2 5.8l5 5"/></svg>';
  const ICON_X =
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6.8 17.2L12 12m5.2-5.2L12 12m0 0L6.8 6.8M12 12l5.2 5.2"/></svg>';

  // ── 유틸 ────────────────────────────────────────────────────────
  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // 아주 가벼운 마크다운 근사치 렌더러 (굵게 / 인라인 코드 / 줄바꿈만).
  // 완전한 마크다운 파서가 아니라, LM Arena 답변을 읽기 좋게 보여주기 위한
  // 최소한의 변환이다.
  function renderInlineMarkdown(text) {
    let out = escapeHtml(text);
    out = out.replace(/`([^`]+)`/g, '<code class="inline">$1</code>');
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    return out;
  }

  function scrollToBottom(smooth = true) {
    chatScroll.scrollTo({ top: chatScroll.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  }

  function hideEmptyState() {
    if (emptyState && emptyState.parentNode) emptyState.remove();
  }

  function copyToClipboard(text, btn, iconOnly) {
    navigator.clipboard.writeText(text).then(() => {
      btn.classList.add("copied");
      const label = btn.querySelector(".copy-label");
      if (iconOnly) {
        btn.innerHTML = ICON_CHECK;
      } else if (label) {
        label.textContent = "복사됨";
      }
      setTimeout(() => {
        btn.classList.remove("copied");
        if (iconOnly) {
          btn.innerHTML = ICON_COPY;
        } else if (label) {
          label.textContent = "복사";
        }
      }, 1200);
    });
  }

  // ── 경량 신택스 하이라이터 ──────────────────────────────────────
  // 외부 의존성 없이 언어별 정규식 규칙으로 토큰을 나눠 <span> 으로 감싼다.
  // 완전한 파서는 아니지만 코드 블록의 가독성을 높이기에 충분하다.
  const LANG_ALIASES = {
    js: "javascript", jsx: "javascript", mjs: "javascript", cjs: "javascript",
    ts: "javascript", tsx: "javascript",
    py: "python", python3: "python",
    sh: "bash", shell: "bash", zsh: "bash", console: "bash",
    htm: "html", xml: "html",
    yml: "yaml",
  };

  function buildTokenizer(rules) {
    const combined = new RegExp(rules.map((r) => `(?<${r.type}>${r.source})`).join("|"), "g");
    return function (code) {
      const tokens = [];
      let last = 0;
      let m;
      combined.lastIndex = 0;
      while ((m = combined.exec(code))) {
        if (m.index > last) tokens.push({ type: null, text: code.slice(last, m.index) });
        const type = Object.keys(m.groups).find((k) => m.groups[k] !== undefined);
        tokens.push({ type, text: m[0] });
        last = m.index + m[0].length;
        if (m[0].length === 0) combined.lastIndex += 1;
      }
      if (last < code.length) tokens.push({ type: null, text: code.slice(last) });
      return tokens;
    };
  }

  const RULE_SETS = {
    javascript: [
      { type: "comment", source: "\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/" },
      { type: "string", source: '"(?:\\\\.|[^"\\\\])*"' + "|'(?:\\\\.|[^'\\\\])*'" + "|`(?:\\\\.|[^`\\\\])*`" },
      { type: "number", source: "\\b0x[0-9a-fA-F]+\\b|\\b\\d+(?:\\.\\d+)?(?:[eE][+-]?\\d+)?\\b" },
      {
        type: "keyword",
        source:
          "\\b(?:const|let|var|function|return|if|else|for|while|do|class|extends|import|export|from|default|new|this|try|catch|finally|async|await|typeof|instanceof|null|undefined|true|false|switch|case|break|continue|static|get|set|throw|yield|of|in|super|void|delete|interface|type|implements|enum|as|public|private|protected|readonly)\\b",
      },
      { type: "function", source: "\\b[A-Za-z_$][\\w$]*(?=\\s*\\()" },
    ],
    python: [
      { type: "comment", source: "#[^\\n]*" },
      {
        type: "string",
        source:
          '"""[\\s\\S]*?"""' + "|'''[\\s\\S]*?'''" + '|"(?:\\\\.|[^"\\\\])*"' + "|'(?:\\\\.|[^'\\\\])*'",
      },
      { type: "number", source: "\\b\\d+(?:\\.\\d+)?\\b" },
      {
        type: "keyword",
        source:
          "\\b(?:def|return|if|elif|else|for|while|class|import|from|as|try|except|finally|with|lambda|yield|pass|break|continue|None|True|False|and|or|not|in|is|global|nonlocal|assert|raise|async|await|del|self)\\b",
      },
      { type: "function", source: "\\b[A-Za-z_]\\w*(?=\\s*\\()" },
    ],
    html: [
      { type: "comment", source: "<!--[\\s\\S]*?-->" },
      { type: "string", source: '"[^"]*"' + "|'[^']*'" },
      { type: "tag", source: "<\\/?[a-zA-Z][a-zA-Z0-9-]*" },
      { type: "property", source: "\\b[a-zA-Z-]+(?=\\=)" },
    ],
    css: [
      { type: "comment", source: "\\/\\*[\\s\\S]*?\\*\\/" },
      { type: "string", source: '"[^"]*"' + "|'[^']*'" },
      { type: "function", source: "@[a-zA-Z-]+" },
      { type: "number", source: "#[0-9a-fA-F]{3,8}\\b|-?\\b\\d+(?:\\.\\d+)?(?:px|em|rem|%|vh|vw|s|ms|deg)?\\b" },
      { type: "property", source: "[a-zA-Z-]+(?=\\s*:)" },
    ],
    json: [
      { type: "property", source: '"(?:\\\\.|[^"\\\\])*"(?=\\s*:)' },
      { type: "string", source: '"(?:\\\\.|[^"\\\\])*"' },
      { type: "number", source: "-?\\b\\d+(?:\\.\\d+)?(?:[eE][+-]?\\d+)?\\b" },
      { type: "keyword", source: "\\b(?:true|false|null)\\b" },
    ],
    bash: [
      { type: "comment", source: "#[^\\n]*" },
      { type: "string", source: '"(?:\\\\.|[^"\\\\])*"' + "|'(?:\\\\.|[^'\\\\])*'" },
      { type: "property", source: "\\$\\{?[A-Za-z_][A-Za-z0-9_]*\\}?" },
      {
        type: "keyword",
        source:
          "\\b(?:if|then|fi|for|do|done|while|case|esac|function|elif|else|in|return|exit|local|export|echo|set)\\b",
      },
    ],
    generic: [
      { type: "comment", source: "\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/|#[^\\n]*" },
      { type: "string", source: '"(?:\\\\.|[^"\\\\])*"' + "|'(?:\\\\.|[^'\\\\])*'" },
      { type: "number", source: "\\b\\d+(?:\\.\\d+)?\\b" },
    ],
  };

  const TOKENIZERS = {};
  function getTokenizer(lang) {
    if (!TOKENIZERS[lang]) TOKENIZERS[lang] = buildTokenizer(RULE_SETS[lang] || RULE_SETS.generic);
    return TOKENIZERS[lang];
  }

  function highlightCode(code, lang) {
    const key = LANG_ALIASES[(lang || "").toLowerCase().trim()] || (lang || "").toLowerCase().trim();
    const tokenize = getTokenizer(RULE_SETS[key] ? key : "generic");
    const tokens = tokenize(code);
    let out = "";
    for (const t of tokens) {
      out += t.type ? `<span class="tok-${t.type}">${escapeHtml(t.text)}</span>` : escapeHtml(t.text);
    }
    return out;
  }

  // ── 메시지 렌더링 ──────────────────────────────────────────────
  function appendUserMessage(text) {
    hideEmptyState();
    const wrap = document.createElement("div");
    wrap.className = "msg user";

    const row = document.createElement("div");
    row.className = "user-row";

    const copyBtn = document.createElement("button");
    copyBtn.className = "user-copy-btn";
    copyBtn.title = "프롬프트 복사";
    copyBtn.setAttribute("aria-label", "프롬프트 복사");
    copyBtn.innerHTML = ICON_COPY;
    copyBtn.addEventListener("click", () => copyToClipboard(text, copyBtn, true));

    const bubble = document.createElement("div");
    bubble.className = "bubble-user";
    bubble.textContent = text;

    row.appendChild(copyBtn);
    row.appendChild(bubble);
    wrap.appendChild(row);
    chatInner.appendChild(wrap);
    scrollToBottom();
  }

  function createAssistantMessage() {
    hideEmptyState();
    const wrap = document.createElement("div");
    wrap.className = "msg assistant";

    const block = document.createElement("div");
    block.className = "assistant-block";

    const label = document.createElement("div");
    label.className = "assistant-label";
    label.textContent = "LM Arena";

    // 생각 중 유기체 블롭 인디케이터 — 첫 서버 응답이 도착하기 전까지만 표시.
    const thinking = document.createElement("div");
    thinking.className = "thinking-orb";
    thinking.innerHTML = `
      <div class="orb-stage">
        <span class="orb-blob"></span>
        <span class="orb-blob b2"></span>
        <span class="orb-blob b3"></span>
      </div>
      <span class="thinking-label">생각하는 중...</span>
    `;

    // 워크스페이스 공유 알림 한 줄 (완전히 새로 추가). 이번 턴에 실제로
    // 폴더를 공유했거나, 공유하려다 실패/미설정으로 건너뛴 경우에만
    // 채워지고 보여진다. 그 외에는 항상 빈 채로 숨겨져 있다.
    const workspaceNote = document.createElement("div");
    workspaceNote.className = "hint workspace-note";
    workspaceNote.style.display = "none";

    const reasoning = document.createElement("div");
    reasoning.className = "reasoning";
    reasoning.style.display = "none";
    reasoning.innerHTML = `
      <div class="reasoning-head">
        <span class="reasoning-icon"></span>
        <span class="reasoning-label">추론 중...</span>
        <svg class="chev" viewBox="0 0 24 24" width="12" height="12"><path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </div>
      <div class="reasoning-body"><div class="reasoning-text"></div></div>
    `;
    const reasoningBody = reasoning.querySelector(".reasoning-body");
    function syncReasoningHeight() {
      reasoningBody.style.maxHeight = reasoning.classList.contains("open")
        ? Math.min(reasoningBody.scrollHeight, 400) + "px"
        : "0px";
    }
    reasoning.querySelector(".reasoning-head").addEventListener("click", () => {
      reasoning.classList.toggle("open");
      syncReasoningHeight();
    });
    reasoning._syncHeight = syncReasoningHeight;

    const textEl = document.createElement("div");
    textEl.className = "assistant-text";

    const codeHost = document.createElement("div");
    codeHost.className = "code-host";

    // Agent 모드 전용 호스트 두 개(완전히 새로 추가). Direct 모드에서는
    // 아무것도 채워지지 않으므로 화면에 전혀 나타나지 않는다.
    const agentWorkspaceHost = document.createElement("div");
    agentWorkspaceHost.className = "agent-workspace-host";
    const agentReviewHost = document.createElement("div");
    agentReviewHost.className = "agent-review-host";

    block.appendChild(label);
    block.appendChild(workspaceNote);
    block.appendChild(thinking);
    block.appendChild(reasoning);
    block.appendChild(textEl);
    block.appendChild(codeHost);
    block.appendChild(agentWorkspaceHost);
    block.appendChild(agentReviewHost);
    wrap.appendChild(block);
    chatInner.appendChild(wrap);
    scrollToBottom();

    return {
      wrap, label, thinking, reasoning, textEl, codeHost, workspaceNote,
      agentWorkspaceHost, agentReviewHost, prevTextLen: 0,
    };
  }

  // 워크스페이스 공유 결과(완전히 새로 추가) 한 줄을 채워 보여준다.
  function showWorkspaceNote(els, text) {
    if (!els || !els.workspaceNote) return;
    els.workspaceNote.textContent = text;
    els.workspaceNote.style.display = "block";
  }

  function hideThinkingOrb(els) {
    if (els && els.thinking && !els.thinking.classList.contains("hidden")) {
      els.thinking.classList.add("hidden");
    }
  }

  function updateReasoning(els, reasoning) {
    if (!reasoning) return;
    // 추론 텍스트가 실제로 생기기 시작했을 때만 "생각 중" 블롭을 내린다.
    if (reasoning.text) hideThinkingOrb(els);
    els.reasoning.style.display = "block";
    const labelEl = els.reasoning.querySelector(".reasoning-label");
    const textEl = els.reasoning.querySelector(".reasoning-text");
    if (reasoning.is_done) {
      els.reasoning.classList.remove("active");
      labelEl.textContent = `추론 완료 · ${Math.round(reasoning.elapsed_seconds)}초`;
    } else {
      els.reasoning.classList.add("active");
      labelEl.textContent = `추론 중 · ${Math.round(reasoning.elapsed_seconds)}초`;
    }
    if (reasoning.text) textEl.textContent = reasoning.text;
    if (els.reasoning.classList.contains("open") && els.reasoning._syncHeight) {
      els.reasoning._syncHeight();
    }
  }

  // 스트리밍 중 새로 도착한 부분만 살짝 페이드-인 시켜서 타이핑감을 살린다.
  // (기존에 렌더된 부분은 그대로 두고, 새로 늘어난 꼬리 부분만 감싼다.)
  const WORK_CODE_LANGS = new Set(["work", "workjson", "actions", "computer", "lma-work", "json", "jsonc", "json5"]);

  function payloadLooksLikeWork(code) {
    const s = (code || "").trim();
    if (!s) return false;
    if (/"actions"\s*:\s*\[/.test(s)) return true;
    if (/"op"\s*:/.test(s) && /"(observe|click|dblclick|rightclick|move|drag|scroll|type|key|hotkey|wait|shell|open|focus|clipboard|read_file|write_file|list_dir|click_name|done)"/.test(s)) {
      return true;
    }
    try {
      const data = JSON.parse(s);
      if (Array.isArray(data)) {
        return !!(data.length && data[0] && typeof data[0] === "object" && (data[0].op || data[0].action));
      }
      return !!(data && typeof data === "object" && ("actions" in data || "op" in data || "action" in data));
    } catch (e) {
      return false;
    }
  }

  function isWorkModeOn() {
    // Agent 모드에서는 LM Arena가 자기 workspace에서 직접 프로젝트를
    // 만들기 때문에 이 PC를 조작하는 Work 루프를 돌리지 않는다(서버
    // webapp.py도 Agent 모드를 Work 모드보다 우선한다). 그래서 토글이
    // 켜져 있어도 Agent 모드에서는 항상 꺼진 것으로 취급해, work 코드
    // 블록 숨김 처리가 Agent artifact를 가려버리지 않게 한다.
    if (isAgentMode()) return false;
    return !!(workModeToggle && workModeToggle.checked);
  }

  function isWorkCodeBlock(cb) {
    if (!cb) return false;
    const raw = (cb.language || "").toLowerCase();
    const lang = raw.trim().split(/[\s/\n]+/)[0] || "";
    if (["work", "workjson", "actions", "computer", "lma-work"].includes(lang) || raw.includes("work")) {
      return true;
    }
    return payloadLooksLikeWork(cb.code);
  }

  function visibleCodeBlocks(codeBlocks) {
    if (isWorkModeOn()) return [];
    return (codeBlocks || []).filter((cb) => !isWorkCodeBlock(cb));
  }

  function stripWorkFromPlainText(text) {
    if (!text) return text;
    let cleaned = text;
    if (isWorkModeOn()) {
      cleaned = cleaned.replace(/```[\s\S]*?```/g, "");
      cleaned = cleaned.replace(/```[\s\S]*$/, "");
      cleaned = cleaned.replace(/\{[\s\S]{0,8000}?"(?:actions|op|action)"\s*:[\s\S]{0,8000}?\}/gi, "");
    } else {
      cleaned = cleaned.replace(/```(?:work|workjson|actions|computer|lma-work)[^\n]*\n[\s\S]*?```/gi, "");
    }
    return cleaned.replace(/\n{3,}/g, "\n\n").trim();
  }

  function updateAssistantText(els, plainText, done) {
    const full = stripWorkFromPlainText(plainText || "");
    // 실제 텍스트가 도착했거나 스트림이 끝났을 때만 블롭을 내린다.
    // (빈 텍스트 상태의 delta 이벤트만으로는 아직 내리지 않는다.)
    if (full.length > 0 || done) hideThinkingOrb(els);
    let prevLen = els.prevTextLen || 0;
    if (full.length < prevLen) prevLen = 0; // 텍스트가 리셋된 경우 대비
    const settled = full.slice(0, prevLen);
    const fresh = full.slice(prevLen);

    let html = renderInlineMarkdown(settled);
    if (fresh) html += `<span class="fresh">${renderInlineMarkdown(fresh)}</span>`;
    // 아직 실제 글자가 하나도 없으면 커서를 띄우지 않는다 — 이 단계는
    // "생각 중" 블롭이 담당하므로 블롭과 커서가 동시에 보이는 것을 막는다.
    if (!done && full.length > 0) html += '<span class="cursor"></span>';

    els.textEl.innerHTML = html;
    els.prevTextLen = full.length;
  }

  function renderCodeBlocks(els, codeBlocks, writtenFiles) {
    els.codeHost.innerHTML = "";
    // Agent 모드의 코드 블록은 LM Arena Agent의 artifact 카드 형태로
    // 그린다(파일명 + 언어 배지 + 접히는 미리보기). Direct 모드 렌더링
    // 경로는 아래 기존 코드 그대로다.
    if (isAgentMode()) {
      renderAgentArtifacts(els, codeBlocks, writtenFiles);
      return;
    }
    if (isWorkModeOn()) return;
    const visible = [];
    (codeBlocks || []).forEach((cb, idx) => {
      if (isWorkCodeBlock(cb)) return;
      visible.push({ cb, file: writtenFiles && writtenFiles[idx] });
    });
    visible.forEach(({ cb, file }) => {
      const box = document.createElement("div");
      box.className = "code-block";

      const head = document.createElement("div");
      head.className = "code-head";

      const headLeft = document.createElement("div");
      headLeft.className = "code-head-left";
      headLeft.innerHTML = '<span class="code-dots"><span></span><span></span><span></span></span>';
      const langSpan = document.createElement("span");
      langSpan.className = "lang";
      langSpan.textContent = cb.language || "text";
      headLeft.appendChild(langSpan);

      const copyBtn = document.createElement("button");
      copyBtn.className = "code-copy";
      copyBtn.innerHTML = `${ICON_COPY}<span class="copy-label">복사</span>`;
      copyBtn.addEventListener("click", () => copyToClipboard(cb.code, copyBtn, false));

      head.appendChild(headLeft);
      head.appendChild(copyBtn);

      const pre = document.createElement("pre");
      const codeEl = document.createElement("code");
      codeEl.innerHTML = highlightCode(cb.code, cb.language);
      pre.appendChild(codeEl);

      box.appendChild(head);
      box.appendChild(pre);

      if (file) {
        const tag = document.createElement("div");
        tag.className = "file-written-tag";
        const path = escapeHtml(file);
        tag.innerHTML =
          `<span class="file-written-badge">${ICON_FILE}<span>저장됨</span></span>` +
          `<span class="file-written-path" title="${path}">${path}</span>`;
        box.appendChild(tag);
      }

      els.codeHost.appendChild(box);
    });
    scrollToBottom();
  }

  // ── Agent 모드 렌더링 (완전히 새로 추가) ───────────────────────
  // LM Arena Agent 모드의 답변 형식을 그대로 따른다:
  //   (1) 코드는 "artifact 카드"(파일명 · 언어 배지 · 접히는 미리보기)
  //   (2) 그 아래 workspace 폴더/파일 트리
  //   (3) 프로젝트가 끝나면 "이 작업이 성공했습니까?" 평가 버튼
  // Direct 모드의 렌더링 함수들은 전혀 건드리지 않는다.
  function renderAgentArtifacts(els, codeBlocks, writtenFiles) {
    const host = els.codeHost;
    host.innerHTML = "";
    const blocks = codeBlocks || [];
    if (!blocks.length) return;

    const list = document.createElement("div");
    list.className = "agent-artifacts";

    blocks.forEach((cb, idx) => {
      const name = (cb.filename_hint || "").trim() || `file_${idx + 1}`;
      const lang = (cb.language || "text").trim();
      const file = writtenFiles && writtenFiles[idx];

      const card = document.createElement("div");
      card.className = "agent-artifact";

      const head = document.createElement("div");
      head.className = "agent-artifact-head";
      head.setAttribute("role", "button");
      head.setAttribute("tabindex", "0");

      const icon = document.createElement("span");
      icon.className = "agent-artifact-icon";
      icon.innerHTML = ICON_CODE;

      const nameEl = document.createElement("span");
      nameEl.className = "agent-artifact-name";
      nameEl.textContent = name;
      nameEl.title = file || name;

      const badge = document.createElement("span");
      badge.className = "agent-artifact-badge";
      badge.textContent = lang;

      const copyBtn = document.createElement("button");
      copyBtn.type = "button";
      copyBtn.className = "agent-artifact-copy";
      copyBtn.title = "코드 복사";
      copyBtn.setAttribute("aria-label", "코드 복사");
      copyBtn.innerHTML = ICON_COPY;
      copyBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        copyToClipboard(cb.code, copyBtn, true);
      });

      const chev = document.createElement("span");
      chev.className = "agent-artifact-chev";
      chev.innerHTML = ICON_CHEV_RIGHT;

      head.append(icon, nameEl, badge, copyBtn, chev);

      const body = document.createElement("div");
      body.className = "agent-artifact-body";
      const pre = document.createElement("pre");
      const codeEl = document.createElement("code");
      codeEl.innerHTML = highlightCode(cb.code, lang);
      pre.appendChild(codeEl);
      body.appendChild(pre);

      const toggle = () => card.classList.toggle("open");
      head.addEventListener("click", toggle);
      head.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          toggle();
        }
      });

      card.append(head, body);

      if (file) {
        const tag = document.createElement("div");
        tag.className = "file-written-tag";
        const path = escapeHtml(file);
        tag.innerHTML =
          `<span class="file-written-badge">${ICON_FILE}<span>저장됨</span></span>` +
          `<span class="file-written-path" title="${path}">${path}</span>`;
        card.appendChild(tag);
      }

      list.appendChild(card);
    });

    host.appendChild(list);
    scrollToBottom();
  }

  // workspace 트리(폴더/파일). 서버가 LM Arena workspace 패널에서 읽어온
  // [{kind, name, depth}] 목록을 그대로 그린다.
  function renderAgentWorkspace(els, tree) {
    if (!els || !els.agentWorkspaceHost) return;
    const host = els.agentWorkspaceHost;
    host.innerHTML = "";
    const rows = tree || [];
    if (!rows.length) return;

    const box = document.createElement("div");
    box.className = "agent-workspace";

    const head = document.createElement("div");
    head.className = "agent-workspace-head";
    const title = document.createElement("span");
    title.className = "agent-workspace-title";
    const fileCount = rows.filter((r) => r.kind === "file").length;
    title.textContent = `workspace · 파일 ${fileCount}개`;

    const dl = document.createElement("button");
    dl.type = "button";
    dl.className = "agent-workspace-download";
    dl.innerHTML = `${ICON_DOWNLOAD}<span>작업 폴더로 받기</span>`;
    dl.addEventListener("click", () => downloadAgentWorkspace(els, dl));

    head.append(title, dl);

    const ul = document.createElement("ul");
    ul.className = "agent-workspace-list";
    rows.forEach((row) => {
      const li = document.createElement("li");
      li.className = "agent-workspace-row " + (row.kind === "dir" ? "dir" : "file");
      li.style.paddingLeft = 8 + Math.max(0, Number(row.depth) || 0) * 14 + "px";
      const ic = document.createElement("span");
      ic.className = "ws-icon";
      ic.innerHTML = row.kind === "dir" ? ICON_FOLDER : ICON_DOC;
      const nm = document.createElement("span");
      nm.className = "ws-name";
      nm.textContent = row.kind === "dir" ? row.name + "/" : row.name;
      nm.title = nm.textContent;
      li.append(ic, nm);
      ul.appendChild(li);
    });

    box.append(head, ul);
    host.appendChild(box);
    scrollToBottom();
  }

  // 수동 다운로드(트리 헤더의 버튼). 프로젝트가 끝나면 서버가 자동으로도
  // 한 번 받아서 작업 폴더에 풀어준다.
  async function downloadAgentWorkspace(els, btn) {
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = `${ICON_DOWNLOAD}<span>받는 중...</span>`;
    }
    try {
      const res = await fetch("/api/agent-download", { method: "POST" });
      const data = await res.json();
      showWorkspaceNote(els, agentDownloadMessage(data));
    } catch (e) {
      showWorkspaceNote(els, "workspace 다운로드 요청에 실패했습니다.");
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = `${ICON_DOWNLOAD}<span>작업 폴더로 받기</span>`;
      }
    }
  }

  function agentDownloadMessage(payload) {
    if (!payload) return "workspace 다운로드 결과를 알 수 없습니다.";
    if (!payload.ok) return `workspace 다운로드 실패: ${payload.error || "알 수 없는 오류"}`;
    const n = (payload.files || []).length;
    const name = payload.archive_name ? ` (${payload.archive_name})` : "";
    return `workspace를 작업 폴더에 반영했습니다${name} · 파일 ${n}개`;
  }

  // 프로젝트 평가 패널. LM Arena Agent가 프로젝트 종료 후 띄우는
  // "이 작업이 성공했습니까?" (예 / 아니요 / 계속 작업하기)를 그대로 옮긴다.
  const REVIEW_FALLBACK = [
    { choice: "yes", label: "예" },
    { choice: "no", label: "아니요" },
    { choice: "continue", label: "계속 작업하기" },
  ];
  const REVIEW_ICONS = { yes: ICON_RV_YES, no: ICON_RV_NO, continue: ICON_RV_CONTINUE };

  function renderAgentReview(els, question, options) {
    if (!els || !els.agentReviewHost) return;
    const host = els.agentReviewHost;
    host.innerHTML = "";

    const list = (options && options.length ? options : REVIEW_FALLBACK).filter(
      (o) => o && o.choice
    );
    if (!list.length) return;

    const box = document.createElement("div");
    box.className = "agent-review";

    const head = document.createElement("div");
    head.className = "agent-review-head";
    const q = document.createElement("span");
    q.textContent = question || "이 작업이 성공했습니까?";
    const close = document.createElement("button");
    close.type = "button";
    close.className = "agent-review-close";
    close.title = "평가 닫기";
    close.setAttribute("aria-label", "평가 닫기");
    close.innerHTML = ICON_X;
    head.append(q, close);

    const opts = document.createElement("div");
    opts.className = "agent-review-options";

    const result = document.createElement("div");
    result.className = "agent-review-result";

    const buttons = [];
    const submit = async (choice, label) => {
      buttons.forEach((b) => (b.disabled = true));
      result.textContent = "평가를 제출하는 중...";
      try {
        const res = await fetch("/api/agent-review", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ choice }),
        });
        const data = await res.json();
        if (data.ok) {
          box.classList.add("answered");
          result.textContent =
            choice === "dismiss" ? "평가를 닫았습니다." : `평가를 제출했습니다: ${label}`;
        } else {
          result.textContent = data.error || "평가를 제출하지 못했습니다.";
          buttons.forEach((b) => (b.disabled = false));
        }
      } catch (e) {
        result.textContent = "서버에 연결할 수 없습니다.";
        buttons.forEach((b) => (b.disabled = false));
      }
    };

    list.forEach((opt) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "agent-review-btn";
      btn.dataset.choice = opt.choice;
      const ic = document.createElement("span");
      ic.className = "rv-icon";
      ic.innerHTML = REVIEW_ICONS[opt.choice] || ICON_RV_CONTINUE;
      const lbl = document.createElement("span");
      lbl.textContent = opt.label || opt.choice;
      btn.append(ic, lbl);
      btn.addEventListener("click", () => submit(opt.choice, opt.label || opt.choice));
      buttons.push(btn);
      opts.appendChild(btn);
    });

    close.addEventListener("click", () => submit("dismiss", ""));

    box.append(head, opts, result);
    host.appendChild(box);
    scrollToBottom();
  }

  // ── 대화 이력 복원 ─────────────────────────────────────────────
  async function loadHistory() {
    try {
      const res = await fetch("/api/history");
      const history = await res.json();
      if (!Array.isArray(history) || history.length === 0) return;
      hideEmptyState();
      history.forEach((entry) => {
        if (entry.role === "user") {
          appendUserMessage(entry.content);
        } else {
          const els = createAssistantMessage();
          hideThinkingOrb(els);
          updateAssistantText(els, entry.content, true);
          if (entry.code_blocks && entry.code_blocks.length) {
            renderCodeBlocks(els, entry.code_blocks, entry.written_files || []);
          }
          // Agent 모드 턴이면 그때 읽었던 workspace 트리도 함께 복원한다.
          if (entry.agent_tree && entry.agent_tree.length) {
            renderAgentWorkspace(els, entry.agent_tree);
          }
        }
      });
      scrollToBottom(false);
    } catch (e) {
      // 이력 로드 실패는 치명적이지 않음 - 빈 화면으로 계속 진행
      console.warn("history load failed", e);
    }
  }

  // ── 프롬프트 전송 (SSE 스트리밍) ───────────────────────────────
  function autoResizeTextarea() {
    promptInput.style.height = "auto";
    promptInput.style.height = Math.min(promptInput.scrollHeight, 200) + "px";
  }

  function setSending(state) {
    isSending = state;
    // 답변 생성 중일 때는 버튼을 비활성화하는 대신 "중단" 버튼으로 바꿔
    // 계속 클릭 가능하게 둔다. 생성 중이 아닐 때는 기존과 동일하게
    // 입력창이 비어 있으면 비활성화한다.
    sendBtn.disabled = state ? false : !promptInput.value.trim();
    sendBtn.classList.toggle("stop-state", state);
    sendBtn.innerHTML = state ? ICON_STOP : ICON_SEND;
    sendBtn.title = state ? "답변 중단" : "전송";
    sendBtn.setAttribute("aria-label", state ? "답변 중단" : "전송");
    promptInput.disabled = state;
    setModelPickerBusy(state); // 답변 생성 중에는 모델을 바꿀 수 없다
    // 모드 전환도 동일하게 막는다(전환 시 LM Arena 대화가 새로 시작되므로).
    if (modeTrigger) modeTrigger.disabled = state || modeState.switching;
    if (state) closeModePopover();
  }

  // 진행 중인 LM Arena 답변 생성을 즉시 중단 요청한다. 실제 중단(LM
  // Arena의 Stop generation 버튼 클릭)은 서버(webapp.py)의 스트리밍
  // 루프가 다음 델타 시점에 수행하며, 이 함수는 그 요청만 보내고 바로
  // 반환한다 - SSE 스트림 자체는 서버가 "stopped"/"final" 이벤트를 보낼
  // 때까지 계속 읽는다(fetch 응답 바디를 여기서 중단하지 않는다).
  async function stopGeneration() {
    if (!isSending) return;
    try {
      await fetch("/api/stop", { method: "POST" });
    } catch (e) {
      console.warn("stop request failed", e);
    }
  }

  async function sendPrompt() {
    const text = promptInput.value.trim();
    if (!text || isSending) return;

    promptInput.value = "";
    autoResizeTextarea();
    setSending(true);

    appendUserMessage(text);
    currentAssistantEls = createAssistantMessage();

    try {
      const res = await fetch("/api/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: text }),
      });

      if (!res.ok || !res.body) {
        const errData = await res.json().catch(() => ({}));
        hideThinkingOrb(currentAssistantEls);
        updateAssistantText(
          currentAssistantEls,
          `오류가 발생했습니다: ${errData.error || res.statusText}`,
          true
        );
        setSending(false);
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let sepIdx;
        while ((sepIdx = buffer.indexOf("\n\n")) !== -1) {
          const rawEvent = buffer.slice(0, sepIdx);
          buffer = buffer.slice(sepIdx + 2);
          const line = rawEvent.split("\n").find((l) => l.startsWith("data:"));
          if (!line) continue;
          const payload = JSON.parse(line.slice(5).trim());
          handleStreamEvent(payload);
        }
      }
    } catch (e) {
      hideThinkingOrb(currentAssistantEls);
      updateAssistantText(currentAssistantEls, `연결 오류: ${e}`, true);
    } finally {
      setSending(false);
      currentAssistantEls = null;
    }
  }

  function handleStreamEvent(payload) {
    if (!currentAssistantEls) return;

    switch (payload.type) {
      case "user_echo":
        break;
      case "workspace_shared":
        showWorkspaceNote(
          currentAssistantEls,
          `작업 폴더를 공유했습니다 (파일 ${payload.included_file_count}개 내용 포함).`
        );
        break;
      case "workspace_share_warning":
        showWorkspaceNote(currentAssistantEls, payload.message || "워크스페이스 공유를 건너뛰었습니다.");
        break;
      case "work_turn":
        if (payload.step > 1) {
          currentAssistantEls = createAssistantMessage();
        }
        if (currentAssistantEls) {
          hideThinkingOrb(currentAssistantEls);
          showWorkspaceNote(currentAssistantEls, `Work ${payload.step}/${payload.max}`);
        }
        break;
      case "work_status":
        hideThinkingOrb(currentAssistantEls);
        showWorkspaceNote(currentAssistantEls, payload.message || "");
        break;
      case "work_action": {
        hideThinkingOrb(currentAssistantEls);
        const mark = payload.ok ? "ok" : "fail";
        showWorkspaceNote(
          currentAssistantEls,
          `Work ${mark}: ${payload.op || ""} — ${payload.detail || ""}`
        );
        break;
      }
      case "work_done":
        hideThinkingOrb(currentAssistantEls);
        showWorkspaceNote(
          currentAssistantEls,
          payload.summary
            ? `Work 종료 (스텝 ${payload.steps}): ${payload.summary}`
            : `Work 종료 (스텝 ${payload.steps})`
        );
        break;
      case "delta":
        // 코드 블록이 먼저 도착하는 경우도 "생각 중" 상태를 끝낸 것으로 본다.
        if (payload.code_blocks && visibleCodeBlocks(payload.code_blocks).length) {
          hideThinkingOrb(currentAssistantEls);
        }
        updateReasoning(currentAssistantEls, payload.reasoning);
        updateAssistantText(currentAssistantEls, payload.plain_text, payload.done);
        if (payload.code_blocks && visibleCodeBlocks(payload.code_blocks).length) {
          renderCodeBlocks(currentAssistantEls, payload.code_blocks, []);
        }
        scrollToBottom();
        break;
      case "final":
        // 스트림이 완전히 끝났으므로 블롭이 남아있었다면 반드시 내린다.
        hideThinkingOrb(currentAssistantEls);
        updateAssistantText(currentAssistantEls, payload.plain_text, true);
        // Agent 모드에서는 isWorkModeOn()이 항상 false이므로, 아래 조건은
        // 기존 Direct/Work 동작을 그대로 둔 채 Agent artifact도 함께 그린다.
        if (!isWorkModeOn()) {
          renderCodeBlocks(currentAssistantEls, payload.code_blocks, payload.written_files || []);
        }
        scrollToBottom();
        break;
      case "agent_workspace":
        // LM Arena workspace 패널의 폴더/파일 트리가 도착했다. 이제는 답변이
        // 끝난 뒤 한 번만이 아니라, 생성되는 도중에도 주기적으로 이 이벤트가
        // 온다(webapp.py의 워크스페이스 실시간 스트리밍 참고) - 파일이 새로
        // 생기고 있다는 뜻이므로 코드 블록과 동일하게 "생각 중" 표시를 내린다.
        hideThinkingOrb(currentAssistantEls);
        renderAgentWorkspace(currentAssistantEls, payload.tree);
        break;
      case "agent_status":
        hideThinkingOrb(currentAssistantEls);
        showWorkspaceNote(currentAssistantEls, payload.message || "");
        break;
      case "agent_download":
        // 프로젝트가 끝나 workspace zip을 받아 작업 폴더에 푼 결과.
        showWorkspaceNote(currentAssistantEls, agentDownloadMessage(payload));
        break;
      case "agent_review":
        // 프로젝트 종료 -> "이 작업이 성공했습니까?" 평가 버튼을 띄운다.
        hideThinkingOrb(currentAssistantEls);
        renderAgentReview(currentAssistantEls, payload.question, payload.options);
        break;
      case "stopped":
        // 사용자가 중단 버튼을 눌러 서버가 실제로 LM Arena의 답변 생성을
        // 멈췄다는 신호. 뒤이어 "final"(또는 Work 모드의 work_status)
        // 이벤트가 도착해 지금까지 생성된 내용으로 메시지를 마무리하므로,
        // 여기서는 "생각 중" 표시만 즉시 내려 반응을 빠르게 보여준다.
        hideThinkingOrb(currentAssistantEls);
        showWorkspaceNote(currentAssistantEls, "답변 생성을 중단했습니다.");
        break;
      case "error":
        hideThinkingOrb(currentAssistantEls);
        updateAssistantText(currentAssistantEls, `오류가 발생했습니다: ${payload.message}`, true);
        break;
    }
  }

  // ── 상태 폴링 (로그인/연결 상태) ────────────────────────────────
  function applyState(state) {
    statusDot.className = "status-dot";
    if (state.stage === "ready" && state.logged_in) {
      statusDot.classList.add("online");
      statusText.textContent = "연결됨";
      loginOverlay.classList.remove("visible");
    } else if (state.stage === "waiting_login") {
      statusDot.classList.add("waiting");
      statusText.textContent = "로그인 대기 중";
      overlayTitle.textContent = "로그인을 기다리는 중...";
      loginOverlay.classList.add("visible");
    } else if (state.stage === "error" || state.stage === "login_timeout") {
      statusDot.classList.add("error");
      statusText.textContent = state.error || "오류 발생";
    } else if (state.stage === "closed") {
      statusDot.classList.add("error");
      statusText.textContent = "세션 종료됨";
    } else {
      statusDot.classList.add("waiting");
      statusText.textContent = "연결하는 중...";
    }

    if (state.target_dir && document.activeElement !== targetInput) {
      targetInput.value = state.target_dir;
    }
    if (typeof state.auto_write_files === "boolean") {
      autoWriteToggle.checked = state.auto_write_files;
    }
    if (typeof state.share_workspace === "boolean" && document.activeElement !== shareWorkspaceToggle) {
      shareWorkspaceToggle.checked = state.share_workspace;
    }
    if (workModeToggle && typeof state.work_mode === "boolean" && document.activeElement !== workModeToggle) {
      workModeToggle.checked = state.work_mode;
    }
    if (typeof state.chat_mode === "string") {
      applyChatModeFromServer(state.chat_mode);
    }
    applyCurrentModel(state.current_model);
  }

  async function pollState() {
    try {
      const res = await fetch("/api/state");
      const state = await res.json();
      applyState(state);
    } catch (e) {
      statusDot.className = "status-dot error";
      statusText.textContent = "서버에 연결할 수 없습니다";
    }
  }

  // ── 사이드바 동작 ──────────────────────────────────────────────
  newChatBtn.addEventListener("click", async () => {
    newChatBtn.disabled = true;
    try {
      await fetch("/api/new-chat", { method: "POST" });
      chatInner.innerHTML = "";
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.id = "emptyState";
      empty.innerHTML = `
        <div class="empty-mark">LM</div>
        <div class="empty-title">무엇을 도와드릴까요?</div>
        <div class="empty-sub">메시지를 입력하면 LM Arena Direct 모드로 바로 전달됩니다.</div>
      `;
      chatInner.appendChild(empty);
    } finally {
      newChatBtn.disabled = false;
    }
  });

  targetSaveBtn.addEventListener("click", async () => {
    const dir = targetInput.value.trim();
    if (!dir) return;
    targetHint.textContent = "저장하는 중...";
    try {
      const res = await fetch("/api/target", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_dir: dir }),
      });
      const data = await res.json();
      targetInput.value = data.target_dir || dir;
      targetHint.textContent = "폴더가 저장되었습니다.";
    } catch (e) {
      targetHint.textContent = "저장에 실패했습니다.";
    }
  });

  autoWriteToggle.addEventListener("change", () => {
    fetch("/api/auto-write", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: autoWriteToggle.checked }),
    });
  });

  shareWorkspaceToggle.addEventListener("change", () => {
    shareWorkspaceHint.textContent = shareWorkspaceToggle.checked
      ? "다음 메시지부터 작업 폴더 전체를 컨텍스트로 공유합니다."
      : "켜면 작업 폴더 전체 구조/내용을 LM Arena에게 한 번 공유합니다.";
    fetch("/api/share-workspace", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: shareWorkspaceToggle.checked }),
    });
  });

  if (workModeToggle) {
    workModeToggle.addEventListener("change", () => {
      if (workModeHint) {
        workModeHint.textContent = workModeToggle.checked
          ? "다음 메시지부터 LM Arena가 명령을 확인한 뒤 이 PC를 직접 조작합니다."
          : "켜면 LM Arena가 명령을 확인한 뒤 이 PC를 직접 조작합니다.";
      }
      fetch("/api/work-mode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: workModeToggle.checked }),
      });
    });
  }

  logoutBtn.addEventListener("click", async () => {
    if (!confirm("저장된 브라우저 세션(로그인 쿠키)을 삭제할까요? 앱을 다시 시작해야 합니다.")) return;
    await fetch("/api/logout", { method: "POST" });
    statusText.textContent = "세션이 종료되었습니다. 앱을 다시 시작해주세요.";
    statusDot.className = "status-dot error";
  });

  collapseBtn.addEventListener("click", () => appEl.classList.add("sidebar-collapsed"));
  expandBtn.addEventListener("click", () => appEl.classList.remove("sidebar-collapsed"));

  // ── 입력창 동작 ────────────────────────────────────────────────
  promptInput.addEventListener("input", () => {
    autoResizeTextarea();
    sendBtn.disabled = isSending || !promptInput.value.trim();
  });
  promptInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendPrompt();
    }
  });
  sendBtn.addEventListener("click", () => {
    if (isSending) {
      stopGeneration();
    } else {
      sendPrompt();
    }
  });
  sendBtn.disabled = true;

  // ── 채팅 모드 선택 (Direct / Agent) : 완전히 새로 추가 ───────────
  // 모델 변경 UI 바로 왼쪽에 위치한다. 모드를 바꾸면 서버가 LM Arena의
  // URL을 Direct(/text/direct) <-> Agent(/agent/)로 이동시키고, 그 과정에서
  // LM Arena 대화가 새로 시작되므로 화면도 새 채팅 상태로 되돌린다.
  // Agent 모드에서는 모델을 직접 고를 수 없어 모델 선택 UI를 숨긴다.
  const modePicker = document.getElementById("modePicker");
  const modeTrigger = document.getElementById("modeTrigger");
  const modeTriggerLabel = document.getElementById("modeTriggerLabel");
  const modePopover = document.getElementById("modePopover");
  const modeList = document.getElementById("modeList");
  const modeStatus = document.getElementById("modeStatus");

  const MODE_LABELS = { direct: "Direct", agent: "Agent" };
  const MODE_TITLES = { direct: "Direct 모드", agent: "Agent 모드" };
  const MODE_PLACEHOLDERS = {
    direct: "메시지를 입력하세요...",
    agent: "만들고 싶은 프로젝트를 설명해주세요...",
  };
  const MODE_EMPTY_SUB = {
    direct: "메시지를 입력하면 LM Arena Direct 모드로 바로 전달됩니다.",
    agent: "LM Arena Agent가 workspace에서 프로젝트를 만들고, 끝나면 작업 폴더로 받아옵니다.",
  };

  const modeState = { switching: false, lastLocalChange: 0 };

  function renderModeTrigger() {
    if (!modeTriggerLabel) return;
    modeTriggerLabel.textContent = MODE_LABELS[chatMode] || "Direct";
    if (modeTrigger) {
      modeTrigger.title = `채팅 모드 변경 (현재: ${MODE_LABELS[chatMode] || "Direct"})`;
    }
    if (modeList) {
      modeList.querySelectorAll(".mode-option").forEach((li) => {
        const on = li.dataset.mode === chatMode;
        li.classList.toggle("selected", on);
        li.setAttribute("aria-selected", on ? "true" : "false");
        const check = li.querySelector(".mode-option-check");
        if (check && !check.innerHTML) check.innerHTML = ICON_MODE_CHECK;
      });
    }
  }

  const ICON_MODE_CHECK =
    '<svg viewBox="0 0 24 24" width="14" height="14"><path d="M5 12.5l4.5 4.5L19 7" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  // 모드에 따라 화면 전체(모델 선택 표시 여부, 상단 제목, 입력창 안내)를 맞춘다.
  function applyModeToUi() {
    appEl.classList.toggle("agent-mode", isAgentMode());
    renderModeTrigger();
    if (topbarTitle) topbarTitle.textContent = MODE_TITLES[chatMode] || "Direct 모드";
    if (promptInput) promptInput.placeholder = MODE_PLACEHOLDERS[chatMode] || MODE_PLACEHOLDERS.direct;
    // Agent 모드에서는 이 PC를 조작하는 Work 에이전트를 함께 쓸 수 없다.
    if (workModeToggle) workModeToggle.disabled = isAgentMode();
    if (workModeHint && isAgentMode()) {
      workModeHint.textContent = "Agent 모드에서는 사용되지 않습니다 (LM Arena가 자체 workspace에서 작업합니다).";
    }
    const sub = document.querySelector("#emptyState .empty-sub");
    if (sub) sub.textContent = MODE_EMPTY_SUB[chatMode] || MODE_EMPTY_SUB.direct;
  }

  // 2초마다 오는 상태 폴링에서 호출된다(모델 선택의 applyCurrentModel과 동일한 패턴).
  function applyChatModeFromServer(mode) {
    if (modeState.switching) return;
    if (Date.now() - modeState.lastLocalChange < 2500) return;
    const next = mode === "agent" ? "agent" : "direct";
    if (next === chatMode) return;
    chatMode = next;
    applyModeToUi();
  }

  function setModeStatus(text, isError) {
    if (!modeStatus) return;
    modeStatus.textContent = text || "";
    modeStatus.classList.toggle("error", !!isError);
  }

  function openModePopover() {
    if (!modePopover || modeTrigger.disabled) return;
    modePopover.classList.add("open");
    modeTrigger.setAttribute("aria-expanded", "true");
    renderModeTrigger();
  }

  function closeModePopover() {
    if (!modePopover) return;
    modePopover.classList.remove("open");
    modeTrigger.setAttribute("aria-expanded", "false");
  }

  function isModePopoverOpen() {
    return !!(modePopover && modePopover.classList.contains("open"));
  }

  async function chooseChatMode(mode) {
    const next = mode === "agent" ? "agent" : "direct";
    if (modeState.switching) return;
    if (next === chatMode) {
      closeModePopover();
      return;
    }
    modeState.switching = true;
    setModeStatus(`${MODE_LABELS[next]} 모드로 바꾸는 중...`, false);
    if (modeTrigger) modeTrigger.disabled = true;
    try {
      const res = await fetch("/api/chat-mode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: next }),
      });
      const data = await res.json();
      if (data.ok) {
        chatMode = data.chat_mode === "agent" ? "agent" : "direct";
        modeState.lastLocalChange = Date.now();
        // 모드를 바꾸면 LM Arena 대화가 새로 시작되므로 화면도 비운다.
        resetChatView();
        applyModeToUi();
        setModeStatus("", false);
        closeModePopover();
      } else {
        setModeStatus(data.error || "모드를 바꾸지 못했습니다.", true);
      }
    } catch (e) {
      setModeStatus("서버에 연결할 수 없습니다.", true);
    } finally {
      modeState.switching = false;
      if (modeTrigger) modeTrigger.disabled = false;
    }
  }

  if (modeTrigger && modePopover) {
    modeTrigger.addEventListener("click", () => {
      if (isModePopoverOpen()) closeModePopover();
      else openModePopover();
    });
    modeList.addEventListener("click", (e) => {
      const li = e.target.closest(".mode-option");
      if (li) chooseChatMode(li.dataset.mode);
    });
    document.addEventListener("mousedown", (e) => {
      if (isModePopoverOpen() && !modePicker.contains(e.target)) closeModePopover();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && isModePopoverOpen()) {
        e.preventDefault();
        closeModePopover();
      }
    });
    renderModeTrigger();
  }

  // ── 모델 선택 ──────────────────────────────────────────────────
  // LM Arena의 모델 변경 UI를 따른다: 입력창 하단의 모델 버튼을 누르면 검색창과
  // 목록이 있는 팝업이 열리고, 항목 왼쪽에 모델 lab 아이콘이 표시된다. 모델
  // 목록/아이콘은 서버(model_selector.py)가 LM Arena에서 읽어온 것이며, 실제
  // 모델 변경도 서버가 LM Arena의 화면을 조작해 수행한다. 기존 전송/스트리밍
  // 로직과는 독립적이다.
  const modelPicker = document.getElementById("modelPicker");
  const modelTrigger = document.getElementById("modelTrigger");
  const modelTriggerIcon = document.getElementById("modelTriggerIcon");
  const modelTriggerLabel = document.getElementById("modelTriggerLabel");
  const modelPopover = document.getElementById("modelPopover");
  const modelSearch = document.getElementById("modelSearch");
  const modelRefreshBtn = document.getElementById("modelRefreshBtn");
  const modelList = document.getElementById("modelList");
  const modelStatus = document.getElementById("modelStatus");

  const ICON_MODEL_CHECK =
    '<svg viewBox="0 0 24 24" width="14" height="14"><path d="M5 12.5l4.5 4.5L19 7" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  const modelState = {
    models: null,        // [{name, search, icon}] — 아직 불러오기 전이면 null
    current: null,       // {name, icon}
    currentSig: "",
    lastLocalChange: 0,  // 직접 바꾼 직후 늦게 도착한 옛 상태 폴링 응답을 무시하기 위한 시각
    open: false,
    loading: false,
    switching: false,
    switchTarget: "",
    error: "",           // 목록 불러오기 오류
    switchError: "",     // 모델 변경 오류
    query: "",
    filtered: [],
    active: -1,
  };

  function sameModelName(a, b) {
    return (a || "").trim().toLowerCase().replace(/\s+/g, " ") === (b || "").trim().toLowerCase().replace(/\s+/g, " ");
  }

  function fallbackModelIcon(name) {
    const f = document.createElement("span");
    f.className = "model-icon-fallback";
    f.textContent = (name || "?").trim().charAt(0).toUpperCase();
    return f;
  }

  // 아이콘은 서버가 내용 해시 ID로 내려주는 SVG 이미지다(<img>로만 쓰므로 스크립트는 실행되지 않는다).
  function fillModelIcon(box, iconId, name) {
    box.replaceChildren();
    if (!iconId) {
      if (name) box.appendChild(fallbackModelIcon(name));
      return;
    }
    const img = new Image();
    img.alt = "";
    img.width = 16;
    img.height = 16;
    img.decoding = "async";
    img.addEventListener("error", () => box.replaceChildren(fallbackModelIcon(name)), { once: true });
    img.src = "/api/model-icon/" + encodeURIComponent(iconId);
    box.appendChild(img);
  }

  function renderModelTrigger() {
    if (!modelTrigger) return;
    const cur = modelState.current;
    modelTriggerLabel.textContent = cur ? cur.name : "모델 선택";
    modelTrigger.title = cur ? `모델 변경 (현재: ${cur.name})` : "모델 변경";
    fillModelIcon(modelTriggerIcon, cur && cur.icon, cur && cur.name);
  }

  function setCurrentModel(cm, fromLocalAction) {
    const name = cm && cm.name ? cm.name : "";
    const sig = name ? name + "|" + (cm.icon || "") : "";
    if (fromLocalAction) modelState.lastLocalChange = Date.now();
    if (sig === modelState.currentSig) return;
    modelState.currentSig = sig;
    modelState.current = name ? { name, icon: cm.icon || null } : null;
    renderModelTrigger();
    if (modelState.open) renderModelList();
  }

  // 2초마다 오는 상태 폴링에서 호출된다.
  function applyCurrentModel(cm) {
    if (!modelTrigger || modelState.switching) return;
    if (Date.now() - modelState.lastLocalChange < 2500) return;
    setCurrentModel(cm, false);
  }

  function setModelPickerBusy(busy) {
    if (!modelTrigger) return;
    modelTrigger.disabled = !!busy;
    if (busy && modelState.open) closeModelPopover(false);
  }

  // 검색: 공백으로 나눈 모든 단어가 (이름 + 항목에 보이는 텍스트)에 들어 있으면 일치.
  // '-', '_', '.'는 공백처럼 취급해 "claude sonnet 5"로도 "claude-sonnet-5-high"를 찾는다.
  function normalizeForSearch(text) {
    return (text || "").toLowerCase().replace(/[-_.]+/g, " ");
  }

  function filterModels(query) {
    const models = modelState.models || [];
    const terms = normalizeForSearch(query).split(/\s+/).filter(Boolean);
    if (!terms.length) return models;
    const scored = [];
    models.forEach((m, i) => {
      const name = normalizeForSearch(m.name);
      const hay = name + " " + normalizeForSearch(m.search);
      if (!terms.every((t) => hay.includes(t))) return;
      // 이름이 검색어로 시작 > 이름에 포함 > 그 외(부가 텍스트에만 있음)
      const rank = terms.every((t) => name.startsWith(t)) ? 0 : terms.every((t) => name.includes(t)) ? 1 : 2;
      scored.push([rank, i, m]);
    });
    scored.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    return scored.map((x) => x[2]);
  }

  function renderModelStatus() {
    let text = "";
    let isError = false;
    if (modelState.switching) {
      text = `${modelState.switchTarget}(으)로 바꾸는 중...`;
    } else if (modelState.switchError) {
      text = modelState.switchError;
      isError = true;
    } else if (modelState.error && modelState.models) {
      text = modelState.error;
      isError = true;
    } else if (modelState.loading && modelState.models) {
      text = "목록을 새로 불러오는 중...";
    }
    modelStatus.textContent = text;
    modelStatus.classList.toggle("error", isError);
  }

  function modelListNote(message, retry) {
    const li = document.createElement("li");
    li.className = "model-list-note";
    li.setAttribute("role", "presentation");
    li.appendChild(document.createTextNode(message));
    if (retry) {
      li.appendChild(document.createElement("br"));
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = "다시 시도";
      btn.addEventListener("click", () => loadModels(true));
      li.appendChild(btn);
    }
    modelList.appendChild(li);
  }

  function setActiveModelOption(index, scroll) {
    const prev = modelList.querySelector(".model-option.active");
    if (prev) prev.classList.remove("active");
    modelState.active = index;
    const el = index >= 0 ? modelList.querySelector(`#model-opt-${index}`) : null;
    if (el) {
      el.classList.add("active");
      modelSearch.setAttribute("aria-activedescendant", el.id);
      if (scroll) el.scrollIntoView({ block: "nearest" });
    } else {
      modelSearch.removeAttribute("aria-activedescendant");
    }
  }

  function renderModelList() {
    modelList.replaceChildren();
    modelState.filtered = [];
    renderModelStatus();

    if (!modelState.models) {
      if (modelState.loading) {
        modelListNote("모델 목록을 불러오는 중입니다.\n처음에는 몇 초 걸릴 수 있어요.");
      } else {
        modelListNote(modelState.error || "모델 목록을 아직 불러오지 못했습니다.", true);
      }
      setActiveModelOption(-1, false);
      return;
    }

    const list = filterModels(modelState.query);
    modelState.filtered = list;
    if (!list.length) {
      modelListNote("일치하는 모델이 없습니다.");
      setActiveModelOption(-1, false);
      return;
    }

    const frag = document.createDocumentFragment();
    let currentIdx = -1;
    list.forEach((m, i) => {
      const isCurrent = !!modelState.current && sameModelName(modelState.current.name, m.name);
      if (isCurrent) currentIdx = i;
      const li = document.createElement("li");
      li.className = "model-option" + (isCurrent ? " selected" : "");
      li.id = "model-opt-" + i;
      li.dataset.index = String(i);
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", isCurrent ? "true" : "false");

      const icon = document.createElement("span");
      icon.className = "model-icon";
      icon.setAttribute("aria-hidden", "true");
      fillModelIcon(icon, m.icon, m.name);

      const nm = document.createElement("span");
      nm.className = "model-option-name";
      nm.textContent = m.name;
      nm.title = m.name;

      const check = document.createElement("span");
      check.className = "model-option-check";
      check.innerHTML = ICON_MODEL_CHECK;

      li.append(icon, nm, check);
      frag.appendChild(li);
    });
    modelList.appendChild(frag);

    // 검색어가 없으면 현재 모델에, 있으면 가장 잘 맞는 첫 항목에 커서를 둔다.
    const start = !modelState.query.trim() && currentIdx >= 0 ? currentIdx : 0;
    setActiveModelOption(start, true);
  }

  async function loadModels(force) {
    if (modelState.loading) return;
    modelState.loading = true;
    modelState.error = "";
    modelRefreshBtn.disabled = true;
    modelRefreshBtn.classList.add("spinning");
    renderModelList();
    try {
      const res = await fetch("/api/models" + (force ? "?refresh=1" : ""));
      const data = await res.json();
      if (Array.isArray(data.models) && data.models.length) modelState.models = data.models;
      if (data.ok === false) modelState.error = data.error || "모델 목록을 불러오지 못했습니다.";
      if (data.current) setCurrentModel(data.current, false);
    } catch (e) {
      modelState.error = "서버에 연결할 수 없습니다.";
    } finally {
      modelState.loading = false;
      modelRefreshBtn.disabled = false;
      modelRefreshBtn.classList.remove("spinning");
      if (modelState.open) renderModelList();
    }
  }

  function openModelPopover() {
    if (modelState.open || modelTrigger.disabled) return;
    modelState.open = true;
    modelState.query = "";
    modelState.switchError = "";
    modelSearch.value = "";
    modelPopover.classList.add("open");
    modelTrigger.setAttribute("aria-expanded", "true");
    renderModelList();
    modelSearch.focus({ preventScroll: true });
    if (!modelState.models) loadModels(false);
  }

  function closeModelPopover(returnFocus) {
    if (!modelState.open) return;
    modelState.open = false;
    modelPopover.classList.remove("open");
    modelTrigger.setAttribute("aria-expanded", "false");
    if (returnFocus) modelTrigger.focus({ preventScroll: true });
  }

  // 새 대화가 시작된 것과 같은 화면 상태로 되돌린다(서버가 URL 이동 폴백을 쓴 경우).
  function resetChatView() {
    chatInner.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.id = "emptyState";
    empty.innerHTML = `
      <div class="empty-mark">LM</div>
      <div class="empty-title">무엇을 도와드릴까요?</div>
      <div class="empty-sub">모델을 바꾸면서 새 대화가 시작되었습니다.</div>
    `;
    chatInner.appendChild(empty);
  }

  async function chooseModel(m) {
    if (!m || modelState.switching) return;
    if (modelState.current && sameModelName(modelState.current.name, m.name)) {
      closeModelPopover(true);
      return;
    }
    modelState.switching = true;
    modelState.switchTarget = m.name;
    modelState.switchError = "";
    modelPicker.classList.add("switching");
    renderModelStatus();
    let ok = false;
    try {
      const res = await fetch("/api/model", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: m.name }),
      });
      const data = await res.json();
      if (data.current) setCurrentModel(data.current, true);
      if (data.reset) resetChatView();
      ok = !!data.ok;
      if (!ok) modelState.switchError = data.error || "모델을 바꾸지 못했습니다.";
    } catch (e) {
      modelState.switchError = "서버에 연결할 수 없습니다.";
    } finally {
      modelState.switching = false;
      modelPicker.classList.remove("switching");
    }
    if (ok) {
      closeModelPopover(true);
    } else if (modelState.open) {
      renderModelList();
    }
  }

  if (modelTrigger && modelPopover) {
    modelTrigger.addEventListener("click", () => {
      if (modelState.open) closeModelPopover(false);
      else openModelPopover();
    });

    modelRefreshBtn.addEventListener("click", () => loadModels(true));

    modelSearch.addEventListener("input", () => {
      modelState.query = modelSearch.value;
      modelState.switchError = "";
      renderModelList();
    });

    modelSearch.addEventListener("keydown", (e) => {
      const n = modelState.filtered.length;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!n) return;
        const step = e.key === "ArrowDown" ? 1 : -1;
        setActiveModelOption((modelState.active + step + n) % n, true);
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (modelState.active >= 0) chooseModel(modelState.filtered[modelState.active]);
      } else if (e.key === "Tab") {
        closeModelPopover(false);
      }
    });

    // 검색창이 포커스를 잃지 않도록(목록 클릭 시 팝업이 닫히지 않도록) 팝업 안 mousedown의 기본 동작을 막는다.
    modelPopover.addEventListener("mousedown", (e) => {
      if (e.target !== modelSearch) e.preventDefault();
    });

    modelList.addEventListener("click", (e) => {
      const li = e.target.closest(".model-option");
      if (li) chooseModel(modelState.filtered[Number(li.dataset.index)]);
    });
    modelList.addEventListener("mousemove", (e) => {
      const li = e.target.closest(".model-option");
      if (!li) return;
      const idx = Number(li.dataset.index);
      if (idx !== modelState.active) setActiveModelOption(idx, false);
    });

    document.addEventListener("mousedown", (e) => {
      if (modelState.open && !modelPicker.contains(e.target)) closeModelPopover(false);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && modelState.open) {
        e.preventDefault();
        closeModelPopover(true);
      }
    });
  }

  // ── JAVIS 음성 채팅 모드 ────────────────────────────────────────
  // Direct 모드의 기존 렌더링/전송 로직은 전혀 건드리지 않고, 별도의
  // 전체 화면 뷰 + 자체 전송 파이프라인으로 동작한다. 대화 내용은
  // handleStreamEvent를 그대로 재사용해 채팅 이력에도 함께 반영된다.
  (() => {
    const modeTabDirect = document.getElementById("modeTabDirect");
    const modeTabJavis = document.getElementById("modeTabJavis");
    const javisView = document.getElementById("javisView");
    const javisExitBtn = document.getElementById("javisExitBtn");
    const javisCanvas = document.getElementById("javisCanvas");
    const javisCaption = document.getElementById("javisCaption");
    const javisStatus = document.getElementById("javisStatus");
    const javisMicBtn = document.getElementById("javisMicBtn");
    const javisVoiceRow = document.getElementById("javisVoiceRow");
    const javisVoiceDropdown = document.getElementById("javisVoiceDropdown");
    const javisVoiceTrigger = document.getElementById("javisVoiceTrigger");
    const javisVoiceTriggerLabel = document.getElementById("javisVoiceTriggerLabel");
    const javisVoiceList = document.getElementById("javisVoiceList");

    if (!modeTabJavis || !javisView) return; // 마크업이 없으면 조용히 스킵

    const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;
    const hasSpeechSynthesis = "speechSynthesis" in window;

    let javisListening = false;
    let javisSpeaking = false;
    let javisRecognition = null;
    let javisListenRafId = null;

    // 음성 인식이 잠깐의 숨 고르기/침묵만으로 바로 전송해버리는 문제를
    // 막기 위한 버퍼 및 무음 타이머. 브라우저의 "isFinal" 판정은 매우
    // 성급해서, 결과가 들어올 때마다 텍스트를 누적만 해두고 실제 전송은
    // 아래 JAVIS_SILENCE_MS 만큼 조용한 상태가 이어진 뒤에야 한다.
    let javisFinalBuffer = ""; // 지금까지 확정(isFinal)된 텍스트 누적분
    let javisLastInterim = ""; // 아직 확정되지 않은 마지막 중간 인식 결과
    let javisSilenceTimer = null;
    const JAVIS_SILENCE_MS = 2600; // 마지막 인식 이벤트 이후 대기 시간 — 여유 있게 잡아 문장 사이 텀에 끊기지 않게 한다.

    // ── 유동적 네트워크 구체 시각화 (캔버스) ─────────────────────
    // 장식적 글로우/코어/펄스/배경 입자를 모두 걷어내고, 순수하게
    // 노드-커넥션 네트워크 하나로만 구체를 표현한다:
    //   1) 각 노드가 구 표면 위에서 저마다 다른 위상/속도로 위도·경도를
    //      따라 천천히 유영(遊泳)하듯 움직인다 — 정적인 점 배열이 아니라
    //      살아 움직이는 네트워크로 보이게 하는 핵심 장치.
    //   2) 노드가 움직이는 만큼 가까운 노드끼리의 연결선도 매 프레임
    //      다시 계산해, 망(網)이 끊어지고 이어지며 유동적으로 흐른다.
    //   3) 화이트 배경 위 단색(잉크) 톤만 사용하고, 깊이는 z-정렬과
    //      알파(불투명도)만으로 표현한다 — 색상 강조 없이 명도로만.
    let javisCtx2d = null;
    let javisDpr = window.devicePixelRatio || 1;
    let javisRafId = null;
    let javisRotY = 0;
    let javisEnergy = 0; // 부드럽게 보간되는 현재 에너지(0~1) — 마이크 볼륨/발화 리듬에 반응
    let javisTargetEnergy = 0; // 목표 에너지 — 다른 로직에서 갱신

    // 추론(reasoning) 스트리밍 · 코드 블록 작성 중임을 나타내는 별도의 "유기적" 효과.
    // 답변을 말할 때(javisEnergy)는 목소리 리듬에 맞춘 규칙적인 펄스인 반면,
    // 이 값은 노드 하나하나가 제각각 다른 위상/속도로 꿈틀거리는 불규칙한
    // 흔들림(난류)을 만들어 "생각/작업 중"임을 시각적으로 구분해준다.
    let javisProcessing = 0; // 부드럽게 보간되는 현재 난류 강도(0~1)
    let javisTargetProcessing = 0; // 목표 난류 강도

    // 화이트 & 블랙 테마: 단일 잉크 톤(브랜드 --ink)만 사용한다.
    const JAVIS_INK_RGB = "20,20,19";

    function buildJavisNodes(count) {
      const nodes = [];
      const offset = 2 / count;
      const increment = Math.PI * (3 - Math.sqrt(5));
      for (let i = 0; i < count; i++) {
        const y = i * offset - 1 + offset / 2;
        const phi = i * increment;
        nodes.push({
          // 구면좌표 기준값 — 여기서 살짝씩 벗어나며 표면을 유영한다.
          thetaBase: Math.acos(Math.max(-1, Math.min(1, y))),
          phiBase: phi,
          // 위도(theta) 방향으로 흐르는 속도/진폭/위상.
          flowPhaseA: Math.random() * Math.PI * 2,
          flowSpeedA: 0.05 + Math.random() * 0.09,
          flowAmpA: 0.05 + Math.random() * 0.11,
          // 경도(phi) 방향으로 흐르는 속도/진폭/위상 — 위도와 다른 리듬을 줘 일제히 움직이지 않게 한다.
          flowPhaseB: Math.random() * Math.PI * 2,
          flowSpeedB: 0.04 + Math.random() * 0.08,
          flowAmpB: 0.09 + Math.random() * 0.16,
          // 반지름 방향의 아주 미세한 호흡.
          jitterPhase: Math.random() * Math.PI * 2,
          jitterSpeed: 0.6 + Math.random() * 0.9,
          jitterAmp: 0.012 + Math.random() * 0.016,
          // "추론/코드 작성 중" 전용 난류 — 평소 흐름보다 훨씬 빠르고
          // 제각각인 위상으로 움직여, 말할 때의 규칙적인 펄스와 뚜렷이
          // 구분되는 불규칙하고 유기적인 꿈틀거림을 만든다. javisProcessing이
          // 0일 때는 아래 flowJavisNode에서 완전히 사라지고, 값이 커질수록
          // 서서히 섞여 들어온다.
          turbPhaseA: Math.random() * Math.PI * 2,
          turbSpeedA: 0.7 + Math.random() * 1.3,
          turbAmpA: 0.05 + Math.random() * 0.1,
          turbPhaseB: Math.random() * Math.PI * 2,
          turbSpeedB: 0.7 + Math.random() * 1.4,
          turbAmpB: 0.06 + Math.random() * 0.12,
          turbJitterPhase: Math.random() * Math.PI * 2,
          turbJitterSpeed: 1.4 + Math.random() * 1.8,
          turbJitterAmp: 0.02 + Math.random() * 0.03,
          shimmerPhase: Math.random() * Math.PI * 2,
        });
      }
      return nodes;
    }
    const javisNodes = buildJavisNodes(170);

    function resizeJavisCanvas() {
      const rect = javisCanvas.parentElement.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      javisDpr = window.devicePixelRatio || 1;
      javisCanvas.width = rect.width * javisDpr;
      javisCanvas.height = rect.height * javisDpr;
      javisCtx2d = javisCanvas.getContext("2d");
      javisCtx2d.setTransform(javisDpr, 0, 0, javisDpr, 0, 0);
    }

    // 노드의 "현재" 단위구 좌표를 계산한다 — 기준 위도/경도에서 각자의
    // 리듬으로 흘러 다니게 하는, 유동성의 핵심 함수.
    function flowJavisNode(node, now, processing) {
      const turb = processing || 0;
      let theta = node.thetaBase + Math.sin(now / 1000 * node.flowSpeedA + node.flowPhaseA) * node.flowAmpA;
      let phi = node.phiBase + Math.cos(now / 1000 * node.flowSpeedB + node.flowPhaseB) * node.flowAmpB;
      let breathe = 1 + Math.sin(now / 1000 * node.jitterSpeed + node.jitterPhase) * node.jitterAmp;
      if (turb > 0.001) {
        // 답변 펄스와는 다른, 빠르고 제각각인 위상의 불규칙한 꿈틀거림을 섞어
        // "추론 중 / 코드 작성 중"에만 나타나는 유기적인 표면 흔들림을 만든다.
        theta += turb * Math.sin(now / 1000 * node.turbSpeedA + node.turbPhaseA) * node.turbAmpA;
        phi += turb * Math.cos(now / 1000 * node.turbSpeedB + node.turbPhaseB) * node.turbAmpB;
        breathe += turb * Math.sin(now / 1000 * node.turbJitterSpeed + node.turbJitterPhase) * node.turbJitterAmp;
      }
      const sinTheta = Math.sin(theta) * breathe;
      return {
        x: sinTheta * Math.cos(phi),
        y: Math.cos(theta) * breathe,
        z: sinTheta * Math.sin(phi),
      };
    }

    function projectJavisPoint(p, cx, cy, radius, rotY, rotX) {
      const x1 = p.x * Math.cos(rotY) - p.z * Math.sin(rotY);
      const z1 = p.x * Math.sin(rotY) + p.z * Math.cos(rotY);
      const y1 = p.y * Math.cos(rotX) - z1 * Math.sin(rotX);
      const z2 = p.y * Math.sin(rotX) + z1 * Math.cos(rotX);
      const perspective = 2.6;
      const scale = perspective / (perspective - z2 * 0.9);
      return { x: cx + x1 * radius * scale, y: cy + y1 * radius * scale, z: z2, scale };
    }

    function drawJavisSphere(cx, cy, radius, rotY, rotX, now, processing) {
      const ctx = javisCtx2d;
      const turb = processing || 0;
      const positions = javisNodes.map((n) => flowJavisNode(n, now, turb));
      const projected = positions.map((p) => projectJavisPoint(p, cx, cy, radius, rotY, rotX));

      // 노드가 흘러 다니는 만큼, 연결선도 "현재" 위치 기준으로 매 프레임
      // 새로 계산한다 — 망이 이어지고 끊어지며 유동적으로 흐르게 하는 부분.
      const edges = [];
      for (let i = 0; i < positions.length; i++) {
        for (let j = i + 1; j < positions.length; j++) {
          const a = positions[i], b = positions[j];
          const dx = a.x - b.x, dy = a.y - b.y, dz = a.z - b.z;
          const d = Math.sqrt(dx * dx + dy * dy + dz * dz);
          if (d < 0.32) edges.push({ i, j, avgZ: (a.z + b.z) / 2 });
        }
      }
      edges.sort((e1, e2) => e1.avgZ - e2.avgZ);

      ctx.lineWidth = 1;
      edges.forEach((e) => {
        const pa = projected[e.i], pb = projected[e.j];
        const avgZ = (pa.z + pb.z) / 2;
        const depthT = Math.max(0, Math.min(1, (avgZ + 1) / 2));
        const alpha = depthT * (0.12 + javisEnergy * 0.3 + turb * 0.16);
        if (alpha < 0.012) return;
        ctx.strokeStyle = `rgba(${JAVIS_INK_RGB},${alpha.toFixed(3)})`;
        ctx.beginPath();
        ctx.moveTo(pa.x, pa.y);
        ctx.lineTo(pb.x, pb.y);
        ctx.stroke();
      });

      // 노드도 z 순서(뒤→앞)로 그려 겹침이 자연스럽게 보이게 한다.
      const order = projected.map((p, idx) => idx).sort((a, b) => projected[a].z - projected[b].z);
      order.forEach((idx) => {
        const p = projected[idx];
        const node = javisNodes[idx];
        const depthT = Math.max(0, Math.min(1, (p.z + 1) / 2));
        // 노드마다 다른 위상으로 반짝이는 "시머"는 처리 중(turb)에만 섞여
        // 들어가, 목소리 리듬에 맞춘 균일한 펄스(javisEnergy)와 뚜렷이
        // 구분되는 불규칙한 깜빡임을 만든다.
        const shimmer = turb > 0.001
          ? turb * (0.5 + 0.5 * Math.sin(now / 260 + node.shimmerPhase))
          : 0;
        const size = (1.1 + javisEnergy * 1.8 + shimmer * 1.4) * p.scale * (0.7 + depthT * 0.6);
        ctx.beginPath();
        ctx.fillStyle = `rgba(${JAVIS_INK_RGB},${(0.32 + depthT * 0.5 + javisEnergy * 0.2 + shimmer * 0.28).toFixed(3)})`;
        ctx.arc(p.x, p.y, Math.max(0.35, size), 0, Math.PI * 2);
        ctx.fill();
      });
    }

    function renderJavisFrame() {
      if (!javisCtx2d) { javisRafId = requestAnimationFrame(renderJavisFrame); return; }
      const w = javisCanvas.width / javisDpr;
      const h = javisCanvas.height / javisDpr;
      javisCtx2d.clearRect(0, 0, w, h);

      javisEnergy += (javisTargetEnergy - javisEnergy) * 0.1;
      // 처리(추론/코드) 난류는 답변 펄스보다 살짝 느리게 보간해, 나타나고
      // 사라질 때 뚝 끊기지 않고 서서히 스며들듯 전환되게 한다.
      javisProcessing += (javisTargetProcessing - javisProcessing) * 0.06;
      const now = Date.now();
      // 아주 느리고 불규칙한(다중 사인 합성) 호흡 — 완전히 기계적인 등속
      // 회전이 아니라, 미묘하게 살아있는 리듬으로 느껴지도록 한다.
      const breathe = Math.sin(now / 1650) * 0.018 + Math.sin(now / 730) * 0.008;
      const baseR = Math.min(w, h) * 0.2;
      const radius = baseR * (1 + breathe + javisEnergy * 0.2 + javisProcessing * 0.05);
      const rotX = 0.38 + Math.sin(now / 5200) * 0.07;
      // 처리 중에는 회전도 살짝 불규칙하게 흔들려, 발화 시의 매끄러운
      // 가속/감속과는 다른 "골똘히 생각하는" 듯한 느낌을 더한다.
      javisRotY += 0.0016 + javisEnergy * 0.0045 + javisProcessing * 0.0015 * Math.sin(now / 900);

      const cx = w / 2, cy = h / 2;

      drawJavisSphere(cx, cy, radius, javisRotY, rotX, now, javisProcessing);

      javisRafId = requestAnimationFrame(renderJavisFrame);
    }

    function startJavisRender() {
      resizeJavisCanvas();
      if (!javisRafId) renderJavisFrame();
    }
    function stopJavisRender() {
      if (javisRafId) cancelAnimationFrame(javisRafId);
      javisRafId = null;
      javisCtx2d = null;
    }
    window.addEventListener("resize", () => {
      if (javisView.classList.contains("visible")) resizeJavisCanvas();
    });

    // ── 모드 전환 ─────────────────────────────────────────────────
    function setMode(mode) {
      if (mode === "javis") {
        modeTabJavis.classList.add("active");
        modeTabDirect.classList.remove("active");
        javisView.classList.add("visible");
        startJavisRender();
      } else {
        modeTabDirect.classList.add("active");
        modeTabJavis.classList.remove("active");
        javisView.classList.remove("visible");
        stopJavisRender();
        stopJavisListening();
        javisFinalBuffer = "";
        javisLastInterim = "";
        cancelJavisSpeech();
      }
    }
    modeTabDirect.addEventListener("click", () => setMode("direct"));
    modeTabJavis.addEventListener("click", () => setMode("javis"));
    javisExitBtn.addEventListener("click", () => setMode("direct"));

    // ── 음성 인식 (마이크 입력) ──────────────────────────────────
    if (!SpeechRecognitionCtor) {
      javisMicBtn.classList.add("unsupported");
      javisStatus.textContent = "이 브라우저는 음성 인식을 지원하지 않습니다.";
    } else if (!window.isSecureContext) {
      // 마이크 관련 API는 보안 컨텍스트(https:// 또는 localhost)에서만 동작한다.
      // http://로 접속했거나 file://로 열었다면 여기서 조용히 막힌다.
      javisMicBtn.classList.add("unsupported");
      javisStatus.textContent = "마이크 사용을 위해 https:// 또는 localhost로 접속해주세요.";
    }

    function ensureJavisRecognition() {
      if (javisRecognition || !SpeechRecognitionCtor) return javisRecognition;
      javisRecognition = new SpeechRecognitionCtor();
      javisRecognition.lang = "ko-KR";
      // continuous: true — 말 사이에 짧게 숨을 쉬어도 세션이 끊기지 않게 한다.
      // (마이크를 다시 누르기 전까지는 계속 듣는다.)
      javisRecognition.continuous = true;
      javisRecognition.interimResults = true;

      javisRecognition.onresult = (e) => {
        // rec.stop()을 호출해도 엔진이 마지막 오디오 버퍼를 마저 처리하며
        // onresult를 한 번 더 비동기로 발생시킬 수 있다. 이미 듣기를
        // 마치고(수동 즉시 전달 등) 다음 단계로 넘어간 뒤라면 이 뒤늦은
        // 이벤트가 상태 문구를 "듣고 있어요..."로 되돌리지 않도록 무시한다.
        if (!javisListening) return;
        let interim = "";
        let finalDelta = "";
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const t = e.results[i][0].transcript;
          if (e.results[i].isFinal) finalDelta += t;
          else interim += t;
        }
        if (finalDelta) javisFinalBuffer += finalDelta;
        javisLastInterim = interim;
        // 인식 중인 텍스트를 실시간으로 캡션에 반영한다(확정분 + 진행 중인 부분).
        const combined = (javisFinalBuffer + interim).trim();
        if (combined) {
          javisCaption.textContent = combined;
          javisStatus.textContent = "듣고 있어요...";
        }
        // 결과가 확정되어도 곧바로 전송하지 않는다. 이 시점부터 다시
        // JAVIS_SILENCE_MS 만큼 새로운 음성 입력 없이 조용해야 비로소
        // "말이 끝났다"고 보고 전송한다 — 문장 사이 짧은 텀에 끊기는
        // 문제를 방지한다.
        scheduleJavisAutoSend();
      };
      javisRecognition.onerror = (e) => {
        const err = e && e.error;
        clearJavisSilenceTimer();
        javisFinalBuffer = "";
        javisLastInterim = "";
        stopJavisListening();
        // 원인별로 구체적인 메시지를 보여준다. 콘솔에도 원본 에러 코드를 남겨
        // 진단이 가능하게 한다 (브라우저 개발자 도구 > Console 탭에서 확인 가능).
        console.warn("[JAVIS] speech recognition error:", err, e);
        switch (err) {
          case "no-speech":
            javisStatus.textContent = "음성이 감지되지 않았어요. 마이크를 눌러 다시 말씀해주세요.";
            break;
          case "audio-capture":
            javisStatus.textContent = "마이크를 찾을 수 없습니다. 장치 연결을 확인해주세요.";
            break;
          case "not-allowed":
          case "permission-denied":
            javisStatus.textContent = "마이크 권한이 차단되어 있습니다. 브라우저 주소창의 마이크 아이콘에서 허용해주세요.";
            break;
          case "network":
            javisStatus.textContent =
              "음성 인식 서비스에 연결하지 못했습니다. 인터넷 연결을 확인하거나, 이 화면을 Chrome/Edge 브라우저에서 열어보세요.";
            break;
          case "service-not-allowed":
            javisStatus.textContent = "이 브라우저(또는 내장 뷰)에서는 음성 인식 서비스가 차단되어 있습니다.";
            break;
          case "aborted":
            javisStatus.textContent = "마이크를 눌러 대화를 시작하세요";
            break;
          default:
            javisStatus.textContent = `음성 인식 오류: ${err || "알 수 없음"}`;
        }
      };
      javisRecognition.onend = () => {
        if (!javisListening) return;
        // 브라우저가 (continuous 설정과 무관하게) 자체적으로 세션을 끝내는
        // 경우에도, 그때까지 인식된 내용이 있다면 그냥 버리지 않고 바로
        // 전송해 사용자가 한 말이 사라지지 않게 한다.
        if ((javisFinalBuffer + javisLastInterim).trim()) {
          finalizeJavisListening();
        } else {
          stopJavisListening();
        }
      };
      return javisRecognition;
    }

    function clearJavisSilenceTimer() {
      if (javisSilenceTimer) {
        clearTimeout(javisSilenceTimer);
        javisSilenceTimer = null;
      }
    }

    function scheduleJavisAutoSend() {
      clearJavisSilenceTimer();
      javisSilenceTimer = setTimeout(() => {
        javisSilenceTimer = null;
        finalizeJavisListening();
      }, JAVIS_SILENCE_MS);
    }

    // 무음 대기 시간이 다 찼을 때(자동), 또는 마이크 버튼을 한 번 더 눌러
    // 사용자가 직접 요청했을 때(수동) 공통으로 호출되는 종료 지점.
    // 지금까지 인식된 내용을 확정해 전송하고 듣기 상태를 마친다.
    function finalizeJavisListening() {
      clearJavisSilenceTimer();
      const text = (javisFinalBuffer + javisLastInterim).trim();
      javisFinalBuffer = "";
      javisLastInterim = "";
      if (!javisListening) return;
      stopJavisListening();
      if (!text) return;
      javisCaption.textContent = text;
      sendJavisPrompt(text);
    }

    // 듣는 동안의 시각적 반응은 실제 마이크 스트림이 아니라 합성 펄스로
    // 만든다. SpeechRecognition이 내부적으로 마이크를 점유하는 도중 별도로
    // getUserMedia를 열면 일부 환경(OS/브라우저 조합)에서 오디오 입력이
    // 정상적으로 인식 엔진에 전달되지 않아 "no-speech" 오류가 발생할 수
    // 있기 때문이다.
    function pumpJavisListenPulse() {
      const tick = () => {
        if (!javisListening) return;
        const t = Date.now() / 1000;
        javisTargetEnergy = 0.16 + Math.abs(Math.sin(t * 2.1)) * 0.22 + Math.abs(Math.sin(t * 5.3)) * 0.08;
        javisListenRafId = requestAnimationFrame(tick);
      };
      tick();
    }

    // 버튼을 누르는 즉시 마이크 장치 접근 자체를 짧게 검증한다. 여기서 실패하면
    // (권한 차단, 장치 없음 등) "no-speech" 5초 타임아웃을 기다리지 않고 바로
    // 정확한 원인을 보여줄 수 있다. 스트림은 검증 직후 바로 해제하므로
    // SpeechRecognition의 마이크 점유와 충돌하지 않는다.
    async function checkJavisMicAccess() {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        return { ok: true }; // API 자체가 없으면 검증을 건너뛰고 인식만 시도
      }
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        stream.getTracks().forEach((t) => t.stop());
        return { ok: true };
      } catch (e) {
        console.warn("[JAVIS] mic pre-check failed:", e && e.name, e);
        return { ok: false, name: e && e.name };
      }
    }

    async function startJavisListening() {
      const rec = ensureJavisRecognition();
      if (!rec || javisListening) return;
      cancelJavisSpeech();
      clearJavisSilenceTimer();
      javisFinalBuffer = "";
      javisLastInterim = "";
      javisStatus.textContent = "마이크 확인 중...";
      javisCaption.textContent = "";

      const check = await checkJavisMicAccess();
      if (!check.ok) {
        switch (check.name) {
          case "NotAllowedError":
          case "SecurityError":
            javisStatus.textContent = "마이크 권한이 차단되어 있습니다. 주소창의 자물쇠 아이콘에서 마이크를 허용해주세요.";
            break;
          case "NotFoundError":
            javisStatus.textContent = "사용 가능한 마이크 장치를 찾을 수 없습니다. 연결 상태를 확인해주세요.";
            break;
          case "NotReadableError":
            javisStatus.textContent = "다른 프로그램이 마이크를 사용 중입니다. 다른 앱을 종료한 뒤 다시 시도해주세요.";
            break;
          default:
            javisStatus.textContent = `마이크 접근에 실패했습니다${check.name ? " (" + check.name + ")" : ""}.`;
        }
        return;
      }

      javisListening = true;
      javisMicBtn.classList.add("listening");
      javisMicBtn.classList.remove("speaking");
      javisStatus.textContent = "듣고 있어요...";
      pumpJavisListenPulse();
      try {
        rec.start();
      } catch (e) {
        console.warn("[JAVIS] failed to start recognition:", e);
        stopJavisListening();
      }
    }

    function stopJavisListening() {
      javisListening = false;
      clearJavisSilenceTimer();
      javisMicBtn.classList.remove("listening");
      if (javisRecognition) {
        try { javisRecognition.stop(); } catch (e) {}
      }
      if (javisListenRafId) cancelAnimationFrame(javisListenRafId);
      javisListenRafId = null;
      javisTargetEnergy = 0;
      if (!javisSpeaking) javisStatus.textContent = "마이크를 눌러 대화를 시작하세요";
    }

    javisMicBtn.addEventListener("click", () => {
      if (!SpeechRecognitionCtor) return;
      if (javisListening) {
        // 듣는 중에 마이크를 한 번 더 누르면, 무음 대기 시간을 기다리지
        // 않고 지금까지 인식된 내용을 즉시 확정해 전달한다.
        finalizeJavisListening();
      } else {
        startJavisListening();
      }
    });

    // ── 음성 합성 (AI 답변 → 음성) ──────────────────────────────
    // 세 가지를 개선한다:
    //  1) 지연 최소화 — 전체 답변이 끝날 때까지 기다리지 않고, 스트리밍
    //     도중 문장이 하나씩 완성될 때마다 바로바로 큐에 넣어 순차 재생한다.
    //  2) 목소리 성별/톤 — 기기에 설치된 한국어 음성 중 "남성" 계열을
    //     우선 탐색하고, 그중에서도 더 자연스러운(온라인/신경망 기반) 음성을
    //     우선한다.
    //  3) pitch/rate — 낮고 부드러운 저음역으로 톤을 낮추고, 로봇처럼
    //     또박또박 끊기지 않도록 속도를 자연스러운 대화 속도에 가깝게
    //     맞춘다. (브라우저 기본 TTS이므로 실제 성우 품질에는 한계가
    //     있다는 점은 감안해줘야 한다.)
    let javisSpeechQueue = [];
    let javisQueueProcessing = false;
    let javisCurrentUtterance = null;
    let javisSpeakPulseRafId = null;
    let javisCachedVoice = null;
    let javisVoicesReady = false;

    // 일반적으로 남성 음성에 쓰이는 이름 키워드 (OS/브라우저마다 표기가 다르다).
    const MALE_VOICE_KEYWORDS = [
      "male", "man", "injoon", "in-joon", "인준", "minsu", "민수",
      "jinho", "진호", "david", "daniel", "james", "junho", "준호",
      "hyunsu", "현수", "gyeongsu", "경수",
    ];
    // 흔히 여성 음성으로 알려진 이름들은 명시적으로 배제해 오탐을 줄인다.
    const FEMALE_VOICE_KEYWORDS = [
      "female", "woman", "yuna", "유나", "heami", "해미", "seoyeon", "서연", "jimin", "지민",
    ];

    function pickJavisVoice() {
      if (!hasSpeechSynthesis) return null;
      const voices = window.speechSynthesis.getVoices();
      if (!voices || !voices.length) return null;
      javisVoicesReady = true;
      const koVoices = voices.filter((v) => v.lang && v.lang.toLowerCase().startsWith("ko"));
      const pool = koVoices.length ? koVoices : voices;

      const isFemale = (name) => FEMALE_VOICE_KEYWORDS.some((kw) => name.includes(kw));
      const isMale = (name) => MALE_VOICE_KEYWORDS.some((kw) => name.includes(kw));
      // 기기 내장 저품질 오프라인 음성("Compact" 등)은 뚝뚝 끊기고 기계적으로
      // 들려 "부드러운" 목소리와는 거리가 멀다 — 대안이 있으면 후순위로 미룬다.
      const isLowQuality = (name) => name.includes("compact");

      const malePool = pool.filter((v) => isMale(v.name.toLowerCase()));
      const nonFemalePool = pool.filter((v) => !isFemale(v.name.toLowerCase()));

      // 낮고 부드러운 남성 목소리를 최우선으로 찾되, 그중에서도 더 자연스럽고
      // 매끄러운(온라인/신경망 기반) 고품질 음성을 우선한다.
      const preferredKeywords = [
        "neural", "natural", "premium", "online", "google", "enhanced",
        "wavenet", "studio", "multilingual", "hd",
      ];
      for (const candidatePoolRaw of [malePool, nonFemalePool, pool]) {
        if (!candidatePoolRaw.length) continue;
        const highQuality = candidatePoolRaw.filter((v) => !isLowQuality(v.name.toLowerCase()));
        const candidatePool = highQuality.length ? highQuality : candidatePoolRaw;
        for (const kw of preferredKeywords) {
          const found = candidatePool.find((v) => v.name.toLowerCase().includes(kw));
          if (found) return found;
        }
        return candidatePool[0];
      }
      return pool[0];
    }
    if (hasSpeechSynthesis) {
      const JAVIS_VOICE_PREF_KEY = "javis-voice-pref";
      let javisVoiceManuallySet = false;
      let javisVoiceOrdered = [];
      let javisVoiceOpen = false;
      const javisVoiceKey = (v) => `${v.name}::${v.lang}`;
      const loadJavisVoicePref = () => {
        try { return localStorage.getItem(JAVIS_VOICE_PREF_KEY) || ""; } catch (e) { return ""; }
      };
      const saveJavisVoicePref = (v) => {
        try { localStorage.setItem(JAVIS_VOICE_PREF_KEY, javisVoiceKey(v)); } catch (e) {}
      };

      function setJavisVoiceOpen(open) {
        javisVoiceOpen = open;
        if (javisVoiceDropdown) javisVoiceDropdown.classList.toggle("open", open);
        if (javisVoiceTrigger) javisVoiceTrigger.setAttribute("aria-expanded", open ? "true" : "false");
      }

      function applyJavisVoiceSelection(key) {
        const voice = javisVoiceOrdered.find((v) => javisVoiceKey(v) === key);
        if (!voice || !javisVoiceList) return;
        javisCachedVoice = voice;
        if (javisVoiceTriggerLabel) javisVoiceTriggerLabel.textContent = voice.name;
        javisVoiceList.querySelectorAll(".javis-voice-option").forEach((li) => {
          li.classList.toggle("selected", li.dataset.key === key);
        });
      }

      function selectJavisVoice(key) {
        applyJavisVoiceSelection(key);
        const voice = javisVoiceOrdered.find((v) => javisVoiceKey(v) === key);
        if (voice) {
          javisVoiceManuallySet = true;
          saveJavisVoicePref(voice);
        }
        setJavisVoiceOpen(false);
      }

      // 브라우저 기본 <select> 목록은 OS마다 제각각으로 렌더링돼 앱 디자인과
      // 어울리지 않는다. 대신 앱과 같은 톤의 커스텀 리스트를 직접 그린다.
      // 자동 판별(이름 키워드 기반)은 기기에 남성 목소리가 아예 설치돼
      // 있지 않으면 소용이 없으므로, 설치된 목록을 그대로 보여주고 사용자가
      // 직접 고를 수 있게 하는 것이 가장 확실한 해결책이다.
      function populateJavisVoiceOptions() {
        if (!javisVoiceList || !javisVoiceRow || !javisVoiceDropdown) return;
        const voices = window.speechSynthesis.getVoices();
        if (!voices || !voices.length) return;

        const koVoices = voices.filter((v) => v.lang && v.lang.toLowerCase().startsWith("ko"));
        const otherVoices = voices.filter((v) => !(v.lang && v.lang.toLowerCase().startsWith("ko")));
        javisVoiceOrdered = [...koVoices, ...otherVoices];
        const autoPicked = pickJavisVoice();
        const savedKey = loadJavisVoicePref();

        javisVoiceList.innerHTML = "";
        javisVoiceOrdered.forEach((v) => {
          const key = javisVoiceKey(v);
          const isAuto = autoPicked && key === javisVoiceKey(autoPicked);
          const li = document.createElement("li");
          li.className = "javis-voice-option";
          li.dataset.key = key;
          li.setAttribute("role", "option");
          li.innerHTML =
            `<svg class="javis-voice-option-check" viewBox="0 0 24 24" fill="none"><path d="M5 13l4 4L19 7" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>` +
            `<span class="javis-voice-option-label" style="overflow:hidden;text-overflow:ellipsis;flex:1;">${v.name} (${v.lang})</span>` +
            (isAuto ? `<span class="javis-voice-option-tag">추천</span>` : "");
          li.addEventListener("click", () => selectJavisVoice(key));
          javisVoiceList.appendChild(li);
        });

        const selectedKey =
          savedKey && javisVoiceOrdered.some((v) => javisVoiceKey(v) === savedKey)
            ? savedKey
            : autoPicked
              ? javisVoiceKey(autoPicked)
              : javisVoiceOrdered[0] && javisVoiceKey(javisVoiceOrdered[0]);
        if (selectedKey) {
          if (savedKey && selectedKey === savedKey) javisVoiceManuallySet = true;
          applyJavisVoiceSelection(selectedKey);
        }

        javisVoiceRow.style.display = javisVoiceOrdered.length ? "flex" : "none";
        // 고를 수 있는 목소리가 둘 이상일 때만 드롭다운을 열 수 있게 한다.
        javisVoiceDropdown.classList.toggle("disabled", javisVoiceOrdered.length <= 1);
      }

      if (javisVoiceTrigger) {
        javisVoiceTrigger.addEventListener("click", () => {
          if (javisVoiceOrdered.length <= 1) return;
          setJavisVoiceOpen(!javisVoiceOpen);
        });
      }
      document.addEventListener("click", (e) => {
        if (!javisVoiceOpen) return;
        if (javisVoiceDropdown && !javisVoiceDropdown.contains(e.target)) setJavisVoiceOpen(false);
      });
      document.addEventListener("keydown", (e) => {
        if (javisVoiceOpen && e.key === "Escape") setJavisVoiceOpen(false);
      });

      populateJavisVoiceOptions();
      window.speechSynthesis.onvoiceschanged = () => {
        // 브라우저가 목소리를 비동기로 계속 추가할 수 있어 이 이벤트가 여러
        // 번 호출될 수 있다. 사용자가 이미 직접 골랐다면 선택은 건드리지
        // 않고 목록만 최신 상태로 맞춘다.
        if (javisVoiceManuallySet) {
          const voices = window.speechSynthesis.getVoices();
          const stillThere = voices.find(
            (v) => javisCachedVoice && javisVoiceKey(v) === javisVoiceKey(javisCachedVoice)
          );
          if (stillThere) javisCachedVoice = stillThere;
          return;
        }
        populateJavisVoiceOptions();
      };
    }

    // ── 음성으로 읽지 않을 요소 제거(이모지 · 코드 · 마크다운 기호) ──
    // TTS는 순수한 자연어 문장만 읽어야 자연스럽다. 코드 블록/인라인 코드는
    // 내용째 제거하고, 마크다운 장식 기호(#, **, `, >, - 등)는 기호만 걷어내고
    // 안의 텍스트는 남기며, 이모지/그림문자는 통째로 지운다.
    const JAVIS_EMOJI_RE = /\p{Extended_Pictographic}|\p{Emoji_Presentation}|[\u{1F1E6}-\u{1F1FF}]|[\u200D\uFE0F\u20E3]/gu;

    function sanitizeJavisSpeechText(text) {
      if (!text) return "";
      let out = text;
      // 펜스 코드 블록은 자연어가 아니므로 내용째 제거한다.
      out = out.replace(/```[\s\S]*?```/g, " ");
      // 스트리밍 도중이라 닫는 펜스가 아직 도착하지 않은 경우, 짝이 안 맞는
      // 마지막 여는 펜스 이후는 통째로 잘라내 미완성 코드가 새어나가지 않게 한다.
      const danglingFence = out.lastIndexOf("```");
      if (danglingFence !== -1) out = out.slice(0, danglingFence);
      // 인라인 코드도 코드이므로 내용째 제거한다.
      out = out.replace(/`[^`]*`/g, " ");
      // 마크다운 장식 기호 — 기호만 걷어내고 안의 자연어 텍스트는 남긴다.
      out = out.replace(/^\s{0,3}#{1,6}\s+/gm, "");
      out = out.replace(/^\s{0,3}>\s?/gm, "");
      out = out.replace(/^\s{0,3}[-*+]\s+/gm, "");
      out = out.replace(/^\s{0,3}\d+[.)]\s+/gm, "");
      out = out.replace(/^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/gm, "");
      out = out.replace(/\*\*([^*]+)\*\*/g, "$1");
      out = out.replace(/__([^_]+)__/g, "$1");
      out = out.replace(/\*([^*\n]+)\*/g, "$1");
      out = out.replace(/(^|[^\w])_([^_\n]+)_(?!\w)/g, "$1$2");
      out = out.replace(/~~([^~]+)~~/g, "$1");
      out = out.replace(/!\[[^\]]*\]\([^)]*\)/g, ""); // 이미지는 통째로 제거
      out = out.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1"); // 링크는 텍스트만 남김
      out = out.replace(/https?:\/\/\S+/g, ""); // 남은 순수 URL은 읽지 않는다
      // 이모지/그림문자 제거.
      out = out.replace(JAVIS_EMOJI_RE, "");
      // 정리 후 남는 공백을 다듬는다.
      out = out.replace(/[ \t]{2,}/g, " ");
      out = out.replace(/\n{3,}/g, "\n\n");
      out = out.replace(/[ \t]+([.,!?])/g, "$1"); // 제거된 기호 자리에 남은 공백 뒤의 문장부호 정리
      return out;
    }

    // ── 캡션 부드럽게 전환하기 ─────────────────────────────────────
    // 문장이 통째로 뚝 바뀌면 끊기는 느낌이 든다. 텍스트를 교체하기 전에
    // 살짝 페이드아웃 → 교체 → 페이드인 시켜, 음성이 다음 문장으로 넘어가는
    // 리듬과 자연스럽게 맞물리도록 한다. (CSS 쪽 .javis-caption.swapping 과 쌍)
    let javisCaptionSwapTimer = null;
    function setJavisCaption(text, immediate) {
      if (javisCaptionSwapTimer) {
        clearTimeout(javisCaptionSwapTimer);
        javisCaptionSwapTimer = null;
      }
      if (immediate) {
        javisCaption.classList.remove("swapping");
        javisCaption.textContent = text;
        return;
      }
      javisCaption.classList.add("swapping");
      javisCaptionSwapTimer = setTimeout(() => {
        javisCaption.textContent = text;
        javisCaption.classList.remove("swapping");
        javisCaptionSwapTimer = null;
      }, 160);
    }

    // 스트리밍 중인 전체 텍스트에서, 아직 큐에 넣지 않은 구간(fromIndex 이후) 중
    // 문장이 종결부호(. ! ? 개행)로 완결된 부분만 잘라 반환한다. 문장이 끝났는지
    // 애매한 마지막 꼬리는 다음 델타에서 이어서 판단하도록 남겨둔다.
    function extractJavisSentences(fullText, fromIndex) {
      const tail = fullText.slice(fromIndex);
      const sentences = [];
      let consumed = 0;
      let cursor = 0;
      for (let i = 0; i < tail.length; i++) {
        if (/[.!?\n]/.test(tail[i])) {
          const next = tail[i + 1];
          if (next === undefined || /\s/.test(next)) {
            const piece = tail.slice(cursor, i + 1).trim();
            if (piece) sentences.push(piece);
            consumed = i + 1;
            cursor = i + 1;
          }
        }
      }
      return { sentences, consumedUpto: fromIndex + consumed };
    }

    function pumpJavisSpeakPulse() {
      const tick = () => {
        if (!javisSpeaking) return;
        const t = Date.now() / 1000;
        javisTargetEnergy =
          0.32 + Math.abs(Math.sin(t * 6.4)) * 0.48 + Math.abs(Math.sin(t * 14.1)) * 0.16;
        javisSpeakPulseRafId = requestAnimationFrame(tick);
      };
      tick();
    }

    function enqueueJavisSpeech(text) {
      if (!hasSpeechSynthesis || !text) return;
      javisSpeechQueue.push(text);
      if (!javisQueueProcessing) processJavisSpeechQueue();
    }

    function processJavisSpeechQueue() {
      if (!javisSpeechQueue.length) {
        javisQueueProcessing = false;
        javisSpeaking = false;
        javisMicBtn.classList.remove("speaking");
        if (javisSpeakPulseRafId) cancelAnimationFrame(javisSpeakPulseRafId);
        javisSpeakPulseRafId = null;
        javisTargetEnergy = 0;
        if (!javisListening) javisStatus.textContent = "마이크를 눌러 대화를 시작하세요";
        return;
      }
      javisQueueProcessing = true;
      if (!javisSpeaking) {
        javisSpeaking = true;
        javisMicBtn.classList.add("speaking");
        javisStatus.textContent = "말하는 중...";
        pumpJavisSpeakPulse();
      }
      const chunk = javisSpeechQueue.shift();
      const utter = new SpeechSynthesisUtterance(chunk);
      utter.lang = "ko-KR";
      const voice = javisCachedVoice || pickJavisVoice();
      if (voice) utter.voice = voice;
      // 낮고 부드러우며 현실적인 남성 목소리에 가깝도록 톤을 튜닝한다.
      // pitch는 너무 극단적으로 낮추면 오히려 갈라지고 기계적으로 들려
      // "부드러움"과는 멀어지므로, 저음역은 유지하되 자연스럽게 들리는
      // 지점으로 살짝 올렸다. rate는 답변 템포가 느리다는 피드백을 반영해
      // 또박또박한 정속(1.0)보다 확실히 빠르게 — 서두르지 않으면서도
      // 답답하지 않은 속도로 맞춘다.
      utter.pitch = 0.82;
      utter.rate = 1.18;
      utter.volume = 1;
      // 캡션은 이 문장이 실제로 "말해지기 시작하는" 순간에 맞춰 갱신한다.
      // 네트워크로 텍스트가 먼저 도착해도 화면에는 바로 띄우지 않고 큐에만
      // 쌓아뒀다가, 음성이 그 문장을 말하기 시작할 때 비로소 보여주는
      // 것 — 이렇게 하면 텍스트가 음성보다 앞서지 않고 항상 동기화되며,
      // 화면에는 지금 말하는 문장 하나만 남아 화면 밖으로 넘치지 않는다.
      utter.onstart = () => {
        setJavisCaption(chunk);
      };
      utter.onboundary = () => {
        javisTargetEnergy = Math.min(1, javisTargetEnergy + 0.22);
      };
      const advance = () => {
        javisCurrentUtterance = null;
        processJavisSpeechQueue();
      };
      utter.onend = advance;
      utter.onerror = advance;
      javisCurrentUtterance = utter;
      window.speechSynthesis.speak(utter);
    }

    function cancelJavisSpeech() {
      javisSpeechQueue = [];
      javisQueueProcessing = false;
      javisCurrentUtterance = null;
      if (hasSpeechSynthesis) window.speechSynthesis.cancel();
      javisSpeaking = false;
      javisMicBtn.classList.remove("speaking");
      if (javisSpeakPulseRafId) cancelAnimationFrame(javisSpeakPulseRafId);
      javisSpeakPulseRafId = null;
      javisTargetEnergy = 0;
    }

    // ── 추론 스트리밍 / 코드 작성 중 표시 (답변 펄스와는 다른 효과) ──
    // 실제 발화(TTS)가 시작되기 전, 서버가 추론 텍스트를 스트리밍하거나
    // 코드 블록을 작성하는 동안에는 구체가 목소리 리듬이 아니라 불규칙한
    // 난류로 꿈틀거리게 해 "생각/작업 중"임을 시각적으로 구분해 보여준다.
    function setJavisProcessing(active, label) {
      javisTargetProcessing = active ? 1 : 0;
      javisMicBtn.classList.toggle("processing", active);
      if (active && label) {
        javisStatus.textContent = label;
      } else if (!active && !javisListening && !javisSpeaking) {
        javisStatus.textContent = "마이크를 눌러 대화를 시작하세요";
      }
    }

    // ── 프롬프트 전송 (Direct 모드의 SSE 파이프라인 재사용) ───────
    // 기존 sendPrompt/handleStreamEvent는 그대로 두고, JAVIS 뷰에서만
    // 쓰는 별도 전송 함수를 둔다. handleStreamEvent를 그대로 호출하므로
    // 채팅 이력(사이드바 전환 시 Direct 모드)에도 동일하게 반영된다.
    async function sendJavisPrompt(text) {
      if (!text || isSending) return;
      cancelJavisSpeech();
      setSending(true);
      setJavisProcessing(true, "생각하는 중...");
      setJavisCaption("", true); // 이전 답변 캡션이 다음 턴까지 남아있지 않도록 즉시 비운다.
      appendUserMessage(text);
      currentAssistantEls = createAssistantMessage();

      let javisFullText = "";
      let javisSanitizedText = ""; // 이모지·코드·마크다운 기호를 걷어낸, 실제로 읽고 보여줄 텍스트
      let javisSpokenUpto = 0; // 이미 TTS 큐에 넣은 부분까지의 인덱스 (javisSanitizedText 기준)

      try {
        const res = await fetch("/api/send", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ prompt: text }),
        });

        if (!res.ok || !res.body) {
          const errData = await res.json().catch(() => ({}));
          hideThinkingOrb(currentAssistantEls);
          setJavisProcessing(false);
          const msg = `오류가 발생했습니다: ${errData.error || res.statusText}`;
          updateAssistantText(currentAssistantEls, msg, true);
          setJavisCaption(msg, true);
          javisStatus.textContent = "오류가 발생했습니다";
          setSending(false);
          currentAssistantEls = null;
          return;
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          let sepIdx;
          while ((sepIdx = buffer.indexOf("\n\n")) !== -1) {
            const rawEvent = buffer.slice(0, sepIdx);
            buffer = buffer.slice(sepIdx + 2);
            const line = rawEvent.split("\n").find((l) => l.startsWith("data:"));
            if (!line) continue;
            const payload = JSON.parse(line.slice(5).trim());
            handleStreamEvent(payload);

            // 추론 텍스트가 스트리밍 중이거나(is_done이 아직 false) 코드
            // 블록이 한창 작성되는 중이면, 실제 답변을 말하기 전까지는
            // "처리 중" 유기적 효과를 유지한다. 둘 다 없는 델타(=실제 답변
            // 본문이 오는 중)라면 이 델타 자체로는 끄지 않고, 아래에서
            // 실제로 말할 문장이 큐에 들어가는 순간에만 끈다 — 그래야 답변
            // 첫 글자가 오자마자 바로 다음 델타를 기다리지 않고 자연스럽게
            // 발화 펄스로 넘어간다.
            if (payload.type === "delta") {
              if (payload.reasoning && !payload.reasoning.is_done) {
                setJavisProcessing(true, "추론하는 중...");
              } else if (payload.code_blocks && visibleCodeBlocks(payload.code_blocks).length && !payload.done) {
                setJavisProcessing(true, "코드를 작성하는 중...");
              }
            }

            if (payload.type === "delta" || payload.type === "final") {
              if (typeof payload.plain_text === "string") {
                javisFullText = payload.plain_text;
                javisSanitizedText = sanitizeJavisSpeechText(javisFullText);
                if (!hasSpeechSynthesis) {
                  // 음성 합성이 없는 환경에선 캡션이 유일한 답변 표시 수단이므로
                  // 원문을 그대로 바로 보여준다(코드/이모지 제거는 TTS 전용 처리).
                  setJavisCaption(javisFullText, true);
                }
                // 문장이 완성되는 즉시 큐에 넣어, 전체 응답이 끝나기 전부터
                // 말하기 시작하게 한다. 이때 넘기는 텍스트는 이모지·코드·마크
                // 다운 기호를 걷어낸 sanitized 버전이라 자연어만 읽힌다. 캡션
                // 자체는 여기서 갱신하지 않는다 — 각 문장이 실제로 재생되기
                // 시작할 때(onstart) 갱신되므로, 텍스트가 음성보다 앞서 나가는
                // 일 없이 항상 동기화된다.
                const { sentences, consumedUpto } = extractJavisSentences(javisSanitizedText, javisSpokenUpto);
                if (sentences.length) {
                  // 실제로 읽을 문장이 생겼다는 것은 "생각/작업 중" 단계가
                  // 끝나고 답변 단계로 넘어갔다는 뜻이므로 처리 중 효과를 끈다.
                  setJavisProcessing(false);
                  sentences.forEach((s) => enqueueJavisSpeech(s));
                }
                javisSpokenUpto = consumedUpto;
              }
            }
            if (payload.type === "final") {
              // 문장부호 없이 끝난 마지막 꼬리도 마저 읽는다.
              const rest = javisSanitizedText.slice(javisSpokenUpto).trim();
              if (rest) enqueueJavisSpeech(rest);
              javisSpokenUpto = javisSanitizedText.length;
              setJavisProcessing(false);
            } else if (payload.type === "error") {
              setJavisProcessing(false);
              setJavisCaption(`오류가 발생했습니다: ${payload.message}`, true);
              javisStatus.textContent = "오류가 발생했습니다";
            }
          }
        }
      } catch (e) {
        hideThinkingOrb(currentAssistantEls);
        setJavisProcessing(false);
        updateAssistantText(currentAssistantEls, `연결 오류: ${e}`, true);
        setJavisCaption(`연결 오류: ${e}`, true);
        javisStatus.textContent = "오류가 발생했습니다";
      } finally {
        setSending(false);
        currentAssistantEls = null;
      }
    }
  })();


  // ── 초기화 ─────────────────────────────────────────────────────
  applyModeToUi();   // 채팅 모드(Direct/Agent)에 맞춰 화면을 먼저 맞춘다
  loadHistory();
  pollState();
  setInterval(pollState, 2000);
})();