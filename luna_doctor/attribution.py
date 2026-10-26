#!/usr/bin/env python3
"""Attribution: whose concurrency a refusal belongs to.

LunaRoute bills *concurrent sessions*, and the pool those sessions draw on is
shared at the organization level: every machine signed in to the same account
contends for one priority concurrency. That is why a machine can look idle -
three agents, nothing else running - and still collect a 429, and why a 429
with two jobs in flight is not a contradiction. The local picture and the
account picture are two halves of one fact, and neither half alone can name
the owner of a refusal:

  local      this machine's peak concurrency, and how many of its requests
             were refused at admission, rate limited, stalled, or failed
             upstream. Reconstructed from the log store, so it is a lower
             bound on what the machine held: a request nobody logged is
             invisible here.
  account    GET /v1/capacity's plan, priority concurrency and live
             occupancy, served from its disk cache by the `capacity` command.

`attribute()` places one half beside the other and names the owner. Three
rules it never breaks:

- It never adds the halves. A local peak is a lower bound on the account's
  occupancy, not a term in a sum, and a local measurement added to a
  configured width is a number nobody measured.
- It never states a capacity number as a gateway fact. Account numbers are
  labelled (account), plan-derived ones (declared plan <name>), and a number
  the tool does not have is unknown - never zero, never a default.
- It never guesses. With no account half and no declared width the verdict
  is unknown and points at the command that fetches the missing half.

Nothing here reads a file, opens a socket or runs a command: these are pure
functions over the dicts `cli.analyse()` and the capacity command already
produce.
"""

from __future__ import annotations

from typing import NamedTuple, Optional

from luna_doctor.plan import PLAN_PRIORITY, parse_plan

# The five verdicts. "unknown" is a first-class answer, not a failure: the
# tool says which half is missing rather than borrowing a number.
VERDICT_YOU = "you"
VERDICT_YOUR_ORG = "your-org"
VERDICT_THE_GATEWAY = "the-gateway"
VERDICT_NOT_CONCURRENCY = "not-concurrency"
VERDICT_UNKNOWN = "unknown"
VERDICTS = (VERDICT_YOU, VERDICT_YOUR_ORG, VERDICT_THE_GATEWAY,
            VERDICT_NOT_CONCURRENCY, VERDICT_UNKNOWN)

# The one command that fetches the account half. It is named in copy
# wherever a verdict is missing that half, so it lives in one place.
CAPACITY_COMMAND = "luna-doctor capacity --online"


# ---- the resolved width -----------------------------------------------------
def _int(value) -> Optional[int]:
    """An int, or None. A bool is not a count and a float is not a count."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def resolve_priority_concurrency(capacity: Optional[dict],
                                plan: Optional[str]) -> tuple:
    """Resolve the priority concurrency a verdict is judged against.

    Two sources, in this order, and no third:

    - the account: `capacity["priority_lanes"]` when it is a real integer.
      That is the number GET /v1/capacity reports for this account and it is
      the first authority on any account fact. A null, a float, a bool or an
      absent key is unknown, never zero.
    - the declaration: `plan`, exactly as the user typed it on --plan. A
      plan name maps to LunaRoute's published default (plan.PLAN_PRIORITY,
      one table, so the names cannot drift); a plain number is the width the
      user states. A declaration is consulted only when the account has no
      number, and it is never a measurement: it is labelled as a declaration
      wherever it renders.

    Returns (value, source) with `source` exactly one of "account",
    "plan <name>", "declared" or "unknown". `value` is None only for
    "unknown": unknown is never expressed as zero or as any other real
    number, so no caller can mistake "not established" for a width.

    An unusable declaration - a name not in the published table, a number
    below 1, a non-integer - resolves to (None, "unknown") rather than
    raising: this function has no error channel, and a width nobody can name
    is not a width to judge a refusal against. The CLI validates --plan when
    it parses the flag, so a bad declaration is reported there.
    """
    if isinstance(capacity, dict):
        lanes = _int(capacity.get("priority_lanes"))
        if lanes is not None:
            return lanes, "account"
    declared = str(plan).strip() if plan is not None else ""
    if not declared:
        return None, "unknown"
    try:
        value = parse_plan(declared)
    except ValueError:
        return None, "unknown"
    name = declared.lower()
    if name in PLAN_PRIORITY:
        return value, "plan %s" % name
    return value, "declared"


class Attribution(NamedTuple):
    """One attribution: the owner, one plain sentence, and the working.

    `rows` are (label, value) pairs, each value naming whose number it is -
    "this machine" or "account" - so a reader can tell a measurement from a
    declaration without reading this module. `actions` is what to do about
    it, and may be empty."""

    verdict: str
    headline: str
    rows: list
    actions: list


# ---- this machine's half ----------------------------------------------------
class _LocalFacts(NamedTuple):
    """analyse()'s numbers, reduced to what an attribution needs."""

    requests: int
    peak: int
    window: str                    # rendered hours phrase, "" when unknown
    admission_timeout: int
    rate_limited: int
    account_limit: int             # org_concurrency_limit: the gateway itself
                                   # named the account's pool as the reason
    client_idle_timeout: int
    upstream: int                  # upstream_unavailable + upstream_error
    admitted_pct: Optional[float]  # interactive tier, None when unmeasured
    starved_peers: int
    deadlines: list                # [(model, deadline_s, n), ...] observed only


def _count(by_outcome, name: str) -> int:
    if not isinstance(by_outcome, dict):
        return 0
    return _int(by_outcome.get(name)) or 0


def _hours(value) -> str:
    """A window as copy, "" when the caller did not say how wide it is."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    if float(value) == int(value):
        hours = int(value)
        return "%d hour" % hours if hours == 1 else "%d hours" % hours
    return "%.1f hours" % float(value)


def _local_facts(local) -> _LocalFacts:
    """This machine's half, None-safe.

    Counts are read from analyse()'s by_outcome verbatim: a missing outcome
    is zero requests, not an error. Only the outcomes the attribution rules
    name are read - admission_timeout and client_idle_timeout (the two the
    logs carry for saturation), rate_limited and org_concurrency_limit (the
    two ways the gateway itself reports that the account's concurrent-request
    capacity is full; a bare 429 and the named
    CONCURRENT_REQUEST_LIMIT_EXCEEDED class arrive as different outcomes) and
    the two upstream failure classes."""
    local = local if isinstance(local, dict) else {}
    by = local.get("by_outcome")

    admitted = None
    conv = local.get("admission_conversion")
    interactive = conv.get("interactive") if isinstance(conv, dict) else None
    if isinstance(interactive, dict):
        pct = interactive.get("admitted_pct")
        if isinstance(pct, (int, float)) and not isinstance(pct, bool):
            admitted = float(pct)

    deadlines = []
    per_model = local.get("per_model_deadlines")
    if isinstance(per_model, dict):
        for model in sorted(per_model):
            entry = per_model.get(model)
            if not isinstance(entry, dict) or entry.get("status") != "observed":
                continue
            seconds = entry.get("deadline_s")
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
                continue
            deadlines.append((str(model), float(seconds), _int(entry.get("n")) or 0))

    return _LocalFacts(
        requests=_int(local.get("requests")) or 0,
        peak=_int(local.get("peak_concurrency")) or 0,
        window=_hours(local.get("window_hours")),
        admission_timeout=_count(by, "admission_timeout"),
        rate_limited=_count(by, "rate_limited"),
        account_limit=_count(by, "org_concurrency_limit"),
        client_idle_timeout=_count(by, "client_idle_timeout"),
        upstream=_count(by, "upstream_unavailable") + _count(by, "upstream_error"),
        admitted_pct=admitted,
        starved_peers=_int(local.get("starved_peer_evidence")) or 0,
        deadlines=deadlines,
    )


# ---- the account's half -----------------------------------------------------
class _AccountFacts(NamedTuple):
    """The capacity dict, reduced to what an attribution needs.

    `present` is the load-bearing field: it says the account half arrived
    with something to place a local measurement against - a priority
    concurrency, a live occupancy, or admission days. A degraded payload
    (config null, now null, no days) is ABSENT, not a quiet account."""

    present: bool
    in_use: Optional[int]
    limit: Optional[int]
    available: Optional[int]
    saturated: bool
    sampled_at: object
    denial_days: list              # [(day, denied, denied_after_wait), ...]
    stale: bool


def _account_facts(capacity) -> _AccountFacts:
    """The account's half, None-safe.

    `saturated` is the API's own word for a full priority pool and is
    believed only as a boolean; `in_use >= limit` counts as saturated too,
    because two fields of the account's own payload agreeing is still the
    account's report. An
    admission day corroborates occupancy only where denied + denied_after_wait
    > 0: a day of accepted traffic says nothing about a refusal."""
    capacity = capacity if isinstance(capacity, dict) else {}
    now = capacity.get("now")
    now = now if isinstance(now, dict) else {}
    in_use = _int(now.get("in_use"))
    limit = _int(now.get("limit"))
    available = _int(now.get("available"))
    saturated = now.get("saturated") is True or (
        in_use is not None and limit is not None and in_use >= limit)

    denial_days = []
    raw_days = capacity.get("window_days")
    if isinstance(raw_days, list):
        for entry in raw_days:
            if not isinstance(entry, dict):
                continue
            denied = _int(entry.get("denied")) or 0
            after = _int(entry.get("denied_after_wait")) or 0
            if denied + after > 0:
                denial_days.append((str(entry.get("day") or "an unreported day"),
                                    denied, after))

    present = bool(
        _int(capacity.get("priority_lanes")) is not None
        or in_use is not None or limit is not None or available is not None
        or now.get("saturated") is not None or now.get("sampled_at") is not None
        or denial_days)
    return _AccountFacts(present=present, in_use=in_use, limit=limit,
                         available=available, saturated=saturated,
                         sampled_at=now.get("sampled_at"),
                         denial_days=denial_days,
                         stale=capacity.get("stale") is True)


# ---- copy -------------------------------------------------------------------
def _width_value(value, source: str) -> str:
    """The resolved width as a labelled number phrase. Always says whose."""
    if not isinstance(value, int) or isinstance(value, bool):
        return ("unknown (no priority concurrency from the account and no "
                "--plan declaration)")
    if source == "account":
        return "%d priority concurrent requests (account)" % value
    if source.startswith("plan "):
        return "%d priority concurrent requests (declared plan %s)" % (
            value, source[len("plan "):])
    return "%d priority concurrent requests (declared by --plan)" % value


def _width_sentence(value, source: str) -> str:
    if source == "account":
        return "the account's priority concurrency is %s" % _width_value(value, source)
    if isinstance(value, int) and not isinstance(value, bool):
        return "the declared width is %s" % _width_value(value, source)
    return "the resolved width is %s" % _width_value(value, source)


def _inside_phrase(value, source: str) -> str:
    """A clause to follow a local peak, saying what the peak sat inside."""
    if not isinstance(value, int) or isinstance(value, bool):
        return ("with the width unknown (no priority concurrency from the "
                "account and no --plan declaration)")
    if source == "account":
        return "inside the account's %s" % _width_value(value, source)
    return "inside the declared width of %s" % _width_value(value, source)


def _day_summary(acct: _AccountFacts) -> str:
    days = sorted({day for day, _, _ in acct.denial_days})
    denied = sum(d for _, d, _ in acct.denial_days)
    after = sum(a for _, _, a in acct.denial_days)
    bits = []
    if after:
        bits.append("%d parked then refused after waiting (denied_after_wait)" % after)
    if denied:
        bits.append("%d denied outright" % denied)
    return "%s on %s UTC" % (" and ".join(bits), ", ".join(days))


def _occupancy_clause(acct: _AccountFacts) -> str:
    """The account's occupancy as one clause, labelled (account)."""
    if acct.in_use is not None and acct.limit is not None:
        clause = "in use %d of %d priority concurrent requests (account)" % (
            acct.in_use, acct.limit)
        return clause + (", saturated" if acct.saturated else "")
    if acct.in_use is not None:
        return ("in use %d priority concurrent requests, limit not reported "
                "(account)" % acct.in_use)
    if acct.limit is not None:
        return ("limit %d priority concurrent requests, in use not reported "
                "(account)" % acct.limit)
    if acct.saturated:
        return "saturated (account)"
    if acct.denial_days:
        return "%s (account)" % _day_summary(acct)
    return "no occupancy (account)"


def _account_clause(acct: _AccountFacts) -> str:
    if not acct.present:
        return "the account half is absent (no capacity payload)"
    return "the account reported %s" % _occupancy_clause(acct)


def _requests(n: int, one: str, many: Optional[str] = None) -> str:
    """`n` requests as copy, with the verb agreeing: "1 request was",
    "2 requests were". `many` defaults to `one` when the verb does not
    change."""
    return "%d %s %s" % (n, "request" if n == 1 else "requests",
                         one if n == 1 else (one if many is None else many))


def _concurrent(n: int) -> str:
    """`n` concurrent requests as copy, always labelled (this machine)."""
    return "%d concurrent request%s (this machine)" % (n, "" if n == 1 else "s")


def _requests_phrase(facts: _LocalFacts) -> str:
    if facts.requests == 0:
        return "no requests in the window (this machine)"
    if facts.window:
        return _requests(facts.requests, "in the last %s (this machine)" % facts.window)
    return _requests(facts.requests, "in the window (this machine)")


def _local_evidence_rows(facts: _LocalFacts) -> list:
    rows = []
    if facts.admission_timeout:
        rows.append((
            "blocked at admission",
            _requests(facts.admission_timeout,
                      "waited for room and was refused (this machine)",
                      "waited for room and were refused (this machine)")))
    if facts.account_limit:
        rows.append((
            "refused at the account limit",
            _requests(facts.account_limit,
                      "was refused with CONCURRENT_REQUEST_LIMIT_EXCEEDED: the "
                      "gateway named the account's whole pool as the reason "
                      "(this machine)",
                      "were refused with CONCURRENT_REQUEST_LIMIT_EXCEEDED: the "
                      "gateway named the account's whole pool as the reason "
                      "(this machine)")))
    if facts.rate_limited:
        rows.append((
            "rate limited",
            _requests(facts.rate_limited,
                      "was told the account's concurrent-request capacity was "
                      "full (this machine)",
                      "were told the account's concurrent-request capacity "
                      "was full (this machine)")))
    if facts.client_idle_timeout:
        rows.append((
            "silent stalls",
            _requests(facts.client_idle_timeout,
                      "stalled with no response to show for the wait "
                      "(this machine)")))
    if facts.upstream:
        rows.append((
            "upstream failures",
            _requests(facts.upstream,
                      "failed upstream (this machine); the error text was "
                      "classed upstream_unavailable or upstream_error")))
    if facts.starved_peers:
        rows.append((
            "starved peers",
            _requests(facts.starved_peers,
                      "stalled but saw another request to the same model "
                      "complete inside the same window, so the model was "
                      "serving (this machine)")))
    if facts.admitted_pct is not None and facts.admission_timeout:
        rows.append((
            "interactive admitted",
            "%.1f%% of interactive requests were admitted (this machine)"
            % facts.admitted_pct))
    for model, seconds, n in facts.deadlines:
        rows.append((
            "park deadline observed",
            "%s: %.1fs median over %s (this machine)"
            % (model, seconds, _requests(n, "refused"))))
    if not rows:
        rows.append((
            "local evidence",
            "no refusal, stall or upstream failure in the window (this machine)"))
    return rows


def _rows(facts: _LocalFacts, value, source: str, acct: _AccountFacts) -> list:
    """Every row the attribution shows, ordered local then account."""
    rows = [
        ("request count", _requests_phrase(facts)),
        ("peak concurrency", _concurrent(facts.peak)),
        ("priority concurrency", _width_value(value, source)),
    ]
    if (acct.in_use is not None or acct.limit is not None
            or acct.available is not None or acct.saturated):
        rows.append(("account occupancy", _occupancy_clause(acct)))
    if acct.sampled_at is not None:
        rows.append(("account sample time", "%s (account)" % acct.sampled_at))
    if acct.stale:
        rows.append((
            "account sample age",
            "past the 5-minute cache TTL, so it may lag what the account is "
            "holding now; run %s to refresh (account)" % CAPACITY_COMMAND))
    if acct.denial_days:
        rows.append((
            "account admission log",
            "%s (account; a different vantage point from this machine's own "
            "counts)" % _day_summary(acct)))
    rows.extend(_local_evidence_rows(facts))
    return rows


# ---- the attribution --------------------------------------------------------
def attribute(local, capacity, priority) -> Attribution:
    """Place this machine's measurement beside the account's occupancy.

    `local` is the dict analyse() returns. `capacity` is the additive
    metrics.capacity dict (plan, priority_lanes, background_lanes,
    overflow_routing, provenance, stale, reason) with, when a live sample is
    attached, its "now" block and its per-UTC-day "window_days". `priority`
    is resolve_priority_concurrency's (value, source); this function renders
    that resolution and never re-resolves it.

    The five verdicts, in the precedence they are applied:

    1. "unknown" - the resolved width is unknown AND the account half is
       absent (no priority concurrency, no live occupancy, no admission
       days). Neither half can name the owner, so the headline says both are
       unestablished and names the command that fetches the account half.
       A local peak alone is never turned into a verdict: with no width to
       place it beside, a busy machine is unknown, never "you".
    2. "you" - this machine's peak concurrency reached or passed the
       resolved width. The local peak and the width are both labelled in the
       headline. (The rule's second clause, blocked at admission > 0
       AND peak >= width, is a subset of the first: the threshold alone
       decides, and the refusals are counted in the rows.)
    3. "your-org" - the local traffic stayed inside the width (peak < the
       resolved width, or no width to exceed), there IS local refusal or
       stall evidence (admission_timeout + client_idle_timeout +
       rate_limited + org_concurrency_limit > 0), AND the account
       corroborates the pool being full (now.in_use >= now.limit,
       now.saturated, an admission day with denied + denied_after_wait > 0,
       or a request the gateway itself refused with
       CONCURRENT_REQUEST_LIMIT_EXCEEDED - a named account-level rejection
       needs no fetched payload to corroborate itself). The headline says plainly that this machine
       was not the width and the account was.
    4. "the-gateway" - upstream failures locally (upstream_unavailable or
       upstream_error) with no refusal at admission and no account
       occupancy evidence. The failures were the gateway's, not this
       machine's concurrency.
    5. "not-concurrency" - none of the above. Concurrency is not what is
       wrong here, and the headline says so plainly.
    """
    facts = _local_facts(local)
    acct = _account_facts(capacity)
    value, source = priority
    width = _int(value)

    rejected = facts.admission_timeout + facts.rate_limited
    stalls = facts.client_idle_timeout
    evidence = rejected + stalls + facts.account_limit
    # An `org_concurrency_limit` rejection is the account's own report that the
    # pool was full: the gateway named it in the response, so it corroborates
    # the account half by itself, with no capacity payload required. Reading it
    # as "nothing was refused" (the pre-fix behaviour) let a window of named
    # org-limit rejections be attributed to "not concurrency".
    corroborated = (acct.saturated or bool(acct.denial_days)
                    or facts.account_limit > 0)
    rows = _rows(facts, width, source, acct)

    # ---- 1. neither half can name the owner -----------------------------
    if width is None and not acct.present:
        local_half = ("not measured, no requests in the window (this machine)"
                      if not facts.requests else
                      "a peak of " + _concurrent(facts.peak))
        headline = (
            "Attribution unknown: the local half is %s and there is no account "
            "width to sit beside it, while the account half is unknown (no "
            "priority concurrency and no occupancy reported), so run %s to "
            "fetch the account's half." % (local_half, CAPACITY_COMMAND))
        actions = [
            "Run %s to fetch the account's priority concurrency and its live "
            "occupancy; without that half no local measurement can name the "
            "owner of a refusal." % CAPACITY_COMMAND,
            "If the capacity API cannot be reached, --plan with a plan name "
            "or a plain number declares the width you expect; a declaration "
            "is labelled as a declaration and is never a measurement.",
        ]
        return Attribution(VERDICT_UNKNOWN, headline, rows, actions)

    # ---- 2. this machine's own peak reached the width -------------------
    if width is not None and facts.peak >= width:
        if evidence:
            tail = ("so the %s"
                    % _requests(
                        evidence,
                        "that was refused or stalled in this window belongs "
                        "to this machine's own traffic.",
                        "that were refused or stalled in this window belong to "
                        "this machine's own traffic."))
        else:
            tail = ("so nothing was refused, but the next burst at this "
                    "machine has no headroom.")
        headline = (
            "You: this machine's peak concurrency was %s and %s, %s"
            % (_concurrent(facts.peak), _width_sentence(width, source), tail))
        actions = [
            "Keep this machine's concurrent requests below %s: the peak "
            "already reached it." % _width_value(width, source),
            "Move the wide part of the work to the -background variant of the "
            "same model, which parks for room instead of sharing the priority "
            "pool.",
        ]
        return Attribution(VERDICT_YOU, headline, rows, actions)

    # ---- 3. the account was the width, not this machine -----------------
    if evidence and corroborated and (width is None or facts.peak < width):
        headline = (
            "Your organization: this machine's peak concurrency was %s, %s, "
            "and %s; this machine was not the width, the account was."
            % (_concurrent(facts.peak), _inside_phrase(width, source),
               _account_clause(acct)))
        actions = [
            "Nothing on this machine buys room: the width is the account's, "
            "and other sessions were holding it.",
            "Check what else runs on this account - other machines, other "
            "people's agents, background work - and send this work when the "
            "account's in_use is lower; luna-doctor capacity shows it.",
        ]
        return Attribution(VERDICT_YOUR_ORG, headline, rows, actions)

    # ---- 4. upstream failures, no refusal, no occupancy -----------------
    if facts.upstream and rejected == 0 and facts.account_limit == 0 and not corroborated:
        headline = (
            "The gateway: %s with no refusal at admission, the peak "
            "concurrency beside them was %s, and %s; the failure was the "
            "gateway's, not this machine's concurrency."
            % (_requests(facts.upstream, "failed upstream (this machine)"),
               _concurrent(facts.peak), _account_clause(acct)))
        actions = [
            "Retry the failed work: upstream failures with no refusal at "
            "admission are the gateway's, and a retry is the cheap move.",
            "luna-doctor analyze lists the upstream error signatures and when "
            "each one started.",
        ]
        return Attribution(VERDICT_THE_GATEWAY, headline, rows, actions)

    # ---- 5. none of the above -------------------------------------------
    if evidence:
        middle = "%s refused or stalled, while %s" % (
            _requests(evidence, "was", "were"), _account_clause(acct))
    else:
        middle = "nothing in the window was refused, stalled or failed"
    headline = (
        "Not concurrency: this machine's peak concurrency was %s, %s, and %s; "
        "concurrency is not what is wrong here."
        % (_concurrent(facts.peak), _inside_phrase(width, source), middle))
    if evidence:
        actions = [
            "Refresh the account sample with %s before concluding anything "
            "about the pool: the refusal is not matched by account "
            "occupancy." % CAPACITY_COMMAND,
            "luna-doctor analyze shows the per-model errors and the park "
            "deadlines this machine observed.",
        ]
    else:
        actions = [
            "Nothing to adjust: no refusal, stall or upstream failure, and the "
            "peak stayed inside the width.",
            "luna-doctor capacity shows the account's occupancy if you want to "
            "watch it move.",
        ]
    return Attribution(VERDICT_NOT_CONCURRENCY, headline, rows, actions)
