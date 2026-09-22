"""Data carried between perception, decision, and action."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace

from PIL import Image

TEXT_ROLES = {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"}
Box = tuple[float, float, float, float]  # x1, y1, x2, y2 in capture pixels

# Accessibility roles as one human word. Anything unlisted is "other".
ROLE_WORDS = {
    "AXButton": "button",
    "AXCell": "cell",
    "AXCheckBox": "checkbox",
    "AXComboBox": "field",
    "AXDockItem": "dock item",
    "AXImage": "image",
    "AXLink": "link",
    "AXMenuBarItem": "menu",
    "AXMenuButton": "button",
    "AXPopUpButton": "popup",
    "AXRadioButton": "radio",
    "AXRow": "cell",
    "AXSearchField": "field",
    "AXSlider": "slider",
    "AXTab": "tab",
    "AXTextArea": "field",
    "AXTextField": "field",
}


class Abort(Exception):
    """Raised when the user triggers an escape hatch."""


@dataclass(frozen=True)
class Exchange:
    """One question the writer put to the user, and what the user said."""

    question: str
    reply: str


@dataclass(frozen=True)
class Guidance:
    """What the run has learned about its goal since it began.

    The goal is one sentence and never changes. `focus` is the sub-goal the writer sent the
    classifier back to work on the last time the classifier stopped, and `exchanges` are the
    questions the user answered on the way. Every model call reads both, so the classifier
    steers by them and the writer types by them.
    """

    focus: str | None = None
    exchanges: tuple[Exchange, ...] = ()

    def focused(self, focus: str) -> Guidance:
        return replace(self, focus=focus)

    def heard(self, question: str, reply: str) -> Guidance:
        return replace(self, exchanges=(*self.exchanges, Exchange(question, reply)))

    def state(self) -> dict:
        """The keys a model packet carries, and only the ones that hold something."""
        return {
            **({"current_focus": self.focus} if self.focus else {}),
            **({"user_said": [{"asked": e.question, "replied": e.reply} for e in self.exchanges]} if self.exchanges else {}),
        }


Signature = tuple[
    str, str | None, str | None, tuple[tuple[str, int], ...]
]  # app, URL, focused field, (text, row) in reading order
LINES_PER_DIFFERENCE = 10  # a screen is the same when at most one line in ten differs...
MAX_DIFFERING_LINES = 1  # ...and at most one line at all: a clock, a ticker, or an OCR slip
ROW_PT = 20.0  # the row a line sits in, in screen points: coarse enough to survive OCR jitter, fine enough to see a scroll


def signature(screen: Screen, items: list[Item]) -> Signature:
    """What identifies a screen from one step to the next: the app, the page, which field has the
    focus, and each line of text with the row it sits in.

    A click that only moves the focus changes no text, but it changes what the next action can do,
    so it counts. A scroll on a dense page keeps nine tenths of the text and moves all of it, so the
    row counts too: the same line lower down is a different line.
    """
    focused = f"{screen.field.role}:{screen.field.label}" if screen.field else None
    row = ROW_PT * screen.scale
    return (screen.app, screen.url, focused, tuple((it.text, round(it.center[1] / row)) for it in items))


def same_screen(a: Signature, b: Signature) -> bool:
    """Whether two captures show the same screen, allowing for a clock, a ticker, or an OCR slip.

    One line may differ, and only when it is one in ten or less. On a dense page a two-line modal
    is a change; on a five-line page any change is one. Measured as lines that appeared or
    vanished, whichever is more, so a page that gained a section counts as changed and one that
    lost a line under the bar counts as the same.
    """
    if a[:3] != b[:3]:
        return False
    lines_a, lines_b = set(a[3]), set(b[3])
    differing = max(len(lines_a - lines_b), len(lines_b - lines_a))
    return differing <= MAX_DIFFERING_LINES and differing * LINES_PER_DIFFERENCE <= max(len(lines_a), len(lines_b))


@dataclass(frozen=True)
class Item:
    """One clickable thing: text plus its pixel box on the capture.

    `source` says where it came from: "ocr" for a merged text block, "ax" for an
    accessibility control, "ax+ocr" when the two agree on the same thing. `role` is a
    short human word (button, link, field, ...) and is empty for OCR-only items.
    """

    index: int
    text: str
    ocr_confidence: float
    x1: float
    y1: float
    x2: float
    y2: float
    role: str = ""
    source: str = "ocr"

    @property
    def center(self) -> tuple[float, float]:
        return (self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2

    @property
    def from_ax(self) -> bool:
        return self.source in ("ax", "ax+ocr")


@dataclass(frozen=True)
class AxNode:
    """One actionable accessibility element, in screen points.

    `ref` is the element itself, the handle an action is sent to. It is opaque here and
    stays out of equality and repr so a node compares as the facts it reports.
    """

    role: str
    label: str
    x: float
    y: float
    w: float
    h: float
    pressable: bool
    ref: object | None = field(default=None, compare=False, repr=False)

    @property
    def role_word(self) -> str:
        return ROLE_WORDS.get(self.role, "other")


@dataclass(frozen=True)
class Field:
    """The focused accessibility element, in screen points.

    `ref` is the element itself, so text can be set on it directly instead of typed.
    """

    role: str
    label: str
    placeholder: str
    value: str
    x: float
    y: float
    w: float
    h: float
    ref: object | None = field(default=None, compare=False, repr=False)

    @property
    def is_text(self) -> bool:
        return self.role in TEXT_ROLES

    def record(self) -> dict:
        """Everything but the opaque element handle, which no log can serialize."""
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "ref"}

    def summary(self) -> dict:
        return {
            "role": self.role,
            "label": self.label,
            "placeholder": self.placeholder,
            "current_value": self.value[:200],
        }


@dataclass(frozen=True)
class Screen:
    """Everything captured about the display at one instant."""

    image: Image.Image
    scale: float  # capture pixels per screen point
    app: str
    field: Field | None
    url: str | None
    pid: int | None = None  # frontmost process, for the accessibility walk; None in replay
    window: tuple[float, float, float, float] | None = None  # frontmost window, x/y/w/h in points; None in replay
    ax_refs: dict[int, object] = field(default_factory=dict)  # item index -> accessibility element, when it has one
    offscreen: list[AxNode] = field(default_factory=list)  # labelled controls the app exposes but does not show

    @property
    def size_pt(self) -> tuple[float, float]:
        return self.image.width / self.scale, self.image.height / self.scale

    def region(self, item: Item) -> str:
        """A coarse cell for the item, clamped both ways.

        Frames lie: Chrome and Notes report nodes thousands of points off the capture, so an index
        computed from one lands outside the three rows. The cell is only ever a hint.
        """
        cx, cy = item.center
        col = ["left", "center", "right"][max(0, min(2, int(3 * cx / self.image.width)))]
        row = ["top", "middle", "bottom"][max(0, min(2, int(3 * cy / self.image.height)))]
        return f"{row}-{col}"

    def to_points(self, item: Item) -> tuple[float, float]:
        cx, cy = item.center
        return cx / self.scale, cy / self.scale
