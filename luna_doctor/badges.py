"""Provenance badges: one glyph per provenance state, for the verdict screens.

The states are sources.PROVENANCE_LEGEND's words, plus attribution's
"plan <name>" source for a plan the user declared. This module decides how
each state reads to a person; it establishes no fact of its own.
"""

from luna_doctor.sources import (PROV_ACCOUNT, PROV_CONFIGURED, PROV_DECLARED,
                                 PROV_MEASURED)

BADGE_ACCOUNT = "●"      # the account reported it: authoritative
BADGE_MEASURED = "○"     # measured here: informative, a lower bound
BADGE_DECLARED = "◌"     # declared or a documented constant: not measured

# An unclassified state gets no measurement badge: null is unknown, so "?".
_BADGE_UNKNOWN = "?"
_HELP_UNKNOWN = "not established"

# state -> (badge, the short phrase behind it). "catalog" is absent on purpose:
# the gateway listing a model limit is none of these three, so it reads unknown
# rather than borrowing a trust level it does not have.
_BY_STATE = {
    PROV_ACCOUNT: (BADGE_ACCOUNT, "the account reported it"),
    PROV_MEASURED: (BADGE_MEASURED, "measured locally, a lower bound"),
    PROV_DECLARED: (BADGE_DECLARED, "declared, not measured"),
    PROV_CONFIGURED: (BADGE_DECLARED, "declared, not measured"),
}

LEGEND = ("%s authoritative (the account reports it) · "
          "%s measured here, a lower bound · "
          "%s declared, not measured"
          % (BADGE_ACCOUNT, BADGE_MEASURED, BADGE_DECLARED))


def _entry(state):
    """The (badge, phrase) for one state string, or None when unclassified."""
    if not isinstance(state, str):
        return None
    name = state.strip().lower()
    if name.startswith("plan "):
        return _BY_STATE[PROV_DECLARED]
    return _BY_STATE.get(name)


def badge_for(state: str) -> str:
    """The one-glyph badge for a provenance state, "?" when it is not one."""
    entry = _entry(state)
    return entry[0] if entry else _BADGE_UNKNOWN


def badge_help(state: str) -> str:
    """The short phrase behind a badge, for the legend or a help line."""
    entry = _entry(state)
    return entry[1] if entry else _HELP_UNKNOWN


def legend_line() -> str:
    """The one-line legend. Every screen that shows a badge shows this line."""
    return LEGEND
