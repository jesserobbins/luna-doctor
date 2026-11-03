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

write_run_tracker writes .swamp/run_tracker.db, the database swamp itself keeps
per repository: active_runs holds one row per method or workflow run with
started_at, completed_at and status, and pending_runs holds the queue. That
tracker, not a file time, is what tells "installed" from "running".
"""

from __future__ import annotations

import os
import sqlite3
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
    runs: list[dict] | None = None,
    pending: list[dict] | None = None,
    tracker: bool = True,
) -> str:
    """Create a fake swamp repo under tmp/repo_name and return its path.

    behaviors: each entry may have {model, instance, resource, version, status}
        — defaults fill in when omitted. One metadata.yaml+raw per entry.
    workflow_runs: each entry is passed to make_workflow_run_yaml.
    bundle_hash: hash dir under .swamp/bundles/<hash>; None to skip bundles.
    env_swamp: text for .env.swamp; None to omit the file.
    runs / pending: rows for .swamp/run_tracker.db, written by write_run_tracker.
    tracker: False leaves the run tracker out, the shape of an older swamp.
    """
    repo = os.path.join(tmp, repo_name)
    os.makedirs(repo, exist_ok=True)

    # Marker for scan fallback (.swamp.yaml at repo root).
    with open(os.path.join(repo, ".swamp.yaml"), "w") as fh:
        fh.write("swampVersion: 1\nrepoId: test-repo\n")

    if env_swamp is not None:
        with open(os.path.join(repo, ".env.swamp"), "w") as fh:
            fh.write(env_swamp)

    if tracker:
        write_run_tracker(repo, runs, pending)

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


RUN_TRACKER_SQL = """
create table run_tracker_meta (key text primary key, value text);
create table active_runs (
  id text primary key, run_kind text, model_type text, method_name text,
  workflow_name text, pid integer, hostname text, started_at text,
  heartbeat_at text, status text, completed_at text, cancel_reason text,
  initiated_by text, instance_id text, definition_id text
);
create table pending_runs (
  id text primary key, source text, workflow_id_or_name text, payload text,
  route text, traceparent text, tracestate text, created_at text
);
insert into run_tracker_meta (key, value) values ('schema_version', '5');
"""


def write_run_tracker(
    repo: str,
    runs: list[dict] | None = None,
    pending: list[dict] | None = None,
) -> str:
    """Write <repo>/.swamp/run_tracker.db the way swamp lays it out.

    runs: one dict per row, keys started_at / completed_at / status /
        workflow_name / run_kind. Stamp format is swamp's: 2026-10-23T17:12:59.642Z.
    pending: the queue rows, each with created_at.
    Returns the database path.
    """
    import uuid

    path = os.path.join(repo, ".swamp", "run_tracker.db")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(RUN_TRACKER_SQL)
        for run in runs or []:
            conn.execute(
                "insert into active_runs (id, run_kind, workflow_name, started_at,"
                " completed_at, status) values (?,?,?,?,?,?)",
                (str(uuid.uuid4()), run.get("run_kind", "workflow"),
                 run.get("workflow_name"), run.get("started_at"),
                 run.get("completed_at"), run.get("status", "running")))
        for item in pending or []:
            conn.execute(
                "insert into pending_runs (id, workflow_id_or_name, created_at)"
                " values (?,?,?)",
                (str(uuid.uuid4()), item.get("workflow_id_or_name"),
                 item.get("created_at")))
        conn.commit()
    finally:
        conn.close()
    return path
