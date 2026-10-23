# Capacity API — design (short)

Date: 2026-10-23
Status: proposed
Owner: Jesse (decisions marked "Jesse's call" below were settled in conversation)

## Intent

LunaRoute now serves `GET https://api.lunaroute.com/v1/capacity` (documented
at docs.lunaroute.com/api/capacity/): the account's effective capacity —
plan, priority lanes, background lanes, overflow routing — plus live
occupancy, 7d/30d occupancy rollups, and per-day admission outcomes
(accepted / deferred / denied / denied_after_wait). luna-doctor currently
*fakes* the one number that matters most: the user types `--plan max` and the
chart draws 4, a declaration. The API makes the real number available
(verified live: this account is `apex`, 12 priority / 12 background lanes,
`overflow_routing: enabled`).

Approach **B**, settled in conversation: capacity enters through the sources
layer and wires the three highest-value consumers; it does not become a
dependency of every command.

## Goals

1. `fetch_capacity()` in `sources.py` beside the existing fetchers, cached to
   disk, provenance-carrying, degrading to unknown — never zero, never a
   default.
2. The chart's priority rule draws from the account's real `priority_lanes`
   when a capacity payload is cached or fetchable; `--plan` remains as the
   explicit override for when the API is unreachable.
3. `analyze` reports the server-side capacity facts next to the local ones —
   "server limit 12 · local peak 17" — and surfaces `denied_after_wait` as
   corroboration for the park-deadline class of rejections.
4. A `capacity` subcommand renders the API's own story (now / config /
   history / admission) as a first-class report.

## Non-goals

- No rewrite of `analyze`'s verdict model in this pass. Server-side numbers
  are reported *alongside* local ones; which one leads a headline is Jesse's
  open call (see Open questions).
- No new JSON-schema break. `metrics.capacity` is additive; schema stays 4.
- No polling, no daemon, no webhook. One fetch, one cache.

## Constraints (from the docs, verified against the live endpoint)

- Host is `api.lunaroute.com` — the account surface, not the inference
  gateway. Header: `LUNAROUTE-API-KEY: <lr_…>` (documented form; Bearer also
  works but the doc form is canonical). `lr_platform_` keys are rejected.
- Rate limit 60/min per key; observed latency 4–17s. **Never fetch
  per-render.** Cache to disk like the catalog.
- `now` and `admission` are computed per call; `history` is cached ~5 min
  server-side. Our own cache TTL: 5 minutes for the whole payload (matches
  the server's own history cadence; `now` staleness is labelled by
  `sampled_at`, which we surface rather than hide).
- **The null contract is load-bearing.** Degraded blocks arrive HTTP 200 with
  `null` — `config: null`, `now` fields null with `ledger: "unknown"`,
  `history.tail: null`. `priority_lanes: null` means unknown, not zero;
  `background_lanes: 0` is a real value (entitled to no overflow). Only a
  history failure is a hard 500. Every absent value renders as unknown with
  its reason; the provenance model (`PROV_UNKNOWN` with a reason) already
  specifies this.
- `now` covers the priority pool only — overflow traffic does not appear in
  `in_use`. `admission` covers everything that contends for the shared pool.
  The report must not present `in_use` as "all traffic".
- One entry per complete UTC day; today is never included in `admission`
  (a bursty afternoon must not read as a collapse). Render the date window
  honestly.

## Provenance mapping

| API fact | provenance |
|---|---|
| `config.priority_lanes` etc. | `catalog`-class: fetched, cached — new word **`account`**? No: reuse `catalog` is wrong. Use **`configured`** for plan defaults and a new **`account`** state for fetched-account values. |

Jesse's rule from the combined-sources design: five words, no more. But
"configured" (documented constant) and a fetched account value are genuinely
different states, and conflating them reintroduces exactly the
borrowed-constant problem. Add **`account`** ("fetched from
api.lunaroute.com, served from disk cache") as a sixth word; update
`PROVENANCE_LEGEND`. This is the one contract addition.

## What gets wired

### `sources.py`

- `CAPACITY_URL = "https://api.lunaroute.com/v1/capacity"`, cache at
  `~/.luna-doctor/capacity.json`, TTL 5 min (overridable like the catalog).
- `fetch_capacity(allow_network, *, key=None, ttl=None, now=None)` →
  `Capacity | None`, mirroring `fetch_catalog`'s cache-first shape:
  fresh cache serves without network; stale serves marked stale when
  offline; network refresh only when `allow_network` and stale/missing.
- `@dataclass Capacity` with `now`, `config`, `history`, `admission`
  sub-objects — fields verbatim from the API, `None`-preserving; plus
  `fetched_at`, `stale`. No derived convenience getters beyond
  `priority_lanes` / `background_lanes` / `plan` / `overflow_routing`
  passthroughs on `config`.
- The `--online/--offline` flag semantics extend naturally: capacity is
  fetched only under the same rules as the catalog.

### `chart`

- Rule line resolution order: `--plan` (explicit user declaration, wins) →
  cached/fetched `config.priority_lanes` → none drawn. Label states which:
  "priority concurrency 12 (account, plan apex)" vs "(declared via --plan
  max)" vs "not established; no rule drawn".
- `--plan` conflicting with the account value is not an error — the
  declaration wins, but the report notes the account says otherwise.

### `analyze`

- Additive `metrics.capacity` block: plan, lanes, overflow routing, and —
  when the window overlaps the API's day range — the server-side admission
  counts for those days (accepted/deferred/denied/denied_after_wait) next to
  the local outcome counts.
- One new adjustment line when `denied_after_wait > 0` in the window's days:
  the server confirms requests parked and were refused after waiting —
  this corroborates the 60s park-deadline rejection class locally
  classified from log strings. Cross-check, don't merge: local and server
  counts come from different vantage points and may differ.
- Local peak vs server limit, both shown: "local peak 17 concurrent requests
  (a lower bound — other machines invisible) · account limit 12 priority".

### `capacity` subcommand (new)

- Renders now / config / 7d / 30d / admission as a report: live occupancy
  (with `saturated` and `ledger` health shown), plan and lanes, the occupancy
  rollups, and the admission table with `avg_queue_wait_ms` (null rendered
  as "no waiting that day", not 0).
- `--json` envelope like every other command, `contract` included.
- It is the one command that may fetch live under the normal online rules;
  everything else consumes the cache.

## Approaches considered

- **A — full wiring everywhere:** rejected for now; makes a slow (4–17s) API
  a dependency of every surface and needs an offline story per command. The
  consumers above are the ones with a use for the facts today.
- **B — sources layer + three consumers + a subcommand:** chosen. The
  layer exists for exactly this; provenance already models the states;
  `--plan` stops being the only way to get a real number without becoming
  forbidden.
- **C — subcommand only:** rejected. Leaves `--plan` declaring numbers the
  API knows, which is the worst of both once the API exists.

## Left to the builder

- Exact report layout of the `capacity` subcommand (plain + `--json`).
- Whether `analyze`'s capacity line sits in `headline` or `adjustments`
  (builder's call: adjustments — the headline is the local story, the
  server block is corroboration).
- Test fixtures: a canned live payload (real one captured 2026-10-23, with
  null-`avg_queue_wait_ms` days) plus synthetic degraded shapes (null
  config, null tail, ledger unknown). Every network test monkeypatched; the
  suite stays offline.
- The success test: on this machine, `chart --since 24h` with a cached
  capacity payload draws the rule at 12 labelled account/apex without
  `--plan`; `analyze --since 7d` shows `denied_after_wait: 16` on Oct 20.

## Open question for Jesse (does not block the build)

When both exist, which number leads "are you over capacity" — the server's
`now.in_use`/`limit` (authoritative, priority pool only) or the local
reconstructed peak (whole story, lower bound)? This pass reports both;
the headline-ordering choice is deliberately not made.
