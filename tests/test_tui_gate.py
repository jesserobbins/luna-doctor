"""The front door: bare `luna-doctor` in a terminal opens the triage screen.

`main()` hands a bare invocation on a terminal, with the optional Textual extra
installed, to `tui.run_triage` and returns 0. Everything else keeps today's
`_default_run`: a pipe (an agent, a script), a subcommand, a machine without
Textual. That path keeps the documented 0/1/2 exit codes, because the screen
has no verdict to return.

Nothing here opens a terminal or the network. `isatty`, `find_spec` and the
screen's entry point are monkeypatched, and the one test that reads the
`--online` refresh patches `sources.fetch_capacity` and `TriageApp.run`.
"""

import builtins
import importlib.util
import sys
import types

import pytest

from luna_doctor import cli


# ---- the two streams, and the optional extra --------------------------------
class _Stream:
    """A stream that answers isatty() and swallows what the run prints."""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass


def _streams(monkeypatch, *, tty: bool) -> None:
    """Make stdin and stdout a terminal, or a pipe, for the run under test."""
    stream = _Stream(tty)
    monkeypatch.setattr(sys, "stdin", stream)
    monkeypatch.setattr(sys, "stdout", stream)


def _textual(monkeypatch, *, installed: bool) -> None:
    """Answer the extra's presence without installing or importing anything.

    Every other lookup goes to the real finder: the process keeps importing
    while the test runs, and only the one name under test is answered here.
    """
    real = importlib.util.find_spec

    def find_spec(name, *args, **kwargs):
        if name == "textual":
            return object() if installed else None
        return real(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)


def _stand_in_tui(monkeypatch):
    """A stand-in for the extra, so the gate is testable without Textual.

    `luna_doctor.tui` is patched on the package as well as in `sys.modules`:
    a test that imported the real module earlier in the session leaves it as a
    package attribute, and `from luna_doctor import tui` reads that attribute
    before it consults `sys.modules`.
    """
    calls = []

    def run_triage(plan=None, online=False):
        calls.append({"plan": plan, "online": online})
        return 0

    module = types.ModuleType("luna_doctor.tui")
    module.run_triage = run_triage
    monkeypatch.setitem(sys.modules, "luna_doctor.tui", module)
    monkeypatch.setattr(sys.modules["luna_doctor"], "tui", module, raising=False)
    return calls


def _record_default_run(monkeypatch):
    """Swap out the linear run: only whether it was taken is under test."""
    seen = []

    def default_run(ap, args):
        seen.append(args)
        return 0

    monkeypatch.setattr(cli, "_default_run", default_run)
    return seen


# ---- the gate ---------------------------------------------------------------
def test_a_bare_run_on_a_terminal_opens_the_triage_screen(monkeypatch):
    calls = _stand_in_tui(monkeypatch)
    _streams(monkeypatch, tty=True)
    _textual(monkeypatch, installed=True)
    default_run = _record_default_run(monkeypatch)

    assert cli.main([]) == 0

    assert len(calls) == 1, "the front door did not open the screen"
    assert calls[0]["plan"] is None
    assert not default_run, "the linear run also ran: one front door, not two"


def test_a_pipe_keeps_the_default_run(monkeypatch):
    calls = _stand_in_tui(monkeypatch)
    _streams(monkeypatch, tty=False)
    _textual(monkeypatch, installed=True)
    default_run = _record_default_run(monkeypatch)

    assert cli.main([]) == 0

    assert len(default_run) == 1, "a piped run must keep the linear path"
    assert not calls, "a piped run opened the screen"


def test_without_textual_the_default_run_still_runs(monkeypatch):
    calls = _stand_in_tui(monkeypatch)
    _streams(monkeypatch, tty=True)
    _textual(monkeypatch, installed=False)
    default_run = _record_default_run(monkeypatch)

    assert cli.main([]) == 0

    assert len(default_run) == 1, "a machine without the extra fell off both paths"
    assert not calls, "the screen opened without Textual installed"


def test_a_textual_that_will_not_import_falls_back_to_the_default_run(monkeypatch):
    """`find_spec` sees the extra and the import fails: no traceback, no screen.

    A Textual installed for another interpreter, or one whose API this module no
    longer matches, is present to `find_spec` and unloadable to `import`. The
    front door treats that machine the same as one without the extra.
    """
    calls = _stand_in_tui(monkeypatch)
    _streams(monkeypatch, tty=True)
    _textual(monkeypatch, installed=True)
    default_run = _record_default_run(monkeypatch)
    real_import = builtins.__import__

    def cannot_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "luna_doctor" and "tui" in (fromlist or ()):
            raise ImportError("cannot import name 'tui' from 'luna_doctor'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", cannot_import)

    assert cli.main([]) == 0

    assert len(default_run) == 1, "an unloadable Textual raised at the front door"
    assert not calls, "the screen opened through a failed import"


def test_a_subcommand_keeps_the_default_run_out_of_it(monkeypatch):
    calls = _stand_in_tui(monkeypatch)
    _streams(monkeypatch, tty=True)
    _textual(monkeypatch, installed=True)
    _record_default_run(monkeypatch)
    ran = []
    monkeypatch.setattr(cli, "_run", lambda args: ran.append(args) or 0)

    assert cli.main(["discover"]) == 0

    assert [a.cmd for a in ran] == ["discover"]
    assert not calls, "a subcommand opened the screen"


def test_version_and_help_never_reach_the_screen(monkeypatch):
    calls = _stand_in_tui(monkeypatch)
    _streams(monkeypatch, tty=True)
    _textual(monkeypatch, installed=True)

    for argv in (["--version"], ["--help"]):
        with pytest.raises(SystemExit) as exited:
            cli.main(argv)
        assert exited.value.code == 0

    assert not calls, "argparse's own answers reached the screen"


def test_online_fetches_capacity_once_before_the_screen(monkeypatch):
    """The front door's `--online` is one GET, then the screen is cache-only."""
    tui = pytest.importorskip("luna_doctor.tui")
    from luna_doctor import sources

    events = []
    monkeypatch.setattr(sources, "fetch_capacity",
                        lambda allow_network=False: events.append(("fetch", allow_network)))
    monkeypatch.setattr(tui.TriageApp, "run", lambda self: events.append(("run", None)))
    _streams(monkeypatch, tty=True)
    _textual(monkeypatch, installed=True)

    assert cli.main(["--online"]) == 0

    assert events == [("fetch", True), ("run", None)]


def test_offline_touches_no_network_before_the_screen(monkeypatch):
    tui = pytest.importorskip("luna_doctor.tui")
    from luna_doctor import sources

    events = []
    monkeypatch.setattr(sources, "fetch_capacity",
                        lambda allow_network=False: events.append(("fetch", allow_network)))
    monkeypatch.setattr(tui.TriageApp, "run", lambda self: events.append(("run", None)))
    _streams(monkeypatch, tty=True)
    _textual(monkeypatch, installed=True)

    assert cli.main(["--offline"]) == 0

    assert events == [("run", None)], "--offline fetched"


def test_plan_reaches_the_screen(monkeypatch):
    calls = _stand_in_tui(monkeypatch)
    _streams(monkeypatch, tty=True)
    _textual(monkeypatch, installed=True)

    assert cli.main(["--plan", "max"]) == 0

    assert calls[0]["plan"] == "max"


def test_plan_reaches_the_default_run_analyze(monkeypatch):
    """The declaration is one claim: the report must be judged against it."""
    _stand_in_tui(monkeypatch)
    _streams(monkeypatch, tty=False)
    _textual(monkeypatch, installed=True)
    monkeypatch.setattr(cli, "_account_line", lambda allow_network=False: "account: unknown")
    ran = []
    # 2 is "nothing was measured", so the run stops before the chart.
    monkeypatch.setattr(cli, "_run", lambda args: ran.append(args) or 2)

    assert cli.main(["--plan", "max", "--offline"]) == 2

    assert [a.cmd for a in ran] == ["analyze"]
    assert ran[0].plan == "max", "the declaration did not reach the analysis"
    assert ran[0].since == "1h"


def test_plan_reaches_the_default_run_chart(monkeypatch):
    """The chart is part of the same run, so the declaration reaches it too.

    Without it the chart footer asks for a `--plan` that this command already
    carried, which reads as if the declaration was dropped.
    """
    _stand_in_tui(monkeypatch)
    _streams(monkeypatch, tty=False)
    _textual(monkeypatch, installed=True)
    monkeypatch.setattr(cli, "_account_line", lambda allow_network=False: "account: unknown")
    monkeypatch.setattr(cli, "_open_png_quietly", lambda path: None)
    ran = []
    monkeypatch.setattr(cli, "_run", lambda args: ran.append(args) or 0)

    assert cli.main(["--plan", "max", "--offline"]) == 0

    assert [a.cmd for a in ran] == ["analyze", "chart"]
    assert ran[1].plan == "max", "the chart asked for a declaration the run carried"
    assert ran[1].since == "1h"
