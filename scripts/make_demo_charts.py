"""Write the two README chart images from synthetic logs, never from a real account.

The images in `assets/` illustrate the tool, so they must not carry anybody's
account. This script builds a fake pi log store in a temporary directory, points
the chart at it, and declares the width with `--plan teams` so the dashed rule is
labelled a declaration. The capacity cache is pointed at a path that does not
exist, so no account value can reach the picture even if one is cached on the
machine that runs this.

The shape is deliberate. The week is busy and crosses the declared width, so the
picture shows flare ticks and a peak above the rule. The last day is quiet, so
the second picture shows the honest opposite: nothing reaches the rule, nothing
is rejected.

    python3 scripts/make_demo_charts.py

Writes assets/chart-7d.svg, assets/chart-7d.png, assets/chart-24h.svg and
assets/chart-24h.png. Deterministic apart from the clock.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "bin", "luna-doctor")
ASSETS = os.path.join(ROOT, "assets")

# Public model ids. The `-background` form is the background scheduling class.
PRIORITY = "glm-5.3"
BACKGROUND = "glm-5.3-background"
SECOND = "deepseek-4.1-flash"

# The declared width these pictures are drawn against, and the plan name that
# carries it. `teams` is a published plan, so the number is a declared one and
# the chart labels it as such.
DECLARED_PLAN = "teams"

REJECTED = ("scheduler admission timeout after 120.2s",)


def record(stamp_ms, model, duration_s, error=None):
    """One pi log record, in the shape the reader parses."""
    end = stamp_ms + int(duration_s * 1000)
    message = {
        "role": "assistant",
        "model": model,
        "provider": "lunaroute",
        "timestamp": stamp_ms,
        "usage": {"input": 0 if error else 900, "output": 0 if error else 700},
        "stopReason": "error" if error else "stop",
        "content": [{"type": "text", "text": "x"}],
        "responseId": "demo-%d-%s" % (stamp_ms, model),
    }
    if error:
        message["errorMessage"] = error
    return {"type": "message", "id": "demo-%d" % stamp_ms,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                       time.gmtime(end / 1000.0)),
            "message": message}


def burst(now, start_s, width, step_s=6.0, model=PRIORITY, error=None):
    """`width` requests that overlap: each starts one step after the last."""
    out = []
    for i in range(width):
        stamp = int((now - start_s + i * step_s) * 1000)
        # A request on the priority tier answers in about 40 s. One that is
        # rejected waited at the admission deadline and produced nothing.
        out.append(record(stamp, model, 120.2 if error else 40.0, error))
    return out


def build_store(home):
    """Busy week, quiet day: the shape each picture is meant to show."""
    now = time.time()
    records = []

    # Days 2 to 7 back: a working day with a few bursts that cross the
    # declared width, and background traffic that runs wide and slow.
    for day in range(2, 8):
        base = day * 86400
        for hour in range(9, 19):
            offset = base - hour * 3600
            records.extend(burst(now, offset, 3))                    # steady
            records.extend(burst(now, offset + 90, 6, step_s=9.0))   # a normal wave
            records.extend(burst(now, offset + 200, 5, step_s=10.0,
                                 model=BACKGROUND))                  # background
        if day in (2, 4, 6):                                         # a burst that spills
            for k in range(4):
                records.extend(burst(now, base - 3600 - k * 600, 16, step_s=1.5))
                records.extend(burst(now, base - 3560 - k * 600, 9, step_s=2.0))
            for k in range(3):
                records.extend(burst(now, base - 3000 - k * 420, 3,
                                     model=SECOND))
            records.extend(burst(now, base - 1800, 4, error=REJECTED[0]))

    # The last 24 hours: quiet. Nothing reaches the declared width, so the
    # second picture has no flare tick and no peak above the rule.
    for hour in range(1, 24):
        records.extend(burst(now, hour * 3600, 2, step_s=8.0))
        records.extend(burst(now, hour * 3600 + 300, 3, step_s=12.0,
                             model=BACKGROUND))

    records.sort(key=lambda r: r["message"]["timestamp"])
    sessions = os.path.join(home, ".pi", "agent", "sessions", "demo")
    os.makedirs(sessions, exist_ok=True)
    path = os.path.join(sessions, "demo-session.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for one in records:
            fh.write(json.dumps(one) + "\n")
    return sessions, len(records)


def chart(home, pi_root, since, out):
    env = dict(os.environ)
    env["HOME"] = home
    # No capacity payload: the dashed rule must come from the declaration
    # alone, so no account number can appear in a public image.
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
        print("synthetic store: %d records in %s" % (count, pi_root))
        os.makedirs(ASSETS, exist_ok=True)
        chart(home, pi_root, "7d", os.path.join(ASSETS, "chart-7d.svg"))
        chart(home, pi_root, "24h", os.path.join(ASSETS, "chart-24h.svg"))
    finally:
        shutil.rmtree(home, ignore_errors=True)
    for name in ("chart-7d.svg", "chart-7d.png", "chart-24h.svg", "chart-24h.png"):
        path = os.path.join(ASSETS, name)
        print("%s  %d bytes" % (name, os.path.getsize(path)))


if __name__ == "__main__":
    main()
