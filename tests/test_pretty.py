"""Tests for the pretty renderer and the stream-capability checks around it.

Run from the repo root:

    python3 -m unittest tests.test_pretty -v
"""

import unittest

from luna_doctor.report import Report, Row, Section
from luna_doctor.style import resolve_style


def _glyphs_only():
    """The gate a terminal gets with the glyphs allowed and no ANSI codes.

    `--style pretty` also opens color, and the headline is painted bold; these
    tests read the glyphs, so they ask for the glyphs alone.
    """
    from luna_doctor.style import Style
    return Style(color=False, emoji=True, motion=False)


class _Tty:
    def isatty(self):
        return True

    encoding = "utf-8"

    def write(self, s):
        pass

    def flush(self):
        pass


class PrettyRenderer(unittest.TestCase):
    def _rep(self):
        return Report("Pressure found.", [Section("traffic", [Row("peak concurrency", "17", "bad")])])

    def test_moon_in_headline_when_emoji_allowed(self):
        from luna_doctor.render_pretty import render_pretty
        style = resolve_style("pretty", _Tty(), {})
        self.assertIn("🌙", render_pretty(self._rep(), style))

    def test_no_moon_when_emoji_not_allowed(self):
        from luna_doctor.render_pretty import render_pretty
        style = resolve_style("plain", _Tty(), {})
        out = render_pretty(self._rep(), style)
        self.assertNotIn("🌙", out)
        self.assertIn("Pressure found.", out)

    def test_ascii_fallback_on_non_utf8_stream(self):
        from luna_doctor.render_pretty import render_pretty, style_for

        class _Cp(_Tty):
            encoding = "cp1252"

        style = style_for("pretty", _Cp())
        out = render_pretty(self._rep(), style)
        out.encode("cp1252")          # must not raise
        self.assertNotIn("🌙", out)
        self.assertIn("(c)", out)     # the moon degraded to its ASCII stand-in

    def test_values_align_within_one_column(self):
        from luna_doctor.render_pretty import render_pretty
        style = resolve_style("plain", _Tty(), {})
        rep = Report("h", [Section("s", [Row("a", "1"), Row("longer label", "22222")])])
        one = [l for l in render_pretty(rep, style).splitlines() if l.endswith("1")]
        two = [l for l in render_pretty(rep, style).splitlines() if l.endswith("22222")]
        self.assertEqual(one[0].index("1") + 1, two[0].index("22222") + 5)  # one right edge

    def test_a_row_wider_than_the_report_falls_back_to_label_value(self):
        from luna_doctor.render_pretty import render_pretty
        style = resolve_style("plain", _Tty(), {})
        rep = Report("h", [Section("s", [Row("model", "x" * 90)])])
        self.assertIn("model: " + "x" * 90, render_pretty(rep, style, width=40))

    def test_emphasis_paints_the_value(self):
        from luna_doctor.render_pretty import render_pretty
        style = resolve_style("pretty", _Tty(), {})
        out = render_pretty(self._rep(), style)
        self.assertIn("\033[31m17\033[0m", out)   # "bad" is red when color is allowed

    def test_adjustments_and_footnote_survive(self):
        from luna_doctor.render_pretty import render_pretty
        style = resolve_style("plain", _Tty(), {})
        rep = Report("h", [Section("s", [Row("a", "1")])],
                     ["Keep concurrent requests at or under 8."], "measured on this account")
        out = render_pretty(rep, style)
        self.assertIn("- Keep concurrent requests at or under 8.", out)
        self.assertIn("measured on this account", out)


class VerdictMoons(unittest.TestCase):
    """The moon is the status scale, decided by the verdict, not decoration.

    A clean window is the full moon, pressure the quarter moon, a window whose
    requests were blocked at admission the new moon. The pretty renderer and
    the triage screen read the one helper, so neither can draw a different
    moon for the same window.
    """

    BLOCKED = ("Concurrency pressure across 17 requests: 3 request(s) were "
               "blocked at admission, peak concurrency 17.")
    PRESSURE = ("Concurrency pressure across 17 requests: 2 request(s) rejected "
                "with 429, peak concurrency 17.")
    HEALTHY = "17 requests, none blocked at admission, peak concurrency 4."

    def test_the_three_states_map_to_their_moons(self):
        from luna_doctor.report import status_moon
        self.assertEqual("moon_full", status_moon(0, self.HEALTHY))
        self.assertEqual("moon_quarter", status_moon(1, self.PRESSURE))
        self.assertEqual("moon_new", status_moon(1, self.BLOCKED))

    def test_an_account_claim_exits_zero_and_is_still_pressure(self):
        """The account can report pressure this machine cannot explain, and
        that answer exits 0 because nothing local measured anything. It is not
        a healthy window, so it draws the quarter moon."""
        from luna_doctor.report import status_moon
        claim = ("The account reports pressure this machine cannot explain: the "
                 "account's priority pool was full at the sample: in use 12 of 12 "
                 "priority concurrent requests at 2026-11-03T22:14:42Z (account).")
        self.assertEqual("moon_quarter", status_moon(0, claim))

    def test_a_report_with_no_verdict_claims_no_status(self):
        from luna_doctor.report import status_moon
        self.assertEqual("moon", status_moon(None, "latency by model"))
        self.assertEqual("moon", status_moon(2, "No requests found in the window."))

    def test_the_renderer_leads_the_headline_with_the_verdict_moon(self):
        from luna_doctor.render_pretty import render_pretty
        style = _glyphs_only()
        rep = Report(self.BLOCKED, [Section("traffic", [Row("peak concurrency", "17")])],
                     exit_code=1)
        lines = render_pretty(rep, style).splitlines()
        self.assertTrue(lines[0].endswith(self.BLOCKED))
        self.assertTrue(lines[0].startswith("\U0001F311"))

    def test_a_phase_moon_prints_the_legend_once(self):
        from luna_doctor.render_pretty import render_pretty
        style = _glyphs_only()
        rep = Report(self.HEALTHY, [Section("traffic", [Row("requests", "17")])],
                     exit_code=0)
        out = render_pretty(rep, style)
        legend = "\U0001F315 healthy \u00b7 \U0001F317 pressure \u00b7 \U0001F311 blocked at admission"
        self.assertEqual(1, out.count(legend))
        self.assertTrue(out.startswith("\U0001F315"))

    def test_a_plain_moon_prints_no_legend(self):
        from luna_doctor.render_pretty import render_pretty
        style = _glyphs_only()
        rep = Report("latency by model, last 24h", [Section("latency", [Row("n", "4")])])
        out = render_pretty(rep, style)
        self.assertIn("\U0001F319", out)
        self.assertNotIn("blocked at admission", out)


class Utf8Capability(unittest.TestCase):
    def test_codecs_that_refuse_the_moon_are_not_capable(self):
        from luna_doctor.render_pretty import utf8_capable

        class _Cp(_Tty):
            encoding = "cp1252"

        class _Ascii(_Tty):
            encoding = "ascii"

        self.assertTrue(utf8_capable(_Tty()))
        self.assertFalse(utf8_capable(_Cp()))
        self.assertFalse(utf8_capable(_Ascii()))


class Reveal(unittest.TestCase):
    class _Sink(_Tty):
        def __init__(self):
            self.buf = []

        def write(self, s):
            self.buf.append(s)

    def test_motion_is_one_bounded_cr_per_line(self):
        from luna_doctor.render_pretty import reveal
        style = resolve_style("pretty", _Tty(), {})
        sink = self._Sink()
        reveal("probing 8 x glm-5.3", style, sink)
        text = "".join(sink.buf)
        self.assertTrue(text.startswith("\r"))
        self.assertEqual(text.count("\r"), 1)
        self.assertTrue(text.endswith("\n"))
        self.assertNotIn("\033", text)       # no other cursor movement, ever

    def test_without_motion_it_prints_normally(self):
        from luna_doctor.render_pretty import reveal
        style = resolve_style("plain", _Tty(), {})
        sink = self._Sink()
        reveal("probing 8 x glm-5.3", style, sink)
        self.assertEqual("".join(sink.buf), "probing 8 x glm-5.3\n")

    def test_no_style_at_all_prints_normally(self):
        from luna_doctor.render_pretty import reveal
        sink = self._Sink()
        reveal("probing 8 x glm-5.3", None, sink)
        self.assertEqual("".join(sink.buf), "probing 8 x glm-5.3\n")


if __name__ == "__main__":
    unittest.main()
