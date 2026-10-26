# Changelog

All notable changes to luna-doctor are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versioning policy:

- **MAJOR** — a change that makes existing config advice wrong or unsafe, or a
  breaking change to the `--json` envelope or CLI flags.
- **MINOR** — new commands, new harness support, new measurements.
- **PATCH** — corrections to wording or fixes that do not change the interface.
- `schema` in the `--json` envelope is bumped independently when the document
  shape changes, so a stored report can be read back correctly.
- Work in progress accumulates under **Unreleased**. The version is bumped
  only when a release is deliberately cut.

## [Unreleased]

### Added

- `luna-doctor explain` answers one question: why was that one request slow?
  `analyze` describes the window. Nothing described the single request that the
  user held. `explain` shows that request alone. It gives the start and the end,
  the length, and the result in words. It gives the count of concurrent requests
  on this machine when the request started, and the peak during the request. It
  says how many requests the same model served inside the span. It gives the
  occupancy of the account when a capacity sample covers that moment. The
  headline names the owner of the wait. The owner is this machine, the shared
  pool of the account, the gateway, or no wall at all. Every number carries a
  label that gives its owner. If you give no flag, `explain` selects the request
  that went wrong. `--blocked` selects the newest refusal, and a refusal is not
  a stall, because the gateway refuses at admission and the harness stops an
  idle stream. `--slowest` sorts by duration. A bare id selects a response id, a
  session id, or a run id. The module is `luna_doctor/explain.py`.
- `luna-doctor watch` gives a live view of an incident. It reads a 15-minute
  window on a fixed interval and prints one line for each change of state. The
  line gives the pressure classes that moved, the peak on this machine, and the
  occupancy of the account. A terminal gets a redrawn frame. A pipe gets
  append-only ASCII lines and no cursor control. `Ctrl-C` prints a summary of
  three lines and exits with the verdict of the last sample. `--count`, `--once`,
  and `--json` cover the scripted cases. The module is `luna_doctor/watch.py`.
- `analyze --for-issue` prints one block that you can send to another person.
  The block gives the tool version, the log schema, the window in UTC, and the
  stores that the run read. It gives the attribution verdict, the headline, and
  the rows with their labels. The tool redacts home paths, email addresses, and
  long opaque tokens before it writes the block. The block is safe for a support
  ticket, and it does not carry the layout of the machine. The module is
  `luna_doctor/bundle.py`.
- The attribution verdict. `analyze` and the default run now open with the
  question that a user asks: was the wall this machine, the account, or the
  gateway? The answer is one word: `you`, `your org`, `the gateway`, `not
  concurrency`, or `unknown`. The tool computes the verdict from the measured
  window and from the cached capacity payload. Each row gives the owner of its
  number as `this machine` or as `account`, because the account shares one
  concurrent-request pool and a machine can look idle from inside its own logs.
  `analyze --json` carries the verdict, the sentence, the rows, and the actions
  in a new `attribution` block. The `schema` value stays `4`, because no field
  was renamed or removed. The module is `luna_doctor/attribution.py`.
- The verification stage, `analyze --baseline prev`. The loop used to stop at
  advice. The report now prints a `change:` section that compares this window
  with the window before it. Both windows come from the same logs. The tool
  reads no network and no store. The section uses the form `blocked at
  admission: 12 -> 3`. If either window holds fewer requests than the sample
  floor, the verdict is `inconclusive` and the reason gives the small count. A
  result from two requests never prints as an improvement. The section never
  claims a cause, so the words *because*, *caused*, *proves*, and *fixed* never
  reach the output. The module is `luna_doctor/baseline.py`. `fix` now ends with
  this step: run the comparison after you restart the harness, and the output
  gives the result that means success.
- `--plan <plan name or number>` on `analyze` and `fix`. The account number
  leads. A declaration is the fallback when the tool cannot reach the capacity
  payload. If the tool knows neither value, the report says `unknown` and names
  the command that fetches the account value. `fix --json` now gives
  `findings.priority_concurrency` and `findings.priority_concurrency_source`
  with the resolved value. Earlier, the field was always null.

### Changed

- The advice no longer quotes a fixed width. `analyze` and `fix` printed "keep
  concurrent requests at or under 8". That number is the default of one plan,
  and `plan.py` already records that 8 is above four of the five published
  plans. The sentence now gives the priority concurrency of the account when the
  tool knows it. If you declare a plan, the sentence gives the value of that
  plan. If the tool knows neither value, the sentence names `luna-doctor
  capacity --online` and `--plan` instead of a number.
- The pressured headline now uses the outcome classes that set the pressure
  flag. Those classes are blocked at admission, silent stalls, refusals at the
  account limit, and 429s. Classes that did not fire are left out. Before, the
  headline named only the first two classes, so a window with rejections
  reported "0 request(s) were blocked at admission, 0 silent stall(s)" and
  exited `1`.
- The preface of `fix` no longer tells the agent to read a plan from `lunaroute
  whoami` or a remaining budget from `lunaroute usage`. Neither value exists.
  `plan.py` checks the first, and the second was a retired feature that brought
  back the spend framing that this project does not use. The preface now points
  at `luna-doctor capacity` for the account value.

### Fixed

- The chart is readable over a short window. Tick labels used to repeat, and
  they now gain seconds before the tick count goes down. A band that ended early
  gets a short mark at the edge instead of a diagonal line across the plot. The
  label of the priority-concurrency rule sits on a white plate, so you can read
  it over a dark band.
- `capacity` with a cold cache exited `2` and printed nothing. It now says that
  the cache is empty and gives the command that fills it.
- The default run claimed that it read the last hour of local logs, and it drew
  a chart, on a machine that had no logs. If the window holds no request, the
  run now says so, names `discover`, and draws nothing.
- The cap-copy guard in `tests/test_cap_copy.py` was pinned to `10`, so it
  missed the `8` that shipped. The guard is now independent of any number. It
  matches `at or under <n>`, `cap of <n>`, and `keep concurrent requests at or
  under`.
- `skills/luna-doctor/SKILL.md` now describes the tool that ships. The chart is
  one stacked-area graph, not two panels, and it draws no advisory-concurrency
  rule. Admission deadlines are per model and carry their sample counts, and
  there is no single 120.2s wall. The skill also describes the attribution line,
  `--plan`, and `--baseline prev`. It tells an agent to say whose number it
  quotes, and never to repeat the width of one plan as a gateway fact.
  `README.md` lost its version check from two releases ago, its promise of a
  latency panel, and its claim that codex is not analysed.

## [0.2.0-alpha.3] - 2026-10-24

### Added

- The sizing-misconfiguration family, `luna_doctor/sizing.py`: six rules that judge each routed model's declared sizing keys against the live catalog (`GET /v1/models`, served from the snapshot), with the harness's shipped default standing in when a key is absent — because the same absent key is wrong in *opposite directions* on different models. pi's defaults are code-verified at three call sites (context 128,000 / output 16,384): `deepseek-4.1-flash` unset runs at 12% of its window and 6% of its output budget, while `clef-flash` unset is *promised* 15.6× its real window — guaranteed `context_length_exceeded`, with compaction that never fires in time to save it. `sizing-unset-underuses-model` (an absent key while the catalog value is ≥2× the default), `sizing-exceeds-model-window`, `sizing-output-exceeds-model-max`, and `sizing-input-plus-output-over-window` raise findings on pi; `codex-sizing-window` fires on declared values only (`model_context_window` is V by observation, its default U); `claude-sizing-window` (`CLAUDE_CODE_MAX_CONTEXT_TOKENS`) and claude-code's output arm stay informational — a U-backed line is never a finding. The audit's discipline holds throughout: a model absent from the catalog is *unchecked* (the routing finding belongs to `model-not-lunaroute`), an absent config file means the default is in force and is judged, an unreadable one is unchecked never clean, a stale or missing catalog is unchecked for every rule, declared small is *correct* (a deliberate cap or an exact match never fires), and catalog lookup is per exact id — no sibling resolution, no `-background` inheritance. `fix` renders findings as the same instruction-only changes it has always emitted, and U-backed lines as recorded no-change rows; `analyze --json` carries the full ledger as an additive `metrics.sizing` block (schema stays 4). Corroboration joins the log half where the design specifies one: `context_length_exceeded` requests escalate the window rule's finding for that model, and a rejection naming `max_tokens` (pi's V wire field) escalates the output rule's — never the other way around.

## [0.2.0-alpha.2] - 2026-10-24

### Removed

- The `--share` WhatsApp surface and share-card PNG are suppressed: `analyze --share`,
  `--style share`, and the card output no longer exist. The renderer module and its
  unit tests remain, so the surface can return when it earns its place. The README
  no longer documents it.

### Added

- The account's real capacity, from LunaRoute's `GET /v1/capacity`. One data-collection layer in `luna_doctor/sources.py`: `fetch_capacity()` beside the existing fetchers, cached to disk for 5 minutes (the endpoint answers in 4–17s and rate-limits at 60/min, so nothing fetches per-render), provenance-carrying (`account` — a new sixth provenance word for fetched-account values, beside measured/declared/catalog/configured/unknown), degrading to unknown — never zero, never a default, every null preserved as the API's own story. `luna-doctor capacity` renders the API's four pages as a first-class report — now (live occupancy, ledger health, saturation), config (plan, priority and background concurrency, overflow routing), history (7d/30d occupancy rollups), and admission (per complete UTC day: accepted / deferred / denied / denied_after_wait, with a null `avg_queue_wait_ms` rendered as no waiting that day, not 0) — and `capacity --online` is the one command that refreshes the cache; every other consumer reads the cache and never touches the network. `analyze` gains an additive `metrics.capacity` block: plan, concurrency and overflow routing, plus the server's admission counts for the window's complete UTC days placed beside the local outcome counts, with `denied_after_wait` rendered as corroboration for the park-deadline rejection class — the paired “local peak 17 · account limit 12” line included. The doc page's semantics are stated wherever server counts sit beside local ones: `days_active` is not `days_covered` (idle days are covered but not active), the overflow-routing caveat says overflow is a capacity allowance, not a promise, and shared-pool exclusions (requests on your own provider keys, embeddings, dedicated capacity) mean the two counts are never reconciled. The chart's priority-concurrency rule now resolves from the account's cached `priority_lanes`, labelled “(account, plan apex)”. A canned live payload — captured once, 2026-10-23, with its null and degraded fields intact — is committed under `tests/fixtures/` so the offline suite tests against the API's real shape.
- Bare `luna-doctor` is a friendly default run. It opens with the account line from the capacity cache — plan, priority concurrency, background concurrency, overflow routing, or `account: unknown (fetch with: luna-doctor capacity)` when the cache is cold — then a short plain-language welcome: what LunaRoute is (a gateway in front of your models; capacity measured in concurrent requests, not tokens), what this run did (read the last hour of your local agent logs; nothing was sent anywhere), and where the rest of the picture lives, one line per command (`analyze`, `chart`, `fix`, `capacity`). Then it runs the one-hour analysis and draws the chart beside it. The exit code is the analysis verdict; a chart that cannot be drawn says so and steps aside.
- `chart` (and the default run) writes a PNG beside the SVG — the same scene rasterised by the package's own renderer, zero new dependencies — so the picture can be pasted into a chat, a note or a doc with no font-substitution worries. Both paths print in the summary; `--out` names the SVG and the PNG takes the same stem; `--no-image` suppresses the PNG; `chart --json` carries `png_path` and `png_bytes`.

### Changed

- The capacity API is the first authority on config. The resolution order for every account fact — priority concurrency, plan, background concurrency, overflow routing — is: live/cached capacity API first, then a `--plan` declaration (the fallback for when the API is unreachable or reports no number), then unknown. This reverses the earlier order, where an explicit `--plan` won: a declaration that disagrees with the account's value is now ignored, and the chart footer and console note say the declaration was set aside because the account reports otherwise.
- The burst-measurement command is now `load-test`, gated by `--load-test-enable` (on it and on `compare`, which keeps its name and now says what it really is: load-testing a model and its -background twin). It is out of the README and the skill: the earlier name understated what the command does — it opens live concurrent sessions against the gateway and spends included inference — so it stays opt-in, its refusal message now says exactly that, and it remains listed in `--help` for those who go looking.
- The `chart` picture is one stacked-area graph. The previous design answered "whose traffic is this" with a row of pictures — the whole machine, then one per agent — which split one number across several canvases and put the account total only in the first. Now a single time axis and a single stack carry the whole machine: the stack's height is the account's total concurrent inference requests — what the gateway prices — layered by band, and a band is one model's traffic from one agent in one scheduling class, with the agent named in the band label ("glm-5.3 · pi") whenever more than one agent has traffic. Band order is fixed and deterministic: every priority-scheduling band below every background-scheduling band, then by peak thickness within a class, so big steady bands base the stack and thin ones cap it. The marks that earned their place stay on the one canvas — the grey per-column peak envelope, the open circle at the exact peak, red flare ticks at the moment the gateway rejected a request, the dashed priority-concurrency rule drawn when its number is known (the account's, or a declared one), direct band labels with hairline leaders, and the wrapped provenance footer — and the folded "other" tail now folds per class and agent, so a band's agent identity survives the fold. A window where no agent had timed traffic draws the one plot area over the requested window — empty axes and the honest footer — instead of a bare message.

## [0.2.0-alpha.1] - 2026-10-23

### Added

- One data-collection layer, `luna_doctor/sources.py`. Every fact a command can quote now comes from one of three states of the world — **measured** (the local log stores: pi, claude-code, and now codex rollouts), **declared** (the harness config files: pi settings, pi subagent config, pi model stores, claude-code settings, codex `config.toml`, swamp knobs), and **catalog** (`GET /v1/models` on the gateway, cached on disk for 24h) — plus **configured** for documented constants and **unknown** for everything else, and every value carries its provenance: the file and key it came from, the endpoint, or the citation behind an "absent → default N". `analyze` and `fix` consume it as one combined snapshot (`metrics.snapshot` in the `--json` envelope): each harness's declared model keys join the live catalog per model, so a declared size can be compared with the real one, token totals are summed per harness under the field names each harness logs (never merged: codex's `input_tokens` includes its cached tokens while pi's `input` excludes `cacheRead`), and a model the logs used but the catalog does not list is flagged as a finding.
- `--online` / `--offline` on `analyze` and `fix`. The default is offline: a cached catalog serves (a stale one too, marked stale), and the network is never touched. `--online` allows one authenticated GET of the model catalog — refreshing the 24h disk cache at `~/.luna-doctor/catalog.json` — and probes the not-yet-deployed `GET /v1/limits`. Any failure degrades to unknown, never to an error: what could not be fetched is reported as such.
- The `chart` picture is redrawn on Tufte lines to draw only what the logs measure: a row of stacked-area panels on one shared time scale — the whole machine first, then one panel per agent, each stacking concurrent inference requests by model and scheduling class (blues for priority, violets for background, red reserved for rejection). Every mark carries one fact: the stacked areas are the average concurrent requests per time column per model — an honest load, not a sum of peaks that never co-occurred — a grey outline is the busiest moment per column, an open circle is the exact peak and when it happened, flare ticks hang one per request the gateway rejected or overflowed from the owning agent's panel at the moment it died, bands are direct-labelled at their right-hand edge in their own ink with no legend box, and the footer carries the provenance — window, version, each model's measured rejection deadline with its sample count, and which numbers are declared rather than measured. The two lines the old chart could not defend are gone: no universal admission deadline, and no advisory-concurrency rule. What depends on the account's plan is declared, never inferred: `--plan` on `chart` accepts a plan name (LunaRoute's published defaults: personal 1, pro 2, max 4, apex 6, teams 10) or a plain number, and the dashed rule is drawn only when declared, on the aggregate panel only, labelled with the declaration so a declared threshold can never be mistaken for a measured one.

### Breaking

- `fix --json`: `findings.fanout_limit` is replaced by `findings.priority_concurrency`, and the envelope `schema` is now `4`. The removed field carried the tool's advised number under a name that was the owner's own shorthand for capacity; it never had a stable consumer, so nothing was preserved for compatibility. The new field is a different quantity — the account's actual priority concurrency allowance, in concurrent requests — and it is `null` until the plan-derivation pass can read the account's plan: never `0`, never a guessed default. `contract.advised_fanout_max` is renamed `contract.advised_concurrency` in the same sweep. While the project is prerelease, `schema` is bumped freely; the envelope has no consumers to protect yet.

### Changed

- The capacity vocabulary is LunaRoute's own documentation language, everywhere a user reads it. "Fan-out" — the owner's personal shorthand, never a LunaRoute term — is gone from every rendered surface (reports, the pretty renderer, share text, fix output, chart SVG, README and the skill), and "lane", which is an internal API field name rather than product language, is gone with it. Copy now says **concurrent requests** for what the gateway counts, **priority concurrency** for the account's allowance, **park or overflow into background scheduling** for what happens past it, and **rejected** for a `429` `CONCURRENT_REQUEST_LIMIT_EXCEEDED`. The advice reads "keep concurrent requests at or under 8" / "keep concurrent requests within your priority concurrency allowance"; the `INTERACTIVE_BURST_CAP` arithmetic is unchanged and will be re-derived from the account's plan in a separate pass.
- The internal spelling follows the copy: `ADVISE_FANOUT_MAX` is now `ADVISED_CONCURRENCY` (also `contract.advised_concurrency` in the `--json` envelope), the swamp scratch key is `_swamp_configured_concurrency`, and the `ADMISSION_DEADLINE_S` alias — kept only for a chart-block compatibility that no longer exists, since both files ship in one package — is removed in favour of `ADMISSION_DEADLINE_GLM53_S`.
- `schema` in the `--json` envelope is bumped 3 → 4. One part is additive — the new `metrics.snapshot` document (measured + declared + catalog, joined per model, with a provenance legend) — and the only keys that moved or were removed are the Breaking renames above.

## [0.1.1-alpha.3] - 2026-10-23

### Added

- `--copy` on `discover`, `analyze`, `probe`, `compare` and `fix`: put the printed text on the clipboard as well as on stdout, via the first of `pbcopy`, `wl-copy`, `xclip`, `xsel` found on PATH. The copy is always plain text — an explicit `--style pretty` renders plain for the whole run, and `--copy --json` copies the JSON document exactly — and stdout is unchanged, so a machine with no clipboard tool (or a failed copy) never loses the answer: stderr says `copied N bytes to the clipboard via pbcopy` or `no clipboard tool found (pbcopy, wl-copy, xclip, xsel); printed instead`, never an error and never a changed exit code. `analyze --share --copy` copies the WhatsApp message, while the card is still written and its path printed to stderr; `fix --copy` copies the whole block, apply lines verbatim. Clipboard integration is a deliberate exception to the "no subprocess" rule: that rule was about rasterising images, whereas the clipboard is an OS affordance, used only when explicitly asked for with a flag, never as part of normal output.

### Fixed

- The package parses on Python 3.9 again. `scene.py` emitted SVG through multi-line f-strings whose expressions nested same-quote string literals — PEP 701 syntax, valid only from 3.12 — so on Python 3.9–3.11, macOS's system `python3` included, every command died at import with `SyntaxError: EOL while scanning string literal` despite `requires-python = ">=3.9"`. The emitters hoist their attribute string into a local first. Three guards now cover it: an `ast.parse(..., feature_version=(3, 9))` sweep for gated grammar, a tokenizer check for PEP 701 f-strings, and — where an older interpreter is installed — a real `compileall` with it. The tokenizer and interpreter guards were each confirmed to fail against the pre-fix file.

## [0.1.1-alpha.2] - 2026-10-23

### Added

- `--style` on `discover`, `analyze`, `probe`, `compare` and `chart`: `auto` (the default) draws the pretty layout only on a terminal that is not `NO_COLOR`, `pretty` asks for it down a pipe too, and `plain` is undecorated text. `NO_COLOR` beats an explicit `--style pretty`, `CLICOLOR_FORCE` colours a pipe, and `--json` is never styled — no `Style` is constructed on that path. `chart` takes the flag for consistency; its console output is two plain lines and the picture is the SVG.
- `--enable-probe` on `probe` and `compare`. Without it neither command opens a burst: each states how many concurrent sessions it would open, on which model, for how long, and exits `2`.
- `analyze --share`: the report as a WhatsApp-ready message on stdout — a quoted headline, at most five `*label:* value` lines, at most three decision-relevant bullets and one provenance line — plus a 1080x1350 PNG share card. The card's path is printed to stderr, so stdout alone is the pasteable artifact, and `--out` chooses where the card is written.

### Changed

- `discover`, `analyze`, `probe` and `compare` render from one report model: `report.py` builds the headline, `label: value` sections, adjustments and footnote, and `render_plain` / `render_pretty` draw it. Rows are no longer column-aligned, so a long value cannot break the layout and output survives a narrow terminal, a pipe, or a chat window that rewraps.
- Swamp appears only when it is in use — files under `.swamp/data` or `.swamp/workflow-runs` written inside the window. A populated but idle store is installed, not running, and `analyze` no longer reports it. When swamp is in use, its fan-out is stated as the configured extract + verify setting and where that setting came from, never as measured concurrency.
- Swamp-only pressure no longer sets exit 1. Pressure is measured, so a warning built from a configured fan-out does not: the combined pi + swamp total and the cap it was held against are gone from `analyze` entirely. Overlapping swamp work is an unmeasured risk, never a number the report computes.
- Copy no longer states an interactive cap as a gateway fact, and no longer tells the reader to go and measure it. `analyze`, `probe`, `chart` and `fix` say the admitted width is specific to the account and cannot be read from these logs. `probe` remains available behind `--enable-probe`, as a guarded tool rather than a suggested next step.
- Outcome copy says "blocked at admission" rather than "died on admission" or "admission deaths" — the request was refused at the gateway's admission deadline because the account was at its concurrency width. The `--json` keys keep their existing names.

### Fixed

- A demotion now carries its reason. `probe` splits its demoted count into `overflow` (the interactive width spilled the request into background) and `no_entitlement` (a `-background` request had no background entitlement and was downgraded into the priority pool, consuming interactive headroom). `fix` says plainly when background traffic is being served on priority concurrency, and hedges when no probe has seen the served tier.
- `Request.saturated` no longer consults `stream_stalled`, an outcome nothing emits.
- Installed copies can run `fix` and `chart` again. The CLI was packaged as a single script, so the two command files it loads from its own directory were left behind in the checkout and both commands answered `support missing` (exit 2). The CLI is now a proper package (`luna_doctor/`) with a console-script entry point, and `bin/luna-doctor` is a launcher that resolves its own symlink for the no-packaging install path. Tests now cover the pipx, package and symlink layouts instead of only the checkout.

## [0.1.1-alpha.1] - 2026-10-22

### Added

- `--pi-root` / `--claude-root` (repeatable): read pi-format or claude-code stores from mirrored archives instead of the live defaults.
- Per-model admission deadlines (measured per model; `unknown` when too few observations), reported in `metrics.per_model_deadlines`.
- Timeout-storm detector, saturation bands (with minimum-sample guard), unknown-error surfacing, wall counts + slow completions, and extraction persistence with an append-only `findings.jsonl` ledger.
- New outcome classes (`org_concurrency_limit`, `model_not_found`, `context_length_exceeded`, `payload_too_large`, `harness_deadline`, `message_limit`) and `w`/`y` duration units for `--since`.
- Swamp visibility: `discover` reports `stores.swamp`, `analyze` emits `metrics.swamp` with a combined-pressure warning, `fix --agent swamp` advises `SWAMP_EXTRACT_CONCURRENCY` / `SWAMP_LLM_CONCURRENCY` with a combined budget, `probe` adds a swamp headroom note. Schema → 3.
- `luna-doctor chart` (standalone SVG) and minute-level concurrency series in `analyze`.

### Changed

- License changed from MIT to Apache-2.0.
- Releases use prerelease tags (`v0.1.1-alpha.1`); documentation reflects the alpha status.

## [0.1.0] - 2026-10-21

### Added

- `luna-doctor analyze` — reconstructs each model request from pi and claude-code session stores (read-only) and classifies outcomes against measured gateway behaviour.
- `luna-doctor fix` — per-agent copy-paste config for pi, claude-code and codex, backed by keys verified in the installed bundles or their shipped docs. Each block backs up, validates, and merges to a temp file before moving into place.
- `luna-doctor probe` / `compare` — live burst measurement capturing tier and demotion headers; shows how the interactive tier and its `-background` twin behave under load.
- `luna-doctor discover` — what log stores exist, how big, how fresh.
- `skills/luna-doctor/SKILL.md` — the agent-facing skill.
