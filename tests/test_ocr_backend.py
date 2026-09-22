from typesafe_computer_use.ocr_backend import normalize_text


def test_normalize_text_removes_spaces_between_cjk_glyphs():
    assert normalize_text("\u516c \u5f00 \u62db \u52df \uff1a 3") == "\u516c\u5f00\u62db\u52df: 3"


def test_normalize_text_keeps_spaces_inside_latin_labels():
    assert normalize_text("Open  documentation") == "Open documentation"
