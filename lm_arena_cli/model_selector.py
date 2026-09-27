"""
LM Arena CLI - 모델 선택(Model selector)

이 모듈은 완전히 새로 추가되는 기능이다. browser.py / ui.py / cli.py /
file_writer.py / prompt_augment.py 등에 있던 기존 로직(로그인 판정, 프롬프트
전송, 스트리밍/응답 완료 판정, 코드 블록 파싱)은 단 한 줄도 건드리지 않는다.
여기서는 LMArenaBrowser가 이미 들고 있는 Playwright Page를 "받아서" 다음
세 가지만 수행한다.

  1) read_current_model(page)  현재 선택된 모델 이름/아이콘 읽기 (DOM 읽기만 함)
  2) scrape_models(page)       LM Arena의 모델 선택 창을 열어 전체 모델 목록과
                               각 모델의 lab 아이콘을 추출
  3) select_model(page, name)  LM Arena의 실제 모델 변경 흐름 그대로 모델 변경

[LM Arena의 모델 변경 흐름 (이 모듈이 그대로 따라 한다)]
  - 프롬프트 입력창 하단의 모델 버튼(aria-haspopup="dialog")을 클릭 ->
    Radix Popover(dialog) 안에 cmdk 커맨드 팔레트가 열린다.
  - 검색창(input[cmdk-input], placeholder="Search models")에 입력하면 목록이
    걸러지고, 항목(cmdk-item)을 클릭하면 모델이 바뀐다.
  - 모델이 바뀌면 URL이 ".../text/direct?model_a=<모델이름>" 으로 바뀐다.
  UI 조작이 실패했을 때에 한해, 마지막 수단으로 이 URL 형식으로 직접 이동한다
  (이 경우 LM Arena 대화가 새로 시작되므로 결과에 reset=True로 표시된다).

[아이콘]
  모델 항목/버튼 안의 lab 아이콘은 <svg> 이고, 색이 fill="hsl(var(--interactive-cta))"
  처럼 LM Arena 페이지의 CSS 변수에 의존한다. 그대로 복사하면 우리 UI에서는
  색이 깨지므로, LM Arena 페이지 안에서 getComputedStyle로 "실제 색"을 해석해
  자기완결적인 SVG 문자열로 바꿔서 가져온다. 이때 태그/속성 화이트리스트로
  다시 직렬화하므로 스크립트나 외부 참조는 포함되지 않는다.
  (가정: 자동화 브라우저의 LM Arena가 라이트 테마. Playwright 기본값이 라이트다.)

Playwright sync API는 만든 스레드에서만 써야 하므로, 이 모듈의 모든 함수는
webapp.AutomationWorker의 워커 스레드에서만 호출해야 한다.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, quote, urlparse

from playwright.sync_api import Page

from . import dom_selectors as sel
from .config import DIRECT_MODE_URL, HOME_DIR

MODELS_CACHE_FILE = HOME_DIR / "models_cache.json"
# 캐시가 이 시간보다 오래되면 다음 목록 요청 때 자동으로 다시 긁어온다.
CACHE_MAX_AGE_SECONDS = 24 * 60 * 60


class ModelSelectorError(Exception):
    """모델 선택 UI 조작 중 발생한 예상 가능한 오류(DOM 변경 포함)."""


@dataclass
class ModelInfo:
    name: str
    search: str          # 항목에 보이는 전체 텍스트(검색용, 소문자 처리는 프런트에서)
    icon_svg: Optional[str]


@dataclass
class SelectResult:
    ok: bool
    current: Optional[dict]   # {"name", "icon_svg", "slug"} 또는 None
    reset: bool               # URL 이동 폴백으로 LM Arena 대화가 새로 시작됐는가
    error: Optional[str]


# ── 페이지 안에서 실행되는 JS ────────────────────────────────────────
# 공통 헬퍼: (1) 아이콘 svg를 "색이 해석된 안전한 SVG 문자열"로 직렬화,
# (2) 항목에서 모델 이름 추출.
_HELPERS_JS = r"""
const SVG_TAGS = new Set(["svg","g","path","rect","circle","ellipse","line","polyline","polygon",
  "defs","clippath","lineargradient","radialgradient","stop","mask"]);
const SVG_ATTRS = new Set(["viewbox","width","height","x","y","cx","cy","r","rx","ry","d","points",
  "x1","y1","x2","y2","fill","stroke","stroke-width","stroke-linecap","stroke-linejoin",
  "stroke-miterlimit","stroke-dasharray","stroke-dashoffset","opacity","fill-opacity",
  "stroke-opacity","fill-rule","clip-rule","transform","id","clip-path","mask","offset",
  "stop-color","stop-opacity","gradientunits","gradienttransform","fx","fy","clippathunits",
  "maskunits","maskcontentunits","preserveaspectratio"]);
const COLOR_ATTRS = ["fill","stroke","stop-color"];
const NEEDS_RESOLVE = /var\(|currentcolor/i;

function esc(v) {
  return String(v).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

function svgToString(el, isRoot) {
  const tag = el.localName;
  if (!SVG_TAGS.has(tag.toLowerCase())) return "";
  const cs = getComputedStyle(el);
  const attrs = {};
  for (const a of Array.from(el.attributes)) {
    if (!SVG_ATTRS.has(a.name.toLowerCase())) continue;
    // 외부 리소스 참조(url(http...))는 버리고, 문서 내부 참조(url(#id))만 허용한다.
    if (/url\(/i.test(a.value) && !/url\(\s*['"]?#/.test(a.value)) continue;
    attrs[a.name] = a.value;
  }
  // LM Arena의 CSS 변수 / currentColor / class로 정해진 색을 실제 색으로 고정한다.
  const styled = el.hasAttribute("style") || (el.getAttribute("class") || "") !== "";
  for (const prop of COLOR_ATTRS) {
    if (prop === "stop-color" && tag.toLowerCase() !== "stop") continue;
    const raw = attrs[prop];
    if ((raw !== undefined && NEEDS_RESOLVE.test(raw)) || (raw === undefined && styled)) {
      const v = cs.getPropertyValue(prop);
      if (v) attrs[prop] = v;
    }
  }
  if (isRoot) {
    attrs["xmlns"] = "http://www.w3.org/2000/svg";
    if (!attrs["viewBox"]) {
      const w = parseFloat(attrs["width"]) || 24;
      const h = parseFloat(attrs["height"]) || 24;
      attrs["viewBox"] = "0 0 " + w + " " + h;
    }
    attrs["width"] = "24";   // 실제 표시 크기는 우리 CSS가 정한다(em 단위 등 방지).
    attrs["height"] = "24";
  }
  let inner = "";
  for (const child of Array.from(el.children)) inner += svgToString(child, false);
  const attrStr = Object.entries(attrs).map(([k, v]) => k + '="' + esc(v) + '"').join(" ");
  return "<" + tag + " " + attrStr + ">" + inner + "</" + tag + ">";
}

function extractIcon(container) {
  const svg = container.querySelector("svg");
  if (!svg) return null;
  const out = svgToString(svg, true);
  return out || null;
}

function labelOf(item) {
  const t = item.querySelector("span.truncate, [class*='truncate']");
  let text = ((t && t.textContent) || "").trim();
  if (!text) {
    const lines = (item.innerText || item.textContent || "")
      .split("\n").map((s) => s.trim()).filter(Boolean);
    text = lines[0] || "";
  }
  return text;
}
"""

_READ_BUTTON_JS = "(btn) => {" + _HELPERS_JS + r"""
  const t = btn.querySelector("span.truncate");
  const name = ((t && t.textContent) || btn.innerText || "").trim();
  return { name: name, icon: extractIcon(btn) };
}"""

_COLLECT_JS = "(args) => {" + _HELPERS_JS + r"""
  const out = [];
  for (const it of Array.from(document.querySelectorAll(args.itemSelector))) {
    if (it.getAttribute("aria-disabled") === "true" || it.getAttribute("data-disabled") === "true") continue;
    const label = labelOf(it);
    if (!label) continue;
    out.push({
      label: label,
      search: (it.innerText || it.textContent || "").replace(/\s+/g, " ").trim(),
      icon: extractIcon(it),
    });
  }
  return out;
}"""

_FIND_ITEM_JS = "(args) => {" + _HELPERS_JS + r"""
  const want = args.label.trim().toLowerCase().replace(/\s+/g, " ");
  const items = Array.from(document.querySelectorAll(args.itemSelector));
  for (let i = 0; i < items.length; i++) {
    const got = labelOf(items[i]).toLowerCase().replace(/\s+/g, " ");
    if (got === want) return i;
  }
  return -1;
}"""

# 목록의 스크롤 컨테이너를 찾아 스크롤한다. (지연 렌더링/가상 스크롤 대비)
#   mode "step"   : 한 화면 가까이 아래로
#   mode "bottom" : 맨 아래로 (아래로 더 불러오는 목록 / 전부 렌더링된 목록용)
#   mode "top"    : 맨 위로
_SCROLL_JS = r"""(args) => {
  const first = document.querySelector(args.itemSelector);
  if (!first) return { found: false, moved: false };
  let el = first.parentElement, scroller = null;
  while (el && el !== document.body) {
    const oy = getComputedStyle(el).overflowY;
    if ((oy === "auto" || oy === "scroll") && el.scrollHeight > el.clientHeight + 2) {
      scroller = el; break;
    }
    el = el.parentElement;
  }
  if (!scroller) return { found: false, moved: false };
  const before = scroller.scrollTop;
  if (args.mode === "top") scroller.scrollTop = 0;
  else if (args.mode === "bottom") scroller.scrollTop = scroller.scrollHeight;
  else scroller.scrollTop = before + Math.max(120, scroller.clientHeight * 0.85);
  return { found: true, moved: scroller.scrollTop !== before };
}"""


# ── 작은 유틸 ───────────────────────────────────────────────────────
def icon_id(svg: str) -> str:
    """SVG 내용 해시 -> 짧은 ID. 같은 lab 아이콘은 같은 ID가 되어 한 번만 저장된다."""
    return hashlib.sha1(svg.encode("utf-8")).hexdigest()[:12]


def _norm(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def _same(a: str, b: str) -> bool:
    return _norm(a) == _norm(b)


def _current_slug(page: Page) -> Optional[str]:
    try:
        values = parse_qs(urlparse(page.url).query).get(sel.MODEL_URL_PARAM)
    except Exception:
        return None
    return values[0] if values else None


def _guess_slug(name: str) -> str:
    return re.sub(r"\s+", "-", name.strip().lower())


# ── DOM 탐색 ────────────────────────────────────────────────────────
def _find_button(page: Page):
    """화면에 보이는 모델 변경 버튼 Locator. 없으면 ModelSelectorError."""
    for selector in sel.MODEL_SELECT_BUTTON:
        try:
            loc = page.locator(selector)
            for i in range(min(loc.count(), 4)):
                cand = loc.nth(i)
                if cand.is_visible():
                    return cand
        except Exception:
            continue
    raise ModelSelectorError(
        "모델 선택 버튼을 찾을 수 없습니다. 자동화 브라우저가 LM Arena Direct 채팅 화면인지 확인해주세요."
    )


def _search_input_visible(page: Page):
    for selector in sel.MODEL_SEARCH_INPUT:
        try:
            loc = page.locator(selector)
            if loc.count() > 0 and loc.first.is_visible():
                return loc.first
        except Exception:
            continue
    return None


def _open_dialog(page: Page):
    """모델 선택 창을 열고(이미 열려 있으면 그대로) 검색 입력창 Locator를 반환."""
    existing = _search_input_visible(page)
    if existing is not None:
        return existing

    btn = _find_button(page)
    for _ in range(2):
        # Radix Popover 트리거는 토글이라, 이미 열리는 중이면 다시 누르지 않는다.
        if btn.get_attribute("aria-expanded") != "true":
            btn.click()
        deadline = time.time() + 4.0
        while time.time() < deadline:
            inp = _search_input_visible(page)
            if inp is not None:
                return inp
            time.sleep(0.1)
    raise ModelSelectorError("모델 선택 창을 열지 못했습니다 (DOM 변경 가능성).")


def _close_dialog(page: Page) -> None:
    for _ in range(2):
        if _search_input_visible(page) is None:
            return
        try:
            page.keyboard.press("Escape")
        except Exception:
            return
        deadline = time.time() + 1.5
        while time.time() < deadline:
            if _search_input_visible(page) is None:
                return
            time.sleep(0.1)


def _wait_item_selector(page: Page, timeout: float = 5.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for selector in sel.MODEL_ITEM:
            try:
                if page.locator(selector).count() > 0:
                    return selector
            except Exception:
                continue
        time.sleep(0.1)
    raise ModelSelectorError("모델 목록 항목을 찾을 수 없습니다 (DOM 변경 가능성).")


# ── 1) 현재 모델 ────────────────────────────────────────────────────
def read_current_model(page: Page) -> Optional[dict]:
    """
    모델 버튼에 표시된 현재 모델. 창을 열지 않고 DOM만 읽는다.
    반환: {"name", "icon_svg"(없으면 None), "slug"(URL의 model_a, 없으면 None)}
    """
    btn = _find_button(page)
    data = btn.evaluate(_READ_BUTTON_JS)
    name = (data.get("name") or "").strip()
    if not name:
        return None
    return {"name": name, "icon_svg": data.get("icon"), "slug": _current_slug(page)}


# ── 2) 전체 모델 목록 ───────────────────────────────────────────────
def scrape_models(page: Page) -> list[ModelInfo]:
    """
    모델 선택 창을 열어 모든 항목(이름 + lab 아이콘)을 읽고 창을 닫는다.
    항목이 지연 렌더링/가상 스크롤이어도 놓치지 않도록 스크롤하며 모은다.
    """
    _open_dialog(page)
    try:
        item_sel = _wait_item_selector(page)
        found: dict[str, ModelInfo] = {}

        def merge() -> int:
            added = 0
            batch = page.evaluate(_COLLECT_JS, {"itemSelector": item_sel})
            for row in batch:
                key = _norm(row["label"])
                if key and key not in found:
                    found[key] = ModelInfo(
                        name=row["label"], search=row.get("search") or row["label"], icon_svg=row.get("icon")
                    )
                    added += 1
            return added

        def scroll(mode: str) -> dict:
            return page.evaluate(_SCROLL_JS, {"itemSelector": item_sel, "mode": mode})

        merge()
        if not found:
            raise ModelSelectorError("모델 목록을 읽지 못했습니다 (DOM 변경 가능성).")
        first_label = next(iter(found.values())).name

        # 1단계: 맨 아래로 내려가며 모은다. 전부 렌더링된 목록은 한 번에, 스크롤하면
        # 이어서 불러오는 목록은 더 안 늘어날 때까지 반복해서 끝난다.
        stall = 0
        for _ in range(60):
            if not scroll("bottom").get("found"):
                break
            time.sleep(0.25)
            stall = stall + 1 if merge() == 0 else 0
            if stall >= 2:
                break

        # 2단계: 맨 위 항목이 DOM에서 사라졌다면 가상 스크롤 목록이다. 이 경우엔 위에서부터
        # 한 화면씩 내려가며 보이는 항목을 모은다.
        if page.evaluate(_FIND_ITEM_JS, {"itemSelector": item_sel, "label": first_label}) < 0:
            scroll("top")
            time.sleep(0.2)
            stall = 0
            for _ in range(300):
                added = merge()
                sc = scroll("step")
                if not sc.get("found"):
                    break
                time.sleep(0.12)
                if sc.get("moved") or added:
                    stall = 0
                else:
                    stall += 1
                    if stall >= 2:
                        break
        models = list(found.values())
    finally:
        _close_dialog(page)

    if not models:
        raise ModelSelectorError("모델 목록을 읽지 못했습니다 (DOM 변경 가능성).")
    return models


# ── 3) 모델 변경 ────────────────────────────────────────────────────
def _click_model_item(page: Page, item_sel: str, target: str) -> bool:
    """
    검색창에 이름을 넣어 걸러본 뒤 정확히 같은 이름의 항목을 클릭한다. cmdk 검색은
    항목의 표시 이름이 아니라 내부 value로 거르므로, 그래도 못 찾으면 검색어를 지우고
    전체 목록을 스크롤하며 찾는다. (같은 이름이 목록에 없으면 False)
    """
    args = {"itemSelector": item_sel, "label": target}
    inp = _search_input_visible(page)
    for query in (target, ""):
        if inp is not None:
            try:
                inp.fill(query)
            except Exception:
                pass
            time.sleep(0.3)  # cmdk가 목록을 다시 거를 시간
        page.evaluate(_SCROLL_JS, {"itemSelector": item_sel, "mode": "top"})
        stall = 0
        for _ in range(300):
            idx = page.evaluate(_FIND_ITEM_JS, args)
            if idx >= 0:
                page.locator(item_sel).nth(idx).click()
                return True
            sc = page.evaluate(_SCROLL_JS, {"itemSelector": item_sel, "mode": "step"})
            if not sc.get("found"):
                break
            time.sleep(0.12)
            if sc.get("moved"):
                stall = 0
            else:
                stall += 1
                if stall >= 2:
                    break
    return False


def _wait_current(page: Page, target: str, before_slug: Optional[str], timeout: float = 6.0) -> Optional[dict]:
    """
    변경이 반영될 때까지 기다린다. 버튼의 표시 이름이 목표와 같거나, URL의
    model_a 값이 바뀌었으면(=LM Arena가 실제로 모델을 바꿈) 그 시점의 현재 모델을 반환.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            cur = read_current_model(page)
        except ModelSelectorError:
            cur = None
        if cur:
            if _same(cur["name"], target):
                return cur
            slug = cur.get("slug")
            if slug and slug != before_slug:
                return cur
        time.sleep(0.2)
    return None


def select_model(page: Page, name: str) -> SelectResult:
    target = (name or "").strip()
    if not target:
        return SelectResult(False, None, False, "모델 이름이 비어 있습니다.")

    before_slug = _current_slug(page)
    ui_error: Optional[str] = None
    ui_broken = False   # 버튼/창/목록 자체를 못 찾은 경우에만 URL 이동 폴백을 쓴다
    clicked = False

    try:
        current = read_current_model(page)
        if current and _same(current["name"], target):
            return SelectResult(True, current, False, None)

        _open_dialog(page)
        item_sel = _wait_item_selector(page)
        clicked = _click_model_item(page, item_sel, target)
        if not clicked:
            ui_error = f"목록에서 '{target}' 모델을 찾지 못했습니다."
    except ModelSelectorError as e:
        ui_error = str(e)
        ui_broken = True
    except Exception as e:  # noqa: BLE001
        ui_error = f"모델 변경 중 오류: {e}"
        ui_broken = True
    finally:
        _close_dialog(page)

    if clicked:
        cur = _wait_current(page, target, before_slug)
        if cur:
            return SelectResult(True, cur, False, None)
        # 클릭은 했지만 반영을 확인하지 못했다. 이 경우 대화를 날리는 URL 이동은
        # 하지 않는다(실제로는 바뀌었을 수도 있으므로).
        return SelectResult(False, _safe_current(page), False, "모델 변경을 확인하지 못했습니다. 잠시 후 다시 확인해주세요.")

    if not ui_broken:
        # 목록은 정상적으로 열렸는데 그 모델이 없다 -> URL로 이동해도 소용없으므로
        # 대화를 날리지 않고 그대로 실패로 알린다.
        return SelectResult(False, _safe_current(page), False, ui_error)

    # 마지막 수단: 모델 선택 UI 자체를 쓸 수 없을 때, LM Arena URL 형식
    # (?model_a=<이름>)으로 직접 이동한다. 대화가 새로 시작된다.
    try:
        slug = _guess_slug(target)
        page.goto(
            f"{DIRECT_MODE_URL}?{sel.MODEL_URL_PARAM}={quote(slug)}", wait_until="domcontentloaded"
        )
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        time.sleep(1.0)
        cur = _wait_current(page, target, before_slug=None)
        if cur and _same(cur["name"], target):
            return SelectResult(True, cur, True, None)
        return SelectResult(False, _safe_current(page), True, ui_error or "모델을 변경하지 못했습니다.")
    except Exception as e:  # noqa: BLE001
        return SelectResult(False, _safe_current(page), False, f"{ui_error or '모델을 변경하지 못했습니다.'} ({e})")


def _safe_current(page: Page) -> Optional[dict]:
    try:
        return read_current_model(page)
    except Exception:
        return None


# ── 캐시 (모델 목록/아이콘을 디스크에 저장해 재시작 후에도 바로 표시) ──────────
def load_cache() -> Optional[dict]:
    """{"fetched_at", "models":[{name,search,icon}], "icons":{id:svg}} 또는 None."""
    try:
        data = json.loads(MODELS_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data.get("models"), list) and isinstance(data.get("icons"), dict):
            return data
    except Exception:
        pass
    return None


def save_cache(models: list[dict], icons: dict[str, str], fetched_at: float) -> None:
    try:
        HOME_DIR.mkdir(parents=True, exist_ok=True)
        used = {m["icon"] for m in models if m.get("icon")}
        MODELS_CACHE_FILE.write_text(
            json.dumps(
                {
                    "fetched_at": fetched_at,
                    "models": models,
                    "icons": {k: v for k, v in icons.items() if k in used},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass  # 캐시는 편의 기능일 뿐이라 저장 실패가 기능 자체를 막지 않게 한다.


def cache_is_fresh(cache: Optional[dict]) -> bool:
    if not cache:
        return False
    try:
        return (time.time() - float(cache.get("fetched_at", 0))) < CACHE_MAX_AGE_SECONDS
    except Exception:
        return False
