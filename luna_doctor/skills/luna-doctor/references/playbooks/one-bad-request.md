# One bad request: `explain`

The user is holding one request: "this call took 95 seconds", "this one came
back 429", "this one died". `analyze` answers what the window did. It cannot
answer for a single request inside it. `explain` answers for that request alone,
inside the last 24 hours unless you pass `--since` or `--now`.

## What explain answers that analyze cannot

The window carries one peak. A request at a quiet moment and a request inside a
burst share that peak and had different waits. `explain` puts four numbers
around one request. The first is how many requests this machine held when it
started. The second is the peak during its own span. The third is how many
requests to the same model were served inside that span. The fourth is the
account's occupancy, from whatever sample the cache holds (the row carries its
own `sampled_at`). Only a sample taken within 15 minutes of the request is used
as evidence in the headline. An older one is background, not evidence.

## Which request you get

- no selector: the request that went wrong, refused or stalled, longest wait
  first. Duration is the fallback only when nothing went wrong.
- `--blocked`: the newest refusal, at admission or at the account's limit. It
  never returns a stalled request.
- `--slowest`: duration only, longest first, whatever the outcome was.
- a bare id: a response id, a session id or a run id. A session or run id names
  many requests, so the newest `--last` of them are shown. A prefix that
  matches two requests is not an identification, and matches nothing.
- `--last N` widens any of these past one (default 1).

## A stall is not a refusal

A refusal came from the gateway: blocked at admission, the account's pool at its
limit, or a bare 429. The gateway never served the request. A stall is the
harness stopping a stream that it was reading. A stall is the silent overflow
signature. The two read alike in a log, and they have different fixes. Move wide
work to the `-background` variant to answer a refusal. Narrow this machine to
answer a stall. Do not put a short idle budget in front of a long request. Do
not describe one as the other, and do not send a stall to `--blocked`.

## Exit codes

- `1`: the request you were shown was refused or stalled. There is something to
  act on.
- `0`: it was served.
- `2`: nothing to explain: no request, no id matched, unusable `--since`.
- `--blocked` with no refusal prints a message to stderr and exits `0`, because
  nothing was refused. That `0` does not mean a request was served.

## The rows

The headline prints first, then the section `REQUEST` (uppercased by the plain
renderer), then one `label: value` row per line:

- what ran: `request` (harness, session, run, provider), `model`, `tier`
  (inferred from the model name, because the logs do not carry the served
  tier), `response id`.
- when and how long: `when` (start and end, UTC), `duration`, `outcome` (the
  class id, then that class in words).
- what it produced: `stop reason`, `produced` (thinking and answer characters).
- this machine around it: `in flight when it started`, `peak during its span`,
  `same model, served meanwhile`. Each value carries `(this machine)`.
- the account: `account occupancy`, when the capacity cache holds a sample.
  That row carries `(account)`, as "in use 12 of 12 priority concurrent requests
  at 00:05:14Z".
- the raw string: `error text`, clipped at 220 characters. It is the client's
  own message, quoted, and not the reason the gateway gives.

A value that was not measured prints `unknown`, never 0. A row with no data is
absent. Each `- ` line under the rows is an action drawn from that evidence.

## Quoting the headline

The headline names the owner of the wait: this machine, the account, the
gateway, or nobody yet.

- this machine: quote number and width together, "This machine held 11
  concurrent requests ... The width is 4 priority concurrent requests (declared
  plan max)". Keep the parenthetical. The width belongs to the account or to
  the declaration, never to this machine.
- the account: "The room that this request needed was not free in the pool of
  the account." Where the gateway named it, the headline carries
  `CONCURRENT_REQUEST_LIMIT_EXCEEDED` or "a 429". The response said so.
- the gateway: "The gateway or the transport failed this request." Not
  concurrency. Retrying once is the whole answer.
- nobody yet: "The cause of this wait is not established." Quote it as it
  stands and fill no half from memory. A served request has no owner. Its
  headline explains the queueing it can see, or points at the p50 in `analyze`.

## Before you quote it

Three ways the output can mislead. Each one matches the code at `149415c` on
2026-10-27:

- A duration that the headline also calls unknown. A record written with
  second-based timestamps is rejected as unusable timing, so the `duration` row
  reads `unknown`. `explain.py:279` still quotes the number of seconds in the
  headline. When the headline says the logged interval is not usable timing,
  quote no duration from it. Say the timing is not recoverable from the record.
- Queueing at the start, or a peak while it waited. The stalled branch gives how
  many requests this machine held when the request started. It gives the peak
  during its span separately. When only the peak reached the width
  (`explain.py:312`), the wait overlapped a burst. That is not proof that the
  request queued at admission. Quote the number that matches the claim you make.
- A background request is not explained by a full priority pool. The account
  occupancy row covers priority admission only. `_account_full` (`explain.py:218`)
  tests the sample's time and saturation, and never the request's tier. For a
  `-background` request, a full priority pool is not the reason it stalled. Say
  the cause is not established.

## How this playbook ends

Four things, in order. The first is the request, with the id that names it. The
second is its span and its wait, `when` and `duration`. The third is the owner
of the wait, in the headline's own words. The fourth is the one change, the
first `- ` line, or "no change, the request was served".

One request, one owner, one change. When the evidence settles no owner, say so
and name the command that can settle it: `luna-doctor capacity --online`.
