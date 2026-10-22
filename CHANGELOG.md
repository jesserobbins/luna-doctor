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

### Fixed

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
