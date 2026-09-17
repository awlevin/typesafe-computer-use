"""Data carried between perception, decision, and action."""

from __future__ import annotations

from dataclasses import dataclass, field, fields

from PIL import Image

TEXT_ROLES = {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"}
Box = tuple[float, float, float, float]  # x1, y1, x2, y2 in capture pixels


class Abort(Exception):
    """Raised when the user triggers an escape hatch."""


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
    ax_refs: dict[int, object] = field(default_factory=dict)  # item index -> accessibility element, when it has one

    @property
    def size_pt(self) -> tuple[float, float]:
        return self.image.width / self.scale, self.image.height / self.scale

    def region(self, item: Item) -> str:
        cx, cy = item.center
        col = ["left", "center", "right"][min(2, int(3 * cx / self.image.width))]
        row = ["top", "middle", "bottom"][min(2, int(3 * cy / self.image.height))]
        return f"{row}-{col}"

    def to_points(self, item: Item) -> tuple[float, float]:
        cx, cy = item.center
        return cx / self.scale, cy / self.scale
