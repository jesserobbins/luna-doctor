# Triage TUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bare `luna-doctor` on a TTY opens a triage TUI (one question → verdict screen with provenance badges); non-TTY keeps the existing default run verbatim.

**Architecture:** One new module `luna_doctor/badges.py` (provenance → badge mapping) and one new module `luna_doctor/tui.py` (Textual app, import-guarded). `main()` gains a TTY gate plus top-level `--plan`; `_default_run` is untouched and becomes the non-TTY/fallback path.

**Tech Stack:** Textual 8.x (optional extra `luna-doctor[tui]`), existing `luna_doctor.sources` / `attribution` / `report` machinery, pytest, Python >=3.9.

**Spec:** `docs/2026-11-10-triage-tui-design.md` (read it first; it carries the builder-check decisions: `_default_run` kept verbatim as non-TTY path, TUI cache-only, `--online` fetches once before the TUI opens, top-level `--plan` resolved via `attribution.resolve_priority_concurrency`, "agent seems stuck" → `client_idle_timeout` + `explain --slowest`, badge rollout to CLI reports deferred).

## Global Constraints

- The TUI never opens network behavior: capacity comes from the 5-minute disk cache via `sources.fetch_capacity(allow_network=False, ...)`; `[f]` fix runs cache-only. The only fetch is a top-level `--online` before the TUI opens.
- Null is unknown, never zero; every absent value renders with its reason (existing `_cap_field`-style helpers show the pattern in `build_capacity_report`).
- `now` covers the priority pool only; admission covers the shared pool; admission holds complete UTC days only.
- Live occupancy (`now`) semantics, binding on Task 2: the occupancy line is qualified by the `ledger` state (not `up` ⇒ in_use/saturated rendered as advisory, "may be stale"); the overflow pool's two null shapes render differently (`limit: null` = "cap could not be read", `limit: 0` = "no overflow entitled, closed pool", the latter never saturated); `saturated` is an observation, never refusal language (refusals come only from admission counts); `sampled_at` freshness is shown; a single null field never collapses the whole pool row.
- Copy rules (memory): say "blocked at admission", never "deaths"; never advise running a probe; never sum measured concurrency with configured fan-out; no cap stated as a gateway fact.
- Provenance badges: `●` = `account` (authoritative), `○` = `measured`/local (informative, lower bound), `◌` = `declared`/configured. One legend line wherever badges appear.
- `main()` returns 0 after the TUI closes; the 0/1/2 exit-code contract belongs to `_default_run` and is not changed.
- Test suite stays offline: every network path monkeypatched; TUI tests assert data→widget mapping, not pixel snapshots.
- Commits end with the canonical trailer:
  `Authored-By: Jesse Robbins (@jesserobbins) <https://jesserobbins.com>`

## Review Focus

1. **Non-TTY invocation with Textual installed** (scripts/agents piping output) must never hang on an interactive prompt — expect the old `_default_run` behavior and exit codes. Test in Task 3.
2. **Degraded capacity payload** (`config: null`, `now` fields null with `ledger: "unknown"`, `history.tail: null`) must render as unknown-with-reason badges in the verdict screen, never as zeros. Test in Task 2.
3. **Textual absent on a TTY** must fall back to `_default_run` without error or warning spam. Test in Task 3.
4. **Triage answers with no supporting data** (empty logs, empty window → exit-2 shape) must produce a verdict screen that says what is missing and why, not an empty panel. Test in Task 2.
5. **A stale capacity cache** (fetched_at older than TTL) must be served and labelled stale, matching the catalog's convention. Test in Task 1.

---

### Task 1: Badge layer (`luna_doctor/badges.py`)

**Files:**
- Create: `luna_doctor/badges.py`
- Test: `tests/test_badges.py`

**Interfaces:**
- Consumes: `luna_doctor.sources.PROV_ACCOUNT` and the provenance-legend states.
- Produces: `BADGE_ACCOUNT = "●"`, `BADGE_MEASURED = "○"`, `BADGE_DECLARED = "◌"`; `badge_for(state: str) -> str` (unknown state → `"○"` is forbidden — return `"?"`); `legend_line() -> str` returning the one-line legend naming all three; `badge_help(state: str) -> str` returning the short phrase for a state ("the account reported it", "measured locally, a lower bound", "declared, not measured").

- [ ] **Step 1: Write the failing tests** — assert `badge_for("account") == "●"`, `badge_for("measured") == "○"`, `badge_for("declared") == "◌"`, `badge_for("plan max") == "◌"`, `badge_for(None) == "?"`, `badge_for("nonsense") == "?"`; assert `legend_line()` contains all three badges and the word "authoritative" exactly once; assert `badge_help("account")` is a non-empty sentence containing "account".
- [ ] **Step 2: Run** `python3 -m pytest tests/test_badges.py -v` — expect FAIL (module not defined).
- [ ] **Step 3: Implement** `luna_doctor/badges.py` with the constants and three functions. A leading `"plan "` prefix on a state maps to `BADGE_DECLARED` (matching `attribution.resolve_priority_concurrency`'s `"plan <name>"` source). Keep the module under 60 lines.
- [ ] **Step 4: Run** `python3 -m pytest tests/test_badges.py -v` — expect PASS.
- [ ] **Step 5: Commit** `feat: add provenance badge layer for triage verdict screens`.

### Task 2: Triage app (`luna_doctor/tui.py`)

**Files:**
- Create: `luna_doctor/tui.py`
- Test: `tests/test_tui.py`

**Interfaces:**
- Consumes: `sources.fetch_capacity`, `sources.CatalogRequest`-style cached log scan used by `analyze` (call the same internal scan `cmd_analyze` uses; read `cli.py` to find the exact entry point and reuse it, do not re-scan differently), `attribution.resolve_priority_concurrency(served, plan)`, `badges.badge_for`/`legend_line`, the `verdict(m) -> (headline, adjustments, exit_code)` function in `cli.py`.
- Produces: `class TriageApp(App)` with `__init__(self, plan: str | None, online: bool)`; `TRIAGE_CHOICES: list[tuple[str, str]]` mapping the six on-screen answers to lead-panel keys (`"failing"`, `"slow"`, `"finish_reason"`, `"concurrency"`, `"stuck"`, `"checking"`); `pick_lead(choice_key: str) -> str` returning the lead panel id (`"admission"`, `"latency"`, `"stuck"`, `"admission"`, `"stuck"`, `"overview"`) — `"finish_reason"` leads `"stuck"` because the spec (Goal 2) binds that symptom to its existing diagnosis path, the `client_idle_timeout` classification, not to admission; `build_verdict_rows(choice_key: str, metrics: dict, cap) -> list[tuple[str, str]]` returning (badge, text) rows in the three-panel ranked-trust order (account block, local block, verdict line); `run_triage(plan, online) -> int` entry point that instantiates and runs the app, returns 0.

- [ ] **Step 1: Write the failing tests** (monkeypatched, offline): (a) `pick_lead` maps all six keys with `"concurrency"` → `"admission"` and `"stuck"` → `"stuck"`; (b) `build_verdict_rows` on the canned capacity fixture `tests/fixtures/capacity-2026-11-02.json` plus a synthetic `metrics` dict with `peak_concurrency` produces an account row whose text contains "priority" and carries badge `"●"`, a local row with badge `"○"` containing "lower bound", and a verdict row; (c) on a degraded payload (fixture with `config: null`, `now` null fields), the account rows say "not reported by the account" and no row renders a bare `0` where the payload has `null`; (d) on empty metrics (`requests == 0`), the verdict row says what is missing ("no request in the window") rather than quoting numbers; (e) the stuck choice names `client_idle_timeout` in its local rows when the metrics contain that outcome; (f) on a payload with `ledger: "degraded"`, the account occupancy row carries the advisory wording ("may be stale"), not plain in_use/limit; (g) on a payload with `now.background.limit == null`, the overflow row says the cap could not be read, and with `limit == 0` it says no overflow is entitled — two different strings; (h) no account row contains the word "denied"/"refus" derived from `saturated: true` alone — refusal wording appears only when admission counts are nonzero.
- [ ] **Step 2: Run** `python3 -m pytest tests/test_tui.py -v` — expect FAIL.
- [ ] **Step 3: Implement.** Textual import at module top is guarded by the caller, not by try/except inside these functions; the pure functions (`TRIAGE_CHOICES`, `pick_lead`, `build_verdict_rows`) must not import Textual so they are testable without it. `TriageApp` (guarded import) shows: the question list as a Textual `ListView` with `TRIAGE_CHOICES`, a status line updated by a worker that runs the 24h scan and `fetch_capacity(allow_network=False)`, then the verdict screen as three `Static` panels built from `build_verdict_rows`, `legend_line()` at the bottom, keybinding `f` = fix output rendered cache-only in-place, `q` = quit. Visual style: rounded borders, dimmed secondary rows, bold accent verdict — Textual CSS inside the module.
- [ ] **Step 4: Run** `python3 -m pytest tests/test_tui.py -v` — expect PASS. Also run `python3 -c "import luna_doctor.tui"` on a machine without Textual in a venv if cheap; skip with a note if not.
- [ ] **Step 5: Commit** `feat: triage TUI with provenance-badged verdict screen`.

### Task 3: Front-door wiring in `main()`

**Files:**
- Modify: `luna_doctor/cli.py` (`main()`, around line 3943; `_add_network` call at 3956 stays)
- Test: `tests/test_tui_gate.py`

**Interfaces:**
- Consumes: `tui.run_triage(plan, online)` (Task 2), `_default_run(ap, args)` unchanged, `attribution.resolve_priority_concurrency`.
- Produces: in `main()`, before subcommand dispatch: if no subcommand is present AND `sys.stdin.isatty()` AND `sys.stdout.isatty()` AND `importlib.util.find_spec("textual")` is truthy AND `--version`/`--help` absent → run a one-off capacity fetch when `--online` was passed (same rules as `_account_network`), then `tui.run_triage(args.plan, online)` and `return 0`. Otherwise the existing path runs unchanged (`_default_run` for bare non-TTY/no-Textual). Add top-level `ap.add_argument("--plan", ...)` passed through to `run_triage` and honored by `_default_run`'s inner analyze parse via `["analyze", "--since", "1h", "--plan", args.plan]` when set.

- [ ] **Step 1: Write the failing tests** (monkeypatch `tui.run_triage`): (a) bare argv on monkeypatched `isatty()==True` with a fake `find_spec` returning truthy calls `run_triage` and returns 0; (b) same argv with `isatty()==False` calls `_default_run` instead (assert via its existing observable output or a monkeypatched `_default_run`); (c) `find_spec("textual") -> None` on a TTY calls `_default_run`; (d) `--version` and `--help` never reach the TUI; (e) `--online` on the TUI path performs exactly one `fetch_capacity(allow_network=True)` call before `run_triage` (monkeypatch `fetch_capacity`, count calls); (f) `--plan max` on the bare path reaches the inner analyze invocation (assert via the parsed args `_default_run` receives — monkeypatch `ap.parse_args` is too deep; instead assert `run_triage` receives the plan string).
- [ ] **Step 2: Run** `python3 -m pytest tests/test_tui_gate.py -v` — expect FAIL.
- [ ] **Step 3: Implement** the gate in `main()` as a small helper `_try_tui_front_door(ap, argv) -> int | None` (returns None when the TUI path does not apply) so the branch stays readable; the inner analyze `--plan` pass-through reuses the existing `_bare_duration`-style argv injection.
- [ ] **Step 4: Run** `python3 -m pytest tests/test_tui_gate.py tests/test_cli.py -v` — expect PASS (test_cli proves the old paths still behave).
- [ ] **Step 5: Commit** `feat: bare luna-doctor opens the triage TUI on a TTY; default run kept for scripts`.

### Task 4: Packaging, docs, and the whole-suite gate

**Files:**
- Modify: `pyproject.toml` (add `[project.optional-dependencies] tui = ["textual>=8,<9"]`; if Textual's floor exceeds 3.9, add the environment marker and note it in the design doc)
- Modify: `README.md` (a "First run" section: what the triage asks, what the badges mean, quoting `legend_line()` verbatim)
- Modify: `CHANGELOG.md` (one entry)
- Test: full suite

**Interfaces:**
- Consumes: everything above.
- Produces: installable `luna-doctor[tui]` extra; docs that match the shipped screens.

- [ ] **Step 1:** Add the extra to `pyproject.toml`; run `pip install -e '.[tui]'` in a scratch venv and `luna-doctor --version` — expect success and the existing version string.
- [ ] **Step 2:** Write README "First run" and CHANGELOG entries; the README badge legend must equal `badges.legend_line()` output (paste it, then assert in `tests/test_badges.py` that the README contains the legend string — one test line reading the README).
- [ ] **Step 3: Run** `python3 -m pytest tests/ -v` — expect PASS, suite offline throughout.
- [ ] **Step 4: Commit** `docs: first-run triage guide and the tui extra`.

## Verification matrix (branch-wide, before merge)

- [ ] `echo | luna-doctor` (piped) → old default run, exit code = analyze verdict, no hang.
- [ ] `luna-doctor` on a TTY (with `[tui]` installed) → triage screen → each of the six answers lands on a verdict screen whose lead panel matches `pick_lead`.
- [ ] `luna-doctor --online` on a TTY → one capacity fetch, then cache-only screens.
- [ ] `luna-doctor capacity --online`, `analyze --since 7d`, `chart`, `fix` → byte-identical behavior to before this branch (spot-check outputs against pre-branch).
- [ ] Degraded-payload fixture drives the TUI verdict screen with unknown-with-reason everywhere.
