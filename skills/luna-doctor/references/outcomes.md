# Outcomes, verdicts and metrics

The report prints outcome ids, not prose: `outcomes: admission_timeout 4,
client_idle_timeout 2`. This file is the vocabulary behind those ids, the five
verdict words, and the two metrics that carry the diagnosis. The table below is
the whole set of ids. `explain` adds a sentence after an id, and `analyze` does
not.

## The outcome ids

| outcome | what it means |
|---|---|
| `aborted` | the caller cancelled the request. |
| `admission_timeout` | blocked at admission: it waited for room and the gateway refused it. It was never served. |
| `auth_error` | authentication failed. The key was not accepted. |
| `client_idle_timeout` | a stream went quiet and the harness's own idle budget stopped it. The silent overflow signature. See below. |
| `completed` | served. |
| `connection_error` | the connection failed at the transport. |
| `context_length_exceeded` | the request exceeded the model's context window. A sizing symptom, not load. |
| `harness_deadline` | the caller's deadline expired, or the harness called the provider unhealthy. The caller gave up, not the gateway. |
| `message_limit` | the account reached its message-count ceiling. Not concurrency. |
| `model_not_found` | the model id does not exist on this account: a stale id in a configuration file. |
| `org_concurrency_limit` | the response said CONCURRENT_REQUEST_LIMIT_EXCEEDED: the account's whole concurrent-request pool was in use. |
| `other_error` | an error whose text matched no class. Unclassified, not a diagnosis. |
| `payload_too_large` | the request body was refused as too large (413). |
| `rate_limited` | a bare 429: the account was at its concurrent-request capacity. |
| `request_timeout` | the client's own request timeout expired. |
| `unknown` | no error and no output tokens. Nothing is established about that request. |
| `upstream_error` | the service behind the gateway returned an error. |
| `upstream_unavailable` | the service behind the gateway was unavailable. |

## The five verdict words

The first row of `analyze` is `the wall you hit`. Its value is one of five words:

| verdict word | the wall it names |
|---|---|
| `you (this machine)` | this machine's own concurrent requests. |
| `your org (the account)` | the account's shared pool, held by traffic that is not this machine's. |
| `the gateway` | the gateway or the transport in front of it. No concurrency in it. |
| `not concurrency` | nothing in the window hit a concurrency wall. |
| `unknown` | the evidence did not settle it, and the headline says which half is missing. |

Quote the word as printed, with its parenthetical. Never upgrade `unknown` into
a diagnosis, and never turn `your org` into a claim about this machine.

## The two metrics that carry the diagnosis

The admission conversion metric is the share of interactive requests that were
served rather than blocked at admission: served over served plus blocked at
admission. It prints as `interactive admitted` in the attribution rows, and as
`admission conversion` in `--baseline prev`. Low conversion says the interactive
tier was oversubscribed. That count covers this machine only. Without a
denominator it reads `unknown`, or `-- (no requests)` for an empty window, and
never `0%`.

The starved-peer evidence metric counts stalled requests that saw another
request to the same model complete inside their own span. A non-zero value
means the model was serving while this request was not: the endpoint was healthy
and the request went unserved. No error string separates "slow" from "not
served". This row does.

## Whose number is it

Two source labels cover every capacity figure. A third state is neither:

- `(account)` is reported by the account through `luna-doctor capacity`, whose
  footnote says how the payload arrived: `(network; fetched 40s ago)` or
  `(cache; fetched 400s ago)`. The account is the first authority.
- `(declared plan <name>)` is the `--plan` declaration, used only when the
  account reports no number. A declaration, never a measurement.
- `(declared by --plan)` is the same declaration, when `--plan` was given as a
  plain number instead of a published plan name. `fix` prints `(declared)` for
  the same state.
- Neither is `unknown (not reported)`. The attribution row says which half is
  missing: no priority concurrency from the account, no `--plan` declaration.

Unknown is never zero and never a default. A zero prints only when the account
sends one, and then it means something: `background` at 0 reads "0 concurrent
requests (the account is entitled to no overflow)", and a null average queue
wait reads "no waiting that day". Account counts never sum with local ones.

Attribution can lean on a daily total, which covers midnight to midnight rather
than your window. When a verdict rests on one, quote `account sample time` beside
it, so the reader can see the reading is not contemporaneous.

## Which outcomes are concurrency, and which are not

Three ids name a concurrency wall directly: `admission_timeout`,
`org_concurrency_limit` and `rate_limited`. In each one the classifier matched a
refusal. The refusal means the request needed room and the account's pool had
none. So the fix is fewer concurrent requests from this machine, or the
`-background` variant of the model. The class comes from a text match on the
error. So quote it as the report's classification, not as a fact you read off
the string.

`client_idle_timeout` is the one you cannot settle from these logs. A quiet
stream stopped by the harness reads the same whether the gateway demoted it or
the connection died. The report calls it the silent overflow signature, not a
finding. Read it beside starved-peer evidence.

Every other id is not concurrency: the sizing ones, the message limit, auth,
upstream, the harness deadline, the transport classes, `aborted`, `completed`.
Only a refusal, a stall, a burst past a named width, or a cohort of
same-duration timeouts ending together moves the verdict to pressure.
`other_error` and `unknown` are unclassified: evidence neither way, so say that.

For the fix that follows from an outcome, see [fix-families.md](fix-families.md).
