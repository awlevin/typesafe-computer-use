"""Perception from the DOM instead of from pixels.

Replaces: screencapture -> Apple Vision OCR -> merge blocks -> reading order.
With:      one Runtime.evaluate that returns the element list already ordered.

The trade is explicit. OCR sees anything painted, including canvases and text
baked into images. The DOM sees only real elements — but it sees them *exactly*:
correct text, correct role, correct label, and a click point in viewport
coordinates, which is what CDP Input wants. The original had to convert Retina
capture pixels back into screen points.

Each collected element is stamped with `data-tscu="<index>"` so later steps have
a stable handle to click, focus and scroll. Stamps from the previous snapshot are
cleared first, so an element that vanished cannot be clicked by mistake.

What a user has typed is never read. An input's current value is not collected,
and no element takes its name from it, so a password, a one-time code or a card
number cannot reach the classifier, the writer, the log or the run folder. The
only value used is a button input's, which is the button's own label. Fields
that ask for a credential are marked `secret`, and nothing is typed into them.

The same script collects the page's visible text, the prices, dates and error
messages that are not controls, as `page_text` blocks kept apart from the element
list, so a block is never a click target. The rule above holds there too: text in
a text control or an editable region, such as an unsent `contenteditable` draft,
is not collected.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .cdp import CDPError

INTERACTIVE_JS = r"""
(() => {
  document.querySelectorAll("[data-tscu]").forEach(el => el.removeAttribute("data-tscu"));
  const ROLES = new Set(["button","link","menuitem","menuitemcheckbox","menuitemradio",
                         "tab","checkbox","radio","switch","combobox","option","searchbox",
                         "textbox","slider","spinbutton"]);
  const TAGS = new Set(["A","BUTTON","INPUT","SELECT","TEXTAREA","SUMMARY","OPTION"]);
  const SEL = "a,button,input,select,textarea,summary,[role],[onclick],[tabindex]";
  // Inputs that take free text. Everything else (checkbox, radio, file, range...) is clicked.
  const TEXT_TYPES = new Set(["text","search","email","url","tel","number","password"]);
  // An input of these types shows its value as its label, and the page wrote that value.
  const BUTTON_TYPES = new Set(["submit","button","reset"]);
  // Autocomplete tokens for credentials and payment data (WHATWG autofill field names).
  const SECRET_AUTOCOMPLETE = /(^|\s)(current-password|new-password|one-time-code|cc-number|cc-csc|cc-exp|cc-exp-month|cc-exp-year)(\s|$)/;
  const vw = window.innerWidth, vh = window.innerHeight;
  const out = [];
  const seen = new Set();
  let sid = 0, belowFold = 0, total = 0;
  for (const el of document.querySelectorAll(SEL)) {
    total++;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    // Viewport test BEFORE any style resolution. On a heavy page most candidates
    // are off-screen, and getComputedStyle is the expensive call in this loop —
    // skipping it for the ones we would discard anyway is the whole optimisation.
    const onScreen = r.top < vh + 8 && r.bottom > -8 && r.left < vw + 8 && r.right > -8;
    if (!onScreen) { belowFold++; continue; }
    const st = getComputedStyle(el);
    if (st.visibility === "hidden" || st.display === "none" || parseFloat(st.opacity || "1") === 0) continue;
    if (el.disabled || el.getAttribute("aria-hidden") === "true") continue;
    const role = (el.getAttribute("role") || "").toLowerCase();
    if (!(TAGS.has(el.tagName) || ROLES.has(role) || el.hasAttribute("onclick") || el.hasAttribute("tabindex"))) continue;
    const type = el.tagName === "INPUT" ? String(el.type || "text").toLowerCase() : "";
    const secret = type === "password" ||
      SECRET_AUTOCOMPLETE.test((el.getAttribute("autocomplete") || "").toLowerCase());
    const field = el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && TEXT_TYPES.has(type));
    let name = (el.getAttribute("aria-label") || el.getAttribute("placeholder") ||
                el.getAttribute("title") || el.getAttribute("alt") || "").trim();
    // Never a text control's value or its own text: that is what the user typed.
    const typedInto = field || el.isContentEditable || role === "textbox" || role === "searchbox";
    if (!name && !typedInto) name = (el.innerText || (BUTTON_TYPES.has(type) ? el.value : "") || "");
    if (!name) name = el.getAttribute("name") || "";
    name = name.replace(/\s+/g, " ").trim();
    if (!name) continue;
    name = name.slice(0, 120);
    const key = [name.toLowerCase(), Math.round(r.left), Math.round(r.top), el.tagName].join("|");
    if (seen.has(key)) continue;
    seen.add(key);
    const x = Math.round(r.left + Math.min(r.width, 1400) / 2);
    const y = Math.round(r.top + r.height / 2);
    const top = document.elementFromPoint(Math.max(0, Math.min(x, vw - 1)), Math.max(0, Math.min(y, vh - 1)));
    const covered = !!(top && !el.contains(top) && !top.contains(el));
    el.setAttribute("data-tscu", String(sid));
    out.push({sid, tag: el.tagName.toLowerCase(), role: role || type,
              name, x, y, w: Math.round(r.width), h: Math.round(r.height),
              in_view: true, covered,
              href: el.tagName === "A" ? (el.href || "") : "",
              field, secret});
    sid++;
  }
  out.sort((a, b) => (Math.abs(a.y - b.y) > 8 ? a.y - b.y : a.x - b.x));
  out.forEach((o, i) => {
    const el = document.querySelector('[data-tscu="' + o.sid + '"]');
    if (el) el.setAttribute("data-tscu", String(i));
    o.index = i;
  });
  // --- visible text: prices, dates, errors, everything that is not a control --------
  // Blocks in reading order, kept apart from `items` so a text block is never a click
  // target. Nothing inside a text control, a textbox role or an editable region is
  // read, so a field's contents and an unsent contenteditable draft stay on the page.
  const SKIP = "script,style,noscript,template,select,textarea,svg,[aria-hidden='true']," +
               "[contenteditable]:not([contenteditable='false'])," +
               "[role='textbox'],[role='searchbox'],[role='combobox']";
  const CONTROL = "a,button,input,select,textarea,summary,option,[onclick],[tabindex]";
  const INLINE = new Set(["B","STRONG","I","EM","U","S","SMALL","ABBR","CODE","MARK","SUB","SUP","SPAN"]);
  const TEXT_MAX = 120, TEXT_CHARS = 240;
  // Text nodes under their nearest block element, in DOM order. From the document
  // itself when there is no body yet (mid-load) or at all (an SVG or XML file).
  const groups = new Map();
  const walker = document.createTreeWalker(document.body || document, NodeFilter.SHOW_TEXT);
  for (let node; (node = walker.nextNode()); ) {
    if (!node.nodeValue.trim()) continue;
    let g = node.parentElement;
    while (g.parentElement && INLINE.has(g.tagName)) g = g.parentElement;
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(node);
  }
  // Per text node, and only inside a block already found on screen, where it is cheap.
  // A shown block can still hold a skipped part, or an inline part with no box or
  // with visibility hidden.
  const range = document.createRange();
  const readable = (n, el) => {
    const p = n.parentElement;
    if (p.isContentEditable || p.closest(SKIP)) return false;
    if (p === el) return true;
    range.selectNodeContents(n);
    return range.getClientRects().length > 0 && getComputedStyle(p).visibility !== "hidden";
  };
  const names = new Set(out.map(o => o.name.toLowerCase()));
  const textOut = [], textSeen = new Set();
  for (const [el, nodes] of groups) {
    if (el.matches(CONTROL) || ROLES.has((el.getAttribute("role") || "").toLowerCase())) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    if (!(r.top < vh + 8 && r.bottom > -8 && r.left < vw + 8 && r.right > -8)) continue;  // before any style
    const st = getComputedStyle(el);
    if (st.visibility === "hidden" || st.display === "none" || parseFloat(st.opacity || "1") === 0) continue;
    // Read only as far as a block can use: a whole file in one <pre> stops early.
    let text = "";
    for (const n of nodes) {
      if (text.length > 2 * TEXT_CHARS) break;
      if (readable(n, el)) text += " " + n.nodeValue.replace(/\s+/g, " ");
    }
    text = text.replace(/\s+/g, " ").trim();
    const key = text.toLowerCase();
    // Too short, a copy of a control's label, or a repeat (nav bars, ARIA duplicates).
    if (text.length < 2 || names.has(key) || textSeen.has(key)) continue;
    textSeen.add(key);
    textOut.push({text: text.slice(0, TEXT_CHARS), x: Math.round(r.left), y: Math.round(r.top),
                  w: Math.round(r.width), h: Math.round(r.height)});
    if (textOut.length >= TEXT_MAX) break;
  }
  textOut.sort((a, b) => (Math.abs(a.y - b.y) > 8 ? a.y - b.y : a.x - b.x));
  const sc = document.scrollingElement || document.documentElement;
  return {url: location.href, title: document.title, vw, vh, count: out.length, items: out,
          text: textOut,
          scroll_y: Math.round(sc.scrollTop), scroll_max: Math.round(sc.scrollHeight - vh),
          candidates: total, below_fold: belowFold,
          can_scroll: sc.scrollHeight > vh + 4,
          history_len: history.length,
          fields: out.filter(o => o.field && !o.secret).length};
})()
"""


@dataclass(frozen=True)
class Element:
    index: int
    tag: str
    role: str
    name: str
    x: int
    y: int
    w: int
    h: int
    in_view: bool
    covered: bool
    href: str
    field: bool = False  # takes free text
    secret: bool = False  # asks for a password, a one-time code or card data: never typed into

    @property
    def typeable(self) -> bool:
        return self.field and not self.secret

    def label(self) -> str:
        bits = [f"<{self.tag}>", repr(self.name)]
        if self.href:
            bits.append(self.href[:70])
        if self.secret:
            bits.append("credential field")
        if not self.in_view:
            bits.append("off-screen")
        if self.covered:
            bits.append("covered by an overlay")
        return " ".join(bits)


@dataclass(frozen=True)
class TextBlock:
    """One visible text block: evidence for the classifier, never a click target."""

    text: str
    x: int
    y: int
    w: int
    h: int


@dataclass
class Page:
    url: str
    title: str
    vw: int
    vh: int
    items: list[Element]
    elapsed_ms: float
    raw_count: int
    can_scroll: bool = True
    history_len: int = 1
    field_count: int = 0
    scroll_y: int = 0
    scroll_max: int = 0
    candidates: int = 0
    below_fold: int = 0
    text: list[TextBlock] = field(default_factory=list)  # visible page text, evidence only

    @property
    def has_field(self) -> bool:
        return self.field_count > 0


def _evaluate_settled(session: Any, tries: int = 5) -> dict:
    """The perception script, retried while a navigation swaps the document out from under it."""
    for attempt in range(tries):
        try:
            return session.evaluate(INTERACTIVE_JS) or {}
        except CDPError:
            if attempt == tries - 1:
                raise
            time.sleep(0.1)
    return {}


def perceive(session: Any, *, budget: int = 120, text_budget: int = 120) -> Page:
    """One CDP round trip -> an ordered, labelled element list, plus the page's visible
    text as evidence blocks. No pixels."""
    start = time.perf_counter()
    data = _evaluate_settled(session)
    elapsed = (time.perf_counter() - start) * 1000
    items = [
        Element(
            index=int(it.get("index", i)),
            tag=str(it.get("tag", "")),
            role=str(it.get("role", "")),
            name=str(it.get("name", "")),
            x=int(it.get("x", 0)),
            y=int(it.get("y", 0)),
            w=int(it.get("w", 0)),
            h=int(it.get("h", 0)),
            in_view=bool(it.get("in_view", True)),
            covered=bool(it.get("covered", False)),
            href=str(it.get("href", "")),
            field=bool(it.get("field", False)),
            secret=bool(it.get("secret", False)),
        )
        for i, it in enumerate((data.get("items") or [])[:budget])
    ]
    text = [
        TextBlock(
            text=str(tb.get("text", "")),
            x=int(tb.get("x", 0)),
            y=int(tb.get("y", 0)),
            w=int(tb.get("w", 0)),
            h=int(tb.get("h", 0)),
        )
        for tb in (data.get("text") or [])[:text_budget]
        if str(tb.get("text", "")).strip()
    ]
    return Page(
        url=str(data.get("url", "")),
        title=str(data.get("title", "")),
        vw=int(data.get("vw", 0)),
        vh=int(data.get("vh", 0)),
        items=items,
        elapsed_ms=elapsed,
        raw_count=int(data.get("count", len(items))),
        can_scroll=bool(data.get("can_scroll", True)),
        history_len=int(data.get("history_len", 1)),
        field_count=int(data.get("fields", 0)),
        scroll_y=int(data.get("scroll_y", 0)),
        scroll_max=int(data.get("scroll_max", 0)),
        candidates=int(data.get("candidates", 0)),
        below_fold=int(data.get("below_fold", 0)),
        text=text,
    )


def to_json(page: Page) -> str:
    return json.dumps(
        {
            "url": page.url,
            "title": page.title,
            "elapsed_ms": round(page.elapsed_ms, 2),
            "items": [it.__dict__ for it in page.items],
            "text": [tb.__dict__ for tb in page.text],
        },
        indent=2,
    )
