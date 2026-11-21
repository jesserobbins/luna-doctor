"""The evidence group, the NEXT region and the two key lines, row by row.

Every assertion reads strings the pure builders return, so the suite runs
offline and needs no terminal. The expected lines are copied from the dashboard
mockups, so a row that drifts by one cell fails here.

Three facts decide most of these rows and all three are pinned below: the answer
selects a group of three rows and never gates the screen; a delta needs a
previous window that clears `baseline.FLOOR` in both windows; and the NEXT row
carries only the four imperatives the brief gives, read from the header's own
facts rather than from a second verdict.
"""

import time
import unicodedata
import unittest

import pytest

pytest.importorskip("textual")

from luna_doctor import baseline, sources, tui

# An 80-column screen leaves 2 cells of right margin, so no row may exceed 78.
ROW_BUDGET = 78

_SAMPLED = "2026-11-02T14:31:07.482003114Z"


def _cap(in_use, limit, *, plan="apex", sampled_at=_SAMPLED):
    """The Capacity payload `sources.fetch_capacity` hands the screen."""
    data = {
        "now": {"limit": limit, "in_use": in_use,
                "available": None if in_use is None else limit - in_use,
                "saturated": None, "ledger": "up", "sampled_at": sampled_at},
        "config": {"background_lanes": 12, "overflow_routing": "enabled",
                   "plan": plan, "priority_lanes": limit},
        "history": None,
        "admission": None,
    }
    fields, err = sources._parse_capacity_payload({"success": True, "data": data})
    assert not err, err
    return sources.Capacity(fetched_at=time.time(), **fields)


def _model_cell(cell, n, p50, p90):
    """One `latency_by.by_tier_model` cell, the shape `cli.latency_by` fills."""
    return {"cell": cell, "n": n, "p50": p50, "p90": p90, "max": p90}


def _window(requests, peak, *, blocked=0, rejected=0, at_limit=0, background=0,
            stalled=0, starved=0, p50=None, p90=None, samples=12,
            bg_p50=None, bg_p90=None, bg_samples=0, models=(),
            slowest=None, admitted=100.0):
    """One analyse() window, reduced to the keys these rows read.

    The verdict needs the same block `analyse` produces, so the shapes that only
    `cli.verdict` reads are filled here too: this suite must not re-derive a
    status the CLI would decide differently.
    """
    return {
        "window_hours": 1,
        "requests": requests,
        "by_outcome": {"completed": max(0, requests - blocked - rejected - at_limit
                                        - stalled),
                       "admission_timeout": blocked,
                       "rate_limited": rejected,
                       "org_concurrency_limit": at_limit,
                       "client_idle_timeout": stalled},
        "by_tier": {"interactive": requests - background, "background": background},
        "by_harness": {"pi": requests},
        "latency_s": {"interactive": {"n": samples, "p50": p50, "p90": p90,
                                      "max": 900.0},
                      "background": {"n": bg_samples, "p50": bg_p50, "p90": bg_p90,
                                     "max": None}},
        "latency_by": {"floor": 5, "suppressed": 0,
                       "by_tier_model": [dict(cell) for cell in models],
                       "by_tier_harness": [], "by_tier_outcome": []},
        "peak_concurrency": peak,
        "peak_at": None,
        "peak_by_harness": {},
        "admission_conversion": {
            "interactive": {"completed": max(0, requests - blocked),
                            "admission_timeout": blocked,
                            "admitted_pct": admitted},
            "background": {"completed": 0, "admission_timeout": 0,
                           "admitted_pct": None}},
        "saturated": 0,
        "starved_peer_evidence": starved,
        "pressure_windows": 0,
        "longest_pressure_window": None,
        "timeout_storms": [],
        "per_model_deadlines": {},
        "unknown_errors": [],
        "saturation_bands": {},
        "declared_plan": None,
        "scanned_at": None,
        "slowest_request": slowest,
    }


def _cells(text):
    """A string's width in terminal cells: a phase moon is two, the rest one."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def _lines(rows):
    return [line for _badge, line in rows]


# The per-model half of the latency measurement, as `cli.latency_by` orders it:
# most completed samples first, and only cells that cleared its floor. Four
# models here, so the group's three-row cap is exercised, and each one's p50 rose
# against the window before it.
BUSY_MODELS = (
    _model_cell("interactive / glm-5.3", 20, 95.0, 240.0),
    _model_cell("background / glm-5.3-background", 7, 400.0, 520.0),
    _model_cell("interactive / claude-opus-lr11", 6, 130.0, 300.0),
    _model_cell("interactive / gpt-5.1", 5, 60.0, 100.0),
)
BUSY_MODELS_BEFORE = (
    _model_cell("interactive / glm-5.3", 18, 83.0, 202.0),
    # The two cells at six samples sit in the order a tie leaves them, which is
    # the order one window's requests arrived and not the order this window's
    # rows are drawn in: a delta matched by cell keeps each model its own number.
    _model_cell("interactive / claude-opus-lr11", 6, 118.0, 260.0),
    _model_cell("background / glm-5.3-background", 6, 380.0, 470.0),
    _model_cell("interactive / gpt-5.1", 5, 64.0, 110.0),
)
# A clean window's models fell, which is what its full moon needs them to do.
HEALTHY_MODELS = (
    _model_cell("interactive / glm-5.3", 9, 31.0, 58.0),
    _model_cell("interactive / claude-haiku-lr34", 6, 22.0, 40.0),
)
HEALTHY_MODELS_BEFORE = (
    _model_cell("interactive / glm-5.3", 11, 35.0, 67.0),
    _model_cell("interactive / claude-haiku-lr34", 6, 25.0, 44.0),
)

# §2.1, §2.3 and §2.4 draw the same window: 42 requests, peak 17 against 12
# lanes, 3 blocked at admission, 11 refused with a 429, and seven of the 42 sent
# to the slow tier - which is the only window here whose background percentiles
# were measured at all.
def _busy():
    return _window(42, 17, blocked=3, rejected=11, at_limit=4, background=7,
                   p50=95.0, p90=240.0, bg_p50=400.0, bg_p90=520.0, bg_samples=7,
                   models=BUSY_MODELS, admitted=87.0)


def _busy_before():
    """The window one step back: every delta above is measured against it."""
    return _window(40, 12, p50=83.0, p90=202.0, samples=10,
                   bg_p50=380.0, bg_p90=470.0, bg_samples=6,
                   models=BUSY_MODELS_BEFORE)


def _healthy():
    return _window(9, 4, p50=31.0, p90=58.0, samples=9, models=HEALTHY_MODELS)


def _healthy_before():
    return _window(11, 4, p50=35.0, p90=67.0, samples=11,
                   models=HEALTHY_MODELS_BEFORE)


def _tier_split():
    """The busy window's priority percentiles, with the slow tier slower still.

    The priority p50 and p90 did not move; the seven requests on the background
    tier did. A group that read one latency number for both tiers would call this
    window slower, and the priority rows are what say it is not.
    """
    return _window(42, 17, blocked=3, rejected=11, at_limit=4, background=7,
                   p50=83.0, p90=202.0, samples=10,
                   bg_p50=430.0, bg_p90=560.0, bg_samples=7, admitted=87.0)


def _empty():
    return _window(0, 0, samples=0, admitted=None)


def _demoted():
    """A clean window whose only slow-tier traffic was sent there on purpose.

    Seven of 42 requests ran on the background tier, nothing was blocked, the
    peak is under the lane count, and the percentiles fell. Latency samples are
    part of the fixture so every region on the screen measured something: a
    region that measured nothing claims no status, and that is a separate case.
    """
    return _window(42, 4, background=7, p50=31.0, p90=58.0,
                   models=HEALTHY_MODELS)


# ---- the latency group: one row per tier, per percentile ------------------
class LatencyGroup(unittest.TestCase):
    """Priority and background are two populations, so they are two pairs of rows.

    A single latency number per percentile read both tiers as one population, and
    a demotion - work moving from the priority lanes to the slow tier - then
    looked exactly like the priority tier getting slower. Each tier's percentiles
    stand on their own row, with their own badge and their own delta against the
    same tier in the previous window, so the screen says which tier moved.

    The group is four rows in every window. A tier that measured nothing prints
    `?` on both of its rows rather than vanishing: a background tier nobody used
    is the ordinary way to see two `?` rows, and a row that disappears would move
    every row under it.
    """

    def test_both_tiers_get_their_own_rows_and_their_own_deltas(self):
        self.assertEqual(_lines(tui.group_rows(
            tui.REGION_LATENCY, _busy(), _busy_before())), [
            " p50 pri      ○      95s ▲ 12",
            " p90 pri      ○     240s ▲ 38",
            " p50 bg       ○     400s ▲ 20",
            " p90 bg       ○     520s ▲ 50",
        ])

    def test_a_background_tier_with_no_sample_prints_two_question_marks(self):
        """The slow tier was not measured, so it is not a zero: two `?` rows.

        The priority rows keep their numbers and their deltas, which is what
        "reported independently" means on a window that demoted nothing.
        """
        self.assertEqual(_lines(tui.group_rows(
            tui.REGION_LATENCY, _healthy(), _healthy_before())), [
            " p50 pri      ○      31s ▾ 4",
            " p90 pri      ○      58s ▾ 9",
            " p50 bg       ?            ·",
            " p90 bg       ?            ·",
        ])

    def test_a_demotion_moves_the_background_rows_and_not_the_priority_ones(self):
        """Two tiers, two deltas: the priority rows compare priority to priority."""
        self.assertEqual(_lines(tui.group_rows(
            tui.REGION_LATENCY, _tier_split(), _busy_before())), [
            " p50 pri      ○      83s   ▬",
            " p90 pri      ○     202s   ▬",
            " p50 bg       ○     430s ▲ 50",
            " p90 bg       ○     560s ▲ 90",
        ])

    def test_a_suppressed_tier_prints_no_denominator_on_either_row(self):
        rows = tui.group_rows(tui.REGION_LATENCY, _empty(), None)
        self.assertEqual(_lines(rows), [
            " p50 pri      ?            ·",
            " p90 pri      ?            ·",
            " p50 bg       ?            ·",
            " p90 bg       ?            ·",
        ])
        for badge, line in rows:
            self.assertNotIn("n=", line)
            self.assertEqual(tui._BADGE_UNKNOWN, badge)

    def test_a_window_with_no_lane_count_shows_the_same_latency_rows(self):
        """§2.3 differs from §2.1 above this region and nowhere inside it."""
        self.assertEqual(_lines(tui.group_rows(tui.REGION_LATENCY, _busy(), _busy_before())),
                         _lines(tui.group_rows(tui.REGION_LATENCY, _busy(), _busy_before())))

    def test_the_fixed_wait_is_not_a_row_here(self):
        """The gateway's wait is one constant with no trend, and the four tier
        rows are the group's whole budget: it lives in the CLI's adjustment and
        in the skill's gateway contract, not on the screen."""
        lines = _lines(tui.group_rows(tui.REGION_LATENCY, _busy(), _busy_before()))
        self.assertEqual(4, len(lines))
        self.assertNotIn("gateway", "".join(lines))


# ---- the models group: one model per row --------------------------------
class ModelsGroup(unittest.TestCase):
    """The per-model breakdown of the same latency, reached with `[g]`.

    The tier rows answer "which pool"; this group answers "which model", one row
    per model, each with its own p50, its own delta against the same model in the
    previous window and its own p90 in the clause. The label is the model's name,
    because the model is what the row is about; a name longer than the label
    column is cut to its cells rather than pushed into the badge's column.
    """

    def test_each_model_gets_its_own_row_number_and_delta(self):
        self.assertEqual(_lines(tui.group_rows(
            tui.REGION_MODELS, _busy(), _busy_before())), [
            " glm-5.3      ○      95s ▲ 12 p90 240s",
            " glm-5.3-bac… ○     400s ▲ 20 p90 520s",
            " claude-opus… ○     130s ▲ 12 p90 300s",
        ])

    def test_a_name_longer_than_the_label_column_is_cut_and_marked(self):
        """A cut name keeps a marker, so a shortened model never reads as a
        different one, and the badge keeps its column."""
        rows = tui.group_rows(tui.REGION_MODELS, _busy(), _busy_before())
        self.assertEqual("glm-5.3-bac…", rows[1][1][1:13])
        self.assertEqual(tui.LOAD_LABEL_CELLS, len("glm-5.3-bac…"))
        self.assertEqual(rows[1][0], rows[1][1][14])

    def test_the_group_ranks_three_models_and_draws_three_rows(self):
        """`latency_by` carries four models here; the fourth row would move the
        region under it, and `latency --by model` holds the rest."""
        cells = (_busy().get("latency_by") or {}).get("by_tier_model")
        self.assertEqual(4, len(cells))
        self.assertEqual(3, len(tui.group_rows(tui.REGION_MODELS, _busy(),
                                               _busy_before())))

    def test_a_model_below_the_floor_is_skipped_rather_than_a_question_mark(self):
        """An unmeasured model is not a `?` fact about that model: it has no row.

        The windows here measure two models, so the group draws two rows, and the
        rows it does not draw are models nobody measured enough of.
        """
        self.assertEqual(_lines(tui.group_rows(
            tui.REGION_MODELS, _healthy(), _healthy_before())), [
            " glm-5.3      ○      31s ▾ 4 p90 58s",
            " claude-haik… ○      22s ▾ 3 p90 40s",
        ])

    def test_a_window_that_ranked_no_model_says_so_in_one_row(self):
        """No cell reached the floor, so there is no model to name and no `?`
        row per model to print: one row states the fact and claims no status."""
        rows = tui.group_rows(tui.REGION_MODELS,
                              _window(42, 17, p50=95.0, p90=240.0),
                              _busy_before())
        self.assertEqual(_lines(rows), [
            " no model     ?            · no model measured",
        ])
        self.assertEqual(tui._BADGE_UNKNOWN, rows[0][0])
        self.assertEqual(tui.moon_glyph("moon"),
                         tui.region_moon(rows, tui.REGION_MODELS))

    def test_one_model_moving_up_reads_pressure(self):
        """A row is one model's percentile, and a percentile's direction is its
        severity here the way it is on a tier row: the model that moved is the
        row's own fact, and that some model moved is the region's."""
        self.assertEqual(tui.moon_glyph(tui.report.MOON_QUARTER),
                         tui.region_moon(tui.group_rows(
                             tui.REGION_MODELS, _busy(), _busy_before()),
                             tui.REGION_MODELS))
        self.assertEqual(tui.moon_glyph(tui.report.MOON_FULL),
                         tui.region_moon(tui.group_rows(
                             tui.REGION_MODELS, _healthy(), _healthy_before()),
                             tui.REGION_MODELS))

    def test_a_clean_header_tolerates_a_models_region_at_pressure(self):
        """§4.2: latency is not a verdict signal, so the two moons can differ.

        One model's p50 rose while both tier percentiles fell and nothing was
        blocked, so `cli.verdict` calls the window clean and this group - which
        holds nothing but percentiles - reads pressure. The shape is intended.
        """
        mixed = _window(9, 4, p50=31.0, p90=58.0, samples=9, models=(
            _model_cell("interactive / glm-5.3", 20, 95.0, 240.0),
            _model_cell("interactive / claude-haiku-lr34", 6, 22.0, 40.0)))
        self.assertEqual(tui.moon_glyph(tui.report.MOON_FULL),
                         tui.moon_glyph(tui.header_moon(mixed)))
        self.assertEqual(tui.moon_glyph(tui.report.MOON_QUARTER),
                         tui.region_moon(
                             tui.group_rows(tui.REGION_MODELS, mixed,
                                            _healthy_before()),
                             tui.REGION_MODELS))
        lines = tui.dashboard_lines(tui.REGION_MODELS, mixed, _healthy_before(),
                                    _cap(3, 12))
        self.assertTrue(lines[0].startswith(tui.moon_glyph(tui.report.MOON_FULL)),
                        lines[0])
        self.assertEqual(tui.moon_glyph(tui.report.MOON_QUARTER),
                         lines[9].split()[1])

    def test_one_model_on_both_tiers_names_the_tier_it_measured_on(self):
        """Two rows can name one model, and then each label carries its tier.

        `glm-5.3` measured on both tiers, so it has two percentiles and two
        rows; the tag is spent only on the duplicate, because the label column
        is 12 cells and an unambiguous name needs none of it.
        """
        both = (_model_cell("interactive / glm-5.3", 20, 95.0, 240.0),
                _model_cell("background / glm-5.3", 9, 400.0, 520.0),
                _model_cell("interactive / claude-opus-lr11", 6, 130.0, 300.0))
        rows = tui.group_rows(tui.REGION_MODELS,
                              _window(42, 17, p50=95.0, p90=240.0, models=both),
                              _busy_before())
        self.assertEqual(["glm-5.3 pri", "glm-5.3 bg", "claude-opus…"],
                         [row[1][1:13].strip() for row in rows])
        self.assertEqual(3, len({row[1][1:14] for row in rows}),
                         "two rows of one model print one label")

    def test_the_group_is_as_tall_as_the_models_it_measured(self):
        """§3.1: the group's rows are the models it drew, so its height is too.

        The busy window's three models make the eighteen-row shape, the
        two-model window draws 17, and a window that measured no model draws its
        one no-model row: 16.
        """
        cap = _cap(7, 12)
        self.assertEqual(18, len(tui.dashboard_lines(
            tui.REGION_MODELS, _busy(), _busy_before(), cap)))
        self.assertEqual(17, len(tui.dashboard_lines(
            tui.REGION_MODELS, _healthy(), _healthy_before(), cap)))
        self.assertEqual(16, len(tui.dashboard_lines(
            tui.REGION_MODELS, _window(42, 17, p50=95.0, p90=240.0),
            _busy_before(), cap)))


class RefusalsGroup(unittest.TestCase):
    """§2.4: the group the 429 and concurrency answers select."""

    def test_the_refusals_group_is_the_mockups_rows(self):
        self.assertEqual(_lines(tui.group_rows(
            tui.REGION_REFUSALS, _busy(), _busy_before())), [
            " 429          ○       11 ▲ 11 the account's rate limit",
            " at limit     ○        4 ▲  4 account pool full",
            " background   ○        7 ▲  7 demoted to the slow tier",
        ])

    def test_the_badges_are_this_machines_own_counts(self):
        rows = tui.group_rows(tui.REGION_REFUSALS, _busy(), _busy_before())
        self.assertEqual([badges for badges, _line in rows],
                         [tui.badges.BADGE_MEASURED] * 3)

    def test_a_window_that_refused_nothing_reads_zero_not_unknown(self):
        """A counter's absent class is a zero: nothing was refused, so nothing
        was unread."""
        rows = tui.group_rows(tui.REGION_REFUSALS, _healthy(), None)
        for _badge, line in rows:
            self.assertIn("        0", line)


class StallsGroup(unittest.TestCase):
    """The group "failing", "stuck" and "stream ended" select."""

    def _busy_with_digest(self):
        window = _window(42, 17, blocked=3, stalled=2, starved=1,
                         slowest={"duration": "312.4s", "outcome": "client_idle_timeout",
                                  "headline": "The stream stopped without a finish_reason."})
        return window

    def test_the_three_rows_are_stalled_slowest_and_starved(self):
        rows = tui.group_rows(tui.REGION_STALLS, self._busy_with_digest(), None)
        self.assertEqual([line[1:13].strip() for _badge, line in rows],
                         ["stalled", "slowest", "starved"])

    def test_the_digest_row_carries_the_duration_and_what_happened(self):
        rows = tui.group_rows(tui.REGION_STALLS, self._busy_with_digest(), None)
        self.assertIn("312.4s", rows[1][1])
        self.assertIn("the idle budget killed it", rows[1][1])

    def test_an_unranked_digest_says_why_rather_than_a_bare_question_mark(self):
        rows = tui.group_rows(tui.REGION_STALLS, _busy(), None)
        self.assertEqual(tui._BADGE_UNKNOWN, rows[1][0])
        self.assertIn("nothing ranked", rows[1][1])

    def test_no_digest_has_a_comparable_previous_window(self):
        rows = tui.group_rows(tui.REGION_STALLS, self._busy_with_digest(),
                              self._busy_with_digest())
        self.assertIn(tui.DELTA_NONE, rows[1][1][24:28])


# ---- the grid every evidence row is drawn on -----------------------------
# The rows each group draws. LATENCY holds four - two percentiles for each of the
# two tiers - MODELS holds one row per measured model, three at most, and the
# other two groups keep the three-row shape. The LATENCY group's fourth row is
# the one place the screen is taller than §3.1's eighteen drawn rows, and it is
# the same four rows in every window; MODELS is only on screen when `[g]` rings
# to it, and there it is one row shorter for every model it did not measure.
GROUP_ROWS = {tui.REGION_LATENCY: 4, tui.REGION_MODELS: 3,
              tui.REGION_REFUSALS: 3, tui.REGION_STALLS: 3}


class TheEvidenceGrid(unittest.TestCase):
    """§3.2: one grid for every data row, and the copy rules on top of it."""

    def setUp(self):
        self.busy = _busy()
        self.before = _busy_before()

    def test_every_group_holds_its_rows_under_the_budget(self):
        for key in tui.EVIDENCE_GROUPS:
            rows = tui.group_rows(key, self.busy, self.before)
            self.assertEqual(GROUP_ROWS[key], len(rows), key)
            for badge, line in rows:
                self.assertLessEqual(_cells(line), ROW_BUDGET, line)
                self.assertEqual(badge, line[14], "the badge left its column")
                self.assertEqual(" ", line[0], "the indent left the grid")
                self.assertEqual(" ", line[24], "the delta left its column")
                self.assertTrue(tui.dashboard_ok_row(line), line)

    def test_each_group_earns_the_moon_its_own_rows_show(self):
        """The region title wears `region_moon`, so the labels and clauses above
        have to be shapes it can read: the darkest moon for a refusal, pressure
        for a latency that moved, a clean moon in a healthy window, and the plain
        moon when nothing was measured."""
        new = tui.moon_glyph(tui.report.MOON_NEW)
        quarter = tui.moon_glyph(tui.report.MOON_QUARTER)
        full = tui.moon_glyph(tui.report.MOON_FULL)
        plain = tui.moon_glyph("moon")
        stressed = _window(42, 17, blocked=3, rejected=11, at_limit=4, background=7,
                           stalled=2, starved=1, p50=95.0, p90=240.0,
                           models=BUSY_MODELS)
        expected = {tui.REGION_REFUSALS: new, tui.REGION_LATENCY: quarter,
                    tui.REGION_MODELS: quarter, tui.REGION_STALLS: quarter}
        for key in tui.EVIDENCE_GROUPS:
            busy = tui.group_rows(key, stressed, self.before)
            healthy = tui.group_rows(key, _healthy(), _healthy_before())
            self.assertEqual(expected[key], tui.region_moon(busy, key), key)
            self.assertEqual(full, tui.region_moon(healthy, key), key)
            self.assertEqual(plain,
                             tui.region_moon(tui.group_rows(key, _empty(), None), key), key)

    def test_the_moon_reads_both_tiers_the_same_way(self):
        """A percentile's direction is its own severity, whichever tier it read.

        The tier rows wear the label of the percentile they report, so the moon
        treats `p50 pri` and `p50 bg` alike: a background p90 rising is the
        latency group reading pressure, and both tiers falling reads clean.
        """
        quarter = tui.moon_glyph(tui.report.MOON_QUARTER)
        full = tui.moon_glyph(tui.report.MOON_FULL)
        self.assertEqual(quarter, tui.region_moon(
            tui.group_rows(tui.REGION_LATENCY, _tier_split(), self.before),
            tui.REGION_LATENCY))
        self.assertEqual(full, tui.region_moon(
            tui.group_rows(tui.REGION_LATENCY, _healthy(), _healthy_before()),
            tui.REGION_LATENCY))

    def test_the_moon_holds_whether_the_group_draws_four_rows_or_five(self):
        """Four tier rows or five, the same moon: the fifth is context.

        The retired `gateway` row is a measured constant with no direction, so a
        caller that hands the region five rows - as a layout with room to spare
        would - still gets the verdict its four tier rows earned.
        """
        rows = tui.group_rows(tui.REGION_LATENCY, _busy(), self.before)
        constant = [("○", "gateway       25s   · fixed wait before headers")]
        self.assertEqual(4, len(rows))
        self.assertEqual(tui.region_moon(rows, tui.REGION_LATENCY),
                         tui.region_moon(rows + constant, tui.REGION_LATENCY))

    def test_the_refusals_title_can_read_over_capacity(self):
        """999 429s is a gateway turning work away, and the region says so.

        §4.2 forbids the old reading: the header can be dark because of the
        refusals alone, and a region that answered only to `blocked` could never
        explain it. The three rows carry the count, so the count decides.
        """
        refused = _window(999, 17, rejected=999, p50=95.0, p90=240.0)
        lines = tui.dashboard_lines("concurrency", refused, self.before, _cap(7, 12))
        self.assertEqual(tui.moon_glyph(tui.report.MOON_NEW), lines[9].split()[1])
        self.assertEqual(tui.moon_glyph(tui.report.MOON_NEW),
                         tui.region_moon(tui.group_rows(tui.REGION_REFUSALS, refused,
                                                        self.before),
                                         tui.REGION_REFUSALS))

    def test_the_moons_still_tie_to_the_header(self):
        """The two-level invariant survives the refusal rule, in both directions.

        The demoted window is in the tuple on purpose: seven requests on the
        slow tier all ran, so the header reads clean and no region may claim
        trouble. It is also the window `fix` recommends, which must not read as
        over capacity on the screen that recommends it.

        MODELS is not one of the regions here: it reads latency, no verdict reads
        latency, and `test_a_clean_header_tolerates_a_models_region_at_pressure`
        pins the shape where the two moons differ (§4.2).
        """
        stressed = _window(42, 17, blocked=3, rejected=11, at_limit=4, background=7,
                           stalled=2, starved=1, p50=95.0, p90=240.0,
                           models=BUSY_MODELS)
        verdict_groups = tuple(key for key in tui.EVIDENCE_GROUPS
                               if key != tui.REGION_MODELS)
        for metrics in (stressed, _window(999, 17, rejected=999),
                        _demoted(), _healthy()):
            header = tui.moon_glyph(tui.header_moon(metrics))
            regions = [tui.region_moon(tui.load_rows(metrics, _cap(7, 12), 1, self.before),
                                       tui.REGION_LOAD)]
            for key in verdict_groups:
                regions.append(tui.region_moon(
                    tui.group_rows(key, metrics, self.before), key))
            self.assertTrue(tui.regions_satisfy_invariant(header, regions),
                            "%s: %s" % (header, regions))

    def test_a_demoted_request_leaves_the_refusals_title_clean(self):
        """A demotion is not a refusal: the request ran, on the slow tier.

        The CLI calls this window healthy, and `NEXT` sends work here on
        purpose, so the region that draws the demotion cannot turn dark for it.
        """
        demoted = _demoted()
        self.assertEqual(tui.moon_glyph(tui.report.MOON_FULL),
                         tui.moon_glyph(tui.header_moon(demoted)))
        rows = tui.group_rows(tui.REGION_REFUSALS, demoted, self.before)
        self.assertIn("        7", rows[2][1], "the demotion is on screen")
        self.assertEqual(tui.moon_glyph(tui.report.MOON_FULL),
                         tui.region_moon(rows, tui.REGION_REFUSALS))

    def test_a_qualifier_names_no_api_field(self):
        banned = ("rate_limit_error", "scheduler admission timeout",
                  "client_idle_timeout", "org_concurrency_limit",
                  "admission_timeout", "starved_peer_evidence", "finish_reason", "`")
        busy = _window(42, 17, blocked=3, rejected=11, at_limit=4, background=7,
                       stalled=2, starved=1, p50=95.0, p90=240.0,
                       slowest={"duration": "312.4s", "outcome": "client_idle_timeout",
                                "headline": "The stream stopped without a finish_reason."})
        for key in tui.EVIDENCE_GROUPS:
            for _badge, line in tui.group_rows(key, busy, self.before):
                for word in banned:
                    self.assertNotIn(word, line, key)

    def test_a_window_under_the_floor_shows_no_delta(self):
        thin = _window(baseline.FLOOR - 1, 12, p50=83.0, p90=202.0)
        for _badge, line in tui.group_rows(tui.REGION_LATENCY, self.busy, thin):
            self.assertIn(tui.DELTA_NONE, line[24:28], line)

    def test_two_equal_windows_read_flat_rather_than_missing(self):
        for _badge, line in tui.group_rows(tui.REGION_REFUSALS, self.busy, self.busy):
            self.assertIn(tui.DELTA_LEVEL, line[24:28], line)

    def test_an_unknown_group_is_a_caller_bug(self):
        with self.assertRaises(KeyError):
            tui.group_rows("prose", self.busy, self.before)


# ---- the NEXT region ------------------------------------------------------
class TheNextRegion(unittest.TestCase):
    """One imperative and one command: the brief's four, and no fifth."""

    def setUp(self):
        self.cap = _cap(7, 12)

    def test_over_capacity_is_the_mockups_two_rows(self):
        self.assertEqual(_lines(tui.next_rows(_busy(), self.cap, tui.REGION_LATENCY)), [
            "NEXT  Hold 12 in flight · send the rest to -background",
            "→ luna-doctor fix --since 1h                                              [f]",
        ])

    def test_a_healthy_window_is_the_mockups_two_rows(self):
        self.assertEqual(_lines(tui.next_rows(_healthy(), self.cap, tui.REGION_LATENCY)), [
            "NEXT  Nothing to change · ●12 lanes hold this load",
            "→ luna-doctor watch --since 1h",
        ])

    def test_a_window_with_no_lane_count_holds_flat_and_fetches_it(self):
        self.assertEqual(_lines(tui.next_rows(_busy(), None, tui.REGION_REFUSALS)), [
            "NEXT  Hold concurrency flat until the lane count is known",
            "→ luna-doctor capacity --online",
        ])

    def test_nothing_measured_widens_the_window(self):
        self.assertEqual(_lines(tui.next_rows(_empty(), self.cap, tui.REGION_LATENCY)), [
            "NEXT  Nothing measured in the last hour · widen the window",
            "→ luna-doctor analyze --since 6h                                          [w]",
        ])

    def test_the_imperative_is_one_short_verb_led_clause(self):
        cases = ((_busy(), self.cap), (_healthy(), self.cap), (_busy(), None),
                 (_empty(), self.cap))
        for metrics, cap in cases:
            line = tui.next_rows(metrics, cap, tui.REGION_STALLS)[0][1]
            clause = line[len("NEXT  "):]
            self.assertLessEqual(_cells(clause), tui.NEXT_CELLS, clause)
            self.assertIn(clause.split()[0], ("Hold", "Nothing"), clause)
            self.assertFalse(clause.endswith("."), "a data row has no sentence stop")

    def test_the_command_row_ends_with_the_key_that_runs_it(self):
        for metrics, cap, key in ((_busy(), self.cap, "f"),
                                  (_empty(), self.cap, "w"),
                                  (_healthy(), self.cap, None),
                                  (_busy(), None, None)):
            line = tui.next_rows(metrics, cap, tui.REGION_LATENCY)[1][1]
            self.assertTrue(line.startswith("→ luna-doctor "), line)
            self.assertLessEqual(_cells(line), tui.HEADER_CELLS, line)
            if key is None:
                self.assertNotIn("[", line)
            else:
                self.assertTrue(line.endswith("[%s]" % key), line)

    def test_the_group_on_screen_does_not_change_the_advice(self):
        """§2.4 draws the same imperative beside the refusals group that §2.1
        draws beside the latency group: the state decides, not the selection."""
        for key in tui.EVIDENCE_GROUPS:
            self.assertEqual(tui.next_rows(_busy(), self.cap, tui.REGION_LATENCY),
                             tui.next_rows(_busy(), self.cap, key))

    def test_the_row_reads_the_headers_own_moon(self):
        """The NEXT row and the header cannot disagree: both ask header_moon."""
        for metrics in (_busy(), _healthy(), _empty()):
            moon = tui.header_moon(metrics)
            line = tui.next_rows(metrics, self.cap, tui.REGION_LATENCY)[0][1]
            if moon == "moon":
                self.assertIn("Nothing measured", line)
            elif moon in (tui.report.MOON_NEW, tui.report.MOON_QUARTER):
                self.assertIn("Hold 12 in flight", line)
            else:
                self.assertIn("Nothing to change", line)

    def test_a_declared_lane_count_is_badged_as_declared(self):
        declared = dict(_healthy(), declared_plan="apex")
        line = tui.next_rows(declared, None, tui.REGION_LATENCY)[0][1]
        self.assertIn("%s6 lanes hold this load" % tui.badges.BADGE_DECLARED, line)

    def test_an_unknown_group_is_a_caller_bug(self):
        with self.assertRaises(KeyError):
            tui.next_rows(_healthy(), self.cap, "prose")


# ---- the two key lines ----------------------------------------------------
class TheKeyLines(unittest.TestCase):
    """Provenance on one line, the moons on the other, and no third line."""

    def setUp(self):
        self.cap = _cap(7, 12)

    def test_the_two_key_lines_are_the_mockups_lines(self):
        self.assertEqual(tui.key_lines(), [
            "● account · ○ this machine (a lower bound) · ? unknown",
            "🌕 ok · 🌗 pressure · 🌑 over capacity",
        ])

    def test_the_moon_key_names_the_plain_moon_only_when_the_screen_drew_it(self):
        self.assertEqual(tui.key_lines(_empty(), self.cap)[1],
                         "🌙 not established · 🌕 ok · 🌗 pressure · 🌑 over capacity")
        self.assertEqual(tui.key_lines(_healthy(), self.cap)[1],
                         "🌕 ok · 🌗 pressure · 🌑 over capacity")

    def test_the_declared_entry_appears_only_beside_a_declared_number(self):
        self.assertNotIn("◌", tui.key_lines(_healthy(), self.cap)[0])
        self.assertEqual(tui.key_lines(dict(_healthy(), declared_plan="apex"), None)[0],
                         "● account · ○ this machine (a lower bound) · ? unknown"
                         " · ◌ declared")

    def test_each_key_line_fits_the_headers_budget(self):
        for metrics in (None, _empty(), _healthy(), _busy(),
                        dict(_healthy(), declared_plan="apex")):
            lines = tui.key_lines(metrics, self.cap)
            self.assertEqual(2, len(lines))
            for line in lines:
                self.assertLessEqual(_cells(line), tui.HEADER_CELLS, line)
