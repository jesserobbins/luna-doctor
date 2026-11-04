# Capacity API design (short)

Date: 2026-10-23
Status: implemented on the capacity-api branch. The precedence below reflects the
owner's later call: the capacity API is the first authority, and `--plan` is the fallback.
Owner: Jesse. A decision marked "Jesse's call" below was settled in conversation.

## Intent

LunaRoute serves `GET https://api.lunaroute.com/v1/capacity`, documented at
docs.lunaroute.com/api/capacity/. It reports the account's effective capacity: the plan,
the priority lane count, the background lane count, and the overflow routing. It also
reports live occupancy, 7-day and 30-day occupancy rollups, and per-day admission outcomes
(accepted, deferred, denied, denied_after_wait).

Before this change, luna-doctor invented the number that matters most. The user typed
`--plan max`, and the chart drew 4, which is a declaration. The API makes the real number
available. This account was verified live: plan `apex`, 12 priority lanes, 12 background
lanes, `overflow_routing: enabled`.

The approach is B, settled in conversation. Capacity enters through the sources layer and
wires the three consumers that gain most from it. It does not become a dependency of every
command.

## Goals

1. `fetch_capacity()` in `sources.py`, beside the existing fetchers. It caches to disk, it
   carries provenance, and it degrades to unknown. It never degrades to zero and never to a
   default.
2. The chart's priority rule draws from the account's real `priority_lanes` when a payload is
   cached or fetchable. The capacity API is the first authority. `--plan` stays as the
   declaration of last resort for the time when the API is unreachable.
3. `analyze` reports the server-side capacity facts beside the local ones, in the form
   "server limit 12 and local peak 17". It also surfaces `denied_after_wait` as
   corroboration for the park-deadline class of rejection.
4. A `capacity` subcommand renders the API's own story (now, config, history, admission) as a
   first-class report.

## Non-goals

- This pass does not rewrite the verdict model in `analyze`. Server-side numbers are
  reported beside local ones. The choice of which number leads a headline belongs to Jesse,
  and the open question below records it.
- No new JSON schema break. `metrics.capacity` is additive, and the schema stays 4.
- No polling, no daemon, no webhook. One fetch, one cache.

## Constraints, from the docs and verified against the live endpoint

- The host is `api.lunaroute.com`, which is the account surface and not the inference
  gateway. The header is `LUNAROUTE-API-KEY: <lr_...>`. That is the documented form. Bearer
  also works, and the documented form is canonical. A key that starts with `lr_platform_` is
  rejected.
- The rate limit is 60 requests per minute per key, and the observed latency is 4 to 17
  seconds. Never fetch for one render. Cache to disk the way the catalog does.
- `now` and `admission` are computed on every call. `history` is cached for about 5 minutes
  on the server. Our cache TTL is 5 minutes for the whole payload, which matches the history
  cadence of the server. A stale `now` is labelled by `sampled_at`, which we surface rather
  than hide.
- The null contract is load-bearing. A degraded block arrives with HTTP 200 and a `null`
  value: `config: null`, `now` fields null with `ledger: "unknown"`, `history.tail: null`.
  `priority_lanes: null` means unknown, not zero. `background_lanes: 0` is a real value,
  which means the account is entitled to no overflow. Only a history failure returns a hard
  500. Every absent value renders as unknown with its reason, and the provenance model
  (`PROV_UNKNOWN` with a reason) already specifies this.
- `now` covers the priority pool only. Overflow traffic does not appear in `in_use`.
  `admission` covers everything that contends for the shared pool. The report must not
  present `in_use` as all traffic.
- `admission` holds one entry per complete UTC day, and it never includes today. A bursty
  afternoon must not read as a collapse. Render the date window honestly.

## Provenance mapping

| API fact | provenance |
|---|---|
| `config.priority_lanes` and its siblings | new state `account`: fetched from api.lunaroute.com and served from the disk cache |

Jesse's rule from the combined-sources design allows five words and no more. A configured
constant and a fetched account value are different states, and merging them reintroduces the
borrowed-constant problem. Add `account` as a sixth word and update `PROVENANCE_LEGEND`.
This is the one contract addition.

## What gets wired

### `sources.py`

- `CAPACITY_URL = "https://api.lunaroute.com/v1/capacity"`. The cache sits at
  `~/.luna-doctor/capacity.json` with a TTL of 5 minutes, which the caller can override the
  way the catalog allows.
- `fetch_capacity(allow_network, *, key=None, ttl=None, now=None)` returns `Capacity | None`
  and mirrors the cache-first shape of `fetch_catalog`. A fresh cache serves without the
  network. A stale cache serves and is marked stale when the run is offline. A network
  refresh happens only when `allow_network` is set and the cache is stale or missing.
- A `@dataclass Capacity` holds `now`, `config`, `history` and `admission` as sub-objects.
  The fields are verbatim from the API and they preserve `None`. It also holds `fetched_at`
  and `stale`. There are no derived getters beyond the passthroughs for `priority_lanes`,
  `background_lanes`, `plan` and `overflow_routing` on `config`.
- The `--online` and `--offline` flags extend to capacity without a new rule. Capacity is
  fetched under the same rules as the catalog.

### `chart`

- The rule line resolves in this order. First the cached or fetched `config.priority_lanes`
  from the capacity API, which is the first authority. Second `--plan`, a declaration for
  the time when the API is unreachable or reports no number. Third nothing, and no rule is
  drawn. The label states which case applies: "priority concurrency 12 (account, plan apex)",
  or "(declared via --plan max)", or "not established, no rule drawn".
- A `--plan` value that conflicts with the account is not an error. The account wins, and
  the report says that it ignored the declaration because the account reports otherwise.

### `analyze`

- An additive `metrics.capacity` block holds the plan, the lanes, and the overflow routing.
  When the window overlaps the day range of the API, it also holds the server-side admission
  counts for those days (accepted, deferred, denied, denied_after_wait) beside the local
  outcome counts.
- One adjustment line appears when `denied_after_wait` is above zero in the days of the
  window. It states that the server confirms requests parked and were refused after waiting,
  which corroborates the 60-second park-deadline rejection class that the local logs
  classify from error strings. It is a cross-check and not a merge. Local and server counts
  come from different vantage points, and they can differ.
- The local peak and the server limit are both shown, in the form "local peak 17 concurrent
  requests, a lower bound because other machines are invisible, and account limit 12
  priority".

### `capacity` subcommand, new

- It renders now, config, 7 days, 30 days and admission as one report. The report holds the
  live occupancy with `saturated` and `ledger` health, the plan and the lanes, the occupancy
  rollups, and the admission table with `avg_queue_wait_ms`. A null wait renders as "no
  waiting that day" and never as 0.
- It takes a `--json` envelope like every other command, and the envelope carries `contract`.
- It is the one command that can fetch live under the normal online rules. Every other
  command consumes the cache.

## Approaches considered

- A, full wiring everywhere: rejected for now. It makes a slow API of 4 to 17 seconds a
  dependency of every surface, and it needs an offline story for every command. The
  consumers above are the ones with a use for the facts today.
- B, sources layer plus three consumers plus a subcommand: chosen. The layer exists for this
  purpose, provenance already models the states, and `--plan` stops being the only way to
  get a real number without becoming forbidden.
- C, subcommand only: rejected. It leaves `--plan` declaring numbers that the API already
  knows, which is the worst of both now that the API exists.

## Left to the builder

- The exact report layout of the `capacity` subcommand, in plain text and in `--json`.
- Whether the capacity line in `analyze` sits in the headline or in the adjustments. The
  builder's call is the adjustments, because the headline carries the local story and the
  server block is corroboration.
- Test fixtures: a canned live payload captured on 2026-10-23, with days whose
  `avg_queue_wait_ms` is null, plus synthetic degraded shapes (null config, null tail, ledger
  unknown). Every network test is monkeypatched, and the suite stays offline.
- The success test: on this machine, `chart --since 24h` with a cached capacity payload draws
  the rule at 12, labelled account and apex, without `--plan`. `analyze --since 7d` shows
  `denied_after_wait: 16` on Oct 20.

## Open question for Jesse, which does not block the build

When both numbers exist, which one leads the answer to "are you over capacity"? The
candidates are `now.in_use` and `limit` from the server, which are authoritative and cover
the priority pool only, and the local reconstructed peak, which covers the whole story and
is a lower bound. This pass reports both. The ordering of the headline is deliberately not
decided here.
