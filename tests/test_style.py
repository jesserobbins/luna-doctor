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
