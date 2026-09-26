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

The same script also collects the page's visible text - the prices, dates and
error messages that are not controls - as bounded `page_text` blocks, separate
from the element list so they can never become click targets. The same privacy
rule holds there: text inside a text control or an editable region (an unsent
draft in a `contenteditable`) is not collected, so what the user typed still
cannot reach the classifier, the writer, the log or the run folder.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

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
  // --- visible text: the half of the page that is not a control ----------------
  // Collected in the same round trip, as bounded blocks in reading order, sent
  // separately from `items` so a text block can never be chosen as a click target.
  // The typing rule still holds: nothing inside a text control (input, textarea,
  // select) or an editable region is read - an unsent draft in a contenteditable
  // is text on the page, and it still does not leave the page.
  const CONTROL = "a,button,input,select,textarea,summary,option,[onclick],[tabindex]";
  const SKIP = "script,style,noscript,template,select,textarea,svg," +
               "[role='textbox'],[role='searchbox'],[role='combobox']";
  const INLINE = new Set(["B","STRONG","I","EM","U","S","SMALL","ABBR","CODE","MARK","SUB","SUP","SPAN"]);
  const TEXT_MAX = 120, TEXT_CHARS = 240;
  const names = new Set(out.map(o => o.name.toLowerCase()));
  const groups = new Map();  // block element -> raw text parts, in DOM order
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const p = node.parentElement;
    if (!p || !(node.nodeValue || "").trim()) continue;
    if (p.closest(SKIP)) continue;
    let editable = false;
    for (let a = p; a; a = a.parentElement) if (a.isContentEditable) { editable = true; break; }
    if (editable) continue;
    if (p.closest("[aria-hidden='true']")) continue;
    let g = p;
    while (g.parentElement && INLINE.has(g.tagName)) g = g.parentElement;
    let parts = groups.get(g);
    if (!parts) groups.set(g, (parts = []));
    parts.push(node.nodeValue);
  }
  const textOut = [];
  const textSeen = new Set();
  for (const [el, parts] of groups) {
    if (el.matches(CONTROL) || ROLES.has((el.getAttribute("role") || "").toLowerCase())) continue;
    const text = parts.join(" ").replace(/\s+/g, " ").trim();
    if (text.length < 2) continue;
    const key = text.toLowerCase();
    // A copy of a control's label, or a repeat of a block already kept (nav bars,
    // ARIA duplicates), adds nothing.
    if (names.has(key) || textSeen.has(key)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    // Same order as the element pass: the cheap viewport test first, the
    // expensive getComputedStyle only for blocks that survive it.
    const onScreen = r.top < vh + 8 && r.bottom > -8 && r.left < vw + 8 && r.right > -8;
    if (!onScreen) continue;
    const st = getComputedStyle(el);
    if (st.visibility === "hidden" || st.display === "none" || parseFloat(st.opacity || "1") === 0) continue;
    textSeen.add(key);
    textOut.push({text: text.slice(0, TEXT_CHARS),
                  x: Math.round(r.left), y: Math.round(r.top),
                  w: Math.round(r.width), h: Math.round(r.height)});
    if (textOut.length >= TEXT_MAX) break;
  }
  textOut.sort((a, b) => (Math.abs(a.y - b.y) > 8 ? a.y - b.y : a.x - b.x));
  textOut.forEach((o, i) => { o.e = "t" + i; });
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

    evidence_id: str
    text: str
    x: int
    y: int
    w: int
    h: int

    def label(self) -> str:
        return f"{self.evidence_id}: {self.text!r}"


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


def perceive(session: Any, *, budget: int = 120, text_budget: int = 120) -> Page:
    """One CDP round trip -> an ordered, labelled element list, plus the page's visible
    text as evidence blocks. No pixels."""
    start = time.perf_counter()
    data = session.evaluate(INTERACTIVE_JS) or {}
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
            evidence_id=str(tb.get("e", f"t{i}")),
            text=str(tb.get("text", "")),
            x=int(tb.get("x", 0)),
            y=int(tb.get("y", 0)),
            w=int(tb.get("w", 0)),
            h=int(tb.get("h", 0)),
        )
        for i, tb in enumerate((data.get("text") or [])[:text_budget])
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
