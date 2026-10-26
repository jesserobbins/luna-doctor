#!/usr/bin/env python3
"""luna-doctor - explain what your local agent traffic is doing to your
LunaRoute concurrency, and what to adjust.

Two halves:

  analyze   reads the local agent logs (pi, claude-code) and reconstructs what
            the machine asked for, what it got, and at what tier.
  capacity  reports the account's own plan, priority concurrency, occupancy
            and admission from the gateway's capacity API, cached on disk.

The constants below are measured, not assumed. See research notes: the gateway
waits ~25.2s before sending SSE headers, kills unadmitted requests at a
per-model deadline (~120.2s measured on glm-5.3, ~60.3s on deepseek-4.1-flash,
reported per model in every run), demotes overflow past the
interactive admission width to the background tier with
`x-lunaroute-warning: overflowed-to-background`, and holds
demoted streams open with an SSE keep-alive every ~10s while producing nothing.
pi's own idle budget is 300s, which is where `Stream ended without finish_reason`
comes from - a client string, never a gateway one.

Exit codes: 0 no pressure, 1 pressure found, 2 no data.
"""

from __future__ import annotations

DESCRIPTION = __doc__

import argparse
import fcntl
import glob
import hashlib
import http.client
import io
import json
import math
import os
import re
import shutil
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from luna_doctor import attribution
from luna_doctor import bundle as bundle_mod
from luna_doctor import explain
from luna_doctor import watch as watch_mod
from luna_doctor import baseline
from luna_doctor import plan
from luna_doctor import report
from luna_doctor import sizing
from luna_doctor import sources
from luna_doctor.render_pretty import render_pretty, reveal, style_for
from luna_doctor.sources import (CLAUDE_STORE, CODEX_STORE, GATEWAY_HOST,
                                 PI_STORE, SWAMP_EXTRACT_CONCURRENCY_DEFAULT,
                                 SWAMP_LLM_CONCURRENCY_DEFAULT)

VERSION = "0.2.0-alpha.3"  # keep in sync with ./VERSION and the git tag (scripts/release.sh)
SCHEMA_VERSION = 4         # 4: capacity fields renamed to the LunaRoute
                           #     vocabulary (contract.advised_concurrency,
                           #     findings.priority_concurrency: null until
                           #     the plan pass) and the additive combined
                           #     snapshot (metrics.snapshot). The later,
                           #     additive metrics.capacity block and the
                           #     capacity command stay on 4: additive, no
                           #     break.

# ---- measured constants -----------------------------------------------------
CONTRACT = {
    # The admission deadline is deliberately absent: it is per model (measured
    # 120.2s on glm-5.3, 60.3s on deepseek-4.1-flash) and reported per model in
    # every run. A borrowed constant is wrong for part of the fleet.
    "pre_header_wait_s": 25.2,
    "client_idle_timeout_s": 300.0,
    "upstream_hold_s": 10.1,
    "keepalive_s": 10.0,
    "interactive_burst_cap": 10,
    "advised_concurrency": 8,
}

PRE_HEADER_WAIT_S = 25.2        # fixed wait before SSE headers, load-independent
# The value measured on glm-5.3, used as the chart's reference line label. Never
# advice and never a default for a model without observations: the deadline is
# per model (60.3s on deepseek-4.1-flash) and `analyze` reports each model's own.
ADMISSION_DEADLINE_GLM53_S = 120.2
CLIENT_IDLE_TIMEOUT_S = 300.0   # pi DEFAULT_HTTP_IDLE_TIMEOUT_MS
UPSTREAM_HOLD_S = 10.1          # 503 INFERENCE_UNAVAILABLE hold
KEEPALIVE_S = 10.0              # SSE keep-alive cadence in background tier
INTERACTIVE_BURST_CAP = 10      # per-burst interactive class cap: min(n, 10)
ADVISED_CONCURRENCY = 8         # our recommendation: below the cap, with headroom
# What user-facing copy says wherever it used to name the gateway's cap as a
# constant. The admitted width is specific to the account and cannot be read
# from these logs, so copy points at the number it sits next to ("roughly
# this many") and says so - it never tells the reader to go measure.
# INTERACTIVE_BURST_CAP stays for arithmetic, never prose.
CAP_COPY = ("the gateway admits roughly this many interactive requests at once; "
            "the admitted width is specific to the account and cannot be read "
            "from these logs")

# The attribution verdict's words. The slugs are the stable contract (--json);
# these strings are what a human sees, and they are deliberately the same words
# the skill documents, so an agent quoting the line and a reader quoting the
# skill cannot drift apart.
_VERDICT_WORDS = {
    "you": "you (this machine)",
    "your-org": "your org (the account)",
    "the-gateway": "the gateway",
    "not-concurrency": "not concurrency",
    "unknown": "unknown",
}

# Harness store locations, the gateway host and the swamp knob defaults live
# in sources.py - the data-collection layer - and are imported above.
LR_CONFIG = "~/.config/lunaroute/config.json"


# ---- model ------------------------------------------------------------------
@dataclass
class Request:
    harness: str
    session: str
    model: str
    run: str = ""
    provider: str = ""
    start: float | None = None
    end: float | None = None
    out_tokens: int = 0
    in_tokens: int = 0
    stop_reason: str | None = None
    error_message: str | None = None
    response_id: str | None = None
    tier: str = "interactive"
    demoted: bool = False
    reason: str = ""
    outcome: str = "unknown"
    thinking_chars: int = 0
    text_chars: int = 0
    # The harness's own usage record, as logged, per harness and never merged:
    # pi writes input/cacheRead/cacheWrite/output/totalTokens, claude-code
    # input_tokens/cache_read_input_tokens/..., codex
    # input_tokens/cached_input_tokens/... - and codex's input_tokens INCLUDES
    # its cached tokens while pi's input EXCLUDES cacheRead. The normalized
    # in_tokens/out_tokens fields stay as they were; this carries the rest.
    usage_raw: dict = field(default_factory=dict)

    @property
    def duration(self) -> float | None:
        if self.start is None or self.end is None:
            return None
        d = self.end - self.start
        return d if d >= 0 else None

    @property
    def saturated(self) -> bool:
        # "stream_stalled" is not an outcome anything emits: the idle stall is
        # logged as client_idle_timeout, so promising it here invented a third
        # vocabulary word. Saturation is the two outcomes the logs really carry.
        return self.outcome in ("admission_timeout", "client_idle_timeout")


def tier_of(model: str | None, warning: str | None = None) -> tuple[str, bool, str]:
    """Return (tier, demoted, reason).

    `reason` names how a demotion was seen: "overflow" when the interactive
    cap spilled the request into the background tier, "no_entitlement" when
    a `-background` request had no background entitlement and was downgraded
    into the priority pool instead. Only a live response header carries a
    warning; log records never do, so log-derived requests get "".
    """
    m = (model or "").lower()
    w = warning or ""
    if "no-background-entitlement" in w:
        return "interactive", True, "no_entitlement"
    if "overflowed-to-background" in w:
        return "background", True, "overflow"
    if m.endswith("-background") or "-background-" in m:
        return "background", False, ""
    return "interactive", False, ""


def classify(rec: dict) -> str:
    """Outcome bucket from a log record (pi/claude shapes normalized first).

    String sets verified against the real stores, not guessed: pi emits
    `scheduler admission timeout` (gateway), `OpenAI Responses stream ended
    before a terminal response event` / `Stream ended without finish_reason`
    (pi client idle budget), `This operation was aborted` / `Operation aborted`
    (client), `Connection error.`, `Request timed out.`, and 5xx passthrough.
    """
    err = rec.get("error_message") or ""
    low = err.lower()
    stop = rec.get("stop_reason") or ""
    if "scheduler admission timeout" in low:
        return "admission_timeout"
    if ("concurrent_request_limit_exceeded" in low
            or "maximum number of concurrent requests" in low):
        return "org_concurrency_limit"
    # Ordered before model_not_found: these carry `param: model` too, and a
    # context-length 400 must not be answered with "refresh your model list".
    if "maximum context length" in low or "context length" in low and "tokens" in low:
        return "context_length_exceeded"
    if "message limit reached" in low or "message limit exceeded" in low:
        return "message_limit"
    if "payload_too_large" in low or err.startswith("413"):
        return "payload_too_large"
    if ("model_not_found" in low
            or re.search(r"model\s+['\"`][^'\"`]+['\"`]\s+(does not exist|not found|unknown)", low)
            or "unknown model" in low or "model not found" in low):
        # Need the *model* named as missing. A bare 404 is a route or resource,
        # and `param: model` alone is present on other 400s besides this one.
        return "model_not_found"
    if "request deadline exceeded" in low or "provider unhealthy after" in low:
        return "harness_deadline"
    if ("stream ended without finish_reason" in low
            or "stream ended before a terminal response event" in low):
        return "client_idle_timeout"
    if ("inference_unavailable" in low or "backend unavailable" in low
            or err.startswith("503")):
        return "upstream_unavailable"
    if "internal_error" in low or err.startswith("500") or err.startswith("502"):
        return "upstream_error"
    if "failed to resolve api key" in low or "401" in low[:4]:
        return "auth_error"
    # A 429 here is this gateway's report that the account's concurrent-request
    # capacity is full: there is no separate quota dimension, so the class is
    # never a "wait it out" signal. The name stays `rate_limited` because it is
    # part of the JSON contract; only the advice behind it is about how many
    # concurrent requests you hold at once.
    if "rate_limit_error" in low or err.startswith("429") or "would exceed your account" in low:
        return "rate_limited"
    if "aborted" in low or stop == "aborted":
        return "aborted"
    if "connection error" in low or "connection reset" in low:
        return "connection_error"
    if "timed out" in low or "timeout" in low:
        return "request_timeout"
    if stop in ("stop", "stop_sequence", "toolUse", "tool_use", "end_turn", "length"):
        return "completed"
    if err:
        return "other_error"
    return "completed" if rec.get("out_tokens") else "unknown"


# ---- adapters ---------------------------------------------------------------
def _iso_to_epoch(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def iter_jsonl(path: str):
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except (OSError, UnicodeError):
        return


def _session_name(path: str) -> str:
    """Subagent runs store their log as `<project>/<session>/<runid>/run-N/session.jsonl`.
    Name those by run id + run dir instead of collapsing them all to 'session'."""
    name = os.path.basename(path).rsplit(".jsonl", 1)[0]
    if name == "session":
        run_dir = os.path.dirname(path)
        run_id = os.path.basename(os.path.dirname(run_dir))
        return f"{run_id}/{os.path.basename(run_dir)}"
    return name


def _run_of(path: str, root: str, qualify: bool = False, harness: str = "") -> str:
    """The project directory a session file belongs to.

    One experiment directory is one run: in the mirrored rat stores every file
    sits under `--rat-...-n10-c10-...--/`, so grouping by the first path
    component under the root reproduces per-run counts.
    """
    rel = os.path.relpath(os.path.dirname(path) or ".", root)
    head = rel.split(os.sep)[0]
    prefix = f"{harness}:" if harness else ""
    if head in (".", ""):
        # A file sitting directly under the root: the root *is* the run. Dropping
        # it here would merge several flat experiment roots into "(unattributed)".
        label = os.path.basename(os.path.normpath(root)) or "store"
        if not qualify:
            return f"{prefix}{label}"
        return f"{prefix}{label}#{hashlib.sha1(os.path.realpath(root).encode()).hexdigest()[:6]}"
    label = os.path.basename(os.path.normpath(root)) or "store"
    if qualify:
        # Two roots can share a basename (/archive-a/pi and /archive-b/pi), so
        # qualify with a digest of the canonical path when a harness has several.
        label += "#" + hashlib.sha1(os.path.realpath(root).encode()).hexdigest()[:6]
    return f"{prefix}{label}/{head}"


# ---- vendor filter -------------------------------------------------------
# The tool explains LunaRoute concurrency, so the request set it analyses must
# be traffic the gateway actually served. pi says so per record (message.
# provider); claude-code does not, so its route is read from the model name.
# The filters live in the iterators - every consumer (analyze, chart, fix,
# share card) then sees a LunaRoute-only set - and the exclusions travel on
# the returned list, because dropping records without saying so is a silent
# lie about the machine's traffic.

class Requests(list):
    """A request list plus the exclusions the iterators made building it.

    `analyse` reads `.excluded` to report what was dropped and why. A plain
    list - one assembled by hand, or by code that filtered nothing - carries
    no exclusions and `analyse` reports none, which is the truth about it.
    """

    def __init__(self, iterable=(), excluded=None):
        super().__init__(iterable)
        self.excluded = {} if excluded is None else excluded


# LunaRoute's alias naming for models it fronts: claude-opus-lr11,
# claude-haiku-lr34. Verified against the real claude-code store, where every
# -lr model's records carry the gateway's own msg_<uuid> response ids, never
# Anthropic's native msg_011C... ids.
LR_ALIAS_RE = re.compile(r"-lr\d+$")


def claude_route(model: str, catalog) -> str:
    """Which route served one claude-code request: "lunaroute", "anthropic"
    (direct), or "" when routing cannot be established from the logs.

    claude-code records no provider, so the route is read from the model
    name, strongest evidence first:
      - a model in the LunaRoute catalog was served by the gateway
        (`catalog` is its id set; None = no catalog was available);
      - a `-lr<digits>` alias is LunaRoute's own naming, so it is gateway
        traffic even when today's catalog no longer lists it
        (claude-opus-lr11 was served when that window was live);
      - any other `claude-...` name is one Anthropic itself serves: direct
        traffic that never touched the gateway (in the real store those
        records carry Anthropic-native msg_011C... ids and non-empty
        inference_geo, while the gateway's carry msg_<uuid> and empty);
      - anything else (a `<synthetic>` local record, a model from some other
        router) is unclassifiable from the logs alone.
    A `-background` suffix is LunaRoute's own tier naming, stripped before
    the catalog match: the background variant of a catalog model is the same
    gateway serving a different tier.
    """
    name = model or ""
    base = name[:-len("-background")] if name.endswith("-background") else name
    if base in (catalog or ()):
        return "lunaroute"
    if LR_ALIAS_RE.search(base):
        return "lunaroute"
    if base.startswith("claude-"):
        return "anthropic"
    return ""


def iter_pi(roots: list[str], since: float, until: float | None = None,
            prune_mtime: bool = True) -> Requests:
    out: list[Request] = []
    seen: set[str] = set()
    pi_excluded: dict[str, int] = {}      # provider -> requests dropped
    top = until if until is not None else time.time()
    for root in sorted(roots):     # argv order must not decide which copy survives
        pattern = os.path.join(glob.escape(root), "**", "*.jsonl")
        for path in sorted(glob.glob(pattern, recursive=True)):
            # A live local store has a trustworthy mtime, so a cheap prune is
            # safe there. A mirrored archive preserves the *source* mtime, which
            # is why explicit roots never prune: a months-old mtime routinely
            # hides data from yesterday.
            if prune_mtime:
                try:
                    if os.path.getmtime(path) < since - 86400:
                        continue
                except OSError:
                    continue
            session = _session_name(path)
            run = _run_of(path, root, qualify=len(roots) > 1, harness="pi")
            for obj in iter_jsonl(path):
                if obj.get("type") != "message":
                    continue
                msg = obj.get("message") or {}
                if msg.get("role") != "assistant":
                    continue
                end = _iso_to_epoch(obj.get("timestamp"))
                if end is None or end < since or end > top:
                    continue
                start_ms = msg.get("timestamp")
                start = (start_ms / 1000.0) if isinstance(start_ms, (int, float)) else None
                model = msg.get("model") or ""
                provider = msg.get("provider") or ""
                usage = msg.get("usage") or {}
                rid = msg.get("responseId")
                key = rid or f"{session}:{obj.get('id')}"
                if key in seen:
                    continue
                seen.add(key)
                if provider != "lunaroute":
                    # pi names the provider that answered, and LunaRoute
                    # concurrency is all this tool explains: ollama, gengen,
                    # a direct Anthropic key or a test gateway never touched
                    # the gateway, and counting them here would inflate its
                    # concurrency, latency and per-model stats. No record in
                    # the real store lacks the field - it predates every
                    # record there - so an empty provider is not attributable
                    # to the gateway either: excluded and counted, never
                    # guessed in.
                    name = provider or "(no provider)"
                    pi_excluded[name] = pi_excluded.get(name, 0) + 1
                    continue
                content = msg.get("content") or []
                think = text = 0
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict):
                        continue
                    t = block.get("type")
                    if t == "thinking":
                        think += len(block.get("thinking") or "")
                    elif t == "text":
                        text += len(block.get("text") or "")
                req = Request(
                    harness="pi",
                    session=session,
                    model=model,
                    run=run,
                    provider=provider,
                    start=start,
                    end=end,
                    out_tokens=int(usage.get("output") or 0),
                    in_tokens=int(usage.get("input") or 0),
                    stop_reason=msg.get("stopReason"),
                    error_message=msg.get("errorMessage"),
                    response_id=rid,
                    thinking_chars=think,
                    text_chars=text,
                    usage_raw=dict(usage),
                )
                req.tier, req.demoted, req.reason = tier_of(model)
                req.outcome = classify(asdict(req))
                out.append(req)
    return Requests(out, excluded={
        "pi_non_lunaroute": sum(pi_excluded.values()),
        "pi_by_provider": dict(sorted(pi_excluded.items())),
    })


def iter_claude(roots: list[str], since: float, until: float | None = None,
                prune_mtime: bool = True, catalog=None) -> Requests:
    """claude-code: one response spans several assistant records sharing
    message.id, and mirrored copies of it may differ.

    Copies are merged globally by message id - not per session - so a mirrored
    and a local copy of one response count once, keeping the earliest plausible
    start, the latest end and whatever terminal metadata either copy carried. The
    surviving session/run label comes from the first copy in sorted path order.

    Only LunaRoute-routed responses are returned: `catalog` is the LunaRoute
    catalog's model ids (None when none was available), a response whose route
    cannot be established is excluded and counted (see claude_route), and the
    counts travel on the returned list's `.excluded`.
    """
    out: list[Request] = []
    # Response identity is global, not per file. Copies of one response can differ:
    # an incomplete copy can sort before its complete twin, and first-wins would
    # then discard the terminal outcome, the usage and the real duration.
    best: dict[str, dict] = {}
    anthropic_direct: dict[str, int] = {}   # model -> requests dropped (direct)
    unclassified: dict[str, int] = {}        # model -> requests dropped (unknown)
    # Copies of one response can straddle the window edge: the fragment with the
    # real start can end before `since` while the terminal record lands inside it.
    # Keeping the whole stale group would hold a mirror in memory, so a pre-window
    # copy contributes only its start.
    pre_starts: dict[str, float] = {}
    top = until if until is not None else time.time()
    for root in sorted(roots):     # argv order must not decide which copy survives
        pattern = os.path.join(glob.escape(root), "**", "*.jsonl")
        for path in sorted(glob.glob(pattern, recursive=True)):
            # Sorted so the copy that survives de-duplication is deterministic.
            if prune_mtime:      # see iter_pi: explicit roots never prune
                try:
                    if os.path.getmtime(path) < since - 86400:
                        continue
                except OSError:
                    continue
            session = _session_name(path)
            run = _run_of(path, root, qualify=len(roots) > 1, harness="claude-code")
            prev_ts: float | None = None
            pending_err: str | None = None
            groups: dict[str, dict] = {}
            order: list[str] = []
            for obj in iter_jsonl(path):
                ts = _iso_to_epoch(obj.get("timestamp"))
                typ = obj.get("type")
                if typ == "assistant":
                    msg = obj.get("message") or {}
                    mid = msg.get("id") or obj.get("uuid")
                    if not mid:
                        continue
                    g = groups.get(mid)
                    if g is None:
                        g = {
                            "start": prev_ts or ts,
                            "end": ts,
                            "model": msg.get("model") or "",
                            "usage": msg.get("usage") or {},
                            "stop": msg.get("stop_reason"),
                            "err": pending_err,
                        }
                        pending_err = None
                        groups[mid] = g
                        order.append(mid)
                    g["end"] = ts or g["end"]
                    u = msg.get("usage") or {}
                    if u.get("output_tokens") is not None:
                        g["usage"] = u
                    if msg.get("stop_reason"):
                        g["stop"] = msg["stop_reason"]
                elif typ == "system" and obj.get("subtype") == "api_error":
                    msg_err = _short_err(obj.get("error") or obj)
                    # A retry loop can emit api_error records before the first
                    # assistant record exists; hold it for the next response
                    # instead of dropping it (this is how 429s got lost).
                    if order and groups[order[-1]].get("err") is None:
                        groups[order[-1]]["err"] = msg_err
                    else:
                        pending_err = msg_err
                if ts is not None:
                    prev_ts = ts

            for mid in order:
                g = groups[mid]
                if g["end"] is None or g["end"] > top:
                    continue
                if g["end"] < since:
                    start = g.get("start")
                    if start is not None:      # the preceding record's time, always useful
                        prev_start = pre_starts.get(mid)
                        pre_starts[mid] = start if prev_start is None else min(start, prev_start)
                    continue
                g = {**g, "session": session, "run": run}
                prev = best.get(mid)
                best[mid] = g if prev is None else _merge_copies(prev, g)

    for mid, g in sorted(best.items()):
        if g["end"] is None or g["end"] < since or g["end"] > top:
            continue
        # claude-code logs no provider, so the route is read from the model
        # name (claude_route). Anthropic-direct sessions and anything
        # unclassifiable are excluded here and counted, so the request set
        # that reaches every consumer is gateway traffic only.
        route = claude_route(g["model"], catalog)
        if route != "lunaroute":
            name = g["model"] or "(no model)"
            bucket = anthropic_direct if route == "anthropic" else unclassified
            bucket[name] = bucket.get(name, 0) + 1
            continue
        # Applied here, not per file: the fragment can live in a file (or a root)
        # that is read after the terminal copy, and the start is still the same.
        if mid in pre_starts:
            ps = pre_starts[mid]
            if g.get("start") is None or ps < g["start"]:
                g = {**g, "start": ps}
        u = g["usage"] or {}
        req = Request(
            harness="claude-code",
            session=g["session"],
            model=g["model"],
            run=g["run"],
            provider="",
            start=g["start"],
            end=g["end"],
            out_tokens=int(u.get("output_tokens") or 0),
            in_tokens=int(u.get("input_tokens") or 0),
            stop_reason=g["stop"],
            error_message=g["err"],
            response_id=mid,
            usage_raw=dict(u),
        )
        req.tier, req.demoted, req.reason = tier_of(req.model)
        req.outcome = classify(asdict(req))
        out.append(req)
    return Requests(out, excluded={
        "claude_non_lunaroute": sum(anthropic_direct.values()),
        "claude_by_model": dict(sorted(anthropic_direct.items())),
        "claude_unclassified": sum(unclassified.values()),
        "claude_unclassified_by_model": dict(sorted(unclassified.items())),
    })


def _merge_copies(a: dict, b: dict) -> dict:
    """One response from two copies, keeping what each copy knows.

    Copies differ by truncation, not by disagreement: one may hold the start
    (a preceding record), another the terminal record and the usage. So the span
    is the earliest plausible start to the latest end, the terminal fields come
    from whichever copy has them, and usage takes the larger of each counter.
    Picking a "winning" copy instead loses exactly one of those dependably.
    """
    ends = [x for x in (a.get("end"), b.get("end")) if x is not None]
    end = max(ends) if ends else None
    starts = [x for x in (a.get("start"), b.get("start"))
              if x is not None and end is not None and x < end]
    start = min(starts) if starts else (a.get("start") if a.get("start") is not None
                                        else b.get("start"))
    ua, ub = a.get("usage") or {}, b.get("usage") or {}
    usage: dict = {}
    for k in set(ua) | set(ub):
        va, vb = ua.get(k), ub.get(k)
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            usage[k] = max(va, vb)
        else:                       # claude also puts strings here, e.g. service_tier
            usage[k] = va if va is not None else vb
    return {
        **b,
        "start": start,
        "end": end,
        "model": a.get("model") or b.get("model"),
        "stop": a.get("stop") or b.get("stop"),
        "err": a.get("err") or b.get("err"),
        "usage": usage,
        "session": a.get("session") or b.get("session"),
        "run": a.get("run") or b.get("run"),
    }


# ---- swamp discovery ------------------------------------------------------
# Task-1 contract, revised: filesystem only. `swamp workspace list` cost a
# second per run and reported nothing the filesystem could not, so swamp is
# resolved by walking directories and never by spawning swamp itself.
# discover() exports stores.swamp.repoDir for downstream tasks (analyze/fix/load-test).

def _swamp_repo_dir() -> str | None:
    """Resolve one swamp repo dir from the filesystem alone, or None.

    Precedence (highest first):
      1. $SWAMP_ROOT \u2014 explicit operator override; authoritative, and a
         dir without the marker still means "no swamp here".
      2. upward scan for .swamp.yaml, from the cwd and from this checkout.
      3. ~/.swamp \u2014 swamp's default workspace location.
      4. a swamp-tracker/swamp sibling of the luna-doctor wrapper.
    Every candidate must carry the .swamp.yaml marker: ~/.swamp in
    particular also exists on machines where it holds only swamp's cache.
    """
    env_root = os.environ.get("SWAMP_ROOT", "").strip()
    if env_root:
        cand = os.path.expanduser(env_root)
        if os.path.isdir(cand) and os.path.isfile(os.path.join(cand, ".swamp.yaml")):
            return os.path.abspath(cand)
        return None
    this_dir = os.path.dirname(os.path.abspath(__file__))
    cur = os.path.dirname(this_dir)  # repo root (parent of luna_doctor/)
    for start in (os.getcwd(), cur):
        found = _scan_for_swamp_yaml(start)
        if found:
            return found
    home_swamp = os.path.expanduser("~/.swamp")
    if os.path.isfile(os.path.join(home_swamp, ".swamp.yaml")):
        return os.path.abspath(home_swamp)
    # wrapper sibling lives one level above the wrapper (luna-doctor wrapper -> GitHub/)
    wrapper = os.path.dirname(cur)
    for base in (wrapper, os.path.dirname(wrapper), cur):
        for name in ("swamp-tracker", "swamp"):
            cand = os.path.join(base, name)
            try:
                if os.path.isfile(os.path.join(cand, ".swamp.yaml")):
                    return os.path.abspath(cand)
            except OSError:
                continue
    return None


def _scan_for_swamp_yaml(start: str) -> str | None:
    """Walk upward from start looking for .swamp.yaml; return its dir or None."""
    cur = os.path.abspath(start)
    for _ in range(20):
        candidate = os.path.join(cur, ".swamp.yaml")
        try:
            if os.path.isfile(candidate):
                return cur
        except OSError:
            pass
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def _count_swamp_activity(root: str, filename_globs: tuple, since: float | None) -> int:
    """Count files under `root` whose mtime falls at or after `since` (epoch).

    mtime, not content: the question is whether swamp is being *used*, and one
    stat per file is the cheapest honest answer. `since` None counts all
    activity regardless of age.
    """
    if not os.path.isdir(root):
        return 0
    n = 0
    for pat in filename_globs:
        for path in glob.glob(os.path.join(root, "**", pat), recursive=True):
            try:
                if since is not None and os.stat(path).st_mtime < since:
                    continue
            except OSError:
                continue
            n += 1
    return n


def swamp_state(repo_dir: str | None = None, since: float | None = None) -> dict:
    """Is swamp installed, and is it actually running inside the window?

    Installed is not in use: `in_use` requires files under `.swamp/data` or
    `.swamp/workflow-runs` modified at or after `since` (epoch seconds; None
    counts all activity). A populated but stale store is installed, not in
    use, and an idle install must cost luna-doctor nothing beyond the marker
    check. Resolution is filesystem-only; swamp itself is never spawned.
    """
    repo = repo_dir if repo_dir is not None else _swamp_repo_dir()
    if not repo or not os.path.isdir(repo) or not os.path.isfile(os.path.join(repo, ".swamp.yaml")):
        return {"installed": False, "in_use": False, "repo": None,
                "behaviors": 0, "workflow_runs": 0, "evidence": "no marker"}
    behaviors = _count_swamp_activity(os.path.join(repo, ".swamp", "data"),
                                      ("metadata.yaml",), since)
    workflow_runs = _count_swamp_activity(os.path.join(repo, ".swamp", "workflow-runs"),
                                           ("*.yaml", "*.yml"), since)
    in_use = bool(behaviors or workflow_runs)
    if in_use:
        parts = []
        if behaviors:
            parts.append(f"{behaviors} behavior record(s)")
        if workflow_runs:
            parts.append(f"{workflow_runs} workflow run(s)")
        evidence = ", ".join(parts) + " in window"
    else:
        evidence = "installed, no activity"
    return {"installed": True, "in_use": in_use, "repo": os.path.abspath(repo),
            "behaviors": behaviors, "workflow_runs": workflow_runs, "evidence": evidence}


def _swamp_store_info(repo_dir: str | None) -> dict:
    """Build stores.swamp payload. Absent -> present:false, no faked data."""
    if repo_dir is None:
        return {
            "present": False,
            "repoDir": None,
            "files": {"behaviorHistory": 0, "workflowRuns": 0},
            "bundleFingerprint": None,
            "envPresence": False,
        }
    bh_count = 0
    swamp_data = os.path.join(repo_dir, ".swamp", "data")
    if os.path.isdir(swamp_data):
        try:
            bh_count = len(glob.glob(os.path.join(swamp_data, "**", "metadata.yaml"), recursive=True))
        except OSError:
            pass
    wf_count = 0
    wf_root = os.path.join(repo_dir, ".swamp", "workflow-runs")
    if os.path.isdir(wf_root):
        try:
            wf_count = len(glob.glob(os.path.join(wf_root, "**", "*.yaml"), recursive=True))
            wf_count += len(glob.glob(os.path.join(wf_root, "**", "*.yml"), recursive=True))
        except OSError:
            pass
    fingerprint = None
    bundles_root = os.path.join(repo_dir, ".swamp", "bundles")
    try:
        if os.path.isdir(bundles_root):
            entries = sorted(os.listdir(bundles_root))
            for e in entries:
                if re.fullmatch(r"[0-9a-f]{7,64}", e):
                    fingerprint = e
                    break
            if fingerprint is None and entries:
                fingerprint = entries[0]
    except OSError:
        pass
    env_present = False
    try:
        env_present = os.path.isfile(os.path.join(repo_dir, ".env.swamp"))
    except OSError:
        pass
    return {
        "present": True,
        "repoDir": os.path.abspath(repo_dir),
        "files": {"behaviorHistory": bh_count, "workflowRuns": wf_count},
        "bundleFingerprint": fingerprint,
        "envPresence": env_present,
    }


# ---- swamp analyze helpers ------------------------------------------------
# Task-2 contract: iter_swamp with env -> .env.swamp -> default knob
# resolution. The knob resolution itself lives in sources.swamp_knobs (one
# reader, provenance per value); iter_swamp and the activity scan stay here.

def _yaml_field_value(text: str, field: str) -> str | None:
    try:
        m = re.search(rf"^\s*{re.escape(field)}\s*:\s*['\"]?([^'\"\n]+)['\"]?", text, re.MULTILINE)
        if m:
            return m.group(1).strip().strip("'\"")
    except Exception:
        pass
    return None

def iter_swamp(repoDir: str | None, since: float | None = None, until: float | None = None):
    """Yield swamp records in window. Never fabricates per-request concurrency.

    Each yielded dict has {source, status, ts, path}.
    Walks .swamp/data/**/metadata.yaml (behavior) and .swamp/workflow-runs/**/*.yaml
    """
    if repoDir is None:
        repoDir = _swamp_repo_dir()
    if not repoDir or not os.path.isdir(repoDir):
        return
        yield  # make it a generator
    sw = os.path.join(repoDir, ".swamp")
    data_root = os.path.join(sw, "data")
    if os.path.isdir(data_root):
        for path in glob.glob(os.path.join(data_root, "**", "metadata.yaml"), recursive=True):
            try:
                text = open(path, "r", errors="replace").read()
            except OSError:
                continue
            try:
                if os.path.islink(path):
                    tgt = os.path.realpath(path)
                    if not tgt.startswith(os.path.realpath(repoDir)):
                        continue
            except Exception:
                pass
            ts_raw = _yaml_field_value(text, "createdAt") or _yaml_field_value(text, "startedAt") or _yaml_field_value(text, "timestamp")
            ts = _iso_to_epoch(ts_raw) if ts_raw else None
            if since is not None and ts is not None and ts < since:
                continue
            if until is not None and ts is not None and ts > until:
                continue
            status = (_yaml_field_value(text, "status") or "succeeded").lower()
            if status not in ("succeeded", "running", "failed"):
                if "fail" in status:
                    status = "failed"
                elif "run" in status:
                    status = "running"
                else:
                    status = "succeeded"
            yield {"source": "behavior", "status": status, "ts": ts, "path": path,
                  "name": _yaml_field_value(text, "name")}
    wr_root = os.path.join(sw, "workflow-runs")
    if os.path.isdir(wr_root):
        for pat in (os.path.join(wr_root, "**", "*.yaml"), os.path.join(wr_root, "**", "*.yml")):
            for path in glob.glob(pat, recursive=True):
                try:
                    text = open(path, "r", errors="replace").read()
                except OSError:
                    continue
                try:
                    if os.path.islink(path):
                        tgt = os.path.realpath(path)
                        if not tgt.startswith(os.path.realpath(repoDir)):
                            continue
                except Exception:
                    pass
                ts_raw = _yaml_field_value(text, "startedAt") or _yaml_field_value(text, "createdAt") or _yaml_field_value(text, "timestamp")
                ts = _iso_to_epoch(ts_raw) if ts_raw else None
                if since is not None and ts is not None and ts < since:
                    continue
                if until is not None and ts is not None and ts > until:
                    continue
                status = (_yaml_field_value(text, "status") or "succeeded").lower()
                if status not in ("succeeded", "running", "failed"):
                    if "fail" in status:
                        status = "failed"
                    elif "run" in status:
                        status = "running"
                    else:
                        status = "succeeded"
                yield {"source": "workflow", "status": status, "ts": ts, "path": path,
                      "name": None}

def _collect_swamp_metrics(repoDir: str | None, since: float | None, until: float | None) -> dict:
    """Build metrics.swamp for the window. Additive, never throws."""
    disc = None
    if repoDir is not None:
        files_bh = files_wr = 0
        bundle_fp = None
        env_presence = False
        try:
            sw = os.path.join(repoDir, ".swamp")
            if os.path.isdir(os.path.join(sw, "data")):
                files_bh = len(glob.glob(os.path.join(sw, "data", "**", "metadata.yaml"), recursive=True))
            if os.path.isdir(os.path.join(sw, "workflow-runs")):
                files_wr = len(glob.glob(os.path.join(sw, "workflow-runs", "**", "*.yaml"), recursive=True))
                files_wr += len(glob.glob(os.path.join(sw, "workflow-runs", "**", "*.yml"), recursive=True))
            bundles = os.path.join(sw, "bundles")
            if os.path.isdir(bundles):
                subs = sorted([d for d in os.listdir(bundles) if os.path.isdir(os.path.join(bundles, d))])
                for e in subs:
                    if re.fullmatch(r"[0-9a-f]{7,64}", e):
                        bundle_fp = e
                        break
                if bundle_fp is None and subs:
                    bundle_fp = subs[0]
            env_presence = os.path.isfile(os.path.join(repoDir, ".env.swamp"))
        except Exception:
            pass
        disc = {"present": True, "repoDir": os.path.abspath(repoDir), "files": {"behaviorHistory": files_bh, "workflowRuns": files_wr}, "bundleFingerprint": bundle_fp, "envPresence": env_presence}
    else:
        disc = _swamp_store_info(_swamp_repo_dir())
    if not disc.get("present"):
        return {"present": False}
    repo = disc.get("repoDir")
    knobs = sources.swamp_knobs(repo)
    bh_counts = {"succeeded": 0, "running": 0, "failed": 0, "total": 0}
    wr_counts = {"succeeded": 0, "running": 0, "failed": 0, "total": 0}
    names: set = set()
    for rec in iter_swamp(repo, since, until):
        tgt = bh_counts if rec["source"] == "behavior" else wr_counts
        tgt[rec["status"]] = tgt.get(rec["status"], 0) + 1
        tgt["total"] += 1
        if rec.get("name"):
            names.add(rec["name"])
    bh_total = bh_counts["total"]
    bh_err = round(bh_counts["failed"] / bh_total, 3) if bh_total else None
    wr_total = wr_counts["total"]
    wr_err = round(wr_counts["failed"] / wr_total, 3) if wr_total else None
    try:
        cfg = int(knobs["extractConcurrency"]["value"]) + int(knobs["llmConcurrency"]["value"])
    except Exception:
        cfg = SWAMP_EXTRACT_CONCURRENCY_DEFAULT + SWAMP_LLM_CONCURRENCY_DEFAULT
    return {
        "present": True,
        "repoDir": repo,
        "files": disc.get("files", {"behaviorHistory": 0, "workflowRuns": 0}),
        "bundleFingerprint": disc.get("bundleFingerprint"),
        "envPresence": disc.get("envPresence"),
        "behaviorHistory": {**bh_counts, "errorRate": bh_err},
        "workflowRuns": {**wr_counts, "errorRate": wr_err},
        "behaviors": sorted(names),
        "knobs": knobs,
        "configuredConcurrency": cfg,
    }


def discover(verbose: bool = False) -> dict:

    """What stores exist, how big, over what range. Never modifies them."""
    found = {}
    for name, root in (("pi", PI_STORE), ("claude-code", CLAUDE_STORE), ("codex", CODEX_STORE)):

        r = os.path.expanduser(root)
        files = glob.glob(os.path.join(r, "**", "*.jsonl*"), recursive=True) if os.path.isdir(r) else []
        size = 0
        newest = oldest = None
        for p in files[:: max(1, len(files) // 400)] if files else []:
            try:
                st = os.stat(p)
            except OSError:
                continue
            size += st.st_size
            newest = max(newest or st.st_mtime, st.st_mtime)
            oldest = min(oldest or st.st_mtime, st.st_mtime)
        found[name] = {
            "root": r,
            "present": os.path.isdir(r),
            "files": len(files),
            "bytes_sampled": size,
            "newest": datetime.fromtimestamp(newest, timezone.utc).isoformat() if newest else None,
            "oldest": datetime.fromtimestamp(oldest, timezone.utc).isoformat() if oldest else None,
        }
    found["lunaroute_config"] = {"path": os.path.expanduser(LR_CONFIG),
                                 "present": os.path.isfile(os.path.expanduser(LR_CONFIG))}
    # Installed is not in use: the report shows swamp only with activity in
    # the window, while --json keeps the stores.swamp stanza for compatibility.
    sw_state = swamp_state(since=time.time() - 24 * 3600)
    sw = _swamp_store_info(sw_state["repo"])
    sw["inUse"] = sw_state["in_use"]
    sw["evidence"] = sw_state["evidence"]
    found["swamp"] = sw
    return found


# ---- metrics ----------------------------------------------------------------
def pct(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    if len(s) == 1:
        return round(s[0], 1)
    k = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return round(s[k], 1)


def fmt(v, unit: str = "") -> str:
    """Never print None into a report."""
    return "n/a" if v is None else f"{v}{unit}"


def envelope(command: str, payload: dict, **extra) -> dict:
    """Every --json document is attributable: which build, which schema."""
    doc = {"schema": SCHEMA_VERSION, "version": VERSION, "command": command}
    doc.update(extra)
    doc.update(payload)
    return doc


def human_window(hours: float) -> str:
    if hours * 60 < 60:
        return f"{round(hours * 60)}m"
    if hours < 24:
        return f"{round(hours)}h"
    return f"{hours / 24:g}d"


def max_concurrency(reqs: list[Request]) -> tuple[int, float | None]:
    events = []
    for r in reqs:
        if r.start is None or r.end is None or r.end <= r.start:
            continue        # zero-length: it never overlapped anything
        events.append((r.start, 1))
        events.append((r.end, -1))
    events.sort()
    cur = peak = 0
    at = None
    for ts, delta in events:
        cur += delta
        if cur > peak:
            peak, at = cur, ts
    return peak, at


def concurrency_at(reqs: list[Request], ts: float) -> int:
    return sum(1 for r in reqs if r.start is not None and r.end is not None and r.start <= ts <= r.end)


def _short_err(err) -> str:
    if isinstance(err, dict):
        inner = err.get("error") if isinstance(err.get("error"), dict) else None
        msg = (inner or err).get("message") if isinstance(inner or err, dict) else None
        return str(msg or err)[:400]
    return str(err)[:400]


def pressure_windows(reqs: list[Request], threshold: int) -> list[dict]:
    """Contiguous stretches where more than `threshold` requests were in flight.

    This is overlap-based on purpose. An earlier version grouped requests whose
    *starts* fell within 15s, which chained sequential traffic into a "burst of
    72" while true concurrency was 4 - and then gave advice based on it.
    """
    events: list[tuple[float, int]] = []
    for r in reqs:
        if r.start is None or r.end is None or r.end < r.start:
            continue
        events.append((r.start, 1))
        events.append((r.end, -1))
    events.sort()
    out: list[dict] = []
    cur = 0
    opened: float | None = None
    peak = 0
    for ts, delta in events:
        cur += delta
        if cur > threshold and opened is None:
            opened, peak = ts, cur
        elif opened is not None:
            peak = max(peak, cur)
            if cur <= threshold:
                out.append({
                    "start": datetime.fromtimestamp(opened, timezone.utc).isoformat(),
                    "end": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                    "seconds": round(ts - opened, 1),
                    "max_concurrent": peak,
                })
                opened, peak = None, 0
    return out


def concurrency_by_minute(reqs: list[Request]) -> list[dict]:
    """Minute-level concurrency series.

    agentsview's finest activity bucket is 5 minutes, which is too coarse to
    line a burst up against the 120 s admission deadline. Request windows here
    have second resolution, so bin them per UTC minute: max requests in flight,
    how many started, and how many were blocked at admission inside that minute.
    """
    events: list[tuple[float, int]] = []
    for r in reqs:
        if r.start is None or r.end is None or r.end < r.start:
            continue
        events.append((r.start, 1))
        events.append((r.end, -1))
    if not events:
        return []
    events.sort()
    lo = math.floor(min(events, key=lambda e: e[0])[0] / 60.0) * 60
    hi = max(events, key=lambda e: e[0])[0]
    deaths: dict[int, int] = {}
    starts: dict[int, int] = {}
    for r in reqs:
        if r.start is not None:
            starts[int(r.start // 60)] = starts.get(int(r.start // 60), 0) + 1
        if r.outcome == "admission_timeout" and r.end is not None:
            m = int(r.end // 60)
            deaths[m] = deaths.get(m, 0) + 1
    out: list[dict] = []
    i = 0
    cur = 0
    peak = 0
    minute = int(lo // 60)
    last = int(hi // 60)
    while minute <= last:
        boundary = (minute + 1) * 60.0
        while i < len(events) and events[i][0] < boundary:
            cur += events[i][1]
            peak = max(peak, cur)
            i += 1
        if peak or minute in starts or minute in deaths:
            out.append({
                "minute": datetime.fromtimestamp(minute * 60, timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
                "max_concurrent": peak,
                "started": starts.get(minute, 0),
                "admission_deaths": deaths.get(minute, 0),
            })
        minute += 1
        # a request spanning the boundary is still in flight: sample cur, not 0
        peak = cur
    return out


# ---- forensic detectors -----------------------------------------------------
# Each of these answers a question the plain aggregates cannot, and each is
# calibrated against measured traffic rather than invented.

STORM_MIN_COHORT = 3          # measured: 10-packs at 300.0s, three rounds
STORM_WINDOW_S = 60.0         # a cohort is one blocking event, not a trend
STORM_DURATION_TOLERANCE_S = 1.0   # 300.4 and 300.6 are one pack; 120 and 300 are not
STORM_MIN_OVERLAP = 2             # serial timeouts are a slow client, not a pack
BAND_MIN_TOP_SAMPLES = 5      # below this a p90 is a coin flip
BAND_INFLATION = 2.0
DEADLINE_MIN_SAMPLES = 3      # below this a per-model deadline is "unknown"
ERROR_GAP_S = 900.0           # a re-sighting after 15 quiet minutes is a new occurrence
SANITY_MAX_DURATION_S = 24 * 3600


def error_signature(msg: str | None) -> str:
    """Stable identity for an error string we have never classified.

    Digits, hex runs and UUIDs become '#', so two reports of the same fault
    collapse whether or not they name different ids. Truncation keeps the
    signature readable in a table.
    """
    if not msg:
        return ""
    s = str(msg).lower()
    s = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "#", s)
    s = re.sub(r"\b0x[0-9a-f]+\b", "#", s)
    s = re.sub(r"\b[0-9a-f]{8,}\b", "#", s)
    s = re.sub(r"\d+", "#", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:120]


def unknown_errors(reqs: list[Request]) -> list[dict]:
    """Unclassified error strings, grouped, with timing and duration shape.

    This is how a new gateway behaviour gets noticed without anyone naming it
    first: a string no outcome class claims is reported rather than swallowed.
    """
    groups: dict[str, list[Request]] = {}
    for r in reqs:
        if r.outcome != "other_error":
            continue
        groups.setdefault(error_signature(r.error_message), []).append(r)
    out: list[dict] = []
    for sig, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        stamps = [x.end if x.end is not None else x.start for x in rs]
        stamps = [t for t in stamps if t is not None]
        durs = sorted(x.duration for x in timed(rs))
        out.append({
            "signature": sig,
            "count": len(rs),
            "first": datetime.fromtimestamp(min(stamps), timezone.utc).isoformat() if stamps else None,
            "last": datetime.fromtimestamp(max(stamps), timezone.utc).isoformat() if stamps else None,
            "durations_s": [round(d, 1) for d in durs[:10]],
        })
    return out


def unknown_error_bursts(reqs: list[Request], gap_s: float = ERROR_GAP_S) -> list[dict]:
    """Split each unclassified signature into discrete occurrences.

    One window-wide row per signature would make a repeat history useless: every
    later run would either duplicate the same span or, with overlap dedupe,
    swallow a genuinely new outbreak. A burst is the event.
    """
    by_sig: dict[str, list[float]] = {}
    for r in reqs:
        if r.outcome != "other_error":
            continue
        stamp = r.end if r.end is not None else r.start
        if stamp is None:
            continue
        by_sig.setdefault(error_signature(r.error_message), []).append(stamp)
    out: list[dict] = []
    for sig, stamps in by_sig.items():
        stamps.sort()
        burst: list[float] = []
        for ts in stamps:
            if burst and ts - burst[-1] > gap_s:
                out.append({"signature": sig, "count": len(burst),
                            "first": _iso(burst[0]), "last": _iso(burst[-1])})
                burst = []
            burst.append(ts)
        if burst:
            out.append({"signature": sig, "count": len(burst),
                        "first": _iso(burst[0]), "last": _iso(burst[-1])})
    out.sort(key=lambda b: b["first"] or "")
    return out


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def per_model_deadlines(reqs: list[Request], min_n: int = DEADLINE_MIN_SAMPLES) -> dict[str, dict]:
    """The admission deadline each model actually showed, never a borrowed one."""
    by_model: dict[str, list[float]] = {}
    for r in reqs:
        if r.outcome != "admission_timeout":
            continue
        # Grouped before the duration filter: a model whose failures carry no
        # usable duration is "unknown", which is a finding, not a silence.
        bucket = by_model.setdefault(r.model or "(unknown)", [])
        # Same sanity bound the storm detector uses. A foreign archive whose
        # timestamps are in seconds yields decade-long "durations", and reporting
        # one as an observed gateway deadline is worse than reporting unknown.
        if r.duration is not None and 0 < r.duration <= SANITY_MAX_DURATION_S:
            bucket.append(r.duration)
    out: dict[str, dict] = {}
    for model, durs in by_model.items():
        durs = sorted(durs)
        span = _span([r for r in reqs
                      if r.outcome == "admission_timeout" and (r.model or "(unknown)") == model])
        if len(durs) < min_n:
            out[model] = {"n": len(durs), "seen_n": len([r for r in reqs if r.outcome == "admission_timeout"
                                                          and (r.model or "(unknown)") == model]),
                          "deadline_s": None, "status": "unknown", **span}
            continue
        out[model] = {
            "n": len(durs),
            "deadline_s": round(statistics.median(durs), 1),
            "low_s": round(durs[0], 1),
            "high_s": round(durs[-1], 1),
            "status": "observed",
            "seen_n": len([r for r in reqs if r.outcome == "admission_timeout"
                           and (r.model or "(unknown)") == model]),
            **span,
        }
    return out


def timeout_storms(reqs: list[Request], min_cohort: int = STORM_MIN_COHORT,
                   window_s: float = STORM_WINDOW_S) -> list[dict]:
    """Synchronous retry cohorts: N same-duration timeouts ending together.

    A single 300s stall is weather. Ten of them ending within a minute is a
    client that launched a pack and blocked on all of it.
    """
    storms: list[dict] = []
    by_class: dict[str, list[Request]] = {}
    for r in timed(reqs):      # zero-length and implausible intervals are not events
        if r.outcome not in ("admission_timeout", "client_idle_timeout", "request_timeout"):
            continue
        by_class.setdefault(r.outcome, []).append(r)
    for outcome, rs in by_class.items():
        storms.extend(_sliding_cohorts(outcome, rs, min_cohort, window_s))
    storms.sort(key=lambda s: s["start"] or "")
    return storms


def _member_id(cohort: list[Request]) -> str:
    """Identity of a cohort from its members, exactly.

    Windows cut events; member sets do not move. A partial view and the full
    view hash differently, which means the ledger may record the same pack twice
    (once as seen through a short window, once complete) but can never lose one -
    and the per-window report remains the authoritative account either way.
    """
    # Response identity when the harness gives one; the session *filename* is
    # not stable across mirrored roots, and would make the same cohort hash
    # differently depending on which copy survived de-duplication.
    keys = sorted(f"{r.response_id or f'{r.harness}|{r.session}|{round(r.start or 0, 1)}|{round(r.end or 0, 1)}'}"
                  f"|{r.model}" for r in cohort)
    return hashlib.sha1("\n".join(keys).encode()).hexdigest()[:12]


def _sliding_cohorts(outcome: str, group: list[Request], min_cohort: int,
                     window_s: float) -> list[dict]:
    """Same-duration cohorts, found time-window-first and consumed as found.

    Time-then-duration, not duration-then-time: partitioning by duration globally
    separates a pack's members whenever an unrelated duration lands nearby, and
    neither half then reaches the minimum.

    An emitted cohort's members are marked used and the scan continues from where
    it was, so two packs interleaved in end order — which is exactly what two
    models with different admission deadlines produce — are both reported. No
    cursor arithmetic on `Request` values: two records can compare equal, and
    `list.index` would then walk backwards or loop forever.

    Eligibility (size, duration spread, real overlap) is decided before the best
    cluster is chosen, so a large serial cluster cannot shadow a smaller
    concurrent one.
    """
    group = sorted(group, key=lambda r: r.end or 0)
    used = [False] * len(group)
    out: list[dict] = []
    i = 0
    while i < len(group):
        if used[i]:
            i += 1
            continue
        window: list[int] = []
        for k in range(i, len(group)):
            if group[k].end - group[i].end > window_s:
                break          # sorted by end: nothing later can be in this window
            if not used[k]:
                window.append(k)
        # Contiguous runs in duration order, not "everything within tolerance of
        # an anchor": one neighbouring outlier must not veto a valid subset that
        # sits right beside it.
        by_dur = sorted(window, key=lambda k: group[k].duration)
        best: list[int] = []
        for a, first in enumerate(by_dur):
            members: list[int] = []
            for k in by_dur[a:]:
                if group[k].duration - group[first].duration > STORM_DURATION_TOLERANCE_S:
                    break
                members.append(k)
            if len(members) < min_cohort or len(members) <= len(best):
                continue
            if max_concurrency([group[k] for k in members])[0] < STORM_MIN_OVERLAP:
                continue     # serial failures are a slow client, not a pack
            best = members
        if best:
            out.extend(_storm_row(outcome, [group[k] for k in best]))
            for k in best:
                used[k] = True
            # Re-scan from the same start: a large pack may have won the window
            # while a smaller one beginning at `i` is still unreported.
            continue
        i += 1
    return out


def _storm_row(outcome: str, cohort: list[Request]) -> list[dict]:
    starts = [r.start for r in cohort if r.start is not None]
    ends = [r.end for r in cohort if r.end is not None]
    durs = [r.duration for r in cohort if r.duration is not None]
    return [{
        "outcome": outcome,
        "duration_s": round(statistics.median(durs), 1) if durs else None,
        "members_id": _member_id(cohort),
        "cohort": len(cohort),
        "start": datetime.fromtimestamp(min(starts), timezone.utc).isoformat() if starts else None,
        "end": datetime.fromtimestamp(max(ends), timezone.utc).isoformat() if ends else None,
        "blocked_s": round(max(ends) - min(starts), 1) if starts and ends else None,
        "models": sorted({r.model for r in cohort if r.model}),
    }]


def peak_concurrency_during(reqs: list[Request]) -> dict[int, int]:
    """For each request, the most requests in flight at any moment of its window.

    This is the sample the saturation bands bucket on: a request's own latency
    is explained by the load it actually experienced, not by the window average.
    """
    events: list[tuple[float, int, int]] = []
    for i, r in enumerate(reqs):
        if r.start is None or r.end is None or r.end <= r.start:
            continue        # zero-length: adding and removing at one instant would
                            # leave it "active" for every later event (codex, iter 1)
        events.append((r.start, 1, i))
        events.append((r.end, -1, i))
    events.sort(key=lambda e: (e[0], e[1]))       # ends before starts at the same instant
    active: set[int] = set()
    peaks: dict[int, int] = {}
    for _, delta, i in events:
        if delta == 1:
            active.add(i)
        else:
            active.discard(i)
        c = len(active)
        for j in active:
            if c > peaks.get(j, 0):
                peaks[j] = c
    return peaks


BANDS = ((0, 2, "0-2"), (3, 5, "3-5"), (6, 7, "6-7"), (8, 9, "8-9"), (10, 10 ** 6, "10+"))


def _bands_for(sub: list[Request], peaks: dict[int, int], min_top_n: int,
               inflation: float) -> dict:
    """`peaks` is indexed by position in the *whole* request list, not in `sub`.

    Concurrency is a property of the account, not of a model: two models each
    at five in flight put ten concurrent requests on the gateway.
    """
    timed = [(peaks[i], r.duration) for i, r in sub
             if i in peaks and r.duration is not None and r.outcome == "completed"]
    bands: list[dict] = []
    for lo, hi, name in BANDS:
        sample = [d for c, d in timed if lo <= c <= hi]
        bands.append({"band": name, "n": len(sample), "p50": pct(sample, 50),
                      "p90": pct(sample, 90)})
    top_reqs = [r for i, r in sub
                if i in peaks and BANDS[-1][0] <= peaks[i] and r.duration is not None
                and r.outcome == "completed"]
    top_span = _span(top_reqs)
    mid = [b["p90"] for b in bands[:-1] if b["n"] >= 3 and b["p90"]]
    top = bands[-1]
    baseline = statistics.median(mid) if mid else None
    ratio = (top["p90"] / baseline) if (baseline and top["p90"]) else None
    flagged = bool(ratio and ratio >= inflation and top["n"] >= min_top_n)
    return {"bands": bands, "mid_p90_baseline_s": round(baseline, 1) if baseline else None,
            "top_inflation": round(ratio, 2) if ratio else None,
            "top_inflation_flagged": flagged,
            "top_first": top_span.get("first"), "top_last": top_span.get("last"),
            "note": None if top["n"] >= min_top_n else (
                f"top band has {top['n']} sample(s); {min_top_n} needed before claiming a gradient")}


def saturation_bands(reqs: list[Request], min_top_n: int = BAND_MIN_TOP_SAMPLES,
                     inflation: float = BAND_INFLATION) -> dict:
    """Latency against load, compared only among requests to the same model on
    the same tier.

    Pooling them lets a slow model look like a concurrency effect: ten
    background completions at ~600s beside three fast interactive ones produce a
    60x "gradient" with nothing under load. The claim this supports is narrow on
    purpose, and needs enough samples at the ceiling to mean anything.
    """
    peaks = peak_concurrency_during(reqs)     # over everything, before grouping
    groups: dict[tuple[str, str], list[tuple[int, Request]]] = {}
    for i, r in enumerate(reqs):
        groups.setdefault((r.model or "(unknown)", r.tier), []).append((i, r))
    out_groups: list[dict] = []
    for (model, tier), pairs in sorted(groups.items()):
        out_groups.append({"model": model, "tier": tier,
                           **_bands_for(pairs, peaks, min_top_n, inflation)})
    flagged = [g for g in out_groups if g["top_inflation_flagged"]]
    worst = max(flagged, key=lambda g: g["top_inflation"] or 0) if flagged else None
    biggest = max(out_groups, key=lambda g: sum(b["n"] for b in g["bands"])) if out_groups else None
    # The summary IS one group - the flagged one if there is one, else the
    # busiest. Mixing a busiest group's bands with a flagged group's ratio
    # produced a headline with no samples behind it.
    summary = worst or biggest or {"bands": [], "mid_p90_baseline_s": None,
                                   "top_inflation": None, "top_first": None,
                                   "top_last": None, "note": None}
    result: dict = {k: v for k, v in summary.items() if k != "groups"}
    result["groups"] = out_groups
    result["top_inflation_flagged"] = bool(flagged)
    if worst:
        result["flagged_model"], result["flagged_tier"] = worst["model"], worst["tier"]
    return result


def timed(reqs: list[Request]) -> list[Request]:
    """Requests whose interval can be trusted.

    A foreign archive may write timestamps in seconds, which turns a 300s stall
    into a decades-long interval. Every timing aggregate - minute bins, peaks,
    bands, storms, wall counts - is computed over this filtered list so a bad
    interval cannot allocate 28 million minute buckets or invent a deadline.
    Outcomes and counts still see every request.
    """
    return [r for r in reqs
            if r.start is not None and r.end is not None
            and 0 < (r.end - r.start) <= SANITY_MAX_DURATION_S]


def _by_run(reqs: list[Request]) -> list[dict]:
    """Per-run breakdown.

    A run is one experiment/project directory. The per-run 300s wall count is
    the measurement the rat-run reproduction is graded on, and it is invisible
    in machine-wide aggregates.
    """
    groups: dict[str, list[Request]] = {}
    for r in reqs:
        groups.setdefault(r.run or "(unattributed)", []).append(r)
    out: list[dict] = []
    for run, rs in groups.items():
        tr = timed(rs)          # timing fields only; counts still see everything
        outcomes: dict[str, int] = {}
        for r in rs:
            outcomes[r.outcome] = outcomes.get(r.outcome, 0) + 1
        walls: dict[str, int] = {}
        for r in tr:
            if r.outcome in ("client_idle_timeout", "request_timeout") and r.duration:
                walls[str(round(r.duration, 0))] = walls.get(str(round(r.duration, 0)), 0) + 1
        # A completion that ran past the wall is not a wall failure, but it is the
        # one request a looser count would fold in, so report it separately.
        slow_ok = sum(1 for r in tr
                      if r.outcome == "completed" and r.duration and r.duration >= 290)
        out.append({
            "run": run,
            "requests": len(rs),
            "peak_concurrency": max_concurrency(tr)[0],
            "outcomes": outcomes,
            "wall_counts_s": dict(sorted(walls.items(), key=lambda kv: -kv[1])[:4]),
            "slow_completions_over_290s": slow_ok,
            "storm_cohorts": len(timeout_storms(rs)),
        })
    out.sort(key=lambda x: -x["requests"])
    return out


def store_dir() -> str:
    """Where extractions live. Overridable so tests never touch $HOME."""
    return os.path.expanduser(os.environ.get("LUNA_DOCTOR_STORE")
                              or "~/.luna-doctor/store")


def _finding_signature(name: str, model: str | None = None) -> str:
    """Signature-dominant identity: windows are occurrences, never identity."""
    return f"{name}" + (f":{model}" if model else "")


def _span(rs: list[Request]) -> dict:
    stamps = [r.end if r.end is not None else r.start for r in rs]
    stamps = [t for t in stamps if t is not None]
    if not stamps:
        return {}
    return {"first": datetime.fromtimestamp(min(stamps), timezone.utc).isoformat(),
            "last": datetime.fromtimestamp(max(stamps), timezone.utc).isoformat()}


def run_findings(m: dict) -> list[dict]:
    """The findings of one run, as storable rows with stable identities.

    One row per signature per run: three 10-packs of the same class are one
    occurrence with a cohort count of three, not three rows that would inflate
    the repeat history the ledger exists to record.
    """
    out: list[dict] = []
    storms: dict[str, list[dict]] = {}
    for storm in m.get("timeout_storms") or []:
        storms.setdefault(_finding_signature("timeout_storm", storm["outcome"]), []).append(storm)
    for sig, ss in storms.items():
        firsts = [s["start"] for s in ss if s.get("start")]
        lasts = [s["end"] for s in ss if s.get("end")]
        out.append({"kind": "timeout_storm", "signature": sig,
                    "count": sum(s["cohort"] for s in ss),
                    "detail": {"cohorts": len(ss), "largest": max(s["cohort"] for s in ss),
                               "first": min(firsts) if firsts else None,
                               "last": max(lasts) if lasts else None}})
    for us in m.get("unknown_errors") or []:
        out.append({"kind": "unknown_error", "signature": us["signature"],
                    "count": us["count"], "detail": {"first": us["first"], "last": us["last"]}})
    bands = m.get("saturation_bands") or {}
    if bands.get("top_inflation_flagged"):
        out.append({"kind": "saturation_gradient",
                    "signature": _finding_signature("saturation_gradient", "10+"),
                    "count": bands["bands"][-1]["n"],
                    "detail": {"ratio": bands["top_inflation"],
                               "first": bands.get("top_first"), "last": bands.get("top_last")}})
    for model, d in (m.get("per_model_deadlines") or {}).items():
        if d.get("status") == "unknown":
            out.append({"kind": "unknown_deadline", "signature": _finding_signature("deadline", model),
                        "count": d.get("seen_n", d["n"]),
                        "detail": {"model": model, "usable_n": d["n"],
                                   "seen_n": d.get("seen_n", d["n"]),
                                   "first": d.get("first"), "last": d.get("last")}})
    spans = m.get("outcome_spans") or {}
    for outcome in ("admission_timeout", "client_idle_timeout", "org_concurrency_limit",
                    "model_not_found", "context_length_exceeded", "harness_deadline"):
        n = (m.get("by_outcome") or {}).get(outcome, 0)
        if n:
            out.append({"kind": "outcome", "signature": _finding_signature(outcome),
                        "count": n, "detail": dict(spans.get(outcome) or {})})
    return out


def _source_id(harnesses: list[str], pi_roots: list[str] | None,
               claude_roots: list[str] | None) -> str:
    """Identity of *what was read*, so a mirror run and a local run never collide."""
    parts: list[str] = []
    # Sorted and de-duplicated by canonical path, so argument order cannot mint a
    # second identity for the same data. Each harness records where *it* read
    # from, so a mirror-only run and a mirror-plus-local run are different.
    for h in sorted(harnesses):
        roots = pi_roots if h == "pi" else claude_roots if h == "claude-code" else None
        if roots is None:
            parts.append(f"{h}:default")
        elif roots:
            parts += [f"{h}:{r}" for r in sorted({os.path.realpath(os.path.expanduser(r))
                                                  for r in roots})]
        else:
            parts.append(f"{h}:none")     # explicit roots were given and rejected
    if not parts:
        parts.append("stores:default")
    return ", ".join(parts)


# The ledger is a list of discrete events. Today that is timeout cohorts only:
# one row per cohort, identified by its member set. Unclassified errors are a
# stream of sightings with no occurrence boundary that survives a rolling
# window, and window state (outcome counts, per-model "unknown" deadlines, a
# saturation gradient) moves whenever the window's edges move - both stay in the
# window's own report, which is the report of record for it.


def ledger_rows(m: dict, sources: str) -> list[dict]:
    """One row per event, for the repeat history."""
    rows: list[dict] = []
    for storm in m.get("timeout_storms") or []:
        # The signature embeds the cohort's member hash: identity is the set of
        # requests in the pack, which no window edge can move. Models stay in
        # detail for display.
        rows.append({
            "kind": "timeout_storm",
            "signature": _finding_signature(
                "timeout_storm", f"{storm['outcome']}:{storm.get('members_id') or ''}"),
            "event_start": storm["start"],
            "event_end": storm["end"],
            "sources": sources,
            "count": storm["cohort"],
            "detail": {"duration_s": storm["duration_s"], "models": storm["models"]},
        })
    # Unclassified errors are a stream of sightings, not discrete events: a
    # rolling window makes any "occurrence" boundary arbitrary, and every scheme
    # tried here either lost a real outbreak or minted a phantom one. They stay a
    # first-class finding in each window's report, with their first/last span and
    # burst structure (metrics.unknown_error_bursts), and out of the ledger.
    return rows


def persist_run(m: dict, window_hours: float, store: str | None = None,
                command: str = "analyze", since: float | None = None,
                until: float | None = None, sources: str = "stores:default") -> dict:
    """Write the run's extraction, and record any event not seen before.

    One JSON file per window (readable, diffable, overwritten when the same
    window and sources are re-analysed) plus an append-only JSONL ledger. The
    file is the report of record for the window; the ledger is the repeat
    history, keyed on the event itself so a rolling window neither repeats
    itself nor loses a genuinely new occurrence.
    """
    root = os.path.expanduser(store or store_dir())
    os.makedirs(root, exist_ok=True)
    # The window recorded is the window analysed, to the second. A *bucket* is
    # derived only for file identity, so re-running the same window overwrites
    # its report instead of filling the store with near-duplicates - while two
    # genuinely different analyses an hour apart stay distinct files.
    end = until if until is not None else time.time()
    start = since if since is not None else end - window_hours * 3600
    # Identity is the window itself, to the minute: two runs whose windows differ
    # (10:05 and 10:50 over 24h) are different reports and must not replace each
    # other, while re-running the same window inside a minute is idempotent.
    start_min = math.floor(start / 60.0) * 60.0
    end_min = math.floor(end / 60.0) * 60.0
    src = hashlib.sha1(sources.encode()).hexdigest()[:8]
    span_s = int(round(window_hours * 3600))     # 90.2m and 90.5m share two minute marks
    stamp = (datetime.fromtimestamp(start_min, timezone.utc).strftime("%Y%m%dT%H%MZ")
             + "_" + datetime.fromtimestamp(end_min, timezone.utc).strftime("%Y%m%dT%H%MZ")
             + f"_{span_s}s")
    path = os.path.join(root, f"{stamp}_{src}.json")
    findings = run_findings(m)
    doc = envelope(command, {
        "window": {"start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
                   "end": datetime.fromtimestamp(end, timezone.utc).isoformat(),
                   "hours": window_hours},
        "sources": sources,
        "metrics": m,
        "findings": findings,
    }, contract=CONTRACT)
    doc["generated_at"] = datetime.fromtimestamp(end, timezone.utc).isoformat()
    tmp = tempfile.mkstemp(prefix=".tmp-", dir=root)   # unique per writer
    try:
        with os.fdopen(tmp[0], "w") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
        os.replace(tmp[1], path)   # a reader never sees a half-written report
    except BaseException:
        try:
            os.unlink(tmp[1])
        except OSError:
            pass
        raise

    ledger = os.path.join(root, "findings.jsonl")
    events = ledger_rows(m, sources)
    lock_path = os.path.join(root, ".findings.lock")
    with open(lock_path, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)     # two analyses may share a store
        except OSError:
            pass
        # Identity is exact: `ledger_rows` embeds the cohort's member hash in the
        # signature, so two rows are the same event only when their member sets
        # are. A pack seen partially and then in full is therefore recorded twice
        # and never lost; the per-window report remains the authoritative account.
        seen: set[tuple] = set()
        if os.path.isfile(ledger):
            try:
                with open(ledger) as fh:
                    for line in fh:
                        try:
                            row = json.loads(line)
                        except Exception:
                            continue
                        seen.add((row.get("signature"), row.get("kind"), row.get("sources")))
            except OSError:
                pass

        written: set[tuple] = set()

        def already_seen(row: dict) -> bool:
            exact = (row["signature"], row["kind"], row["sources"],
                     row.get("event_start"), row.get("event_end"))
            if exact in written:
                return True      # this exact event is already recorded in this run
            return (row["signature"], row["kind"], row["sources"]) in seen

        with open(ledger, "a") as fh:
            for row in events:
                if already_seen(row):
                    continue      # this event is already in the history
                written.add((row["signature"], row["kind"], row["sources"],
                             row.get("event_start"), row.get("event_end")))
                fh.write(json.dumps({**row,
                                     "window_start": doc["window"]["start"],
                                     "window_end": doc["window"]["end"],
                                     "window_hours": window_hours,
                                     "version": VERSION}) + "\n")
    return {"path": path, "ledger": ledger, "findings": findings}


def analyse(reqs: list[Request], window_hours: float) -> dict:
    n = len(reqs)
    # What the vendor filter excluded travels on a Requests list from the
    # iterators; a plain list carries none and the block stays absent, which
    # is the truth about that list. Additive, so schema stays 4.
    excluded = getattr(reqs, "excluded", None)
    by_outcome: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    by_harness: dict[str, int] = {}
    for r in reqs:
        by_outcome[r.outcome] = by_outcome.get(r.outcome, 0) + 1
        by_tier[r.tier] = by_tier.get(r.tier, 0) + 1
        by_harness[r.harness] = by_harness.get(r.harness, 0) + 1

    ok = timed(reqs)
    done = [r for r in ok if r.outcome == "completed"]
    durs = [r.duration for r in done]
    lat = {}
    for tier in ("interactive", "background"):
        sub = [r.duration for r in done if r.tier == tier]
        lat[tier] = {
            "n": len(sub),
            "p50": pct(sub, 50),
            "p90": pct(sub, 90),
            "max": round(max(sub), 1) if sub else None,
        }

    sat = [r for r in ok if r.saturated]
    adm = [r for r in reqs if r.outcome == "admission_timeout"]
    peak, peak_at = max_concurrency(ok)

    # starved-peer test: a saturated request whose model completed elsewhere
    # inside its own window => the endpoint was fine, this request was not served.
    starved = 0
    for r in sat:
        if r.start is None or r.end is None:
            continue
        for o in done:
            if o is r or o.model != r.model:
                continue
            if o.start is None or o.end is None:
                continue
            if o.start >= r.start and o.end <= r.end:
                starved += 1
                break

    win = pressure_windows(ok, ADVISED_CONCURRENCY)
    longest = max(win, key=lambda w: w["seconds"]) if win else None
    by_harness_peak = {}
    for h in sorted(by_harness):
        sub = [r for r in ok if r.harness == h]
        by_harness_peak[h] = max_concurrency(sub)[0]

    conv = {}
    for tier in ("interactive", "background"):
        served = sum(1 for r in reqs if r.tier == tier and r.outcome == "completed")
        dead = sum(1 for r in reqs if r.tier == tier and r.outcome == "admission_timeout")
        tot = served + dead
        conv[tier] = {"completed": served, "admission_timeout": dead,
                      "admitted_pct": round(100.0 * served / tot, 1) if tot else None}

    metrics = {
        "window_hours": window_hours,
        "requests": n,
        "by_outcome": by_outcome,
        "by_tier": by_tier,
        "by_harness": by_harness,
        "latency_s": lat,
        "saturated": len(sat),
        "starved_peer_evidence": starved,
        "peak_concurrency": peak,
        "peak_at": datetime.fromtimestamp(peak_at, timezone.utc).isoformat() if peak_at else None,
        "peak_by_harness": by_harness_peak,
        "admission_conversion": conv,
        "pressure_windows": len(win),
        "longest_pressure_window": longest,
        "by_minute": concurrency_by_minute(ok),
        "requests_by_model": [
            {"model": model, "n": n, "tier": tier_of(model)[0]}
            for model, n in sorted(
                ((mdl, sum(1 for r in reqs if r.model == mdl))
                 for mdl in {r.model for r in reqs}),
                key=lambda kv: -kv[1])
        ],
        "by_run": _by_run(reqs),
        "outcome_spans": {oc: _span([r for r in reqs if r.outcome == oc])
                          for oc in set(r.outcome for r in reqs)},
        "per_model_deadlines": per_model_deadlines(reqs),
        "timeout_storms": timeout_storms(reqs),
        "saturation_bands": saturation_bands(ok),
        "unknown_errors": unknown_errors(reqs),
        "unknown_error_bursts": unknown_error_bursts(reqs),
        # Counted over every request, like by_outcome: `done` is the latency
        # sample and excludes requests whose interval cannot be trusted.
        "totals": {"completed": by_outcome.get("completed", 0),
                   "admission_deaths": len(adm)},
    }
    if excluded is not None:
        metrics["excluded"] = excluded
    return metrics


# ---- verdict ----------------------------------------------------------------
def _priority_for_advice(m: dict) -> tuple:
    """(value, source) for the account's interactive width, or (None, 'unknown').

    The account's own number wins; a `--plan` declaration is the fallback for
    when the capacity payload is unreachable; anything else is unknown. No
    constant stands in for a missing number: the advice that depends on it
    changes shape instead of quoting a figure nobody measured.
    """
    capm = m.get("capacity") or {}
    served = (capm if capm.get("provenance") == sources.PROV_ACCOUNT else None)
    try:
        return attribution.resolve_priority_concurrency(served, m.get("declared_plan"))
    except Exception:
        return (None, "unknown")


def _priority_phrase(value, source: str) -> str:
    """How the width is named in copy: whose number it is, never a gateway fact."""
    if not isinstance(value, int) or isinstance(value, bool):
        return "your priority concurrency allowance"
    if source == "account":
        return f"the account's priority concurrency ({value}, account)"
    name = source[len("plan "):] if source.startswith("plan ") else source
    return f"your declared plan's priority concurrency ({value}, declared plan {name})"


def _advice_line(m: dict) -> str:
    """The one sentence every pressured report ends with.

    It used to end with a hardcoded width, which is one plan's default wearing
    a ruler: the account's own number leads when it is known, and when it is
    not the sentence names the command that fetches it rather than inventing a
    figure.
    """
    value, source = _priority_for_advice(m)
    if isinstance(value, int) and not isinstance(value, bool):
        return (f"Adjust: keep concurrent requests within {_priority_phrase(value, source)}, "
                f"and move anything wider to the -background variant of the same model.")
    return ("Adjust: keep concurrent requests within your priority concurrency allowance - these "
            "logs cannot tell you what it is. Read it from the account with "
            "`luna-doctor capacity --online`, or declare it with `--plan <name or number>` - and "
            "move anything wider to the -background variant of the same model.")


def verdict(m: dict) -> tuple[str, list[str], int]:
    """Return (headline, adjustments, exit_code)."""
    adj: list[str] = []
    n = m["requests"]
    if n == 0:
        return "No requests found in the window.", ["Widen the window with --since, or run `luna-doctor discover`."], 2

    dead = m["by_outcome"].get("admission_timeout", 0)
    idle = m["by_outcome"].get("client_idle_timeout", 0)
    bg = m["by_tier"].get("background", 0)
    pressure = False

    if dead:
        pressure = True
        conv = m["admission_conversion"]["interactive"]["admitted_pct"]
        per_model = m.get("per_model_deadlines") or {}
        observed = ", ".join(
            f"{model} {d['deadline_s']:.1f}s (n={d['n']})"
            for model, d in sorted(per_model.items()) if d.get("deadline_s"))
        line = (f"{dead} request(s) were blocked at admission (`scheduler admission timeout`). "
                f"Of the interactive requests that finished or were blocked at admission, "
                f"{conv}% were admitted.")
        if observed:
            line += f" Observed deadlines: {observed}."
        unknown = [(mo, d) for mo, d in per_model.items() if d.get("status") == "unknown"]
        if unknown:
            detail = ", ".join(
                f"{mo} ({d.get('seen_n', d['n'])} blocked at admission, {d['n']} usable duration(s))"
                for mo, d in sorted(unknown))
            line += (f" No deadline is established for {detail} - fewer than "
                     f"{DEADLINE_MIN_SAMPLES} usable observations, so do not assume another "
                     f"model's number.")
        adj.append(line)
    if idle:
        pressure = True
        adj.append(
            f"{idle} request(s) stalled until the harness's own ~{CLIENT_IDLE_TIMEOUT_S:.0f}s idle "
            f"budget killed them. That is the silent-overflow signature: the request was likely "
            f"demoted to the background tier, which streams only SSE keep-alives for minutes."
        )
    if m["pressure_windows"]:
        pressure = True
        w = m["longest_pressure_window"] or {}
        pv, _psrc = _priority_for_advice(m)
        width = (f"more than {pv} concurrent requests" if isinstance(pv, int) and not isinstance(pv, bool)
                 else "more concurrent requests than the account's priority concurrency admits")
        adj.append(
            f"{m['pressure_windows']} window(s) held {width} at once; the longest lasted "
            f"{fmt(w.get('seconds'), 's')} and peaked at "
            f"{fmt(w.get('max_concurrent'))} concurrent. {CAP_COPY[0].upper()}{CAP_COPY[1:]}; "
            f"past that width, requests park or overflow into background scheduling "
            f"rather than fail."
        )
    if bg:
        adj.append(
            f"{bg} request(s) already ran on the -background tier. That is the right home for "
            f"concurrent requests beyond your priority concurrency, but its SLA is minutes, not "
            f"seconds: budget for it and do not put a short client timeout in front of it."
        )
    storms = m.get("timeout_storms") or []
    if storms:
        pressure = True
        worst = max(storms, key=lambda x: x["cohort"])
        adj.append(
            f"{len(storms)} timeout cohort(s): {worst['cohort']} {worst['outcome']} requests of "
            f"~{worst['duration_s']}s ending together, blocking ~{fmt(worst['blocked_s'], 's')} of "
            f"wall clock. That is a synchronous retry: the client launched a pack and waited on "
            f"all of it."
        )
    bands = m.get("saturation_bands") or {}
    if bands.get("top_inflation_flagged"):
        top = bands["bands"][-1]
        adj.append(
            f"Latency inflates at the ceiling: p90 {fmt(top['p90'], 's')} at concurrency "
            f"{top['band']} (n={top['n']}) against a mid-band p90 of "
            f"{fmt(bands.get('mid_p90_baseline_s'), 's')} - {bands['top_inflation']}x. "
            f"Holding more concurrent requests than this band buys latency, not throughput."
        )
    unknown = m.get("unknown_errors") or []
    if unknown:
        # Not pressure: an unclassified string says nothing about concurrency,
        # and flipping the exit code on it would cry queue on a rename.
        u = unknown[0]
        adj.append(
            f"{len(unknown)} error string(s) matched no known class; the most common is "
            f"`{u['signature'][:80]}` ({u['count']}x, {u['first']} .. {u['last']}). "
            f"A new gateway behaviour looks like this before anyone names it."
        )
    ocl = m["by_outcome"].get("org_concurrency_limit", 0)
    if ocl:
        pressure = True
        adj.append(
            f"{ocl} request(s) were rejected (`CONCURRENT_REQUEST_LIMIT_EXCEEDED`) - the "
            f"account's whole priority concurrency was in use, not one model's share. Hold "
            f"fewer concurrent requests at once, or move work to the -background tier."
        )
    mnf = m["by_outcome"].get("model_not_found", 0)
    if mnf:
        adj.append(
            f"{mnf} request(s) failed with a model that does not exist. That is a stale model id "
            f"in a config file, not load - refresh the model list before chasing anything else."
        )
    cle = m["by_outcome"].get("context_length_exceeded", 0)
    if cle:
        adj.append(
            f"{cle} request(s) exceeded the context window. Check maxTokens against contextWindow "
            f"in the harness model config: input and output share the window."
        )
    ptl = m["by_outcome"].get("payload_too_large", 0)
    if ptl:
        adj.append(
            f"{ptl} request(s) were rejected as too large (413). Images at full resolution in the "
            f"conversation are the usual cause, not prompt text."
        )
    ml = m["by_outcome"].get("message_limit", 0)
    if ml:
        adj.append(
            f"{ml} request(s) hit a message-count limit (`Message limit reached`, a 400). That is "
            f"a ceiling on how many messages this account may send, not concurrency pressure - "
            f"holding fewer concurrent requests will not fix it."
        )
    hd = m["by_outcome"].get("harness_deadline", 0)
    if hd:
        adj.append(
            f"{hd} request(s) ended on the caller's own deadline (`request deadline exceeded` / "
            f"`provider unhealthy`). The harness gave up, not the gateway: check its request "
            f"timeout before blaming load."
        )
    rl = m["by_outcome"].get("rate_limited", 0)
    if rl:
        pressure = True
        adj.append(
            f"{rl} request(s) were rejected (429 `rate_limit_error`). That error is the "
            f"account's concurrent-request capacity being exceeded, nothing else: keep concurrent "
            f"requests within your priority concurrency allowance, or move the work to the "
            f"-background tier."
        )

    lat = m["latency_s"]["interactive"]
    if lat["p50"] is not None and lat["p50"] > 60:
        adj.append(
            f"Even admitted interactive requests have a p50 of {lat['p50']}s end to end; a fixed "
            f"~{PRE_HEADER_WAIT_S:.0f}s of that is the gateway sending headers. Treat a minute of "
            f"wait as the floor, not as a fault."
        )
    if m["starved_peer_evidence"]:
        adj.append(
            f"{m['starved_peer_evidence']} stalled request(s) had another request to the same model "
            f"complete inside their own window, so the model was healthy - the request was not "
            f"served, not slow."
        )

    # ---- swamp concurrency is configuration, never measured traffic ----
    # The peak concurrency above is an observation; swamp's configured
    # concurrency is a setting. They are reported as separate labelled facts
    # and never combined: swamp's configured figure enters no arithmetic, no
    # sum and no stated limit here, and never reaches the headline. What the
    # two together might mean is a risk this report does not compute, not a
    # number this report computes.
    sw = m.get("swamp")
    configured = None
    split = ""
    if sw and sw.get("present"):
        try:
            knobs = sw.get("knobs") or {}
            extract = int((knobs.get("extractConcurrency") or {}).get("value", SWAMP_EXTRACT_CONCURRENCY_DEFAULT))
            llm = int((knobs.get("llmConcurrency") or {}).get("value", SWAMP_LLM_CONCURRENCY_DEFAULT))
            configured = int(sw.get("configuredConcurrency") or extract + llm)
            split = f" (extract {extract} + verify {llm})"
        except Exception:
            configured = None
    if configured is None:
        # _swamp_configured_concurrency carries the setting when the full
        # in-use scan is not attached to these metrics; pop it so --json
        # never sees the scratch key.
        configured = m.pop("_swamp_configured_concurrency", None)
    else:
        m.pop("_swamp_configured_concurrency", None)
    if configured:
        peak = m.get("peak_concurrency") or 0
        adj.append(
            f"swamp is configured for {configured} concurrent requests per behavior"
            f"{split} - a setting, not measured traffic. The measured peak concurrency this "
            f"window is {peak}, a separate observation reported above. Swamp's configured "
            f"concurrency on top of interactive bursts is an unmeasured risk, not a computed "
            f"total; the width this account actually admits cannot be read from these logs."
        )

    # ---- the account's capacity: corroboration, never the headline ----
    # The server's numbers sit beside the local ones as a cross-check, and
    # which one leads "are you over capacity" is deliberately not decided
    # here: the headline stays the local story, the server story is
    # corroboration, and the two are never merged. Everything renders only
    # when a capacity payload was served; without one the block is unknown
    # and says nothing.
    capm = m.get("capacity") or {}
    if capm.get("provenance") == sources.PROV_ACCOUNT:
        lanes = capm.get("priority_lanes")
        if isinstance(lanes, int) and not isinstance(lanes, bool):
            peak = m.get("peak_concurrency") or 0
            plan_name = capm.get("plan")
            adj.append(
                f"local peak {peak} concurrent request(s) (a lower bound - other machines "
                f"are invisible to these logs) \u00b7 account limit {lanes} priority "
                f"concurrent request(s) (plan {plan_name or 'not reported'}, fetched from "
                f"the account's capacity and served from its disk cache)"
            )
        days = capm.get("window_days") or []
        if days:
            daw_days = []
            daw_total = 0
            for d in days:
                v = d.get("denied_after_wait")
                if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                    daw_days.append(str(d.get("day")))
                    daw_total += v
            if daw_days:
                # per-day counts live in metrics.capacity.window_days; the
                # line carries the total and names the days
                adj.append(
                    f"the account's admission log confirms {daw_total} request(s) were "
                    f"parked and then refused after waiting (denied_after_wait) on "
                    f"{', '.join(daw_days)} UTC - the server-side corroboration of the "
                    f"park-deadline rejection class these logs classify from error "
                    f"strings. Cross-check, not a merge: local and server counts come "
                    f"from different vantage points and may differ."
                )
            # the exclusions are stated wherever server counts sit beside
            # local ones; local counts see BYOK and embedding traffic the
            # server never counts, so a difference is expected, not an error
            adj.append(CAPACITY_COUNTS_EXCLUDE)

    if not pressure and not adj:
        return f"No concurrency pressure across {n} requests.", ["Nothing to adjust."], 0

    # The headline is built from the classes that actually set the pressure
    # flag. It used to name only blocked-at-admission and silent stalls, so a
    # window whose whole story was 429s read "0 request(s) were blocked at
    # admission, 0 silent stall(s)" while exiting 1 - the one line a reader
    # trusts most contradicted the verdict.
    parts = []
    if dead:
        parts.append(f"{dead} request(s) were blocked at admission")
    if idle:
        parts.append(f"{idle} silent stall(s)")
    if ocl:
        parts.append(f"{ocl} request(s) rejected at the account's concurrent-request limit")
    if rl:
        parts.append(f"{rl} request(s) rejected with 429")
    storms_all = m.get("timeout_storms") or []
    if storms_all and not parts:
        parts.append(f"{len(storms_all)} synchronous retry cohort(s)")
    if m["pressure_windows"] and not parts:
        parts.append(f"{m['pressure_windows']} window(s) wider than the account's priority concurrency")
    if pressure:
        if not parts:
            parts.append("pressure in the window")
        head = (f"Concurrency pressure across {n} requests: " + ", ".join(parts)
                + f", peak concurrency {m['peak_concurrency']}.")
    else:
        # Nothing set the pressure flag: never open with the words "concurrency
        # pressure" on a window that has none.
        head = f"{n} requests, none blocked at admission, peak concurrency {m['peak_concurrency']}."
    adj.append(_advice_line(m))
    return head, adj, (1 if pressure else 0)


def _excluded_rows(m: dict) -> list:
    """The vendor-filter row: what was dropped from the window, and why.

    The iterators already excluded these requests; the report says so beside
    the request count it explains, because dropping traffic without saying
    so would understate what the machine actually ran.
    """
    e = m.get("excluded") or {}
    lines: list[str] = []
    n_pi = e.get("pi_non_lunaroute") or 0
    if n_pi:
        provs = ", ".join(f"{k} {v}" for k, v in
                          sorted((e.get("pi_by_provider") or {}).items(),
                                 key=lambda kv: (-kv[1], kv[0])))
        lines.append(f"{n_pi} pi request(s) on other providers ({provs}) excluded - "
                     f"they never touched the gateway.")
    n_direct = e.get("claude_non_lunaroute") or 0
    if n_direct:
        models = ", ".join(sorted(e.get("claude_by_model") or {}))
        lines.append(f"{n_direct} claude-code request(s) went to Anthropic direct "
                     f"({models}) - excluded, they never touched the gateway.")
    n_unc = e.get("claude_unclassified") or 0
    if n_unc:
        models = ", ".join(sorted(e.get("claude_unclassified_by_model") or {}))
        lines.append(f"{n_unc} claude-code request(s) on models with unknown routing "
                     f"({models}) - excluded as unclassified, never guessed in.")
    if not lines:
        return []
    return [report.Row("excluded", "\n".join(lines))]


def build_report(m: dict, head: str, adj: list[str], att=None) -> report.Report:
    """Turn analyse() metrics into the Report every renderer prints.

    `att` is an attribution.Attribution (optional): when present it becomes the
    first section, because the question it answers - was the wall this machine,
    the account, or the gateway - comes before any evidence for it. Its rows
    keep their own labels, and its actions join the adjustments, so a plain or
    pretty renderer needs no change to carry them.

    The rows keep the analysis order - traffic first, then the admission and
    saturation evidence, then the caveats - so the plain renderer says what
    the old inline printer said, from data instead of prints. Blocked
    admissions and saturation inflation carry "bad" emphasis, unclassified
    errors "warn", and a clean admission wall "good".
    """
    traffic: list[report.Row] = [
        report.Row("window", f"last {human_window(m['window_hours'])}"),
        report.Row("requests",
                   f"{m['requests']}  ({', '.join(f'{k} {v}' for k, v in sorted(m['by_harness'].items()))})"),
        report.Row("tiers", ', '.join(f"{k} {v}" for k, v in sorted(m["by_tier"].items()))),
        report.Row("outcomes", ', '.join(f"{k} {v}" for k, v in sorted(m["by_outcome"].items()))),
    ]
    traffic.extend(_excluded_rows(m))
    peaks = ', '.join(f"{k} {v}" for k, v in sorted(m["peak_by_harness"].items()))
    traffic.append(report.Row(
        "peak concurrency",
        f"{m['peak_concurrency']} ({peaks})" + (f" at {m['peak_at']}" if m["peak_at"] else "")))
    bm = m.get("by_minute") or []
    dead_minutes = [x for x in bm if x["admission_deaths"]]
    if dead_minutes:
        traffic.append(report.Row("minute-level", f"{len(bm)} active minute(s); blocked admissions in:"))
        for x in dead_minutes[:6]:
            traffic.append(report.Row(
                x["minute"],
                f"max {x['max_concurrent']} in flight, {x['started']} started, "
                f"{x['admission_deaths']} blocked at admission", "bad"))
    elif bm:
        busy = max(bm, key=lambda x: (x["max_concurrent"], x["started"]))
        traffic.append(report.Row(
            "minute-level",
            f"{len(bm)} active minute(s); busiest {busy['minute']} "
            f"(max {busy['max_concurrent']} in flight, {busy['started']} started)"))
    for tier in ("interactive", "background"):
        l = m["latency_s"][tier]
        if l["n"]:
            traffic.append(report.Row(
                f"latency {tier}",
                f"n={l['n']} p50={fmt(l['p50'], 's')} p90={fmt(l['p90'], 's')} "
                f"max={fmt(l['max'], 's')}"))

    pressure: list[report.Row] = []
    pm = m.get("per_model_deadlines") or {}
    if pm:
        bits = []
        for model, d in sorted(pm.items()):
            bits.append(f"{model} {d['deadline_s']}s (n={d['n']})"
                        if d.get("deadline_s")
                        else f"{model} unknown ({d.get('seen_n', d['n'])} blocked at admission, "
                             f"{d['n']} usable)")
        pressure.append(report.Row("admission wall", ', '.join(bits)))
    storms = m.get("timeout_storms") or []
    if storms:
        w = max(storms, key=lambda x: x["cohort"])
        pressure.append(report.Row(
            "timeout cohorts",
            f"{len(storms)}; largest {w['cohort']}x {w['outcome']} "
            f"at {w['duration_s']}s (blocked {fmt(w['blocked_s'], 's')})"))
    bands = m.get("saturation_bands") or {}
    if bands.get("bands"):
        shown = ", ".join(f"{b['band']}:{b['n']}" + (f"/p90 {fmt(b['p90'], 's')}" if b["p90"] else "")
                          for b in bands["bands"] if b["n"])
        emphasis = "normal"
        if bands.get("top_inflation_flagged"):
            shown += f"\ntop band p90 is {bands['top_inflation']}x the mid bands"
            emphasis = "bad"
        elif bands.get("note"):
            shown += "\n" + bands["note"]
        pressure.append(report.Row("saturation", shown, emphasis))
    for u in (m.get("unknown_errors") or [])[:3]:
        pressure.append(report.Row(
            "unclassified",
            f"{u['count']}x `{u['signature'][:70]}` ({u['first']} .. {u['last']})", "warn"))
    c = m["admission_conversion"]["interactive"]
    if c["completed"] or c["admission_timeout"]:
        pressure.append(report.Row(
            "admission",
            f"{c['admitted_pct']}% of interactive served ({c['completed']} served, "
            f"{c['admission_timeout']} blocked at admission)",
            "bad" if c["admission_timeout"] else "good"))

    notes: list[report.Row] = []
    if "claude-code" in m["by_harness"]:
        notes.append(report.Row(
            "note", "claude-code span starts are inferred from record gaps; treat its\n"
                    "concurrency as a lower bound."))
    notes.append(report.Row(
        "note", "tiers here are inferred from model names; the served tier is not in these logs; "
                "only a load test can see it."))
    sw = m.get("swamp")
    if sw and sw.get("present"):
        # Only an in-use swamp reaches here (installed is not in use), and its
        # configured concurrency is labelled as a setting so it can never be
        # read as measured traffic next to the peak concurrency row above.
        bh = (sw.get("behaviorHistory") or {}).get("total") or 0
        wr = (sw.get("workflowRuns") or {}).get("total") or 0
        names = list(sw.get("behaviors") or [])
        who = ""
        if names:
            shown = ", ".join(names[:4])
            if len(names) > 4:
                shown += f" and {len(names) - 4} more"
            who = f" ({shown})"
        knobs = sw.get("knobs") or {}
        try:
            ex = int((knobs.get("extractConcurrency") or {}).get("value", SWAMP_EXTRACT_CONCURRENCY_DEFAULT))
            ll = int((knobs.get("llmConcurrency") or {}).get("value", SWAMP_LLM_CONCURRENCY_DEFAULT))
            src = (knobs.get("extractConcurrency") or {}).get("source") or "default"
            setting = (f"; configured concurrency {ex + ll} per behavior (extract {ex} + verify {ll}, "
                       f"from {src}) - a setting, not measured traffic")
        except Exception:
            setting = ""
        notes.append(report.Row(
            "swamp", f"in use this window: {bh} behavior record(s){who}, {wr} workflow run(s)"
                     + setting))

    sections = [report.Section(title, rows)
                for title, rows in (("traffic", traffic), ("pressure", pressure),
                                    ("notes", notes)) if rows]
    if att is not None:
        rows = [report.Row("the wall you hit", _VERDICT_WORDS.get(att.verdict, att.verdict), "bad"
                           if att.verdict in ("you", "your-org") else "normal"),
                report.Row("why", att.headline)]
        rows.extend(report.Row(label, value) for label, value in att.rows)
        sections.insert(0, report.Section("attribution", rows))
        adj = list(adj) + list(att.actions)
    return report.Report(headline=head, sections=sections, adjustments=list(adj))


def write_out(text: str) -> None:
    """Write `text` to stdout without ever dying on the stream's codec.

    A `LANG=C` terminal cannot encode `·` or an em dash; the report still
    prints, with each unencodable character replaced, instead of crashing
    part way through. The moon is handled earlier - `style_for` degrades it
    before rendering - so this backstop is for the characters that ride
    inside row values.
    """
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.write(text.encode(enc, "replace").decode(enc, "replace"))


# ---- clipboard ---------------------------------------------------------------
# The one deliberate exception to luna-doctor's "no subprocess" rule. That
# rule was about rasterising images (an expensive, failure-prone pipeline);
# the clipboard is a small OS affordance, reached only when --copy asks for
# it and never on the normal output path.

CLIPBOARD_TOOLS = (
    # Detection order: the first name that exists on PATH is used. One
    # candidate per window system, with the arguments each needs to take
    # text on stdin for the system clipboard (never the mouse selection).
    ("pbcopy", ()),                         # macOS
    ("wl-copy", ()),                        # Wayland
    ("xclip", ("-selection", "clipboard")),  # X11
    ("xsel", ("--clipboard", "--input")),     # X11
)
CLIPBOARD_TIMEOUT_S = 5.0


def clipboard_copy(text: str) -> str | None:
    """Put `text` on the system clipboard; return the tool used, or None.

    A hung clipboard daemon must not hang the report, so every run is bounded
    by CLIPBOARD_TIMEOUT_S. A candidate that exists but refuses the text
    yields to the next one; None means nothing took it, and the caller keeps
    the printed answer, so a failed copy is never a lost answer.
    """
    for name, extra in CLIPBOARD_TOOLS:
        path = shutil.which(name)
        if not path:
            continue
        try:
            proc = subprocess.run([path, *extra], input=text, text=True,
                                  capture_output=True, timeout=CLIPBOARD_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return name
    return None


def _copy_or_say(text: str) -> None:
    """The --copy report line, on stderr; never an error, never an exit code."""
    tool = clipboard_copy(text)
    if tool is None:
        print("no clipboard tool found (pbcopy, wl-copy, xclip, xsel); printed instead",
              file=sys.stderr)
    else:
        print(f"copied {len(text.encode('utf-8'))} bytes to the clipboard via {tool}",
              file=sys.stderr)


def print_report(rep: report.Report, args) -> None:
    """Render `rep` to stdout the way this run's `--style` asked for.

    `--json` never reaches here - it prints its own envelope, and no Style
    is ever constructed on that path. `plain` renders undecorated, and
    `pretty` always takes the pretty layout - with the gate closed the same
    layout prints bare - and `auto` picks pretty only when the gate opened
    for this stream.
    """
    style = getattr(args, "_style", None)
    requested = getattr(args, "style", None)
    if style is not None and (requested == "pretty" or style.color or style.emoji):
        write_out(render_pretty(rep, style))
        return
    write_out(report.render_plain(rep))



def render(m: dict, head: str, adj: list[str], as_json: bool,
           stored: dict | None = None, args=None, att=None) -> None:
    if as_json:
        # A zero-request window returns from verdict() before the swamp
        # scratch key is consumed, and the envelope must never ship it:
        # nothing prefixed with "_" is contract data.
        m.pop("_swamp_configured_concurrency", None)
        doc = envelope("analyze", {
            "headline": head,
            "adjustments": adj,
            "metrics": m,
        }, contract=CONTRACT)
        if att is not None:
            # Additive: the verdict, its sentence and its labelled rows. The
            # envelope's schema stays 4 - nothing was renamed or removed.
            doc["attribution"] = {"verdict": att.verdict, "headline": att.headline,
                                  "rows": [{"label": k, "value": v} for k, v in att.rows],
                                  "actions": list(att.actions)}
        if stored:
            doc["store"] = {"path": stored["path"], "ledger": stored["ledger"],
                            "findings": len(stored["findings"])}
        print(json.dumps(doc, indent=2))
        return
    print_report(build_report(m, head, adj, att=att), args)


# ---- live probe -------------------------------------------------------------
def routing_key() -> str:
    k = os.environ.get("LUNAROUTE_KEY")
    if k:
        return k.strip()
    with open(os.path.expanduser(LR_CONFIG)) as fh:
        return json.load(fh)["profiles"]["default"]["routing_key"]


PROBE_PROMPT = (
    "Write a thorough 2500-word technical essay about distributed systems backpressure. "
    "Cover bounded queues, admission control, credit-based flow control, load shedding, and "
    "tail latency amplification. Do not stop early."
)


def probe_one(idx: int, key: str, model: str, max_tokens: int, timeout: float,
              rec: dict, socks: list, lock: threading.Lock) -> None:
    body = json.dumps({
        "model": model, "stream": True, "max_tokens": max_tokens, "temperature": 0.7,
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
    })
    conn = None
    t0 = time.monotonic()
    try:
        conn = http.client.HTTPSConnection(GATEWAY_HOST, 443, timeout=timeout,
                                           context=ssl.create_default_context())
        conn.request("POST", "/v1/chat/completions", body=body, headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": f"luna-doctor/{VERSION}",
        })
        with lock:
            socks.append(conn.sock)
        resp = conn.getresponse()
        t_hdr = time.monotonic()
        rec["status"] = resp.status
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        rec["headers"] = hdrs
        rec["wait_headers"] = round(t_hdr - t0, 2)
        rec["class"] = hdrs.get("x-lunaroute-service-class")
        rec["warning"] = hdrs.get("x-lunaroute-warning")
        rec["tier"], rec["demoted"], rec["reason"] = tier_of(model, rec["warning"])
        if resp.status != 200:
            rec["error"] = f"http {resp.status}: " + resp.read().decode("utf-8", "replace")[:300]
            rec["duration"] = round(time.monotonic() - t0, 2)
            return
        while True:
            line = resp.readline()
            if not line:
                break
            txt = line.decode("utf-8", "replace").rstrip("\r\n")
            if not txt:
                continue
            if txt.startswith(":"):
                rec["keepalives"] += 1
                continue
            if not txt.startswith("data:"):
                continue
            payload = txt[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if obj.get("error"):
                rec["error"] = str(obj["error"])[:300]
                continue
            for ch in obj.get("choices") or []:
                delta = ch.get("delta") or {}
                if delta.get("content") or delta.get("reasoning_content"):
                    rec["chunks"] += 1
                    if rec["ttfb"] is None:
                        rec["ttfb"] = round(time.monotonic() - t0, 2)
                if ch.get("finish_reason"):
                    rec["finish"] = ch["finish_reason"]
        rec["duration"] = round(time.monotonic() - t0, 2)
        rec["outcome"] = "streamed" if rec["chunks"] else "silent"
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["duration"] = round(time.monotonic() - t0, 2)
        low = str(e).lower()
        if "scheduler admission timeout" in low:
            rec["outcome"] = "admission_timeout"
        elif rec["chunks"]:
            rec["outcome"] = "streamed"
        else:
            rec["outcome"] = "error"
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


def run_probe(n: int, model: str, max_tokens: int, timeout: float,
              hard_limit: float) -> dict:
    """Fire n concurrent streams. `hard_limit` is a true wall clock: when it is
    reached the sockets are shut down and whatever each request had reached is
    reported. A request that never terminated is counted as `cut`, never as a
    success."""
    if n < 1:
        raise SystemExit("luna-doctor: --n must be at least 1")
    if hard_limit <= 0:
        raise SystemExit("luna-doctor: --max-seconds must be positive")
    key = routing_key()
    # A socket timeout longer than the wall clock is a lie: derive it.
    timeout = hard_limit if timeout <= 0 else min(timeout, max(5.0, hard_limit))
    results = [
        {"idx": i, "model": model, "status": None, "tier": None, "warning": None,
         "class": None, "wait_headers": None, "ttfb": None, "chunks": 0,
         "keepalives": 0, "duration": None, "finish": None, "error": None,
         "headers": {}, "outcome": "budget_exceeded", "cut": False}
        for i in range(n)
    ]
    socks: list = []
    lock = threading.Lock()
    threads = [
        threading.Thread(target=probe_one, daemon=True,
                         args=(i, key, model, max_tokens, timeout, results[i], socks, lock))
        for i in range(n)
    ]
    t0 = time.monotonic()
    deadline = t0 + hard_limit
    for t in threads:
        t.start()
    for t in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        t.join(remaining)
    for s in list(socks):
        try:
            s.shutdown(2)
        except Exception:
            pass
    for r in results:
        if r["outcome"] == "budget_exceeded":
            r["cut"] = True
            r["outcome"] = "streamed" if r["chunks"] else "silent"
            r["error"] = r["error"] or f"cut at the {hard_limit:.0f}s budget"
    live = results
    wait = [r["wait_headers"] for r in live if r["wait_headers"] is not None]
    ttfb = [r["ttfb"] for r in live if r["ttfb"] is not None]
    return {
        "n": n,
        "model": model,
        "wall_s": round(time.monotonic() - t0, 1),
        "budget_s": hard_limit,
        "socket_timeout_s": timeout,
        "tiers": {t: sum(1 for r in live if r["class"] == t) for t in ("interactive", "background")},
        "demoted": sum(1 for r in live if r["warning"]),
        "streamed": sum(1 for r in live if r["outcome"] == "streamed"),
        "silent": sum(1 for r in live if r["outcome"] == "silent"),
        "admission_deaths": sum(1 for r in live if r["outcome"] == "admission_timeout"),
        "errors": sum(1 for r in live if r["outcome"] == "error"),
        "cut": sum(1 for r in live if r["cut"]),
        "wait_headers": pct(wait, 50),
        "ttfb": pct(ttfb, 50),
        "keepalives": sum(r["keepalives"] for r in live),
        "direct": live,
    }


def _probe_swamp_headroom(probe_n: int) -> str | None:
    """One-sentence headroom note for a load test, or None when swamp is
    not in use.

    The load test measured probe_n; swamp's extract+llmConcurrency via
    sources.swamp_knobs() is a setting. They are reported as separate facts,
    and the only sum sits inside an explicitly assumed "if ... would" clause.
    No network, no second load test.
    """
    try:
        state = swamp_state(since=time.time() - 24 * 3600)
        if not state["in_use"]:
            return None
        knobs = sources.swamp_knobs(state["repo"])
        ex = int(knobs.get("extractConcurrency", {}).get("value", SWAMP_EXTRACT_CONCURRENCY_DEFAULT))
        ll = int(knobs.get("llmConcurrency", {}).get("value", SWAMP_LLM_CONCURRENCY_DEFAULT))
    except Exception:
        return None
    configured = ex + ll
    assumed = probe_n + configured
    verdict_word = "within" if assumed <= INTERACTIVE_BURST_CAP else "past"
    return (
        f"swamp is configured for {configured} concurrent requests per behavior (extract {ex} + verify {ll}) "
        f"- a setting, not measured traffic; this load test measured {probe_n} concurrent sessions. "
        f"If swamp ran at its configured width during such a load test, the account would see "
        f"{assumed} concurrent requests, {verdict_word} the conservative combined budget - an "
        f"assumption, not a measurement. {CAP_COPY[0].upper()}{CAP_COPY[1:]}."
    )


def build_probe_report(r: dict, n: int, model: str) -> report.Report:
    """One load-test run as rows, with the run's own advice as adjustments.

    The rows keep the old inline printer's order and labels; the bullets
    that printer printed conditionally become `adjustments` so every
    renderer can carry them.
    """
    # Demotions split by what the live warning header actually said: an
    # overflow spill and a missing entitlement are different situations.
    direct = r.get("direct") or []
    overflow_n = sum(1 for x in direct if x.get("reason") == "overflow")
    no_ent_n = sum(1 for x in direct if x.get("reason") == "no_entitlement")
    demoted = r.get("demoted", 0)
    tier_line = (f"interactive {r['tiers']['interactive']} · background {r['tiers']['background']} "
                 f"· demoted {demoted}")
    if demoted:
        tier_line += f" (overflow {overflow_n} · no entitlement {no_ent_n})"
    burst = [
        report.Row("tier header", tier_line),
        report.Row("outcome",
                   f"streamed {r['streamed']} · no first token {r['silent']} · "
                   f"blocked at admission {r['admission_deaths']} · errors {r['errors']} · cut {r['cut']}"),
        report.Row("wait to headers", f"p50 {fmt(r['wait_headers'], 's')}"),
        report.Row("first token", f"p50 {fmt(r['ttfb'], 's')}"),
        report.Row("keep-alives", f"{r['keepalives']}"),
    ]
    adj: list[str] = []
    if r["cut"]:
        adj.append(f"{r['cut']} request(s) had no terminal outcome inside the {r['budget_s']:.0f}s budget "
                   f"and were cut. That is not a failure: the background tier can take ~600s to first "
                   f"token. Raise --max-seconds before judging it.")
    if r["admission_deaths"]:
        adj.append(f"{r['admission_deaths']}/{n} were blocked at admission (`scheduler admission timeout`). "
                   f"For {n} concurrent requests at once, use the -background variant of the same model.")
    if overflow_n:
        adj.append(f"{overflow_n} request(s) carried `overflowed-to-background`: the burst was "
                   f"wider than the interactive admission width - {CAP_COPY}.")
    if no_ent_n:
        # demotion_note lives in _fix_block.py, exec'd into this namespace at
        # import time; the probe and fix advice must say the same thing.
        adj.append(f"{no_ent_n} request(s) carried `no-background-entitlement`: "
                   + demotion_note("no_entitlement", model))
    if r["streamed"] == r["n"] and not r["cut"]:
        adj.append("all requests streamed; no pressure at this width.")
    return report.Report(headline=f"load-test: {n} x {model}",
                         sections=[report.Section("burst", burst)], adjustments=adj)


def cmd_probe(args) -> int:
    # Progress is not report: it goes to stderr whenever stdout is spoken for
    # (--json or a pipe), and to stdout only on a terminal, where motion may
    # redraw it.
    stream = sys.stderr if args.json or not sys.stdout.isatty() else sys.stdout
    reveal(f"load-testing {args.n} x {args.model} (max_tokens {args.max_tokens}, budget {args.max_seconds:.0f}s)",
           getattr(args, "_style", None), stream)
    r = run_probe(args.n, args.model, args.max_tokens, args.timeout, args.max_seconds)
    headroom = _probe_swamp_headroom(args.n)
    if args.json:
        payload: dict = {"run": r}
        if headroom:
            payload["summary"] = headroom
            payload["swamp_headroom"] = headroom
        print(json.dumps(envelope("load-test", payload, contract=CONTRACT), indent=2))
        return 0 if r["streamed"] == r["n"] and not r["cut"] else 1
    rep = build_probe_report(r, args.n, args.model)
    if headroom:
        rep.adjustments.append(headroom + ".")
    print_report(rep, args)
    return 0 if r["streamed"] == r["n"] and not r["cut"] else 1


def build_compare_report(rows: list) -> report.Report:
    """Both runs side by side: one row per model, its statistics inside the value.

    The old printer's class column leads each value so a demoted burst still
    shows which tier answered; the fixed tier advice and the dirty-run note
    ride along as adjustments.
    """
    runs = []
    for r in rows:
        klass = ", ".join(k for k, v in r["tiers"].items() if v) or "n/a"
        runs.append(report.Row(
            r["model"],
            f"{klass} · streamed {r['streamed']} · no token {r['silent']} · "
            f"adm.blocked {r['admission_deaths']} · cut {r['cut']} · ttfb p50 {fmt(r['ttfb'], 's')}"))
    adj = ["the interactive tier admits fewer parallel requests but answers in tens of seconds; "
           "the -background tier admits more and answers in minutes. Pick the tier by how many "
           "concurrent requests you hold at once, not by impatience."]
    if any(r["cut"] or r["admission_deaths"] or r["silent"] for r in rows):
        adj.append("at least one row did not stream cleanly at this width. Read the two rows against "
                   "each other before changing models: `cut` means the budget was too small to judge "
                   "the tier.")
    return report.Report(headline=f"compare at width {rows[0]['n']}",
                         sections=[report.Section("runs", runs)], adjustments=adj)


def cmd_compare(args) -> int:
    base = args.model
    bg = base if base.endswith("-background") else base + "-background"
    rows = []
    stream = sys.stderr if args.json or not sys.stdout.isatty() else sys.stdout
    for model in (base, bg):
        reveal(f"running {args.n} x {model} ...", getattr(args, "_style", None), stream)
        r = run_probe(args.n, model, args.max_tokens, args.timeout, args.max_seconds)
        rows.append(r)
    bad = any(r["cut"] or r["admission_deaths"] or r["silent"] for r in rows)
    if args.json:
        print(json.dumps(envelope("compare", {"runs": rows}, contract=CONTRACT), indent=2))
        return 1 if bad else 0
    print_report(build_compare_report(rows), args)
    return 1 if bad else 0


# ---- cli --------------------------------------------------------------------
def window_hours(spec: str | None, now: bool) -> float:
    """Parse 24h/90m/7d. Raises ValueError; the caller turns that into exit 2."""
    if now:
        return 0.25
    if not spec:
        return 24.0
    s = spec.strip().lower()
    unit = s[-1]
    try:
        val = float(s[:-1] or 0) if unit in "hmdwy" else float(s)
    except ValueError:
        raise ValueError(f"bad --since value: {spec!r} (use e.g. 90m, 24h, 7d, 100y)")
    if val <= 0:
        raise ValueError(f"bad --since value: {spec!r} (must be positive)")
    return {"m": val / 60.0, "h": val, "d": val * 24.0,
            "w": val * 168.0, "y": val * 8760.0}.get(unit, val)


def _check_roots(roots: list[str] | None, flag: str) -> list[str]:
    """A typo'd root silently yields a clean, empty diagnosis. Say it out loud."""
    out: list[str] = []
    for r in roots or []:
        path = os.path.expanduser(r)
        if not os.path.isdir(path):
            print(f"luna-doctor: {flag} {r!r} is not a directory; nothing read from it",
                  file=sys.stderr)
            continue
        out.append(path)
    return out


def _catalog_model_ids() -> set | None:
    """The LunaRoute catalog's model ids, from the disk cache only.

    Never the network: a default run stays offline, and no cached catalog
    simply means the claude-code classifier cannot recognize catalog models,
    which are then excluded and counted as unclassified rather than guessed
    in. One `analyze --online` populates the 24h cache that offline runs
    serve (stale included).
    """
    try:
        cat = sources.fetch_catalog(allow_network=False)
        return set(cat.models) if cat is not None else None
    except Exception:
        return None


def collect(since_hours: float, harnesses: list[str],
            pi_roots: list[str] | None = None,
            claude_roots: list[str] | None = None,
            now: float | None = None) -> Requests:
    """Gather requests. Explicit roots replace the default store, never add to it:
    mixing a mirror with the local store would contaminate every per-store number.

    The returned set is LunaRoute-only: the iterators exclude - and count -
    traffic that never touched the gateway, and the counts travel on the
    list's `.excluded` so `analyse` can report them.
    """
    until = now if now is not None else time.time()
    since = until - since_hours * 3600
    reqs = Requests()
    if "pi" in harnesses:
        roots = [os.path.expanduser(PI_STORE)] if pi_roots is None else pi_roots
        pi = iter_pi(roots, since, until, prune_mtime=pi_roots is None)
        reqs += pi
        reqs.excluded.update(pi.excluded)
    if "claude-code" in harnesses:
        roots = ([os.path.expanduser(CLAUDE_STORE)] if claude_roots is None
                 else claude_roots)
        cc = iter_claude(roots, since, until, prune_mtime=claude_roots is None,
                         catalog=_catalog_model_ids())
        reqs += cc
        reqs.excluded.update(cc.excluded)
    return reqs


def cmd_discover(args) -> int:
    d = discover()
    if args.json:
        print(json.dumps(envelope("discover", {"stores": d}, contract=CONTRACT), indent=2))
    else:
        print_report(build_discover_report(_discover_stores(d)), args)
    return 0 if any(v.get("present") for k, v in d.items() if isinstance(v, dict)) else 2


def _discover_stores(d: dict) -> dict:
    """Flatten discover()'s result into the store list a report is built from.

    discover() keeps one nested dict per store because --json prints it that
    way; the report builder takes the same facts as a flat `stores` list plus
    the swamp and LunaRoute stanzas it already knows the shapes of.
    """
    stores = []
    for name in ("pi", "claude-code", "codex"):
        s = d[name]
        stores.append({"name": name, "path": s["root"], "exists": s["present"],
                       "files": s["files"], "bytes": s["bytes_sampled"],
                       "newest": s["newest"]})
    out = {"stores": stores, "lunaroute_config": d["lunaroute_config"]}
    if d.get("swamp") is not None:
        out["swamp"] = d["swamp"]
    return out


def build_discover_report(d: dict) -> report.Report:
    """One row per readable store: what state, how many files, how fresh.

    Swamp and the LunaRoute config ride along as rows of their own so the
    plain output still names everything the old inline printer named.
    """
    rows: list[report.Row] = []
    for s in d.get("stores") or []:
        bits = ["present" if s.get("exists") else "absent",
                f"{s.get('files', 0)} files", f"{s.get('bytes', 0)} bytes"]
        if s.get("newest"):
            bits.append(f"newest {s['newest']}")
        rows.append(report.Row(s["name"], " · ".join(bits)))
    sw = d.get("swamp")
    if sw is not None and sw.get("present") and sw.get("inUse"):
        # Only a swamp with activity in the window earns a row; an idle
        # install is visible in `discover --json` and nowhere else.
        fp = sw.get("bundleFingerprint") or "\u2014"
        files = sw.get("files") or {}
        bits = ["in use",
                f"{files.get('behaviorHistory', 0)} behavior",
                f"{files.get('workflowRuns', 0)} workflow",
                f"bundle {fp}"]
        if sw.get("envPresence"):
            bits.append(".env.swamp")
        if sw.get("repoDir"):
            bits.append(sw["repoDir"])
        bits.append(f"({sw.get('evidence', 'activity in window')})")
        rows.append(report.Row("swamp", " · ".join(bits)))
    lr = d.get("lunaroute_config")
    if lr is not None:
        state = "present" if lr.get("present") else "absent"
        rows.append(report.Row("lunaroute", f"{state} · {lr.get('path', '')}"))
    footnote = ("luna-doctor reads these stores read-only. codex rollouts are mostly zstd and are\n"
                "not LunaRoute traffic on most machines, so tier analysis skips them for now.")
    return report.Report(headline="stores", sections=[report.Section("", rows)],
                         footnote=footnote)


# ---- the capacity command ----------------------------------------------------
# The one command that may fetch the account capacity live (with --online):
# it renders the API's own story - live priority-pool occupancy, plan and
# concurrency, the 7d/30d occupancy rollups, and the per-complete-UTC-day
# admission table. The null contract governs every line: an absent value
# renders as unknown with its reason, never as a zero; a null
# avg_queue_wait_ms is "no waiting that day"; background_lanes 0 is a real
# value (no overflow entitled); priority_lanes null is unknown, not zero.

def _cap_field(block, name):
    """One sub-block field, None-safe for absent blocks and out-of-contract
    ones alike."""
    return getattr(block, name, None) if block is not None else None


def _cap_unknown(value) -> str:
    """A capacity value or the unknown line - never a defaulted zero."""
    if value is None:
        return "unknown (not reported)"
    return str(value)


def _cap_flag(value) -> str:
    """A saturated-style flag: booleans read yes/no, null reads unknown."""
    if value is None:
        return "unknown (not reported)"
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return str(value)


def _cap_wait(ms) -> str:
    """avg_queue_wait_ms in seconds. Null is common on days nobody waited
    and means exactly that - "no waiting that day", never 0."""
    if ms is None:
        return "no waiting that day"
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return str(ms)
    return "%.1fs" % (ms / 1000.0)


def _cap_background(value) -> str:
    """background_lanes in the product vocabulary. 0 is a real value - the
    account is entitled to no overflow - and null is unknown, never zero."""
    if value is None:
        return "unknown (not reported)"
    if value == 0:
        return "0 concurrent requests (the account is entitled to no overflow)"
    return "%s concurrent requests" % value


def _cap_overflow(value) -> str:
    """overflow_routing with its scope stated, never a bare switch.

    `enabled` is not a general switch: automatic spill applies to eligible
    managed text-generation requests only - a request explicitly marked
    background (for example background image generation) reaches overflow
    capacity by its own marking, not through this setting."""
    if value is None:
        return "unknown (not reported)"
    if value == "enabled":
        return ("enabled - automatic spill applies to eligible managed "
                "text-generation requests only; requests explicitly marked "
                "background reach overflow capacity by their own marking, "
                "not through this switch")
    return str(value)


def _capacity_rollup_value(r: dict) -> str:
    """One occupancy rollup in the product vocabulary.

    The API's internal field names for concurrent requests are field names,
    not product language, so the rendered copy speaks concurrent requests.
    days_active (days with traffic) and days_covered (days with data) are
    different facts, labelled separately, and never combined into a rate;
    every absent value is unknown, not zero."""
    def n(v):
        return "unknown (not reported)" if v is None else str(v)
    return " \u00b7 ".join([
        "peak %s concurrent requests" % n(r.get("peak_lanes")),
        "average %s" % n(r.get("avg_lanes")),
        "p95 %s" % n(r.get("p95_lanes")),
        "occupancy %s concurrent-request-hours" % n(r.get("lane_hours")),
        "queued %s%% of the time" % n(r.get("queued_pct")),
        "%s day(s) with traffic" % n(r.get("days_active")),
        "%s day(s) with data" % n(r.get("days_covered")),
    ])


def _capacity_admission_row(entry: dict) -> report.Row:
    """One complete-UTC-day admission entry as a row: every count is the
    API's own, a null count is unknown, and a null avg_queue_wait_ms is
    "no waiting that day" - never a zero anywhere."""
    bits = []
    for name in ("accepted", "deferred", "denied", "denied_after_wait"):
        v = entry.get(name)
        bits.append("%s %s" % (name.replace("_", " "),
                               "unknown (not reported)" if v is None else str(v)))
    bits.append("avg queue wait %s" % _cap_wait(entry.get("avg_queue_wait_ms")))
    return report.Row(str(entry.get("day") or "(no day)"), " \u00b7 ".join(bits))


def build_capacity_report(cap, note: str = "") -> report.Report:
    """The capacity API's own story as a Report.

    `now` is the priority pool only (overflow traffic does not appear in
    in_use; it does appear in admission), the admission table covers
    complete UTC days only, and the counts' exclusions are stated where the
    counts are - a naive reader would otherwise take them for the whole
    account's traffic. When `cap` is None everything is unknown with its
    reason; fetching is `--online`, and a fetch failure is never an error.
    """
    if cap is None:
        return report.Report(
            headline="account capacity: unknown",
            sections=[],
            adjustments=[],
            footnote=(f"{note or 'not established'}\n"
                      "pass --online to fetch it now (api.lunaroute.com, one "
                      "authenticated GET, cached 5 minutes); what cannot be "
                      "fetched is reported unknown, never an error"))

    n, cfg, h, adm = cap.now, cap.config, cap.history, cap.admission
    now_rows = [report.Row(
        "covers", "the priority pool only - overflow traffic does not appear "
        "in in_use (it does appear in the admission counts below)")]
    if n is None:
        now_rows.append(report.Row("now", "not reported by the account (unknown)"))
    else:
        in_use, limit, avail = (_cap_field(n, "in_use"), _cap_field(n, "limit"),
                                _cap_field(n, "available"))
        if in_use is None and limit is None:
            occ = "occupancy unknown (not reported)"
        elif in_use is None:
            occ = f"in use unknown (not reported) \u00b7 limit {limit}"
        elif limit is None:
            occ = f"in use {in_use} \u00b7 limit unknown (not reported)"
        else:
            occ = f"in use {in_use} of {limit} priority concurrent requests"
        if avail is not None:
            occ += f" \u00b7 {avail} available"
        now_rows.append(report.Row("occupancy", occ))
        now_rows.append(report.Row("saturated", _cap_flag(_cap_field(n, "saturated"))))
        now_rows.append(report.Row("ledger", _cap_unknown(_cap_field(n, "ledger"))))
        now_rows.append(report.Row("sampled at", _cap_unknown(_cap_field(n, "sampled_at"))))

    config_rows = [
        report.Row("plan", _cap_unknown(_cap_field(cfg, "plan"))),
        report.Row("priority concurrency", _cap_unknown(_cap_field(cfg, "priority_lanes"))),
        report.Row("background", _cap_background(_cap_field(cfg, "background_lanes"))),
        report.Row("overflow routing", _cap_overflow(_cap_field(cfg, "overflow_routing"))),
    ]

    hist_rows = []
    for label, key in (("last 7 days", "last_7d"), ("last 30 days", "last_30d")):
        r = _cap_field(h, key)
        if isinstance(r, dict):
            val = _capacity_rollup_value(r)
            as_of = _cap_field(h, "as_of")
            if as_of is not None:
                val += f"\nrollup as of {as_of}"
            hist_rows.append(report.Row(label, val))
        else:
            hist_rows.append(report.Row(label, "not reported by the account (unknown)"))
    tail = _cap_field(h, "tail")
    if isinstance(tail, dict):
        hist_rows.append(report.Row(
            "tail", "minute-level occupancy through %s"
                    % _cap_unknown(tail.get("through"))))
    else:
        hist_rows.append(report.Row(
            "tail", "not reported (null) - days_covered already excludes it, "
            "so a smaller count never means fewer days had traffic"))
    rc = _cap_field(h, "rollup_computed_at")
    if rc is not None:
        hist_rows.append(report.Row("rollup computed at", str(rc)))

    as_of = _cap_field(adm, "as_of")
    admission_sections = []
    for label, key in (("admission \u00b7 last 7 days", "last_7d"),
                       ("admission \u00b7 last 30 days", "last_30d")):
        entries = _cap_field(adm, key)
        rows = [report.Row(
            "covers", "one entry per complete UTC day%s - the in-progress day "
            "is never included, so a bursty afternoon must not read as a "
            "collapse" % (f", through {as_of} (as_of)" if as_of is not None else "")),
            report.Row("counts", CAPACITY_COUNTS_EXCLUDE)]
        if isinstance(entries, list) and entries:
            for e in entries:
                if isinstance(e, dict):
                    rows.append(_capacity_admission_row(e))
        else:
            rows.append(report.Row("days", "not reported by the account (unknown)"))
        admission_sections.append(report.Section(label, rows))

    head_bits = []
    plan = _cap_field(cfg, "plan")
    pri = _cap_field(cfg, "priority_lanes")
    if plan is not None:
        head_bits.append(f"plan {plan}")
    if pri is not None:
        head_bits.append(f"priority concurrency {pri}")
    headline = "account capacity"
    headline += ": " + " \u00b7 ".join(head_bits) if head_bits \
        else ": plan and concurrency not reported (unknown)"

    foot = note or cap.describe()
    foot += (" \u00b7 history is cached \u22485 minutes server-side; now and "
             "admission are computed per call \u00b7 rate limit 60/min per key")
    if cap.stale:
        foot += (" \u00b7 the cache is past its 5-minute TTL: run with "
                 "--online to refresh")
    foot += f"\nluna-doctor v{VERSION}"

    sections = ([report.Section("now", now_rows), report.Section("config", config_rows),
                 report.Section("history", hist_rows)] + admission_sections)
    return report.Report(headline=headline, sections=sections, footnote=foot)


def cmd_capacity(args) -> int:
    """The one command that may fetch the capacity live: --online refreshes
    the 5-minute disk cache from api.lunaroute.com; the default serves the
    cache and never touches the network. Whatever cannot be established
    renders unknown with its reason - a network failure never turns the
    report red, and exit codes follow the tool's convention (0 data, 2 none).
    """
    online = bool(getattr(args, "online", False))
    try:
        cap, note = sources._capacity_with_note(online)
    except Exception as e:
        cap, note = None, f"collection failed: {type(e).__name__}: {e}"[:200]
    if cap is None and online:
        print(f"luna-doctor: capacity unavailable ({note}); reported unknown",
              file=sys.stderr)
    elif cap is None:
        # A cold cache is not a failure and must not be silent: exit 2 says
        # "no data", so the way to get data has to be on screen.
        print(f"luna-doctor: no capacity on disk yet ({note}); reported unknown. "
              f"Fetch it with: luna-doctor capacity --online", file=sys.stderr)
    if args.json:
        payload = ({"capacity": cap.to_dict()} if cap is not None
                   else {"capacity": {"provenance": sources.PROV_UNKNOWN,
                                       "reason": note}})
        print(json.dumps(envelope("capacity", payload, contract=CONTRACT), indent=2))
    else:
        print_report(build_capacity_report(cap, note), args)
    return 0 if cap is not None else 2


PRERELEASE_NOTE = "alpha — not for production" if "-" in VERSION else ""

def _version_string() -> str:
    return f"luna-doctor {VERSION}" + (f" ({PRERELEASE_NOTE})" if PRERELEASE_NOTE else "")

def _add_style(sp) -> None:
    """The renderer selector every report-producing command takes."""
    sp.add_argument("--style", choices=("auto", "pretty", "plain"),
                    default=None,
                    help="how to render the report: auto picks pretty on a color "
                         "terminal and plain otherwise (default)")


def _add_copy(sp) -> None:
    """The clipboard opt-in for the commands whose stdout is a pasteable artifact.

    `chart` is excluded on purpose: its artifact is the SVG file, and the only
    stdout it owns is a path line.
    """
    sp.add_argument("--copy", action="store_true",
                    help="also put the printed text on the clipboard, as plain "
                         "text (pbcopy, wl-copy, xclip or xsel); stdout is "
                         "unchanged, so nothing is lost when no tool exists")


def _add_network(sp, subject: str = "the live model catalog from the gateway",
                 ttl: str = "24h") -> None:
    """The offline/online pair for the commands that consume fetched facts.

    The default is offline: a cached payload serves (a stale one too,
    marked stale), and the network is never touched. `--online` allows one
    authenticated GET (refreshing the disk cache); a network failure still
    never turns the report into an error - what could not be fetched is
    reported unknown.
    """
    grp = sp.add_mutually_exclusive_group()
    grp.add_argument("--online", action="store_true",
                     help=f"fetch {subject} (one authenticated GET) and "
                          f"refresh the {ttl} disk cache; without this flag "
                          f"nothing leaves the machine")
    grp.add_argument("--offline", action="store_true",
                     help=f"never touch the network, even for {subject} "
                          f"(this is the default; stated for scripts)")


def probe_required_message(cmd: str, n: int, model: str, budget_s: float) -> str:
    """What a load test would do, stated without running it.

    LunaRoute bills concurrent sessions, so the message counts sessions,
    not usage of any other kind. The run is opt-in because it is real:
    it opens live sessions against the gateway and spends included
    inference, and the message says both.
    """
    width = f"{n:g}"
    if cmd == "compare":
        return (f"compare would load-test {model} and its -background twin: two "
                f"bursts of {width} concurrent sessions each, for up to "
                f"{budget_s:g}s, spending included inference - a load test "
                f"against the live gateway, opt-in only; pass "
                f"--load-test-enable to run it")
    return (f"{cmd} would open {width} concurrent sessions at {model} for up "
            f"to {budget_s:g}s, spending included inference - a load test "
            f"against the live gateway, opt-in only; pass --load-test-enable "
            f"to run it")


# ---- the friendly default run (bare `luna-doctor`) ---------------------------

WELCOME = (
    "LunaRoute is the gateway in front of your models, and its capacity is\n"
    "measured in concurrent requests, not tokens: how many may run at once.\n"
    "\n"
    "This run read the last hour of your local agent logs - nothing was sent\n"
    "anywhere. The same hour is drawn as a chart beside the report below,\n"
    "written to this directory as an SVG and a PNG (their paths print after\n"
    "the report).\n"
    "\n"
    "The rest of the picture, one command per line:\n"
    "  analyze   the same report for any window (luna-doctor analyze --since 24h)\n"
    "  chart     the drawing alone, for any window\n"
    "  fix       copy-paste settings that lower your concurrency\n"
    "  capacity  the account line above, in full: plan, occupancy, admission"
)


def _account_line() -> str:
    """The account's own capacity facts, one line, from the disk cache.

    The default run opens with this because it frames everything after
    it: the local numbers are being judged against the account's real
    allowance. Cache-only by construction - the bare run never waits on
    the 4-17s endpoint - and a cold cache, a null config or a collection
    failure is unknown with the command that fills it: never a block,
    never a fabrication."""
    try:
        cap, _ = sources._capacity_with_note(False)
    except Exception:
        cap = None
    cfg = getattr(cap, "config", None) if cap is not None else None

    def _num(v):
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    bits: list = []
    plan_name = getattr(cfg, "plan", None)
    if isinstance(plan_name, str) and plan_name:
        bits.append("%s plan" % plan_name)
    if _num(getattr(cfg, "priority_lanes", None)) is not None:
        bits.append("%d priority concurrent requests" % cfg.priority_lanes)
    if _num(getattr(cfg, "background_lanes", None)) is not None:
        bits.append("%d background" % cfg.background_lanes)
    overflow = getattr(cfg, "overflow_routing", None)
    if isinstance(overflow, str) and overflow:
        bits.append("overflow %s" % overflow)
    if not bits:
        return "account: unknown (fetch with: luna-doctor capacity)"
    return "LunaRoute account: " + " \u00b7 ".join(bits)


def _open_png_quietly(path) -> None:
    """Open the chart PNG in the system viewer, when that can work.

    macOS `open`, and only when stdout is a terminal someone is reading:
    printing the paths is the required minimum, and this can never be a
    failure point - every way it can go wrong is swallowed."""
    if not path or not sys.stdout.isatty() or sys.platform != "darwin":
        return
    try:
        subprocess.run(["open", path], check=False, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _default_run(ap) -> int:
    """What bare `luna-doctor` does: the friendly default run.

    The account line first - it frames the judgement - then a short
    welcome mat, then the one-hour analysis and the chart beside it, SVG
    and PNG both written to the working directory. The exit code is the
    analysis verdict; the chart is a bonus that can never turn the run
    red, and a chart that cannot be drawn says so and steps aside."""
    print(_account_line())
    print()
    print(WELCOME)
    code = _run(ap.parse_args(["analyze", "--since", "1h"]))
    if code == 2:
        # Nothing was measured, so there is nothing to draw: a picture and a
        # "this run read the last hour of your logs" claim on a machine with
        # no logs is a lie with a timestamp on it.
        print()
        print("No agent log held a request in the last hour, so no chart was drawn. "
              "Run `luna-doctor discover` to see which stores exist on this machine, "
              "or `luna-doctor analyze --since 7d` for a wider window.")
        return code
    chart_args = ap.parse_args(["chart", "--since", "1h"])
    try:
        _run(chart_args)
    except Exception as e:
        print("luna-doctor: the chart could not be drawn: %s: %s"
              % (type(e).__name__, e), file=sys.stderr)
    _open_png_quietly(getattr(chart_args, "_png_path", None))
    return code


def main(argv: list[str] | None = None) -> int:
    # The gate is not resolved yet when argparse builds help text, so the
    # prerelease banner decides inline: no ANSI when NO_COLOR is set or
    # stdout is not a terminal someone is reading.
    desc = DESCRIPTION
    if PRERELEASE_NOTE and not os.environ.get("NO_COLOR", "") and sys.stdout.isatty():
        desc += f"\n\n\033[33m⚠ This is a prerelease ({PRERELEASE_NOTE}) — not recommended for production.\033[0m"
    ap = argparse.ArgumentParser(prog="luna-doctor", description=desc,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=_version_string())
    # No subcommand is the friendly default run, so `cmd` is optional; the
    # welcome path prints the account line, a short explanation, the
    # one-hour analysis and the chart beside it.
    sub = ap.add_subparsers(dest="cmd", required=False)

    d = sub.add_parser("discover", help="what log stores exist on this machine")
    d.add_argument("--json", action="store_true")
    _add_copy(d)
    _add_style(d)
    d.set_defaults(func=cmd_discover)

    ex = sub.add_parser("explain",
                        help="explain one request: what it was, how long it waited, whose room "
                             "it was waiting for")
    ex.add_argument("--since", default=None,
                    help="window, e.g. 24h, 90m, 7d (default 24h)")
    ex.add_argument("--now", action="store_true", help="last 15 minutes")
    ex.add_argument("--last", type=int, default=1, metavar="N",
                    help="explain the N slowest requests instead of one (default 1)")
    ex.add_argument("--slowest", action="store_true",
                    help="explain the slowest requests by duration instead of the ones that "
                         "were refused or stalled")
    ex.add_argument("--blocked", action="store_true",
                    help="explain the most recent refused or stalled request instead of the "
                         "slowest")
    ex.add_argument("id", nargs="?", default=None, metavar="ID",
                    help="a response id, session id or run id to explain")
    ex.add_argument("--harness", default="pi,claude-code")
    ex.add_argument("--pi-root", action="append", default=None, metavar="DIR",
                    help="pi-format store to read instead of ~/.pi/agent/sessions (repeatable)")
    ex.add_argument("--claude-root", action="append", default=None, metavar="DIR",
                    help="claude-code store to read instead of ~/.claude/projects (repeatable)")
    ex.add_argument("--json", action="store_true")
    _add_copy(ex)
    _add_style(ex)
    plan.add_plan_arg(ex)
    ex.set_defaults(func=cmd_explain)

    w = sub.add_parser("watch",
                       help="watch the last few minutes live: one line per state change, and a "
                            "summary when you stop it")
    w.add_argument("--since", default="15m", help="lookback window per sample (default 15m)")
    w.add_argument("--now", action="store_true", help="last 15 minutes (the default)")
    w.add_argument("--interval", type=int, default=10, metavar="SECONDS",
                   help="seconds between samples (default 10)")
    w.add_argument("--count", type=int, default=0, metavar="N",
                   help="stop after N samples (default: until Ctrl-C)")
    w.add_argument("--once", action="store_true", help="print one sample and stop")
    w.add_argument("--harness", default="pi,claude-code")
    w.add_argument("--json", action="store_true", help="print each sample as JSON lines")
    _add_style(w)
    plan.add_plan_arg(w)
    w.set_defaults(func=cmd_watch)

    a = sub.add_parser("analyze", help="explain what your traffic is doing to your concurrency")
    a.add_argument("--since", default=None, help="window, e.g. 24h, 90m, 7d (default 24h)")
    a.add_argument("--now", action="store_true", help="last 15 minutes")
    a.add_argument("--harness", default="pi,claude-code")
    a.add_argument("--pi-root", action="append", default=None, metavar="DIR",
                   help="pi-format store to read instead of ~/.pi/agent/sessions (repeatable)")
    a.add_argument("--claude-root", action="append", default=None, metavar="DIR",
                   help="claude-code store to read instead of ~/.claude/projects (repeatable)")
    a.add_argument("--store-dir", default=None, metavar="DIR",
                   help="where run extractions are written (default ~/.luna-doctor/store)")
    a.add_argument("--no-store", action="store_true", help="do not persist this run")
    a.add_argument("--baseline", default=None, metavar="prev",
                   help="compare with the window before this one (only 'prev' is supported) "
                        "and print a change: section")
    plan.add_plan_arg(a)
    a.add_argument("--for-issue", action="store_true",
                   help="print one redacted, datable block for a support ticket or an issue "
                        "instead of the report")
    a.add_argument("--json", action="store_true")
    _add_copy(a)
    _add_network(a)
    _add_style(a)
    a.set_defaults(func=cmd_analyze)

    # Out of the advertised surfaces on purpose: this command opens real
    # concurrent sessions against the live gateway, so it is opt-in (the
    # gate below) and undocumented - the README, the skill and the bare
    # run's welcome do not mention it. `--help` is the one place it stays
    # visible: argparse cannot hide a subcommand cleanly (help=SUPPRESS
    # leaks a literal marker into the listing), so it is listed like any
    # other command, invocable as ever, and its own `--help` documents it.
    p = sub.add_parser("load-test",
                       help="load-test the gateway at a chosen burst width "
                            "(needs --load-test-enable to actually open one)",
                       description="load-test the gateway at a chosen burst width "
                                   "(needs --load-test-enable to actually open one)")
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--model", default="glm-5.3")
    p.add_argument("--max-tokens", type=int, default=800)
    p.add_argument("--timeout", type=float, default=320.0)
    p.add_argument("--max-seconds", type=float, default=170.0)
    p.add_argument("--load-test-enable", action="store_true",
                   help="open the burst; without this flag luna-doctor only says "
                        "what it would open")
    p.add_argument("--json", action="store_true")
    _add_copy(p)
    _add_style(p)
    p.set_defaults(func=cmd_probe)

    c = sub.add_parser("compare", help="load-test a model and its -background twin "
                                       "(needs --load-test-enable to actually open one)")
    c.add_argument("--n", type=int, default=10)
    c.add_argument("--model", default="glm-5.3")
    c.add_argument("--max-tokens", type=int, default=800)
    c.add_argument("--timeout", type=float, default=320.0)
    c.add_argument("--max-seconds", type=float, default=170.0)
    c.add_argument("--load-test-enable", action="store_true",
                   help="open the bursts; without this flag luna-doctor only says "
                        "what they would open")
    c.add_argument("--json", action="store_true")
    _add_copy(c)
    _add_style(c)
    c.set_defaults(func=cmd_compare)

    f = sub.add_parser("fix", help="print per-agent copy-paste instructions")
    f.add_argument("--since", default=None)
    f.add_argument("--now", action="store_true")
    f.add_argument("--harness", default="pi,claude-code")
    f.add_argument("--agent", default=None,
                   help="which agents to emit fixes for: pi,claude-code,codex (default all)")
    f.add_argument("--pi-root", action="append", default=None, metavar="DIR",
                   help="pi-format store to read instead of the default (repeatable)")
    f.add_argument("--claude-root", action="append", default=None, metavar="DIR",
                   help="claude-code store to read instead of the default (repeatable)")
    f.add_argument("--json", action="store_true")
    _add_copy(f)
    _add_network(f)
    plan.add_plan_arg(f)
    f.set_defaults(func=None)

    ch = sub.add_parser("chart", help="write the chart as a standalone SVG, plus a pasteable PNG")
    ch.add_argument("--since", default=None)
    ch.add_argument("--now", action="store_true")
    ch.add_argument("--harness", default="pi,claude-code")
    ch.add_argument("--out", default=None,
                   help="path for the .svg (default: ./luna-doctor-<ts>.svg); "
                        "the PNG takes the same stem")
    ch.add_argument("--no-image", action="store_true",
                    help="do not write the PNG beside the SVG")
    ch.add_argument("--width", type=int, default=1120)
    ch.add_argument("--height", type=int, default=640)
    ch.add_argument("--open", action="store_true", help="open the SVG when it is written")
    ch.add_argument("--pi-root", action="append", default=None, metavar="DIR",
                    help="pi-format store to read instead of the default (repeatable)")
    ch.add_argument("--claude-root", action="append", default=None, metavar="DIR",
                    help="claude-code store to read instead of the default (repeatable)")
    plan.add_plan_arg(ch)
    ch.add_argument("--json", action="store_true")
    _add_style(ch)
    ch.set_defaults(func=None)

    cp = sub.add_parser("capacity",
                        help="the account's capacity: plan, priority concurrency, "
                              "occupancy and admission (api.lunaroute.com, cached)")
    cp.add_argument("--json", action="store_true")
    _add_copy(cp)
    _add_network(cp, subject="the account's capacity from api.lunaroute.com",
                 ttl="5-minute")
    _add_style(cp)
    cp.set_defaults(func=cmd_capacity)

    args = ap.parse_args(argv)
    if args.cmd is None:
        # the bare run: account line, welcome mat, the one-hour analysis,
        # and the chart (SVG and PNG) beside it
        return _default_run(ap)
    if args.cmd in ("load-test", "compare") and getattr(args, "n", 1) < 1:
        print("luna-doctor: --n must be at least 1", file=sys.stderr)
        return 2
    # A burst opens real concurrent sessions on the account, so it stays
    # behind an explicit opt-in: without the flag we say what it would do
    # and nothing is opened.
    if args.cmd in ("load-test", "compare") and not args.load_test_enable:
        print(probe_required_message(args.cmd, args.n, args.model, args.max_seconds),
              file=sys.stderr)
        return 2
    if getattr(args, "copy", False):
        return _run_captured(args)
    return _run(args)


def _run(args) -> int:
    """Style resolution and dispatch: everything after argument validation."""
    # One style resolution per command. `--json` bypasses styling entirely:
    # no Style is constructed on that path, so no code can reach the envelope.
    if hasattr(args, "style") and not args.json:
        args._style = style_for(args.style, sys.stdout)
    else:
        args._style = None
    if args.cmd == "analyze":
        return cmd_analyze(args)
    if args.cmd == "fix":
        return cmd_fix(args)
    if args.cmd == "chart":
        return cmd_chart(args)
    if args.cmd == "explain":
        return cmd_explain(args)
    if args.cmd == "watch":
        return cmd_watch(args)
    return args.func(args)


def _run_captured(args) -> int:
    """`--copy`: run with stdout captured, echo it, then offer it to the clipboard.

    Capture is the only mechanism that can promise "exactly what the command
    printed" across five commands that each own their printing - `fix` prints
    its block directly, `--json`
    prints its own envelope. The captured text is echoed to the real stdout
    after the run, so stdout is unchanged and a failed copy is never a lost
    answer. It is also why `--copy` renders plain even under an explicit
    `--style pretty`: the clipboard is a paste target, not a terminal, and the
    copied text must carry no ANSI escapes - since the copy is the echo, the
    whole run renders plain.
    """
    if not args.json and getattr(args, "style", None) == "pretty":
        args.style = "plain"
    real = sys.stdout
    buf = io.StringIO()
    sys.stdout = buf
    try:
        code = _run(args)
    finally:
        sys.stdout = real
    text = buf.getvalue()
    write_out(text)
    _copy_or_say(text)
    return code


# ---- the account's capacity (analyze consumes the cache) ---------------------

# What the capacity API's admission and history counts cover, stated wherever
# server counts sit beside local ones: the server sees only traffic
# contending for the shared pool. Requests routed on the account's own
# provider keys (BYOK), embeddings, and dedicated-capacity traffic are
# always excluded (System One decision requests count only when the org has
# no dedicated System One allocation); the local logs DO see such requests,
# so the two counts may legitimately differ for reasons that are not errors
# - and nothing reconciles them.
CAPACITY_COUNTS_EXCLUDE = (
    "server counts cover only traffic contending for the shared pool: requests "
    "routed on your own provider keys (BYOK), embeddings, and dedicated-capacity "
    "traffic are always excluded (System One decision requests count only without "
    "a dedicated System One allocation); the local counts are not so filtered, so "
    "the two may differ for those reasons and are never reconciled")


def _utc_days(since: float, until: float) -> list:
    """The UTC calendar-day strings a window touches, ascending.

    The capacity API reports admission per complete UTC day, so this is
    the join key: every calendar day the window overlaps, whether or not
    the API has an entry for it (the in-progress day never has one)."""
    out: list = []
    if until < since:
        return out
    d = datetime.fromtimestamp(since, timezone.utc).date()
    last = datetime.fromtimestamp(until, timezone.utc).date()
    while d <= last:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _capacity_metrics(since: float, until: float, m: dict) -> dict:
    """The additive metrics.capacity block: the account's plan, concurrency
    and overflow routing, plus - when the window overlaps the API's complete
    UTC days - the server's admission counts for those days placed next to
    the local outcome counts.

    analyze reads the 5-minute disk cache and never fetches live: the
    endpoint answers in 4-17s, and `luna-doctor capacity` is the command
    that refreshes it. No cache, an unreadable cache or a null config all
    degrade to unknown with a reason - never to zero, never to an error,
    and every field keeps the API's own name verbatim.
    """
    try:
        cap, note = sources._capacity_with_note(False)
    except Exception as e:
        return {"provenance": sources.PROV_UNKNOWN,
                "reason": f"collection failed: {type(e).__name__}: {e}"[:200]}
    if cap is None:
        return {"provenance": sources.PROV_UNKNOWN, "reason": note}

    def cfg(name):
        return getattr(cap.config, name, None) if cap.config is not None else None

    block = {
        "provenance": sources.PROV_ACCOUNT,
        "source": cap.describe(),
        "stale": cap.stale,
        "note": note,
        "plan": cfg("plan"),
        "priority_lanes": cfg("priority_lanes"),
        "background_lanes": cfg("background_lanes"),
        "overflow_routing": cfg("overflow_routing"),
    }
    if cap.admission is not None:
        block["admission_as_of"] = getattr(cap.admission, "as_of", None)
    by_day = dict(sources.capacity_admission_days(cap))
    days = [d for d in _utc_days(since, until) if d in by_day]
    if days:
        # verbatim day entries - accepted/deferred/denied/denied_after_wait/
        # avg_queue_wait_ms keep the API's names and nulls
        block["window_days"] = [dict(by_day[d]) for d in days]
        block["local_outcomes"] = dict(m.get("by_outcome") or {})
        block["counts_exclude"] = CAPACITY_COUNTS_EXCLUDE
    return block


def _watch_sample(args, now_ts=None):
    """One sample: collect the lookback window, analyse it, attach the account.

    The lookback is short by design - a live view should show the last few
    minutes, not a day - and it is a plain log read: no network, no store write.
    """
    hours = window_hours(args.since, args.now)
    harnesses = [h.strip() for h in args.harness.split(",") if h.strip()]
    now = now_ts if now_ts is not None else time.time()
    reqs = collect(hours, harnesses, None, None, now=now)
    m = analyse(reqs, hours)
    m["declared_plan"] = getattr(args, "plan", None)
    try:
        m["capacity"] = _capacity_metrics(now - hours * 3600, now, m)
    except Exception:
        m["capacity"] = {"provenance": sources.PROV_UNKNOWN}
    capm = m.get("capacity") or {}
    served = capm if capm.get("provenance") == sources.PROV_ACCOUNT else None
    return m, served


def cmd_watch(args) -> int:
    """Live view: one line per state change, until Ctrl-C or --count.

    A terminal gets a redrawn frame; a pipe gets append-only lines, because
    cursor control down a pipe is how a log file ends up full of escape
    sequences. Ctrl-C prints the session summary and exits with the verdict of
    the last sample, so `watch` is usable as a slow poll from a script.
    """
    interval = max(1, int(getattr(args, "interval", 10) or 10))
    count = max(0, int(getattr(args, "count", 0) or 0))
    if getattr(args, "once", False):
        # --once is one sample and out: without this the loop would sample once
        # and then wait forever, which is the opposite of "once".
        count = 1
    tty = sys.stdout.isatty()
    started = datetime.now(timezone.utc).isoformat()
    samples = 0
    last_line = None
    previous = None
    previous_capacity = None
    m = None
    served = None
    code = 0

    def one(tag):
        nonlocal samples, last_line, previous, previous_capacity, m, served, code
        m, served = _watch_sample(args)
        now_iso = datetime.now(timezone.utc).isoformat()
        line = watch_mod.sample_line(m, served, now_iso, previous)
        samples += 1
        if getattr(args, "json", False):
            # JSON lines: one object per sample, so a cron job or a chat bridge
            # can consume the stream without parsing prose.
            print(json.dumps({"sample": samples, "at": now_iso, "line": line}), flush=True)
            last_line = line
            previous, previous_capacity = m, served
            _h, _a, code = verdict(m)
            return line
        if tag == "print":
            print(line, flush=True)
            last_line = line
        elif watch_mod.state_changed(previous, m, previous_capacity, served):
            if tty and not getattr(args, "once", False):
                hist = ([last_line] if last_line else []) + [line]
                frame = watch_mod.frame(m, served, now_iso, hist[-6:])
                sys.stdout.write("\x1b[H\x1b[2J" + "\n".join(frame) + "\n")
                sys.stdout.flush()
            else:
                print(line, flush=True)
            last_line = line
        previous, previous_capacity = m, served
        head, adj, code = verdict(m)
        return line

    print("luna-doctor watch - one line per state change. Ctrl-C stops and prints a summary.",
          file=sys.stderr if tty else sys.stdout, flush=True)
    print("(each sample reads the last %s of local logs; nothing is sent anywhere)"
          % human_window(window_hours(args.since, args.now)),
          file=sys.stderr if tty else sys.stdout, flush=True)
    try:
        one("print")
        while True:
            if count and samples >= count:
                break
            time.sleep(interval)
            one("append")
    except KeyboardInterrupt:
        pass
    ended = datetime.now(timezone.utc).isoformat()
    if m is not None:
        for row in watch_mod.summary(m, served, samples, started, ended):
            print(row, flush=True)
    return code


def cmd_explain(args) -> int:
    """One request, explained: what it was, how long it took, what it got, and
    whose room it was waiting for.

    Selection, in the order the flags imply: an explicit id, then the most
    recent refusals (`--blocked`), then the slowest requests. Exit codes follow
    the tool's convention: 1 when the request explained was refused or stalled
    (there is something to act on), 0 when it was served, 2 when the window
    holds nothing to explain.
    """
    try:
        hours = window_hours(args.since, args.now)
    except ValueError as e:
        print(f"luna-doctor: {e}", file=sys.stderr)
        return 2
    harnesses = [h.strip() for h in args.harness.split(",") if h.strip()]
    raw_pi = getattr(args, "pi_root", None)
    raw_claude = getattr(args, "claude_root", None)
    pi_roots = _check_roots(raw_pi, "--pi-root") if raw_pi else None
    claude_roots = _check_roots(raw_claude, "--claude-root") if raw_claude else None
    now = time.time()
    reqs = collect(hours, harnesses, pi_roots, claude_roots, now=now)
    if not reqs:
        if args.json:
            print(json.dumps(envelope("explain", {
                "headline": "No requests found in the window.",
                "requests": [],
            }, contract=CONTRACT), indent=2))
        else:
            print(f"No requests found in the last {human_window(hours)}.")
            print("Widen the window with --since, or run `luna-doctor discover`.")
        return 2
    limit = max(1, int(getattr(args, "last", 1) or 1))
    if getattr(args, "id", None):
        picked = explain.by_id(reqs, args.id)
        if not picked:
            print(f"luna-doctor: no request in the last {human_window(hours)} matches {args.id!r}; "
                  f"ids are response ids, session ids or run ids", file=sys.stderr)
            return 2
    elif getattr(args, "blocked", False):
        picked = explain.blocked(reqs, limit)
        if not picked:
            print(f"luna-doctor: no request in the last {human_window(hours)} was refused or "
                  f"stalled, so there is no blocked request to explain", file=sys.stderr)
            return 0
    elif getattr(args, "slowest", False):
        picked = explain.slowest(reqs, limit)
    else:
        # No selector: the request that went wrong, not the longest that happened
        # to be served. Duration is only the fallback when nothing went wrong.
        picked = explain.worst(reqs, limit)

    # The account half and the width resolution, the same way analyze does it:
    # a cache read, never a fetch, and unknown is a fine answer.
    m = analyse(reqs, hours)
    m["declared_plan"] = getattr(args, "plan", None)
    try:
        m["capacity"] = _capacity_metrics(now - hours * 3600, now, m)
    except Exception:
        m["capacity"] = {"provenance": sources.PROV_UNKNOWN}
    capm = m.get("capacity") or {}
    served = capm if capm.get("provenance") == sources.PROV_ACCOUNT else None
    priority = _priority_for_advice(m)

    docs = []
    any_room = False
    for req in picked:
        doc = explain.explain(req, reqs, served, priority)
        any_room = any_room or doc["room"]
        docs.append({
            "headline": doc["headline"],
            "rows": [{"label": k, "value": v} for k, v in doc["rows"]],
            "actions": doc["actions"],
            "outcome": doc["outcome"],
        })

    if args.json:
        print(json.dumps(envelope("explain", {
            "headline": docs[0]["headline"],
            "window_hours": hours,
            "requests": docs,
            "priority_concurrency": priority[0],
            "priority_concurrency_source": priority[1],
            "capacity": capm,
        }, contract=CONTRACT), indent=2))
        return 1 if any_room else 0

    for i, (req, doc) in enumerate(zip(picked, docs)):
        if i:
            print()
        rep = report.Report(
            headline=doc["headline"],
            sections=[report.Section("request", [report.Row(r["label"], r["value"])
                                                 for r in doc["rows"]])],
            adjustments=list(doc["actions"]),
            footnote="one request, from this machine's logs · `luna-doctor analyze` for the window")
        print_report(rep, args)
    return 1 if any_room else 0


def cmd_analyze(args) -> int:
    try:
        hours = window_hours(args.since, args.now)
    except ValueError as e:
        print(f"luna-doctor: {e}", file=sys.stderr)
        return 2
    harnesses = [h.strip() for h in args.harness.split(",") if h.strip()]
    raw_pi = getattr(args, "pi_root", None)
    raw_claude = getattr(args, "claude_root", None)
    # A flag that validated nothing must not fall back to the local store: the
    # user asked for a mirror, and reading $HOME instead would answer a question
    # they did not ask.
    pi_roots = _check_roots(raw_pi, "--pi-root") if raw_pi else None
    claude_roots = _check_roots(raw_claude, "--claude-root") if raw_claude else None
    now = time.time()
    reqs = collect(hours, harnesses, pi_roots, claude_roots, now=now)
    m = analyse(reqs, hours)
    # A declared plan is the fallback for the account's own number, never an
    # override of it: attribution.resolve_priority_concurrency decides which
    # one leads, and the report says which it used.
    m["declared_plan"] = getattr(args, "plan", None)
    # swamp enrichment (additive, never fails; installed is not in use, so
    # only a swamp with activity in the window contributes anything)
    try:
        sw_state = swamp_state(since=now - hours * 3600)
        if sw_state["in_use"]:
            m["swamp"] = _collect_swamp_metrics(sw_state["repo"], now - hours * 3600, now)
            m["_swamp_configured_concurrency"] = m["swamp"].get("configuredConcurrency")
    except Exception:
        pass
    # Combined snapshot (additive: metrics.snapshot joins the measured
    # requests, the declared configs and the catalog; analyse()'s own
    # numbers above are unchanged). Offline by default: a cached catalog
    # serves, the network is touched only with --online, and a failure
    # anywhere in here degrades to unknown, never to an error.
    snap = None
    try:
        snap = sources.build_snapshot(reqs, since=now - hours * 3600, until=now,
                                      allow_network=bool(getattr(args, "online", False)))
        m["snapshot"] = snap.to_dict()
    except Exception as e:
        m["snapshot"] = {"provenance": sources.PROV_UNKNOWN,
                         "reason": f"collection failed: {type(e).__name__}: {e}"[:200]}
    # The account's capacity block (additive; schema stays 4): plan and
    # concurrency from the disk cache, plus the server's admission counts
    # for the window's complete UTC days next to the local outcome counts.
    # Cache-only by construction - the 4-17s endpoint is never on this
    # path - and a failure degrades to unknown, never to an error.
    try:
        m["capacity"] = _capacity_metrics(now - hours * 3600, now, m)
    except Exception as e:
        m["capacity"] = {"provenance": sources.PROV_UNKNOWN,
                         "reason": f"collection failed: {type(e).__name__}: {e}"[:200]}
    # The pressure-window count is overlap-based and needs a width. analyse()
    # computes it with the fallback constant before the account is known, so a
    # resolved width re-counts the windows here - otherwise the report would
    # quote one width in prose and measure "wide" against another.
    pv, _pvsrc = _priority_for_advice(m)
    if isinstance(pv, int) and not isinstance(pv, bool) and pv > 0 \
            and pv != ADVISED_CONCURRENCY and m.get("requests"):
        try:
            wide = pressure_windows(timed(reqs), pv)
            m["pressure_windows"] = len(wide)
            m["longest_pressure_window"] = (max(wide, key=lambda w: w["seconds"])
                                            if wide else None)
            m["pressure_window_threshold"] = pv
        except Exception:
            pass
    if bool(getattr(args, "online", False)) and (snap is None or snap.catalog is None):
        note = snap.catalog_note if snap is not None else m["snapshot"].get("reason", "")
        print(f"luna-doctor: catalog unavailable ({note}); "
              f"model limits are reported unknown", file=sys.stderr)
    # Sizing rules (additive: metrics.sizing is the misconfiguration family's
    # ledger - declared/effective/catalog sizes per routed model, with the
    # V/U discipline: only V-backed comparisons raise findings, U-backed
    # lines are informational, an unreadable config or a stale/missing
    # catalog is unchecked, never clean). Config findings are checked-now,
    # not as-of-window; they never touch the verdict or the exit code. A
    # sizing block that cannot be computed is itself unchecked, never an
    # error.
    try:
        m["sizing"] = sizing.evaluate(snap)
    except Exception as e:
        m["sizing"] = {"provenance": sources.PROV_UNKNOWN,
                       "reason": (f"sizing evaluation failed: "
                                  f"{type(e).__name__}: {e}")[:200]}
    head, adj, code = verdict(m)
    # The attribution verdict: was the wall this machine, the account, or the
    # gateway. It is computed from the same measured window plus the account's
    # cached capacity, and it never decides the exit code - the verdict above
    # does, exactly as before.
    att = None
    try:
        capm = m.get("capacity") or {}
        served = capm if capm.get("provenance") == sources.PROV_ACCOUNT else None
        att = attribution.attribute(m, served, _priority_for_advice(m))
    except Exception as e:
        print(f"luna-doctor: attribution unavailable: {type(e).__name__}: {e}", file=sys.stderr)
    # The verification stage: the same analysis over the equal-length window
    # immediately before this one. Cache and network are untouched - both
    # windows come out of the same log stores - and a verdict below the
    # sample floor is reported as inconclusive rather than as a win.
    change = None
    if getattr(args, "baseline", None):
        try:
            p0, p1 = baseline.previous_window(now, hours)
            prev_reqs = collect(hours, harnesses, pi_roots, claude_roots, now=p1)
            prev = analyse(prev_reqs, hours)
            change = {"rows": baseline.change_rows(m, prev),
                      "word": baseline.change_verdict(m, prev)}
        except Exception as e:
            print(f"luna-doctor: baseline unavailable: {type(e).__name__}: {e}", file=sys.stderr)
    if change is not None:
        rows = " · ".join(f"{label}: {value}" for label, value in change["rows"]
                         if label != "change")
        adj.append(f"change, against the window before this one: {rows}.")
        word, reason = change["word"]
        adj.append(f"change verdict: {word} - {reason}")
    # `--for-issue`: the same evidence as one redacted, datable block. It goes
    # out instead of the report, never beside it, so a paste carries one thing.
    if getattr(args, "for_issue", False) and not args.json:
        rep = build_report(m, head, adj, att=att)
        # The verdict already opens the block, so its own rows would repeat it.
        skip = ("the wall you hit", "why")
        rows = []
        for section in rep.sections:
            for row in section.rows:
                if row.label in skip:
                    continue
                rows.append((row.label, row.value))
        for line in rep.adjustments[:3]:
            rows.append(("next", line))
        provenance = {
            "version": VERSION,
            "schema": SCHEMA_VERSION,
            "window_start": datetime.fromtimestamp(now - hours * 3600, timezone.utc)
                                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window_end": datetime.fromtimestamp(now, timezone.utc)
                                  .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "stores": [_source_id(harnesses, pi_roots, claude_roots)],
        }
        verdict_pair = (att.verdict, att.headline) if att is not None else None
        print(bundle_mod.issue_block(head, rows, provenance, verdict_pair), end="")
        return code

    stored = None
    if m["requests"] and not getattr(args, "no_store", False):
        try:
            stored = persist_run(m, hours, getattr(args, "store_dir", None),
                                 since=now - hours * 3600, until=now,
                                 sources=_source_id(harnesses, pi_roots, claude_roots))
        except (OSError, ValueError, OverflowError) as e:
            # A read-only or unwritable store must not lose the report.
            print(f"luna-doctor: could not persist run: {e}", file=sys.stderr)
    render(m, head, adj, args.json, stored=stored, args=args, att=att)
    return code


# The `fix` command lives in a sibling file so its snippets can be reviewed and
# verified on their own. It is executed in this module's namespace so it can
# reuse Request/collect/analyse without a package import. It ships beside this
# file in the installed package, so the lookup works from a checkout and from
# site-packages alike.
_HERE = os.path.dirname(os.path.abspath(__file__))

_FIX_PATH = os.path.join(_HERE, "_fix_block.py")
if os.path.isfile(_FIX_PATH):
    with open(_FIX_PATH, "r") as _fh:
        exec(compile(_fh.read(), _FIX_PATH, "exec"), globals())
else:  # pragma: no cover
    def cmd_fix(args):  # type: ignore
        print(f"luna-doctor: fix support missing ({_FIX_PATH} not found)", file=sys.stderr)
        return 2


_CHART_PATH = os.path.join(_HERE, "_chart_block.py")
if os.path.isfile(_CHART_PATH):
    with open(_CHART_PATH, "r") as _fh:
        exec(compile(_fh.read(), _CHART_PATH, "exec"), globals())
else:  # pragma: no cover
    def cmd_chart(args):  # type: ignore
        print(f"luna-doctor: chart support missing ({_CHART_PATH} not found)", file=sys.stderr)
        return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
