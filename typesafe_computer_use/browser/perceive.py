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
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

INTERACTIVE_JS = r"""
(() => {
  document.querySelectorAll("[data-tscu]").forEach(el => el.removeAttribute("data-tscu"));
  const ROLES = new Set(["button","link","menuitem","menuitemcheckbox","menuitemradio",
                         "tab","checkbox","radio","switch","combobox","option","searchbox",
                         "textbox","slider","spinbutton"]);
  const TAGS = new Set(["A","BUTTON","INPUT","SELECT","TEXTAREA","SUMMARY","OPTION"]);
  const SEL = "a,button,input,select,textarea,summary,[role],[onclick],[tabindex]";
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
    let name = (el.getAttribute("aria-label") || el.getAttribute("placeholder") ||
                el.getAttribute("title") || el.getAttribute("alt") || "").trim();
    if (!name) name = (el.innerText || el.value || el.getAttribute("name") || "").replace(/\s+/g, " ").trim();
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
    out.push({sid, tag: el.tagName.toLowerCase(), role: role || (el.type ? String(el.type).toLowerCase() : ""),
              name, x, y, w: Math.round(r.width), h: Math.round(r.height),
              in_view: true, covered,
              href: el.tagName === "A" ? (el.href || "") : "",
              value: el.value === undefined ? "" : String(el.value).slice(0, 80)});
    sid++;
  }
  out.sort((a, b) => (Math.abs(a.y - b.y) > 8 ? a.y - b.y : a.x - b.x));
  out.forEach((o, i) => {
    const el = document.querySelector('[data-tscu="' + o.sid + '"]');
    if (el) el.setAttribute("data-tscu", String(i));
    o.index = i;
  });
  const sc = document.scrollingElement || document.documentElement;
  return {url: location.href, title: document.title, vw, vh, count: out.length, items: out,
          scroll_y: Math.round(sc.scrollTop), scroll_max: Math.round(sc.scrollHeight - vh),
          candidates: total, below_fold: belowFold,
          can_scroll: sc.scrollHeight > vh + 4,
          history_len: history.length,
          fields: out.filter(o => o.tag === "input" || o.tag === "textarea").length};
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
    value: str

    def label(self) -> str:
        bits = [f"<{self.tag}>", repr(self.name)]
        if self.href:
            bits.append(self.href[:70])
        if self.value:
            bits.append(f"value={self.value[:40]!r}")
        if not self.in_view:
            bits.append("off-screen")
        if self.covered:
            bits.append("covered by an overlay")
        return " ".join(bits)


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

    @property
    def has_field(self) -> bool:
        return self.field_count > 0


def perceive(session: Any, *, budget: int = 120) -> Page:
    """One CDP round trip -> an ordered, labelled element list. No pixels."""
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
            value=str(it.get("value", "")),
        )
        for i, it in enumerate((data.get("items") or [])[:budget])
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
    )


def to_json(page: Page) -> str:
    return json.dumps(
        {
            "url": page.url,
            "title": page.title,
            "elapsed_ms": round(page.elapsed_ms, 2),
            "items": [it.__dict__ for it in page.items],
        },
        indent=2,
    )
