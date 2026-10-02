from datetime import date

from typesafe_computer_use.dates import date_hints, describe_offset, first_date

TODAY = date(2026, 9, 16)


def test_parses_common_forms():
    assert first_date("October 13 - 15, 2026", TODAY) == date(2026, 10, 13)
    assert first_date("November 4, 2026", TODAY) == date(2026, 11, 4)
    assert first_date("Last day to book Sept 18", TODAY) == date(2026, 9, 18)
    assert first_date("Posted 9/1/2026", TODAY) == date(2026, 9, 1)
    assert first_date("2026-12-01 release", TODAY) == date(2026, 12, 1)
    assert first_date("4 Nov 2026", TODAY) == date(2026, 11, 4)


def test_ocr_dates_keep_year_and_prefer_overlapping_month_first():
    assert first_date("Jun 18,2026", TODAY) == date(2026, 6, 18)
    assert first_date("1 Apr 30, 2026", TODAY) == date(2026, 4, 30)
    assert first_date("October 13 - 15, 2026", TODAY) == date(2026, 10, 13)
    assert first_date("30 Sep 2026", TODAY) == date(2026, 9, 30)


def test_first_genuine_date_still_wins_in_multi_date_text():
    assert first_date("30 Sep 2026, then Oct 13, 2026", TODAY) == date(2026, 9, 30)
    assert first_date("Jun 18,2026, then Oct 13, 2026", TODAY) == date(2026, 6, 18)


def test_ocr_dates_reach_classifier_hints(screen, make_item):
    items = [make_item(0, "TechCrunch event Jun 18,2026"), make_item(1, "1 Apr 30, 2026", y1=200, y2=230)]
    hints = date_hints(items, screen, TODAY)
    assert hints[0] == "dated 2026-06-18 (90 days ago)"
    assert hints[1] == "dated 2026-04-30 (139 days ago)"


def test_no_date_and_invalid_date():
    assert first_date("Register Now", TODAY) is None
    assert first_date("Feb 30", TODAY) is None


def test_missing_year_rolls_forward_when_well_past():
    assert first_date("Jan 5", TODAY) == date(2027, 1, 5)
    assert first_date("Sep 1", TODAY) == date(2026, 9, 1)


def test_describe_offset():
    assert describe_offset(date(2026, 9, 16), TODAY) == "2026-09-16 (today)"
    assert describe_offset(date(2026, 10, 13), TODAY) == "2026-10-13 (in 27 days)"
    assert describe_offset(date(2026, 9, 1), TODAY) == "2026-09-01 (15 days ago)"


def test_neighbours_inherit_nearest_date(screen, make_item):
    items = [
        make_item(0, "TechCrunch Disrupt 2026 | October 13 - 15, 2026", y1=1325, y2=1355),
        make_item(1, "Register Now", y1=1359, y2=1389),
        make_item(2, "Founder Summit | November 4, 2026", y1=1601, y2=1631),
        make_item(3, "Register Now", y1=1631, y2=1661),
        make_item(4, "Footer", y1=2400, y2=2430),
    ]
    hints = date_hints(items, screen, TODAY)
    assert hints[0].startswith("dated 2026-10-13")
    assert hints[1] == "near a line dated 2026-10-13 (in 27 days)"
    assert hints[3] == "near a line dated 2026-11-04 (in 49 days)"
    assert 4 not in hints


def test_the_menu_bar_clock_dates_nothing(screen, make_item):
    items = [
        make_item(0, "Sep 16 01:54", x1=950, x2=1040, y1=8, y2=30),  # the clock, in the strip along the top
        make_item(1, "Settings - Memory usage", y1=66, y2=108),  # a tab just under it
        make_item(2, "Coldplay | Oct 2", y1=600, y2=630),
        make_item(3, "Buy", y1=640, y2=670),
    ]
    hints = date_hints(items, screen, TODAY)
    assert hints == {2: "dated 2026-10-02 (in 16 days)", 3: "near a line dated 2026-10-02 (in 16 days)"}
