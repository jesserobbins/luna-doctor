#!/usr/bin/env python3
"""`chart` command for luna-doctor: the concurrency picture.

Executed inside luna_doctor.cli's namespace (see the loader at the bottom of
that script), so it can use Request, collect, analyse, window_hours and the
measured constants directly. The picture is built as a Scene of shapes (see
luna_doctor.scene), then serialised; nothing here touches SVG syntax.

Design note -- what the picture is, why each mark exists, and what was
chosen not to draw.

WHAT. A row of stacked-area panels sharing one time axis and one vertical
scale: first the whole machine ("all agents"), then one panel per agent
(harness). Each panel stacks concurrent inference requests by model, split
by scheduling class: blues are priority scheduling, violets are background
scheduling (the `-background` model-id form). The purpose is the build-up:
at a glance the reader sees which agent piled up, on which models, in which
class, and whether that pile crossed what the account is allowed to run.

WHY EACH MARK EXISTS.

Stacked area   the core mark: the average concurrent inference requests per
               time column, one band per model and class. Average, not
               per-column maxima, so the stack's height is an honest load
               rather than a sum of peaks that never co-occurred.
Grey outline   the busiest moment in each column. Averages smooth away the
               bursts the reader is looking for, so the envelope carries
               the peaks the areas cannot.
Open circle    the exact peak, and when it happened.
Dashed rule    the account's priority concurrency, drawn only when the user
               declares it with --plan, and labelled with the declaration
               ("priority concurrency (--plan max = 4)") so a declared
               threshold can never be mistaken for a measured one. It is
               drawn on the aggregate panel only: it is an account-wide
               limit, and drawing it per agent would let a single agent
               look "over the limit" when the account was not.
Flare ticks    one tick per request the gateway rejected, hung from the
               top edge of the owning agent's panel at the moment it died.
               Red is reserved for rejection (and overflow); nothing else
               wears it.
Direct labels  each band is named at its right-hand edge, in its own
               colour, at the height the band left the panel, with a
               hairline leader when the band ended early. There is no
               legend box: a legend makes the eye travel.
Footer         provenance: window, version, which numbers are measured
               (per-model rejection deadlines, with sample counts) and
               which are declared (--plan). Rows are wrapped to the canvas
               width and the canvas grows downward when there is more to
               say: provenance is never cut to fit.

WHAT WAS CHOSEN NOT TO DRAW.

No universal admission deadline. The previous chart drew one wall at 120.2s
for every model; that is one model's constant (another model's requests
were rejected at 60.3s). Rejections are now per-request ticks, and the
footer reports each model's observed deadline, measured, with its n.
No advisory concurrency rule. The old "advised 8" was headroom under a cap
of 10 that turned out to be one plan's default, not a gateway constant.
Advice that depends on the plan is being re-derived per plan elsewhere in
the tool; a number drawn here would be a guess wearing a ruler.
No overflow-capacity rule. Only one plan documents overflow capacity, it
is not declarable yet, and a missing line is honest where a wrong one is
not.
No priority rule at all when nothing is declared: the footer says the
number is specific to the account and cannot be read from these logs.
No latency panel. It shared a frame with the old bars and halved the space
each story got; latency keeps its own home in `analyze`.
No panel fills, panel borders, or legend: the data is the ink.
"""

from luna_doctor.plan import PLAN_PRIORITY, resolve_capacity
from luna_doctor.scene import Circle, Line, Rect, Scene, Text, svg_from

CHART_FONT = "ui-sans-serif,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"

# Colour carries meaning: one blue ramp for priority scheduling (one step
# per model, in volume order), a violet ramp for background scheduling,
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
NAMED_PER_CLASS = 4      # models named per class; the rest fold into "other"
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

    Every panel stacks the same bands in the same order -- the priority
    block from the axis up, then the background scheduling block -- so
    shapes compare across panels and a model keeps one colour everywhere.
    The busiest NAMED_PER_CLASS models per class get their own band; the
    rest fold into one "other" band per class rather than a soup of slivers
    no label could name.
    """
    per_class = {}
    for r in reqs:
        models = per_class.setdefault(r.tier, {})
        models[r.model] = models.get(r.model, 0) + 1
    bands = []
    for tier, styles, other_style in (
            ("interactive", PRIORITY_STYLE, PRIORITY_OTHER_STYLE),
            ("background", BACKGROUND_STYLE, BACKGROUND_OTHER_STYLE)):
        counts = per_class.get(tier) or {}
        if not counts:
            continue
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        cap = min(NAMED_PER_CLASS, len(styles))
        for i, (model, _n) in enumerate(ranked[:cap]):
            fill, ink = styles[i]
            bands.append({"tier": tier, "model": model,
                          "display": _display_name(model, tier),
                          "fill": fill, "ink": ink, "folded": None})
        rest = ranked[cap:]
        if rest:
            fill, ink = other_style
            plural = "s" if len(rest) != 1 else ""
            bands.append({"tier": tier, "model": None,
                          "display": "other (%d model%s)" % (len(rest), plural),
                          "fill": fill, "ink": ink,
                          "folded": [model for model, _n in rest]})
    band_of = {}
    for b in bands:
        if b["folded"] is None:
            band_of[(b["tier"], b["model"])] = (b["tier"], b["model"])
        else:
            for model in b["folded"]:
                band_of[(b["tier"], model)] = (b["tier"], None)
    return bands, band_of


def _panel_series(panel_reqs, bands, band_of, t0, t1, n_cols):
    """Column statistics for one panel.

    `means[key][k]` is the time-integral of that band's concurrent-request
    count over column k (divide by the column width for the average);
    `colmax[k]` is the most concurrent inference requests at any instant in
    column k; `peak`/`peak_t` the exact maximum and the first instant it
    was reached. One event sweep computes all of it.
    """
    keys = [(b["tier"], b["model"]) for b in bands]
    means = {key: [0.0] * n_cols for key in keys}
    colmax = [0] * n_cols
    col_w = (t1 - t0) / n_cols
    events = []
    for r in panel_reqs:
        key = band_of.get((r.tier, r.model))
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
    the panels draw one tick per rejected request. `capacity` is a
    plan.Capacity (None means nothing was declared, and then no priority
    rule is drawn). Shape order below is paint order.
    """
    timed_reqs = timed(reqs)
    if not timed_reqs:
        return Scene(640, 120, C["bg"], [
            Text(20, 60, "luna-doctor: no timed requests in this window "
                         "(try a wider --since)", size=15, fill=C["text"]),
        ], font=CHART_FONT)

    cap = capacity
    declared_n = cap.priority if cap is not None and cap.priority is not None else None
    declared_txt = cap.declared if cap is not None else ""
    declared_named = bool(declared_txt) and declared_txt.lower() in PLAN_PRIORITY

    t0 = min(r.start for r in timed_reqs)
    t1 = max(r.end for r in timed_reqs)
    # t1 > t0 always: every timed request has a positive duration, so the
    # request holding the latest end carries t1 past t0. No floor is needed,
    # and one would lie: span also sizes the averaging column, and a window
    # shorter than the floor would scale every band's height by (t1 - t0).
    span = t1 - t0

    bands, band_of = _bands(timed_reqs)
    # agent panels in volume order, biggest next to the aggregate, so the
    # reader compares the shapes that matter most, side by side
    by_harness_n = {}
    for r in timed_reqs:
        by_harness_n[r.harness] = by_harness_n.get(r.harness, 0) + 1
    harnesses = sorted(by_harness_n, key=lambda h: (-by_harness_n[h], h))
    multi = len(harnesses) > 1
    panel_specs = [("all agents", timed_reqs)]
    if multi:
        for h in harnesses:
            panel_specs.append((h, [r for r in timed_reqs if r.harness == h]))
    panel_of_harness = {h: i + 1 for i, h in enumerate(harnesses)}

    # ---- geometry ---------------------------------------------------------
    pad_l, pad_r = 56, 18
    title_y, sub_y, key_y = 30, 53, 74
    top = 98
    bot = max(top + 100, height - 132)
    xlab_y = bot + 20

    label_w = max([_est_w(b["display"], 10) for b in bands] + [40.0])
    gutter = max(64.0, min(180.0, label_w + 12))
    row_w = width - pad_l - pad_r
    panel_w = row_w / len(panel_specs)
    plot_w = max(36.0, panel_w - gutter)

    n_cols = max(48, min(480, round(span / 240)))
    n_cols = max(24, min(n_cols, int(plot_w)))
    col_s = span / n_cols

    # one shared vertical scale: the peak of everything, plus the declared
    # priority concurrency when there is one (a declared rule must be
    # drawable, so it joins the scale instead of clipping off the panel)
    agg_series = _panel_series(timed_reqs, bands, band_of, t0, t1, n_cols)
    peak_total, peak_ts = agg_series[2], agg_series[3]
    y_raw = max(1, peak_total, declared_n or 0)
    step = 1 if y_raw <= 10 else (2 if y_raw <= 30 else 5)
    head = max(2, y_raw // 8 + 1)
    y_max = ((y_raw + head + step - 1) // step) * step
    y_vals = list(range(0, y_max + 1, step))

    def y_of(v):
        return bot - (v / y_max) * (bot - top)

    def col_x(px, k):
        return px + plot_w * k / n_cols

    def x_of(px, ts):
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
    add(Text(pad_l, sub_y,
             "%d requests \u00b7 peak %d concurrent inference requests "
             "\u00b7 %d rejected at admission \u00b7 %d%% on background scheduling"
             % (n_reqs, m["peak_concurrency"], dead, bg_pct),
             size=12.5, fill=C["muted"]))
    # the hue key: three words, each in the colour it names. No boxes.
    kx = pad_l
    for word, colour in (("priority", PRIORITY_STYLE[0][1]),
                         ("background scheduling", BACKGROUND_STYLE[0][1]),
                         ("rejected", C["flare"])):
        add(Text(kx, key_y, word, size=11, fill=colour))
        kx += _est_w(word, 11) + 20

    # ---- panels -----------------------------------------------------------
    x_ticks = 6 if plot_w >= 260 else (4 if plot_w >= 120 else 3)
    # Tick labels are clock times until the window holds more than two days:
    # up to there the day cycle wraps at most once, so a repeated HH:MM is
    # resolved by position; past it most clock times occur twice and the
    # label becomes the day.
    day_labels = span > 2 * 86400

    for pi, (title, preqs) in enumerate(panel_specs):
        px = pad_l + pi * panel_w
        if pi == 0:
            means, colmax, peak, peak_ts_p = agg_series
        else:
            means, colmax, peak, peak_ts_p = _panel_series(
                preqs, bands, band_of, t0, t1, n_cols)

        # header: the panel's name, and its own peak
        add(Text(px, top - 12, title, size=12, weight="600", fill=C["text"]))
        add(Text(px + plot_w, top - 12, "peak %d" % peak, size=10.5,
                 fill=C["muted"], anchor="end"))

        # muted furniture, under the data: level hairlines and time hairlines
        for v in y_vals:
            if v:
                y = y_of(v)
                add(Line(px, y, px + plot_w, y, C["grid"], 1.0, (), 0.8))
        for i in range(x_ticks + 1):
            x = x_of(px, t0 + span * i / x_ticks)
            add(Line(x, top, x, bot, C["grid"], 1.0, (), 0.7))

        # the stack: one rect run per band, averages per column
        cum = [0.0] * n_cols
        label_entries = []      # (band, y0 at the band's end, leader x or None)
        for b in bands:
            key = (b["tier"], b["model"])
            mvals = means.get(key)
            if not mvals:
                continue
            segs = []           # (y_top, y_bot) per column, None when nothing
            last_k, last_y0, leader_x = None, None, None
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
                add(Rect(col_x(px, k), seg[0], col_x(px, j + 1) - col_x(px, k),
                         seg[1] - seg[0], fill=b["fill"]))
                k = j + 1
            if last_k is not None:
                at_edge = last_k >= n_cols - 1
                label_entries.append((b, last_y0,
                                      None if at_edge else col_x(px, last_k + 1)))

        # the busiest moment per column, as one step outline
        k = 0
        while k < n_cols:
            v = colmax[k]
            j = k
            while j + 1 < n_cols and colmax[j + 1] == v:
                j += 1
            if v > 0:
                y = y_of(v)
                add(Line(col_x(px, k), y, col_x(px, j + 1), y,
                         C["outline"], 1.1, (), 0.9))
            k = j + 1
        for k in range(1, n_cols):
            a, b2 = colmax[k - 1], colmax[k]
            if a != b2:
                add(Line(col_x(px, k), y_of(a), col_x(px, k), y_of(b2),
                         C["outline"], 1.1, (), 0.9))

        # the axis under the stack
        add(Line(px, bot, px + plot_w, bot, C["axis"]))

        # the exact peak, marked where it happened
        peak_txt = datetime.fromtimestamp(peak_ts_p, timezone.utc).strftime("%H:%M UTC")
        add(Circle(x_of(px, peak_ts_p), y_of(peak), 2.6, fill=C["bg"],
                   stroke=C["text"], stroke_width=1.2,
                   title="peak %d concurrent inference requests \u00b7 %s"
                         % (peak, peak_txt)))

        # level labels, on the first panel only; the scale is shared
        if pi == 0:
            for v in y_vals:
                add(Text(pad_l - 10, y_of(v) + 3.5, str(v), size=10.5,
                         fill=C["muted"], anchor="end"))

        # time ticks: marks on each panel, labels under each panel
        for i in range(x_ticks + 1):
            ts = t0 + span * i / x_ticks
            x = x_of(px, ts)
            add(Line(x, bot, x, bot + 5, C["axis"]))
            fmt = "%b %d" if day_labels else "%H:%M"
            label = datetime.fromtimestamp(ts, timezone.utc).strftime(fmt)
            add(Text(x, xlab_y, label, size=9.5, anchor="middle", fill=C["muted"]))

        # direct labels: one per band at the right-hand edge, in the band's
        # own ink, at the height the band left the panel; a hairline leader
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
            if lx is not None and px + plot_w - lx > 4:
                add(Line(lx, y0, px + plot_w + 2, y, C["axis"], 0.7, (), 0.6))
        for (b, y0, lx), y in zip(label_entries, ys):
            add(Text(px + plot_w + 6, y + 3.2, b["display"], size=10, fill=b["ink"]))

    # ---- flare ticks: one per rejected or overflowed request ---------------
    # Hung from the top edge of the owning agent's panel at the moment the
    # request died, so the reader can see which agent was hit and when.
    for r in timed_reqs:
        if r.demoted:
            ts, kind = r.start, "overflowed to background scheduling"
        elif r.outcome == "admission_timeout":
            ts, kind = r.end, "rejected at admission"
        elif r.outcome == "org_concurrency_limit":
            ts, kind = r.end, "rejected: concurrency limit"
        else:
            continue
        pi = panel_of_harness.get(r.harness, 0)
        px = pad_l + pi * panel_w
        x = x_of(px, ts)
        title = "%s \u00b7 %s \u00b7 waited %s" % (kind, r.model or "(unknown)",
                                                  _fmt_dur(r.duration))
        add(Line(x, top, x, top + 7, C["flare"], 1.4, (), 0.95))
        # a Line cannot carry a tooltip, so an invisible hover spot does
        add(Circle(x, top + 3.5, 3, fill=None, stroke=None, title=title))

    # ---- the declared priority concurrency, on the aggregate panel only ----
    if declared_n is not None:
        px = pad_l
        ry = y_of(declared_n)
        add(Line(px, ry, px + plot_w, ry, C["rule"], 1.0, (5, 4), 0.9))
        if declared_named:
            rule_label = "priority concurrency (--plan %s = %d)" % (
                declared_txt, declared_n)
        else:
            rule_label = "priority concurrency (--plan %d)" % declared_n
        ly = ry - 5 if ry - 15 >= top else ry + 12
        add(Text(px + 5, ly, rule_label, size=10, fill=C["rule"]))

    add(Text(width - pad_r, xlab_y, "UTC", size=9.5, anchor="end", fill=C["muted"]))

    # ---- footer: provenance, measured vs declared --------------------------
    if declared_n is not None:
        if declared_named:
            line1 = ("declared: priority concurrency %d (--plan %s) \u2014 "
                     "LunaRoute's published plan default; an org-level override "
                     "on the dashboard Metrics page wins. A declaration, not a "
                     "measurement." % (declared_n, declared_txt))
        else:
            line1 = ("declared: priority concurrency %d (--plan %s) \u2014 "
                     "declared by you; an org-level override on the dashboard "
                     "Metrics page wins. A declaration, not a measurement."
                     % (declared_n, declared_txt))
    else:
        line1 = ("no priority concurrency rule drawn: priority concurrency is "
                 "specific to the account and cannot be read from these logs "
                 "\u2014 declare it with chart --plan <plan name or number>")

    if declared_n is not None and m["peak_concurrency"] > declared_n:
        if dead:
            line2 = ("reading: the peak of %d concurrent inference requests "
                     "exceeded the declared priority concurrency %d; %d "
                     "request(s) waited for admission and were rejected"
                     % (m["peak_concurrency"], declared_n, dead))
        else:
            line2 = ("reading: the peak of %d concurrent inference requests "
                     "exceeded the declared priority concurrency %d; no request "
                     "was rejected in this window"
                     % (m["peak_concurrency"], declared_n))
    elif declared_n is not None:
        line2 = ("reading: the peak of %d concurrent inference requests stayed "
                 "within the declared priority concurrency %d"
                 % (m["peak_concurrency"], declared_n))
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

    line4 = ("bands: average concurrent inference requests per %s column "
             "\u00b7 grey outline: the busiest moment per column \u00b7 a request "
             "waiting for admission counts as open until it is served or "
             "rejected \u00b7 luna-doctor v%s \u00b7 last %s \u00b7 read from %s logs"
             % (_fmt_dur(col_s), VERSION, human_window(hours),
                ", ".join(harnesses)))

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
    # an unreadable number.
    try:
        capacity = resolve_capacity(args)
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
    svg = svg_chart(reqs, m, hours, width=args.width, height=args.height,
                    capacity=capacity)

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

    if args.json:
        print(json.dumps(envelope("chart", {
            "path": os.path.abspath(out),
            "bytes": len(svg),
            "metrics": m,
            "plan": {"priority": capacity.priority,
                     "source": capacity.source,
                     "declared": capacity.declared},
        }, contract=CONTRACT), indent=2))
    else:
        dead = (m["by_outcome"].get("admission_timeout", 0)
                + m["by_outcome"].get("org_concurrency_limit", 0))
        print("wrote %s  (%.0f KB)" % (os.path.abspath(out), len(svg) / 1024))
        print("  %d requests, peak concurrency %d, %d rejected at admission"
              % (m["requests"], m["peak_concurrency"], dead))
        if capacity.known:
            print("  priority concurrency %d (declared via --plan %s): rule drawn"
                  % (capacity.priority, capacity.declared))
        else:
            print("  priority concurrency: not declared, no rule drawn "
                  "(declare it with --plan)")
        if args.open:
            import webbrowser
            webbrowser.open("file://" + os.path.abspath(out))
    return 0
