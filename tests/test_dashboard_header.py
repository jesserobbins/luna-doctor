"""The dashboard's header and its status moons: the pure half of the rework.

Row one is where a dashboard answers its first two questions - how bad is it,
and is it getting worse - so this file holds it to the mockups in the design
brief (`.superpowers/sdd/2026-11-16-dashboard/design-brief.md` §2) verbatim:
each of the five states draws the header line the mockup draws, at the same 77
cells, with the clock right-aligned at the edge the rules stop at.

Three contracts are tested here, all of them pure functions of a metrics dict, a
capacity block and a window:

- the header word is a one-to-one reading of `report.status_moon`'s own glyph,
  and the trend word is `baseline.change_verdict`'s own word, so neither can
  hold a second opinion about a window;
- each region title wears the moon its own rows earn, and
  `regions_satisfy_invariant` is the tie §4.2 states between the conclusion and
  its causes;
- the mechanical copy rules (§7 rules 1 and 6, and the 78-cell budget) hold for
  every row the mockups draw, checked through `tui.dashboard_ok_row`.

Textual is imported by `tui` at the top, so this file skips when the extra is
not installed, exactly as the rest of the screen's suite does.
"""

import datetime
import time
import unittest

import pytest

pytest.importorskip("textual")

from rich.cells import cell_len

from luna_doctor import baseline, cli, report, sources, tui

# The mockups' own clock: 14:32Z on the day the fixtures were measured.
_CLOCK = datetime.datetime(2026, 11, 2, 14, 32, tzinfo=datetime.timezone.utc).timestamp()

# The account's capacity block, as cli._capacity_metrics builds it.
_APEX = {"provenance": "account", "plan": "apex", "priority_lanes": 12}
# The same payload with a null lane count: a real shape, and the one the
# no-capacity mockup's header draws (`apex · ? lanes`).
_APEX_NO_LANES = {"provenance": "account", "plan": "apex", "priority_lanes": None}


def _capacity(config):
    """The Capacity dataclass fetch_capacity builds, through the real parser.

    The header reads the plan and the lane count from this payload - the same
    object the LOAD rows read - so the fixture goes through the parser the fetch
    uses and a null stays a null, exactly as it does on the disk-cache path.
    """
    payload = {"success": True, "generated_at": "2026-11-02T14:31:00.000000000Z",
               "data": {"now": {"limit": 12, "in_use": 7, "available": 5,
                                "saturated": False, "ledger": "up",
                                "sampled_at": "2026-11-02T14:30:12.123456789Z"},
                        "config": config, "history": None, "admission": None}}
    fields, error = sources._parse_capacity_payload(payload)
    assert not error, error
    return sources.Capacity(fetched_at=time.time(), **fields)


_APEX_CAP = _capacity({"plan": "apex", "priority_lanes": 12})
_APEX_NO_LANES_CAP = _capacity({"plan": "apex", "priority_lanes": None})


def _window(capacity=_APEX, **over):
    """What analyse() produces, reduced to the keys the header reads."""
    metrics = {
        "window_hours": 1,
        "requests": 42,
        "by_outcome": {"completed": 39, "admission_timeout": 3},
        "by_tier": {"interactive": 39, "background": 3},
        "by_harness": {"pi": 42},
        "latency_s": {"interactive": {"n": 39, "p50": 95.0, "p90": 240.0, "max": 300.0}},
        "peak_concurrency": 17,
        "admission_conversion": {"interactive": {"completed": 39,
                                                 "admission_timeout": 3,
                                                 "admitted_pct": 92.9}},
        "saturated": 4,
        "starved_peer_evidence": 0,
        "pressure_windows": 0,
        "longest_pressure_window": None,
        "timeout_storms": [],
        "per_model_deadlines": {},
        "unknown_errors": [],
        "saturation_bands": {},
        "declared_plan": None,
        "by_minute": [],
        "capacity": capacity,
        "scanned_at": _CLOCK,
    }
    metrics.update(over)
    return metrics


def _previous(capacity=_APEX, **over):
    """The equal-length window before this one: quiet, and wide enough to judge."""
    return _window(capacity, requests=40,
                   by_outcome={"completed": 40, "admission_timeout": 0},
                   peak_concurrency=12, **over)


# A clean window's predecessor: the same traffic, one peak lower.
_HEALTHY_PREVIOUS = _window(requests=9, by_outcome={"completed": 9},
                            peak_concurrency=3)


def _healthy(**over):
    """A clean window, and its quiet predecessor."""
    metrics = _window(requests=9, by_outcome={"completed": 9}, peak_concurrency=4)
    metrics.update(over)
    return metrics


# ---- the five states, as the mockups draw them ------------------------------
# The header's right-hand cell is now the LIVE age of the scan, not a static
# `14:32Z`: a clock the reader has to subtract from cannot answer "is this
# current?" at a glance, and the absolute sample minute survives on the `now`
# row (`sampled HH:MM±HH:MM`). A plain call names no wall clock, so the age a
# header carries without one is the age of a scan that has just closed.
#
# §2.1 (and §2.4, which reaches the same conclusion by a different group of
# rows): the owner's case.
_OVER_CAPACITY_HEADER = (
    "🌑 OVER CAPACITY · worse ▲ · 1h · 42 req · apex · ●12 lanes            age 0s")
# §2.2: nothing blocked, nothing over, and both windows the same size.
_HEALTHY_HEADER = (
    "🌕 OK · unchanged ▬ · 1h · 9 req · apex · ●12 lanes                    age 0s")
# §2.3: the honest degradation - a plan is known and the lane count is not.
_NO_CAPACITY_HEADER = (
    "🌗 PRESSURE · worse ▲ · 1h · 42 req · apex · ? lanes                   age 0s")
# §2.5: nothing measured at all, so no trend is claimed either.
_NO_TRAFFIC_HEADER = (
    "🌙 NOT ESTABLISHED · 1h · 0 req · apex · ●12 lanes                     age 0s")

def _local(stamp):
    """`HH:MM±HH:MM` for one UTC stamp, on the clock of the machine running the suite.

    The screen prints local time with the offset beside it, so a fixture row that
    carries a sample minute is derived the same way instead of being pinned to one
    developer's zone.
    """
    return tui.local_hhmm(tui._stamp_epoch(stamp))


# The mockup rows, as printed (leading spaces included): the copy rules are
# measured in terminal cells, so the strings are the ones a reader sees. The now
# row models both pools and the local sample minute, which is what the screen
# draws; the UTC one-pool row is retired.
_MOCKUP_ROWS = [
    " now          ●     7/12   · background 0/12 · sampled %s"
    % _local("2026-11-02T14:20Z"),
    " peak 1h      ○       17 ▲ 5 over ●12 by 5",
    " blocked      ○        3 ▲ 3 0 × 429",
    " █ = at or over ●12 lanes · over 7m of 60 · peak at %s"
    % _local("2026-11-02T14:07Z"),
    " p50 pri      ○      95s ▲ 12",
    " p90 pri      ○     240s ▲ 38",
    " p50 bg       ?            ·",
    " p90 bg       ?            ·",
    " now          ?            · no capacity cached",
    " peak 1h      ○       17 ▲ 5 lane count unknown",
    " peak 1h      ?            · nothing measured here",
    " blocked      ?            ·",
    " 429          ○       11 ▲ 11 the account's rate limit",
    " at limit     ○        4 ▲  4 account pool full",
    " background   ○        7 ▲  7 sent to the slow tier",
    "NEXT  Hold 12 in flight · send the rest to -background",
    "→ luna-doctor fix --since 1h                                              [f]",
    "● account · ○ this machine (a lower bound) · ? unknown",
    "🌕 ok · 🌗 pressure · 🌑 over capacity",
]

# The regions' rows, per mockup, as (badge, text) pairs.
_LOAD_OVER = [("●", "now          7/12   · background 0/12 · sampled %s"
               % _local("2026-11-02T14:20Z")),
              ("○", "peak 1h       17 ▲ 5 over ●12 by 5"),
              ("○", "blocked        3 ▲ 3 0 × 429")]
# The latency group's four rows: one per tier and percentile. This window sent
# nothing to the slow tier, so its two rows print `?` - the ordinary shape of a
# background tier nobody used - and the priority rows carry the move.
_LATENCY_OVER = [("○", "p50 pri       95s ▲ 12"),
                 ("○", "p90 pri      240s ▲ 38"),
                 ("?", "p50 bg           ·"),
                 ("?", "p90 bg           ·")]
_LOAD_HEALTHY = [("●", "now          3/12   · background 1/12 · sampled %s"
                 % _local("2026-11-02T14:31Z")),
                 ("○", "peak 1h        4 ▲ 1 never over"),
                 ("○", "blocked        0   · 0 × 429")]
_LATENCY_HEALTHY = [("○", "p50 pri       31s ▾ 4"),
                    ("○", "p90 pri       58s ▾ 9"),
                    ("?", "p50 bg           ·"),
                    ("?", "p90 bg           ·")]
_LOAD_NOTHING_MEASURED = [("●", "now          4/12   · background 0/12 · sampled %s"
                          % _local("2026-11-02T14:31Z")),
                          ("?", "peak 1h          · nothing measured here"),
                          ("?", "blocked          ·")]
_LATENCY_NOTHING_MEASURED = [("?", "p50 pri          ·"),
                             ("?", "p90 pri          ·"),
                             ("?", "p50 bg           ·"),
                             ("?", "p90 bg           ·")]
_REFUSALS_ROWS = [("○", "429            11 ▲ 11 the account's rate limit"),
                  ("○", "at limit        4 ▲  4 account pool full"),
                  ("○", "background      7 ▲  7 sent to the slow tier")]


# ---- the header line -------------------------------------------------------
class TheHeaderLine(unittest.TestCase):
    """Row one, against the five states the brief draws."""

    def test_the_over_capacity_header_is_the_mockup(self):
        self.assertEqual(_OVER_CAPACITY_HEADER,
                         tui.header_line(_window(), _previous(), _APEX_CAP, 1))

    def test_the_healthy_header_is_the_mockup(self):
        self.assertEqual(_HEALTHY_HEADER,
                         tui.header_line(_healthy(), _HEALTHY_PREVIOUS, _APEX_CAP, 1))

    def test_the_no_capacity_header_is_the_mockup(self):
        # The plan is known and the lane count is not: `apex · ? lanes`.
        metrics = _window(_APEX_NO_LANES,
                          by_outcome={"completed": 31, "rate_limited": 11})
        self.assertEqual(_NO_CAPACITY_HEADER,
                         tui.header_line(metrics, _previous(), _APEX_NO_LANES_CAP, 1))

    def test_the_no_traffic_header_is_the_mockup(self):
        metrics = _window(requests=0, by_outcome={})
        self.assertEqual(_NO_TRAFFIC_HEADER,
                         tui.header_line(metrics, None, _APEX_CAP, 1))

    def test_the_refusals_state_reaches_the_same_conclusion(self):
        """§2.4 changes the evidence group, not the header: the conclusion holds."""
        metrics = _window(by_outcome={"completed": 28, "admission_timeout": 3,
                                      "rate_limited": 11})
        self.assertEqual(_OVER_CAPACITY_HEADER,
                         tui.header_line(metrics, _previous(), _APEX_CAP, 1))

    def test_every_mockup_header_ends_at_column_77(self):
        for line in (_OVER_CAPACITY_HEADER, _HEALTHY_HEADER, _NO_CAPACITY_HEADER,
                     _NO_TRAFFIC_HEADER):
            self.assertEqual(77, cell_len(line))

    def test_the_age_is_the_scans_own_reading_and_the_wall_clock_given(self):
        """The header holds two numbers the screen already has: the scan's clock
        and the moment the row is drawn. Neither is a second reading of time."""
        earlier = _window(scanned_at=_CLOCK - 3600)
        self.assertTrue(tui.header_line(earlier, _previous(), _APEX_CAP, 1,
                                        _CLOCK - 3600 + 120).endswith("age 2m"))
        self.assertTrue(tui.header_line(earlier, _previous(), _APEX_CAP, 1,
                                        _CLOCK + 600).endswith("age 70m stale"))

    def test_a_scan_with_no_clock_prints_no_timestamp(self):
        # A header is a pure function of its arguments: an unclocked scan loses
        # the cell rather than taking a second, later reading of the wall clock.
        line = tui.header_line(_window(scanned_at=None), _previous(), _APEX_CAP, 1,
                               _CLOCK)
        self.assertEqual("🌑 OVER CAPACITY · worse ▲ · 1h · 42 req · apex · ●12 lanes",
                         line)

    def test_the_window_cell_is_the_window_the_parameter_names(self):
        for hours, cell in ((6, " · 6h · "), (24, " · 24h · ")):
            self.assertIn(cell, tui.header_line(_healthy(), _HEALTHY_PREVIOUS,
                                                _APEX_CAP, hours))

    def test_a_declared_width_wears_the_declaration_badge(self):
        # ◌ is a person's declaration, never a measurement (§4.1).
        metrics = _window(_APEX_NO_LANES, declared_plan="12")
        line = tui.header_line(metrics, None, _APEX_NO_LANES_CAP, 1)
        self.assertIn(" · ◌12 lanes", line)

    def test_a_window_with_no_plan_and_no_width_says_so(self):
        metrics = _window({"provenance": "unknown"})
        line = tui.header_line(metrics, None, None, 1)
        self.assertIn(" · ? plan · ? lanes", line)


# ---- the status words ------------------------------------------------------
class StatusWords(unittest.TestCase):
    """Four glyph names, four words, no fifth state and no synonym."""

    def test_the_four_moons_are_the_four_words(self):
        self.assertEqual("OK", tui.status_word(report.MOON_FULL))
        self.assertEqual("PRESSURE", tui.status_word(report.MOON_QUARTER))
        self.assertEqual("OVER CAPACITY", tui.status_word(report.MOON_NEW))
        self.assertEqual("NOT ESTABLISHED", tui.status_word("moon"))

    def test_the_severity_vocabulary_is_closed(self):
        self.assertEqual(("OK", "PRESSURE", "OVER CAPACITY", "NOT ESTABLISHED"),
                         tui.SEVERITY_WORDS)
        for banned in ("fine", "busy", "degraded", "bad", "healthy"):
            self.assertNotIn(banned, tui.SEVERITY_WORDS)

    def test_a_name_outside_the_table_claims_no_status(self):
        self.assertEqual("NOT ESTABLISHED", tui.status_word("something else"))

    def test_the_header_moon_is_the_cli_verdicts_moon(self):
        for metrics in (_window(),
                        _healthy(),
                        _window(by_outcome={"completed": 31, "rate_limited": 11}),
                        _window(requests=0, by_outcome={})):
            headline, _adjustments, exit_code = cli.verdict(metrics)
            self.assertEqual(report.status_moon(exit_code, headline),
                             tui.header_moon(metrics))

    def test_the_plain_moon_claims_no_phase(self):
        # `moon` is report's name for "no status", so it is not a phase, and a
        # surface that draws it prints no legend for a scale it did not use.
        self.assertNotIn("moon", report.MOON_PHASES)
        self.assertEqual("🌙", tui.moon_glyph("moon"))


# ---- the trend word --------------------------------------------------------
class TrendWords(unittest.TestCase):
    """The direction, from baseline's own comparison - never a second one."""

    def test_the_trend_vocabulary_is_baselines_four(self):
        self.assertEqual(("better", "worse", "unchanged", "inconclusive"),
                         tui.TREND_WORDS)
        for before, after in ((_previous(), _window()),
                              (_window(), _previous()),
                              (_healthy(), _healthy()),
                              (_window(requests=2), _window(requests=3))):
            self.assertIn(baseline.change_verdict(after, before)[0], tui.TREND_WORDS)

    def test_the_arrow_table_covers_every_trend_word(self):
        self.assertEqual(set(tui.TREND_WORDS), set(tui.TREND_ARROWS))

    def test_each_direction_word_draws_its_own_arrow(self):
        self.assertEqual("worse ▲", tui.trend_cell(_window(), _previous()))
        self.assertEqual("unchanged ▬", tui.trend_cell(_healthy(), _HEALTHY_PREVIOUS))
        # A window that stopped refusing reads better, and points down.
        refused = _window(by_outcome={"completed": 28, "rate_limited": 11})
        self.assertEqual("better ▾", tui.trend_cell(_previous(), refused))

    def test_the_failed_comparison_draws_no_arrow(self):
        quiet = _window(requests=0, by_outcome={})
        self.assertEqual("", tui.trend_cell(quiet, None))
        self.assertEqual("", tui.TREND_ARROWS["inconclusive"])

    def test_a_missing_previous_window_is_inconclusive_not_unchanged(self):
        # `unchanged` would claim two windows were measured; only one was.
        self.assertEqual("inconclusive", tui.trend_word(_healthy(), None))

    def test_a_previous_window_under_the_floor_is_inconclusive(self):
        thin = _window(requests=3, by_outcome={"completed": 3})
        self.assertEqual("inconclusive", tui.trend_word(_healthy(), thin))


# ---- the region moons ------------------------------------------------------
class RegionMoons(unittest.TestCase):
    """Each region title wears the moon its own rows earn (§4.2)."""

    def test_an_admission_block_darkens_the_region(self):
        self.assertEqual(tui.moon_glyph(report.MOON_NEW),
                         tui.region_moon(_LOAD_OVER, tui.REGION_LOAD))

    def test_a_peak_over_the_lanes_reads_pressure(self):
        rows = [("○", "peak 1h       17 ▲ 5 over ●12 by 5"),
                ("○", "blocked        0   · 0 × 429")]
        self.assertEqual(tui.moon_glyph(report.MOON_QUARTER),
                         tui.region_moon(rows, tui.REGION_LOAD))

    def test_a_peak_with_no_lane_count_cannot_read_clean(self):
        rows = [("○", "peak 1h       17 ▲ 5 lane count unknown"),
                ("○", "blocked        0   · 0 × 429")]
        self.assertEqual(tui.moon_glyph(report.MOON_QUARTER),
                         tui.region_moon(rows, tui.REGION_LOAD))

    def test_a_peak_that_never_went_over_reads_ok_even_when_it_moved(self):
        self.assertEqual(tui.moon_glyph(report.MOON_FULL),
                         tui.region_moon(_LOAD_HEALTHY, tui.REGION_LOAD))

    def test_a_refusal_count_above_zero_is_over_capacity(self):
        """The REFUSALS region's own rows are refusals, so their count decides.

        §4.2's rule is that the darkest moon means work that never ran, and the
        region holding the refusals has to be able to say it: the header can be
        dark because the gateway refused the work, with no admission block in the
        LOAD rows to explain it. A refusal that did not happen cannot darken it.
        """
        self.assertEqual(tui.moon_glyph(report.MOON_NEW),
                         tui.region_moon(_REFUSALS_ROWS, tui.REGION_REFUSALS))
        self.assertEqual(tui.moon_glyph(report.MOON_NEW),
                         tui.region_moon([("○", "429           999   · the account's rate limit"),
                                          ("○", "at limit        0   · ·"),
                                          ("○", "background      0   · ·")],
                                         tui.REGION_REFUSALS))
        self.assertEqual(tui.moon_glyph(report.MOON_FULL),
                         tui.region_moon([("○", "429            0   · nothing refused"),
                                          ("○", "at limit        0   · ·"),
                                          ("○", "background      0   · ·")],
                                         tui.REGION_REFUSALS))

    def test_a_refusal_row_alone_does_not_darken_another_region(self):
        """The rule is the region's subject, not a label the moon hunts for."""
        refused = [("○", "429            11   · the account's rate limit")]
        self.assertEqual(tui.moon_glyph(report.MOON_FULL),
                         tui.region_moon(refused + _LATENCY_HEALTHY,
                                         tui.REGION_LATENCY))

    def test_a_latency_moving_up_reads_pressure(self):
        self.assertEqual(tui.moon_glyph(report.MOON_QUARTER),
                         tui.region_moon(_LATENCY_OVER, tui.REGION_LATENCY))

    def test_a_latency_moving_down_reads_ok(self):
        self.assertEqual(tui.moon_glyph(report.MOON_FULL),
                         tui.region_moon(_LATENCY_HEALTHY, tui.REGION_LATENCY))

    def test_nothing_measured_claims_no_status(self):
        for rows, kind in ((_LOAD_NOTHING_MEASURED, tui.REGION_LOAD),
                           (_LATENCY_NOTHING_MEASURED, tui.REGION_LATENCY)):
            self.assertEqual(tui.moon_glyph("moon"), tui.region_moon(rows, kind))

    def test_a_context_row_cannot_move_a_moon(self):
        # `now` has no previous window to move against, and a row whose label the
        # screen cannot read - the retired `gateway` constant is one - carries no
        # direction at all. Neither is evidence a region can be judged on.
        measured = tui.moon_glyph(report.MOON_FULL)
        self.assertEqual(measured,
                         tui.region_moon(_LATENCY_HEALTHY, tui.REGION_LATENCY))
        # The same moon from the two rows that measured something, and from those
        # rows with the two context rows a taller layout would hand the region.
        self.assertEqual(measured,
                         tui.region_moon(_LATENCY_HEALTHY[:2], tui.REGION_LATENCY))
        self.assertEqual(
            measured,
            tui.region_moon([("●", "now          12/12   · background 0/12 "
                             "· sampled %s" % _local("2026-11-02T14:31Z")),
                             ("○", "gateway       25s   · fixed wait before headers")]
                            + _LATENCY_HEALTHY, tui.REGION_LATENCY))

    def test_a_partly_measured_region_reads_on_what_it_measured(self):
        # One unread percentile does not make the region unknown: it has a
        # measurement that moved, and that is what the moon reports.
        rows = [("○", "p50 pri       95s ▲ 12"),
                ("?", "p90 pri          ·"),
                ("?", "p50 bg           ·"),
                ("?", "p90 bg           ·")]
        self.assertEqual(tui.moon_glyph(report.MOON_QUARTER),
                         tui.region_moon(rows, tui.REGION_LATENCY))
        clean = [("○", "p50 pri       31s ▾ 4"),
                 ("?", "p90 pri          ·"),
                 ("?", "p50 bg           ·"),
                 ("?", "p90 bg           ·")]
        self.assertEqual(tui.moon_glyph(report.MOON_FULL),
                         tui.region_moon(clean, tui.REGION_LATENCY))

    def test_a_background_row_alone_can_read_pressure(self):
        # The tiers are reported independently and read the same way: a slow tier
        # whose p90 rose is the latency group's own pressure, even when the
        # priority rows did not move.
        rows = [("○", "p50 pri       83s   ▬"),
                ("○", "p90 pri      202s   ▬"),
                ("○", "p50 bg       430s ▲ 50"),
                ("○", "p90 bg       560s ▲ 90")]
        self.assertEqual(tui.moon_glyph(report.MOON_QUARTER),
                         tui.region_moon(rows, tui.REGION_LATENCY))

    def test_a_row_with_no_known_label_is_context(self):
        rows = [("○", "▁▁▂▁▁▂▂▂▁▁▁▁▂▁▁▂▂▂▁▁▃▄▆███████▆▅"),
                ("○", "peak 1h        4 ▲ 1 never over")]
        self.assertEqual(tui.moon_glyph(report.MOON_FULL),
                         tui.region_moon(rows, tui.REGION_LOAD))

    def test_an_unknown_region_kind_is_a_key_error(self):
        with self.assertRaises(KeyError):
            tui.region_moon(_LOAD_OVER, "overview")

    def test_the_over_capacity_mockup_wears_a_dark_load_and_a_pressured_latency(self):
        # §2.1: `LOAD 🌑` / `LATENCY 🌗` - the reader sees which dimension is dark.
        self.assertEqual(tui.moon_glyph(report.MOON_NEW),
                         tui.region_moon(_LOAD_OVER, tui.REGION_LOAD))
        self.assertEqual(tui.moon_glyph(report.MOON_QUARTER),
                         tui.region_moon(_LATENCY_OVER, tui.REGION_LATENCY))

    def test_the_healthy_mockup_wears_two_full_moons(self):
        self.assertEqual(tui.moon_glyph(report.MOON_FULL),
                         tui.region_moon(_LOAD_HEALTHY, tui.REGION_LOAD))
        self.assertEqual(tui.moon_glyph(report.MOON_FULL),
                         tui.region_moon(_LATENCY_HEALTHY, tui.REGION_LATENCY))

    def test_every_region_kind_is_named(self):
        self.assertEqual(("load", "latency", "models", "refusals", "stalls"),
                         tui.REGION_KINDS)


# ---- the tie between the two levels ---------------------------------------
class TheTwoLevelInvariant(unittest.TestCase):
    """§4.2: the regions are the causes, the header is the conclusion."""

    def test_the_mockups_satisfy_it(self):
        states = [
            (tui.header_moon(_window()),
             [_LOAD_OVER, _LATENCY_OVER],
             [tui.REGION_LOAD, tui.REGION_LATENCY]),
            (tui.header_moon(_healthy()),
             [_LOAD_HEALTHY, _LATENCY_HEALTHY],
             [tui.REGION_LOAD, tui.REGION_LATENCY]),
            (tui.header_moon(_window(requests=0, by_outcome={})),
             [_LOAD_NOTHING_MEASURED, _LATENCY_NOTHING_MEASURED],
             [tui.REGION_LOAD, tui.REGION_LATENCY]),
            (tui.header_moon(_window(by_outcome={"completed": 31, "rate_limited": 11})),
             [_LOAD_OVER, _REFUSALS_ROWS],
             [tui.REGION_LOAD, tui.REGION_REFUSALS]),
        ]
        for name, regions, kinds in states:
            glyphs = [tui.region_moon(rows, kind) for rows, kind in zip(regions, kinds)]
            self.assertTrue(tui.regions_satisfy_invariant(tui.moon_glyph(name), glyphs),
                            "%s: %s" % (name, glyphs))

    def test_a_dark_header_needs_a_dark_region(self):
        self.assertFalse(tui.regions_satisfy_invariant(
            tui.moon_glyph(report.MOON_NEW),
            [tui.moon_glyph(report.MOON_QUARTER), tui.moon_glyph("moon")]))

    def test_a_clean_header_admits_no_region_that_claims_trouble(self):
        self.assertFalse(tui.regions_satisfy_invariant(
            tui.moon_glyph(report.MOON_FULL),
            [tui.moon_glyph(report.MOON_FULL), tui.moon_glyph(report.MOON_QUARTER)]))
        self.assertTrue(tui.regions_satisfy_invariant(
            tui.moon_glyph(report.MOON_FULL),
            [tui.moon_glyph(report.MOON_FULL), tui.moon_glyph(report.MOON_FULL)]))

    def test_a_pressure_header_constrains_nothing(self):
        # A window can be under pressure for a reason no region here measures.
        for glyph in tui.moon_glyph(report.MOON_FULL), tui.moon_glyph("moon"):
            self.assertTrue(tui.regions_satisfy_invariant(
                tui.moon_glyph(report.MOON_QUARTER), [glyph]))

    def test_the_plain_header_constrains_nothing(self):
        self.assertTrue(tui.regions_satisfy_invariant(
            tui.moon_glyph("moon"), [tui.moon_glyph(report.MOON_NEW)]))


# ---- the copy rules --------------------------------------------------------
class TheCopyRules(unittest.TestCase):
    """Rules 1 and 6 and the 78-cell budget, as far as a helper can hold them."""

    def test_every_mockup_header_passes(self):
        for line in (_OVER_CAPACITY_HEADER, _HEALTHY_HEADER, _NO_CAPACITY_HEADER,
                     _NO_TRAFFIC_HEADER):
            self.assertTrue(tui.dashboard_ok_row(line), line)

    def test_the_generated_headers_pass_the_lint(self):
        states = [(_window(), _previous(), _APEX_CAP),
                  (_healthy(), _HEALTHY_PREVIOUS, _APEX_CAP),
                  (_window(_APEX_NO_LANES,
                           by_outcome={"completed": 31, "rate_limited": 11}),
                   _previous(), _APEX_NO_LANES_CAP),
                  (_window(requests=0, by_outcome={}), None, _APEX_CAP)]
        for metrics, previous, cap in states:
            line = tui.header_line(metrics, previous, cap, 1)
            self.assertTrue(tui.dashboard_ok_row(line), line)

    def test_every_mockup_data_row_passes(self):
        for row in _MOCKUP_ROWS:
            self.assertTrue(tui.dashboard_ok_row(row), row)

    def test_a_row_over_the_budget_is_rejected(self):
        self.assertFalse(tui.dashboard_ok_row("x" * 79))
        self.assertTrue(tui.dashboard_ok_row("x" * 78))

    def test_the_budget_is_measured_in_cells_not_code_points(self):
        # A phase moon is two cells, so 20 moons and 38 other glyphs is 78 cells
        # in 58 code points - a row that measures 79 cells is over the budget.
        self.assertTrue(tui.dashboard_ok_row("🌑" * 20 + "x" * 38))
        self.assertFalse(tui.dashboard_ok_row("🌑" * 20 + "x" * 39))

    def test_a_sentence_in_a_row_is_rejected(self):
        self.assertFalse(tui.dashboard_ok_row(
            "🌑 Concurrency pressure across 17 requests: 3 were blocked."))
        self.assertFalse(tui.dashboard_ok_row("blocked 3 now!"))

    def test_a_question_mark_ending_a_row_is_rejected(self):
        self.assertFalse(tui.dashboard_ok_row("is anything over capacity?"))

    def test_the_unknown_glyph_is_not_a_question(self):
        self.assertTrue(tui.dashboard_ok_row("apex · ? lanes"))
        self.assertTrue(tui.dashboard_ok_row("peak 1h ? · nothing measured here"))

    def test_a_field_name_parenthetical_is_rejected(self):
        self.assertFalse(tui.dashboard_ok_row(
            "blocked 3 (`scheduler admission timeout`)"))
        self.assertFalse(tui.dashboard_ok_row("blocked 3 (rate_limit_error)"))

    def test_a_plain_parenthetical_is_allowed(self):
        self.assertTrue(tui.dashboard_ok_row(
            "● account · ○ this machine (a lower bound) · ? unknown"))

    def test_the_severity_and_trend_words_are_the_closed_sets(self):
        for word in tui.SEVERITY_WORDS:
            self.assertTrue(tui.dashboard_ok_row(word), word)
        for word in tui.TREND_WORDS:
            self.assertTrue(tui.dashboard_ok_row(word), word)
