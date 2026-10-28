"""Plan capacity: the per-plan calibration constant, declared not measured.

LunaRoute schedules concurrent inference requests. Every plan admits some
number of *priority* requests at once (preferential scheduling); requests
beyond that *overflow* into *background scheduling* only as capacity
allows, or park (up to 60s for priority, up to 240s for `-background`)
waiting for room, and are *rejected* when the park deadline expires or the
queue is full.

The number a user can know without probing is their plan's published
default, and this module is the one seam where that constant enters the
tool. Two sources now meet here: the account's real `priority_lanes` from
GET /v1/capacity (fetched by the `capacity` command, served to everyone
else from its disk cache) and the user's `--plan` declaration. The account
is the first authority on every account fact; `--plan` is the fallback
for when the API is unreachable. Today `chart` consumes the resolution;
the thresholds that are still hardcoded elsewhere (analyze's pressure
verdict, fix's advice, the share
card's copy) are per-plan wrong and should read their number from here in
a follow-up pass.

Three rules this module never breaks:

- It never infers a plan from observed traffic. A guessed number drawn as
  a threshold is worse than no threshold.
- It never claims `lunaroute whoami` reports a plan. Verified: `whoami`
  (via `lunaroute` on PATH or `npx --yes @lunaroute/cli`) prints login,
  organization id, profile and API base only -- no plan, no number.
- The account source is only ever the fetched capacity payload: its
  `config.priority_lanes` verbatim, null preserved as unknown. Nothing is
  inferred from it and no default is substituted when it is null.
"""

from __future__ import annotations

from dataclasses import dataclass

# LunaRoute's published plan defaults for priority concurrent inference
# requests. A plan declared by name is a declaration by the user, not a
# measurement by the tool: an organization-level override on the dashboard
# Metrics page wins over the published default. `personal` is the separate
# one-request offer with no priority scheduling; its capacity is 1.
PLAN_PRIORITY = {
    "personal": 1,
    "pro": 2,
    "max": 4,
    "apex": 6,
    "teams": 10,
}

# Table order, smallest offer first, for error messages and help text.
PLAN_NAMES = ", ".join(PLAN_PRIORITY)

PLAN_HELP = (
    "declare this account's priority concurrency: a plan name (%s) or a "
    "plain number of priority concurrent requests. The capacity API is the "
    "first authority: this declaration is used only when the account's "
    "capacity payload has no number, and one that disagrees with the "
    "account is ignored and said so. Names map to LunaRoute's published "
    "plan defaults; an organization-level override on the dashboard "
    "Metrics page wins over them. This declares a capacity, it does not "
    "measure one." % PLAN_NAMES
)


def parse_plan(value) -> int:
    """Resolve a --plan declaration to a number of priority requests.

    A plan name, case-insensitive, maps to its published default. A bare
    integer passes through unclamped: Teams sells capacity in expandable
    10-request bundles, so the account's real number may exceed the table.
    Anything else raises ValueError whose message lists the accepted names;
    an integer below 1 is rejected the same way.
    """
    s = str(value).strip()
    try:
        n = int(s)
    except ValueError:
        name = s.lower()
        if name in PLAN_PRIORITY:
            return PLAN_PRIORITY[name]
        raise ValueError(
            "unknown --plan value '%s'; expected a plan name (%s) or a "
            "plain number of priority concurrent requests" % (s, PLAN_NAMES)
        ) from None
    if n < 1:
        raise ValueError("--plan must be at least 1 priority concurrent "
                         "request, got %d" % n)
    return n


@dataclass
class Capacity:
    """What the tool knows about the account's priority concurrency.

    `priority` is None when unknown. Unknown is never expressed as zero or
    as any real number, so a caller cannot mistake "not known" for a
    measured capacity -- including personal's 1. `source` names where the
    number came from: "account" (the fetched capacity payload's
    config.priority_lanes -- the first authority), "plan" (the user's
    --plan declaration, the fallback when the account has no number),
    "default" (an explicit config read; reserved, never traffic
    inference), or "none". `declared` is the raw string the user wrote,
    "" when absent -- kept even when the account's number won, so the
    consumer can say the declaration was set aside.

    The `account_*` fields carry the fetched-account facts whenever a
    capacity payload was available, whatever won the resolution, so a
    consumer can say what the account reports even when a --plan
    declaration is in play: `account_present` (a payload was served at
    all), `account_priority` (None when the account did not report one --
    null is unknown, never zero), `account_plan`, `account_stale` (the
    payload was past its TTL when served) and `account_note` (the
    cache/fetch note). `declared_ignored` is True when a --plan
    declaration was set aside because the account reports a different
    number."""

    priority: "int | None"
    source: str
    declared: str
    declared_ignored: bool = False
    account_present: bool = False
    account_priority: "int | None" = None
    account_plan: "str | None" = None
    account_stale: bool = False
    account_note: str = ""

    @property
    def known(self) -> bool:
        """True when priority is a real number, however small."""
        return self.priority is not None


def _account_facts(account):
    """The fetched payload's priority-concurrency facts, None-safe.

    `account` is a sources.Capacity or None. `config.priority_lanes` null
    (or a config block the account did not send) is unknown, never zero;
    only a real integer counts, so a boolean or a float never sneaks in."""
    if account is None:
        return None
    cfg = getattr(account, "config", None)
    pri = getattr(cfg, "priority_lanes", None) if cfg is not None else None
    if isinstance(pri, bool) or not isinstance(pri, int):
        pri = None
    name = getattr(cfg, "plan", None) if cfg is not None else None
    if not isinstance(name, str):
        name = None
    return {"account_present": True, "account_priority": pri,
            "account_plan": name,
            "account_stale": bool(getattr(account, "stale", False)),
            "account_note": str(getattr(account, "note", "") or "")}


def resolve_capacity(args, account=None) -> Capacity:
    """Resolve the account's priority concurrency, the best source known.

    Order: the capacity API first -- a payload with a real
    config.priority_lanes is THE number, fresh or stale (staleness is the
    cache's, labelled by the consumer, and never a reason to demote the
    account behind a declaration). Then a --plan declaration, the
    fallback for when the API is unreachable or reports no number. No
    payload and no --plan means exactly that nothing is known -- never a
    guess, not from traffic, and not from `lunaroute whoami`, which reports
    no plan.

    A --plan that disagrees with the account's number is not an error: the
    account wins, `declared_ignored` is set so the consumer can say the
    declaration was set aside, and a payload whose priority_lanes is null
    falls back to the declaration."""
    acct = _account_facts(account)
    raw = getattr(args, "plan", None)
    declared = str(raw).strip() if raw is not None else ""
    if acct and acct["account_priority"] is not None:
        ignored = bool(declared) and parse_plan(declared) != acct["account_priority"]
        return Capacity(priority=acct["account_priority"], source="account",
                        declared=declared, declared_ignored=ignored, **acct)
    if declared:
        return Capacity(priority=parse_plan(declared), source="plan",
                        declared=declared, **(acct or {}))
    return Capacity(priority=None, source="none", declared="", **(acct or {}))


def _plan_arg(value):
    """argparse type for --plan: validate once, so no command can drop it silently.

    Without this, a typo (`--plan team`) reached the resolver, which swallows the
    ValueError and reports "unknown", so the report claimed no declaration was
    made when one was. argparse turns a bad value into exit 2 with the accepted
    names, which is what `chart` already did by hand.
    """
    try:
        parse_plan(value)
    except ValueError as e:
        import argparse
        raise argparse.ArgumentTypeError(str(e)) from None
    return value


def add_plan_arg(parser, help_text: str = PLAN_HELP) -> None:
    """Add the one --plan definition every command shares.

    One helper so `chart`, `analyze`, `fix` and `explain`
    cannot drift apart as the follow-up pass wires them to
    resolve_capacity().
    """
    parser.add_argument("--plan", default=None, metavar="NAME|N", help=help_text,
                        type=_plan_arg)
