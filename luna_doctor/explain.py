"""One request, explained: the answer to "why was that one slow".

The window report answers "what has this machine been doing". A user with one
bad request needs a different answer. This module explains that request: what
it was, how long it waited, what it got, and whose room it was waiting for.

Every function here is pure. Input is the request, the other requests in the
window, and the account capacity block. There is no file access, no clock, and
no network. The caller collects the requests and renders the result through the
same Report model that every command uses. So `--style plain`, `--json`, and
`--copy` work without extra code.

Three rules hold throughout.

Every number carries its owner. `(this machine)` marks a value measured in the
logs. `(account)` marks a value the capacity API reported. `(declared plan
<name>)` marks a value the user asserted. This module never adds values from
two owners. A value that was not measured renders as `unknown`, never as 0.

This module names a cause only when the logs support it. The requests in the
window are evidence. The state of the gateway at that moment is not evidence,
unless the capacity payload covers that moment.

This module never claims that a change caused a result. It describes the
timeline of one request.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# What an outcome means, in the words a reader needs. This copy never says
# "died" and never says "killed by the gateway". The gateway blocks a request at
# admission. The harness stops an idle stream. The two failures stay apart.
OUTCOME_COPY = {
    "completed": "served",
    "admission_timeout": "blocked at admission. It waited for room and the gateway refused it",
    "client_idle_timeout": ("stalled until the harness idle budget stopped the stream. "
                            "This is the silent overflow signature"),
    "org_concurrency_limit": ("refused with CONCURRENT_REQUEST_LIMIT_EXCEEDED. The whole "
                             "concurrent-request pool of the account was in use"),
    "rate_limited": "refused with a 429. The account reached its concurrent-request capacity",
    "upstream_unavailable": "the upstream service behind the gateway was unavailable",
    "upstream_error": "the upstream service returned an error",
    "model_not_found": "the model id does not exist on this account",
    "context_length_exceeded": "the request exceeded the context window of the model",
    "payload_too_large": "the request was too large (413)",
    "message_limit": "the account reached its message-count limit",
    "harness_deadline": "the harness deadline expired",
    "auth_error": "authentication failed",
    "aborted": "the caller cancelled the request",
    "connection_error": "the connection failed",
    "request_timeout": "the client request timeout expired",
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
    """Count the requests to the same model that were served inside this span.

    This is the starved-peer test for one request. If the same model answered
    another request while this one waited, then the model was serving. This
    request was not served. That result separates "the model was slow" from "we
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


def _sentence(text: str) -> str:
    """Capitalize the first letter and keep one sentence, for a clause in copy."""
    text = text.strip()
    if not text:
        return text
    return text[0].upper() + text[1:]


def _session_label(req) -> str:
    bits = [req.harness or "unknown harness"]
    if req.session:
        bits.append("session %s" % req.session)
    if req.run:
        bits.append("run %s" % req.run)
    if req.provider:
        bits.append(req.provider)
    return " · ".join(bits)


def _account_full(capacity) -> bool:
    """True when the account sample shows the concurrent-request pool full.

    A cached sample that reports 1 of 12 in use cannot explain a refusal, so it
    is data in the rows and never a cause in the headline.
    """
    now = (capacity or {}).get("now") or {}
    if now.get("saturated") is True:
        return True
    in_use, limit = now.get("in_use"), now.get("limit")
    if isinstance(in_use, int) and isinstance(limit, int):
        return in_use >= limit
    return False


def _why(req, reqs, capacity, priority) -> tuple[str, list]:
    """The one sentence the reader came for, plus the actions that follow it.

    The order is the attribution order. Was this machine wide? Was the account
    full? Did the request fail on its own terms? If a refusal has no local width
    and no account evidence, this function says so. It does not guess.
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
            head = ("Served, but slowly. It ran for %s while this machine held %d concurrent "
                    "requests, above %s. The wait came from the concurrent requests on this "
                    "machine, not from the latency of the model."
                    % (_seconds(req.duration), peak, _width_words(value, source)))
            actions.append("Narrow the concurrent requests on this machine to fit %s. If you do, "
                           "this request will not queue behind the others."
                           % _width_words(value, source))
        elif peak is not None and peak > 1:
            head = ("Served in %s while up to %d concurrent requests were open from this machine. "
                    "A duration alone does not show a fault. Compare it with the p50 in "
                    "`luna-doctor analyze` before you call it slow."
                    % (_seconds(req.duration), peak))
        else:
            head = ("Served in %s on a machine with almost no other load. The duration is the "
                    "latency of the model itself. Compare it with the p50 in "
                    "`luna-doctor analyze`." % _seconds(req.duration))
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
            held = ("this machine held %d concurrent requests"
                    % in_flight if in_flight else
                    "the number of concurrent requests on this machine at that moment is unknown")
            head = ("The concurrent-request pool of the account was full at %s. %s. At that "
                    "moment, %s. The room that this request needed was not free in the pool of "
                    "the account." % (_clock(req.start), _sentence(named), held))
            actions.append("Other sessions on the account held the room. Run `luna-doctor "
                           "capacity` to read the occupancy of the account. Send wide work to "
                           "the -background variant.")
            if peers:
                head += (" %d request(s) to %s were served inside this request's span, so the "
                         "model itself was answering." % (peers, req.model or "the same model"))
            return head, actions
        if isinstance(value, int) and peak is not None and peak >= value:
            head = ("This machine held %d concurrent requests at %s. The width is %s. The wait "
                    "came from the concurrent requests on this machine, because this request "
                    "queued behind them."
                    % (peak, _clock(req.start), _width_words(value, source)))
            actions.append("Narrow the concurrent requests on this machine to fit %s. Or send "
                           "the wide work to the -background variant of the same model."
                           % _width_words(value, source))
        elif _account_full(capacity):
            # The account sample has to show the pool full. A sample that says
            # 1 of 12 in use does not explain a refusal, and saying that it did
            # would be the same mistake the attribution module refuses to make.
            now = capacity["now"]
            head = ("This machine held only %s concurrent requests at %s. The account was "
                    "holding %s of %s at that sample, so the pool was full and the wait came "
                    "from outside this machine."
                    % (peak if peak is not None else "unknown", _clock(req.start),
                       now.get("in_use"), now.get("limit")))
            actions.append("Other machines or other agents held the room. Run `luna-doctor "
                           "capacity` and send this work again when in_use is lower.")
        else:
            now = (capacity or {}).get("now") or {}
            sample = ""
            if now.get("in_use") is not None and now.get("limit") is not None:
                sample = (" The account sample shows %s of %s in use, which is not full."
                          % (now.get("in_use"), now.get("limit")))
            head = ("No width on this machine explains this request. This machine peaked at %s "
                    "during the span.%s The cause of this wait is not established."
                    % (peak if peak is not None else "unknown", sample))
            actions.append("Run `luna-doctor capacity --online` to read the occupancy of the "
                           "account. Without that value, this tool cannot name the owner of the "
                           "wait.")
        if peers:
            head += (" %d request(s) to %s were served inside the span of this request. The "
                     "model was answering, but this request was not served."
                     % (peers, req.model or "the same model"))
            actions.append("Treat the model as healthy here. It answered another request while "
                           "this one waited.")
        return head, actions

    if own in ("upstream_unavailable", "upstream_error", "connection_error", "request_timeout"):
        head = ("The gateway or the transport failed this request. It did not queue the "
                "request. Reason: %s." % OUTCOME_COPY.get(own, own))
        actions.append("Retry once. If the failure repeats on more than one model, run "
                       "`luna-doctor capacity` and read its ledger row.")
        return head, actions

    if own in ("model_not_found", "context_length_exceeded", "payload_too_large",
               "message_limit", "auth_error", "harness_deadline", "aborted"):
        head = "%s." % OUTCOME_COPY.get(own, own).rstrip(".")
        if own == "model_not_found":
            actions.append("Refresh the model list. `luna-doctor fix` reads the catalog. Then "
                           "correct the id in the harness configuration file.")
        elif own == "context_length_exceeded":
            actions.append("Make sure that maxTokens fits the contextWindow of this model. The "
                           "input and the output share one window.")
        elif own == "harness_deadline":
            actions.append("Raise the request deadline of the harness, or make sure that the "
                           "client timeout in front of this call is long enough.")
        return head, actions

    head = ("This request ended as `%s`. The logs carry no other cause." % own)
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
    """The requests that went wrong, longest wait first.

    This is the default for `explain` with no selector. A user who asks "why is
    my agent slow" means the request that was refused or that stalled. The
    longest request that completed is not the same thing. If nothing was refused
    or stalled, this function returns the slowest requests instead, because then
    duration is the only evidence in the window.
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

    The return value holds a headline, rows, actions, the outcome, and a flag
    that says whether the request was refused or stalled. The rows are
    `(label, value)` pairs in reading order. They give what ran, when it ran,
    how long it took, what it got, and the state of the window at that moment.
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
