from pathlib import Path

from PIL import Image, ImageDraw

from typesafe_computer_use import perception
from typesafe_computer_use.models import AxNode, Item, Screen
from typesafe_computer_use.perception import (
    drawn,
    goal_echoes,
    is_echo,
    merge_blocks,
    merge_sources,
    order_items,
    to_items,
)
from typesafe_computer_use.platform_adapter import desktop


def line(text, x1, y1, x2, y2, conf=1.0):
    return (text, conf, (float(x1), float(y1), float(x2), float(y2)))


def test_merges_stacked_lines_across_columns():
    lines = [
        line("Kash Patel defends", 1080, 1531, 1300, 1561),
        line("Two House Democrats", 1400, 1531, 1600, 1561),
        line("removing bestiality as", 1075, 1571, 1300, 1601),
        line("defect again on key vote", 1400, 1571, 1600, 1601),
        line("FBI applicants", 1080, 1606, 1300, 1636),
    ]
    texts = sorted(t for t, _, _ in merge_blocks(lines))
    assert texts == ["Kash Patel defends removing bestiality as FBI applicants", "Two House Democrats defect again on key vote"]


def test_does_not_merge_far_or_misaligned_lines():
    lines = [line("Home", 100, 100, 200, 130), line("World", 400, 100, 500, 130), line("Footer", 100, 900, 200, 930)]
    assert len(merge_blocks(lines)) == 3


def test_merged_block_keeps_min_confidence_and_union_box():
    lines = [line("a", 100, 100, 200, 130, conf=1.0), line("b", 102, 140, 260, 170, conf=0.5)]
    ((text, conf, box),) = merge_blocks(lines)
    assert text == "a b" and conf == 0.5 and box == (100, 100, 260, 170)


def test_reading_order_rows_then_columns():
    lines = [line("right", 800, 100, 900, 130), line("left", 100, 105, 200, 135), line("below", 100, 300, 200, 330)]
    assert [it.text for it in to_items(lines, 255)] == ["left", "right", "below"]


def test_budget_caps_items():
    lines = [line(str(i), 100, 100 + 40 * i, 200, 130 + 40 * i) for i in range(10)]
    assert len(to_items(lines, 3)) == 3


def test_goal_echo_matches_wrapped_command_lines():
    goal = "go to cnn and click onto something related to AI on the homepage"
    echoes = goal_echoes(goal)
    assert is_echo('clear && uv run clicker "go to cnn and click onto something', echoes)
    assert is_echo('related to AI on the homepage" --act', echoes)
    assert not is_echo("Trending: Trump and AI warnings", echoes)


def ocr_item(index, text, x1, y1, x2, y2, conf=0.9):
    return Item(index, text, conf, float(x1), float(y1), float(x2), float(y2))


def ax_item(index, text, x1, y1, x2, y2, role="button"):
    return Item(index, text, 1.0, float(x1), float(y1), float(x2), float(y2), role=role, source="ax")


def test_merge_folds_an_overlapping_control_onto_the_ocr_block_that_names_it():
    block = ocr_item(0, "Register Now", 100, 100, 300, 130)
    control = ax_item(0, "Register Now for Disrupt", 110, 102, 290, 128, role="link")
    (merged,) = merge_sources([block], [control])
    assert merged.source == "ax+ocr" and merged.role == "link"
    assert merged.text == "Register Now for Disrupt"  # the longer of the two labels
    assert (merged.x1, merged.y1, merged.x2, merged.y2) == (100.0, 100.0, 300.0, 130.0)  # the OCR box


def test_merge_matches_on_shared_words_not_only_containment():
    block = ocr_item(0, "Buy tickets now", 100, 100, 300, 130)
    control = ax_item(0, "Buy tickets", 100, 100, 300, 130)
    assert [it.source for it in merge_sources([block], [control])] == ["ax+ocr"]


def test_merge_keeps_both_when_the_boxes_overlap_but_the_text_does_not_agree():
    block = ocr_item(0, "Search the docs", 100, 100, 300, 130)
    control = ax_item(0, "Clear input", 100, 100, 300, 130)
    assert sorted(it.source for it in merge_sources([block], [control])) == ["ax", "ocr"]


def test_merge_keeps_both_when_the_text_agrees_but_the_boxes_are_apart():
    block = ocr_item(0, "Share", 100, 100, 200, 130)
    control = ax_item(0, "Share", 900, 600, 960, 630)
    assert sorted(it.source for it in merge_sources([block], [control])) == ["ax", "ocr"]


def test_merge_consumes_each_ocr_block_at_most_once():
    block = ocr_item(0, "Send", 100, 100, 200, 130)
    controls = [ax_item(0, "Send", 100, 100, 200, 130), ax_item(1, "Send", 104, 104, 196, 126)]
    merged = merge_sources([block], controls)
    assert sorted(it.source for it in merged) == ["ax", "ax+ocr"]


def test_merge_numbers_everything_in_reading_order():
    blocks = [ocr_item(0, "below", 100, 300, 200, 330), ocr_item(1, "right", 800, 100, 900, 130)]
    controls = [ax_item(0, "left", 100, 105, 200, 135)]
    assert [(it.index, it.text) for it in merge_sources(blocks, controls)] == [(0, "left"), (1, "right"), (2, "below")]


def test_budget_drops_the_faintest_ocr_blocks_before_any_control():
    blocks = [ocr_item(i, f"text {i}", 100, 100 + 40 * i, 200, 130 + 40 * i, conf=0.3 + 0.1 * i) for i in range(3)]
    controls = [ax_item(0, "Send", 800, 100, 900, 130)]
    kept = merge_sources(blocks, controls, budget=2)
    assert sorted(it.text for it in kept) == ["Send", "text 2"]


def test_budget_falls_back_to_dropping_controls_when_only_controls_remain():
    controls = [ax_item(i, f"control {i}", 100, 100 + 40 * i, 200, 130 + 40 * i) for i in range(4)]
    assert len(merge_sources([], controls, budget=2)) == 2


def test_order_items_renumbers_rows_then_columns():
    items = [ocr_item(7, "right", 800, 100, 900, 130), ocr_item(2, "left", 100, 105, 200, 135)]
    assert [(it.index, it.text) for it in order_items(items)] == [(0, "left"), (1, "right")]


# ----- a control the capture does not show ---------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "osworld"


def control(x: float, y: float, w: float, h: float, label: str = "Agency") -> AxNode:
    return AxNode(role="AXComboBox", label=label, x=x, y=y, w=w, h=h, pressable=True)


def page(size=(300, 120)) -> Image.Image:
    return Image.new("RGB", size, (232, 236, 244))


def test_a_flat_box_is_not_drawn():
    assert not drawn(page(), control(20, 20, 200, 30), 1.0)


def test_an_empty_field_is_drawn_by_its_border():
    image = page()
    ImageDraw.Draw(image).rectangle((20, 20, 220, 50), outline=(40, 40, 40))
    assert drawn(image, control(20, 20, 201, 31), 1.0)


def test_a_label_inside_is_drawn():
    image = page()
    ImageDraw.Draw(image).rectangle((60, 30, 90, 40), fill=(30, 30, 30))
    assert drawn(image, control(20, 20, 200, 30), 1.0)


def test_a_neighbours_border_over_the_top_edge_does_not_count():
    """Chosen's clipped search box starts on the bottom border of the control above it."""
    image = page()
    ImageDraw.Draw(image).rectangle((10, 0, 250, 21), outline=(40, 40, 40))
    assert not drawn(image, control(20, 20, 200, 30), 1.0)


def test_the_scale_maps_points_to_capture_pixels():
    image = page((600, 240))
    ImageDraw.Draw(image).rectangle((120, 60, 180, 80), fill=(30, 30, 30))
    assert drawn(image, control(20, 20, 200, 30), 2.0)
    assert not drawn(image, control(20, 80, 200, 30), 2.0)


def test_chosens_closed_dropdown_shows_its_trigger_and_hides_its_search_box():
    """A crop of justice.gov's Forms page in OSWorld, at (780, 560) on the screen, with the frames
    Chrome reported for the Agency filter: Chosen's trigger, and the search box of its closed panel."""
    image = Image.open(FIXTURES / "chosen-dropdown-closed.png").convert("RGB")
    assert drawn(image, control(837 - 780, 668 - 560, 405, 32, "-Any-"), 1.0)
    assert not drawn(image, control(842 - 780, 697 - 560, 435, 31), 1.0)


def test_perceive_offers_a_hidden_control_off_the_list(monkeypatch):
    image = page((400, 200))
    ImageDraw.Draw(image).rectangle((20, 20, 120, 50), outline=(40, 40, 40))
    shown, hidden = control(20, 20, 101, 31, "Title"), control(20, 120, 200, 30)
    monkeypatch.setattr(desktop, "actionable_elements", lambda pid, w, h: ([shown, hidden], [], False))
    monkeypatch.setattr(perception, "ocr", lambda *args: [])
    screen = Screen(image=image, scale=1.0, app="Google Chrome", field=None, url=None, pid=7)
    items = perception.perceive(screen, 255, "goal")
    assert [it.text for it in items] == ["Title"]
    assert screen.offscreen == [hidden]
