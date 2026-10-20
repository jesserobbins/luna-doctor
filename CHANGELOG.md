# Changelog

All notable changes to luna-doctor are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versioning policy:

- **MAJOR** — a change that makes existing config advice wrong or unsafe, or a
  breaking change to the `--json` envelope or CLI flags.
- **MINOR** — new commands, new harness support, new measurements.
- **PATCH** — corrections to measured constants, wording, or fixes that do not
  change the interface.
- `schema` in the `--json` envelope is bumped independently when the document
  shape changes, so a stored report can be read back correctly.
- Work in progress accumulates under **Unreleased**. The version is bumped
  only when a release is deliberately cut.

## [Unreleased]

### Added

- `luna-doctor chart` writes a standalone SVG: requests in flight over time,
  split by tier with the burst cap and advised fan-out drawn in, and every
  request's end-to-end latency on a log axis with the 120.2s admission deadline
  and the 300s client idle budget as reference walls. Failures are plotted on the
  wall that killed them, so a burst's tail is visible pushing into it.
- `--width`, `--height`, `--out` and `--open` for the chart; `--json` reports the
  written path plus the metrics.
- Minute-level concurrency series in `analyze`: per UTC minute, the max requests
  in flight, how many started, and how many died on admission — the finest grain
  the request windows support, and finer than agentsview's 5-minute activity
  bucket, so a burst can be lined up against the 120.2s admission deadline.

### Changed

- License changed from MIT to Apache-2.0.

## [0.1.0] - 2026-10-21

### Added

- `luna-doctor analyze` — reconstructs each model request from pi and
  claude-code session stores (read-only) and classifies outcomes against the
  measured gateway contract: admission death at 120.2s, silent stall at the
  client idle budget, upstream 5xx, rate limit, abort, connection error.
- `luna-doctor fix` — per-agent copy-paste config for pi, claude-code and codex,
  built only from keys verified in the installed bundles or their shipped docs.
  Backs up, validates, merges to a temp file, then moves it into place.
- `luna-doctor probe` / `compare` — live burst measurement capturing
  `x-lunaroute-service-class`, `x-lunaroute-service-tier` and
  `x-lunaroute-warning: overflowed-to-background`; the tier comparison shows the
  interactive tier being killed at the admission deadline while the `-background`
  twin is admitted and answers in minutes.
- `luna-doctor discover` — what log stores exist, how big, how fresh.
- `skills/luna-doctor/SKILL.md` — the agent-facing skill.

### Notes

- Measured facts underlying the classification live in `CONTRACT` and in the
  private research notes: 25.2s before SSE headers, 120.2s admission deadline,
  interactive burst cap of 10, overflow demoted to the background tier, SSE
  keep-alive every ~10s, background first token observed at 633s, pi's own idle
  budget at 300s.
- `x-lunaroute-queue-ms` is reported by the gateway as `0` even on requests that
  waited ten minutes, so the tool never uses it.
