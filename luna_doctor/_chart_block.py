#!/usr/bin/env python3
"""`chart` command for luna-doctor: the concurrency picture.

Executed inside luna_doctor.cli's namespace (see the loader at the bottom of
that script), so it can use Request, collect, analyse, window_hours and the
measured constants directly. The picture is built as a Scene of shapes (see
luna_doctor.scene), then serialised; nothing here touches SVG syntax.

Design note -- what the picture is, why each mark exists, and what was
chosen not to draw.

WHAT. One stacked-area graph: one plot area, one time axis, one vertical
scale, one stack. The stack's height at any moment is the account's total
concurrent inference requests -- everything in flight at once, which is
what the gateway prices and what a priority-concurrency limit compares
against. The stack is layered by band, and a band is one model's traffic
from one agent in one scheduling class: blues are priority scheduling,
violets are background scheduling (the `-background` model-id form), and
each band is named with its agent ("glm-5.3 \u00b7 pi") whenever more than
one agent has traffic in the window. The machine's whole story -- every
agent, every model, both scheduling classes -- is one stack: the build-up
the reader is looking for is which bands piled up, for whom, in which
class, and whether the total crossed what the account is allowed to run.

BAND ORDER. Fixed and deterministic: scheduling class first -- every
priority-scheduling band below every background-scheduling band -- then,
within a class, by the band's peak thickness (its widest moment over the
window), then by total volume, then by name. Priority scheduling sits at
the base because it is the load the account's priority concurrency is
about and the work someone is actively waiting on, and because background
scheduling rides above it, which is also how the gateway admits it (only
as capacity allows). The order puts the big, steady bands at the base of
the stack where the eye reads totals, and the thin ones cap it.

WHY EACH MARK EXISTS.

Stacked area   the core mark: the average concurrent inference requests per
               time column, one band per model, agent and class. Average,
               not per-column maxima, so the stack's height is an honest
               load rather than a sum of peaks that never co-occurred.
Grey outline   the busiest moment in each column. Averages smooth away the
               bursts the reader is looking for, so the envelope carries
               the peaks the areas cannot. It is the envelope of the
               account total: the stack's own height at its worst.
Open circle    the exact peak, and when it happened.
Dashed rule    the account's priority concurrency, drawn when the number is
               established, and labelled with its source so a fetched
               account value can never be mistaken for a declared one:
               "priority concurrency 12 (account, plan apex)" when the
               capacity payload is cached, "priority concurrency
               (--plan max = 4)" when the user declared it and the
               account had no number. The capacity API is the first
               authority: a --plan declaration is the fallback, and one
               the account contradicts is set aside with a note. It is
               an account-wide limit, so it is drawn once, against the
               one total it governs.
Flare ticks    one tick per request the gateway rejected, hung from the top
               edge of the plot area at the moment it died. The hover
               title names the agent whose request died; on the single
               canvas the tick itself is anonymous without that title,
               which is a price paid for one graph.
Direct labels  each band is named at its right-hand edge, in its own
               colour, at the height the band left the stack, with a
               hairline leader when the band ended early. The name carries
               the agent whenever more than one agent has traffic, so
               attribution never needs a second picture. There is no
               legend box: a legend makes the eye travel.
Footer         provenance: window, version, which numbers are measured
               (per-model rejection deadlines, with sample counts) and
               which are declared (--plan). Rows are wrapped to the canvas
               width and the canvas grows downward when there is more to
               say: provenance is never cut to fit.

WHAT WAS CHOSEN NOT TO DRAW.

No per-agent frames. The previous design answered "whose traffic is
this" by drawing a row of pictures -- the whole machine, then one per
agent, on a shared scale. Splitting one number across several canvases
made each thinner, and the account total -- the number the gateway
prices -- lived only in the first of them. The row is gone; attribution
moved into the band names, and the one stack carries the total at every
moment of the window.
No universal admission deadline. The previous chart drew one wall at 120.2s
for every model; that is one model's constant (another model's requests
were rejected at 60.3s). Rejections are per-request ticks, and the footer
reports each model's observed deadline, measured, with its n.
No advisory concurrency rule. The old "advised 8" was headroom under a cap
of 10 that turned out to be one plan's default, not a gateway constant.
Advice that depends on the plan is being re-derived per plan elsewhere in
the tool; a number drawn here would be a guess wearing a ruler.
No overflow-capacity rule. Only one plan documents overflow capacity, it
is not declarable yet, and a missing line is honest where a wrong one is
not.
No priority rule at all when nothing is established: the footer says the
number is specific to the account and cannot be read from these logs, and
points at `luna-doctor capacity` and --plan.
No latency picture. It shared a frame with the old bars and halved the
space each story got; latency keeps its own home in `analyze`.
No plot fills, plot borders, or legend: the data is the ink.

Two deliberate trades. The folded tail: bands below legibility fold into
one "other" band per scheduling class and agent, so an agent's identity
survives the fold while the model names beneath it do not -- the residue
is, by definition, the traffic no label could name. And when only one
agent has traffic in the window the agent suffix is dropped from every
name: with one agent there is nothing to distinguish, and the footer's
"read from pi logs" already says whose traffic it is. A window where no
agent had timed traffic still draws the one plot area over the requested
window -- empty axes and the honest footer, never a bare message.
"""

from luna_doctor.plan import PLAN_PRIORITY, resolve_capacity
from luna_doctor.png import png_from
from luna_doctor.scene import Circle, Line, Rect, Scene, Text, svg_from


def _system_rasterize(svg_path: str, png_path: str, width: int):
    """Rasterise the written SVG with a system tool, best first.

    The SVG names a real font stack, so a system rasteriser draws real
    type; the package's own `png_from` carries only a 95-glyph bitmap font,
    which renders the same chart in blocky pixel type. These tools are
    optional extras detected at runtime - the stdlib guarantee holds:
    when none exists the caller falls back to `png_from` and nothing
    about the contract changes. Returns True when `png_path` was written.
    """
    import shutil
    import subprocess
    candidates = []
    if shutil.which("rsvg-convert"):
        candidates.append(["rsvg-convert", "-w", str(width), svg_path,
                           "-o", png_path])
    if shutil.which("inkscape"):
        candidates.append(["inkscape", svg_path, "--export-type=png",
                           "--export-filename=" + png_path,
                           "--export-width=" + str(width)])
    for name in ("magick", "convert"):
        if shutil.which(name):
            candidates.append([name, "-background", "white",
                               svg_path, png_path])
    if not candidates:
        return False
    for cmd in candidates:
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=30)
            if proc.returncode == 0 and os.path.isfile(png_path) \
                    and os.path.getsize(png_path) > 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


CHART_FONT = "ui-sans-serif,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"

# Colour carries meaning: one blue ramp for priority scheduling (one step
# per band, in volume order), a violet ramp for background scheduling,
# slate for each class's folded "other" residue. Red is reserved for
# rejected/overflowed requests. Each entry is (fill, label ink): pale fills
# keep a readable label by re-inking the name in the same hue.
PRIORITY_STYLE = [
    ("#173f8f", "#173f8f"),
    ("#8ab6f8", "#1d4ed8"),
    ("#3b82f6", "#2563eb"),
    ("#cfe2fb", "#3b82f6"),
]
PRIORITY_OTHER_STYLE = ("#94a3b8", "#64748b")
BACKGROUND_STYLE = [
    ("#5b21b6", "#5b21b6"),
    ("#a78bfa", "#7c3aed"),
    ("#c4b5fd", "#8b5cf6"),
]
BACKGROUND_OTHER_STYLE = ("#ddd6fe", "#7c3aed")
NAMED_PER_CLASS = 4      # bands named per class; the rest fold into "other"
MAX_LABEL_CHARS = 24

C = {
    "grid": "#e5e7eb",
    "axis": "#9ca3af",
    "text": "#111827",
    "muted": "#6b7280",
    "rule": "#374151",
    "flare": "#dc2626",
    "outline": "#9ca3af",
    "bg": "#ffffff",
}


def _fmt_dur(seconds):
    if seconds is None:
        return "n/a"
    if seconds < 1:
        return "%gms" % round(seconds * 1000)
    if seconds < 90:
        return "%.1fs" % seconds
    if seconds < 5400:
        return "%.1fm" % (seconds / 60)
    return "%.1fh" % (seconds / 3600)


def _est_w(s, size):
    """Conservative sans width of one text run, for gutter and key layout."""
    return len(s) * 0.56 * size


def _wrap_to_width(s, max_w, size):
    """Greedy word wrap measured in the same _est_w the layout trusts.

    Footer lines carry provenance, so a row that ran past the canvas would
    take its facts with it: every footer row is wrapped against the width
    it is drawn on.
    """
    out, cur = [], ""
    for word in s.split():
        trial = (cur + " " + word) if cur else word
        if _est_w(trial, size) <= max_w or not cur:
            cur = trial
        else:
            out.append(cur)
            cur = word
    if cur:
        out.append(cur)
    return out


def _display_name(model, tier):
    """Band label: the base model; background scheduling is marked (bg)."""
    m = model or "(unknown)"
    if tier == "background":
        if m.endswith("-background"):
            m = m[:-len("-background")]
        m = m + " (bg)"
    if len(m) > MAX_LABEL_CHARS:
        m = m[:MAX_LABEL_CHARS - 1] + "\u2026"
    return m


def _bands(reqs):
    """The global band list and the request-to-band map.

    A band is one model's traffic from one agent in one scheduling class,
    so the single stack can say whose traffic it is drawing: a model that
    runs on two agents is two bands, and a band keeps one colour everywhere.
    The busiest NAMED_PER_CLASS (model, agent) pairs per class get their own
    band; the rest fold into one "other" band per class and agent, which
    keeps the agent's identity while giving up the model names no label
    could fit.
    """
    multi = len({r.harness for r in reqs}) > 1
    per_class = {}
    for r in reqs:
        pairs = per_class.setdefault(r.tier, {})
        pair = (r.model, r.harness)
        pairs[pair] = pairs.get(pair, 0) + 1
    bands = []
    for tier, styles, other_style in (
            ("interactive", PRIORITY_STYLE, PRIORITY_OTHER_STYLE),
            ("background", BACKGROUND_STYLE, BACKGROUND_OTHER_STYLE)):
        counts = per_class.get(tier) or {}
        if not counts:
            continue
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        cap = min(NAMED_PER_CLASS, len(styles))
        for i, ((model, harness), _n) in enumerate(ranked[:cap]):
            fill, ink = styles[i]
            bands.append({"tier": tier, "model": model, "harness": harness,
                          "display": _band_label(_display_name(model, tier),
                                                 harness, multi),
                          "fill": fill, "ink": ink, "folded": None})
        rest = ranked[cap:]
        by_agent = {}
        for (model, harness), _n in rest:
            by_agent.setdefault(harness, []).append(model)
        for harness in sorted(by_agent):
            models = by_agent[harness]
            fill, ink = other_style
            plural = "s" if len(models) != 1 else ""
            label = "other (%d model%s)" % (len(models), plural)
            bands.append({"tier": tier, "model": None, "harness": harness,
                          "display": _band_label(label, harness, multi),
                          "fill": fill, "ink": ink,
                          "folded": models})
    band_of = {}
    for b in bands:
        if b["folded"] is None:
            band_of[(b["tier"], b["model"], b["harness"])] = (
                b["tier"], b["model"], b["harness"])
        else:
            for model in b["folded"]:
                band_of[(b["tier"], model, b["harness"])] = (
                    b["tier"], None, b["harness"])
    return bands, band_of


def _band_label(name, harness, multi):
    """A band's name: the model (and class) plus the agent when it matters.

    The agent suffix is the whole attribution mechanism of the single
    stack, so it is drawn whenever more than one agent has traffic -- the
    one case where "whose traffic" has more than one answer.
    """
    if not multi:
        return name
    return "%s \u00b7 %s" % (name, harness)


def _panel_series(reqs, bands, band_of, t0, t1, n_cols):
    """Column statistics for the whole window.

    `means[key][k]` is the time-integral of that band's concurrent-request
    count over column k (divide by the column width for the average);
    `colmax[k]` is the most concurrent inference requests at any instant in
    column k; `peak`/`peak_t` the exact maximum and the first instant it
    was reached. One event sweep computes all of it. (The name is pinned by
    tests that call this directly.)
    """
    keys = [(b["tier"], b["model"], b["harness"]) for b in bands]
    means = {key: [0.0] * n_cols for key in keys}
    colmax = [0] * n_cols
    col_w = (t1 - t0) / n_cols
    events = []
    for r in reqs:
        key = band_of.get((r.tier, r.model, r.harness))
        if key is None:
            continue          # a class with no band has no stack to draw
        events.append((max(t0, r.start), 1, key))
        events.append((min(t1, r.end), -1, key))
    events.sort(key=lambda e: (e[0], e[1]))
    level = {}
    state = {"total": 0, "cur": t0, "peak": 0, "peak_t": t0}

    def _accum(a, b):
        # Spread the current levels over the columns covered by [a, b).
        # The walk is bounded by the column count, not by the cursor: the
        # final column's nominal right edge is t0 + n_cols * col_w, which
        # float rounding can place an ulp left of t1, so a cursor that
        # recomputes that edge for an a already sitting on it resolves
        # e == a and never advances -- the first version of this loop hung
        # exactly there, on windows as small as 179 requests. Here the
        # column index moves forward every pass, the final column owns
        # everything from wherever the walk reaches to b, and a cursor
        # already past the last edge folds into that final column and
        # exits, so every input terminates.
        if b <= a:
            return
        k = int((a - t0) / col_w) if col_w > 0 else 0
        if k < 0:
            k = 0
        if k >= n_cols:
            k = n_cols - 1
        while a < b:
            if k >= n_cols - 1:
                e = b          # the final column owns the rest of the window
            else:
                e = min(b, t0 + (k + 1) * col_w)
            span = e - a
            if span > 0:
                for key, lv in level.items():
                    if lv:
                        means[key][k] += lv * span
                if state["total"] > colmax[k]:
                    colmax[k] = state["total"]
            if e >= b:
                break
            a = e
            k += 1

    for t, d, key in events:
        if t > state["cur"]:
            _accum(state["cur"], t)
            state["cur"] = t
        level[key] = level.get(key, 0) + d
        state["total"] += d
        if state["total"] > state["peak"]:
            state["peak"] = state["total"]
            state["peak_t"] = t
    if t1 > state["cur"]:
        _accum(state["cur"], t1)
    return means, colmax, state["peak"], state["peak_t"]


def build_chart_scene(reqs, m, hours, width=1120, height=640, capacity=None):
    """Collect the chart as an ordered list of shapes.

    `m` is the metrics dict from analyse(); `reqs` the raw requests, because
    the picture draws one tick per rejected request. `capacity` is a
    plan.Capacity: the rule is drawn when it carries a number, from the
    account's fetched config.priority_lanes (source "account", the first
    authority) or from `--plan` (source "plan", the fallback when the
    account has no number), and the label says which. None means nothing
    was established, and then no priority rule is drawn. Shape order
    below is paint order.
    """
    timed_reqs = timed(reqs)

    cap = capacity
    rule_n = cap.priority if cap is not None and cap.priority is not None else None
    rule_src = cap.source if cap is not None else "none"
    declared_txt = cap.declared if cap is not None else ""
    declared_named = bool(declared_txt) and declared_txt.lower() in PLAN_PRIORITY
    acct_n = cap.account_priority if cap is not None else None
    acct_plan = cap.account_plan if cap is not None else None
    acct_stale = cap.account_stale if cap is not None else False
    # The capacity API is the first authority: a --plan that disagrees
    # with the account's number was set aside in resolve_capacity, and
    # the footer says so -- the declaration is named as ignored, never
    # silently dropped.
    conflict = (rule_src == "account" and cap.declared_ignored)

    if timed_reqs:
        t0 = min(r.start for r in timed_reqs)
        t1 = max(r.end for r in timed_reqs)
        # t1 > t0 always: every timed request has a positive duration, so
        # the request holding the latest end carries t1 past t0. No floor is
        # needed, and one would lie: span also sizes the averaging column,
        # and a window shorter than the floor would scale every band's
        # height by (t1 - t0).
    else:
        # No agent had timed traffic in this window. The picture is still
        # one plot area, drawn over the requested window -- empty axes and
        # the honest footer -- never a bare message.
        t1 = time.time()
        t0 = t1 - hours * 3600
    span = t1 - t0

    bands, band_of = _bands(timed_reqs)
    # the agents in volume order, for the footer's provenance
    by_harness_n = {}
    for r in timed_reqs:
        by_harness_n[r.harness] = by_harness_n.get(r.harness, 0) + 1
    harnesses = sorted(by_harness_n, key=lambda h: (-by_harness_n[h], h))

    # ---- geometry ---------------------------------------------------------
    pad_l, pad_r = 56, 18
    title_y, sub_y, key_y = 30, 53, 74
    top = 98
    bot = max(top + 100, height - 132)
    xlab_y = bot + 20

    label_w = max([_est_w(b["display"], 10) for b in bands] + [40.0])
    gutter = max(64.0, min(260.0, label_w + 14))
    plot_w = max(120.0, width - pad_l - pad_r - gutter)
    px = pad_l

    n_cols = max(48, min(480, round(span / 240)))
    n_cols = max(24, min(n_cols, int(plot_w)))
    col_s = span / n_cols

    # one series for the whole machine: the stack's height is the account
    # total, and one sweep computes every band's share of it
    means, colmax, peak_total, peak_ts = _panel_series(
        timed_reqs, bands, band_of, t0, t1, n_cols)

    # the one stack order, read off the series itself: scheduling class
    # first (priority below background), then peak thickness, then volume,
    # then name -- deterministic, big steady bands at the base, thin ones
    # capping the stack
    def _stack_order(b):
        mv = means.get((b["tier"], b["model"], b["harness"])) or []
        pk = max([v / col_s for v in mv] or [0.0])
        return (0 if b["tier"] == "interactive" else 1,
                -pk, -sum(mv), b["display"])
    bands = sorted(bands, key=_stack_order)

    # one vertical scale: the peak of everything, plus the resolved
    # priority concurrency when there is one (a rule must be drawable, so
    # it joins the scale instead of clipping off the plot)
    y_raw = max(1, peak_total, rule_n or 0)
    step = 1 if y_raw <= 10 else (2 if y_raw <= 30 else 5)
    head = max(2, y_raw // 8 + 1)
    y_max = ((y_raw + head + step - 1) // step) * step
    y_vals = list(range(0, y_max + 1, step))

    def y_of(v):
        return bot - (v / y_max) * (bot - top)

    def col_x(k):
        return px + plot_w * k / n_cols

    def x_of(ts):
        return px + (ts - t0) / span * plot_w

    n_reqs = m["requests"]
    dead = (m["by_outcome"].get("admission_timeout", 0)
            + m["by_outcome"].get("org_concurrency_limit", 0))
    stalls = m["by_outcome"].get("client_idle_timeout", 0)
    bg_n = m["by_tier"].get("background", 0)
    bg_pct = round(100.0 * bg_n / max(1, n_reqs))

    shapes = []
    add = shapes.append

    # ---- title block ------------------------------------------------------
    add(Text(pad_l, title_y, "luna-doctor \u00b7 last %s" % human_window(hours),
             size=20, weight="600", fill=C["text"]))
    if timed_reqs:
        add(Text(pad_l, sub_y,
                 "%d requests \u00b7 peak %d concurrent inference requests "
                 "\u00b7 %d rejected at admission \u00b7 %d%% on background scheduling"
                 % (n_reqs, m["peak_concurrency"], dead, bg_pct),
                 size=12.5, fill=C["muted"]))
    else:
        add(Text(pad_l, sub_y,
                 "no timed requests in this window (try a wider --since)",
                 size=12.5, fill=C["muted"]))
    # the hue key: three words, each in the colour it names. No boxes.
    kx = pad_l
    for word, colour in (("priority", PRIORITY_STYLE[0][1]),
                         ("background scheduling", BACKGROUND_STYLE[0][1]),
                         ("rejected", C["flare"])):
        add(Text(kx, key_y, word, size=11, fill=colour))
        kx += _est_w(word, 11) + 20

    # ---- the one plot area -------------------------------------------------
    x_ticks = 6 if plot_w >= 260 else (4 if plot_w >= 120 else 3)
    # Tick labels are clock times until the window holds more than two days:
    # up to there the day cycle wraps at most once, so a repeated HH:MM is
    # resolved by position; past it most clock times occur twice and the
    # label becomes the day.
    day_labels = span > 2 * 86400

    # muted furniture, under the data: level hairlines and time hairlines
    for v in y_vals:
        if v:
            y = y_of(v)
            add(Line(px, y, px + plot_w, y, C["grid"], 1.0, (), 0.8))
    for i in range(x_ticks + 1):
        x = x_of(t0 + span * i / x_ticks)
        add(Line(x, top, x, bot, C["grid"], 1.0, (), 0.7))

    # the stack: one rect run per band, averages per column, in the fixed
    # band order; the running total is the account's in-flight requests
    cum = [0.0] * n_cols
    label_entries = []      # (band, y0 at the band's end, leader x or None)
    for b in bands:
        key = (b["tier"], b["model"], b["harness"])
        mvals = means.get(key)
        if not mvals:
            continue
        segs = []           # (y_top, y_bot) per column, None when nothing
        last_k, last_y0 = None, None
        for k in range(n_cols):
            base = cum[k]
            v = mvals[k] / col_s
            cum[k] = base + v
            if v <= 0:
                segs.append(None)
                continue
            y_t, y_b = y_of(base + v), y_of(base)
            segs.append((y_t, y_b))
            if y_b - y_t < 0.15:
                continue      # sub-pixel bands stay in the stack, not the ink
            last_k, last_y0 = k, (y_t + y_b) / 2.0
        # merge columns whose band segment rounds to the same geometry
        k = 0
        while k < n_cols:
            seg = segs[k]
            if seg is None:
                k += 1
                continue
            q = (round(seg[0] * 4), round(seg[1] * 4))
            j = k
            while (j + 1 < n_cols and segs[j + 1] is not None
                   and (round(segs[j + 1][0] * 4),
                        round(segs[j + 1][1] * 4)) == q):
                j += 1
            add(Rect(col_x(k), seg[0], col_x(j + 1) - col_x(k),
                     seg[1] - seg[0], fill=b["fill"]))
            k = j + 1
        if last_k is not None:
            at_edge = last_k >= n_cols - 1
            label_entries.append((b, last_y0,
                                  None if at_edge else col_x(last_k + 1)))

    # the busiest moment per column, as one step outline over the stack:
    # the envelope of the account's total in-flight requests
    k = 0
    while k < n_cols:
        v = colmax[k]
        j = k
        while j + 1 < n_cols and colmax[j + 1] == v:
            j += 1
        if v > 0:
            y = y_of(v)
            add(Line(col_x(k), y, col_x(j + 1), y,
                     C["outline"], 1.1, (), 0.9))
        k = j + 1
    for k in range(1, n_cols):
        a, b2 = colmax[k - 1], colmax[k]
        if a != b2:
            add(Line(col_x(k), y_of(a), col_x(k), y_of(b2),
                     C["outline"], 1.1, (), 0.9))

    # the axis under the stack
    add(Line(px, bot, px + plot_w, bot, C["axis"]))

    # the exact peak, marked where it happened
    if peak_total > 0:
        peak_txt = datetime.fromtimestamp(peak_ts, timezone.utc).strftime("%H:%M UTC")
        add(Circle(x_of(peak_ts), y_of(peak_total), 2.6, fill=C["bg"],
                   stroke=C["text"], stroke_width=1.2,
                   title="peak %d concurrent inference requests \u00b7 %s"
                         % (peak_total, peak_txt)))

    # level labels
    for v in y_vals:
        add(Text(pad_l - 10, y_of(v) + 3.5, str(v), size=10.5,
                 fill=C["muted"], anchor="end"))

    # time ticks: marks on the axis, labels under it. A short window puts two
    # ticks in the same minute, and two identical labels on one axis read as a
    # rendering bug - so the format gains seconds before the ticks are reduced,
    # and only if that still repeats does the axis carry fewer, unique ticks.
    fmt = "%b %d" if day_labels else "%H:%M"
    ticks = [t0 + span * i / x_ticks for i in range(x_ticks + 1)]
    def _labels(f, ts_list):
        return [datetime.fromtimestamp(t, timezone.utc).strftime(f) for t in ts_list]
    labels = _labels(fmt, ticks)
    if not day_labels and len(set(labels)) != len(labels):
        fmt = "%H:%M:%S"
        labels = _labels(fmt, ticks)
        while len(set(labels)) != len(labels) and len(ticks) > 2:
            ticks = ticks[::2]
            labels = _labels(fmt, ticks)
    for ts, label in zip(ticks, labels):
        x = x_of(ts)
        add(Line(x, bot, x, bot + 5, C["axis"]))
        add(Text(x, xlab_y, label, size=9.5, anchor="middle", fill=C["muted"]))

    # direct labels: one per band at the right-hand edge, in the band's
    # own ink, at the height the band left the stack; a hairline leader
    # connects an early-ending band to its name. Gaps are enforced so
    # the label column can never collide with itself.
    ys = []
    for b, y0, _lx in label_entries:
        y = y0
        if ys and y < ys[-1] + 11.0:
            y = ys[-1] + 11.0
        ys.append(y)
    if ys and ys[-1] > bot - 7:
        shift = ys[-1] - (bot - 7)
        ys = [max(top + 7.0, y - shift) for y in ys]
        for i in range(1, len(ys)):
            if ys[i] < ys[i - 1] + 11.0:
                ys[i] = ys[i - 1] + 11.0
    for (b, y0, lx), y in zip(label_entries, ys):
        if lx is None or px + plot_w - lx <= 4:
            continue
        if px + plot_w - lx <= 60:
            # a short leader: the band ended near the right edge, so the line
            # joins the name to the band's own end
            add(Line(lx, y0, px + plot_w + 2, y, C["axis"], 0.7, (), 0.6))
        else:
            # a band that ended early would need a long diagonal across the data
            # to reach its name. A stub at the edge names it without drawing
            # over the picture.
            add(Line(px + plot_w - 10, y, px + plot_w + 2, y, C["axis"], 0.7, (), 0.6))
    for (b, y0, lx), y in zip(label_entries, ys):
        add(Text(px + plot_w + 6, y + 3.2, b["display"], size=10, fill=b["ink"]))

    # ---- flare ticks: one per rejected or overflowed request ---------------
    # Hung from the top edge of the plot area at the moment the request
    # died; the hover title names the agent whose request it was.
    for r in timed_reqs:
        if r.demoted:
            ts, kind = r.start, "overflowed to background scheduling"
        elif r.outcome == "admission_timeout":
            ts, kind = r.end, "rejected at admission"
        elif r.outcome == "org_concurrency_limit":
            ts, kind = r.end, "rejected: concurrency limit"
        else:
            continue
        x = x_of(ts)
        title = "%s \u00b7 %s \u00b7 %s \u00b7 waited %s" % (
            kind, r.harness, r.model or "(unknown)", _fmt_dur(r.duration))
        add(Line(x, top, x, top + 7, C["flare"], 1.4, (), 0.95))
        # a Line cannot carry a tooltip, so an invisible hover spot does
        add(Circle(x, top + 3.5, 3, fill=None, stroke=None, title=title))

    # ---- the resolved priority concurrency, against the one stack ----------
    if rule_n is not None:
        ry = y_of(rule_n)
        add(Line(px, ry, px + plot_w, ry, C["rule"], 1.0, (5, 4), 0.9))
        if rule_src == "account":
            rule_label = "priority concurrency %d (account" % rule_n
            if acct_plan:
                rule_label += ", plan %s" % acct_plan
            rule_label += ")"
            if acct_stale:
                # a rule drawn from a stale payload is still the best number
                # available, but it is never drawn without saying so
                rule_label += " \u00b7 stale cache"
        elif declared_named:
            rule_label = "priority concurrency (--plan %s = %d)" % (
                declared_txt, rule_n)
        else:
            rule_label = "priority concurrency (--plan %d)" % rule_n
        ly = ry - 5 if ry - 15 >= top else ry + 12
        # The rule crosses the stack, and its own ink on a dark band is
        # unreadable. A white plate behind the label keeps the text legible on
        # whatever the bands are doing underneath, without hiding the rule.
        plate_w = 5.4 * len(rule_label) + 6
        add(Rect(px + 2, ly - 10, plate_w, 14, fill="#ffffff", fill_opacity=0.88))
        add(Text(px + 5, ly, rule_label, size=10, fill=C["rule"]))

    add(Text(width - pad_r, xlab_y, "UTC", size=9.5, anchor="end", fill=C["muted"]))

    # ---- footer: provenance, measured vs declared vs fetched ---------------
    if rule_src == "account":
        line1 = "account: priority concurrency %d" % rule_n
        if acct_plan:
            line1 += " (plan %s)" % acct_plan
        line1 += (" \u2014 fetched from api.lunaroute.com, served from disk cache; "
                  "the account's own number, never a declaration")
        if conflict:
            # capacity API first: the declaration was set aside because the
            # account reports otherwise, and the footer says so
            line1 += (" Note: your --plan %s declaration was ignored \u2014 the "
                      "account reports priority concurrency %d%s."
                      % (declared_txt, acct_n,
                         ", plan %s" % acct_plan if acct_plan else ""))
        if acct_stale:
            line1 += "; the cache is stale, so the rule may be out of date"
    elif rule_src == "plan":
        if declared_named:
            line1 = ("declared: priority concurrency %d (--plan %s) \u2014 "
                     "LunaRoute's published plan default; an org-level override "
                     "on the dashboard Metrics page wins. A declaration, not a "
                     "measurement." % (rule_n, declared_txt))
        else:
            line1 = ("declared: priority concurrency %d (--plan %s) \u2014 "
                     "declared by you; an org-level override on the dashboard "
                     "Metrics page wins. A declaration, not a measurement."
                     % (rule_n, declared_txt))
        if cap is not None and cap.account_present:
            # the payload served but had no number, so the declaration
            # stood as the fallback -- the footer keeps the two states apart
            line1 += (" The account's capacity payload reports no priority "
                      "concurrency, so the declaration stands.")
    elif cap is not None and cap.account_present:
        line1 = ("no priority concurrency rule drawn: the account's capacity "
                 "payload is cached but reports no priority concurrency; "
                 "declare it with chart --plan <plan name or number>")
    else:
        line1 = ("no priority concurrency rule drawn: priority concurrency is "
                 "specific to the account and cannot be read from these logs "
                 "\u2014 fetch the account's real number with `luna-doctor capacity` "
                 "(the chart reads its disk cache), or declare it with "
                 "chart --plan <plan name or number>")

    if not timed_reqs:
        line2 = ("reading: no timed request in this window; the axes span the "
                 "requested window and nothing was measured \u2014 try a wider "
                 "--since or run luna-doctor discover")
    elif rule_n is not None and m["peak_concurrency"] > rule_n:
        kind = "declared" if rule_src == "plan" else "account"
        if dead:
            line2 = ("reading: the peak of %d concurrent inference requests "
                     "exceeded the %s priority concurrency %d; %d "
                     "request(s) waited for admission and were rejected"
                     % (m["peak_concurrency"], kind, rule_n, dead))
        else:
            line2 = ("reading: the peak of %d concurrent inference requests "
                     "exceeded the %s priority concurrency %d; no request "
                     "was rejected in this window"
                     % (m["peak_concurrency"], kind, rule_n))
    elif rule_n is not None:
        line2 = ("reading: the peak of %d concurrent inference requests stayed "
                 "within the %s priority concurrency %d"
                 % (m["peak_concurrency"],
                    "declared" if rule_src == "plan" else "account", rule_n))
    elif dead:
        line2 = ("reading: %d request(s) waited for admission and were rejected; "
                 "declare --plan to see them against your priority concurrency"
                 % dead)
    else:
        line2 = "reading: no request was rejected at admission in this window"

    pm = m.get("per_model_deadlines") or {}
    bits = []
    for model in sorted(pm):
        d = pm[model]
        if d.get("status") == "observed" and d.get("deadline_s") is not None:
            bits.append("%s %.1fs (n=%d)" % (model, d["deadline_s"], d["n"]))
    for model in sorted(pm):
        d = pm[model]
        if d.get("status") != "observed":
            bits.append("%s: unknown (n=%d)" % (model, d.get("seen_n", 0)))
    if bits:
        line3 = ("measured: observed admission deadline per model \u2014 %s "
                 "\u00b7 client idle budget %.0fs: %d silent stall(s)"
                 % (" \u00b7 ".join(bits), CLIENT_IDLE_TIMEOUT_S, stalls))
    else:
        line3 = ("measured: no admission deadline to report (no request was "
                 "rejected at admission in this window) \u00b7 client idle "
                 "budget %.0fs: %d silent stall(s)"
                 % (CLIENT_IDLE_TIMEOUT_S, stalls))

    if harnesses:
        read_from = "read from %s logs" % ", ".join(harnesses)
    else:
        read_from = "no agent had a timed request in this window"
    line4 = ("bands: average concurrent inference requests per %s column "
             "\u00b7 grey outline: the busiest moment per column \u00b7 a request "
             "waiting for admission counts as open until it is served or "
             "rejected \u00b7 luna-doctor v%s \u00b7 last %s \u00b7 %s"
             % (_fmt_dur(col_s), VERSION, human_window(hours), read_from))

    # the footer is wrapped, and the canvas grows to keep it: provenance is
    # mandatory content, so a window with more to report than four rows hold
    # (many models' measured deadlines, a long declared-plan note) extends
    # the canvas downward instead of clipping. The plot above keeps its
    # geometry; only the footer band moves.
    rows = []
    for line in (line1, line2, line3, line4):
        rows.extend(_wrap_to_width(line, width - pad_l - pad_r, 10))
    need = xlab_y + 16 + (len(rows) - 1) * 16 + 24
    if need > height:
        height = need
    y = height - 22 - (len(rows) - 1) * 16
    for row in rows:
        add(Text(pad_l, y, row, size=10, fill=C["muted"]))
        y += 16

    return Scene(width, height, C["bg"], shapes, font=CHART_FONT)


def svg_chart(reqs, m, hours, width=1120, height=640, capacity=None):
    return svg_from(build_chart_scene(reqs, m, hours, width, height, capacity))


def cmd_chart(args) -> int:
    try:
        hours = window_hours(args.since, args.now)
    except ValueError as e:
        print("luna-doctor: %s" % e, file=sys.stderr)
        return 2
    # --plan is a declaration, and a malformed one is a usage error, not a
    # guess: the message lists what is accepted, and nothing is drawn from
    # an unreadable number. The account source beside it is the capacity
    # cache (the chart never waits on the 4-17s endpoint; `luna-doctor
    # capacity` refreshes it), and a collection failure there is unknown,
    # never an error.
    try:
        account_payload = sources.fetch_capacity(False)
    except Exception:
        account_payload = None
    try:
        capacity = resolve_capacity(args, account_payload)
    except ValueError as e:
        print("luna-doctor: %s" % e, file=sys.stderr)
        return 2
    harnesses = [h.strip() for h in args.harness.split(",") if h.strip()]
    raw_pi = getattr(args, "pi_root", None)
    raw_claude = getattr(args, "claude_root", None)
    # A flag that named a directory must not silently fall back to $HOME:
    # the user asked for a mirror, and reading the local store instead would
    # answer a question they did not ask.
    pi_roots = _check_roots(raw_pi, "--pi-root") if raw_pi else None
    claude_roots = _check_roots(raw_claude, "--claude-root") if raw_claude else None
    reqs = collect(hours, harnesses, pi_roots, claude_roots)
    m = analyse(reqs, hours)
    # one scene, two pictures: the PNG is the same scene the SVG serialises,
    # rasterised by the package's own renderer, so the two cannot disagree
    # and no new dependency enters. --no-image suppresses the PNG; the
    # default writes it beside the SVG under the same stem because the
    # raster is the one a chat, a note or a doc can paste inline.
    scene = build_chart_scene(reqs, m, hours, width=args.width,
                              height=args.height, capacity=capacity)
    svg = svg_from(scene)

    out = args.out
    if not out:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = "luna-doctor-%s.svg" % stamp
    try:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(svg)
    except OSError as e:
        print("luna-doctor: cannot write %s: %s" % (out, e), file=sys.stderr)
        return 2

    png_out = None
    if not getattr(args, "no_image", False):
        png_out = os.path.splitext(out)[0] + ".png"
        # real type first: the SVG names a font stack, so a system
        # rasteriser renders it properly; the package's bitmap renderer
        # (95 ASCII glyphs, everything folded) is the everywhere-fallback
        if not _system_rasterize(os.path.abspath(out), png_out, args.width):
            try:
                with open(png_out, "wb") as fh:
                    fh.write(png_from(scene))
            except OSError as e:
                # additive surface: a failed PNG write loses the inline image,
                # never the chart and never the exit code
                print("luna-doctor: cannot write %s: %s" % (png_out, e),
                      file=sys.stderr)
                png_out = None
    # the paths ride on the namespace so a caller that ran the chart as
    # part of a bigger whole (the bare default run) can name or open them
    args._svg_path = os.path.abspath(out)
    args._png_path = os.path.abspath(png_out) if png_out else None

    if args.json:
        doc = {
            "path": os.path.abspath(out),
            "bytes": len(svg),
            "metrics": m,
            "plan": {"priority": capacity.priority,
                     "source": capacity.source,
                     "declared": capacity.declared,
                     "declared_ignored": capacity.declared_ignored,
                     "account_present": capacity.account_present,
                     "account_priority": capacity.account_priority,
                     "account_plan": capacity.account_plan,
                     "account_stale": capacity.account_stale,
                     "account_note": capacity.account_note},
        }
        if png_out:
            doc["png_path"] = os.path.abspath(png_out)
            doc["png_bytes"] = os.path.getsize(png_out)
        print(json.dumps(envelope("chart", doc, contract=CONTRACT), indent=2))
    else:
        dead = (m["by_outcome"].get("admission_timeout", 0)
                + m["by_outcome"].get("org_concurrency_limit", 0))
        print("wrote %s  (%.0f KB)" % (os.path.abspath(out), len(svg) / 1024))
        if png_out:
            print("wrote %s  (%.0f KB) - the inline image, paste it anywhere"
                  % (os.path.abspath(png_out), os.path.getsize(png_out) / 1024))
        print("  %d requests, peak concurrency %d, %d rejected at admission"
              % (m["requests"], m["peak_concurrency"], dead))
        if capacity.known:
            if capacity.source == "account":
                who = "account"
                if capacity.account_plan:
                    who += ", plan %s" % capacity.account_plan
                if capacity.account_stale:
                    who += ", stale cache"
                print("  priority concurrency %d (%s): rule drawn"
                      % (capacity.priority, who))
                if capacity.declared_ignored:
                    print("  the --plan %s declaration was ignored: the account "
                          "reports priority concurrency %d%s"
                          % (capacity.declared, capacity.account_priority,
                             " (plan %s)" % capacity.account_plan
                             if capacity.account_plan else ""))
            else:
                print("  priority concurrency %d (declared via --plan %s): rule drawn"
                      % (capacity.priority, capacity.declared))
                if capacity.account_present:
                    # the payload served but had no number, so the fallback
                    # stood -- say so rather than let the declaration look
                    # like the only source that was asked
                    print("  the account's capacity payload reports no priority "
                          "concurrency, so the declaration stands")
        elif capacity.account_present:
            print("  priority concurrency: the cached capacity payload reports "
                  "none; no rule drawn (declare it with --plan)")
        else:
            print("  priority concurrency: not established, no rule drawn "
                  "(fetch it with `luna-doctor capacity`, or declare it "
                  "with --plan)")
        if args.open:
            import webbrowser
            webbrowser.open("file://" + os.path.abspath(out))
    return 0
