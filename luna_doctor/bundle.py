#!/usr/bin/env python3
"""The paste-safe evidence block: one thing to hand a maintainer.

A report that a user pastes into a support thread is usually undatable and
unattributable. It carries no tool version, no window and no store identity.
The --json path also adds the absolute home paths of the machine that
produced it. A maintainer cannot reproduce that report. The reporter
published the shape of the filesystem to say it. This module makes one block
that fixes both halves.

  redact()       the same text, with home paths, email addresses and opaque
                 tokens replaced by markers. A user can paste it in public.
  issue_block()  tool and version, the window, the stores, the verdict when
                 there is one, the headline and the rows. The stores are the
                 ones that the evidence came from. Each line is plain ASCII
                 and no wider than 100 columns. A chat client or an issue
                 tracker therefore shows the block unchanged.
  manifest()     what the block names and what it leaves out. It always
                 states the omission, because a reader must be able to tell a
                 quiet window from a truncated one.

Three rules hold throughout:

- Redaction is narrow on purpose. A date, an ISO timestamp, a duration, a
  count and a model id are facts that a maintainer needs. A maintainer uses
  them to date a report and to reproduce it. redact() therefore leaves them
  alone. It removes identity, not evidence.
- LunaRoute bills concurrent sessions. Nothing here counts or prices any
  other unit. Nothing here states a capacity width as a universal fact. A
  width belongs to one account or to one declaration, never to the product.
- These are pure functions over strings and lists. No file is read, no socket
  is opened, no command is run and no clock is consulted. A block is exactly
  as reproducible as the data that a caller hands to it.
"""

from __future__ import annotations

import re
import textwrap
from typing import List

# The widest line an issue block may emit. A pasted block wraps in the reader's
# client at some width nobody here controls, so the tool wraps its own text
# first and never relies on the client to do it.
MAX_LINE = 100

MARK_EMAIL = "<redacted:email>"
MARK_TOKEN = "<redacted:token>"


# ---- redaction --------------------------------------------------------------
# A path is matched only where it starts a token: a leading slash preceded by a
# letter or a dot is a URL path ("example.com/home/x"), not a home directory, and
# rewriting it would corrupt a link without redacting anything.
_BOUNDARY = r"(?<![A-Za-z0-9_.\-])"
_EMAIL = r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+"
# /Users/<name> and /home/<name>, up to the next slash: the rest of the path is
# left as it was, so ~/GitHub/repo keeps its shape and its readability.
_HOME_PATH = r"/(?:Users|home)/[^/\s]+(?=/|$)"
# 24 characters of an alphabet real keys are drawn from. The floor is short
# enough to catch a LunaRoute key and long enough that ordinary prose, a date, a
# timestamp (its colons break the run) and a model id with a dot or a slash in
# it ("deepseek-4.1-flash") are never mistaken for one. A 24+ character
# hyphenated identifier with no other separator is redacted: that fails closed,
# and losing one word to a marker costs a reader less than leaking a key.
_TOKEN = r"[A-Za-z0-9_\-]{24,}"


def _redaction_pattern(home):
    """The one pass over the text: email, home, path, token, in that order.

    Order is the contract. An email address outranks a token, because its local
    part uses the same alphabet and the whole address reads better redacted as
    an address. The home-specific prefix outranks the generic forms, so a home
    outside /Users and /home is still caught.
    """
    parts = ["(?P<email>%s)" % _EMAIL]
    if home:
        parts.append("(?P<home>%s%s(?=/|$))" % (_BOUNDARY, re.escape(home)))
    parts.append("(?P<path>%s%s)" % (_BOUNDARY, _HOME_PATH))
    parts.append("(?P<token>%s)" % _TOKEN)
    return re.compile("|".join(parts))


def _redacted(match):
    if match.group("email") is not None:
        return MARK_EMAIL
    if match.group("token") is not None:
        return MARK_TOKEN
    return "~"


def redact(text, home=None):
    """`text` with the identity of the machine removed and its evidence intact.

    Replaced:

    - an absolute path under `home`, when a caller gives a home directory, and
      also the generic /Users/<name> and /home/<name> prefixes. The prefix
      becomes ~, so ~/GitHub/repo still says where a file was. It does not say
      whose machine it was on.
    - an email address -> <redacted:email>.
    - a run of 24 or more characters of [A-Za-z0-9_-] -> <redacted:token>.
      That run is what an API key looks like.

    Deliberately left alone: dates, ISO timestamps, durations ("120.2s"),
    counts, sizes and model ids. They are what dates and reproduces a report.
    None of them names a person or a credential.

    The result is idempotent, so redact(redact(x)) == redact(x). Each marker
    that it writes contains a character ("<", ":", ">", "~") that no pattern
    above matches. No marker is long enough or joined enough to read as a
    token. That property matters wherever a caller redacts a block a second
    time on its way out.
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    if home is not None:
        home = str(home).rstrip("/")
    return _redaction_pattern(home or None).sub(_redacted, text)


# ---- the block --------------------------------------------------------------
# ANSI escapes and non-ASCII are stripped on the way out: a pasted block must
# render the same in a terminal, a chat client and an issue tracker, and only
# ASCII does. A run of three backticks would close the fence the block is meant
# to sit inside, so it is collapsed to one; inside a fence nothing else is
# interpreted, so backticks are the only sequence that has to be handled.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_FENCE = re.compile(r"`{3,}")


def _clean(text) -> str:
    text = _ANSI.sub("", "" if text is None else str(text))
    text = _FENCE.sub("`", text)
    return text.encode("ascii", "replace").decode("ascii")


def _lines(text, indent="") -> List[str]:
    """`text` as cleaned, wrapped lines, each no longer than MAX_LINE.

    A word that is longer than the width is broken, because the width is a
    promise to the reader. One long path must not break that promise.
    """
    body = _clean(text)
    if not body.strip():
        return []
    return textwrap.wrap(body, width=MAX_LINE, subsequent_indent=indent,
                         break_long_words=True, break_on_hyphens=False)


def _stated(value) -> str:
    """`value` as copy, "unknown" when the caller has nothing to say."""
    text = "" if value is None else str(value).strip()
    return text or "unknown"


def _window_line(provenance) -> str:
    start = _stated(provenance.get("window_start"))
    end = _stated(provenance.get("window_end"))
    if start == "unknown" and end == "unknown":
        return "window: unknown (the provenance states no window)"
    return "window: %s .. %s" % (start, end)


def _stores_line(provenance) -> str:
    stores = provenance.get("stores")
    names = [str(s) for s in stores] if isinstance(stores, (list, tuple)) else []
    if not names:
        return "stores: none reported"
    return "stores: %d (%s)" % (len(names), ", ".join(names))


def issue_block(headline, rows, provenance, verdict=None) -> str:
    """The paste-safe block: datable, attributable and reproducible.

    `rows` are (label, value) pairs. `provenance` carries the version, the log
    schema, the ISO window bounds and the store names that the evidence came
    from. It is a dict with the keys version, schema, window_start, window_end
    and stores. `verdict` is an optional (word, sentence) pair: the owner from
    the attribution and the sentence that explains it. The block leaves the
    verdict out when there is none, because a block without a verdict reports
    what was measured. It makes no claim about who was at fault.

    The order is fixed: tool and version, the window, the stores, the verdict
    when a caller gives one, the headline, then the rows. Every line is ASCII,
    with no ANSI escape and no emoji, and each line fits inside MAX_LINE
    columns. A fenced code block in a chat client or an issue tracker
    therefore keeps the block as it is. Nothing reflows it and nothing
    reinterprets it. The result ends in a single newline.
    """
    provenance = provenance if isinstance(provenance, dict) else {}
    lines = []
    lines.extend(_lines("luna-doctor %s (log schema %s)"
                        % (_stated(provenance.get("version")),
                           _stated(provenance.get("schema")))))
    lines.extend(_lines(_window_line(provenance)))
    lines.extend(_lines(_stores_line(provenance)))
    if verdict:
        word, sentence = verdict
        lines.extend(_lines("verdict: %s - %s" % (_stated(word), _stated(sentence))))
    lines.extend(_lines(headline))
    for label, value in rows or ():
        lines.extend(_lines("%s: %s" % (_stated(label), _stated(value)),
                            indent="  "))
    return "\n".join(lines) + "\n"


# ---- what is in the block and what is not -----------------------------------
def _manifest_line(kind, what, detail) -> str:
    what = _stated(what)
    detail = "" if detail is None else str(detail).strip()
    if detail:
        return _clean("%s: %s (%s)" % (kind, what, detail))
    return _clean("%s: %s" % (kind, what))


def manifest(included, omitted) -> List[str]:
    """What the block includes and what it leaves out, one line each.

    `included` and `omitted` are lists of (what, detail) pairs. Each entry is
    named on both sides, and an empty side still gets a line. A reader can then
    tell a quiet window from a truncated one, and never has to guess what a
    caller cut. The omissions are always listed, even when there are none. The
    line "omitted: none" is itself the answer to a question that the reader
    will ask.
    """
    lines = [_manifest_line("included", what, detail)
             for what, detail in included or ()]
    if not lines:
        lines.append("included: none (no local evidence matched the window)")
    omissions = [_manifest_line("omitted", what, detail)
                 for what, detail in omitted or ()]
    if not omissions:
        omissions.append("omitted: none (every part named above is in the block)")
    return lines + omissions
