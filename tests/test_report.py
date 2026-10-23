import unittest
from luna_doctor.report import Report, Row, Section, render_plain

class PlainRenderer(unittest.TestCase):
    def _report(self):
        return Report(
            headline="No pressure in this window.",
            sections=[Section("traffic", [
                Row("requests", "1661"),
                Row("peak concurrency", "17", "bad"),
                Row("model", "very-long-model-name-with-an-unwrap-friendly-suffix-0123456789"),
            ])],
            adjustments=["Keep concurrent requests at or under 8."],
            footnote="measured on this account",
        )

    def test_no_ansi_no_emoji(self):
        out = render_plain(self._report())
        self.assertNotIn("\033", out)
        self.assertNotIn("🌙", out)

    def test_labels_are_self_describing_without_alignment(self):
        out = render_plain(self._report())
        self.assertIn("requests: 1661", out)
        self.assertIn("peak concurrency: 17", out)

    def test_inline_label_value_without_column_padding(self):
        out = render_plain(self._report())
        line = [l for l in out.splitlines() if l.startswith("model: ")][0]
        self.assertEqual(
            line, "model: very-long-model-name-with-an-unwrap-friendly-suffix-0123456789"
        )

    def test_adjustments_and_footnote_present(self):
        out = render_plain(self._report())
        self.assertIn("- Keep concurrent requests at or under 8.", out)
        self.assertIn("measured on this account", out)

    def test_empty_report_still_renders(self):
        out = render_plain(Report(headline="no data", sections=[]))
        self.assertIn("no data", out)

    def test_value_newlines_become_indented_continuation_lines(self):
        report = Report(
            headline="note",
            sections=[Section("state", [Row("detail", "first line\nsecond line")])],
        )
        lines = render_plain(report).splitlines()
        self.assertIn("detail: first line", lines)
        self.assertIn("  second line", lines)

    def test_output_ends_with_exactly_one_newline(self):
        out = render_plain(self._report())
        self.assertTrue(out.endswith("\n"))
        self.assertFalse(out.endswith("\n\n"))
        self.assertNotEqual(out.splitlines()[-1], "")
