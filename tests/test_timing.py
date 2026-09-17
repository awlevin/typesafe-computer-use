from typesafe_computer_use.timing import format_timing, ordered, phase, summarize


def test_phase_records_seconds_and_tolerates_none():
    timing: dict[str, float] = {}
    with phase(timing, "ocr"):
        pass
    assert timing["ocr"] >= 0.0
    with phase(None, "ocr"):
        pass


def test_phase_records_even_when_the_block_raises():
    timing: dict[str, float] = {}
    try:
        with phase(timing, "act"):
            raise ValueError("boom")
    except ValueError:
        pass
    assert "act" in timing


def test_ordered_puts_pipeline_phases_first_then_extras():
    timing = {"total": 1.4, "mystery": 0.1, "ocr": 0.8, "capture": 0.3}
    assert [name for name, _ in ordered(timing)] == ["capture", "ocr", "total", "mystery"]


def test_format_line_shows_two_decimals_in_pipeline_order():
    line = format_timing({"total": 1.45, "ocr": 0.823, "capture": 0.31, "decide": 0.21, "act": 0.05})
    assert line == "  timing: capture 0.31s  ocr 0.82s  decide 0.21s  act 0.05s  total 1.45s"


def test_format_line_omits_act_when_the_step_did_not_act():
    assert "act" not in format_timing({"capture": 0.3, "ocr": 0.8, "decide": 0.2, "act": 0.0, "total": 1.3})


def test_summarize_means_and_maxes_each_phase():
    got = summarize([{"ocr": 0.8, "total": 1.0}, {"ocr": 0.4, "total": 2.0}])
    assert got == {"steps_timed": 2, "mean": {"ocr": 0.6, "total": 1.5}, "max": {"ocr": 0.8, "total": 2.0}}


def test_summarize_averages_a_phase_over_the_steps_that_recorded_it():
    got = summarize([{"ocr": 0.8}, {"ocr": 0.4, "url": 0.6}])
    assert got["mean"] == {"ocr": 0.6, "url": 0.6} and got["max"]["url"] == 0.6


def test_summarize_of_no_steps():
    assert summarize([]) == {"steps_timed": 0, "mean": {}, "max": {}}
