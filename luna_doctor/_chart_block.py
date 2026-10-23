#!/usr/bin/env python3
"""`chart` command for luna-doctor.

Executed inside luna_doctor.cli's namespace (see the loader at the bottom of
that script), so it can use Request, collect, analyse, window_hours and the
measured constants directly.

Emits a standalone SVG: no fonts, scripts or CDNs, so it opens anywhere and can
be pasted into a doc. Two panels share one time axis:

  top     how many requests were in flight, split by tier. This is the shape of
          your concurrent requests, which is what the gateway prices.
  bottom  every request's latency, on a log axis, with the two measured walls
          drawn as reference lines: the 120.2s admission deadline and the 300s
          client idle budget. Failures sit on those lines, which is the point:
          you can see a burst push its tail into the wall.

The picture is built as a Scene of shapes (see luna_doctor.scene), then
serialised. Nothing here touches SVG syntax directly, so the same scene can
later be rasterised by a second renderer without drifting from this one.
"""

from luna_doctor.scene import Circle, Line, Rect, Scene, Text, svg_from

CHART_COLORS = {
    "interactive": "#2563eb",
    "background": "#7c3aed",
    "admission": "#dc2626",
    "stall": "#d97706",
    "grid": "#e5e7eb",
    "axis": "#9ca3af",
    "text": "#111827",
    "muted": "#6b7280",
    "panel": "#f9fafb",
}

CHART_FONT = "ui-sans-serif,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"


def _fmt_dur(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 90:
        return f"{seconds:.1f}s"
    if seconds < 5400:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def _log_pos(value: float, lo: float, hi: float, y_bottom: float, y_top: float) -> float:
    import math
    v = max(lo, min(hi, value))
    frac = (math.log10(v) - math.log10(lo)) / (math.log10(hi) - math.log10(lo))
    return y_bottom - frac * (y_bottom - y_top)


def build_chart_scene(reqs, m, hours: float, width: int = 1120, height: int = 640) -> Scene:
    """Collect the chart as an ordered list of shapes.

    `m` is the metrics dict from analyse(); `reqs` are the raw requests, because
    the latency panel draws one dot per request. The shape order below is the
    paint order: later shapes sit on top of earlier ones.
    """
    c = CHART_COLORS
    pad_l, pad_r, pad_t, pad_b = 74, 26, 92, 52
    plot_w = width - pad_l - pad_r
    top_a, bot_a = pad_t + 8, pad_t + 238          # concurrency panel
    top_b, bot_b = pad_t + 300, height - pad_b     # latency panel

    timed = [r for r in reqs if r.start is not None and r.end is not None and r.end >= r.start]
    if not timed:
        return Scene(640, 120, "#ffffff", [
            Text(20, 60, "luna-doctor: no timed requests in this window",
                 size=15, fill=c["text"]),
        ], font="ui-sans-serif,system-ui,sans-serif")

    t0 = min(r.start for r in timed)
    t1 = max(r.end for r in timed)
    span = max(1.0, t1 - t0)

    def x_of(ts: float) -> float:
        return pad_l + (ts - t0) / span * plot_w

    # ---- concurrency: one event sweep, then aggregated into readable buckets ----
    # Sampling a 24h window at fine resolution renders a barcode of 0/1. Bucket it
    # (about 5 minutes each) and draw the bucket's mean as an area and its max as a
    # step, so a burst is visible as a spike above the ordinary load.
    events = []
    for r in timed:
        a = 0 if r.tier == "background" else 1
        b = 1 - a
        events.append((r.start, 1, a, b))
        events.append((r.end, -1, a, b))
    events.sort(key=lambda e: e[0])
    samples = 1600
    counts = []
    ca = cb = 0
    i = 0
    for s in range(samples + 1):
        ts = t0 + span * s / samples
        while i < len(events) and events[i][0] <= ts:
            _, delta, a, b = events[i]
            ca += delta * a
            cb += delta * b
            i += 1
        counts.append((ts, ca, cb))

    n_buckets = max(48, min(288, int(span / 300) or 48))
    bucket_w = span / n_buckets
    max_a = [0] * n_buckets
    max_s = [0] * n_buckets
    sum_a = [0.0] * n_buckets
    sum_b = [0.0] * n_buckets
    n_per = [0] * n_buckets
    for ts, a, b in counts:
        k = min(n_buckets - 1, int((ts - t0) / bucket_w))
        max_a[k] = max(max_a[k], a)
        max_s[k] = max(max_s[k], a + b)
        sum_a[k] += a
        sum_b[k] += b
        n_per[k] += 1
    mean_a = [sum_a[k] / max(1, n_per[k]) for k in range(n_buckets)]
    mean_s = [(sum_a[k] + sum_b[k]) / max(1, n_per[k]) for k in range(n_buckets)]

    peak_total = max([a + b for _, a, b in counts] + [1])
    y_max = max(peak_total + 1, 6)

    def y_conc(v: float) -> float:
        return bot_a - (v / y_max) * (bot_a - top_a)

    def bucket_x(k: int) -> float:
        return pad_l + (k + 0.5) / n_buckets * plot_w

    def area_bars(vals, colour):
        # the average load, as one translucent bar per bucket, filled to the axis
        bar_w = plot_w / n_buckets
        return [Rect(bucket_x(k) - bar_w / 2, y_conc(v), bar_w, bot_a - y_conc(v),
                     fill=colour, fill_opacity=0.13)
                for k, v in enumerate(vals) if y_conc(v) < bot_a]

    def step_lines(vals, colour, stroke_width):
        # the busiest moment per bucket, as a step: horizontal run, vertical riser
        shapes = []
        prev = None
        for k, v in enumerate(vals):
            x, y = bucket_x(k), y_conc(v)
            if prev is not None:
                shapes.append(Line(prev[0], prev[1], x, prev[1], colour, stroke_width))
                if y != prev[1]:
                    shapes.append(Line(x, prev[1], x, y, colour, stroke_width))
            prev = (x, y)
        return shapes

    # ---- latency scatter ----
    lat_lo, lat_hi = 0.5, 800.0

    def y_lat(v: float) -> float:
        return _log_pos(v, lat_lo, lat_hi, bot_b, top_b)

    shapes = []
    add = shapes.append

    # title block
    add(Text(pad_l, 34, f"luna-doctor \u00b7 last {human_window(hours)}",
             size=20, weight="600", fill=c["text"]))
    dead = m["by_outcome"].get("admission_timeout", 0)
    stalls = m["by_outcome"].get("client_idle_timeout", 0)
    bgshare = round(100.0 * m["by_tier"].get("background", 0) / max(1, m["requests"]))
    conv = m["admission_conversion"]["interactive"]["admitted_pct"]
    add(Text(pad_l, 56,
             f"{m['requests']} requests \u00b7 peak concurrency {m['peak_concurrency']} "
             f"\u00b7 {dead} blocked at the admission deadline \u00b7 {stalls} silent stall(s) "
             f"\u00b7 {bgshare}% on the background tier \u00b7 {conv}% of interactive served",
             size=12.5, fill=c["muted"]))

    # panels
    for y0, y1 in ((top_a, bot_a), (top_b, bot_b)):
        add(Rect(pad_l, y0, plot_w, y1 - y0, fill=c["panel"], stroke=c["grid"]))

    # concurrency gridlines + labels
    for v in range(0, y_max + 1):
        y = y_conc(v)
        add(Line(pad_l, y, pad_l + plot_w, y, c["grid"]))
        add(Text(pad_l - 10, y + 4, str(v), size=11, fill=c["muted"], anchor="end"))

    shapes.extend(area_bars(mean_s, c["background"]))
    shapes.extend(area_bars(mean_a, c["interactive"]))
    shapes.extend(step_lines(max_s, c["background"], 1.4))
    shapes.extend(step_lines(max_a, c["interactive"], 1.8))

    # the two rules that matter: the admission level you get demoted past, and
    # the advice. The label names no number: the footer says the level is per
    # account and not readable from these logs, so the rule itself is the only "this many".
    for value, label, colour in ((INTERACTIVE_BURST_CAP, "gateway admission level", c["stall"]),
                                 (ADVISED_CONCURRENCY, "advised 8", c["muted"])):
        if value <= y_max:
            y = y_conc(value)
            add(Line(pad_l, y, pad_l + plot_w, y, colour, 1.0, (2, 4), 0.8))
            add(Text(pad_l + 6, y - 4, label, size=10, fill=colour))

    peak_ts = max(counts, key=lambda s: s[1] + s[2])[0]
    px, py = x_of(peak_ts), y_conc(peak_total)
    add(Circle(px, py, 3, stroke=c["text"], stroke_width=1.2))
    if px > pad_l + plot_w - 90:
        add(Text(px - 7, py + 3, f"peak {peak_total}", size=10, fill=c["text"], anchor="end"))
    else:
        add(Text(px + 7, py + 3, f"peak {peak_total}", size=10, fill=c["text"]))

    # say the quiet part out loud: most machines never get near the cap, and that is
    # the finding, not a missing chart
    if peak_total <= ADVISED_CONCURRENCY:
        verdict_line = f"never reached the advised concurrency of {ADVISED_CONCURRENCY}"
        vcolour = c["muted"]
    else:
        verdict_line = f"crossed the advised concurrency of {ADVISED_CONCURRENCY}"
        vcolour = c["stall"]
    add(Text(pad_l + plot_w, top_a - 8, verdict_line, size=11.5, anchor="end", fill=vcolour))

    add(Text(pad_l, top_a - 8, "requests in flight", size=12, weight="600", fill=c["text"]))
    add(Text(pad_l + 130, top_a - 8, "line = busiest moment per bucket, fill = average load",
             size=11.5, fill=c["muted"]))

    # latency reference walls: labels sit inside the panel, on a pale plate, so they
    # never collide with the plot edge
    for value, label in ((ADMISSION_DEADLINE_GLM53_S, "120.2s: admission deadline"),
                         (CLIENT_IDLE_TIMEOUT_S, "300s: client idle budget")):
        y = y_lat(value)
        add(Line(pad_l, y, pad_l + plot_w, y, c["admission"], 1.0, (5, 4), 0.5))
        w_est = round(6.1 * len(label))
        add(Rect(pad_l + 8, y - 15, w_est, 13, fill="#ffffff", fill_opacity=0.85))
        add(Text(pad_l + 12, y - 4, label, size=10.5, fill=c["admission"]))

    for value in (1, 10, 60, 300, 600):
        if lat_lo <= value <= lat_hi:
            y = y_lat(value)
            add(Line(pad_l, y, pad_l + plot_w, y, c["grid"]))
            add(Text(pad_l - 10, y + 4, _fmt_dur(float(value)),
                     size=11, fill=c["muted"], anchor="end"))

    # rolling median of completed latency per bucket: the trend through the cloud.
    # Break the line across empty buckets, or it draws a confident diagonal through
    # hours where nothing ran.
    ok = [r for r in timed if r.outcome == "completed" and r.duration]
    med_pts = []
    for k in range(n_buckets):
        lo = t0 + k * bucket_w
        hi = lo + bucket_w
        vals = [r.duration for r in ok if r.duration and lo <= r.end < hi]
        if vals:
            med_pts.append((k, bucket_x(k), y_lat(statistics.median(vals))))

    def _trend(segment) -> None:
        if len(segment) > 1:
            for (x1, y1), (x2, y2) in zip(segment, segment[1:]):
                add(Line(x1, y1, x2, y2, c["text"], 1.5, (), 0.5))

    segment = []
    prev_k = None
    for k, x, y in med_pts:
        if prev_k is not None and k - prev_k > 1:
            _trend(segment)
            segment = []
        segment.append((x, y))
        prev_k = k
    _trend(segment)

    add(Text(pad_l, top_b - 8, "each request, end to end", size=12, weight="600", fill=c["text"]))
    add(Text(pad_l + 148, top_b - 8,
             "log scale \u00b7 a dot on a dashed line was stopped by that wall",
             size=11.5, fill=c["muted"]))

    # dots: every failure, sampled successes so a 3000-request window stays legible
    sampled = ok if len(ok) <= 1200 else ok[:: max(1, len(ok) // 1200)]
    for r in sampled:
        cx = x_of((r.start + r.end) / 2)
        cy = y_lat(r.duration)
        add(Circle(cx, cy, 1.6, fill=c[r.tier], fill_opacity=0.42,
                   title=f"{r.model} \u00b7 {_fmt_dur(r.duration)} \u00b7 {r.tier}"))
    for r in timed:
        if r.outcome == "completed":
            continue
        cx = x_of(r.end)
        if r.outcome == "admission_timeout":
            cy = y_lat(ADMISSION_DEADLINE_GLM53_S)
            colour, glyph = c["admission"], "\u2715"
        elif r.outcome == "client_idle_timeout":
            cy = y_lat(CLIENT_IDLE_TIMEOUT_S)
            colour, glyph = c["stall"], "\u25cf"
        else:
            cy = y_lat(r.duration or 1.0)
            colour, glyph = c["muted"], "\u25cf"
        add(Text(cx, cy + 4, glyph, size=11, anchor="middle", fill=colour,
                 title=f"{r.outcome} \u00b7 {r.model} \u00b7 {_fmt_dur(r.duration)}"))

    # x axis: ticks + labels
    import datetime as _dt
    ticks = 6
    for i in range(ticks + 1):
        ts = t0 + span * i / ticks
        x = x_of(ts)
        add(Line(x, bot_b, x, bot_b + 5, c["axis"]))
        add(Line(x, top_a, x, bot_a, c["grid"], 1.0, (), 0.7))
        label = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%H:%M")
        add(Text(x, bot_b + 19, label, size=10.5, anchor="middle", fill=c["muted"]))
    add(Text(pad_l + plot_w, height - 14, "UTC", size=10.5, anchor="end", fill=c["muted"]))

    # legend
    lx, ly = pad_l, 74
    for name, colour in (("interactive tier", c["interactive"]),
                         ("background tier", c["background"])):
        add(Rect(lx, ly - 8, 10, 10, fill=colour))
        add(Text(lx + 15, ly + 1, name, size=11.5, fill=c["text"]))
        lx += 132
    add(Text(lx, ly + 1, "\u2715 blocked at admission", size=11.5, fill=c["admission"]))
    lx += 128
    add(Text(lx, ly + 1, "\u25cf silent stall", size=11.5, fill=c["stall"]))

    # footer note
    add(Text(pad_l, height - 14,
             f"luna-doctor v{VERSION} \u00b7 {CAP_COPY} "
             f"\u00b7 advised concurrency \u2264 {ADVISED_CONCURRENCY}",
             size=10.5, fill=c["muted"]))

    return Scene(width, height, "#ffffff", shapes, font=CHART_FONT)


def svg_chart(reqs, m, hours: float, width: int = 1120, height: int = 640) -> str:
    return svg_from(build_chart_scene(reqs, m, hours, width, height))


def cmd_chart(args) -> int:
    try:
        hours = window_hours(args.since, args.now)
    except ValueError as e:
        print(f"luna-doctor: {e}", file=sys.stderr)
        return 2
    harnesses = [h.strip() for h in args.harness.split(",") if h.strip()]
    reqs = collect(hours, harnesses)
    m = analyse(reqs, hours)
    svg = svg_chart(reqs, m, hours, width=args.width, height=args.height)

    out = args.out
    if not out:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = f"luna-doctor-{stamp}.svg"
    try:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(svg)
    except OSError as e:
        print(f"luna-doctor: cannot write {out}: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(envelope("chart", {
            "path": os.path.abspath(out),
            "bytes": len(svg),
            "metrics": m,
        }, contract=CONTRACT), indent=2))
    else:
        print(f"wrote {os.path.abspath(out)}  ({len(svg) / 1024:.0f} KB)")
        print(f"  {m['requests']} requests, peak concurrency {m['peak_concurrency']}, "
              f"{m['by_outcome'].get('admission_timeout', 0)} blocked admission(s)")
        if args.open:
            import webbrowser
            webbrowser.open("file://" + os.path.abspath(out))
    return 0
