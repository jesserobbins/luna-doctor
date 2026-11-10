"""The triage screen: one question, one 24h scan, one provenance-badged verdict.

Bare `luna-doctor` on a terminal opens this instead of argparse help: it asks
what the person is seeing, scans the last 24 hours of local logs and the cached
account capacity while the question is on screen, and lands on one verdict with
the next action.

Everything that decides what a reader sees - TRIAGE_CHOICES, pick_lead, the
three row builders and build_verdict_rows - is a pure function of a metrics
dict, a Capacity payload and the chosen answer, so the suite can test it
offline and without a terminal. Only TriageApp needs Textual, which is the
optional `luna-doctor[tui]` extra: this module imports it at the top, and the
caller (cli.main) guards the import so a machine without the extra falls back
to the linear path.

The panels are in ranked-trust order: what the account reports first (●), then
what this machine's logs show (○), then the verdict. Every number carries its
badge, and one legend line names all three.
"""

from __future__ import annotations

import contextlib
import io
import time

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Label, ListItem, ListView, Static

from luna_doctor import attribution, badges, cli, explain, report, sources
from luna_doctor.style import Style

SCAN_HOURS = 24
SCAN_HARNESSES = ("pi", "claude-code")

PANEL_ACCOUNT = "What the account reports (authoritative)"
PANEL_LOCAL = "What this machine's logs show (informative, a lower bound)"
PANEL_VERDICT = "Verdict"

# The six on-screen answers, in the order the design draws them.
TRIAGE_CHOICES: list[tuple[str, str]] = [
    ("Requests failing or hanging", "failing"),
    ("Things are slow", "slow"),
    ('"Stream ended without finish_reason"', "finish_reason"),
    ("429 / concurrency errors", "concurrency"),
    ("Agent seems stuck mid-task", "stuck"),
    ("Nothing yet, just checking", "checking"),
]

# Which evidence leads the verdict for each answer. "admission" is the admission
# and saturation panel, "latency" the latency-and-queue-wait panel, "stuck" the
# idle-budget diagnosis, and "overview" the plain traffic summary.
_LEAD = {
    "failing": "admission",
    "slow": "latency",
    "finish_reason": "stuck",
    "concurrency": "admission",
    "stuck": "stuck",
    "checking": "overview",
}

_BADGE_UNKNOWN = badges.badge_for(sources.PROV_UNKNOWN)
_BADGE_MEASURED = badges.badge_for(sources.PROV_MEASURED)

# The status moons and their legend, through the one gate that owns glyphs: a
# screen is not a byte stream, and Textual draws UTF-8, so the screen asks for
# the emoji form - the same glyphs `--style pretty` prints.
_GLYPH_STYLE = Style(color=False, emoji=True, motion=False)


def pick_lead(choice_key: str) -> str:
    """The lead panel id for one on-screen answer."""
    return _LEAD[choice_key]


def _count(value):
    """An int count, or None. A bool is not a count and a float is not a count."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _sec(value) -> str:
    return "unknown" if value is None else f"{value}s"


# ---- the account panel: what the account reports (●) -----------------------
def account_rows(metrics: dict, cap) -> list[tuple[str, str]]:
    """The account's own numbers, one row per fact, badged ●.

    Every row is read from the Capacity payload with its nulls intact: a null
    is unknown and gets the "?" badge with its reason, never a zero. The
    priority pool's occupancy is qualified by its ledger, so a count served
    beside a ledger that is not "up" is advisory and never wears the
    authoritative badge. The two overflow-pool null shapes stay apart: a cap
    that could not be read is unknown, a zero cap is a closed pool. Refusal
    words appear only where the admission counts put them.

    A plan the person declared still gets its row when no payload is cached:
    --plan exists so the ◌ declared width can appear at the front door, and a
    missing payload is no reason to hide a declaration the reader typed.
    """
    if cap is None:
        rows = [(_BADGE_UNKNOWN,
                 "the account did not answer: no capacity payload is cached "
                 "(`luna-doctor capacity --online` fetches one)")]
        if (metrics or {}).get("declared_plan"):
            rows.append(_width_row(metrics, None))
        return rows
    rows = [_width_row(metrics, cap)]
    rows.extend(_occupancy_rows(cap))
    rows.extend(_overflow_rows(cap))
    rows.extend(_admission_rows(cap))
    return rows


def _width_row(metrics: dict, cap) -> tuple[str, str]:
    """The priority concurrency the verdict is judged against, and whose it is.

    attribution.resolve_priority_concurrency decides account first, then a
    --plan declaration, and this row wears the badge that decision reports:
    ● when the account said the number, ◌ when a person declared it.
    """
    config = cli._cap_field(cap, "config")   # cap is None when nothing is cached
    served = config.to_dict() if hasattr(config, "to_dict") else None
    value, source = attribution.resolve_priority_concurrency(
        served, (metrics or {}).get("declared_plan"))
    if value is None:
        return (_BADGE_UNKNOWN,
                "priority concurrency not reported by the account, and no --plan "
                "declared (unknown)")
    where = "" if source == "account" else f" ({source})"
    return (badges.badge_for(source),
            f"priority concurrency {value}, {badges.badge_help(source)}{where}")


def _occupancy_rows(cap) -> list[tuple[str, str]]:
    """The priority pool's live occupancy and the moment it was taken.

    The flat `now` fields cover the priority pool only. The occupancy line is
    qualified by the ledger: when the ledger is not "up", the count is advisory
    and the row says it may be stale or missing, because a pool line that looks
    authoritative over an unhealthy ledger is a claim nobody measured.
    """
    now = cap.now
    if now is None:
        return [(_BADGE_UNKNOWN, "live occupancy not reported by the account (unknown)")]
    in_use, limit, available = (cli._cap_field(now, "in_use"),
                                cli._cap_field(now, "limit"),
                                cli._cap_field(now, "available"))
    if in_use is None and limit is None:
        occupancy = "occupancy unknown (not reported)"
    elif in_use is None:
        occupancy = f"in use unknown (not reported) · limit {limit}"
    elif limit is None:
        occupancy = f"in use {in_use} · limit unknown (not reported)"
    else:
        occupancy = f"in use {in_use} of {limit} priority concurrent requests"
    if available is not None:
        occupancy += f" · {available} available"
    ledger = cli._cap_field(now, "ledger")
    saturated = cli._cap_field(now, "saturated")
    if saturated is not None:
        occupancy += f" · saturated: {cli._cap_flag(saturated)}"
    if ledger == "up" and in_use is not None:
        rows = [(badges.badge_for(sources.PROV_ACCOUNT), f"priority pool {occupancy}")]
    elif ledger == "up":
        # The ledger is healthy and the cap is there; the count is not. The
        # row says so, so it must not wear the badge of a number the account
        # reported.
        rows = [(_BADGE_UNKNOWN, f"priority pool {occupancy}")]
    else:
        rows = [(_BADGE_UNKNOWN,
                 f"priority pool {occupancy}, but the ledger is "
                 f"{cli._cap_unknown(ledger)}, so the count may be stale or missing")]
    sampled = cli._cap_field(now, "sampled_at")
    sample = f"pool ledger {cli._cap_unknown(ledger)} · sampled at {cli._cap_unknown(sampled)}"
    if getattr(cap, "stale", False):
        sample += "; the cache is past its time to live, so run `luna-doctor capacity --online`"
    known = ledger is not None and sampled is not None
    rows.append((badges.badge_for(sources.PROV_ACCOUNT) if known else _BADGE_UNKNOWN, sample))
    return rows


def _overflow_rows(cap) -> list[tuple[str, str]]:
    """The overflow pool, as its own row: overflow traffic never appears in the
    priority pool's in_use.

    The wording is the capacity report's own (`cli._cap_pool_now`), so both
    surfaces name the same shapes with the same words. An absent block is
    unknown, not a closed pool. The two null shapes are different facts and are
    rendered differently: `limit: null` is a cap that could not be read,
    `limit: 0` is a closed pool the account is entitled to no overflow on, which
    is never saturated. The saturated flag is reported as an observation
    ("saturated: yes"), never as a refusal.

    The authoritative badge follows the same rule as the priority pool's
    occupancy row: a cap beside a count nobody could read is not a line the
    account reported. A closed pool rests on its cap alone, so it keeps ●.
    """
    pool = cli._cap_field(getattr(cap, "now", None), "background")
    text = "overflow pool: " + cli._cap_pool_now(pool)
    limit = cli._cap_field(pool, "limit")
    if limit == 0 and not isinstance(limit, bool):
        return [(badges.badge_for(sources.PROV_ACCOUNT), text)]
    if pool is not None:
        text += f" · saturated: {cli._cap_flag(cli._cap_field(pool, 'saturated'))}"
    read = limit is not None and cli._cap_field(pool, "in_use") is not None
    badge = badges.badge_for(sources.PROV_ACCOUNT) if read else _BADGE_UNKNOWN
    return [(badge, text)]


# The admission counts as the report's prose names them: the API field name is
# a field name, and "denied" here is the same class the CLI calls a block.
_ADMISSION_CLASSES = (
    ("denied_after_wait", "denied after waiting"),
    ("denied", "blocked at admission"),
    ("deferred", "deferred"),
)


def _admission_rows(cap) -> list[tuple[str, str]]:
    """The admission record over complete UTC days, beside the days it covers.

    The counts are the API's own; the wording is the capacity report's ("denied
    after waiting", "blocked at admission"), so the same class reads the same
    way on both surfaces, and the days a count happened on are named where the
    list stays short. The in-progress day is never included by the API.
    """
    days = sources.capacity_admission_days(cap)
    if not days:
        return [(_BADGE_UNKNOWN,
                 "the account's admission record is not available (unknown)")]
    totals: dict[str, int] = {}
    for _day, entry in days:
        for name, _phrase in _ADMISSION_CLASSES:
            value = _count(entry.get(name))
            if value:
                totals[name] = totals.get(name, 0) + value
    window = f"admission last {len(days)} complete UTC day(s) through {days[-1][0]}"
    if not totals:
        return [(badges.badge_for(sources.PROV_ACCOUNT),
                 f"{window}: no request was blocked at admission or after waiting")]
    bits = []
    for name, phrase in _ADMISSION_CLASSES:
        if not totals.get(name):
            continue
        bit = f"{phrase} {totals[name]}"
        if name == "denied_after_wait":
            named = [day for day, entry in days if _count(entry.get(name))]
            if 0 < len(named) <= 4:
                bit += f" on {', '.join(named)}"
        bits.append(bit)
    return [(badges.badge_for(sources.PROV_ACCOUNT),
             f"{window}: " + " · ".join(bits))]


# ---- the local panel: what this machine's logs show (○) --------------------
def local_rows(choice_key: str, metrics: dict) -> list[tuple[str, str]]:
    """What this machine's logs show, badged ○: informative, a lower bound.

    Every count here is a measurement of these logs, so other machines are
    invisible and the peak is a floor, not a total. Nothing sums a measured
    count with a configured width. The answer picks which evidence follows the
    peak (pick_lead), so the classes the person named come first.
    """
    rows = []
    peak = _count(metrics.get("peak_concurrency"))
    if peak is None:
        rows.append((_BADGE_MEASURED,
                     "peak concurrency not measured in this window"))
    else:
        rows.append((_BADGE_MEASURED,
                     f"peak {peak} concurrent, {badges.badge_help(sources.PROV_MEASURED)} "
                     f"- other machines are invisible to these logs"))
    lead = pick_lead(choice_key)
    if lead == "latency":
        rows.extend(_latency_rows(metrics))
    elif lead == "stuck":
        rows.extend(_stuck_rows(metrics))
    elif lead == "admission":
        rows.extend(_admission_evidence_rows(metrics))
    else:
        rows.extend(_overview_rows(metrics))
    return rows


def _outcome_rows(metrics: dict, classes) -> list[tuple[str, str]]:
    """One row per outcome class the person's answer is about, when it counted."""
    by_outcome = metrics.get("by_outcome") or {}
    rows = []
    for name, phrase in classes:
        count = _count(by_outcome.get(name))
        if count:
            rows.append((_BADGE_MEASURED, f"{count} request(s) {phrase}"))
    return rows


_PRESSURE_CLASSES = (
    ("admission_timeout", "blocked at admission here (`scheduler admission timeout`)"),
    ("rate_limited", "rejected with a 429 (`rate_limit_error`)"),
    ("org_concurrency_limit", "rejected at the account's concurrent-request limit"),
    ("client_idle_timeout", "stalled until the harness's own idle budget killed them"),
)


def _admission_evidence_rows(metrics: dict) -> list[tuple[str, str]]:
    """The local admission and saturation evidence behind the admission lead."""
    rows = _outcome_rows(metrics, _PRESSURE_CLASSES)
    saturated = _count(metrics.get("saturated"))
    if saturated:
        rows.append((_BADGE_MEASURED,
                     f"{saturated} request(s) sat at the concurrency ceiling here"))
    background = _count((metrics.get("by_tier") or {}).get("background"))
    if background:
        rows.append((_BADGE_MEASURED,
                     f"{background} request(s) ran on the -background tier"))
    if not rows:
        rows.append((_BADGE_MEASURED,
                     "no request here was blocked at admission, rejected with a 429, "
                     "or demoted to the -background tier"))
    return rows


def _latency_rows(metrics: dict) -> list[tuple[str, str]]:
    """The latency answer: the account's queue wait, then the local latencies.

    The queue wait is the account's own admission measure, so it wears the
    account badge even though it answers a latency question; a null there means
    nobody waited that day, and cli._cap_wait says exactly that rather than a
    zero. The local rows are completed requests only.
    """
    rows = [_queue_wait_row(metrics)]
    for tier in ("interactive", "background"):
        lat = (metrics.get("latency_s") or {}).get(tier) or {}
        if _count(lat.get("n")):
            rows.append((_BADGE_MEASURED,
                         f"{tier} latency n={lat['n']} p50={_sec(lat.get('p50'))} "
                         f"p90={_sec(lat.get('p90'))}"))
    bands = metrics.get("saturation_bands") or {}
    if bands.get("top_inflation_flagged"):
        top = (bands.get("bands") or [])[-1]
        rows.append((_BADGE_MEASURED,
                     f"p90 {_sec(top.get('p90'))} at concurrency {top.get('band')} "
                     f"against a mid-band p90 of {_sec(bands.get('mid_p90_baseline_s'))}"))
    if len(rows) == 1:
        rows.append((_BADGE_MEASURED,
                     "no completed request in this window, so no latency was measured"))
    return rows


def _queue_wait_row(metrics: dict) -> tuple[str, str]:
    """The account's queue wait on the most recent complete UTC day it reports.

    avg_queue_wait_ms is the account's own measure, so the row wears the account
    badge. A window with no complete day in the account's record still gets a
    row: the dimension is unknown, and a missing queue-wait line would read as
    "nobody waited".
    """
    days = (metrics.get("capacity") or {}).get("window_days") or []
    for day in reversed(days):
        if isinstance(day, dict) and "avg_queue_wait_ms" in day:
            return (badges.badge_for(sources.PROV_ACCOUNT),
                    f"queue wait, the account's most recent complete UTC day "
                    f"({day.get('day')}): {cli._cap_wait(day.get('avg_queue_wait_ms'))}")
    return (_BADGE_UNKNOWN,
            "queue wait: the account reports no complete UTC day inside this window "
            "(unknown)")


def _stuck_rows(metrics: dict) -> list[tuple[str, str]]:
    """The idle-budget diagnosis, plus one row from `explain --slowest`.

    "Agent seems stuck" binds to the client_idle_timeout class *and* to the
    explain --slowest panel (the builder-check decision), so one bounded row
    carries the slowest request's own why, and the whole panel stays in
    `luna-doctor explain`.
    """
    stalled = _count((metrics.get("by_outcome") or {}).get("client_idle_timeout"))
    if stalled:
        rows = [(_BADGE_MEASURED,
                 f"{stalled} request(s) stalled until the harness's own idle budget killed "
                 f"them (`client_idle_timeout`: the stream ended without a finish_reason)")]
    else:
        rows = [(_BADGE_MEASURED,
                 "no request in these logs ended on `client_idle_timeout`, so nothing "
                 "here shows the harness's idle budget stopping a stream")]
    slow = metrics.get("slowest_request") or {}
    if slow.get("headline"):
        rows.append((_BADGE_MEASURED,
                     f"the slowest request here took {slow.get('duration', 'unknown')} "
                     f"({slow.get('outcome', 'unknown')}): {slow['headline']}"))
    else:
        # The digest is None for more than one reason - an empty window, an
        # explain that raised, a request set with nothing to rank - and this
        # row cannot tell them apart. It states the one fact it has: no request
        # was ranked, and why is not established here.
        rows.append((_BADGE_MEASURED,
                     "the slowest request here is not established (unknown): this "
                     "scan ranked none, and it recorded no reason why"))
    return rows


def _overview_rows(metrics: dict) -> list[tuple[str, str]]:
    """The plain traffic summary, for "nothing yet, just checking"."""
    hours = int(metrics.get("window_hours") or SCAN_HOURS)
    n = _count(metrics.get("requests")) or 0
    by_harness = metrics.get("by_harness") or {}
    harnesses = ", ".join(f"{name} {count}" for name, count in sorted(by_harness.items()))
    text = f"{n} request(s) in the last {hours}h"
    if harnesses:
        text += f" ({harnesses})"
    rows = [(_BADGE_MEASURED, text)]
    by_tier = metrics.get("by_tier") or {}
    if by_tier:
        rows.append((_BADGE_MEASURED,
                     "tiers: " + ", ".join(f"{name} {count}"
                                           for name, count in sorted(by_tier.items()))))
    return rows


# ---- the verdict panel: the CLI's own verdict, never a second one ----------
def verdict_rows(metrics: dict) -> list[tuple[str, str]]:
    """The verdict line and its adjustments, from cli.verdict and nowhere else.

    The badge column stays empty: a verdict is a conclusion drawn from the rows
    above it, not a number one source reported. The text is the headline and
    adjustments `analyze` prints, so the TUI cannot disagree with the CLI about
    what the window means. The status moon leads the headline, read from
    `report.status_moon` - the same helper the pretty renderer asks. The legend
    for that scale is not a row here: `status_legend` returns it, and the screen
    prints it as its own quiet line under the panel, the weight the pretty
    renderer gives it. The one arrow marks the action this screen can take in
    place.
    """
    headline, adjustments, exit_code = cli.verdict(metrics)
    moon = report.status_moon(exit_code, headline)
    glyph = _GLYPH_STYLE.symbol(moon)
    rows = [("", f"{glyph} {headline}" if glyph else headline)]
    rows += [("", line) for line in adjustments]
    rows += [("→", "press [f] for the per-agent fix, or run `luna-doctor fix`")]
    return rows


def status_legend(metrics: dict) -> str:
    """The one-line legend for the status moons, or "" when no phase is drawn.

    It is the same sentence the pretty renderer closes with, from the same
    table, and the screen prints it as its own muted line: a key to a scale is
    not a conclusion, so it never wears the verdict panel's bold.
    """
    headline, _adjustments, exit_code = cli.verdict(metrics)
    if report.status_moon(exit_code, headline) not in report.MOON_PHASES:
        return ""
    return _GLYPH_STYLE.moon_legend()


def build_verdict_rows(choice_key: str, metrics: dict, cap) -> list[tuple[str, str]]:
    """Every (badge, text) row of the verdict screen, in ranked-trust order."""
    return (account_rows(metrics, cap)
            + local_rows(choice_key, metrics)
            + verdict_rows(metrics))


def panel_text(heading: str, rows, *, bold: bool = False) -> Text:
    """One panel: its heading, then one line per row.

    The badge is what a reader sorts on, so it leads the line. A row with no
    badge is a conclusion (the verdict and its action), and it is not given a
    source it does not have. Measured rows render dimmed, the verdict bold.
    """
    text = Text()
    text.append(heading, style="bold")
    for badge, line in rows:
        text.append("\n")
        row = f"{badge} {line}" if badge else line
        if bold:
            text.append(row, style="bold")
        elif badge == _BADGE_MEASURED:
            text.append(row, style="dim")
        else:
            text.append(row)
    return text


# ---- the scan: the same collect() and analyse() cmd_analyze calls ----------
def scan(plan: str | None = None) -> tuple:
    """One 24h scan of local logs plus the cached capacity payload.

    collect() and analyse() are the CLI's own entry points, so the screen
    re-slices the same metrics `analyze` renders and never re-scans differently.
    The enrichment after them is the part of cmd_analyze the verdict reads: the
    declared plan, the account's cached capacity block, and the pressure-window
    count re-counted against the resolved width - without that re-count the
    verdict would measure "wide" against the tool's fallback constant and then
    tell a reader to fetch a width the account panel already shows.

    The capacity cache is read twice on purpose: `_capacity_metrics` builds the
    CLI's own block (the shape cli.verdict reads), and fetch_capacity returns
    the Capacity payload the badges read. Both are cache-only; neither opens
    the network, and neither re-derives the other's facts.
    """
    now = time.time()
    reqs = cli.collect(SCAN_HOURS, list(SCAN_HARNESSES), None, None, now=now)
    metrics = cli.analyse(reqs, SCAN_HOURS)
    metrics["declared_plan"] = plan
    try:
        metrics["peak_concurrency_priority"] = cli.max_concurrency(
            [r for r in cli.timed(reqs) if getattr(r, "tier", "") == "interactive"])[0]
    except Exception:
        metrics["peak_concurrency_priority"] = metrics.get("peak_concurrency")
    try:
        metrics["capacity"] = cli._capacity_metrics(
            now - SCAN_HOURS * 3600, now, metrics, allow_network=False)
    except Exception as e:
        metrics["capacity"] = {
            "provenance": sources.PROV_UNKNOWN,
            "reason": f"collection failed: {type(e).__name__}: {e}"[:200]}
    width, _source = cli._priority_for_advice(metrics)
    if (isinstance(width, int) and not isinstance(width, bool) and width > 0
            and metrics.get("requests")):
        try:
            wide = cli.pressure_windows(cli._priority_tier(reqs), width)
            metrics["pressure_windows"] = len(wide)
            metrics["longest_pressure_window"] = (
                max(wide, key=lambda w: w["seconds"]) if wide else None)
            metrics["pressure_window_threshold"] = width
        except Exception:
            pass
    digest = slowest_request_digest(reqs, metrics)
    if digest is not None:
        metrics["slowest_request"] = digest
    return metrics, sources.fetch_capacity(allow_network=False)


def slowest_request_digest(reqs, metrics: dict):
    """One bounded digest of the slowest request, from explain's own machinery.

    "Agent seems stuck" reaches the explain --slowest panel too, so the screen
    carries the slowest request's own why in one row. It calls explain.slowest
    and explain.explain rather than re-deriving a reason from the metrics, and
    it returns None when there is nothing to rank. It never raises: the screen
    loses one row, not the verdict.
    """
    if not reqs:
        return None
    try:
        slowest = explain.slowest(reqs, 1)[0]
        served = metrics.get("capacity") or {}
        if served.get("provenance") != sources.PROV_ACCOUNT:
            served = None
        doc = explain.explain(slowest, reqs, served, cli._priority_for_advice(metrics))
    except Exception:
        return None
    return {
        "duration": dict(doc["rows"]).get("duration", "unknown"),
        "outcome": doc.get("outcome"),
        "headline": doc.get("headline", ""),
    }


def scan_status(metrics: dict, cap, online: bool = False) -> str:
    """The scan's own line: what was read, never a verdict.

    It names the request count and the account's plan and width, and it says
    "not reported" where a number is missing rather than leaving a gap a reader
    would fill with a zero.
    """
    n = _count(metrics.get("requests")) or 0
    bits = [f"✓ {n} request(s) in the last {int(metrics.get('window_hours') or SCAN_HOURS)}h"]
    if cap is None:
        bits.append("account capacity: not cached (unknown)")
    else:
        plan = cli._cap_unknown(cli._cap_field(cap.config, "plan"))
        width = cli._cap_unknown(cli._cap_field(cap.config, "priority_lanes"))
        bits.append(f"account: plan {plan} · priority concurrency {width}")
    if online:
        bits.append("the cache was refreshed by --online")
    return " · ".join(bits)


def fix_output(plan: str | None = None) -> str:
    """The `fix` command's own output, cache-only, as text for the screen.

    It runs `fix` instead of re-wiring recommendations: the fix block owns its
    printing, and a re-render here would be a second copy that could drift.
    `--offline` keeps the promise that the TUI opens no new network behavior.
    """
    argv = ["fix", "--since", f"{SCAN_HOURS}h", "--offline"]
    if plan:
        argv += ["--plan", plan]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            cli.main(argv)
    except SystemExit:      # a usage error exiting 2 is output on this screen, not a crash
        pass
    return buf.getvalue()


# ---- the screen -----------------------------------------------------------
class TriageApp(App):
    """The triage screen: one question, then one badged verdict.

    The scan and the cache-only capacity read run in a thread worker while the
    question is on screen, so the answer is never blocked. The app opens no
    network behavior of its own: run_triage(online=True) refreshes the cache
    once before the screen opens, exactly like `--online` on the CLI.
    """

    TITLE = "luna-doctor triage"

    CSS = """
    Screen { background: $surface; }
    #question { padding: 1 2 0 2; text-style: bold; }
    #choices { margin: 1 2 0 2; height: auto; max-height: 10; border: round $primary; }
    #status { padding: 1 2; color: $text-muted; }
    #verdict { display: none; padding: 0 2; }
    .panel { border: round $primary; padding: 0 1; margin: 1 0 0 0; height: auto; }
    #verdict-line { border: round $accent; }
    #legend, #moon-legend { padding: 1 2; color: $text-muted; }
    #fix { display: none; border: round $secondary; padding: 0 1; }
    """

    BINDINGS = [
        Binding("f", "show_fix", "fix"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, plan: str | None = None, online: bool = False) -> None:
        super().__init__()
        self.plan = plan
        self.online = online
        self.metrics: dict | None = None
        self.capacity = None
        self.pending_choice: str | None = None
        self.scan_failed: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("What are you seeing?", id="question")
        yield ListView(
            *[ListItem(Label(text), id=key) for text, key in TRIAGE_CHOICES],
            id="choices")
        # markup=False: this line repeats the CLI's words, and those name keys
        # as [f] and [q]. As Rich markup, the brackets and what they hold vanish.
        yield Static("", id="status", markup=False)
        with VerticalScroll(id="verdict"):
            yield Static("", id="account", classes="panel")
            yield Static("", id="local", classes="panel")
            yield Static("", id="verdict-line", classes="panel")
            # its own line, in the muted weight every legend on this screen
            # wears: the verdict panel above it is bold, and a key is not a
            # conclusion
            yield Static("", id="moon-legend")
            yield Static(badges.legend_line(), id="legend")
        with VerticalScroll(id="fix"):
            # markup=False: the fix block's prose may name a tier as [agents].
            yield Static("", id="fix-text", markup=False)

    def on_mount(self) -> None:
        self._set_status("scanning local logs… (last 24h)")
        self.query_one("#choices", ListView).focus()
        self._scan_worker()

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    # -- the scan ---------------------------------------------------------
    @work(thread=True)
    def _scan_worker(self) -> None:
        try:
            metrics, cap = scan(self.plan)
        except Exception as e:
            self.call_from_thread(self._scan_failed, f"{type(e).__name__}: {e}")
            return
        self.call_from_thread(self._scan_done, metrics, cap)

    def _scan_failed(self, reason: str) -> None:
        self.scan_failed = reason
        self._set_status(f"the scan failed ({reason}); no verdict is available")

    def _scan_done(self, metrics: dict, cap) -> None:
        self.metrics = metrics
        self.capacity = cap
        self._set_status(scan_status(metrics, cap, self.online))
        if self.pending_choice is not None:
            self._show_verdict(self.pending_choice)

    # -- the answer -------------------------------------------------------
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        key = event.item.id
        if key is None:
            return
        if self.scan_failed is not None:
            self._set_status(f"the scan failed ({self.scan_failed}); there is no verdict to show")
            return
        self.pending_choice = key
        if self.metrics is None:
            self._set_status("the scan is still running; the verdict appears when it finishes")
            return
        self._show_verdict(key)

    def _show_verdict(self, key: str) -> None:
        self.query_one("#account", Static).update(
            panel_text(PANEL_ACCOUNT, account_rows(self.metrics, self.capacity)))
        self.query_one("#local", Static).update(
            panel_text(PANEL_LOCAL, local_rows(key, self.metrics)))
        self.query_one("#verdict-line", Static).update(
            panel_text(PANEL_VERDICT, verdict_rows(self.metrics), bold=True))
        legend = status_legend(self.metrics)
        moon_legend = self.query_one("#moon-legend", Static)
        moon_legend.update(legend)
        # no phase drawn means no key: an empty line would be decoration
        moon_legend.display = bool(legend)
        self.query_one("#question").display = False
        self.query_one("#choices").display = False
        self.query_one("#verdict").display = True
        self._set_status(f"{scan_status(self.metrics, self.capacity, self.online)} · "
                         f"[f] shows the fix, [q] quits")

    # -- the fix, in place, cache only ------------------------------------
    def action_show_fix(self) -> None:
        if self.scan_failed is not None:
            self._set_status(f"the scan failed ({self.scan_failed}); the fix output needs a scan")
            return
        if self.metrics is None:
            self._set_status("the scan is still running; the fix output needs it")
            return
        self._set_status("rendering the fix output (cache only)…")
        self._fix_worker()

    @work(thread=True)
    def _fix_worker(self) -> None:
        self.call_from_thread(self._fix_done, fix_output(self.plan))

    def _fix_done(self, text: str) -> None:
        self.query_one("#fix-text", Static).update(text or "the fix output was empty")
        self.query_one("#fix").display = True
        self._set_status("the fix output is on screen · [q] quits")


def run_triage(plan: str | None = None, online: bool = False) -> int:
    """Open the triage screen and return 0.

    `online` is the bare run's `--online`: one capacity fetch refreshes the
    cache before the screen opens, and the screen itself reads only the cache.
    A failed fetch is never an error - the account panel then says the number
    is unknown. Without Textual installed this module fails to import, and the
    caller falls back to the linear path.
    """
    if online:
        # The refresh is a network round trip that can take seconds, and it
        # happens before the screen exists: say what it is doing first, so a
        # quiet terminal does not read as a hung app.
        cli.notice("refreshing the account's capacity cache before the screen opens")
        try:
            sources.fetch_capacity(allow_network=True)
        except Exception:
            pass
    TriageApp(plan=plan, online=online).run()
    return 0
