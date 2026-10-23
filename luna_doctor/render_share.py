"""The WhatsApp share surfaces: a paste-safe message and a share card.

One report, two artifacts: `render_share_text` turns the same Report every
other renderer prints into WhatsApp markup, and `write_card` rasterises the
1080x1350 card scene to a PNG. Callers print the message to stdout and the
card's path to stderr, so the copyable artifact is stdout alone.

WhatsApp has no escape character, so a model or session string carrying
`*`, `_`, `~` or a backtick would open a formatting run and reformat the
message; `wa_safe` substitutes every metacharacter before any field is
emitted. Share text is never colored and never carries emoji - share
bypasses the style gate entirely, and those rules are what it enforces by
hand instead.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from luna_doctor.png import png_from
from luna_doctor.scene import build_card_scene, human_window

# WhatsApp's markup characters, each with a stand-in that reads the same way
# but cannot open or close a run: `_` becomes a plain hyphen, `*` becomes the
# multiplication sign, `~` (approximation here, strike marker elsewhere)
# becomes the almost-equal sign, and a backtick is dropped outright.
_META = {"*": "\u00d7", "_": "-", "~": "\u2248", "`": ""}


def wa_safe(s: str) -> str:
    """Rewrite `s` so WhatsApp cannot open a formatting run with it.

    Tabs collapse to a single space and no line may begin with whitespace,
    because WhatsApp renders leading spaces as a monospace block - the
    thing share text must never become.
    """
    s = s.replace("\t", " ")
    s = re.sub(r"(?m)^ +", "", s)
    for ch, sub in _META.items():
        s = s.replace(ch, sub)
    return s


def _share_numbers(m: dict) -> list:
    """The four or five numbers that earn a place on a phone screen.

    Measured figures only: swamp's configured fan-out, when swamp is in use,
    is a setting and gets its own line labelled "configured" - it is never
    summed into the measured peak beside it.
    """
    harnesses = ", ".join(f"{k} {v}" for k, v in sorted((m.get("by_harness") or {}).items()))
    reqs = m.get("requests", 0)
    out = [
        ("requests", f"{reqs} ({harnesses})" if harnesses else str(reqs)),
        ("peak concurrency", str(m.get("peak_concurrency", 0))),
    ]
    by_outcome = m.get("by_outcome") or {}
    out.append(("admission deaths", str(by_outcome.get("admission_timeout", 0))))
    out.append(("silent stalls", str(by_outcome.get("client_idle_timeout", 0))))
    lat = ((m.get("latency_s") or {}).get("interactive") or {})
    if lat.get("p50") is not None:
        p90 = f", p90 {lat['p90']:.1f}s" if lat.get("p90") is not None else ""
        out.append(("interactive latency", f"p50 {lat['p50']:.1f}s{p90}"))
    sw = m.get("swamp") or {}
    if sw.get("present") and sw.get("configuredConcurrency"):
        out.append(("swamp fan-out (configured)",
                    f"{sw['configuredConcurrency']} - a setting, not measured traffic"))
    return out


def _provenance(m: dict) -> str:
    window = human_window(m["window_hours"]) if m.get("window_hours") else ""
    if window:
        return f"measured on this account, last {window}"
    return "measured on this account"


def render_share_text(report, m: dict) -> str:
    """The report as a WhatsApp message, ending in a single newline.

    `>` quotes the headline, `*label:* value` lines carry the numbers, `- `
    bullets carry the advice, and one provenance line closes it. WhatsApp
    markup only: no monospace, no alignment, no ANSI, no emoji.
    """
    lines = ["> " + wa_safe(report.headline)]
    for label, value in _share_numbers(m):
        lines.append(f"*{wa_safe(label)}:* {wa_safe(value)}")
    for adj in report.adjustments:
        lines.append("- " + wa_safe(adj))
    lines.append(wa_safe(_provenance(m)))
    return "\n".join(lines) + "\n"


def default_card_path() -> str:
    """A timestamped PNG name in the cwd, for `--share` without `--out`."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"luna-doctor-share-{stamp}.png"


def write_card(m: dict, headline: str, path: str) -> str:
    """Rasterise the share card to `path` as a PNG and return the path."""
    with open(path, "wb") as fh:
        fh.write(png_from(build_card_scene(m, headline)))
    return path
