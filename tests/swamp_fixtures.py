"""Shared swamp fixtures for luna-doctor tests (task-1).

Creates a minimal fake swamp repo on disk without touching $HOME or real
~/.swamp. Downstream tasks reuse these helpers for analyze/fix/load-test tests.

Layout created by make_swamp_repo:
  <repo>/.swamp.yaml              — marker so scan can find it
  <repo>/.env.swamp               — optional (only when env_swamp=...)
  <repo>/.swamp/data/@jesse/<model>/<instance>/<resource>/<version>/metadata.yaml + raw
  <repo>/.swamp/workflow-runs/<workflowId>/workflow-run-<uuid>.yaml
  <repo>/.swamp/bundles/<hash>/obsidian_ideas.js

make_workflow_run_yaml builds one workflow-run YAML string.
env_with_swamp_root returns an env dict with SWAMP_ROOT set for subprocess
tests (and optionally extra overrides).
"""

from __future__ import annotations

import os
import textwrap
import uuid


def make_swamp_repo(
    tmp: str,
    *,
    behaviors: list[dict] | None = None,
    workflow_runs: list[dict] | None = None,
    bundle_hash: str | None = "abc123def456",
    env_swamp: str | None = None,
    repo_name: str = "swamp_repo",
) -> str:
    """Create a fake swamp repo under tmp/repo_name and return its path.

    behaviors: each entry may have {model, instance, resource, version, status}
        — defaults fill in when omitted. One metadata.yaml+raw per entry.
    workflow_runs: each entry is passed to make_workflow_run_yaml.
    bundle_hash: hash dir under .swamp/bundles/<hash>; None to skip bundles.
    env_swamp: text for .env.swamp; None to omit the file.
    """
    repo = os.path.join(tmp, repo_name)
    os.makedirs(repo, exist_ok=True)

    # Marker for scan fallback (.swamp.yaml at repo root).
    with open(os.path.join(repo, ".swamp.yaml"), "w") as fh:
        fh.write("swampVersion: 1\nrepoId: test-repo\n")

    if env_swamp is not None:
        with open(os.path.join(repo, ".env.swamp"), "w") as fh:
            fh.write(env_swamp)

    # Bundles: .swamp/bundles/<hash>/obsidian_ideas.js
    if bundle_hash is not None:
        bundle_dir = os.path.join(repo, ".swamp", "bundles", bundle_hash)
        os.makedirs(bundle_dir, exist_ok=True)
        with open(os.path.join(bundle_dir, "obsidian_ideas.js"), "w") as fh:
            fh.write(f"// bundle {bundle_hash}\nexport const BUNDLE_HASH = {bundle_hash!r};\n")

    # Behavior history: versioned resource store
    # .swamp/data/@jesse/<model>/<instance>/<resource>/<version>/
    for idx, b in enumerate(behaviors or []):
        model = b.get("model", f"model-{idx}")
        instance = b.get("instance", f"instance-{idx}")
        resource = b.get("resource", f"resource-{idx}")
        version = b.get("version", str(idx + 1))
        resource_dir = os.path.join(repo, ".swamp", "data", "@jesse", model, instance, resource, version)
        os.makedirs(resource_dir, exist_ok=True)
        status = b.get("status", "succeeded")
        with open(os.path.join(resource_dir, "metadata.yaml"), "w") as fh:
            fh.write(textwrap.dedent(f"""\
                id: {resource}-{version}
                version: {version}
                status: {status}
                model: {model}
                startedAt: '2026-01-01T00:00:00.000Z'
                completedAt: '2026-01-01T00:01:00.000Z'
            """))
        with open(os.path.join(resource_dir, "raw"), "w") as fh:
            fh.write("{}")

    # Workflow runs: .swamp/workflow-runs/<workflowId>/workflow-run-*.yaml
    for wr in workflow_runs or []:
        yaml_text = make_workflow_run_yaml(**wr) if isinstance(wr, dict) else str(wr)
        wf_id = wr.get("workflowId", "test-workflow") if isinstance(wr, dict) else "test-workflow"
        run_id = wr.get("id", str(uuid.uuid4())) if isinstance(wr, dict) else str(uuid.uuid4())
        wf_dir = os.path.join(repo, ".swamp", "workflow-runs", wf_id)
        os.makedirs(wf_dir, exist_ok=True)
        with open(os.path.join(wf_dir, f"workflow-run-{run_id}.yaml"), "w") as fh:
            fh.write(yaml_text)

    return repo


def make_workflow_run_yaml(
    *,
    workflowId: str = "test-workflow",
    id: str | None = None,  # noqa: A002 — mirrors swamp field name
    status: str = "succeeded",
    workflowName: str = "test-workflow",
    startedAt: str = "2026-01-01T00:00:00.000Z",
    completedAt: str = "2026-01-01T00:01:00.000Z",
    jobs: list[dict] | None = None,
) -> str:
    """Return a workflow-run YAML string like swamp's .swamp/workflow-runs/*."""
    rid = id or str(uuid.uuid4())
    jobs = jobs if jobs is not None else [{"jobName": "main", "status": status}]
    jobs_yaml = ""
    for j in jobs:
        jobs_yaml += textwrap.dedent(f"""\
            - jobName: {j.get("jobName", "main")}
              status: {j.get("status", status)}
        """)
    return textwrap.dedent(f"""\
        id: {rid}
        workflowId: {workflowId}
        workflowName: {workflowName}
        status: {status}
        startedAt: '{startedAt}'
        completedAt: '{completedAt}'
        jobs:
        {jobs_yaml}""")


def env_with_swamp_root(repo_dir: str, extra: dict | None = None) -> dict:
    """Return env dict for subprocess tests: os.environ + SWAMP_ROOT=repo_dir.

    extra: additional env overrides (e.g. {"SWAMP_EXTRACT_CONCURRENCY": "6"}).
    """
    env = dict(os.environ)
    env["SWAMP_ROOT"] = repo_dir
    if extra:
        env.update(extra)
    return env
