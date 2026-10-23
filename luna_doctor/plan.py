"""Plan capacity: the per-plan calibration constant, declared not measured.

LunaRoute schedules concurrent inference requests. Every plan admits some
number of *priority* requests at once (preferential scheduling); requests
beyond that *overflow* into *background scheduling* only as capacity
allows, or park (up to 60s for priority, up to 240s for `-background`)
waiting for room, and are *rejected* when the park deadline expires or the
queue is full.

The number a user can know without probing is their plan's published
default, and this module is the one seam where that constant enters the
tool. Today only `chart` consumes it; the thresholds that are still
hardcoded elsewhere (analyze's pressure verdict, fix's advice, probe and
compare's default widths, the share card's copy) are per-plan wrong and
should read their number from here in a follow-up pass.

Two rules this module never breaks:

- It never infers a plan from observed traffic. A guessed number drawn as
  a threshold is worse than no threshold.
- It never claims `lunaroute whoami` reports a plan. Verified: `whoami`
  (via `lunaroute` on PATH or `npx --yes @lunaroute/cli`) prints login,
  organization id, profile and API base only -- no plan, no number. A
  future source may be added here only when something actually exposes one.
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
    "plain number of priority concurrent requests. Names map to LunaRoute's "
    "published plan defaults; an organization-level override on the "
    "dashboard Metrics page wins over them. This declares a capacity, it "
    "does not measure one." % PLAN_NAMES
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
    number came from: "plan" (the user's --plan declaration), "default"
    (an explicit config read; reserved, never traffic inference), or
    "none". `declared` is the raw string the user wrote, "" when absent.
    """

    priority: "int | None"
    source: str
    declared: str

    @property
    def known(self) -> bool:
        """True when priority is a real number, however small."""
        return self.priority is not None


def resolve_capacity(args) -> Capacity:
    """Read the account's declared priority concurrency from --plan.

    No --plan means exactly that: nothing is known. The number is specific
    to the account and cannot be read from these logs, so this returns
    priority None rather than guessing -- not from traffic, and not from
    `lunaroute whoami`, which reports no plan.
    """
    raw = getattr(args, "plan", None)
    if raw is None:
        return Capacity(priority=None, source="none", declared="")
    return Capacity(priority=parse_plan(raw), source="plan",
                    declared=str(raw).strip())


def add_plan_arg(parser, help_text: str = PLAN_HELP) -> None:
    """Add the one --plan definition every command shares.

    One helper so `chart`, `analyze`, `fix`, `probe` and `compare` cannot
    drift apart as the follow-up pass wires them to resolve_capacity().
    """
    parser.add_argument("--plan", default=None, metavar="NAME|N", help=help_text)
