# tests/test_badges.py
"""The badge layer's contract: one glyph per provenance state.

The TUI verdict screen puts a badge on every number, so a badge that claims
more trust than its state has is the bug this file guards. The states are
sources.PROVENANCE_LEGEND's words plus attribution's "plan <name>" source.
"""

import os
import unittest

from luna_doctor import badges

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BadgeFor(unittest.TestCase):
    def test_account_is_the_filled_badge(self):
        self.assertEqual("●", badges.BADGE_ACCOUNT)
        self.assertEqual("●", badges.badge_for("account"))

    def test_measured_is_the_hollow_badge(self):
        self.assertEqual("○", badges.BADGE_MEASURED)
        self.assertEqual("○", badges.badge_for("measured"))

    def test_declared_is_the_dotted_badge(self):
        self.assertEqual("◌", badges.BADGE_DECLARED)
        self.assertEqual("◌", badges.badge_for("declared"))

    def test_a_plan_name_is_a_declaration(self):
        """attribution.resolve_priority_concurrency reports "plan <name>" for
        a plan name the user typed; that is stated, never measured."""
        self.assertEqual("◌", badges.badge_for("plan max"))

    def test_a_documented_constant_is_a_declaration(self):
        self.assertEqual("◌", badges.badge_for("configured"))

    def test_an_unclassified_state_never_looks_measured(self):
        for state in (None, "unknown", "nonsense", "", "catalog"):
            self.assertEqual("?", badges.badge_for(state),
                             f"{state!r} must not borrow a measured badge")


class LegendLine(unittest.TestCase):
    def test_legend_names_all_three_badges(self):
        line = badges.legend_line()
        for badge in ("●", "○", "◌"):
            self.assertIn(badge, line)

    def test_legend_says_authoritative_exactly_once(self):
        self.assertEqual(1, badges.legend_line().count("authoritative"))

    def test_legend_is_one_line_with_no_trailing_space(self):
        line = badges.legend_line()
        self.assertNotIn("\n", line)
        self.assertEqual(line, line.rstrip())

    def test_readme_quotes_the_legend_line(self):
        """The README's legend is the one the screen draws, not a paraphrase."""
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        self.assertIn(badges.legend_line(), readme)


class BadgeHelp(unittest.TestCase):
    def test_account_help_names_the_account(self):
        phrase = badges.badge_help("account")
        self.assertTrue(phrase.strip())
        self.assertIn("account", phrase)

    def test_the_three_phrases_match_the_design(self):
        self.assertEqual("the account reported it", badges.badge_help("account"))
        self.assertEqual("measured locally, a lower bound",
                         badges.badge_help("measured"))
        self.assertEqual("declared, not measured", badges.badge_help("declared"))
        self.assertEqual("declared, not measured", badges.badge_help("plan max"))

    def test_unclassified_help_is_not_a_measurement_claim(self):
        phrase = badges.badge_help(None)
        self.assertTrue(phrase.strip())
        self.assertNotIn("measured locally", phrase)


if __name__ == "__main__":
    unittest.main()
