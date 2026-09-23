"""Scenarios of increasing difficulty, driven through the real `runner.run` loop.

Each test builds a page graph (the world), hands the loop a policy standing in for the classifier,
and asserts three things: the outcome the runner named, the page the world ended on, and the actions
the world received. A scenario that fails says the architecture cannot do that task; it is kept as
written and marked xfail with the reason, never weakened until it passes.
"""

from __future__ import annotations

import json

import pytest
from world import FakeWriter, Page, World, drive, scripted

GOAL = "buy a ticket to the next show"

# The click point of row N, in screen points: the center of (100, 100+40N, 600, 130+40N) halved.
FIRST_ROW = (175.0, 57.5)
SECOND_ROW = (175.0, 77.5)


def test_l1_goal_already_achieved(monkeypatch, tmp_path):
    world = World([Page(name="confirmation", items=["Checkout complete", "Order 4821"], url="https://example.com/done")])

    state = drive(world, scripted(("done", None)), goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.answer is not None and state.answer.achieved
    assert "Order 4821" in state.answer.text
    assert state.history == []
    assert world.page.name == "confirmation"
    assert world.log == []


def test_l2_one_click_reaches_the_target(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="home", items=["Home", "Tickets", "About"], url="https://example.com/", on={"click:Tickets": "tickets"}),
            Page(name="tickets", items=["Buy", "Terms"], url="https://example.com/tickets"),
        ]
    )

    state = drive(
        world, scripted(("click_item", "Tickets"), ("done", None)), goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path
    )

    assert state.outcome == "done"
    assert state.history == ["clicked 'Tickets'"]
    assert world.page.name == "tickets"
    assert world.log == ["click:Tickets"]
    assert world.mouse == [SECOND_ROW]  # an OCR-only item has no element, so the mouse does the work


def test_l3_accessibility_controls_are_pressed_not_clicked(monkeypatch, tmp_path):
    world = World(
        [
            Page(
                name="home",
                items=["Home", ("Tickets", "link"), "About"],
                url="https://example.com/",
                on={"click:Tickets": "tickets"},
            ),
            Page(name="tickets", items=["Buy", "Terms"], url="https://example.com/tickets"),
        ]
    )

    state = drive(
        world, scripted(("click_item", "Tickets"), ("done", None)), goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path
    )

    assert state.outcome == "done"
    assert state.history == ["pressed 'Tickets' via accessibility"]
    assert world.page.name == "tickets"
    assert world.log == ["click:Tickets"]
    assert world.mouse == []  # the press went to the control itself, so no pixel was clicked
    assert world.fake.states[0]["screen_items_in_reading_order"][1]["role"] == "link"


def test_l4_a_slow_page_needs_waiting(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="home", items=["Home", "Tickets", "About"], url="https://example.com/", on={"click:Tickets": "tickets"}),
            Page(
                name="tickets",
                items=["Buy", "Terms"],
                url="https://example.com/tickets",
                loads_in=1,
                on={"click:Buy": "checkout"},
            ),
            Page(name="checkout", items=["Order summary", "Pay now"], url="https://example.com/checkout"),
        ]
    )
    policy = scripted(("click_item", "Tickets"), ("wait", None), ("click_item", "Buy"), ("done", None))

    state = drive(world, policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["clicked 'Tickets'", "waited", "clicked 'Buy'"]
    assert world.page.name == "checkout"
    assert world.log == ["click:Tickets", "wait", "click:Buy"]
    assert world.fake.states[1]["screen_items_in_reading_order"][0]["text"] == "Loading..."


def test_l5_a_dialog_is_dismissed_with_escape(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="home", items=["Home", "Tickets", "About"], url="https://example.com/", on={"click:Tickets": "cookies"}),
            Page(
                name="cookies",
                items=["Accept cookies", "Reject"],
                url="https://example.com/tickets",
                on={"escape": "tickets"},
            ),
            Page(name="tickets", items=["Buy", "Terms"], url="https://example.com/tickets", on={"click:Buy": "checkout"}),
            Page(name="checkout", items=["Order summary", "Pay now"], url="https://example.com/checkout"),
        ]
    )
    policy = scripted(("click_item", "Tickets"), ("press_escape", None), ("click_item", "Buy"), ("done", None))

    state = drive(world, policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["clicked 'Tickets'", "pressed Escape", "clicked 'Buy'"]
    assert world.page.name == "checkout"
    assert world.log == ["click:Tickets", "escape", "click:Buy"]


def test_l6_a_form_is_filled_and_submitted(monkeypatch, tmp_path):
    def submitted(world: World) -> str | None:
        """Return only takes the page to the results the field actually holds."""
        return "results" if world.typed.get("Search") == "bruno mars tour" else None

    world = World(
        [
            Page(
                name="search",
                items=["Search", "Popular tours"],
                url="https://example.com/",
                field="Search",
                on={"enter": submitted},
            ),
            Page(
                name="results",
                items=["First result", "Second result"],
                url="https://example.com/results",
                on={"click:First result": "detail"},
            ),
            Page(name="detail", items=["Bruno Mars", "Buy tickets"], url="https://example.com/detail"),
        ]
    )
    policy = scripted(("type_text", None), ("press_enter", None), ("click_item", "First result"), ("done", None))

    state = drive(
        world,
        policy,
        goal="search for the bruno mars tour",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=FakeWriter(text="bruno mars tour"),
    )

    assert state.outcome == "done"
    assert "typed 'bruno mars tour'" in state.history[0]
    assert state.history[1:] == ["pressed Return", "clicked 'First result'"]
    assert world.typed["Search"] == "bruno mars tour"
    assert world.page.name == "detail"
    assert world.log == ["type:bruno mars tour", "enter", "click:First result"]


def long_list_policy(state: dict, questions: dict) -> tuple:
    """Scroll until Buy is on screen, click it, and stop once it has been clicked."""
    texts = [it["text"] for it in state["screen_items_in_reading_order"]]
    if "Buy" in texts:
        return ("click_item", "Buy")
    if any("Buy" in action for action in state["previous_actions"]):
        return ("done", None)
    return ("scroll_down", None)


def test_l7_a_long_page_is_scrolled_three_times(monkeypatch, tmp_path):
    listing = "https://example.com/list"  # scrolling one page never changes the URL
    world = World(
        [
            Page(name="list", items=["Event A", "Event B"], url=listing, on={"scroll_down": "list2"}),
            Page(name="list2", items=["Event C", "Event D"], url=listing, on={"scroll_down": "list3"}),
            Page(name="list3", items=["Event E", "Event F"], url=listing, on={"scroll_down": "list4"}),
            Page(name="list4", items=["Buy"], url=listing, on={"click:Buy": "checkout"}),
            Page(name="checkout", items=["Order summary", "Pay now"], url="https://example.com/checkout"),
        ]
    )

    state = drive(world, long_list_policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["scrolled down", "scrolled down", "scrolled down", "clicked 'Buy'"]
    assert world.page.name == "checkout"
    assert world.log == ["scroll_down", "scroll_down", "scroll_down", "click:Buy"]


def test_l8_a_slow_page_needs_three_waits(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="home", items=["Home", "Tickets", "About"], url="https://example.com/", on={"click:Tickets": "tickets"}),
            Page(
                name="tickets",
                items=["Buy", "Terms"],
                url="https://example.com/tickets",
                loads_in=3,
                on={"click:Buy": "checkout"},
            ),
            Page(name="checkout", items=["Order summary", "Pay now"], url="https://example.com/checkout"),
        ]
    )
    policy = scripted(
        ("click_item", "Tickets"),
        ("wait", None),
        ("wait", None),
        ("wait", None),
        ("click_item", "Buy"),
        ("done", None),
    )

    state = drive(world, policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["clicked 'Tickets'", "waited", "waited", "waited", "clicked 'Buy'"]
    assert world.page.name == "checkout"
    assert world.log == ["click:Tickets", "wait", "wait", "wait", "click:Buy"]
    assert world.mouse == [SECOND_ROW, FIRST_ROW]


def test_l9_a_dead_end_is_undone_with_go_back(monkeypatch, tmp_path):
    world = World(
        [
            Page(
                name="home",
                items=["Blog", "Tickets"],
                url="https://example.com/",
                on={"click:Blog": "blog", "click:Tickets": "tickets"},
            ),
            Page(name="blog", items=["Our summer recap", "Older posts"], url="https://example.com/blog", on={"back": "home"}),
            Page(name="tickets", items=["Buy", "Terms"], url="https://example.com/tickets"),
        ]
    )
    policy = scripted(("click_item", "Blog"), ("go_back", None), ("click_item", "Tickets"), ("done", None))

    state = drive(world, policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["clicked 'Blog'", "went back", "clicked 'Tickets'"]
    assert world.page.name == "tickets"
    assert world.log == ["click:Blog", "back", "click:Tickets"]


def first_item_policy(state: dict, questions: dict) -> tuple:
    """The naive policy: whatever reads first on screen looks like the way forward."""
    return ("click_item", state["screen_items_in_reading_order"][0]["text"])


def test_l10_a_two_page_cycle_stops_as_stalled(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="a", items=["Next", "Page 1"], url="https://example.com/a", on={"click:Next": "b"}),
            Page(name="b", items=["Back", "Page 2"], url="https://example.com/b", on={"click:Back": "a"}),
        ]
    )

    state = drive(world, first_item_policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "stalled"
    assert len(state.history) <= 5  # the cycle is caught by the repeat rule, long before the step limit
    assert set(state.history) == {"clicked 'Next'", "clicked 'Back'"}


def test_l11_a_ticking_clock_does_not_hide_a_stall(monkeypatch, tmp_path):
    def rows(world: World) -> list[str]:
        """A departures board: one line ticks with the clock, the rest of the page never moves."""
        return [
            f"12:{world.ticks:02d}",
            "Refresh",
            "Departures",
            "Gate A",
            "Gate B",
            "Gate C",
            "Gate D",
            "Gate E",
            "Gate F",
            "Gate G",
        ]

    world = World([Page(name="board", items=rows, url="https://example.com/board")])  # "Refresh" has no transition

    state = drive(world, scripted(*[("click_item", "Refresh")] * 8), goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "stalled"
    assert len(state.history) <= 4
    assert set(state.history) == {"clicked 'Refresh'"}
    clock = [state["screen_items_in_reading_order"][0]["text"] for state in world.fake.states]
    assert len(set(clock)) == len(clock)  # the clock really did tick between the captures


def wizard_policy(state: dict, questions: dict) -> tuple:
    """Click Next while the wizard still offers one, then call it done."""
    texts = [it["text"] for it in state["screen_items_in_reading_order"]]
    return ("click_item", "Next") if "Next" in texts else ("done", None)


def test_l12_a_wizard_repeats_next_across_distinct_pages(monkeypatch, tmp_path):
    wizard = "https://example.com/signup"  # every step of the wizard lives at the same URL
    world = World(
        [
            Page(name="step1", items=["Your name", "Next"], url=wizard, on={"click:Next": "step2"}),
            Page(name="step2", items=["Your address", "Next"], url=wizard, on={"click:Next": "step3"}),
            Page(name="step3", items=["Your payment", "Next"], url=wizard, on={"click:Next": "finished"}),
            Page(name="finished", items=["All set", "Receipt"], url=wizard),
        ]
    )

    state = drive(world, wizard_policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["clicked 'Next'"] * 3  # the same action three times, on three different screens
    assert world.page.name == "finished"
    assert world.log == ["click:Next"] * 3


def test_l13_an_offscreen_control_is_pressed(monkeypatch, tmp_path):
    world = World(
        [
            Page(
                name="home",
                items=["Welcome", "About"],
                url="https://example.com/",
                offscreen=("Register Now",),
                on={"press:Register Now": "registration"},
            ),
            Page(name="registration", items=["Full name", "Submit"], url="https://example.com/register"),
        ]
    )
    policy = scripted(("press_offscreen", "Register Now"), ("done", None))

    state = drive(world, policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["pressed 'Register Now' (off-screen control) via accessibility"]
    assert world.page.name == "registration"
    assert world.log == ["press:Register Now"]
    assert world.mouse == []  # there is no pixel to click: the control is parked above the viewport


def always_type_policy(state: dict, questions: dict) -> tuple:
    return ("type_text", None)


def test_l14_a_field_the_writer_declines_is_never_typed_and_the_run_stalls(monkeypatch, tmp_path):
    world = World(
        [
            Page(
                name="login",
                items=["Password", "Sign in"],
                url="https://example.com/login",
                field="Password",
                on={"click:Sign in": "account"},
            ),
            Page(name="account", items=["Your account"], url="https://example.com/account"),
        ]
    )

    state = drive(
        world,
        always_type_policy,
        goal=GOAL,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=FakeWriter(text=""),  # the writer will not put a password in a password field
    )

    assert state.outcome == "stalled"
    assert world.typed == {}
    # The same refusal on the same screen three times over: the repeat rule is what ends it, and
    # none of the three touched the machine.
    assert len(state.history) == 3
    assert all("refused" in line for line in state.history)
    assert world.page.name == "login"
    assert world.log == []


def test_l15_use_browser_from_another_app_opens_a_catalog_site(monkeypatch, tmp_path):
    world = World(
        [
            Page(
                name="finder",
                items=["Documents", "Downloads"],
                url=None,
                app="Finder",
                on={"open:https://github.com/": "github"},
            ),
            Page(
                name="github",
                items=["Sign in", "Pull requests"],
                url="https://github.com/",
                on={"click:Sign in": "signin"},
            ),
            Page(name="signin", items=["Username", "Password"], url="https://github.com/login"),
        ]
    )
    policy = scripted(("use_browser", "github"), ("click_item", "Sign in"), ("done", None))

    state = drive(world, policy, goal="sign in to github", monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history[0] == "opened https://github.com/"
    assert state.history == ["opened https://github.com/", "clicked 'Sign in'"]
    assert world.page.name == "signin"
    assert world.log == ["open:https://github.com/", "click:Sign in"]
    assert world.fake.states[0]["frontmost_app"] == "Finder"


def test_l16_a_page_that_never_loads_stops_within_the_idle_budget(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="home", items=["Home", "Tickets", "About"], url="https://example.com/", on={"click:Tickets": "tickets"}),
            Page(name="tickets", items=["Buy", "Terms"], url="https://example.com/tickets", loads_in=10),
        ]
    )
    policy = scripted(("click_item", "Tickets"), *[("wait", None)] * 10)

    state = drive(world, policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "stalled"
    assert state.history == ["clicked 'Tickets'"] + ["waited"] * 3
    assert world.page.name == "tickets"
    assert world.log == ["click:Tickets"] + ["wait"] * 3


def test_l17_low_confidence_stops_the_run(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="home", items=["Home", "Tickets", "About"], url="https://example.com/", on={"click:Tickets": "tickets"}),
            Page(name="tickets", items=["Buy", "Terms"], url="https://example.com/tickets"),
        ]
    )

    state = drive(world, scripted(("click_item", "Tickets", 0.3)), goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "low confidence"
    assert state.history == []
    assert world.page.name == "home"
    assert world.log == []
    assert world.mouse == []


def always_scroll_policy(state: dict, questions: dict) -> tuple:
    return ("scroll_down", None)


def test_l18_the_step_limit_ends_an_endless_list(monkeypatch, tmp_path):
    listing = "https://example.com/feed"
    world = World(
        [
            Page(name=f"list{n}", items=[f"Post {2 * n - 1}", f"Post {2 * n}"], url=listing, on={"scroll_down": f"list{n + 1}"})
            for n in range(1, 7)
        ]
    )

    state = drive(world, always_scroll_policy, goal=GOAL, steps=3, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "step limit"
    assert state.history == ["scrolled down"] * 3
    assert world.page.name == "list4"
    assert world.log == ["scroll_down"] * 3


def test_l19_typing_that_fails_verification_gives_the_field_its_old_value_back(monkeypatch, tmp_path):
    world = World([Page(name="search", items=["Search", "Popular tours"], url="https://example.com/", field="Search")])
    world.typed["Search"] = "old query"
    policy = scripted(("type_text", None), ("done", None))

    state = drive(
        world,
        policy,
        goal="search for the bruno mars tour",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=FakeWriter(text="bruno mars tour"),
        noul=0.2,  # the classifier does not believe the field holds what was typed
    )

    assert state.outcome == "done"
    assert "verification failed" in state.history[0] and "restored previous value" in state.history[0]
    assert world.typed["Search"] == "old query"
    # Set through the element both ways: no Select All and Delete, which go wherever the focus is now.
    assert world.log == ["type:bruno mars tour", "type:old query"]


def steering_policy(state: dict, questions: dict) -> tuple:
    """Click the first link the state does not list as already tried here; stop when Buy was clicked."""
    if any("Buy" in action for action in state["previous_actions"]):
        return ("done", None)
    tried = state["already_tried_on_this_screen"]
    for it in state["screen_items_in_reading_order"]:
        if f"clicked {it['text']!r}" not in tried:
            return ("click_item", it["text"])
    return ("none", None)


def test_l20_a_dead_link_is_steered_around_with_what_was_already_tried(monkeypatch, tmp_path):
    """The first link leads back to the same screen. The state says so; the policy takes the second."""
    world = World(
        [
            Page(name="home", items=["Sponsors", "Tickets"], url="https://example.com/", on={"click:Tickets": "tickets"}),
            Page(name="tickets", items=["Buy"], url="https://example.com/tickets", on={"click:Buy": "checkout"}),
            Page(name="checkout", items=["Order summary", "Pay now"], url="https://example.com/checkout"),
        ]
    )

    state = drive(world, steering_policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["clicked 'Sponsors'", "clicked 'Tickets'", "clicked 'Buy'"]
    assert world.page.name == "checkout"
    assert world.fake.states[0]["already_tried_on_this_screen"] == []
    assert world.fake.states[1]["already_tried_on_this_screen"] == ["clicked 'Sponsors'"]
    assert world.fake.states[2]["already_tried_on_this_screen"] == []


def test_l21_a_composite_task_runs_the_whole_pipeline(monkeypatch, tmp_path):
    """Every seam in one run: the browser, a modal, a slow page, a scroll, a field, and a result."""

    def searched(world: World) -> str | None:
        return "results" if world.typed.get("Search") == "bruno mars" else None

    listing = "https://shows.example.com/listing"
    world = World(
        [
            Page(
                name="finder",
                items=["Documents", "Downloads"],
                url=None,
                app="Finder",
                on={"open:https://shows.example.com/": "cookies"},
            ),
            Page(
                name="cookies",
                items=["Accept cookies", "Reject"],
                url="https://shows.example.com/",
                on={"escape": "listing"},
            ),
            Page(name="listing", items=["Event A", "Event B"], url=listing, loads_in=2, on={"scroll_down": "listing2"}),
            Page(name="listing2", items=[("Search", "field"), "Event C"], url=listing, on={"click:Search": "search"}),
            Page(
                name="search",
                items=["Search", "Popular tours"],
                url="https://shows.example.com/search",
                field="Search",
                on={"enter": searched},
            ),
            Page(
                name="results",
                items=["First result", "Second result"],
                url="https://shows.example.com/results",
                on={"click:First result": "detail"},
            ),
            Page(name="detail", items=["Bruno Mars", "Buy tickets"], url="https://shows.example.com/detail"),
        ]
    )
    policy = scripted(
        ("use_browser", "other"),
        ("press_escape", None),
        ("wait", None),
        ("wait", None),
        ("scroll_down", None),
        ("click_item", "Search"),
        ("type_text", None),
        ("press_enter", None),
        ("click_item", "First result"),
        ("done", None),
    )

    state = drive(
        world,
        policy,
        goal="find the bruno mars show",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=FakeWriter(text="bruno mars", url="https://shows.example.com/"),
    )

    assert state.outcome == "done"
    assert state.history[:6] == [
        "opened https://shows.example.com/",
        "pressed Escape",
        "waited",
        "waited",
        "scrolled down",
        "pressed 'Search' via accessibility",
    ]
    assert state.history[6] == "typed 'bruno mars' into 'Search' via accessibility (verified 0.95)"
    assert state.history[7:] == ["pressed Return", "clicked 'First result'"]
    assert world.page.name == "detail"
    assert world.log == [
        "open:https://shows.example.com/",
        "escape",
        "wait",
        "wait",
        "scroll_down",
        "click:Search",
        "type:bruno mars",
        "enter",
        "click:First result",
    ]


LINKS = [f"Link {letter}" for letter in "ABCDEFGHIJ"]


def hub_policy(state: dict, questions: dict) -> tuple:
    """Take the first link this screen has not been sent down before; back out of every dead end."""
    texts = [it["text"] for it in state["screen_items_in_reading_order"]]
    if "Buy tickets" in texts:
        return ("done", None)
    if "Nothing here" in texts:
        return ("go_back", None)
    tried = state["already_tried_on_this_screen"]
    for text in texts:
        if f"clicked {text!r}" not in tried:
            return ("click_item", text)
    return ("none", None)


def test_l22_a_hub_of_dead_links_is_searched_beyond_the_history_window(monkeypatch, tmp_path):
    dead = [
        Page(
            name=f"dead{letter}",
            items=["Nothing here", f"Dead {letter}"],
            url=f"https://example.com/{letter.lower()}",
            on={"back": "hub"},
        )
        for letter in "ABCDEFGHI"
    ]
    hub = Page(
        name="hub",
        items=LINKS,
        url="https://example.com/",
        on={**{f"click:Link {letter}": f"dead{letter}" for letter in "ABCDEFGHI"}, "click:Link J": "target"},
    )
    world = World([hub, *dead, Page(name="target", items=["Buy tickets", "Terms"], url="https://example.com/j")])

    state = drive(world, hub_policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert len(state.history) == 19  # nine dead links, nine ways back, and the tenth link
    assert state.history[-1] == "clicked 'Link J'"
    assert world.page.name == "target"
    last_hub = world.fake.states[18]  # the hub as it looked the step Link J was clicked
    assert len(last_hub["previous_actions"]) == 8
    assert not any("Link A" in action for action in last_hub["previous_actions"])  # fallen out of the window
    assert last_hub["already_tried_on_this_screen"] == [f"clicked 'Link {letter}'" for letter in "ABCDEFGHI"]


def modal_policy(state: dict, questions: dict) -> tuple:
    """Escape a modal once; if the screen is still there, the modal wants its own button pressed."""
    texts = [it["text"] for it in state["screen_items_in_reading_order"]]
    if "Sign up for news" in texts:
        if "pressed Escape" in state["already_tried_on_this_screen"]:
            return ("click_item", "Close")
        return ("press_escape", None)
    if "Order summary" in texts:
        return ("done", None)
    return ("click_item", "Buy" if "Buy" in texts else "Tickets")


def test_l23_a_modal_escape_cannot_close_is_closed_by_its_button(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="home", items=["Home", "Tickets"], url="https://example.com/", on={"click:Tickets": "modal"}),
            Page(
                name="modal",
                items=["Sign up for news", "Close"],
                url="https://example.com/tickets",
                on={"click:Close": "tickets"},  # escape does nothing here
            ),
            Page(name="tickets", items=["Buy", "Terms"], url="https://example.com/tickets", on={"click:Buy": "checkout"}),
            Page(name="checkout", items=["Order summary", "Pay now"], url="https://example.com/checkout"),
        ]
    )

    state = drive(world, modal_policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["clicked 'Tickets'", "pressed Escape", "clicked 'Close'", "clicked 'Buy'"]
    assert world.page.name == "checkout"
    assert world.log == ["click:Tickets", "escape", "click:Close", "click:Buy"]


def test_l24_type_email_fills_the_focused_email_field(monkeypatch, tmp_path):
    world = World(
        [
            Page(
                name="login",
                items=["Email", "Sign in"],
                url="https://example.com/login",
                field="Email",
                on={"click:Sign in": "account"},
            ),
            Page(name="account", items=["Your account"], url="https://example.com/account"),
        ]
    )
    policy = scripted(("type_email", None), ("done", None))

    state = drive(world, policy, goal="sign in", monkeypatch=monkeypatch, tmp_path=tmp_path, email="user@example.com")

    assert state.outcome == "done"
    assert state.history == ["typed email via accessibility"]
    assert world.typed["Email"] == "user@example.com"
    assert world.log == ["type:user@example.com"]
    assert "type_email" in world.fake.asked[0]["kind"].criteria  # the action is only offered when an email is known


def test_l25_a_stalled_run_still_answers_from_the_last_screen(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="a", items=["Next", "Page 1"], url="https://example.com/a", on={"click:Next": "b"}),
            Page(name="b", items=["Back", "Page 2"], url="https://example.com/b", on={"click:Back": "a"}),
        ]
    )

    state = drive(world, first_item_policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "stalled"
    assert state.answer is not None
    # The answer leads with the screen the run ended on; what follows is the screens seen before it.
    assert state.answer.text.startswith(" ".join(world.page.items))
    assert len(world.log) == len(state.history)  # re-reading the screen costs no action


def test_l26_a_refused_action_then_a_good_one_does_not_stall(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="home", items=["Home", "Tickets"], url="https://example.com/", on={"click:Tickets": "tickets"}),
            Page(name="tickets", items=["Buy", "Terms"], url="https://example.com/tickets"),
        ]
    )
    policy = scripted(("type_text", None), ("click_item", "Tickets"), ("done", None))

    state = drive(world, policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["type_text refused: no text field is focused", "clicked 'Tickets'"]
    assert world.page.name == "tickets"
    assert world.log == ["click:Tickets"]  # the refusal never reached the machine


def test_l27_scrolling_past_the_end_stops_as_a_repeated_action(monkeypatch, tmp_path):
    listing = "https://example.com/list"
    world = World(
        [
            Page(name="list", items=["Event A", "Event B"], url=listing, on={"scroll_down": "list2"}),
            Page(name="list2", items=["Event C", "Event D"], url=listing),  # the end: scrolling does nothing
        ]
    )

    state = drive(world, always_scroll_policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "stalled"
    # One scroll that moved, then three at the bottom: the last of those is the second scroll
    # already taken on this screen, so the repeat rule ends it.
    assert state.history == ["scrolled down"] * 4
    assert world.page.name == "list2"


def answer_packet(writer: FakeWriter) -> dict:
    """The packet of the writer's last call: the one that composed the answer."""
    return json.loads(writer.requests[-1]["messages"][0]["content"][-1]["text"])


def step_record(tmp_path, step: int) -> dict:
    """The answers file the loop wrote for one step, which carries where the stop rules stood."""
    return json.loads((tmp_path / "run" / f"step-{step:03d}-answers.json").read_text())


def test_l28_the_answer_can_use_a_screen_seen_on_the_way(monkeypatch, tmp_path):
    """The price was two screens back. The run ended on checkout, and the answer still has to carry it."""
    world = World(
        [
            Page(name="home", items=["Home", "Tickets"], url="https://example.com/", on={"click:Tickets": "tickets"}),
            Page(
                name="tickets",
                items=["Standard $45", "VIP $120", "Buy"],
                url="https://example.com/tickets",
                on={"click:Buy": "checkout"},
            ),
            Page(name="checkout", items=["Order summary", "Pay now"], url="https://example.com/checkout"),
        ]
    )
    policy = scripted(("click_item", "Tickets"), ("click_item", "Buy"), ("done", None))
    writer = FakeWriter()

    state = drive(
        world,
        policy,
        goal="find the ticket price and go to checkout",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=writer,
    )

    assert state.outcome == "done"
    assert world.page.name == "checkout"
    assert state.answer is not None and "$45" in state.answer.text
    assert answer_packet(writer)["earlier_screens"][-1]["url"] == "https://example.com/tickets"


def test_l29_keystrokes_replace_what_a_field_already_holds(monkeypatch, tmp_path):
    def searched(world: World) -> str | None:
        """Only the exact query reaches the results: a field typed on top of its old value does not."""
        return "results" if world.typed.get("Search") == "bruno mars tour" else None

    world = World(
        [
            Page(
                name="search",
                items=["Search", "Popular tours"],
                url="https://example.com/",
                field="Search",
                no_ax_value=True,  # this field refuses the value path, so the keystrokes run
                on={"enter": searched},
            ),
            Page(name="results", items=["First result", "Second result"], url="https://example.com/results"),
        ]
    )
    world.typed["Search"] = "old query"  # the field is not empty when the loop first sees it
    policy = scripted(("type_text", None), ("press_enter", None), ("done", None))

    state = drive(
        world,
        policy,
        goal="search for the bruno mars tour",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=FakeWriter(text="bruno mars tour"),
    )

    assert state.outcome == "done"
    assert "via keystrokes" in state.history[0]
    assert world.typed["Search"] == "bruno mars tour"
    assert world.log.index("clear_field") < world.log.index("type:bruno mars tour")
    assert world.page.name == "results"


def test_l30_focus_stolen_by_another_app_is_taken_back_with_use_browser(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="tickets", items=["Buy", "Terms"], url="https://example.com/tickets", on={"click:Buy": "finder"}),
            Page(name="finder", items=["Downloads", "receipt.pdf"], url=None, app="Finder", on={"activate": "checkout"}),
            Page(name="checkout", items=["Order summary", "Pay now"], url="https://example.com/checkout"),
        ]
    )
    policy = scripted(("click_item", "Buy"), ("use_browser", "none"), ("done", None))

    state = drive(world, policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["clicked 'Buy'", "activated Google Chrome"]
    assert world.page.name == "checkout"
    assert world.log == ["click:Buy", "activate"]


def test_l31_an_unusable_writer_url_is_refused_and_the_catalog_is_used_instead(monkeypatch, tmp_path):
    world = World(
        [
            Page(
                name="finder",
                items=["Documents", "Downloads"],
                url=None,
                app="Finder",
                on={"open:https://github.com/": "github"},
            ),
            Page(name="github", items=["Sign in", "Pull requests"], url="https://github.com/"),
        ]
    )
    policy = scripted(("use_browser", "other"), ("use_browser", "github"), ("done", None))

    state = drive(
        world,
        policy,
        goal="open the issue tracker",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=FakeWriter(url="http://insecure.example.com/"),  # not https: the writer's proposal is dropped
    )

    assert state.outcome == "done"
    assert state.history[0] == "use_browser refused: the writer proposed no usable URL for this goal"
    assert state.history[1] == "opened https://github.com/"
    assert world.page.name == "github"
    assert world.log == ["open:https://github.com/"]
    assert not any("insecure.example.com" in action for action in world.log)


def growing_feed(world: World) -> list[str]:
    """A feed that gains a post every time it is read."""
    return [f"Post {n}" for n in range(1, world.ticks + 4)] + ["Load more"]


def test_l32_a_feed_that_grows_every_step_is_not_mistaken_for_a_stall(monkeypatch, tmp_path):
    world = World([Page(name="feed", items=growing_feed, url="https://example.com/feed")])
    policy = scripted(*[("click_item", "Load more")] * 5)

    state = drive(world, policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["clicked 'Load more'"] * 5  # every capture showed a longer page, so nothing stalled
    assert len(world.fake.states[-1]["screen_items_in_reading_order"]) > len(
        world.fake.states[0]["screen_items_in_reading_order"]
    )


def test_l32b_a_feed_that_never_grows_is_a_stall(monkeypatch, tmp_path):
    world = World([Page(name="feed", items=["Post 1", "Post 2", "Post 3", "Load more"], url="https://example.com/feed")])
    policy = scripted(*[("click_item", "Load more")] * 8)

    state = drive(world, policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "stalled"
    # Three clicks either way: the repeat rule fires on the third, and with it gone the idle rule
    # would stop step 4 before it acted. The two rules agree here; L10 and L16 tell them apart.
    assert state.history == ["clicked 'Load more'"] * 3


def coldplay_policy(state: dict, questions: dict) -> tuple:
    """Three buttons read 'Buy'. Only the row they sit in says which show they buy."""
    for it in state["screen_items_in_reading_order"]:
        if it["text"] == "Buy" and "Coldplay" in it.get("beside", []):
            return ("click_item", it["i"])  # by index: the text alone names three items
    return ("done", None)


def test_l33_duplicate_labels_are_told_apart_by_their_row(monkeypatch, tmp_path):
    world = World(
        [
            Page(
                name="listing",
                items=[
                    ["Bruno Mars", "Sep 25", "Buy"],
                    ["Coldplay", "Oct 2", "Buy"],
                    ["Adele", "Oct 9", "Buy"],
                ],
                url="https://example.com/listing",
                on={
                    "click:Buy@0": "bruno_checkout",
                    "click:Buy@1": "coldplay_checkout",
                    "click:Buy@2": "adele_checkout",
                },
            ),
            Page(name="bruno_checkout", items=["Order summary", "Bruno Mars"], url="https://example.com/checkout/bruno"),
            Page(name="coldplay_checkout", items=["Order summary", "Coldplay"], url="https://example.com/checkout/coldplay"),
            Page(name="adele_checkout", items=["Order summary", "Adele"], url="https://example.com/checkout/adele"),
        ]
    )

    state = drive(world, coldplay_policy, goal="buy a ticket to Coldplay", monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == [
        "clicked 'Buy' beside 'Coldplay', 'Oct 2'"
    ]  # the line says which Buy, so trying one does not mark them all
    assert world.page.name == "coldplay_checkout"
    assert world.log == ["click:Buy@1"]

    listing = world.fake.states[0]["screen_items_in_reading_order"]
    chosen = next(it for it in listing if it["text"] == "Buy" and "Coldplay" in it.get("beside", []))
    assert chosen["beside"] == ["Coldplay", "Oct 2"]
    assert "in the row of 'Coldplay', 'Oct 2'" in world.fake.asked[0]["item"].criteria[str(chosen["i"])]
    unique = next(it for it in listing if it["text"] == "Coldplay")
    assert "beside" not in unique  # nothing else on screen reads 'Coldplay', so the row says nothing new


def banner_policy(state: dict, questions: dict) -> tuple:
    texts = [it["text"] for it in state["screen_items_in_reading_order"]]
    return ("done", None) if "Order summary" in texts else ("click_item", "Buy")


def test_l34_a_banner_covering_the_page_takes_the_first_click(monkeypatch, tmp_path):
    tickets = "https://example.com/tickets"
    world = World(
        [
            Page(
                name="tickets",
                items=["Buy", "Terms", "Accept cookies"],
                url=tickets,
                covered_by="Accept cookies",  # the banner is over the page: every click lands on it
                on={"click:Accept cookies": "tickets_clear"},
            ),
            Page(name="tickets_clear", items=["Buy", "Terms"], url=tickets, on={"click:Buy": "checkout"}),
            Page(name="checkout", items=["Order summary", "Pay now"], url="https://example.com/checkout"),
        ]
    )

    state = drive(world, banner_policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["clicked 'Buy'", "clicked 'Buy'"]  # the loop aimed at Buy twice
    assert world.log == ["click:Accept cookies", "click:Buy"]  # the banner took the first one
    assert world.page.name == "checkout"


def crawl_policy(state: dict, questions: dict) -> tuple:
    """A depth-first crawl written only against the state: take the first untried link, else back out."""
    texts = [it["text"] for it in state["screen_items_in_reading_order"]]
    if any("Buy" in action for action in state["previous_actions"]):
        return ("done", None)
    if "Buy" in texts:
        return ("click_item", "Buy")
    tried = state["already_tried_on_this_screen"]
    for text in texts:
        if not text.startswith("Page: ") and f"clicked {text!r}" not in tried:
            return ("click_item", text)
    return ("none", None) if "Page: home" in texts else ("go_back", None)


def test_l35_a_small_site_is_searched_exhaustively_for_the_one_page_that_sells(monkeypatch, tmp_path):
    site = "https://example.com"
    world = World(
        [
            Page(
                name="home",
                items=["Page: home", "About", "Contact", "Shows"],
                url=f"{site}/",
                on={"click:About": "about", "click:Contact": "contact", "click:Shows": "shows"},
            ),
            Page(name="about", items=["Page: about"], url=f"{site}/about", on={"back": "home"}),
            Page(name="contact", items=["Page: contact"], url=f"{site}/contact", on={"back": "home"}),
            Page(
                name="shows",
                items=["Page: shows", "Past", "Upcoming"],
                url=f"{site}/shows",
                on={"click:Past": "past", "click:Upcoming": "upcoming", "back": "home"},
            ),
            Page(name="past", items=["Page: past"], url=f"{site}/shows/past", on={"back": "shows"}),
            Page(
                name="upcoming",
                items=["Page: upcoming", "Buy"],
                url=f"{site}/shows/upcoming",
                on={"click:Buy": "checkout", "back": "shows"},
            ),
            Page(name="checkout", items=["Page: checkout", "Order summary"], url=f"{site}/checkout"),
        ]
    )

    state = drive(world, crawl_policy, goal="buy a ticket", monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert world.page.name == "checkout"
    assert state.history == [
        "clicked 'About'",
        "went back",
        "clicked 'Contact'",
        "went back",
        "clicked 'Shows'",
        "clicked 'Past'",
        "went back",
        "clicked 'Upcoming'",
        "clicked 'Buy'",
    ]
    assert len(state.history) == 9  # nothing stalled: every step of the crawl moved the screen


def long_run_policy(state: dict, questions: dict) -> tuple:
    """One policy for the whole run, reading nothing but the state the loop hands it."""
    texts = [it["text"] for it in state["screen_items_in_reading_order"]]
    tried = state["already_tried_on_this_screen"]
    field = state["focused_field"]
    if "Loading..." in texts or "Verifying" in texts:
        return ("wait", None)
    if state["frontmost_app"] == "Finder":  # the first Finder needs a site; the second only the front
        return ("use_browser", "other" if "Desktop" in texts else "none")
    if "Order summary" in texts:
        return ("done", None)
    if "Sign up for news" in texts:
        return ("click_item", "Close") if "pressed Escape" in tried else ("press_escape", None)
    if field and field["label"] == "Email":
        return ("press_enter", None) if field["current_value"] else ("type_email", None)
    for it in state["screen_items_in_reading_order"]:
        if it["text"] == "Buy" and "Coldplay" in it.get("beside", []):
            return ("click_item", it["i"])
    if "Nothing here" in texts:
        return ("go_back", None)
    if "Older posts" in texts:
        return ("go_back", None) if "clicked 'Older posts'" in tried else ("click_item", "Older posts")
    if "Blog" in texts and "clicked 'Blog'" not in tried:
        return ("click_item", "Blog")
    if state.get("offscreen_controls"):
        return ("press_offscreen", "Show all dates")
    if "Next" in texts:
        return ("click_item", "Next")
    if "Buy" in texts:
        return ("click_item", "Buy")
    return ("scroll_down", None)


def long_run_world() -> World:
    shows = "https://shows.example.com"
    return World(
        [
            Page(name="finder", items=["Desktop", "Documents"], url=None, app="Finder", on={f"open:{shows}/": "front"}),
            Page(
                name="front",
                items=["Buy", "Terms", "Accept cookies"],
                url=f"{shows}/",
                covered_by="Accept cookies",
                on={"click:Accept cookies": "listing"},
            ),
            Page(
                name="listing",
                items=["Listing", "Blog", "Event A"],
                url=f"{shows}/listing",
                loads_in=2,
                offscreen=("Show all dates",),
                on={"click:Blog": "blog", "press:Show all dates": "dates1"},
            ),
            Page(
                name="blog",
                items=["Blog post", "Older posts"],
                url=f"{shows}/blog",
                on={"click:Older posts": "blog2", "back": "listing"},
            ),
            Page(name="blog2", items=["Older posts page", "Nothing here"], url=f"{shows}/blog/older", on={"back": "blog"}),
            Page(name="dates1", items=["All dates", "Event C"], url=f"{shows}/dates", on={"scroll_down": "dates2"}),
            Page(name="dates2", items=["More dates", "Event D"], url=f"{shows}/dates", on={"scroll_down": "dates3"}),
            Page(name="dates3", items=["Even more", "Event E"], url=f"{shows}/dates", on={"scroll_down": "rows"}),
            Page(
                name="rows",
                items=[
                    ["Bruno Mars", "Sep 25", "$45", "Buy"],
                    ["Coldplay", "Oct 2", "$60", "Buy"],
                    ["Adele", "Oct 9", "$80", "Buy"],
                ],
                url=f"{shows}/dates/rows",
                on={"click:Buy@0": "modal", "click:Buy@1": "modal", "click:Buy@2": "modal"},
            ),
            Page(
                name="modal",
                items=["Sign up for news", "Close"],
                url=f"{shows}/dates/rows",
                on={"click:Close": "login"},  # Escape does nothing to this one
            ),
            Page(name="login", items=["Sign in", "Email"], url=f"{shows}/login", field="Email", on={"enter": "verifying"}),
            Page(
                name="verifying",
                items=["Verifying", "Almost done"],
                url=f"{shows}/verify",
                loads_in=3,
                on={"wait": "finder2"},  # the page finishes by itself, into a window that steals the front
            ),
            Page(name="finder2", items=["Downloads", "receipt.pdf"], url=None, app="Finder", on={"activate": "wiz1"}),
            Page(name="wiz1", items=["Step 1 of 3", "Next"], url=f"{shows}/signup", on={"click:Next": "wiz2"}),
            Page(name="wiz2", items=["Step 2 of 3", "Next"], url=f"{shows}/signup", on={"click:Next": "wiz3"}),
            Page(name="wiz3", items=["Step 3 of 3", "Next"], url=f"{shows}/signup", on={"click:Next": "checkout"}),
            Page(name="checkout", items=["Order summary", "Pay now"], url=f"{shows}/checkout"),
        ]
    )


def test_l36_a_long_run_mixes_everything(monkeypatch, tmp_path):
    world = long_run_world()

    state = drive(
        world,
        long_run_policy,
        goal="buy a ticket to Coldplay and tell me the price",
        steps=40,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=FakeWriter(url="https://shows.example.com/"),
        email="user@example.com",
    )

    assert state.outcome == "done"
    assert world.page.name == "checkout"
    assert state.history == [
        "opened https://shows.example.com/",
        "clicked 'Buy'",
        "waited",
        "waited",
        "clicked 'Blog'",
        "clicked 'Older posts'",
        "went back",
        "went back",
        "pressed 'Show all dates' (off-screen control) via accessibility",
        "scrolled down",
        "scrolled down",
        "scrolled down",
        "clicked 'Buy' beside 'Coldplay', 'Oct 2', '$60'",
        "pressed Escape",
        "clicked 'Close'",
        "typed email via accessibility",
        "pressed Return",
        "waited",
        "waited",
        "waited",
        "waited",
        "activated Google Chrome",
        "clicked 'Next'",
        "clicked 'Next'",
        "clicked 'Next'",
    ]
    assert len(state.history) == 25
    assert world.log[world.log.index("click:Buy@1")] == "click:Buy@1"  # the Coldplay row, picked by what sits beside it
    login = next(i for i, s in enumerate(world.fake.states) if s["focused_field"] and s["focused_field"]["label"] == "Email")
    assert "type_email" in world.fake.asked[login]["kind"].criteria
    assert state.answer is not None and "$60" in state.answer.text  # the price was on a screen the run passed through


LISTING_ROWS = [f"Row {n}" for n in range(1, 41)] + ["Buy"]


def modal_listing_policy(state: dict, questions: dict) -> tuple:
    """Dismiss the modal when it is up, otherwise buy; stop once Buy has been clicked twice."""
    texts = [it["text"] for it in state["screen_items_in_reading_order"]]
    if sum(1 for action in state["previous_actions"] if "Buy" in action) >= 2:
        return ("done", None)
    return ("click_item", "Close" if "Close" in texts else "Buy")


def test_l37_a_small_modal_on_a_dense_page_is_not_mistaken_for_no_change(monkeypatch, tmp_path):
    """Two lines over forty is a small share of the page, but it is a modal: the screen did change."""
    listing = "https://example.com/listing"
    world = World(
        [
            Page(name="listing", items=LISTING_ROWS, url=listing, on={"click:Buy": "modal"}),
            Page(
                name="modal",
                items=[*LISTING_ROWS, "Sign up for news", "Close"],
                url=listing,
                on={"click:Close": "listing_after"},
            ),
            Page(name="listing_after", items=LISTING_ROWS, url=listing, on={"click:Buy": "checkout"}),
            Page(name="checkout", items=["Order summary", "Pay now"], url="https://example.com/checkout"),
        ]
    )

    state = drive(world, modal_listing_policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["clicked 'Buy'", "clicked 'Close'", "clicked 'Buy'"]
    assert world.page.name == "checkout"
    assert world.log == ["click:Buy", "click:Close", "click:Buy"]
    # Step 2 captured the modal and step 3 the page behind it again. Two lines over forty is well
    # under the one-in-ten bar, so only the absolute one-line limit can call either a new screen:
    # were it not there, step 2 would read as the first idle action and step 3 as the second.
    assert step_record(tmp_path, 2)["idle_actions"] == 0
    assert step_record(tmp_path, 3)["idle_actions"] == 0


def two_tickers(world: World) -> list[str]:
    """A dense page with two lines that move on their own: a clock and an unread badge."""
    return [f"12:{world.ticks:02d}", f"{world.ticks} new", *[f"Row {n}" for n in range(1, 31)], "Refresh"]


def refresh_policy(state: dict, questions: dict) -> tuple:
    return ("click_item", "Refresh")


def test_l38_two_tickers_on_a_dense_page_let_a_futile_run_reach_the_step_limit(monkeypatch, tmp_path):
    """Two lines changing every capture is more than one, so every capture reads as a new screen.

    Nothing the run does achieves anything, and neither stop rule fires: the budget is spent instead.
    That is the direction the rules are meant to err in -- a futile run that costs its steps, never a
    working run cut short by a false stall -- and this scenario is here to keep that trade-off honest.
    """
    world = World([Page(name="board", items=two_tickers, url="https://example.com/board")])  # Refresh does nothing

    state = drive(world, refresh_policy, goal=GOAL, steps=6, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "step limit"
    assert state.history == ["clicked 'Refresh'"] * 6
    assert world.log == ["click:Refresh"] * 6


def picky_buy_policy(state: dict, questions: dict) -> tuple:
    """Take the first Buy this listing has not been through yet; back out of any checkout but Coldplay's."""
    texts = [it["text"] for it in state["screen_items_in_reading_order"]]
    if "Coldplay checkout" in texts:
        return ("done", None)
    if any(text.endswith("checkout") for text in texts):
        return ("go_back", None)
    for it in state["screen_items_in_reading_order"]:
        if it["text"] == "Buy":
            line = "clicked 'Buy' beside " + ", ".join(repr(mate) for mate in it["beside"])
            if line not in state["already_tried_on_this_screen"]:
                return ("click_item", it["i"])
    return ("none", None)


def test_l39_the_wrong_buy_is_undone_and_the_right_one_is_not_marked_as_tried(monkeypatch, tmp_path):
    """Trying one row's Buy must not read as having tried the others: the history line says which row."""
    world = World(
        [
            Page(
                name="listing",
                items=[
                    ["Bruno Mars", "Sep 25", "Buy"],
                    ["Coldplay", "Oct 2", "Buy"],
                    ["Adele", "Oct 9", "Buy"],
                ],
                url="https://example.com/listing",
                on={
                    "click:Buy@0": "bruno_checkout",
                    "click:Buy@1": "coldplay_checkout",
                    "click:Buy@2": "adele_checkout",
                },
            ),
            Page(
                name="bruno_checkout",
                items=["Bruno Mars checkout", "Pay"],
                url="https://example.com/checkout/bruno",
                on={"back": "listing"},
            ),
            Page(name="coldplay_checkout", items=["Coldplay checkout", "Pay"], url="https://example.com/checkout/coldplay"),
            Page(
                name="adele_checkout",
                items=["Adele checkout", "Pay"],
                url="https://example.com/checkout/adele",
                on={"back": "listing"},
            ),
        ]
    )

    state = drive(world, picky_buy_policy, goal="buy a ticket to Coldplay", monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == [
        "clicked 'Buy' beside 'Bruno Mars', 'Sep 25'",
        "went back",
        "clicked 'Buy' beside 'Coldplay', 'Oct 2'",
    ]
    assert world.page.name == "coldplay_checkout"
    assert world.log == ["click:Buy@0", "back", "click:Buy@1"]
    assert state.repeats == 0  # the second Buy is a different line, so nothing reads as a repeat
    assert world.fake.states[2]["already_tried_on_this_screen"] == ["clicked 'Buy' beside 'Bruno Mars', 'Sep 25'"]


def note_policy(state: dict, questions: dict) -> tuple:
    """Make notes until three of them have been made."""
    made = sum(1 for action in state["previous_actions"] if "New note" in action)
    return ("done", None) if made >= 3 else ("click_item", "New note")


@pytest.mark.xfail(
    strict=True,
    reason="a repeated action on a screen whose visible text does not change reads as a cycle; nothing on the capture says the third note exists",
)
def test_l40_a_legitimate_repeat_that_leaves_the_visible_text_unchanged_is_taken_for_a_stall(monkeypatch, tmp_path):
    """Three notes is one action done three times, and the app says so nowhere the capture can see.

    A known limit of a signature read off the screen alone: the stop rules cannot tell a button that
    does nothing from a button that does something invisible. The scenario is kept as the behaviour
    that is wanted, so any fix -- a counted action, a window title, an accessibility value -- is
    measured against it rather than argued about.
    """

    def made_a_note(world: World) -> None:
        world.notes += 1  # the note exists, and the screen looks exactly as it did
        return None

    world = World(
        [Page(name="notes", items=["Notes", ("New note", "button")], url=None, app="Notes", on={"click:New note": made_a_note})]
    )
    world.notes = 0

    state = drive(world, note_policy, goal="create three new notes", monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["pressed 'New note' via accessibility"] * 3
    assert world.notes == 3


def test_l41_a_single_page_search_changes_results_not_the_url(monkeypatch, tmp_path):
    """One URL for the whole app: only the text on screen says the search ran."""

    def searched(world: World) -> str | None:
        return "results" if world.typed.get("Search") == "bruno mars" else None

    app = "https://example.com/app"
    world = World(
        [
            Page(name="app", items=["Search", "Recent"], url=app, field="Search", on={"enter": searched}),
            Page(
                name="results",
                items=["Search", "Results", "Bruno Mars - Sep 25", "Buy"],
                url=app,
                field="Search",
                on={"click:Buy": "checkout"},
            ),
            Page(name="checkout", items=["Order summary"], url=app),
        ]
    )
    policy = scripted(("type_text", None), ("press_enter", None), ("click_item", "Buy"), ("done", None))

    state = drive(
        world,
        policy,
        goal="search for the bruno mars tour and buy a ticket",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=FakeWriter(text="bruno mars"),
    )

    assert state.outcome == "done"
    assert world.page.name == "checkout"
    assert state.history == [
        "typed 'bruno mars' into 'Search' via accessibility (verified 0.95)",
        "pressed Return",
        "clicked 'Buy'",
    ]
    assert world.log == ["type:bruno mars", "enter", "click:Buy"]
    last = json.loads((tmp_path / "run" / "step-003-answers.json").read_text())
    assert (last["idle_actions"], last["repeated_actions"]) == (0, 0)  # a page that only changed its text still moved


# ----- the hand-off: the classifier stops, the writer reads the screen, and the run goes on -----

SHOP_GOAL = "find the cheapest macbook that arrives in under a week"
FILTER = "Arrives in 2-4 days"


def shop() -> World:
    """A results page where two moves both look right: open the cheapest listing, or filter by delivery first."""
    results = "https://shop.example.com/search?q=macbook"
    return World(
        [
            Page(
                name="results",
                items=["Sort: lowest price", FILTER, "MacBook 2010", "$59.75"],
                url=results,
                on={f"click:{FILTER}": "filtered", "click:MacBook 2010": "slow listing"},
            ),
            Page(name="filtered", items=[f"{FILTER} x", "MacBook Air 2015", "$140.00"], url=results + "&fast=1"),
            Page(name="slow listing", items=["MacBook 2010", "$59.75", "Arrives in 3 weeks"], url="https://shop.example.com/1"),
        ]
    )


def torn_until_focused(state: dict, questions: dict) -> tuple:
    """Split between the listing and the filter until a focus says which, then sure of it."""
    if state["browser_active_tab_url"].endswith("&fast=1"):
        return ("done", None)
    focus = state.get("current_focus")
    if focus is None:
        return ("click_item", "MacBook 2010", 0.38)
    return ("click_item", next(it["text"] for it in state["screen_items_in_reading_order"] if it["text"] in focus))


def test_l42_a_stop_between_two_good_moves_is_settled_by_the_writer_and_the_run_goes_on(monkeypatch, tmp_path):
    world = shop()
    writer = FakeWriter(reviews=[{"focus": f"Click the '{FILTER}' filter"}])

    state = drive(world, torn_until_focused, goal=SHOP_GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path, writer=writer)

    assert state.outcome == "done"
    assert world.page.name == "filtered"
    assert world.log == [f"click:{FILTER}"]
    assert [(h.step, h.outcome, h.focus, h.actions) for h in state.handoffs] == [
        (1, "low confidence", f"Click the '{FILTER}' filter", 0)
    ]
    assert state.answer.achieved and "MacBook Air 2015" in state.answer.text
    # The classifier was asked under the focus, and told what a focus is, only once there was one.
    states, asked = world.fake.states, world.fake.asked
    assert "current_focus" not in states[0] and "current focus" not in asked[0]["kind"].instructions
    assert states[1]["current_focus"] == f"Click the '{FILTER}' filter" and "current focus" in asked[1]["kind"].instructions
    # The second time round, the writer is told where it sent the classifier the first time.
    assert "earlier_stops" not in writer.packets[0]
    assert writer.packets[1]["earlier_stops"] == [
        {
            "after_action": 0,
            "why": "the classifier was not confident enough in any next action",
            "focus_given": state.handoffs[0].focus,
        }
    ]
    reviews = json.loads((tmp_path / "run" / "step-001-review.json").read_text())
    assert [(r["outcome"], r["focus"], r["handed_back"]) for r in reviews] == [("low confidence", state.handoffs[0].focus, True)]


def test_l43_a_focus_the_classifier_cannot_act_on_leaves_the_answer_standing(monkeypatch, tmp_path):
    world = shop()
    writer = FakeWriter(reviews=[{"focus": "Open the listing", "answer": "Not confirmed yet."}])
    unsure = scripted(("click_item", "MacBook 2010", 0.38), ("click_item", "MacBook 2010", 0.38))

    state = drive(world, unsure, goal=SHOP_GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path, writer=writer)

    assert state.outcome == "low confidence"
    assert world.log == []
    assert len(world.fake.states) == 2  # asked again under the focus, and no surer
    assert len(writer.packets) == 1  # the same screen and no new action: nothing to read a second time
    assert state.answer.text == "Not confirmed yet." and not state.answer.achieved


def test_l44_the_writer_asks_the_user_and_the_reply_steers_the_rest_of_the_run(monkeypatch, tmp_path):
    world = World(
        [
            Page(
                name="sizes",
                items=["13 inch", "15 inch"],
                url="https://shop.example.com/sizes",
                on={"click:13 inch": "small", "click:15 inch": "large"},
            ),
            Page(name="small", items=["MacBook Air 13", "$140.00"], url="https://shop.example.com/13"),
            Page(name="large", items=["MacBook Air 15", "$210.00"], url="https://shop.example.com/15"),
        ]
    )

    def policy(state: dict, questions: dict) -> tuple:
        if state["browser_active_tab_url"].endswith("/sizes"):
            said = state.get("user_said")
            return ("click_item", f"{said[0]['replied']} inch") if said else ("none", None)
        return ("done", None)

    def focus_on_the_reply(packet: dict) -> dict:
        return {"focus": f"Click '{packet['user_said'][0]['replied']} inch'"}

    writer = FakeWriter(reviews=[{"question": "13 or 15 inch?"}, focus_on_the_reply])

    state = drive(world, policy, goal=SHOP_GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path, writer=writer, replies=["15"])

    assert state.outcome == "done"
    assert world.asked == ["13 or 15 inch?"]
    assert world.page.name == "large"
    assert world.log == ["activate", "click:15 inch"]  # the reply was typed in the terminal, so the work is brought back first
    assert writer.packets[0]["user_can_be_asked"] and "user_said" not in writer.packets[0]
    assert writer.packets[1]["user_said"] == [{"asked": "13 or 15 inch?", "replied": "15"}]
    assert [h.focus for h in state.handoffs] == ["Click '15 inch'"]
    summary = json.loads((tmp_path / "run" / "run.json").read_text())
    assert summary["questions"] == [{"question": "13 or 15 inch?", "reply": "15"}]
    reviews = json.loads((tmp_path / "run" / "step-001-review.json").read_text())
    assert [(r["question"], r["reply"], r["handed_back"]) for r in reviews] == [("13 or 15 inch?", "15", False), ("", None, True)]


def test_l45_with_nobody_at_the_terminal_a_question_is_never_put(monkeypatch, tmp_path):
    world = shop()
    writer = FakeWriter(reviews=[{"question": "13 or 15 inch?"}])

    state = drive(world, scripted(("none", None)), goal=SHOP_GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path, writer=writer)

    assert state.outcome == "nothing helps"
    assert world.asked == []
    assert writer.packets[0]["user_can_be_asked"] is False
    assert len(writer.packets) == 1 and not state.answer.achieved


def test_l46_a_user_who_declines_to_answer_ends_the_run(monkeypatch, tmp_path):
    world = shop()
    writer = FakeWriter(reviews=[{"question": "13 or 15 inch?"}])

    state = drive(
        world, scripted(("none", None)), goal=SHOP_GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path, writer=writer, replies=[""]
    )

    assert state.outcome == "nothing helps"
    assert world.asked == ["13 or 15 inch?"]
    assert world.log == [] and state.handoffs == []
    assert len(writer.packets) == 1


def test_l47_the_asking_is_bounded(monkeypatch, tmp_path):
    world = shop()
    writer = FakeWriter(reviews=[{"question": f"question {n}?"} for n in range(9)])

    state = drive(
        world,
        scripted(("none", None)),
        goal=SHOP_GOAL,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=writer,
        replies=["a", "b", "c", "d"],
    )

    assert world.asked == ["question 0?", "question 1?", "question 2?"]
    assert [p["user_can_be_asked"] for p in writer.packets] == [True, True, True, False]
    assert state.outcome == "nothing helps" and not state.answer.achieved


def test_l48_a_done_the_writer_does_not_see_on_screen_is_sent_back(monkeypatch, tmp_path):
    """The classifier calls the results page the answer; the writer reads it and finds no delivery time."""
    world = shop()
    writer = FakeWriter(reviews=[{"focus": f"Click the '{FILTER}' filter"}])

    def policy(state: dict, questions: dict) -> tuple:
        return ("click_item", FILTER) if state.get("current_focus") and not state["previous_actions"] else ("done", None)

    state = drive(world, policy, goal=SHOP_GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path, writer=writer)

    assert [h.outcome for h in state.handoffs] == ["done"]
    assert state.outcome == "done" and state.answer.achieved
    assert world.page.name == "filtered"


def test_l49_a_stall_handed_back_starts_the_new_focus_with_clean_counts(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="home", items=["Dead link", "Tickets"], url="https://example.com/", on={"click:Tickets": "tickets"}),
            Page(name="tickets", items=["Buy"], url="https://example.com/tickets"),
        ]
    )
    writer = FakeWriter(reviews=[{"focus": "Click 'Tickets'"}])

    def policy(state: dict, questions: dict) -> tuple:
        if state["browser_active_tab_url"].endswith("/tickets"):
            return ("done", None)
        return ("click_item", "Tickets" if state.get("current_focus") else "Dead link")

    state = drive(world, policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path, writer=writer)

    assert [h.outcome for h in state.handoffs] == ["stalled"]
    assert state.outcome == "done" and world.page.name == "tickets"
    assert world.log == ["click:Dead link"] * 3 + ["click:Tickets"]
    resumed = json.loads((tmp_path / "run" / "step-004-answers.json").read_text())
    assert (resumed["idle_actions"], resumed["repeated_actions"]) == (
        1,
        0,
    )  # the screen had not moved; the count of it began again


@pytest.mark.parametrize(("handoffs", "expected"), [(0, 0), (2, 2)])
def test_l50_the_handing_back_is_bounded(handoffs, expected, monkeypatch, tmp_path):
    """A writer that always has another focus, and a classifier that takes one action under each and stops."""
    world = World([Page(name="feed", items=lambda w: [f"Post {w.ticks}", "More"], url="https://example.com/feed")])
    writer = FakeWriter(reviews=[{"focus": f"Scroll on, round {n}"} for n in range(9)])

    def policy(state: dict, questions: dict) -> tuple:
        acted_under = len(state["previous_actions"])
        rounds = int(state["current_focus"][-1]) + 1 if "current_focus" in state else 0
        return ("scroll_down", None) if acted_under < rounds else ("none", None)

    state = drive(world, policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path, writer=writer, handoffs=handoffs)

    assert len(state.handoffs) == expected
    assert world.log == ["scroll_down"] * expected
    assert state.outcome == "nothing helps" and not state.answer.achieved
    assert len(writer.packets) == expected + 1  # the stop past the budget is still answered, and that answer is final


def test_l51_a_stop_on_the_last_step_is_answered_once(monkeypatch, tmp_path):
    world = shop()
    writer = FakeWriter(reviews=[{"focus": f"Click the '{FILTER}' filter"}])

    state = drive(world, torn_until_focused, goal=SHOP_GOAL, steps=1, monkeypatch=monkeypatch, tmp_path=tmp_path, writer=writer)

    assert state.outcome == "low confidence"  # not "step limit": the step was spent on a stop, not on an action
    assert state.handoffs == [] and len(writer.packets) == 1


def test_l52_the_run_counts_the_requests_each_model_took(monkeypatch, tmp_path):
    """A search, typed by the writer and checked by the classifier, then one hand-off on the way to the answer."""
    app = "https://example.com/app"
    world = World(
        [
            Page(
                name="app",
                items=["Search", "Recent"],
                url=app,
                field="Search",
                on={"enter": lambda w: "results" if w.typed.get("Search") == "bruno mars" else None},
            ),
            Page(
                name="results",
                items=["Search", "Bruno Mars - Sep 25", "Buy"],
                url=app,
                field="Search",
                on={"click:Buy": "checkout"},
            ),
            Page(name="checkout", items=["Order summary"], url=app),
        ]
    )
    policy = scripted(("type_text", None), ("press_enter", None), ("none", None), ("click_item", "Buy"), ("done", None))
    writer = FakeWriter(text="bruno mars", reviews=[{"focus": "Click 'Buy'"}])

    state = drive(
        world,
        policy,
        goal="search for the bruno mars tour and buy a ticket",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=writer,
    )

    assert world.page.name == "checkout"
    # Five decisions and the check of what was typed; the typed text and two readings of a stopped screen.
    assert state.calls.count == {"classifier": 6, "writer": 3}
    summary = json.loads((tmp_path / "run" / "run.json").read_text())
    assert summary["calls"]["classifier"]["calls"] == 6 and summary["calls"]["classifier"]["share"] == 0.667
    assert summary["calls"]["writer"]["calls"] == 3 and summary["calls"]["writer"]["share"] == 0.333
    assert "calls: classifier 6 (67%, " in (tmp_path / "run" / "run.log").read_text()


def test_l53_unverified_keystrokes_stay_when_the_field_will_not_take_a_value(monkeypatch, tmp_path):
    world = World(
        [Page(name="search", items=["Search", "Popular tours"], url="https://example.com/", field="Search", no_ax_value=True)]
    )
    world.typed["Search"] = "old query"
    policy = scripted(("type_text", None), ("done", None))

    state = drive(
        world,
        policy,
        goal="search for the bruno mars tour",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=FakeWriter(text="bruno mars tour"),
        noul=0.2,
    )

    assert state.outcome == "done"
    assert "via keystrokes" in state.history[0] and "could not safely restore previous value" in state.history[0]
    assert world.typed["Search"] == "bruno mars tour"  # left for the next step to see, not erased blind
    assert world.log == ["clear_field", "type:bruno mars tour"]  # emptied before typing, never after


# ------------------------------------------------------------------ beyond the one window
# The actions that reach past what the capture shows: another app, the menu bar, another window,
# the keyboard, and the gestures a plain click is not. Each is offered only when it has something
# to act on this step, so the scenarios also read what the classifier was offered.


def test_l54_an_app_that_is_not_open_is_opened_by_name(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="desktop", app="Finder", items=["Desktop", "Documents"], on={"activate:Notes": "notes"}),
            Page(name="notes", app="Notes", items=["New Note", "All iCloud"]),
        ]
    )
    world.running = {"Finder", "Google Chrome"}
    policy = scripted(("open_app", "Notes"), ("done", None))

    state = drive(
        world,
        policy,
        goal="open my notes",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        apps=("Finder", "Google Chrome", "Notes"),
    )

    assert state.outcome == "done"
    assert state.history == ["opened Notes"]
    assert world.page.name == "notes"
    assert world.log == ["activate:Notes"]
    offered = world.fake.asked[0]["app"].criteria
    # The browser is use_browser's, and the app already in front is nothing to switch to.
    assert list(offered) == ["Notes"] and "Not running" in offered["Notes"]
    assert "open_app" in world.fake.asked[0]["kind"].criteria


def test_l55_with_no_other_app_to_open_the_action_is_not_offered(monkeypatch, tmp_path):
    world = World([Page(name="desktop", app="Finder", items=["Desktop"])])

    drive(world, scripted(("done", None)), monkeypatch=monkeypatch, tmp_path=tmp_path, apps=("Finder", "Google Chrome"))

    asked = world.fake.asked[0]
    assert "open_app" not in asked["kind"].criteria and "app" not in asked


def test_l56_a_menu_command_runs_without_the_menu_opening(monkeypatch, tmp_path):
    world = World(
        [
            Page(
                name="note",
                app="Notes",
                items=["Groceries", "eggs milk bread"],
                menu=("Format ▸ Checklist", ("Edit ▸ Undo", ("z", True, False)), "Edit ▸ AutoFill ▸ Passwords…"),
                on={"menu:Format ▸ Checklist": "checklist"},
            ),
            Page(name="checklist", app="Notes", items=["Groceries", "☐ eggs", "☐ milk", "☐ bread"]),
        ]
    )
    policy = scripted(("press_menu", "Format ▸ Checklist"), ("done", None))

    state = drive(world, policy, goal="make the grocery note a checklist", monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["ran the menu command 'Format ▸ Checklist'"]
    assert world.log == ["menu:Format ▸ Checklist"]
    assert world.mouse == []  # pressed through accessibility, not clicked
    # Undo is the undo key's, and AutoFill fills in passwords: neither is offered from the menu.
    assert list(world.fake.asked[0]["menu"].criteria.values()) == ["Format ▸ Checklist"]


def test_l57_another_window_of_the_app_is_brought_forward(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="draft", app="Mail", items=["Draft", "To:"], windows=("Draft", "Inbox"), on={"window:Inbox": "inbox"}),
            Page(name="inbox", app="Mail", items=["Inbox", "Receipt from the venue"], windows=("Inbox", "Draft")),
        ]
    )
    policy = scripted(("focus_window", "Inbox"), ("done", None))

    state = drive(world, policy, goal="find the venue's receipt", monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["brought the window 'Inbox' to the front"]
    assert world.log == ["window:Inbox"]
    assert list(world.fake.asked[0]["window"].criteria.values()) == ["'Inbox'"]  # the front one is not on offer
    assert world.fake.states[0]["other_windows_of_this_app"] == ["Inbox"]


def test_l58_a_single_window_offers_no_window_to_switch_to(monkeypatch, tmp_path):
    world = World([Page(name="draft", app="Mail", items=["Draft"], windows=("Draft",))])

    drive(world, scripted(("done", None)), monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert "focus_window" not in world.fake.asked[0]["kind"].criteria and "window" not in world.fake.asked[0]


def test_l59_a_named_shortcut_saves_the_document(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="edited", app="TextEdit", items=["Untitled — Edited", "hello"], on={"cmd+s": "saved"}),
            Page(name="saved", app="TextEdit", items=["Untitled", "hello"]),
        ]
    )
    policy = scripted(("press_key", "save"), ("done", None))

    state = drive(world, policy, goal="save the document", monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["pressed save"]
    assert world.log == ["cmd+s"]
    keys = world.fake.asked[0]["key"].criteria
    assert "back" not in keys and "page_down" not in keys  # go_back and scroll_down already are those
    assert "backspace" not in keys  # no field to delete in


def test_l60_a_file_is_opened_with_a_double_click_and_its_menu_with_a_right_click(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="folder", app="Finder", items=["Report.pdf", "Notes.txt"], on={"right-click:Report.pdf": "context"}),
            Page(name="context", app="Finder", items=["Open With", "Move to Trash"], on={"click:Open With": "folder2"}),
            Page(name="folder2", app="Finder", items=["Report.pdf", "Notes.txt"], on={"double-click:Report.pdf": "preview"}),
            Page(name="preview", app="Preview", items=["Report.pdf — Page 1 of 3"]),
        ]
    )
    policy = scripted(
        ("right_click_item", "Report.pdf"), ("click_item", "Open With"), ("double_click_item", "Report.pdf"), ("done", None)
    )

    state = drive(world, policy, goal="open the report", monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["right-clicked 'Report.pdf'", "clicked 'Open With'", "double-clicked 'Report.pdf'"]
    assert world.log == ["right-click:Report.pdf", "click:Open With", "double-click:Report.pdf"]
    assert world.mouse == [FIRST_ROW, FIRST_ROW, FIRST_ROW]  # both gestures go to the pixel, never to an element


def test_l61_nothing_is_offered_that_would_put_text_into_a_password_field(monkeypatch, tmp_path):
    world = World(
        [
            Page(
                name="login",
                items=["Sign in", "Password"],
                field="Password",
                secure=True,
                url="https://example.com/login",
                menu=("Edit ▸ Paste and Match Style", "View ▸ Actual Size"),
            )
        ]
    )
    world.typed["Password"] = "hunter2"

    state = drive(world, scripted(("none", None)), goal="sign in", monkeypatch=monkeypatch, tmp_path=tmp_path)

    offered = world.fake.asked[0]
    assert "type_text" not in offered["kind"].criteria
    assert "paste" not in offered["key"].criteria and "backspace" not in offered["key"].criteria
    assert list(offered["menu"].criteria.values()) == ["View ▸ Actual Size"]  # no paste from the menu either
    assert world.fake.states[0]["focused_field"]["current_value"] == ""  # and its value never reaches the classifier
    assert state.history == [] and world.log == []
