#!/usr/bin/env python3
"""`chart` command for luna-doctor.

Executed inside bin/luna-doctor's namespace (see the loader at the bottom of
that script), so it can use Request, collect, analyse, window_hours and the
measured constants directly.

Emits a standalone SVG: no fonts, scripts or CDNs, so it opens anywhere and can
be pasted into a doc. Two panels share one time axis:

  top     how many requests were in flight, split by tier. This is the shape of
          the fan-out, which is what the gateway prices.
  bottom  every request's latency, on a log axis, with the two measured walls
          drawn as reference lines: the 120.2s admission deadline and the 300s
          client idle budget. Failures sit on those lines, which is the point:
          you can see a burst push its tail into the wall.
"""

from html import escape as _esc

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


def svg_chart(reqs, m, hours: float, width: int = 1120, height: int = 640) -> str:
    c = CHART_COLORS
    pad_l, pad_r, pad_t, pad_b = 74, 26, 92, 52
    plot_w = width - pad_l - pad_r
    top_a, bot_a = pad_t + 8, pad_t + 238          # concurrency panel
    top_b, bot_b = pad_t + 300, height - pad_b     # latency panel

    timed = [r for r in reqs if r.start is not None and r.end is not None and r.end >= r.start]
    if not timed:
        return ('<svg xmlns="http://www.w3.org/2000/svg" width="640" height="120">'
                '<text x="20" y="60" font-family="ui-sans-serif,system-ui,sans-serif" '
                'font-size="15" fill="#111827">luna-doctor: no timed requests in this window'
                '</text></svg>')

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

    def area(vals) -> str:
        pts = [(bucket_x(k), y_conc(v)) for k, v in enumerate(vals)]
        d = f"M {pts[0][0]:.1f} {bot_a:.1f} " + " ".join(f"L {x:.1f} {y:.1f}" for x, y in pts)
        return d + f" L {pts[-1][0]:.1f} {bot_a:.1f} Z"

    def step(vals) -> str:
        pts = []
        for k, v in enumerate(vals):
            x = bucket_x(k)
            y = y_conc(v)
            if pts:
                pts.append((x, pts[-1][1]))
            pts.append((x, y))
        return "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in pts)

    bg_mean_path = area(mean_s)
    bg_max_path = step(max_s)
    inter_mean_path = area(mean_a)
    inter_max_path = step(max_a)

    # ---- latency scatter ----
    lat_lo, lat_hi = 0.5, 800.0

    def y_lat(v: float) -> float:
        return _log_pos(v, lat_lo, lat_hi, bot_b, top_b)

    out = []
    add = out.append
    add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="ui-sans-serif,-apple-system,Segoe UI,'
        f'Roboto,Helvetica,Arial,sans-serif">')
    add(f'<rect width="{width}" height="{height}" fill="#ffffff"/>')

    # title block
    add(f'<text x="{pad_l}" y="34" font-size="20" font-weight="600" fill="{c["text"]}">'
        f'luna-doctor &#183; last {_esc(human_window(hours))}</text>')
    dead = m["by_outcome"].get("admission_timeout", 0)
    stalls = m["by_outcome"].get("client_idle_timeout", 0)
    bgshare = round(100.0 * m["by_tier"].get("background", 0) / max(1, m["requests"]))
    conv = m["admission_conversion"]["interactive"]["admitted_pct"]
    add(f'<text x="{pad_l}" y="56" font-size="12.5" fill="{c["muted"]}">'
        f'{m["requests"]} requests &#183; peak concurrency {m["peak_concurrency"]} '
        f'&#183; {dead} died at the admission deadline &#183; {stalls} silent stall(s) '
        f'&#183; {bgshare}% on the background tier &#183; {conv}% of interactive served</text>')

    # panels
    for y0, y1 in ((top_a, bot_a), (top_b, bot_b)):
        add(f'<rect x="{pad_l}" y="{y0}" width="{plot_w}" height="{y1 - y0}" fill="{c["panel"]}" '
            f'stroke="{c["grid"]}"/>')

    # concurrency gridlines + labels
    for v in range(0, y_max + 1):
        y = y_conc(v)
        add(f'<line x1="{pad_l}" x2="{pad_l + plot_w}" y1="{y:.1f}" y2="{y:.1f}" '
            f'stroke="{c["grid"]}" stroke-width="1"/>')
        add(f'<text x="{pad_l - 10}" y="{y + 4:.1f}" font-size="11" text-anchor="end" '
            f'fill="{c["muted"]}">{v}</text>')

    add(f'<path d="{bg_mean_path}" fill="{c["background"]}" fill-opacity="0.13"/>')
    add(f'<path d="{inter_mean_path}" fill="{c["interactive"]}" fill-opacity="0.13"/>')
    add(f'<path d="{bg_max_path}" fill="none" stroke="{c["background"]}" stroke-width="1.4"/>')
    add(f'<path d="{inter_max_path}" fill="none" stroke="{c["interactive"]}" stroke-width="1.8"/>')

    # the two numbers that matter: the cap you get demoted past, and the advice
    for value, label, colour in ((INTERACTIVE_BURST_CAP, "burst cap 10", c["stall"]),
                                 (ADVISE_FANOUT_MAX, "advised 8", c["muted"])):
        if value <= y_max:
            y = y_conc(value)
            add(f'<line x1="{pad_l}" x2="{pad_l + plot_w}" y1="{y:.1f}" y2="{y:.1f}" '
                f'stroke="{colour}" stroke-width="1" stroke-dasharray="2 4" stroke-opacity="0.8"/>')
            add(f'<text x="{pad_l + 6}" y="{y - 4:.1f}" font-size="10" fill="{colour}">{label}</text>')

    peak_ts = max(counts, key=lambda s: s[1] + s[2])[0]
    px, py = x_of(peak_ts), y_conc(peak_total)
    add(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3" fill="none" stroke="{c["text"]}" '
        f'stroke-width="1.2"/>')
    if px > pad_l + plot_w - 90:
        add(f'<text x="{px - 7:.1f}" y="{py + 3:.1f}" font-size="10" text-anchor="end" '
            f'fill="{c["text"]}">peak {peak_total}</text>')
    else:
        add(f'<text x="{px + 7:.1f}" y="{py + 3:.1f}" font-size="10" fill="{c["text"]}">'
            f'peak {peak_total}</text>')

    # say the quiet part out loud: most machines never get near the cap, and that is
    # the finding, not a missing chart
    if peak_total <= ADVISE_FANOUT_MAX:
        verdict_line = f"never reached the advised fan-out of {ADVISE_FANOUT_MAX}"
        vcolour = c["muted"]
    else:
        verdict_line = f"crossed the advised fan-out of {ADVISE_FANOUT_MAX}"
        vcolour = c["stall"]
    add(f'<text x="{pad_l + plot_w}" y="{top_a - 8}" font-size="11.5" text-anchor="end" '
        f'fill="{vcolour}">{_esc(verdict_line)}</text>')

    add(f'<text x="{pad_l}" y="{top_a - 8}" font-size="12" font-weight="600" fill="{c["text"]}">'
        f'requests in flight</text>')
    add(f'<text x="{pad_l + 130}" y="{top_a - 8}" font-size="11.5" fill="{c["muted"]}">'
        f'line = busiest moment per bucket, fill = average load</text>')

    # latency reference walls: labels sit inside the panel, on a pale plate, so they
    # never collide with the plot edge
    for value, label in ((ADMISSION_DEADLINE_S, "120.2s: admission deadline"),
                         (CLIENT_IDLE_TIMEOUT_S, "300s: client idle budget")):
        y = y_lat(value)
        add(f'<line x1="{pad_l}" x2="{pad_l + plot_w}" y1="{y:.1f}" y2="{y:.1f}" '
            f'stroke="{c["admission"]}" stroke-width="1" stroke-dasharray="5 4" stroke-opacity="0.5"/>')
        w_est = 6.1 * len(label)
        add(f'<rect x="{pad_l + 8}" y="{y - 15:.1f}" width="{w_est:.0f}" height="13" '
            f'fill="#ffffff" fill-opacity="0.85"/>')
        add(f'<text x="{pad_l + 12}" y="{y - 4:.1f}" font-size="10.5" '
            f'fill="{c["admission"]}">{_esc(label)}</text>')

    for value in (1, 10, 60, 300, 600):
        if lat_lo <= value <= lat_hi:
            y = y_lat(value)
            add(f'<line x1="{pad_l}" x2="{pad_l + plot_w}" y1="{y:.1f}" y2="{y:.1f}" '
                f'stroke="{c["grid"]}"/>')
            add(f'<text x="{pad_l - 10}" y="{y + 4:.1f}" font-size="11" text-anchor="end" '
                f'fill="{c["muted"]}">{_fmt_dur(float(value))}</text>')

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
            add('<path d="M ' + " L ".join(f"{x:.1f} {y:.1f}" for x, y in segment)
                + f'" fill="none" stroke="{c["text"]}" stroke-width="1.5" '
                  f'stroke-opacity="0.5"/>')

    segment = []
    prev_k = None
    for k, x, y in med_pts:
        if prev_k is not None and k - prev_k > 1:
            _trend(segment)
            segment = []
        segment.append((x, y))
        prev_k = k
    _trend(segment)

    add(f'<text x="{pad_l}" y="{top_b - 8}" font-size="12" font-weight="600" fill="{c["text"]}">'
        f'each request, end to end</text>')
    add(f'<text x="{pad_l + 148}" y="{top_b - 8}" font-size="11.5" fill="{c["muted"]}">'
        f'log scale &#183; a dot on a dashed line was killed by that wall</text>')

    # dots: every failure, sampled successes so a 3000-request window stays legible
    ok = [r for r in timed if r.outcome == "completed" and r.duration]
    sampled = ok if len(ok) <= 1200 else ok[:: max(1, len(ok) // 1200)]
    for r in sampled:
        cx = x_of((r.start + r.end) / 2)
        cy = y_lat(r.duration)
        add(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="1.6" fill="{c[r.tier]}" fill-opacity="0.42">'
            f'<title>{_esc(r.model)} &#183; {_fmt_dur(r.duration)} &#183; {r.tier}</title></circle>')
    for r in timed:
        if r.outcome == "completed":
            continue
        cx = x_of(r.end)
        if r.outcome == "admission_timeout":
            cy = y_lat(ADMISSION_DEADLINE_S)
            colour, glyph = c["admission"], "&#10005;"
        elif r.outcome == "client_idle_timeout":
            cy = y_lat(CLIENT_IDLE_TIMEOUT_S)
            colour, glyph = c["stall"], "&#9679;"
        else:
            cy = y_lat(r.duration or 1.0)
            colour, glyph = c["muted"], "&#9679;"
        add(f'<text x="{cx:.1f}" y="{cy + 4:.1f}" font-size="11" text-anchor="middle" '
            f'fill="{colour}">{glyph}<title>{_esc(r.outcome)} &#183; {_esc(r.model)} '
            f'&#183; {_fmt_dur(r.duration)}</title></text>')

    # x axis: ticks + labels
    import datetime as _dt
    ticks = 6
    for i in range(ticks + 1):
        ts = t0 + span * i / ticks
        x = x_of(ts)
        add(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{bot_b}" y2="{bot_b + 5}" stroke="{c["axis"]}"/>')
        add(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top_a}" y2="{bot_a}" stroke="{c["grid"]}" '
            f'stroke-opacity="0.7"/>')
        label = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%H:%M")
        add(f'<text x="{x:.1f}" y="{bot_b + 19}" font-size="10.5" text-anchor="middle" '
            f'fill="{c["muted"]}">{label}</text>')
    add(f'<text x="{pad_l + plot_w}" y="{height - 14}" font-size="10.5" text-anchor="end" '
        f'fill="{c["muted"]}">UTC</text>')

    # legend
    lx, ly = pad_l, 74
    for name, colour in (("interactive tier", c["interactive"]),
                         ("background tier", c["background"])):
        add(f'<rect x="{lx}" y="{ly - 8}" width="10" height="10" fill="{colour}"/>')
        add(f'<text x="{lx + 15}" y="{ly + 1}" font-size="11.5" fill="{c["text"]}">{name}</text>')
        lx += 132
    add(f'<text x="{lx}" y="{ly + 1}" font-size="11.5" fill="{c["admission"]}">'
        f'&#10005; admission death</text>')
    lx += 128
    add(f'<text x="{lx}" y="{ly + 1}" font-size="11.5" fill="{c["stall"]}">'
        f'&#9679; silent stall</text>')

    # footer note
    add(f'<text x="{pad_l}" y="{height - 14}" font-size="10.5" fill="{c["muted"]}">'
        f'luna-doctor v{_esc(VERSION)} &#183; gateway burst cap {INTERACTIVE_BURST_CAP} '
        f'&#183; advised fan-out &#8804; {ADVISE_FANOUT_MAX}</text>')
    add("</svg>")
    return "\n".join(out)


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
        with open(out, "w") as fh:
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
              f"{m['by_outcome'].get('admission_timeout', 0)} admission death(s)")
        if args.open:
            import webbrowser
            webbrowser.open("file://" + os.path.abspath(out))
    return 0
