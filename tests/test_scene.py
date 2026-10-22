"""Scene primitives and the SVG serialiser.

The chart command draws by building a Scene (an ordered list of Rect, Line,
Circle and Text) and rendering it twice: once to SVG here, and later to PNG.
Both renderers consume the same shapes, so the two formats cannot disagree.
"""

import unittest
import xml.etree.ElementTree as ET

from luna_doctor.scene import Circle, Line, Rect, Scene, Text, svg_from


class SvgFromScene(unittest.TestCase):
    def test_each_primitive_emits_markup(self):
        scene = Scene(200, 100, "#fff", [
            Rect(0, 0, 10, 10, fill="#000"),
            Line(0, 0, 10, 0, stroke="#000"),
            Circle(5, 5, 3, fill="#f00"),
            Text(2, 8, "hi & <there>", size=7),
        ])
        svg = svg_from(scene)
        ET.fromstring(svg)                       # well-formed
        self.assertIn("<rect", svg); self.assertIn("<line", svg)
        self.assertIn("<circle", svg); self.assertIn("hi &amp; &lt;there&gt;", svg)
        self.assertEqual(svg.count("http://") + svg.count("https://"), 1)

    def test_dash_and_stroke_width_survive(self):
        svg = svg_from(Scene(50, 50, "#fff", [Line(0, 0, 5, 5, "#000", 2.0, (4, 4))]))
        self.assertIn("stroke-dasharray", svg)
        self.assertIn('stroke-width="2"', svg)

    def test_background_rect_is_emitted(self):
        svg = svg_from(Scene(50, 50, "#ffffff", []))
        self.assertIn('width="50"', svg)
        self.assertIn('height="50"', svg)
        self.assertIn('fill="#ffffff"', svg)

    def test_opacities_and_font_weight_are_emitted(self):
        scene = Scene(50, 50, "#fff", [
            Rect(1, 1, 8, 8, fill="#fff", fill_opacity=0.85),
            Line(0, 0, 5, 5, "#000", 1.0, (), opacity=0.5),
            Circle(5, 5, 3, fill="#f00", fill_opacity=0.42),
            Text(2, 8, "title", size=11, weight="600"),
        ])
        svg = svg_from(scene)
        ET.fromstring(svg)
        self.assertIn('fill-opacity="0.85"', svg)
        self.assertIn('stroke-opacity="0.5"', svg)
        self.assertIn('fill-opacity="0.42"', svg)
        self.assertIn('font-weight="600"', svg)

    def test_default_text_style_matches_the_chart(self):
        svg = svg_from(Scene(50, 50, "#fff", [Text(2, 8, "x")]))
        self.assertIn('fill="#111827"', svg)
        self.assertIn('font-size="11"', svg)
        self.assertNotIn("text-anchor", svg)      # "start" is the SVG default

    def test_unfilled_circle_is_drawn_as_an_outline(self):
        svg = svg_from(Scene(50, 50, "#fff", [Circle(5, 5, 3, stroke="#111827")]))
        self.assertIn('fill="none"', svg)
        self.assertIn('stroke="#111827"', svg)

    def test_titles_survive_as_tooltip_children(self):
        scene = Scene(50, 50, "#fff", [
            Circle(5, 5, 3, fill="#f00", title="a & b"),
            Text(2, 8, "\u25cf", title="c & d"),
        ])
        svg = svg_from(scene)
        root = ET.fromstring(svg)
        titled = [el for el in root.iter() if el.tag.endswith("title")]
        self.assertEqual([t.text for t in titled], ["a & b", "c & d"])
        self.assertIn("a &amp; b", svg)           # escaped in the markup

    def test_default_stroke_width_is_omitted_not_zeroed(self):
        svg = svg_from(Scene(50, 50, "#fff", [Line(0, 0, 5, 5, "#000", 1.0)]))
        ET.fromstring(svg)
        self.assertNotIn("stroke-width", svg)   # 1.0 is the SVG default


if __name__ == "__main__":
    unittest.main()
