"""Write the two README chart images from a synthetic store that looks like real use.

The pictures in `assets/` illustrate the tool, so they must not carry anybody's
account and must not carry anybody's logs. This script builds a fake pi log
store in a temporary directory, points the chart at it, and declares the width
with `--plan teams`. The capacity cache is sent to a path that does not exist,
so the dashed rule is a declaration and no account value can reach a public
image.

The shape is modelled on a week of real agent traffic: the same hour-of-week
rhythm, the same mix of models and tiers, the same latency spread, and the same
small share of refusals and upstream errors. Only the shape is taken. Every
record here is generated, so no timestamp, session or prompt comes from a real
machine.

    python3 scripts/make_demo_charts.py

Writes assets/chart-7d.svg, assets/chart-7d.png, assets/chart-24h.svg and
assets/chart-24h.png. Deterministic: the seed is fixed, so two runs on the same
day produce the same pictures.
"""

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "bin", "luna-doctor")
ASSETS = os.path.join(ROOT, "assets")

# The width these pictures are drawn against, and the plan name that carries it.
# `teams` is a published plan, so the number is declared and labelled as such.
DECLARED_PLAN = "teams"

# The week's day blocks are ordered so the last day is a busy one: the picture
# of the last 24 hours should show a working day, not the quiet weekend that
# happened to fall at the end of the week these figures came from.
DAY_ORDER = (6, 5, 1, 2, 0, 3, 4)

# Requests started in each hour of the week, Monday 00:00 UTC first.
STARTED = (
    526, 271, 0, 255, 316, 119, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 9, 5, 46, 130, 122, 110, 152, 250,
    5, 42, 241, 75, 49, 66, 30, 145, 12, 0, 0, 0, 0, 0, 0, 43, 46, 709, 1062, 859, 327, 94, 0, 0,
    0, 0, 0, 49, 0, 66, 1319, 275, 28, 24, 0, 0, 18, 42, 33, 84, 117, 289, 299, 227, 198, 196, 326, 47,
    474, 66, 242, 289, 275, 368, 436, 71, 0, 0, 0, 0, 0, 122, 357, 564, 534, 601, 320, 399, 40, 157, 465, 650,
    549, 374, 66, 12, 0, 132, 0, 0, 0, 0, 0, 0, 0, 0, 0, 54, 704, 501, 53, 216, 541, 735, 530, 458,
    147, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 107, 448, 467, 72, 38, 0, 60, 0, 9,
    62, 0, 0, 143, 26, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 34, 53, 0, 9, 0, 11, 94,
)

# The busiest moment seen in each of those hours: how wide a burst gets.
PEAK = (
    6, 4, 0, 5, 4, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 3, 2, 4, 4, 4, 4,
    1, 1, 7, 2, 3, 3, 2, 3, 2, 0, 0, 0, 0, 0, 0, 1, 1, 6, 7, 7, 7, 2, 0, 0,
    0, 0, 0, 2, 0, 2, 12, 5, 1, 1, 0, 0, 2, 3, 2, 2, 2, 4, 4, 4, 4, 4, 5, 1,
    5, 2, 4, 6, 5, 5, 6, 3, 0, 0, 0, 0, 0, 5, 6, 9, 7, 7, 5, 17, 1, 2, 5, 6,
    5, 7, 2, 1, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 9, 9, 2, 4, 6, 8, 6, 4,
    4, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 3, 5, 7, 2, 2, 0, 2, 0, 1,
    3, 0, 0, 3, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 1, 0, 1, 0, 1, 1,
)

# The mix of models a real week carried, by share. Model ids are public names.
MIX = (
    ("deepseek-4.1-flash", 0.47),
    ("deepseek-4.1-flash-background", 0.20),
    ("glm-5.3", 0.16),
    ("claude-opus-lr11", 0.11),
    ("glm-5.3-background", 0.017),
    ("glm-5.3-flash", 0.015),
    ("muse-spark-open", 0.010),
    ("glm-5.3-vision-background", 0.009),
    ("glm-5.3-flash-background", 0.007),
    ("glm-5.3-vision", 0.002),
)

# Share of requests in each outcome class, from the same week.
ERRORS = (
    ("upstream_unavailable",
     "the backend is unavailable: INFERENCE_UNAVAILABLE", 0.0050),
    ("aborted", "request aborted by the client", 0.0033),
    ("other_error", "stream closed before the first chunk", 0.0013),
    ("admission_timeout", "scheduler admission timeout after 120.2s", 0.0012),
    ("client_idle_timeout", "Stream ended without finish_reason", 0.0004),
)

# Median and p90 latency in seconds, per tier. Interactive work answers a little
# faster at the tail, which is what the real week showed.
LATENCY = {"interactive": (7.5, 32.6), "background": (7.4, 40.9)}

SEED = 20261027


def pick_model(rng):
    roll = rng.random()
    total = 0.0
    for name, share in MIX:
        total += share
        if roll <= total:
            return name
    return MIX[0][0]


def pick_error(rng):
    roll = rng.random()
    total = 0.0
    for _name, text, share in ERRORS:
        total += share
        if roll <= total:
            return text
    return None


def duration_for(rng, tier):
    """A latency drawn from the tier's median and p90, with a real tail."""
    median, p90 = LATENCY[tier]
    sigma = max(0.2, (p90 / median) ** 0.5)
    seconds = rng.lognormvariate(0.0, sigma) * median
    return min(1200.0, max(0.8, seconds))


def record(start, model, seconds, error=None):
    """One pi log record, in the shape the reader parses."""
    start_ms = int(start.timestamp() * 1000)
    end = start + timedelta(seconds=seconds)
    message = {
        "role": "assistant",
        "model": model,
        "provider": "lunaroute",
        "timestamp": start_ms,
        "usage": {"input": 0 if error else 900, "output": 0 if error else 700},
        "stopReason": "error" if error else "stop",
        "content": [{"type": "text", "text": "x"}],
        "responseId": "demo-%d" % start_ms,
    }
    if error:
        message["errorMessage"] = error
    return {
        "type": "message",
        "id": "demo-%d" % start_ms,
        "timestamp": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "message": message,
    }


def build_store(home):
    """One week of traffic, from the hour-of-week rhythm in STARTED and PEAK."""
    rng = random.Random(SEED)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    week_start = now - timedelta(days=7)
    records = []
    cells = [(day, hour) for day in DAY_ORDER for hour in range(24)]
    for slot, (day, hour) in enumerate(cells):
        source = day * 24 + hour
        started, peak = STARTED[source], max(1, PEAK[source])
        if not started:
            continue
        hour_start = week_start + timedelta(hours=slot)
        # A busy hour is a sequence of bursts, not one flat block: each burst
        # gets its own slice of the hour, so burst widths add up to the peak the
        # hour really reached instead of stacking into a number nobody saw.
        bursts = max(1, int(round(float(started) / peak)))
        width = max(1, int(round(float(started) / bursts)))
        slice_s = 3600.0 / bursts
        for b in range(bursts):
            burst_start = b * slice_s + rng.uniform(0, max(0.0, slice_s - 60.0))
            spacing = min(30.0, 45.0 / width)
            for i in range(width):
                if len(records) and False:
                    pass
                start = hour_start + timedelta(seconds=burst_start + i * spacing)
                model = pick_model(rng)
                tier = "background" if model.endswith("-background") else "interactive"
                error = pick_error(rng)
                # A refusal waits at the admission deadline and produces nothing.
                if error and "admission" in error:
                    seconds = 120.2
                else:
                    seconds = min(120.0, duration_for(rng, tier))
                records.append(record(start, model, seconds, error))
    records.sort(key=lambda r: r["message"]["timestamp"])
    sessions = os.path.join(home, ".pi", "agent", "sessions", "demo")
    os.makedirs(sessions, exist_ok=True)
    with open(os.path.join(sessions, "demo-session.jsonl"), "w", encoding="utf-8") as fh:
        for one in records:
            fh.write(json.dumps(one) + "\n")
    return sessions, len(records)


def chart(home, pi_root, since, out):
    env = dict(os.environ)
    env["HOME"] = home
    # No capacity payload: the dashed rule must come from the declaration alone.
    env["LUNA_DOCTOR_CAPACITY"] = os.path.join(home, "no-such-capacity.json")
    env["LUNA_DOCTOR_STORE"] = os.path.join(home, "store")
    proc = subprocess.run(
        [sys.executable, CLI, "chart", "--since", since, "--pi-root", pi_root,
         "--harness", "pi", "--plan", DECLARED_PLAN, "--out", out,
         "--width", "1120", "--height", "640"],
        capture_output=True, text=True, env=env, cwd=ROOT)
    if proc.returncode not in (0, 1):
        sys.exit("chart failed for %s:\n%s\n%s" % (since, proc.stdout, proc.stderr))
    print(proc.stdout.strip())


def main():
    home = tempfile.mkdtemp(prefix="luna-doctor-demo-")
    try:
        pi_root, count = build_store(home)
        print("synthetic store: %d generated records" % count)
        os.makedirs(ASSETS, exist_ok=True)
        chart(home, pi_root, "7d", os.path.join(ASSETS, "chart-7d.svg"))
        chart(home, pi_root, "24h", os.path.join(ASSETS, "chart-24h.svg"))
    finally:
        shutil.rmtree(home, ignore_errors=True)
    for name in ("chart-7d.svg", "chart-7d.png", "chart-24h.svg", "chart-24h.png"):
        print("%s  %d bytes" % (name, os.path.getsize(os.path.join(ASSETS, name))))


if __name__ == "__main__":
    main()
