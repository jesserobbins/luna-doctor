# Triage TUI and provenance badges (design)

Date: 2026-11-10
Status: draft, approved in conversation 2026-11-10.
Owner: Jesse.

## Builder-check decisions (Jesse, 2026-11-10)

- Today's bare `_default_run` (account line, welcome text, 1h `analyze` whose
  verdict is the exit code, chart) is kept verbatim as the non-TTY path.
  TTY opens the TUI. Piped output keeps the old behavior and the documented
  0/1/2 exit-code contract. After the TUI closes, `main()` returns 0.
- `--online` on a bare invocation forces one capacity fetch before the TUI
  opens. The TUI itself runs cache-only from then on. `[f]` (fix in-place)
  runs cache-only too. The TUI never opens network behavior.
- Bare `luna-doctor` gains a top-level `--plan`, resolved through the existing
  `attribution.resolve_priority_concurrency` path so `◌` declared badges can
  appear at the front door.
- "Agent seems stuck" maps to the `client_idle_timeout` classification and the
  `explain --slowest` panel. One 24h scan feeds every triage answer (re-sliced
  per answer). No second analysis runs after selection.
- Rolling badges into `analyze`/`chart` rendered text is deferred to a later
  pass. This build is TUI-only.

## Intent

People open luna-doctor when something is broken. Today bare `luna-doctor`
prints argparse help, which answers "what flags exist" and not "what do I
do". First use must be a diagnosis, not a menu.

Two problems, settled in conversation:

1. The front door is unhelpful. Bare `luna-doctor` must assume "I am having
   problems right now", ask one question about what the person is seeing,
   steer the analysis from that answer, and land on a verdict with a next
   action.
2. Source-of-truth is invisible. The codebase already tracks provenance
   (`account` from the capacity API, `measured` from local logs, `declared`
   via `--plan`), but the reports do not put it where Jesse can see it. He
   cannot tell at a glance whether a number was said by the server or
   reconstructed from logs. The fix is presentation: a badge on every number
   and a one-line legend, ranked by trust.

## Goals

1. `luna-doctor` with no arguments opens a triage flow: one question ("What
   are you seeing?"), a scan of local logs and the cached capacity payload
   while the question is on screen, then a single verdict screen.
2. The triage answer steers what leads the verdict: "429 / concurrency
   errors" leads with admission and denial counts. "things are slow" leads
   with latency and queue waits. "stream ended without finish_reason" and
   "agent seems stuck" lead with their existing diagnosis paths.
3. Every number in the verdict screen carries a badge: `●` the account
   reported it (authoritative), `○` measured locally (informative, a lower
   bound), `◌` declared (`--plan` or a config default). One legend line.
4. All existing subcommands keep working verbatim. The TUI wraps the
   commands, it does not replace them. Scripts and agents keep the CLI.
5. Textual is an optional dependency. Without it, bare `luna-doctor` falls
   back to a linear prompt or plain help, and nothing errors.

## Non-goals

- No new data sources. The TUI consumes what `analyze` and `capacity`
  already produce.
- No live-refresh dashboard. One pass, one verdict. `watch` already exists
  for the live case.
- No rewrite of the verdict model. The TUI renders the same `metrics` and
  Report objects the CLI renders.

## Approaches considered

- A, TUI as front door (chosen). Bare `luna-doctor` opens the triage.
  Subcommands unchanged. Matches first use: people run it when broken.
- B, `luna-doctor tui` as a separate mode: rejected. Two entry points means
  first-time users still meet argparse help, which is the problem being
  fixed.
- C, Questionary wizard only: rejected as the whole answer, but kept as the
  fallback shape. It cannot do the two-truths verdict screen.

## Framework

Textual, current major (8.x), as an optional extra (`luna-doctor[tui]`).
CSS-like styling, widgets for the list and panels, asyncio-friendly for the
background capacity fetch. Import-guarded: `main()` checks for the module
and falls back when absent. Visual target is Charm's terminal aesthetic
(rounded borders, dimmed secondary text, bold accent on the verdict line)
rendered through Textual's CSS, with no Go sidecar. Every verdict the TUI
shows is computed by the existing Python code, so a Bubble Tea binary turns
each of those into an IPC call. Settled: Textual 8.2 requires Python 3.9 or
newer, which is this repo's `>=3.9` floor, so the `luna-doctor[tui]` extra
needs no environment marker and a machine without the extra keeps the fallback.

## The triage screen

```
$ luna-doctor

  What are you seeing?
  > Requests failing or hanging          <- default cursor
    Things are slow
    "Stream ended without finish_reason"
    429 / concurrency errors
    Agent seems stuck mid-task
    Nothing yet, just checking

  scanning local logs… (last 24h)  ✓ 17 sessions · 3 candidates
  fetching account capacity…       ✓ plan apex · 12 lanes (cached 2m ago)
```

The scan and the capacity read run while the question is on screen. They
never block the answer. The capacity payload comes from the 5-minute disk
cache when fresh. `--online` semantics stay with the `capacity` command. The
TUI never opens new network behavior.

## The verdict screen

```
  ── What the account reports (authoritative) ──────────────
  ● limit 12 priority · in_use 8 · overflow 4/12 in use
  ● pool ledger up · sample taken 2s before this screen
  ● last 7d: denied_after_wait 16 (Oct 20, Oct 22)

  ── What this machine's logs show (informative, lower bound) ─
  ○ peak 17 concurrent — other machines invisible, so ≥ 17
  ○ 3 requests classified park-deadline refusals

  ── Verdict ───────────────────────────────────────────────
  Over capacity. Server confirms refusals after waiting.
  → Run `fix` for per-agent settings, or press [f]
```

Three panels in ranked-trust order. The verdict line reuses the existing
`analyze` verdict model. `[f]` runs the `fix` output in-place. `--json` and
copy actions are available as keybindings. Leave keys return to the CLI's
own report text so nothing is shown only in the TUI.

## Provenance badges

The `PROVENANCE_LEGEND` words already exist. The badge layer maps them:

| state | badge | trust |
|---|---|---|
| `account` | `●` | authoritative, the API said it |
| `measured` / local | `○` | informative, lower bound |
| `declared` / configured constant | `◌` | stated, not measured |

Applied first to the TUI verdict screen. Rolling the badges into `analyze`
and `chart` rendered text is a follow-on pass the builder can begin with the
`analyze` headline if it lands cleanly.

## Constraints carried from the capacity design

- Null is unknown, never zero. Absent blocks render with their reason.
- `now` covers the priority pool only. Admission covers the shared pool.
- Live-occupancy semantics, from the API's `now` section (binding):
  - The occupancy line is qualified by `ledger`: when the ledger is not
    `up`, `in_use` and `saturated` are advisory and the screen says the
    count can be stale or missing, so the pool line never looks
    authoritative when its source ledger is not healthy.
  - The overflow pool's two null shapes stay distinct: `limit: null` is
    "the cap is not readable (unknown)". `limit: 0` is "no overflow
    entitled, a closed pool", which is never saturated. The TUI renders
    both, differently, in the account panel.
  - `saturated` is an observation, never a verdict: a full pool is not a
    refusal. Refusal language comes only from the admission counts.
  - `sampled_at` is shown so the reader knows how fresh the live read is.
  - The block is read together: a single null field cannot tell "idle"
    from "unavailable", so one null never collapses the whole pool row.
- Admission holds complete UTC days only.
- Copy rules from memory: "blocked at admission", no "deaths", no probe
  advice, no summing measured concurrency with configured fan-out.

## Left to the builder

- Widget choices and layout, within the screens sketched above.
- The fallback (no Textual) shape: linear prompt via a tiny hand-rolled
  selector or plain help. No new hard dependency either way.
- Which `analyze` code paths the verdict screen calls directly versus
  re-renders from cached `metrics`.
- Test fixtures: the suite stays offline. TUI screens get unit tests on
  their data-to-widget mapping, not snapshot tests of pixels.
- Keybinding set. `[f]` for fix is a proposal, the rest is the builder's.

## Second pass: the orientation dashboard (Jesse, after first use)

First use showed the verdict screen too wordy, and the chart work unresolved:
the long-window stacked-area picture does not read for most humans, while the
1h picture (each request drawn) does. Decisions, taken 2026-11-10:

1. The triage verdict screen becomes an orientation dashboard: medium density
   (about 80 columns, 4-5 rows per panel), sparklines (Unicode blocks) carry
   the shapes, prose shrinks to labels. Details move to keypresses.
2. Sparklines in the dashboard, and the rendered chart stays available on a
   keypress (c opens it over the scan window, 24h). No braille plots.
3. The 1h chart stays the default picture and the useful one. The capacity
   rule on every chart is drawn according to its provenance: an authoritative
   (account) rule is a thick, clearly labelled solid line, and a declared rule is
   visibly thinner and dashed. Authority is a property of the mark, not only
   of the label.
4. Moon phases become the status vocabulary across surfaces: 🌕 healthy,
   🌗 pressure, 🌑 blocked at admission (renderer degrades to ASCII as the
   existing moon does). Each surface that shows a phase shows the legend
   once. This is a status scale, decided by the planner, not decoration.

## Third pass: Textual is a default dependency (planner + Jesse, 2026-11-11)

First use found friction that decided the question. A plain `pipx install
luna-doctor` lost the front door and said nothing, so the person who ran the
tool because they were broken met the linear run and never learned a screen
existed. Ruling: the `tui` extra graduates to a default dependency.

1. `pyproject.toml` names `textual>=8,<9` in `dependencies`. Goal 5 above, and
   the "no new hard dependency" line under "Left to the builder", are both
   superseded by this ruling.
2. The `luna-doctor[tui]` extra stays as a compatibility alias for the same
   pin, so an install command or a requirements file that names it keeps
   working. The extra is no longer the way to get the screen.
3. The import guard in `cli._try_tui_front_door` stays. It no longer answers a
   machine that chose the linear run. It answers a broken or partial install:
   a Textual from another interpreter, or a version this module's API calls do
   not match.
