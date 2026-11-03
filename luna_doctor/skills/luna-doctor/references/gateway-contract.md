# What the gateway does, measured

Every measured number in this file carries its source and its sample count.
Two values are configured rather than measured, and each one says so where it
appears: pi's idle budget and the gateway's body ceiling. Where the observations
are too few, the answer is `unknown`, and you say `unknown`.

## The fixed wait before headers

The gateway holds every request about 25.2 s before it sends a single SSE
header. The wait does not depend on load: 25.182 s with one request in flight,
25.324 s with twenty, 0.133 s of spread across 32 responses (four captures on
2026-10-20, glm-5.3). Every observed request paid it before any token arrived.
`cli.py` carries the number as `pre_header_wait_s`.

## The admission deadline is per model

A request that finds no room is not refused at once. It waits, then ends with
`scheduler admission timeout`. The length of that wait belongs to the model.

- glm-5.3: 120.2 s, over 23 observations, every one inside 120.16-120.40 s
  (12 in a 30-day sweep of one machine's logs, 11 in the 2026-10-20 captures).
- A rejection seen near 60.3 s on a second model is unsettled: 60 s is also how
  long a priority request can park for room, so these logs cannot tell an
  admission deadline from an expired park. Quote that model's own median with
  its count, or report `unknown`.

`analyze` measures each model separately. The `admission wall` row names the
model, its median and its sample count, in the form `glm-5.3 120.2s (n=12)`.
Below `DEADLINE_MIN_SAMPLES = 3` usable observations, the row reads
`unknown (N blocked at admission, M usable)` and the advice says not to assume
another model's number. Never borrow one.

## The client gives up first

pi's idle budget is 300 s. That 300 is pi's shipped default, not a gateway
number. Measured: 18 `Stream ended without finish_reason` records at
300.20-302.96 s, 17 of them between 300 and 309 s. While a request is parked,
the gateway emits the SSE comment `: lunaroute-keepalive` every 10.0 s. 729 of
732 gaps fell between 9.5 and 10.5 s at 20 concurrent requests. The gateway
pauses the keep-alives while chunks are flowing.

So a request can stay alive for its whole 300 s and produce nothing. That is
the silent-overflow signature, and the 300 s in that error string is the
harness's number, not the gateway's.

## The response headers

`x-lunaroute-service-class` (`interactive` / `background`),
`x-lunaroute-service-tier` (`realtime` / `background`) and
`x-lunaroute-warning` (`overflowed-to-background`) are honest. They appeared
where the traffic predicts. At 20 concurrent requests on the account measured
on 2026-10-20, the run split 10 interactive and 10 background. The warning
sat on the demoted half, and no background header appeared at 1 or 10. That
split follows that account's priority concurrency. It is not a gateway
constant, and no plan width is a gateway fact.

`x-lunaroute-queue-ms` is not honest. It read `0` on all 32 responses. Of
those, 11 died at the 120.2 s deadline and produced zero tokens. Do not offer
it as evidence that nothing queued. Use `x-lunaroute-service-class` and
`x-lunaroute-warning` instead: between them they name what the request
actually got.

## The body ceiling

16,777,216 bytes, the ceiling the gateway's own 413 body reports rather than a
number we measured. A request over it returns 413 with
`{"code":"PAYLOAD_TOO_LARGE","declared_bytes":18970123,"max_bytes":16777216}`
(observed 2026-09-13), and the report classifies it `payload_too_large`. Images
at full resolution in a long conversation are the usual cause.

## What these logs do not record

- Time to first token. `analyze` measures end-to-end latency only.
- Token usage on a failed request. The harness zeroes it, so input size is
  unavailable there.
- Anything off this machine. Concurrency rebuilt from one machine is a lower
  bound: other machines and other tenants on the account are invisible.
