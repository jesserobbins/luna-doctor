"""The PNG rasteriser and its bitmap font.

`png_from` rasterises the same Scene `svg_from` serialises, so a chart drawn
for a terminal and a card drawn for a phone cannot disagree. These tests
decode the IDAT by hand (the standard library has no PNG reader) and check
the primitives, the opacity blending and the font's arithmetic.
"""

import struct
import unittest
import zlib

from luna_doctor.font import FONT, ascii_fold, text_width
from luna_doctor.png import png_from
from luna_doctor.scene import Circle, Line, Rect, Scene, Text


def _pixels(data, w, h):
    """Decode a PNG written by png_from into a flat list of (r, g, b) rows."""
    idat = data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8]
    raw = zlib.decompress(idat)
    stride = 1 + w * 3                       # one filter byte per scanline
    expected = h * stride
    if len(raw) != expected:
        raise AssertionError(f"scanline buffer is {len(raw)} bytes, expected {expected}")
    rows = []
    for y in range(h):
        base = y * stride + 1                # skip the filter byte
        rows.append([tuple(raw[base + x * 3:base + x * 3 + 3]) for x in range(w)])
    return rows


def _dark_count(rows):
    return sum(1 for row in rows for px in row if px != (255, 255, 255))


class PngWriter(unittest.TestCase):
    def test_signature_and_chunks(self):
        data = png_from(Scene(4, 3, "#ffffff", [Rect(1, 1, 2, 1, fill="#000000")]))
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn(b"IHDR", data); self.assertIn(b"IDAT", data); self.assertIn(b"IEND", data)
        w, h = struct.unpack(">II", data[16:24])
        self.assertEqual((w, h), (4, 3))
        self.assertEqual(data[24:26], bytes([8, 2]))     # 8-bit, truecolor

    def test_pixels_round_trip(self):
        data = png_from(Scene(2, 2, "#ffffff", [Rect(1, 1, 1, 1, fill="#000000")]))
        idat = data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8]
        raw = zlib.decompress(idat)
        # 2 rows x (1 filter byte + 2 px x 3 bytes); bottom-right pixel set
        self.assertEqual(raw[0], 0)
        self.assertEqual(raw[-3:], b"\x00\x00\x00")

    def test_single_idat_and_filter_bytes(self):
        data = png_from(Scene(6, 4, "#ffffff", []))
        self.assertEqual(data.count(b"IDAT"), 1)
        raw = zlib.decompress(data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8])
        self.assertEqual(len(raw), 4 * (1 + 6 * 3))
        self.assertEqual([raw[i] for i in range(0, len(raw), 1 + 6 * 3)], [0, 0, 0, 0])

    def test_text_is_rendered_from_the_font(self):
        data = png_from(Scene(30, 20, "#ffffff", [Text(0, 0, "A", size=7)]))
        idat = data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8]
        self.assertNotEqual(zlib.decompress(idat).count(b"\xff"), 30 * 20 * 3)

    def test_rect_fill_and_stroke_cover_the_right_pixels(self):
        data = png_from(Scene(10, 10, "#ffffff", [Rect(2, 2, 6, 6, fill="#000000")]))
        rows = _pixels(data, 10, 10)
        self.assertEqual(rows[2][2], (0, 0, 0))       # inside
        self.assertEqual(rows[4][4], (0, 0, 0))
        self.assertEqual(rows[2][1], (255, 255, 255))  # just outside
        self.assertEqual(_dark_count(rows), 36)

        data = png_from(Scene(10, 10, "#ffffff", [Rect(2, 2, 6, 6, stroke="#000000")]))
        rows = _pixels(data, 10, 10)
        self.assertEqual(rows[4][4], (255, 255, 255))  # stroke only: centre untouched
        self.assertEqual(rows[2][2], (0, 0, 0))         # border is inked
        self.assertEqual(rows[7][7], (0, 0, 0))        # ...all the way round
        self.assertEqual(rows[2][0], (255, 255, 255))   # and no further

    def test_opacity_blends_against_what_is_already_there(self):
        scene = Scene(4, 1, "#ffffff", [Rect(0, 0, 1, 1, fill="#000000", fill_opacity=0.5)])
        rows = _pixels(png_from(scene), 4, 1)
        self.assertEqual(rows[0][0], (128, 128, 128))   # 255 * (1 - 0.5)
        # a second translucent pass darkens the same pixel further
        scene.shapes.append(Rect(0, 0, 1, 1, fill="#000000", fill_opacity=0.5))
        rows = _pixels(png_from(scene), 4, 1)
        self.assertEqual(rows[0][0], (64, 64, 64))
        # the blend sees shapes underneath, not just the background
        scene = Scene(4, 1, "#ffffff", [
            Rect(1, 0, 1, 1, fill="#ff0000"),
            Rect(1, 0, 1, 1, fill="#000000", fill_opacity=0.5),
        ])
        rows = _pixels(png_from(scene), 4, 1)
        self.assertEqual(rows[0][1], (128, 0, 0))

    def test_line_is_solid_by_default_and_gapped_when_dashed(self):
        solid = _pixels(png_from(Scene(10, 1, "#ffffff",
                                       [Line(0, 0, 9, 0, "#000")])), 10, 1)
        self.assertEqual(_dark_count(solid), 10)
        dashed = _pixels(png_from(Scene(10, 1, "#ffffff",
                                         [Line(0, 0, 9, 0, "#000", 1.0, (1, 3))])), 10, 1)
        inked = [x for x in range(10) if dashed[0][x] == (0, 0, 0)]
        self.assertEqual(inked, [0, 4, 8])            # 1 on, 3 off, repeat

    def test_line_stroke_width_thickens_the_walk(self):
        rows = _pixels(png_from(Scene(10, 5, "#ffffff",
                                       [Line(0, 2, 9, 2, "#000", 3.0)])), 10, 5)
        for y in (1, 2, 3):
            self.assertEqual(rows[y][5], (0, 0, 0))
        self.assertEqual(rows[0][5], (255, 255, 255))
        self.assertEqual(rows[4][5], (255, 255, 255))

    def test_diagonal_line_walks_bresenham(self):
        rows = _pixels(png_from(Scene(5, 5, "#ffffff", [Line(0, 0, 4, 4, "#000")])), 5, 5)
        self.assertEqual(_dark_count(rows), 5)
        for i in range(5):
            self.assertEqual(rows[i][i], (0, 0, 0))

    def test_circle_fills_a_disc(self):
        rows = _pixels(png_from(Scene(11, 11, "#ffffff",
                                       [Circle(5, 5, 2, fill="#000")])), 11, 11)
        self.assertEqual(_dark_count(rows), 13)       # dx^2 + dy^2 <= r^2 has 13 pixels
        self.assertEqual(rows[5][5], (0, 0, 0))
        self.assertEqual(rows[3][5], (0, 0, 0))       # (0, -2) is on the boundary
        self.assertEqual(rows[5][8], (255, 255, 255))  # (3, 0) is outside
        self.assertEqual(rows[3][3], (255, 255, 255))  # so is the corner

    def test_circle_outlines_a_ring(self):
        rows = _pixels(png_from(Scene(11, 11, "#ffffff",
                                       [Circle(5, 5, 2, stroke="#000")])), 11, 11)
        self.assertEqual(rows[5][5], (255, 255, 255))  # midpoint: centre stays empty
        self.assertEqual(rows[5][7], (0, 0, 0))        # (2, 0) on the ring
        self.assertEqual(rows[7][5], (0, 0, 0))        # (0, 2) too
        self.assertEqual(rows[6][6], (255, 255, 255))  # (1, 1) is inside the ring
        self.assertGreaterEqual(_dark_count(rows), 8)

    def test_text_anchor_and_scale(self):
        # "end" right-aligns: the last ink column is the canvas edge minus tracking
        rows = _pixels(png_from(Scene(40, 10, "#ffffff",
                                       [Text(40, 0, "A", size=7, anchor="end")])), 40, 10)
        inked = [x for row in rows for x, px in enumerate(row) if px != (255, 255, 255)]
        self.assertEqual(max(inked), 38)               # width 6 - 1 tracking = 5 cols
        self.assertEqual(min(inked), 34)
        # scale 3 (size 21) turns each font pixel into a 3x3 block
        rows = _pixels(png_from(Scene(60, 30, "#ffffff",
                                       [Text(0, 0, "A", size=21)])), 60, 30)
        self.assertEqual(_dark_count(rows), 18 * 9)    # 'A' has 18 ink cells

    def test_text_outside_the_font_folds_instead_of_drawing_a_box(self):
        # the chart legend uses \u2715 and \u25cf; both fold to ASCII glyphs
        # before rasterising, so the raster shows letters, not hollow boxes
        for mark, folded in (("\u2715", "x"), ("\u25cf", "o")):
            marked = png_from(Scene(30, 10, "#ffffff", [Text(0, 0, mark, size=7)]))
            plain = png_from(Scene(30, 10, "#ffffff", [Text(0, 0, folded, size=7)]))
            self.assertEqual(marked, plain)

    def test_canvas_text_falls_back_to_a_box_for_a_missing_glyph(self):
        # ascii_fold runs before the canvas, so png_from cannot reach this
        # fallback; it stays as a last resort for direct canvas.text calls
        from luna_doctor.png import _Canvas
        canvas = _Canvas(6, 7, b"\xff" * (6 * 7 * 3))
        canvas.text(0, 0, "\u2605", b"\x00\x00\x00", 7, "start")
        self.assertEqual(canvas.buf.count(0), 20 * 3)    # the box outline, in ink

    def test_folded_text_rasterises_identically_to_its_ascii_spelling(self):
        # the copy says "a\u00b7b"; the raster must be the one "a-b" draws
        dotted = png_from(Scene(60, 30, "#ffffff", [Text(0, 0, "a\u00b7b", size=21)]))
        dashed = png_from(Scene(60, 30, "#ffffff", [Text(0, 0, "a-b", size=21)]))
        self.assertEqual(dotted, dashed)

    def test_unmapped_character_draws_a_question_mark_not_the_box(self):
        # \u2605 BLACK STAR has no fold entry: it must draw the '?' glyph
        star = png_from(Scene(30, 10, "#ffffff", [Text(0, 0, "\u2605", size=7)]))
        asked = png_from(Scene(30, 10, "#ffffff", [Text(0, 0, "?", size=7)]))
        self.assertEqual(star, asked)                      # never the box glyph

    def test_emoji_in_text_does_not_raise_and_leaves_ink(self):
        data = png_from(Scene(30, 10, "#ffffff",
                              [Text(0, 0, "\U0001f680", size=7)]))   # ROCKET
        rows = _pixels(data, 30, 10)
        self.assertGreater(_dark_count(rows), 0)           # folded to '?', which has ink
        self.assertEqual(data, png_from(Scene(30, 10, "#ffffff",
                                              [Text(0, 0, "?", size=7)])))

    def test_every_primitive_clips_to_the_canvas(self):
        scene = Scene(8, 8, "#ffffff", [
            Rect(-4, -4, 8, 8, fill="#000"),
            Rect(4, 4, 8, 8, stroke="#000"),
            Line(-5, -5, 20, 20, "#000"),
            Circle(-2, 10, 4, fill="#000"),
            Circle(20, -3, 3, stroke="#000"),
            Text(6, 3, "WWWWWW", size=7),
        ])
        data = png_from(scene)                          # must not raise or read OOB
        rows = _pixels(data, 8, 8)
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(len(r) == 8 for r in rows))


class Font(unittest.TestCase):
    def test_covers_printable_ascii(self):
        for i in range(32, 127):
            self.assertIn(chr(i), FONT)
            self.assertEqual(len(FONT[chr(i)]), 7)

    def test_rows_are_five_cells_of_hash_and_dot(self):
        for glyph in FONT.values():
            self.assertEqual(len(glyph), 7)
            for row in glyph:
                self.assertEqual(len(row), 5)
                self.assertTrue(set(row) <= {"#", "."})

    def test_width_accounts_for_scale_and_tracking(self):
        self.assertEqual(text_width("AB", 3), 2 * (5 + 1) * 3)

    def test_width_of_nothing_is_zero(self):
        self.assertEqual(text_width("", 1), 0)


class AsciiFold(unittest.TestCase):
    def test_typographic_characters_map_to_font_drawable_ascii(self):
        cases = {
            "\u00b7": "-",     # MIDDLE DOT joins the chart's summary line
            "\u2014": "-",     # EM DASH
            "\u2013": "-",     # EN DASH
            "\u2264": "<=",
            "\u2265": ">=",
            "\u00d7": "x",
            "\u2192": "->",
            "\u2026": "...",
            "\u2018": "'",
            "\u2019": "'",
            "\u201c": '"',
            "\u201d": '"',
            "\u00b0": "deg",
            "\u00a0": " ",    # NO-BREAK SPACE
        }
        for src, want in cases.items():
            self.assertEqual(ascii_fold(src), want, f"{src!r} should fold to {want!r}")
        self.assertEqual(ascii_fold("a\u00b7b\u2014c \u2264 8"), "a-b-c <= 8")

    def test_plain_ascii_passes_through_untouched(self):
        for i in range(32, 127):
            self.assertEqual(ascii_fold(chr(i)), chr(i))

    def test_unmapped_characters_become_a_question_mark(self):
        self.assertEqual(ascii_fold("\u2605"), "?")       # BLACK STAR: no entry
        self.assertEqual(ascii_fold("a\u2605b"), "a?b")
        self.assertEqual(ascii_fold("\U0001f680"), "?")    # emoji: no entry either


if __name__ == "__main__":
    unittest.main()
