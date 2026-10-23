"""Tests for the WhatsApp share surfaces: the message and the card.

Run from the repo root:

    python3 -m unittest tests.test_share -v
"""

import collections
import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

from luna_doctor.render_share import render_share_text, wa_safe
from luna_doctor.report import Report, Row, Section

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "bin", "luna-doctor")


class WhatsAppSafety(unittest.TestCase):
    """A model or session string must never open a WhatsApp formatting run."""

    def test_metacharacters_are_neutralised(self):
        out = wa_safe("glm-5.3_background *peak* ~x~ `y`")
        # The plan's own wa_safe spec substitutes or drops every metacharacter,
        # so none of them can survive; a balanced pair like *peak* would still
        # render as bold inside WhatsApp, which is exactly what must not happen.
        for ch in "*_~`":
            self.assertNotIn(ch, out)

    def test_substitutes_still_read(self):
        out = wa_safe("glm-5.3_background")
        self.assertEqual(out, "glm-5.3-background")

    def test_no_leading_spaces_or_tabs_survive(self):
        out = wa_safe("    indented\tline")
        self.assertFalse(out.startswith(" "))
        self.assertNotIn("\t", out)

    def test_untouched_text_passes_through(self):
        self.assertEqual(wa_safe("peak concurrency 17"), "peak concurrency 17")


class ShareText(unittest.TestCase):
    def _rep(self, headline="Pressure found."):
        return Report(headline, [Section("traffic", [Row("peak concurrency", "17", "bad")])],
                      ["Narrow the fan-out to 8."])

    def _m(self, **over):
        m = {"requests": 1661, "peak_concurrency": 17,
             "by_outcome": {"admission_timeout": 0, "client_idle_timeout": 3},
             "by_harness": {"pi": 1500, "claude-code": 161},
             "latency_s": {"interactive": {"n": 100, "p50": 45.0, "p90": 120.0, "max": 300.0},
                           "background": {"n": 0, "p50": None, "p90": None, "max": None}},
             "window_hours": 24.0}
        m.update(over)
        return m

    def test_headline_is_quoted_and_bold_is_balanced(self):
        text = render_share_text(self._rep(), {})
        self.assertTrue(text.startswith("> ") or "\n> " in text)
        self.assertEqual(text.count("*") % 2, 0)

    def test_numbers_are_bold_labelled_pairs(self):
        text = render_share_text(self._rep(), self._m())
        self.assertIn("*requests:* 1661", text)
        self.assertIn("pi 1500", text)
        self.assertIn("*peak concurrency:* 17", text)
        self.assertIn("*admission deaths:* 0", text)
        self.assertIn("*silent stalls:* 3", text)
        self.assertIn("*interactive latency:* p50 45.0s, p90 120.0s", text)

    def test_adjustments_are_bullets(self):
        text = render_share_text(self._rep(), {})
        self.assertIn("- Narrow the fan-out to 8.", text)

    def test_provenance_names_the_window_and_the_account(self):
        text = render_share_text(self._rep(), self._m())
        # 24.0 hours renders as "1d", the same spelling cli.human_window gives
        # the terminal's window row - the card and the text cannot disagree.
        self.assertIn("measured on this account, last 1d", text)

    def test_no_monospace_blocks_or_emoji(self):
        text = render_share_text(self._rep(), {})
        self.assertNotIn("```", text)
        self.assertNotIn("\U0001F319", text)
        self.assertNotIn("\033", text)

    def test_zero_request_window_says_no_data(self):
        text = render_share_text(self._rep("No data."), {"requests": 0})
        self.assertIn("No data", text)
        self.assertNotIn("nan", text.lower())
        self.assertNotIn("None", text)

    def test_swamp_configured_never_sums_into_measured_numbers(self):
        m = self._m(requests=6, peak_concurrency=6, by_outcome={},
                   latency_s={"interactive": {"n": 6, "p50": 45.0, "p90": 120.0, "max": 60.0},
                              "background": {"n": 0, "p50": None, "p90": None, "max": None}},
                   swamp={"present": True, "configuredConcurrency": 20})
        text = render_share_text(self._rep(), m)
        self.assertIn("configured", text)
        self.assertIn("*peak concurrency:* 6", text)
        # 6 measured + 20 configured must never be presented as 26 of anything.
        self.assertNotIn("26", text)

    def _busy_m(self):
        """The shape of a real pressured run: measured figures, swamp in use."""
        return self._m(requests=6654, peak_concurrency=17,
                        by_harness={"pi": 5884, "claude-code": 770},
                        by_outcome={"admission_timeout": 20, "client_idle_timeout": 3},
                        pressure_windows=21,
                        swamp={"present": True, "configuredConcurrency": 20})

    def _busy_rep(self):
        return Report(
            "Concurrency pressure across 6654 requests: 20 admission death(s), "
            "3 silent stall(s), peak concurrency 17.",
            [Section("traffic", [Row("peak concurrency", "17", "bad")])],
            [
                "20 request(s) died on admission (`scheduler admission timeout`).",
                "3 request(s) stalled until the harness's own ~300s idle budget killed them.",
                "21 window(s) had more than 8 requests in flight at once.",
                "1577 request(s) already ran on the -background tier.",
                "1 request(s) hit an account rate limit (429 rate_limit_error).",
                "8 stalled request(s) had another request to the same model complete "
                "inside their own window.",
                "swamp is configured to fan out to 20 concurrent requests per behavior - "
                "a setting, not measured traffic.",
                "Adjust: keep interactive fan-out at or under 8, and move any wider "
                "fan-out to the -background variant of the same model.",
            ])

    def test_message_is_trimmed_for_a_group_chat(self):
        """One headline, at most five value lines, at most three bullets,
        one provenance line - a chat message, not the terminal report."""
        text = render_share_text(self._busy_rep(), self._busy_m())
        lines = text.rstrip("\n").split("\n")
        self.assertTrue(lines[0].startswith("> "), "the headline opens the message")
        self.assertEqual(len([l for l in lines if l.startswith("> ")]), 1)
        value_lines = [l for l in lines if re.match(r"^\*[^*]+\*: ", l)]
        self.assertLessEqual(len(value_lines), 5)
        bullets = [l for l in lines if l.startswith("- ")]
        self.assertLessEqual(len(bullets), 3)
        # The three that decide something: the overflow cause, the stall
        # cause, the action - and the action closes the message body.
        self.assertTrue(any("probe --enable-probe" in b for b in bullets),
                        "the overflow bullet points at the probe")
        self.assertTrue(any("silent-overflow" in b for b in bullets),
                        "the stall bullet names the signature")
        self.assertEqual(bullets[-1],
                         "- Keep interactive fan-out at or under 8, and move any wider "
                         "fan-out to the -background variant of the same model.")
        self.assertEqual(lines[-1], "measured on this account, last 1d")
        # Swamp is in use, so it gets exactly one mention - its configured
        # value line - and the latency line yields its slot to stay at five.
        self.assertEqual(len([l for l in lines if "swamp" in l.lower()]), 1)
        self.assertNotIn("interactive latency", text)
        # No bullet may repeat a number the value lines already carry.
        value_numbers = set(re.findall(r"\d+(?:\.\d+)?", " ".join(value_lines)))
        for b in bullets:
            self.assertEqual(set(re.findall(r"\d+(?:\.\d+)?", b)) & value_numbers, set(),
                             f"bullet repeats a value-line number: {b}")

    def test_swamp_is_silent_when_not_in_use(self):
        """Installed is not in use: no value line, no bullet, no mention."""
        text = render_share_text(self._busy_rep(), self._m())
        self.assertNotIn("swamp", text.lower())


class ShareCard(unittest.TestCase):
    def _m(self, **over):
        m = {"requests": 1661, "peak_concurrency": 17,
             "by_outcome": {"admission_timeout": 3, "client_idle_timeout": 0},
             "by_harness": {"pi": 1500, "claude-code": 161},
             "latency_s": {"interactive": {"n": 100, "p50": 45.0, "p90": 120.0, "max": 300.0},
                           "background": {"n": 0, "p50": None, "p90": None, "max": None}},
             "window_hours": 24.0}
        m.update(over)
        return m

    def _png(self, m, headline):
        from luna_doctor.png import png_from
        from luna_doctor.scene import build_card_scene
        return png_from(build_card_scene(m, headline))

    def test_png_is_1080x1350_with_drawn_content(self):
        from luna_doctor.scene import CARD_PALETTE

        data = self._png(self._m(), "No concurrency pressure across 1661 requests.")
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        w, h = struct.unpack(">II", data[16:24])
        self.assertEqual((w, h), (1080, 1350))
        counts = collections.Counter(_pixels(data, w, h))
        bg, _ = counts.most_common(1)[0]
        # Moon, text and stars: more than one colour beyond the background.
        self.assertGreaterEqual(len([c for c in counts if c != bg]), 2)
        # Not a blank page: the background is the night sky, and no pure black.
        self.assertNotEqual(bg, (255, 255, 255))
        self.assertNotIn((0, 0, 0), counts)
        # The crescent: moon ink is present, but the sky-coloured bite disc
        # carved into it, so it is not a full disc either.
        moon = tuple(int(CARD_PALETTE["moon"][i:i + 2], 16) for i in (1, 3, 5))
        self.assertGreater(counts[moon], 0)
        self.assertLess(counts[moon], int(3.14 * 100 * 100))

    def test_scene_carries_the_numbers_and_provenance(self):
        from luna_doctor.scene import build_card_scene, svg_from

        svg = svg_from(build_card_scene(self._m(), "No concurrency pressure across 1661 requests."))
        self.assertIn("1661", svg)
        self.assertIn("17", svg)
        self.assertIn("peak concurrency", svg)
        self.assertIn("measured on this account", svg)
        self.assertNotIn("nan", svg)
        self.assertNotIn("None", svg)

    def test_scene_labels_swamp_fanout_as_configured(self):
        from luna_doctor.scene import build_card_scene, svg_from

        m = self._m(swamp={"present": True, "configuredConcurrency": 20})
        svg = svg_from(build_card_scene(m, "head"))
        self.assertIn("configured", svg)
        self.assertNotIn("37", svg)   # 17 measured + 20 configured is never a sum

    def test_zero_request_card_renders_without_nan(self):
        from luna_doctor.scene import build_card_scene, svg_from

        svg = svg_from(build_card_scene({"requests": 0}, "No requests found in the window."))
        # The headline wraps at the card's width, so it spans two text lines.
        self.assertIn("No requests found in the", svg)
        self.assertIn("window.", svg)
        self.assertNotIn("nan", svg)
        self.assertNotIn("None", svg)


class ShareCli(unittest.TestCase):
    """The stdout/stderr split: stdout alone is the copyable artifact."""

    def test_share_puts_the_message_on_stdout_and_the_path_on_stderr(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "card.png")
            env = dict(os.environ)
            env["LUNA_DOCTOR_STORE"] = os.path.join(d, "store")
            proc = subprocess.run(
                [sys.executable, CLI, "analyze", "--since", "1h", "--share", "--out", out],
                capture_output=True, text=True, timeout=180, cwd=ROOT, env=env)
            self.assertIn(proc.returncode, (0, 1, 2))
            self.assertTrue(proc.stdout.startswith("> "))
            self.assertNotIn("\033", proc.stdout)
            self.assertNotIn("\U0001F319", proc.stdout)
            self.assertIn(os.path.basename(out), proc.stderr)
            self.assertNotIn("share card", proc.stdout)
            self.assertTrue(os.path.isfile(out))
            with open(out, "rb") as fh:
                self.assertTrue(fh.read(8).startswith(b"\x89PNG"))


def _pixels(data, w, h):
    """Yield every (r, g, b) of a filter-0 truecolor PNG, for content checks."""
    idat = data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8]
    raw = zlib.decompress(idat)
    stride = w * 3
    for y in range(h):
        base = y * (stride + 1) + 1
        row = raw[base:base + stride]
        for x in range(w):
            yield (row[x * 3], row[x * 3 + 1], row[x * 3 + 2])


if __name__ == "__main__":
    unittest.main()
