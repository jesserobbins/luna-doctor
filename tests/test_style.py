# tests/test_style.py
import unittest
from luna_doctor.style import resolve_style

class _Stream:
    def __init__(self, tty): self._tty = tty
    def isatty(self): return self._tty

class StyleGate(unittest.TestCase):
    def test_no_color_wins_over_explicit_pretty(self):
        s = resolve_style("pretty", _Stream(True), {"NO_COLOR": "1"})
        self.assertFalse(s.color)
        self.assertEqual(s.paint("x", "bad"), "x")

    def test_no_color_empty_string_is_unset(self):
        self.assertTrue(resolve_style("pretty", _Stream(True), {"NO_COLOR": ""}).color)

    def test_clicolor_force_colors_a_pipe(self):
        self.assertTrue(resolve_style(None, _Stream(False), {"CLICOLOR_FORCE": "1"}).color)

    def test_clicolor_force_zero_counts_as_unset(self):
        s = resolve_style(None, _Stream(False), {"CLICOLOR_FORCE": "0"})
        self.assertFalse(s.color)
        self.assertFalse(s.emoji)

    def test_auto_off_when_not_a_tty(self):
        s = resolve_style(None, _Stream(False), {})
        self.assertFalse(s.color); self.assertFalse(s.emoji)

    def test_auto_on_for_a_tty(self):
        s = resolve_style(None, _Stream(True), {})
        self.assertTrue(s.color); self.assertTrue(s.emoji)

    def test_plain_is_never_colored_even_forced(self):
        s = resolve_style("plain", _Stream(True), {"CLICOLOR_FORCE": "1"})
        self.assertFalse(s.color); self.assertFalse(s.emoji)


    def test_emoji_degrades_to_ascii_symbol(self):
        s = resolve_style("plain", _Stream(True), {})
        self.assertEqual(s.symbol("moon"), "(c)")


# ---- the moon status vocabulary --------------------------------------------
# The moons are the status scale every surface reads, so the glyphs and the
# words beside them are pinned here: a renamed symbol, a missing stand-in or a
# drift in the legend sentence fails in one place.
MOON_GLYPHS = {"moon_full": "\U0001F315", "moon_quarter": "\U0001F317",
               "moon_new": "\U0001F311"}


class MoonStatusSymbols(unittest.TestCase):
    def test_each_phase_has_its_glyph(self):
        s = resolve_style("pretty", _Stream(True), {})
        for name, glyph in MOON_GLYPHS.items():
            self.assertEqual(glyph, s.symbol(name))

    def test_the_plain_moon_is_unchanged(self):
        s = resolve_style("pretty", _Stream(True), {})
        self.assertEqual("\U0001F319", s.symbol("moon"))

    def test_every_phase_degrades_to_its_own_ascii_stand_in(self):
        s = resolve_style("plain", _Stream(True), {})
        stand_ins = []
        for name in MOON_GLYPHS:
            stand_in = s.symbol(name)
            self.assertTrue(stand_in, f"{name} has no ASCII stand-in")
            stand_in.encode("ascii")     # 7-bit: it survives any encoding
            stand_ins.append(stand_in)
        self.assertEqual(len(stand_ins), len(set(stand_ins)),
                         "each phase needs its own stand-in")

    def test_the_legend_is_one_line_in_each_alphabet(self):
        styled = resolve_style("pretty", _Stream(True), {})
        self.assertEqual("\U0001F315 healthy \u00b7 \U0001F317 pressure \u00b7 "
                         "\U0001F311 blocked at admission", styled.moon_legend())
        plain = resolve_style("plain", _Stream(True), {})
        ascii_legend = plain.moon_legend()
        ascii_legend.encode("ascii")     # the degraded legend stays 7-bit text
        for name, word in (("moon_full", "healthy"), ("moon_quarter", "pressure"),
                           ("moon_new", "blocked at admission")):
            self.assertIn(f"{plain.symbol(name)} {word}", ascii_legend)

    def test_the_legend_names_exactly_the_verdict_states(self):
        """The glyph table and the verdict's own names are one vocabulary; a
        rename in either place would draw nothing, so they are pinned equal."""
        from luna_doctor.report import MOON_PHASES
        from luna_doctor.style import MOON_STATUS
        self.assertEqual(MOON_PHASES, tuple(name for name, _word in MOON_STATUS))
