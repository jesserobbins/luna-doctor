"""One request, explained - the 'why was *that* one slow' view.

The aggregate report answers "what has this machine been doing". This module
answers the question a user actually asks in the middle of it, holding one bad
request in their hand: what happened to *this* one, at *this* moment, and whose
fault was the wait.

Everything here is a pure function of (the request, the other requests in the
window, the account's capacity block). No I/O, no clock, no network: the caller
collects, and the caller renders through the same Report model every other
command uses, so `--style plain`, `--json` and `--copy` keep working for free.

The discipline is the same one the rest of the tool holds itself to:

* Every number carries its owner - `(this machine)` for something measured in
  these logs, `(account)` for something the capacity API reported, `(declared
  plan <name>)` for something the user asserted. Nothing is summed across
  owners, and an unmeasured value renders as `unknown`, never as 0.
* A cause is only named when the logs support it. The requests in this window
  are evidence; the gateway's state at the time is not, unless the capacity
  payload happens to cover that minute.
* Nothing here implies causation about *why the user changed something*; it
  describes one request's own timeline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# What an outcome means, in the words a reader needs. Never "died", never
# "killed by the gateway" - the gateway blocks at admission, the harness kills
# idle streams, and the copy has to keep those apart.
OUTCOME_COPY = {
    "completed": "served",
    "admission_timeout": "blocked at admission - it waited for room and the gateway refused it",
    "client_idle_timeout": ("stalled until the harness's own idle budget killed the stream - "
                            "the silent-overflow signature"),
    "org_concurrency_limit": ("refused with CONCURRENT_REQUEST_LIMIT_EXCEEDED - the account's "
                             "whole concurrent-request pool was in use"),
    "rate_limited": "refused with a 429 - the account's concurrent-request capacity was full",
    "upstream_unavailable": "the upstream behind the gateway was unavailable",
    "upstream_error": "the upstream returned an error",
    "model_not_found": "the model id does not exist on this account",
    "context_length_exceeded": "the request exceeded the model's context window",
    "payload_too_large": "the request was too large (413)",
    "message_limit": "the account's message-count limit was reached",
    "harness_deadline": "the harness's own deadline expired",
    "auth_error": "authentication failed",
    "aborted": "the caller cancelled it",
    "connection_error": "the connection failed",
    "request_timeout": "the client's request timeout expired",
}

# Outcomes where the wait was about room at the gateway, not about the work.
_ROOM = ("admission_timeout", "client_idle_timeout", "org_concurrency_limit", "rate_limited")
# Refused at the gateway, as opposed to stalled by the harness: the two failures
# a user conflates, and the two the copy keeps apart.
_REFUSED = ("admission_timeout", "org_concurrency_limit", "rate_limited")


def _iso(ts: Optional[float]) -> str:
    if ts is None:
        return "unknown"
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clock(ts: Optional[float]) -> str:
    if ts is None:
        return "unknown"
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%H:%M:%SZ")


def _seconds(value) -> str:
    if value is None:
        return "unknown"
    return "%.1fs" % float(value)


def _in_flight_phrase(n) -> str:
    if n is None:
        return "an unknown number of requests were"
    if n == 0:
        return "no request was"
    if n == 1:
        return "1 request was"
    return "%d requests were" % n


def _in_flight_at(reqs, moment: float) -> int:
    """How many requests were open at `moment`, inclusive of the one we ask about."""
    n = 0
    for r in reqs:
        if r.start is None or r.end is None:
            continue
        if r.start <= moment < r.end:
            n += 1
    return n


def _peak_during(reqs, start: float, end: float) -> int:
    """Peak overlap inside [start, end]: measured, not inferred from a start burst."""
    points = sorted({r.start for r in reqs
                     if r.start is not None and start <= r.start <= end})
    points.append(start)
    return max(_in_flight_at(reqs, p) for p in points) if points else 0


def _peers_served_during(reqs, req) -> int:
    """Requests to the same model that were *served* inside this request's own span.

    The starved-peer test, one request at a time: if the same model answered
    somebody else while this one waited, the model was serving and this request
    was not served. That is the difference between "the model was slow" and "we
    were at our width".
    """
    if req.start is None or req.end is None:
        return 0
    n = 0
    for other in reqs:
        if other is req or other.model != req.model:
            continue
        if other.outcome != "completed":
            continue
        if other.start is None or other.end is None:
            continue
        if req.start <= other.start and other.end <= req.end:
            n += 1
    return n


def _session_label(req) -> str:
    bits = [req.harness or "unknown harness"]
    if req.session:
        bits.append("session %s" % req.session)
    if req.run:
        bits.append("run %s" % req.run)
    if req.provider:
        bits.append(req.provider)
    return " · ".join(bits)


def _why(req, reqs, capacity, priority) -> tuple[str, list]:
    """The one sentence a reader came for, plus the actions that follow from it.

    The order is the attribution order: was this machine wide, was the account
    full, or did the request simply fail on its own terms. A refusal with no
    local width and no account evidence is described as exactly that, rather
    than guessed at.
    """
    value, source = priority if priority else (None, "unknown")
    peak = _peak_during(reqs, req.start, req.end) if req.start and req.end else None
    in_flight = _in_flight_at(reqs, req.start) if req.start is not None else None
    peers = _peers_served_during(reqs, req)
    own = req.outcome
    actions = []

    if own == "completed":
        # Served, but the question was "why was this slow". Duration only means
        # something beside the width it ran at: a 145s answer on a quiet machine
        # is the model, and the same answer inside a 12-wide burst is a queue.
        if isinstance(value, int) and peak is not None and peak > value:
            head = ("Served, but not quickly: it ran for %s while this machine held %d concurrent "
                    "requests, past %s. The wait was queueing behind this machine's own fan-out, "
                    "not the model taking that long."
                    % (_seconds(req.duration), peak, _width_words(value, source)))
            actions.append("Narrow the fan-out to fit %s and this request's share of the queue "
                           "goes away." % _width_words(value, source))
        elif peak is not None and peak > 1:
            head = ("Served in %s while up to %d concurrent requests were open from this machine. "
                    "Duration on its own says nothing: compare it with the p50 in "
                    "`luna-doctor analyze` before calling it slow."
                    % (_seconds(req.duration), peak))
        else:
            head = ("Served in %s on a machine that was barely busy, so the shape of this run was "
                    "the model's own latency - compare it with the p50 in `luna-doctor analyze`."
                    % _seconds(req.duration))
        if peers:
            head += (" %d request(s) to %s finished inside this one's span." % (peers, req.model))
        return head, actions

    if own in _ROOM:
        if own in ("org_concurrency_limit", "rate_limited"):
            # The gateway named the reason in the response itself: this is
            # account-level evidence, whether or not a capacity payload covers
            # the moment. Saying less than that would be hiding the answer the
            # user already holds in their hand.
            named = ("the gateway said so in the response (CONCURRENT_REQUEST_LIMIT_EXCEEDED)"
                     if own == "org_concurrency_limit" else "the gateway returned a 429")
            held = ("this machine held %d concurrent requests at that moment"
                    % in_flight if in_flight else
                    "this machine's own in-flight count at that moment is unknown")
            head = ("The account's concurrent-request pool was full at %s - %s, and %s. The room "
                    "this request needed was not this machine's to give."
                    % (_clock(req.start), named, held))
            actions.append("The room was not this machine's to give: other sessions on the "
                           "account held it. Check `luna-doctor capacity` for the account's "
                           "occupancy, and move wide work to the -background variant.")
            if peers:
                head += (" %d request(s) to %s were served inside this request's span, so the "
                         "model itself was answering." % (peers, req.model or "the same model"))
            return head, actions
        if isinstance(value, int) and peak is not None and peak >= value:
            head = ("This machine was %d wide at %s against %s, so the wait was our own "
                    "concurrency: the request was queued behind this machine's fan-out."
                    % (peak, _clock(req.start), _width_words(value, source)))
            actions.append("Narrow this machine's fan-out to fit %s, or move the wide part "
                           "to the -background variant of the same model."
                           % _width_words(value, source))
        elif capacity and (capacity.get("now") or {}).get("in_use") is not None and (capacity.get("now") or {}).get("limit") is not None:
            now = capacity["now"]
            head = ("This machine was only %s wide at %s, but the account was holding %s of %s "
                    "concurrent requests - the pool is shared, so the wait came from outside "
                    "this machine."
                    % (peak if peak is not None else "unknown", _clock(req.start),
                       now.get("in_use"), now.get("limit")))
            actions.append("The room this request needed was held elsewhere on the account: "
                           "check other machines and other agents, or send this work when "
                           "`luna-doctor capacity` reports lower in_use.")
        else:
            head = ("No local width explains this request: this machine peaked at %s during its "
                    "span, and the account's own occupancy was not captured for that moment. "
                    "Run `luna-doctor capacity --online` to see the account's half."
                    % (peak if peak is not None else "unknown"))
            actions.append("Fetch the account's occupancy with `luna-doctor capacity --online`; "
                           "without it the owner of the wait cannot be named.")
        if peers:
            head += (" %d request(s) to %s were served inside this request's own span, so the "
                     "model was answering - this request was not served."
                     % (peers, req.model or "the same model"))
            actions.append("Treat the model as healthy here: the same model answered somebody "
                           "else while this one waited.")
        return head, actions

    if own in ("upstream_unavailable", "upstream_error", "connection_error", "request_timeout"):
        head = ("The gateway or the transport failed this request rather than queueing it: "
                "%s." % OUTCOME_COPY.get(own, own))
        actions.append("Retry once. A failure that repeats across models is worth "
                       "`luna-doctor capacity` - its ledger row reports the account side.")
        return head, actions

    if own in ("model_not_found", "context_length_exceeded", "payload_too_large",
               "message_limit", "auth_error", "harness_deadline", "aborted"):
        head = "%s." % OUTCOME_COPY.get(own, own).rstrip(".")
        if own == "model_not_found":
            actions.append("Refresh the model list (`luna-doctor fix` reads the catalog) and "
                           "correct the id in the harness config.")
        elif own == "context_length_exceeded":
            actions.append("Check maxTokens against contextWindow for this model: input and "
                           "output share one window.")
        elif own == "harness_deadline":
            actions.append("Raise the harness's own request deadline, or check the client timeout "
                           "in front of this call.")
        return head, actions

    head = "This request ended as `%s`; the logs do not carry a cause beyond that." % own
    return head, actions


def _width_words(value: int, source: str) -> str:
    if source == "account":
        return "%d priority concurrent requests (account)" % value
    if source.startswith("plan "):
        return "%d priority concurrent requests (declared plan %s)" % (value, source[5:])
    if source == "declared":
        return "%d priority concurrent requests (declared)" % value
    return "%d priority concurrent requests (declared)" % value


# ---- selection --------------------------------------------------------------
def _duration(req) -> float:
    return req.duration if req.duration is not None else -1.0


def slowest(reqs, limit: int = 1) -> list:
    """The longest-lived requests in the window, longest first."""
    ranked = sorted(reqs, key=lambda r: (_duration(r), r.end or 0.0), reverse=True)
    return ranked[:max(1, int(limit or 1))]


def worst(reqs, limit: int = 1) -> list:
    """The requests that went wrong first, longest-waiting first.

    This is the default for `explain` with no selector: a user asking "why is my
    agent slow" means the request that was refused or stalled, not necessarily
    the longest one that happened to complete. When nothing was refused or
    stalled, it falls back to the slowest requests, because then duration is the
    only thing there is.
    """
    hits = [r for r in reqs if r.outcome in _ROOM]
    if not hits:
        return slowest(reqs, limit)
    hits.sort(key=lambda r: (_duration(r), r.end or 0.0), reverse=True)
    return hits[:max(1, int(limit or 1))]


def blocked(reqs, limit: int = 1) -> list:
    """The most recent requests that were refused or that stalled, newest first.

    A refusal, not a stall: `--blocked` is the request the gateway turned away
    (blocked at admission, or refused at the account's limit), newest first.
    A stalled request is the default `worst()` view, because "blocked" and
    "stalled" are different failures with different fixes and the copy must not
    blur them.
    """
    hits = [r for r in reqs if r.outcome in _REFUSED]
    hits.sort(key=lambda r: (r.end or r.start or 0.0), reverse=True)
    return hits[:max(1, int(limit or 1))]


def by_id(reqs, needle: str) -> list:
    """Match a response id, session id, or run id - exact first, then prefix.

    Ids differ per harness, so this is deliberately forgiving: an exact hit
    wins, otherwise a unique prefix, otherwise nothing. It never guesses between
    two candidates.
    """
    needle = (needle or "").strip()
    if not needle:
        return []
    for attr in ("response_id", "session", "run"):
        exact = [r for r in reqs if getattr(r, attr, None) == needle]
        if exact:
            return exact
    prefixed = [r for r in reqs
                if any(str(getattr(r, attr, "") or "").startswith(needle)
                       for attr in ("response_id", "session", "run"))]
    # A prefix that matches two requests is not an identification: returning the
    # first one would be a guess presented as an answer.
    return prefixed if len(prefixed) == 1 else []


# ---- the report -------------------------------------------------------------
def explain(req, reqs, capacity=None, priority=None) -> dict:
    """One request as rows, a reasoned sentence, and the actions it implies.

    Returns {"headline", "rows", "actions", "outcome", "room": bool}. The rows
    are `(label, value)` pairs in reading order: what ran, when, for how long,
    what it got, and what the window looked like at that moment.
    """
    reqs = list(reqs or [])
    duration = req.duration
    start_iso, end_iso = _iso(req.start), _iso(req.end)
    peak = _peak_during(reqs, req.start, req.end) if req.start is not None and req.end is not None else None
    in_flight = _in_flight_at(reqs, req.start) if req.start is not None else None
    peers = _peers_served_during(reqs, req)

    rows = [
        ("request", _session_label(req)),
        ("model", req.model or "unknown"),
        ("tier", "%s (inferred from the model name - these logs do not carry the served tier)"
         % (req.tier or "unknown")),
        ("when", "unknown" if start_iso == end_iso == "unknown"
         else "%s → %s" % (start_iso, end_iso)),
        ("duration", _seconds(duration)),
        ("outcome", "%s - %s" % (req.outcome, OUTCOME_COPY.get(req.outcome, "no known class"))),
    ]
    if req.stop_reason:
        rows.append(("stop reason", str(req.stop_reason)))
    if req.thinking_chars or req.text_chars:
        rows.append(("produced", "%d character(s) of thinking, %d of answer"
                     % (req.thinking_chars, req.text_chars)))
    if req.response_id:
        rows.append(("response id", str(req.response_id)))
    if in_flight is not None:
        rows.append(("in flight when it started", "%d concurrent request(s) (this machine)" % in_flight))
    if peak is not None:
        rows.append(("peak during its span", "%d concurrent request(s) (this machine)" % peak))
    if peers:
        rows.append(("same model, served meanwhile",
                     "%d request(s) completed inside this request's span (this machine)" % peers))
    now = (capacity or {}).get("now") or {}
    if now.get("in_use") is not None:
        rows.append(("account occupancy", "%s of %s in use at %s (account)"
                     % (now.get("in_use"), now.get("limit"), now.get("sampled_at") or "an unknown time")))
    if req.error_message:
        text = " ".join(str(req.error_message).split())
        rows.append(("error text", text[:220] + ("…" if len(text) > 220 else "")))

    headline, actions = _why(req, reqs, capacity, priority)
    return {
        "headline": headline,
        "rows": rows,
        "actions": actions,
        "outcome": req.outcome,
        "room": req.outcome in _ROOM,
    }
