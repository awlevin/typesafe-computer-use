"""The main window: type or dictate a goal, watch every step, read the answer.

The run itself is `runner.run`, exactly as the CLI drives it. This module only puts a face on it:
the loop runs on a background thread and reports through `RunHooks`, and every callback hops back
to the main thread before touching a control, because AppKit tolerates nothing else.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import AppKit
import objc
from Foundation import NSMakeRect, NSObject, NSOperationQueue

from .. import session, voice
from ..chat_classifier import ClassifierError
from ..goal import LiveGoal
from ..models import Abort
from ..platform_adapter import desktop
from ..runner import RunConfig, RunHooks, StepEvent
from ..settings import SettingsError
from ..settings import load as load_settings
from ..writer import Answer
from . import sounds
from . import widgets as w
from .hotkey import Hotkey
from .keys_panel import KeysWindow
from .listening import Listener
from .overlay import Dot
from .settings_panel import SettingsWindow

WIDTH, HEIGHT = 1060, 720
SHOT_WIDTH = 400  # the screenshot column, fixed while the log column takes the slack
TOP = 116  # everything above the log and the screenshot
BOTTOM = 104  # the answer box
HIDE_DELAY = 0.6  # long enough for the window to be off screen before the first capture


def _app_visibility(change: str) -> None:
    """Hide this app, bring it back, or bring it to the front: each takes the screen from, or gives it
    back to, whatever the user is doing, so the test guard refuses it."""
    app = AppKit.NSApp()
    {"hide": lambda: app.hide_(None), "unhide": lambda: app.unhide_(None), "front": lambda: app.activateIgnoringOtherApps_(True)}[
        change
    ]()


def on_main(fn) -> None:
    """Run `fn` on the main thread. Every UI update from the run thread goes through here."""
    NSOperationQueue.mainQueue().addOperationWithBlock_(fn)


class Controller:
    """The window and everything it owns: the run thread, the recorder, and the settings sheet.

    Plain Python, not an NSObject: every control routes through `widgets.Action`, so nothing here
    has to be shaped like a selector.
    """

    def __init__(self, goal: str = ""):
        self.settings = load_settings()
        self.stop_flag = threading.Event()
        self.thread: threading.Thread | None = None
        self.settings_window: SettingsWindow | None = None
        self.keys_window: KeysWindow | None = None
        self.live: LiveGoal | None = None  # set while a goal is being spoken into a running loop
        self.starting = False  # a run that has been asked for but whose thread is not up yet
        self.dot = Dot()
        self.listener = Listener(
            self.settings.voice,
            on_text=lambda text: on_main(lambda: self._dictated(text)),
            on_utterance=lambda said: on_main(lambda: self._heard(said)),
            on_pending=lambda text: on_main(lambda: self._hearing(text)),
            on_trouble=lambda message: on_main(lambda: self.say(message)),
        )
        self.hotkey = Hotkey(on_toggle=lambda: on_main(self.toggle_dictation))
        self._build(goal)
        self._watch_for_the_key()

    # --- building ------------------------------------------------------------------------------

    def _build(self, goal: str) -> None:
        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT),
            AppKit.NSWindowStyleMaskTitled
            | AppKit.NSWindowStyleMaskClosable
            | AppKit.NSWindowStyleMaskMiniaturizable
            | AppKit.NSWindowStyleMaskResizable,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self.window.setTitle_("typesafe-computer-use")
        self.window.setMinSize_(AppKit.NSMakeSize(860, 560))
        # Kept alive when closed, so the Dock icon can bring the same window back rather than
        # leaving an app running with no way into it.
        self.window.setReleasedWhenClosed_(False)
        self.window_delegate = WindowDelegate.alloc().initWithController_(self)
        self.window.setDelegate_(self.window_delegate)
        root = self.window.contentView()
        run = self.settings.run

        w.label(root, "Goal", 20, 22, 36, 17, font=w.BOLD)
        self.goal = w.field(root, goal, 60, 18, WIDTH - 60 - 380, 26, placeholder="what should this computer do?")
        self.goal.setFont_(AppKit.NSFont.systemFontOfSize_(14))
        self.goal.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewMinYMargin)
        self.listen_button = w.button(root, "Listen & go", self.toggle_listening, WIDTH - 314, 17, 110, 28)
        self.listen_button.setToolTip_("Keep listening, and act on each sentence as you finish it  (\u2318L)")
        self.dictate = w.button(root, "Dictate", self.toggle_dictation, WIDTH - 194, 17, 90, 28)
        self.dictate.setToolTip_(f"Dictate a goal  (\u2318D, or {self.settings.voice.shortcut.describe()} anywhere)")
        self.run_button = w.button(root, "Run", self.start_run, WIDTH - 100, 17, 80, 28, key="\r")
        self.run_button.setToolTip_("Start the run  (Return, or \u2318R)")
        for control in (self.listen_button, self.dictate, self.run_button):
            control.setAutoresizingMask_(AppKit.NSViewMinXMargin | AppKit.NSViewMinYMargin)

        self.act = w.checkbox(root, "act (click and type for real)", run.act, 22, 58, 210)
        w.label(root, "steps", 240, 60, 36, 15, font=w.SMALL)
        self.steps = w.field(root, str(run.steps), 278, 56, 52, 22)
        w.label(root, "min confidence", 340, 60, 90, 15, font=w.SMALL)
        self.min_confidence = w.field(root, f"{run.min_confidence:g}", 432, 56, 46, 22)
        w.label(root, "delay", 488, 60, 34, 15, font=w.SMALL)
        self.delay = w.field(root, f"{run.delay:g}", 524, 56, 46, 22)
        self.hide = w.checkbox(root, "hide this window while it runs", run.hide_window, 584, 58, 220)
        self.settings_button = w.button(root, "Settings", self.open_settings, WIDTH - 194, 55, 90, 26)
        self.stop_button = w.button(root, "Stop", self.stop_run, WIDTH - 100, 55, 80, 26, key="\x1b")
        self.stop_button.setToolTip_("Stop the run  (Escape, or \u2318.)")
        self.stop_button.setEnabled_(False)
        for control in (self.settings_button, self.stop_button):
            control.setAutoresizingMask_(AppKit.NSViewMinXMargin | AppKit.NSViewMinYMargin)

        self.status = w.label(root, "", 22, 90, WIDTH - 44, 16, font=w.SMALL, color=AppKit.NSColor.secondaryLabelColor())
        self.status.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewMinYMargin)

        middle = HEIGHT - TOP - BOTTOM - 8
        log_width = WIDTH - SHOT_WIDTH - 60
        self.log_scroll, self.log = w.text_view(root, 20, TOP, log_width, middle)
        self.log_scroll.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        self.shot = w.image_view(root, WIDTH - SHOT_WIDTH - 20, TOP, SHOT_WIDTH, middle - 22)
        self.shot.setAutoresizingMask_(AppKit.NSViewMinXMargin | AppKit.NSViewHeightSizable)
        self.caption = w.label(
            root,
            "",
            WIDTH - SHOT_WIDTH - 20,
            TOP + middle - 18,
            SHOT_WIDTH,
            15,
            font=w.SMALL,
            color=AppKit.NSColor.secondaryLabelColor(),
        )
        self.caption.setAutoresizingMask_(AppKit.NSViewMinXMargin | AppKit.NSViewMinYMargin)

        answer_title = w.label(root, "Answer", 22, HEIGHT - BOTTOM + 2, 80, 15, font=w.BOLD)
        answer_title.setAutoresizingMask_(AppKit.NSViewMaxYMargin)
        self.answer_scroll, self.answer = w.text_view(root, 20, HEIGHT - BOTTOM + 20, WIDTH - 40, BOTTOM - 30, mono=False)
        self.answer_scroll.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewMaxYMargin)

        self.describe_services()

    def show(self) -> None:
        """Bring the window up, whether this is the first time or after it was closed or hidden."""
        if not self.window.isVisible():
            self.window.center()
        _app_visibility("unhide")
        w.present(self.window)
        self.window.makeFirstResponder_(self.goal)

    @property
    def running(self) -> bool:
        """True from the moment a run is asked for, not from the moment its thread exists.

        Hiding the window delays the start by a fraction of a second, and a sentence landing in
        that gap would otherwise start a second run driving the same machine.
        """
        return self.starting or (self.thread is not None and self.thread.is_alive())

    def should_close(self) -> bool:
        """Closing the window quits the app, so a run in progress gets a say first."""
        if not self.running:
            return True
        if w.confirm(
            "A run is in progress",
            "Closing this window quits the app and stops the run. It can keep running instead, "
            "with the window out of the way; the Dock icon brings it back.",
            "Stop and close",
            "Keep running",
        ):
            self.stop_flag.set()
            return True
        _app_visibility("hide")
        return False

    # --- status --------------------------------------------------------------------------------

    def describe_services(self) -> None:
        s = self.settings
        writer = f"{s.writer_spec().label} / {s.writer_model()}"
        answer = f"{s.answer_spec().label} / {s.answer_model()}"
        same = " (same as writer)" if answer == writer else ""
        self.status.setStringValue_(f"classifier: {s.decisions_spec().label}    writer: {writer}    answer: {answer}{same}")

    def say(self, message: str) -> None:
        self.status.setStringValue_(message)

    # --- settings ------------------------------------------------------------------------------

    def open_settings(self, sender=None) -> None:
        def saved(settings) -> None:
            self.settings = settings
            self.listener.voice = settings.voice
            self.dictate.setToolTip_(f"Dictate a goal  (\u2318D, or {settings.voice.shortcut.describe()} anywhere)")
            self._watch_for_the_key()
            self.describe_services()

        self.settings_window = SettingsWindow(self.settings, saved)
        self.settings_window.show()

    def open_keys(self, sender=None) -> None:
        """The keys window on its own, so a key can be entered without opening Settings first."""
        self.keys_window = KeysWindow(self.settings, lambda settings: self.describe_services())
        self.keys_window.show()

    # --- dictation -----------------------------------------------------------------------------

    def _watch_for_the_key(self) -> None:
        """Start watching for the push-to-talk chord, and say so if macOS will not deliver it."""
        shortcut = self.settings.voice.shortcut
        if not shortcut.enabled:
            self.hotkey.stop()
            return
        if not self.hotkey.start(shortcut) and not desktop.accessibility_trusted():
            self.say(
                f"press {shortcut.describe()} anywhere to dictate \u2014 once this app has Accessibility "
                "permission in System Settings > Privacy & Security"
            )

    def toggle_dictation(self, sender=None) -> None:
        """Start dictating, or stop. The button, the menu item and the global key all land here."""
        if self.listener.listening:
            return  # listen-and-go already has the microphone; its own button stops it
        if self.listener.recorder.recording:
            self._stop_dictation()
        elif self.dictate.isEnabled():  # the key and the menu item have no idea it is greyed out
            self._start_dictation()

    def _start_dictation(self) -> None:
        """Start recording, with a sound and the dot, since the window may not be in front."""
        if not voice.available():
            w.alert("Dictation is not installed", voice.INSTALL_HINT)
            return
        if not self.listener.hold():
            return
        sounds.started()
        self.dot.show()
        self.dictate.setTitle_("Stop")
        self.say(f"listening \u2014 press {self.settings.voice.shortcut.describe()} again, or the button, when you are done")

    def _stop_dictation(self) -> None:
        """Stop, and transcribe what was said. The text arrives in `_heard`, off the main thread."""
        sounds.stopped()
        self.dot.hide()
        self.dictate.setTitle_("Dictate")
        self.say(f"transcribing with {self.settings.voice.model}\u2026")
        self.listener.release()

    def toggle_listening(self, sender=None) -> None:
        """Listen and go: keep listening, and act on each sentence as it is finished."""
        if self.listener.listening:
            self._stop_listening()
            return
        if not voice.available():
            w.alert("Dictation is not installed", voice.INSTALL_HINT)
            return
        if self.running:
            self.say("a run is already going; stop it first")
            return
        self.live = LiveGoal(self.goal.stringValue(), listening=True)
        try:
            self.listener.start()
        except Exception as e:
            self.live = None
            w.alert("The microphone could not be used", str(e))
            return
        sounds.started()
        self.listen_button.setTitle_("Stop listening")
        self.dot.show()
        self.say("listening. say what you want done \u2014 it starts on your first sentence and keeps up as you talk.")
        if self.live.text:
            self.start_run()

    def _stop_listening(self) -> None:
        """Let the last sentence land, then tell the loop the goal is complete."""
        self.listen_button.setEnabled_(False)
        self.say("finishing what you said\u2026")

        def work() -> None:
            self.listener.finish()
            on_main(self._listening_stopped)

        threading.Thread(target=work, daemon=True).start()

    def _listening_stopped(self) -> None:
        if self.live is not None:
            self.live.finish()  # the loop may now conclude that the goal is done
        sounds.stopped()
        self.dot.hide()
        self.listen_button.setTitle_("Listen & go")
        self.listen_button.setEnabled_(True)
        self.say("that is the whole goal; finishing the run." if self.running else "stopped listening.")

    def _dictated(self, text: str) -> None:
        """Everything said so far, rewritten as the model corrects itself.

        The goal is replaced rather than added to, so a sentence the model reworded reaches the
        loop as the correction rather than twice. The loop reads the goal afresh every step.
        """
        if self.live is None:
            return
        self.live.set(text)
        self.goal.setStringValue_(text)
        if not self.running:
            self.start_run()

    def _heard(self, said: str) -> None:
        """One complete push-to-talk dictation."""
        if self.live is not None:
            self.goal.setStringValue_(self.live.add(said))
            return
        existing = self.goal.stringValue().strip()
        self.goal.setStringValue_(f"{existing} {said}".strip() if self.settings.voice.append and existing else said)
        self.dictate.setEnabled_(True)
        self.describe_services()
        self.window.makeFirstResponder_(self.goal)

    def _hearing(self, text: str) -> None:
        """The sentence still being spoken: shown, never acted on."""
        if self.listener.listening:
            self.say(f"listening\u2026 {text}" if text else "listening\u2026")

    # --- running -------------------------------------------------------------------------------

    def start_run(self, sender=None) -> None:
        if self.running:
            return
        goal = self.goal.stringValue().strip()
        if not goal and self.live is None:
            self.say("type or dictate a goal first")
            return
        act = self.act.state() == AppKit.NSControlStateValueOn
        if act and not desktop.accessibility_trusted():
            w.alert(
                "Accessibility permission is missing",
                "Grant it to this app in System Settings > Privacy & Security > Accessibility, then try again. "
                "Without it, clicks and keystrokes are silently dropped.",
            )
            return
        try:
            services = session.build(self.settings)
        except SettingsError as e:
            w.alert("The classifier is not configured", str(e))
            return

        cfg = RunConfig(
            goal=goal,
            out=Path("runs") / time.strftime("%Y%m%d-%H%M%S"),
            act=act,
            steps=self._number(self.steps, self.settings.run.steps, int),
            min_confidence=self._number(self.min_confidence, self.settings.run.min_confidence, float),
            delay=self._number(self.delay, self.settings.run.delay, float),
            clipboard=self.settings.share_clipboard,
        )
        self._remember(cfg, act)

        self.log.setString_("")
        self.answer.setString_("")
        self.shot.setImage_(None)
        self.caption.setStringValue_("")
        self.stop_flag.clear()
        self.run_button.setEnabled_(False)
        self.stop_button.setEnabled_(True)
        self.dictate.setEnabled_(False)
        for note in services.notes:
            self._append(note)

        self.starting = True
        hooks = RunHooks(
            log=lambda line: on_main(lambda: self._append(line)),
            on_step=lambda event: on_main(lambda: self._step(event)),
            on_answer=lambda answer: on_main(lambda: self._answer(answer)),
            should_stop=self.stop_flag.is_set,
            quiet=True,
        )
        factory = session.context_factory(self.settings, goal, services)
        hidden = act and self.hide.state() == AppKit.NSControlStateValueOn

        def begin() -> None:
            args = (cfg, factory, services.classifier, hooks, hidden, self.live)
            self.thread = threading.Thread(target=self._drive, args=args, daemon=True)
            self.thread.start()
            self.starting = False

        if not hidden:
            self.say("running. this window is on screen, so its text is part of what the loop reads.")
            begin()
            return
        # Hiding is asynchronous and needs the main thread to keep running, so the first capture is
        # scheduled rather than slept on: sleeping here would photograph the window on its way out.
        self.say("running hidden. click the Dock icon to bring this back, or slam the mouse into the top-left corner to stop.")
        _app_visibility("hide")
        AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(HIDE_DELAY, False, lambda timer: begin())

    def _drive(self, cfg, factory, classifier, hooks, hidden: bool, live) -> None:
        from ..runner import run

        try:
            state = run(cfg, factory, classifier, hooks, live)
            outcome = state.outcome
        except (Abort, SettingsError, ClassifierError) as e:
            outcome = str(e)
        except Exception as e:  # a crash belongs in the window, not only in the terminal
            outcome = f"crashed: {e}"
            hooks.log(outcome)
        on_main(lambda: self._finished(outcome, hidden))

    def _finished(self, outcome: str, hidden: bool) -> None:
        self.starting = False
        if self.listener.listening:
            self._stop_listening()  # the loop is over, so there is nothing left to listen for
        self.live = None
        self.run_button.setEnabled_(True)
        self.stop_button.setToolTip_("Stop the run  (Escape, or \u2318.)")
        self.stop_button.setEnabled_(False)
        self.dictate.setEnabled_(True)
        self.say(f"stopped: {outcome}")
        if hidden:
            _app_visibility("unhide")
            _app_visibility("front")

    def stop_run(self, sender=None) -> None:
        if self.running:
            self.stop_flag.set()
            self.say("stopping after this step...")

    def _remember(self, cfg: RunConfig, act: bool) -> None:
        """Keep the run controls as they were left, so the next launch opens where this one ended."""
        run = self.settings.run
        run.act, run.steps, run.min_confidence, run.delay = act, cfg.steps, cfg.min_confidence, cfg.delay
        run.hide_window = self.hide.state() == AppKit.NSControlStateValueOn
        self.settings.save()

    def _number(self, control, fallback, kind):
        try:
            return kind(control.stringValue().strip())
        except ValueError:
            control.setStringValue_(f"{fallback:g}" if kind is float else str(fallback))
            return fallback

    # --- run callbacks, all on the main thread --------------------------------------------------

    def _append(self, line: str) -> None:
        w.append(self.log, line)

    def _step(self, event: StepEvent) -> None:
        image = AppKit.NSImage.alloc().initWithContentsOfFile_(str(event.annotated))
        if image is not None:
            self.shot.setImage_(image)
        target = f" → {event.item_text!r}" if event.item_text else ""
        self.caption.setStringValue_(f"step {event.step}: {event.kind} ({event.confidence:.2f}){target}  {event.seconds:.1f}s")

    def _answer(self, answer: Answer) -> None:
        verdict = "goal achieved" if answer.achieved else "goal not achieved"
        self.answer.setString_(f"{verdict}\n\n{answer.text}")


class WindowDelegate(NSObject):
    """Asks the controller before the window closes, since closing it ends the app."""

    def initWithController_(self, controller):
        self = objc.super(WindowDelegate, self).init()
        self._controller = controller
        return self

    def windowShouldClose_(self, window) -> bool:
        return self._controller.should_close()


class Delegate(NSObject):
    """Quits when the window is closed, and brings it back when the Dock icon is clicked."""

    def initWithController_(self, controller):
        self = objc.super(Delegate, self).init()
        self._controller = controller
        return self

    def applicationShouldTerminateAfterLastWindowClosed_(self, app) -> bool:
        return True

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, visible) -> bool:
        """The Dock icon reopens the window: hidden during a run, or closed while one was kept alive."""
        if not visible:
            self._controller.show()
        return True

    def applicationWillTerminate_(self, notification) -> None:
        self._controller.stop_flag.set()
        self._controller.hotkey.stop()
        self._controller.dot.hide()


def launch(goal: str = "") -> None:
    """Open the window and run the event loop until it is closed."""
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    controller = Controller(goal)
    delegate = Delegate.alloc().initWithController_(controller)
    app.setDelegate_(delegate)
    _install_menu(app, controller)
    controller.show()
    _app_visibility("front")
    app.run()


def _install_menu(app, controller: Controller) -> None:
    """A real menu bar: the shortcuts live here, which is also where a Mac user looks for them."""
    main = AppKit.NSMenu.alloc().init()

    app_menu = w.submenu(main, "typesafe-computer-use")
    w.menu_item(app_menu, "Settings\u2026", controller.open_settings, ",")
    w.menu_item(app_menu, "API keys\u2026", controller.open_keys, ",", option=True)
    app_menu.addItem_(AppKit.NSMenuItem.separatorItem())
    w.menu_item(app_menu, "Hide", "hide:", "h")
    w.menu_item(app_menu, "Quit", "terminate:", "q")

    edit = w.submenu(main, "Edit")
    for title, selector, key in (
        ("Cut", "cut:", "x"),
        ("Copy", "copy:", "c"),
        ("Paste", "paste:", "v"),
        ("Select All", "selectAll:", "a"),
    ):
        w.menu_item(edit, title, selector, key)

    run = w.submenu(main, "Run")
    w.menu_item(run, "Run", controller.start_run, "r")
    w.menu_item(run, "Stop", controller.stop_run, ".")
    run.addItem_(AppKit.NSMenuItem.separatorItem())
    w.menu_item(run, "Dictate", controller.toggle_dictation, "d")
    w.menu_item(run, "Listen & go", controller.toggle_listening, "l")

    app.setMainMenu_(main)
