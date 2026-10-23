"""Tests for the pretty renderer and the stream-capability checks around it.

Run from the repo root:

    python3 -m unittest tests.test_pretty -v
"""

import unittest

from luna_doctor.report import Report, Row, Section
from luna_doctor.style import resolve_style


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
