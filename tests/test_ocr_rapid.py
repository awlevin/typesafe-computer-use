"""The RapidOCR backend, offline: the conversion from RapidOCR's output to `OcrLine`s on canned
output, and how `osworld.ocr` loads the backend with and without the `rapidocr` extra.

No test loads the models. Where the extra is not installed, `ocr_rapid` is imported against a
stand-in RapidOCR that refuses to build an engine, and the import is undone afterward, so the rest
of the suite sees the machine as it is.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types

import pytest

import typesafe_computer_use
from typesafe_computer_use.osworld import ocr

MODULE = "typesafe_computer_use.ocr_rapid"
HAS_EXTRA = importlib.util.find_spec("rapidocr") is not None
INSTALL = "install it with: uv sync --extra rapidocr (or pip install 'typesafe-computer-use[rapidocr]')"


class _NoEngine:
    def __init__(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("the rapidocr extra is not installed; tests must not build an engine")


@pytest.fixture(scope="module")
def rapid():
    """`ocr_rapid`, for its pure functions."""
    if HAS_EXTRA:
        yield importlib.import_module(MODULE)
        return
    stand_in = types.ModuleType("rapidocr")
    stand_in.RapidOCR = _NoEngine
    sys.modules["rapidocr"] = stand_in
    try:
        yield importlib.import_module(MODULE)
    finally:
        sys.modules.pop("rapidocr", None)
        sys.modules.pop(MODULE, None)
        if hasattr(typesafe_computer_use, "ocr_rapid"):
            delattr(typesafe_computer_use, "ocr_rapid")


def result(lines):
    """RapidOCR's result object, as `rapidocr` returns it: parallel boxes, texts, and scores. Its
    boxes are a numpy array, which iterates as these lists do."""
    if not lines:
        return types.SimpleNamespace(boxes=None, txts=None, scores=None)
    corners, texts, scores = zip(*lines, strict=True)
    return types.SimpleNamespace(boxes=list(corners), txts=tuple(texts), scores=tuple(scores))


UPRIGHT = [[37.0, 31.0], [215.0, 31.0], [215.0, 68.0], [37.0, 68.0]]
TILTED = [[102.4, 210.0], [300.0, 180.5], [305.5, 216.0], [107.0, 246.25]]  # rotated about 9 degrees


# ----- the conversion -------------------------------------------------------------------------


def test_an_upright_line_keeps_its_corners_as_the_box(rapid):
    assert rapid.to_line(UPRIGHT, "Hello World", 0.99991) == ("Hello World", 0.99991, (37.0, 31.0, 215.0, 68.0))


def test_a_tilted_line_gets_the_upright_box_that_holds_every_corner(rapid):
    text, confidence, box = rapid.to_line(TILTED, "Tilted", 0.87)
    assert (text, confidence) == ("Tilted", 0.87)
    assert box == (102.4, 180.5, 305.5, 246.25)


def test_the_line_is_plain_python_floats_and_str(rapid):
    np = pytest.importorskip("numpy")  # RapidOCR's own types, which come with the extra
    text, confidence, box = rapid.to_line(np.array(UPRIGHT, dtype=np.float32), np.str_("Hi"), np.float32(0.5))
    assert type(text) is str and type(confidence) is float
    assert all(type(v) is float for v in box)


def test_the_result_object_reads_as_lines_top_to_bottom_then_left_to_right(rapid):
    below = [[37.0, 142.0], [212.0, 142.0], [212.0, 179.0], [37.0, 179.0]]
    right = [[347.0, 31.0], [431.0, 31.0], [431.0, 75.0], [347.0, 75.0]]
    output = result([(below, "Second line", 0.99), (right, "Right", 0.98), (UPRIGHT, "Hello World", 0.97)])
    assert [line[0] for line in rapid.lines_of(output)] == ["Hello World", "Right", "Second line"]
    assert rapid.lines_of(output)[1] == ("Right", pytest.approx(0.98), (347.0, 31.0, 431.0, 75.0))


def test_a_line_a_little_higher_on_the_right_still_reads_after_the_one_on_its_left(rapid):
    search = [[894.0, 38.0], [1091.0, 38.0], [1091.0, 78.0], [894.0, 78.0]]
    gmail = [[35.0, 41.0], [214.0, 41.0], [214.0, 80.0], [35.0, 80.0]]
    below = [[36.0, 62.0], [253.0, 62.0], [253.0, 95.0], [36.0, 95.0]]  # overlaps the row, but starts past its middle
    output = result([(below, "Below", 0.9), (search, "Search Google", 0.9), (gmail, "Gmail", 0.9)])
    assert [line[0] for line in rapid.lines_of(output)] == ["Gmail", "Search Google", "Below"]


def test_a_result_with_no_text_is_no_lines(rapid):
    assert rapid.lines_of(result([])) == []


def test_the_older_tuple_of_lists_shape_reads_the_same(rapid):
    older = ([[TILTED, "Tilted", 0.87], [UPRIGHT, "Hello World", 0.99]], [0.1, 0.02, 0.05])
    assert rapid.lines_of(older) == [
        ("Hello World", 0.99, (37.0, 31.0, 215.0, 68.0)),
        ("Tilted", 0.87, (102.4, 180.5, 305.5, 246.25)),
    ]
    assert rapid.lines_of((None, [0.1])) == []


# ----- boxes around the ink -------------------------------------------------------------------


def page(background: str = "white", ink: str = "black", strokes=((50, 40, 120, 55),)):
    """A grey page with dark (or light) bars standing in for lines of text."""
    from PIL import Image, ImageDraw

    image = Image.new("L", (240, 200), background)
    draw = ImageDraw.Draw(image)
    for stroke in strokes:
        draw.rectangle((stroke[0], stroke[1], stroke[2] - 1, stroke[3] - 1), fill=ink)
    return image


def test_a_padded_box_shrinks_to_the_ink_inside_it(rapid):
    assert rapid.hug_ink(page(), (38.4, 29.6, 131.0, 66.2)) == (50.0, 40.0, 120.0, 55.0)


def test_light_text_on_a_dark_page_is_ink_too(rapid):
    assert rapid.hug_ink(page("black", "white"), (40.0, 30.0, 130.0, 65.0)) == (50.0, 40.0, 120.0, 55.0)


def test_a_box_with_no_ink_or_off_the_image_stays_as_it_is(rapid):
    assert rapid.hug_ink(page(), (150.0, 100.0, 200.0, 140.0)) == (150.0, 100.0, 200.0, 140.0)
    assert rapid.hug_ink(page(), (300.0, 300.0, 340.0, 320.0)) == (300.0, 300.0, 340.0, 320.0)


def test_hugged_menu_entries_stay_apart_where_padded_ones_read_as_one_paragraph(rapid):
    """Chrome's settings sidebar at 1x: 16-pixel text every 40 pixels, which RapidOCR boxes 25 high."""
    from typesafe_computer_use.perception import merge_blocks

    tops = (345, 385, 425)
    image = page(strokes=[(128, top + 5, 220, top + 20) for top in (y - 300 for y in tops)])
    padded = [
        (name, 1.0, (126.0, top - 300.0, 222.0, top - 300.0 + 26.0))
        for name, top in zip(("Appearance", "Search engine", "Default browser"), tops, strict=True)
    ]
    assert [text for text, _, _ in merge_blocks(padded)] == ["Appearance Search engine Default browser"]
    hugged = [(text, score, rapid.hug_ink(image, box)) for text, score, box in padded]
    assert [text for text, _, _ in merge_blocks(hugged)] == ["Appearance", "Search engine", "Default browser"]


def test_importing_the_backend_loads_no_models(rapid):
    assert rapid._engine.cache_info().currsize == 0


# ----- loading the backend --------------------------------------------------------------------


def test_rapidocr_is_a_known_backend_and_an_unknown_name_still_fails():
    assert set(ocr.backends()) >= {"vision", "rapidocr"}
    with pytest.raises(ValueError, match=r"unknown OCR backend 'nope'; known backends: rapidocr, vision"):
        ocr.backend("nope")


def test_without_the_extra_rapidocr_is_unavailable_and_says_how_to_install_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "rapidocr", None)  # an import of it raises ImportError
    monkeypatch.delitem(sys.modules, MODULE, raising=False)
    monkeypatch.delattr(typesafe_computer_use, "ocr_rapid", raising=False)
    with pytest.raises(ocr.Unavailable, match="the rapidocr backend cannot load") as raised:
        ocr.backend("rapidocr")
    assert INSTALL in str(raised.value)
    assert isinstance(raised.value.__cause__, ImportError)


def test_with_the_module_importable_rapidocr_is_its_recognizer(monkeypatch):
    fake = types.ModuleType(MODULE)
    fake.recognize_text = lambda image: [("fake", 1.0, (0.0, 0.0, 1.0, 1.0))]
    monkeypatch.setitem(sys.modules, MODULE, fake)
    monkeypatch.setattr(typesafe_computer_use, "ocr_rapid", fake, raising=False)
    assert ocr.backend("rapidocr") is fake.recognize_text


@pytest.mark.skipif(not HAS_EXTRA, reason="needs the rapidocr extra")
def test_with_the_extra_rapidocr_loads_without_loading_the_models():
    rapid = importlib.import_module(MODULE)
    assert ocr.backend("rapidocr") is rapid.recognize_text
    assert rapid._engine.cache_info().currsize == 0
