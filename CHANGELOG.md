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

## [0.6.0-alpha.9] - 2026-11-04

### Fixed

- The report died with an `IndexError` when a request started before the
  earliest completion in the window. The trend columns are computed against the
  window of completions, and only the right end was clamped, so a long request
  that began earlier put a negative index into the in-flight count. Both ends
  are clamped now.
- A failure inside the trend no longer takes the report with it. It renders as
  "the trend was not read in this window", with the reason, and the rest of the
  report stands.

## [0.6.0-alpha.8] - 2026-11-04

### Changed

- Every surface keeps the Simple English rules now, and a test enforces them.
  The documents under `docs/` and the copy that `analyze`, `capacity`, `latency`
  and `fix` print were unchecked. They are scanned now, each run in a throwaway
  home so a leftover cache cannot make the guard flaky.
- The report, the capacity footer, the latency table and the fix advice split
  their semicolons into sentences and use can or must where they used a modal.
- The 2026-10-23 capacity design note was rewritten in Simple English.

## [0.6.0-alpha.7] - 2026-11-04

### Added

- `analyze` reads the local agentsview archive, when it is installed:
  `agentsview export hour` gives one closed UTC hour at a time with 5-minute
  buckets carrying how many agents were in flight, split interactive, subagent
  and automated, with output tokens and reported model spend. That is every
  agent the archive holds, not only the two log stores this tool parses.

  It stops at the last closed hour, so the live hour stays this tool's own
  reading of the logs. A missing binary, a failed query or an empty window reads
  as unavailable with a reason, never as zeros. `metrics.agentsview` carries
  the closed hours, buckets, peak agents, cost, per-model usage and the count of
  rows the archive did not price.

  Its spend is model spend for traffic on your own keys and is never summed with
  a LunaRoute number, which bills concurrency.

## [0.6.0-alpha.6] - 2026-11-04

### Fixed

- The bare run acknowledges before it can sit silent. With no arguments the
  tool printed nothing until the account answered, and that wait runs to a
  minute. It now says what it is about to do before the account line, and the
  analyze and chart calls it makes are marked as already acknowledged, so the
  notice appears once. It still stays off a pipe.

## [0.6.0-alpha.5] - 2026-11-04

### Added

- `luna-doctor 15m` is the command: a leading duration is rewritten to
  `analyze --since 15m`, and flags after it still work.

- A working notice on stderr when stderr is a terminal, so a run that waits a
  minute on the account does not read as broken. Nothing is printed to a pipe,
  and `LUNA_DOCTOR_QUIET=1` turns it off on a terminal too.

- The window's shape in one line: a sparkline of each column's p90 with the
  range beside it, in the report's `TREND` section, because the picture is a
  file and a file has to be opened.

### Changed

- The bare run prints the answer first and the pointer lines after.

## [0.6.0-alpha.4] - 2026-11-04

### Changed

- The answer is in the terminal. A `TREND` section now leads the report: the p90
  of the first third against the last third says whether duration moved, the
  correlation between in-flight count and p90 says what it tracks, the three
  slowest requests are named, and the section states that these logs hold no
  first-token time. It used to live only in the chart's footer.

- The bare run writes its SVG and PNG to one stable path,
  `~/.luna-doctor/last/chart.svg` and `.png`. Each run overwrites them, so no
  timestamped files land in whatever directory the tool was called from. The
  welcome text is four lines instead of nine.

## [0.6.0-alpha.3] - 2026-11-04

### Changed

- The chart asks the fifteen-minute and one-hour question first. Up to two
  hours it draws a request timeline: one bar per request, sorted by start,
  length as duration, coloured by tier with refusals in red, labelled with its
  duration and model. Past 36 requests the slowest are kept and the count of the
  rest is printed. Beyond two hours the distribution forms stay: admission fates
  per column, the duration heatmap with its completion bar, and the concurrency
  stack.

- The footer answers "is it increasing, and what does it track" in words: the
  p90 of the first third against the last third, and the correlation between a
  column's in-flight count and its p90. It also states the limit: these logs
  hold no first-token time, so a slower first byte and slower generation look
  alike.

## [0.6.0-alpha.2] - 2026-11-04

### Changed

- The chart was rebuilt on observability practice. The p90 line is replaced by a
  duration heatmap: log-scale rows labelled on octave edges, columns at the
  measurement resolution, colour as the count in each cell, p50 and p90 traced
  over it, and a completion bar beneath. A percentile line cannot separate
  "everything got three times slower" from "one percent started taking twenty
  minutes", and those need different fixes.

- Only completed requests enter the duration distribution. Refusals used to
  enter it, so a 200 ms 429 pulled the percentile down and a 120 s admission
  timeout pulled it up, and neither said anything about the served traffic.

- The account's day-granular counters read as a sentence in the footer instead
  of a step line, because a daily count on an hourly axis asserts a step
  function the data does not contain. The top panel now counts what happened to
  each request in the same columns: served, demoted, refused at admission,
  refused at the account's limit.

- The footer carries the heatmap reading key: a vertical stripe is a change at
  one moment, a horizontal band is several modes at once, a diagonal drift is a
  slow shift.

## [0.6.0-alpha.1] - 2026-11-03

### Added

- `luna-doctor latency` prints the latency table: one row per cell with n, p50,
  p90 and max, split by `--by model|harness|outcome` and filtered by `--tier`.
  The matrix existed only inside the `--json` envelope, so the report's three
  lines were the whole readable story and answering "which model is slow" meant
  parsing JSON by hand.

  Every number is measured on this machine: LunaRoute's API reports no request
  latency, no first token and no percentile, so nothing here is quoted from the
  account. A cell under the sample floor is counted and left out of the table
  rather than printed as if it were a rate.

### Changed

- `analyze` keeps three bounded lines in its latency section and names the
  command that carries the rest.

## [0.5.0-alpha.1] - 2026-11-03

### Changed

- The report leads with the account. `SERVICE` comes first: plan and width,
  the overflow pool with its routing, both live pools with the sample time they
  were read at, and the admission record with the count of requests denied
  after waiting. Every number in it carries `(account)`, and a stale payload
  says in its own row that it describes another moment.
- `analyze`, `fix`, `chart` and the bare run ask the account for its capacity
  by default. The cache serves first and the network is touched only past its
  5-minute TTL, so a warm run answers at once. `--offline` is the escape on all four
  commands. The bare run gains the flag too, which it never had.
- Five published claims corrected against the gateway's own documentation: a
  429 is not always the account's concurrency, other quota dimensions exist,
  the overflow pool is where overflow traffic runs rather than where a demoted
  request waits, the 5-minute figure is the server's history cache, and the
  keep-alive cadence with two response headers are measured here and
  undocumented by LunaRoute. The 60 s documented park against this machine's
  measured 120.2 s remains unresolved, and says so.

### Added

- The insufficient-evidence outcome. When the account reports pressure and
  this machine cannot explain it, the report answers in three parts: the
  account's claim with its time, which local evidence is absent and why, and
  the two moves that produce an explanation. `fix` states that it has no
  concurrency change and why, then prints the config audit. That audit is a
  question about declared settings, never a guess from account numbers.
- `metrics.latency_by`: latency per (tier, model), (tier, harness) and (tier,
  outcome) from local logs only, with a sample floor. Cells below the floor are
  counted, not printed. The report carries three bounded lines.
- The chart draws two panels on one time axis: the account's admission record
  as a step line of denied-after-waiting per complete UTC day, above this
  machine's concurrency stack.
- The server's own `generated_at` rides beside the client's fetch time, and the
  capacity timeout is 90 s: four live samples ran 3.3 s, 9.7 s, 37.8 s and
  39.3 s, so the old 30 s failed on half the honest responses.

## [0.4.0-alpha.1] - 2026-11-03

### Added

- The wheel carries the agent skill. `pipx install luna-doctor` used to give a
  command with no skill beside it: the wheel held `luna_doctor/` and stopped
  there, and `tests/test_skill_package.py` read the checkout, so the guard
  passed while the artifact was incomplete. The skill now lives at
  `luna_doctor/skills/luna-doctor/` and `[tool.setuptools.package-data]`
  declares it, so it ships by construction rather than by a build step. The
  repository keeps `skills/luna-doctor` as a symlink to that path, which is
  where the skills tooling looks for one.

- `luna-doctor skill` prints the packaged path and the copy line per harness.
  It prints and never copies, the same contract `fix` keeps.

- How many subagents ran at once is measured. pi writes each async subagent's transcript at
  `<store>/<session>/subagent-artifacts/<runId>_<agent>_transcript.jsonl`, and
  no record there carries `type`, so the request parser read none of them. The
  report gains a `subagents` row with the count of runs, the peak number open
  at one instant, the widest single launch, and how many runs exited non-zero.
  `metrics.subagents` carries `runs`, `peakInFlight`, `peakAt`, `byAgent`,
  `byParent` and `failed`. It is a count of runs and it never joins a request
  number, because eight subagents each issue their own requests and those are
  already counted from the logs.

## [0.3.0-alpha.6] - 2026-11-03

### Changed

- Swamp is opt-in. It kept run history per repository, and nothing on the
  machine lists the repositories a person uses swamp in, so the tool had to
  guess: it walked up from the working directory, then tried `~/.swamp` and two
  sibling directory names. On any other machine that guess either found nothing
  or found the wrong repository, and the guess decided a number the report told
  the reader to set. A swamp repository now enters a report only when the run
  names one with `--swamp-root DIR`.
- Inside a named repository, use comes from swamp's own run tracker, not from
  file times. `<repo>/.swamp/run_tracker.db` holds one row per method and
  workflow run with `started_at`, `completed_at` and `status`, so a run that
  started before the window closed and finished after it opened counts as in
  flight during the window. A store with thousands of old files reads idle. A
  repository without a tracker reads `installed, no run tracker`, and a corrupt
  one says it is unreadable. The database is opened read-only, and swamp is
  never spawned.
- The install is one command. `pipx install luna-doctor` resolves from PyPI,
  and the checkout path stays as the way to run an unreleased commit. The README
  no longer prints a version number in the install block, because the number
  drifted from `VERSION` twice.

### Added

- `--swamp-root DIR` on `discover`, `analyze`, `fix` and `chart`.
- `metrics.swamp.runTracker` carries what the tracker said: `overlapping`,
  `in_flight`, `pending` and `last_started`. The envelope schema moves to 5.
- `scripts/publish.sh` builds the tagged release, uploads it with the token
  from `~/.config/luna-doctor/pypi-token`, and then reads the index back to
  confirm that the version is live. It refuses a dirty tree and a missing tag,
  so an upload cannot describe a commit that nobody can find.

## [0.3.0-alpha.5] - 2026-11-03

### Added

- The package declares its license as an SPDX expression, `Apache-2.0`, and
  ships the license text beside it. PyPI reads that field in a sidebar. The
  earlier form put the whole Apache text in the field, so the page opened with
  boilerplate.
- `[project.urls]` names the repository, the issue tracker, the changelog and
  the documentation. The `Homepage` entry pointed at a personal site and gave a
  reader no path to the code.
- A `Development Status :: 3 - Alpha` classifier states what every report
  already prints.

### Changed

- The README carries absolute URLs for its two chart images and for the install
  document. The README is the description on the PyPI page, and a relative path
  leads nowhere there.
- The install document says where the agent skill lives. The wheel holds
  `luna_doctor/` only, so the skill stays in the repository at
  `skills/luna-doctor/SKILL.md` until packaging carries it.

## [0.3.0-alpha.4] - 2026-11-03

### Fixed

- The unresolved verdict no longer sends a reader to fetch an account sample
  that the report already holds. When the cache carries a fresh sample, and
  that sample sits below the cap, the headline now states the reading and says
  plainly that the sample does not corroborate a full pool. The earlier copy
  said "the account half gives nothing that says whether the pool was full"
  while the rows underneath it printed `in use 8 of 12 priority concurrent
  requests (account)`. The verdict is unchanged: a reading below the cap still
  does not name an owner, and the new sentence says which fact is missing.
- The two unresolved branches printed the count noun twice, as "2 requests
  requests refused or stalled" and "1 request request stalled", because the
  sentence supplied its own verb phrase after `_requests` had already supplied
  the noun.

### Added

- `capacity` reports the account's overflow pool, not only its priority pool.
  The API sends the live overflow occupancy inside `now`, and its own 7-day
  and 30-day measures inside both history windows. Each pool now gets its own
  row, and the report never adds one pool to the other. A request demoted to a
  background tier waits in the overflow pool, so it never appears in the
  priority pool's in-use count. The old report said that and then left a
  reader with no way to see the pool that the demoted request was sitting in.
- The two null shapes of a pool cap read as two different facts. A null cap
  means the cap was not read, so the report claims nothing about the pool. A
  zero cap is a closed pool, which is never saturated and always reports 0
  available. A payload captured before the account reported the overflow pool
  carries no such block, and that reads as unknown, never as a pool with no
  capacity.
- `metrics.capacity` carries the same overflow facts in the `--json` envelope:
  `now.background` verbatim, and `overflow_pool` with the 7-day and 30-day
  measures beside their priority counterparts. `analyze` adds one adjustment
  line when a fresh sample shows the overflow pool at its cap, because that is
  the pool a paused demoted stream waits in. A stale sample says nothing,
  because it is a fact about a different moment.
- A new fixture, `tests/fixtures/capacity-2026-11-02.json`, holds the live
  payload captured on 2026-11-02, the first one this suite holds that reports
  the overflow pool.


## [0.3.0-alpha.3] - 2026-10-28

### Removed

- The `load-test` and `compare` commands are gone, with the `--load-test-enable`
  gate that guarded them. They opened real concurrent sessions against the
  gateway to measure a width. That is the one thing this tool must not do. It
  reads what a machine already did, and a diagnostic run must not add to the
  pressure it is asked to explain. The width keeps two honest sources, the
  account through `luna-doctor capacity` and a `--plan` declaration, and neither
  of them opens a session.
- The skill and the README no longer describe such a command. The guidance now
  says that the tool does not generate load at all.
  `tests/test_skill_package.py` fails if a skill file names a command that the
  parser does not serve, so the surface cannot return by accident.


## [0.3.0-alpha.2] - 2026-10-27

### Added

- `discover` reports a local `agentsview`, when that command is installed. It
  gives the number of projects and sessions the index holds. The tool reads it
  with one `agentsview projects --json` query. It starts no daemon, serves
  nothing and syncs nothing, because a diagnostic run must not change the data
  it reads. An installed command that does not answer is reported as
  `present · unreadable` with the reason, never as absent and never as a count.
  The reports keep reading the harness logs. agentsview is a second view of
  session history, so it tells a rotated or moved store apart from a quiet
  machine.

### Changed

- Every surface that a person reads now follows Simple English, in the spirit of
  ASD-STE100: short sentences, active voice, simple tenses, one word for one
  meaning. The change removes 385 em-dashes, semicolons, contractions, modal
  forms and bold lead-ins from the README, the agent skill and the newest
  release notes, and 10 more from the copy that the CLI prints. A release note
  from an earlier version stays as it was written, because it is a record.
- `scripts/check_simple_english.py` finds the violations that a machine can see,
  and `tests/test_simple_english.py` runs it over the README, every skill file
  and the newest CHANGELOG section. A new em-dash in the README now fails a
  test.

### Fixed

- The two chart images in `assets/` come from a generated log store, not from an
  account and not from a real session. The first version of those images carried
  a real plan name and a real account width in the dashed rule.
  `scripts/make_demo_charts.py` builds the store from an hour-of-week rhythm, a
  mix of models and tiers, and a small share of refusals. It leaves the capacity
  cache empty, so the rule is a `--plan teams` declaration and the picture states
  that it is declared. The seed is fixed, so the images regenerate.
- The advice copy writes "belongs" where it used to write a modal, and the
  prerelease banner uses a comma instead of an em-dash.


## [0.3.0-alpha.1] - 2026-10-27

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
  that went wrong. `--blocked` selects the newest refusal. A refusal is not a
  stall, because the gateway refuses at admission and the harness stops an idle
  stream. `--slowest` sorts by duration. A bare id selects a response id, a
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
  number as `this machine` or as `account`. The account shares one
  concurrent-request pool, so a machine can look idle from inside its own logs.
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
  this step: run the comparison after you restart the harness. The output gives
  the result that means success.
- `--plan <plan name or number>` on `analyze`, `explain`, `watch` and `fix`.
  `chart` already had the flag. Every command that reads the account's capacity
  can now be given a number to use when that read returns none. The account
  value leads. A declaration is the fallback when the tool cannot reach the
  capacity payload. If the tool knows neither value, the report says `unknown`
  and names the command that fetches the account value. `fix --json` now gives
  `findings.priority_concurrency` and `findings.priority_concurrency_source`
  with the resolved value. Earlier, the field was always null.

- The agent skill is a package, not one file. `skills/luna-doctor/SKILL.md` is
  now a 76-line router: the invariants, a symptom table, the command index and
  the exit codes. The depth lives in `skills/luna-doctor/references/`. It holds
  five playbooks that each end in a verdict (slow right now, one bad request,
  too wide, after the fix, share the evidence). It also holds four catalogs
  (outcome ids and verdict words, the measured gateway contract, the `fix`
  families including the sizing rules, the chart anatomy). An agent loads one
  branch of that instead of 280 lines of everything. Three gaps closed on the
  way. The sizing family had no words at all, so a `400 maximum context length`
  had nothing to connect it to `fix`. `explain`, `watch`, `analyze --for-issue`,
  `--baseline prev` and `--plan` were undocumented. Nothing compared the prose
  against the parser.

### Changed

- The advice no longer quotes a fixed width. `analyze` and `fix` printed an
  instruction to keep concurrent requests to 8. That number is the default of
  one plan, and `plan.py` already records that 8 is above four of the five
  published plans. The sentence now gives the priority concurrency of the
  account when the tool knows it. If you declare a plan, the sentence gives the
  value of that plan. If the tool knows neither value, the sentence names
  `luna-doctor capacity --online` and `--plan` instead of a number.
- The pressured headline now uses the outcome classes that set the pressure
  flag. Those classes are blocked at admission, silent stalls, refusals at the
  account limit, and 429s. Classes that did not fire are left out. Before, the
  headline named only the first two classes. A window with rejections then
  reported "0 request(s) were blocked at admission, 0 silent stall(s)" and
  exited `1`.
- The preface of `fix` no longer tells the agent to read a plan from `lunaroute
  whoami` or a remaining budget from `lunaroute usage`. Neither value exists.
  `plan.py` makes sure that the first value exists. The second was a retired
  feature that brought back cost framing which this project does not use. The
  preface now points at `luna-doctor capacity` for the account value.

### Fixed

- Skill drift is a test failure now, not a review note. `tests/test_cap_copy.py`
  and `tests/test_terminology.py` walked one hardcoded `SKILL.md` path, so every
  new reference file stayed outside the scan. Both now walk every markdown file
  under `skills/luna-doctor` and fail if the walk finds nothing, with their
  patterns unchanged. A new `tests/test_skill_package.py` parses every
  subcommand's `--help`. It fails on a documented flag that the parser does not
  carry. It fails on a relative link that resolves to nothing from the file it
  is written in. It fails on a reference the router never links. It fails on a
  command name that does not exist (the retired `probe` was in the invariant).
  It fails on a router past its 150-line, 1500-word budget.
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
- The guard in `tests/test_cap_copy.py` was pinned to `10`, so it missed the `8`
  that shipped. The guard is now independent of any number. It matches `at or
  under <n>`, `cap of <n>`, and `keep concurrent requests at or under`.
- `skills/luna-doctor/SKILL.md` now describes the tool that ships. The chart is
  one stacked-area graph, not two panels, and it draws no advisory-concurrency
  rule. Admission deadlines are per model and carry their sample counts, and
  there is no single 120.2s wall. The skill also describes the attribution line,
  `--plan`, and `--baseline prev`. It tells an agent to say whose number it
  quotes, and never to repeat the width of one plan as a gateway fact.
  `README.md` lost its version check from two releases ago, its promise of a
  latency panel, and its claim that codex is not analyzed.

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

- The account's real capacity, from LunaRoute's `GET /v1/capacity`. One data-collection layer in `luna_doctor/sources.py`: `fetch_capacity()` beside the existing fetchers, cached to disk for 5 minutes (the endpoint answers in 4–17s and rate-limits at 60/min, so nothing fetches per-render), provenance-carrying (`account` — a new sixth provenance word for fetched-account values, beside measured/declared/catalog/configured/unknown), degrading to unknown — never zero, never a default, every null preserved as the API's own story. `luna-doctor capacity` renders the API's four pages as a first-class report — now (live occupancy, ledger health, saturation), config (plan, priority and background concurrency, overflow routing), history (7d/30d occupancy rollups), and admission (per complete UTC day: accepted / deferred / denied / denied_after_wait, with a null `avg_queue_wait_ms` rendered as no waiting that day, not 0) — and `capacity --online` is the one command that refreshes the cache; every other consumer reads the cache and never touches the network. `analyze` gains an additive `metrics.capacity` block: plan, concurrency and overflow routing, plus the server's admission counts for the window's complete UTC days placed beside the local outcome counts, with `denied_after_wait` rendered as corroboration for the park-deadline rejection class — the paired “local peak · account limit” line included. The doc page's semantics are stated wherever server counts sit beside local ones: `days_active` is not `days_covered` (idle days are covered but not active), the overflow-routing caveat says overflow is a capacity allowance, not a promise, and shared-pool exclusions (requests on your own provider keys, embeddings, dedicated capacity) mean the two counts are never reconciled. The chart's priority-concurrency rule now resolves from the account's cached `priority_lanes`, labelled “(account, plan <name>)”. A canned live payload — captured once, 2026-10-23, with its null and degraded fields intact — is committed under `tests/fixtures/` so the offline suite tests against the API's real shape.
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
