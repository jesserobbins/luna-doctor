# After the fix: did the change work?

The user changed a configuration value and wants to know whether it worked.
The answer is a before, an after, and one of four words: `better`, `worse`,
`unchanged`, or `inconclusive` when there is not enough traffic to tell.

## Restart first, or you measure the old configuration

Every key `fix` emits takes effect only once the process that reads it restarts,
and that process is not always the harness. A process already running holds the
values it read at startup, so a measurement before a restart measures the
configuration the user just replaced. Two families need more than a harness
restart. Swamp keys are read by the swamp runner and by the shell that starts
it. A key that is read nowhere reads `unchanged` however often the harness
restarts. That covers claude-code's top-level timeout keys behind a custom base
URL, and an env var that shadows the file.

| agent | keys | read from |
|---|---|---|
| pi | `globalConcurrencyLimit` | `~/.pi/agent/extensions/subagent/config.json` |
| pi | `httpIdleTimeoutMs`, `subagents.defaultModel` | `~/.pi/agent/settings.json` |
| pi | `models[<id>].contextWindow`, `models[<id>].maxTokens` | `~/.pi/agent/models-store.json` |
| claude-code | the `env.` block: `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`, `CLAUDE_STREAM_IDLE_TIMEOUT_MS`, `API_TIMEOUT_MS`, `CLAUDE_CODE_SUBAGENT_MODEL`, `CLAUDE_CODE_MAX_OUTPUT_TOKENS`, `CLAUDE_CODE_MAX_CONTEXT_TOKENS` | `~/.claude/settings.json` |
| codex | `agents.max_concurrent_threads_per_session`, `agents.default_subagent_model`, `model_context_window` | `~/.codex/config.toml` |
| swamp | `SWAMP_EXTRACT_CONCURRENCY`, `SWAMP_LLM_CONCURRENCY` | `fix` names `<repo>/.env.swamp`. The live value resolves the env var, then `~/.swamp.yaml`, then that file. So read the shell and `~/.swamp.yaml` first |

## Corroborate the live process

- macOS: `ps eww -p <pid>` prints the process environment after the command.
- Linux: `tr '\0' '\n' < /proc/<pid>/environ` splits it one variable per line.

A failed read is unreadable. Permission denied, a process that already exited, a
missing `/proc` entry, an empty environment under `ps eww`: unreadable. It is
never clean and never broken. Say that the reading was not possible.

A key that lives in a configuration file and not in the environment appears in
neither place. Read the file, and call it the declared state, not proof of what
a given process loaded.

## Measure: `analyze --baseline prev`

`luna-doctor analyze --baseline prev` compares the current window with the
equal-length window immediately before it. Both come from the same logs. The run
opens no network and no store beyond what `analyze` already reads. `fix` ends
with a `verify` section that names this command. It appends two lines:

```
change, against the window before this one: requests: 41 -> 63 · blocked at
admission: 12 -> 3 · silent stalls: 2 -> 0 · ...
change verdict: better - better: blocked at admission fell from 29.3% (12 of 41
requests) to 4.8% (3 of 63 requests)
```

Cells read previous -> current with an ASCII arrow. The rows are requests,
blocked at admission, silent stalls, concurrency rejections (429), refusals at
the account limit, peak concurrency, admission conversion. A window with no
requests reads `-- (no requests)`. A conversion the logs cannot compute reads
`unknown`. Both are absences, not zeroes.

## The floor, and the word below it

The verdict runs on the smaller of the two request counts. The floor is 8
requests. Under it the word is `inconclusive`, and the reason names the small
count. Quote that word and that count. Two requests never print as an improvement.

## Nothing in this section implies causation

It is two windows of the same logs, side by side. The counts moving is the whole
claim. If the user wants the why, find the evidence: `explain` for one request,
`watch` for the minutes around the restart.

## What success looks like

- `change verdict: better`.
- `blocked at admission` came down as a rate, not only as a count.
- `silent stalls` and `concurrency rejections (429)` also fell, or held at zero.
- Both windows cleared the floor.
- The peak this machine held did not exceed the priority concurrency that
  `luna-doctor capacity` reports for the account. Peak is this machine's measured
  number and concurrency belongs to the account. The two are never summed, and
  neither one decides the verdict on its own.

`unchanged` is a real answer: the configuration value moved and the pressure
did not. `worse` is a real answer too, and it goes back to the user as one.

## How this playbook ends

Before: the window the user was reading when something felt wrong.
After: an equal-length window, after a restart, from the same logs.
Then one of two sentences: it worked (the verdict is `better`), or not enough
traffic to tell (the verdict is `inconclusive`). `worse` and `unchanged` are each
their own answer, said plainly rather than folded into either sentence.
