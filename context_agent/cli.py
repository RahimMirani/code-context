"""CLI entrypoint for local context memory management."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .constants import DEFAULT_CAP_BYTES, RECENT_EVENTS_DEFAULT
from .project_db import ProjectStore, project_memory_paths
from .recorder import Recorder
from .registry import Registry
from .utils import human_bytes, is_pid_alive, normalize_path, terminate_pid, wait_for_process_exit


def default_ctx_home() -> Path:
    override = os.environ.get("CTX_HOME")
    if override:
        return normalize_path(override)
    return normalize_path(Path.home() / ".context-agent")


def resolve_project_path(args, registry: Registry) -> Path:
    if getattr(args, "path", None):
        return normalize_path(args.path)

    name = getattr(args, "name", None)
    if name:
        matches = registry.find_projects_by_name(name)
        if not matches:
            raise SystemExit(f"No active project found with name '{name}'.")
        if len(matches) > 1:
            print(f"Display name '{name}' is ambiguous. Provide --path. Candidates:")
            for row in matches:
                print(f"- {row['path']}")
            raise SystemExit(2)
        return normalize_path(matches[0]["path"])

    return normalize_path(Path.cwd())


def spawn_recorder(project_path: Path, session_id: int, agent: str, ctx_home: Path) -> int:
    package_root = Path(__file__).resolve().parents[1]
    cmd = [
        sys.executable,
        "-m",
        "context_agent.cli",
        "_recorder_run",
        "--path",
        str(project_path),
        "--session-id",
        str(session_id),
        "--agent",
        agent,
        "--ctx-home",
        str(ctx_home),
    ]
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        env["PYTHONPATH"] = f"{package_root}{os.pathsep}{existing_pythonpath}"
    else:
        env["PYTHONPATH"] = str(package_root)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=env,
    )
    return int(proc.pid)


def cmd_start(args) -> int:
    ctx_home = default_ctx_home()
    registry = Registry(ctx_home)
    project_path = resolve_project_path(args, registry)
    project_path.mkdir(parents=True, exist_ok=True)
    store = ProjectStore(project_path)
    memory_root, db_path, logs_path = project_memory_paths(project_path)

    registry.upsert_project(project_path, args.name, db_path, logs_path)
    project_row = registry.get_project(project_path)
    if project_row and project_row["deleted_at"]:
        print(f"Project '{project_path}' is soft-deleted. Purge or restore before start.")
        return 1

    if project_row and project_row["recording_state"] == "recording":
        pid = project_row["recorder_pid"]
        session_id = project_row["active_session_id"]
        if pid and is_pid_alive(pid):
            print(f"Already recording. Session: {session_id}, PID: {pid}")
            print(f"DB: {db_path}")
            print(f"Logs: {logs_path}")
            return 0

    # Repair stale recording state.
    if project_row and project_row["recording_state"] == "recording":
        stale_session = project_row["active_session_id"]
        if stale_session:
            store.set_session_state(int(stale_session), "stopped")
        registry.set_recording_state(project_path, "stopped", None, None)

    store.set_project_metadata(args.name, "recording")
    session_id = store.create_session(args.agent)
    pid = spawn_recorder(project_path, session_id, args.agent, ctx_home)
    registry.set_recording_state(project_path, "recording", session_id, pid)

    print(f"Recording started. Session: {session_id}, PID: {pid}")
    print(f"DB: {db_path}")
    print(f"Logs: {logs_path}")
    return 0


def cmd_stop(args) -> int:
    ctx_home = default_ctx_home()
    registry = Registry(ctx_home)
    project_path = resolve_project_path(args, registry)
    project_row = registry.get_project(project_path)
    if not project_row:
        print(f"Project not found: {project_path}")
        return 1

    state = project_row["recording_state"]
    store = ProjectStore(project_path)
    session_id = project_row["active_session_id"]
    pid = project_row["recorder_pid"]

    if state != "recording":
        print("Recorder already stopped.")
        return 0

    if session_id:
        store.set_session_state(int(session_id), "stopping")

    if pid and is_pid_alive(int(pid)):
        exited = wait_for_process_exit(int(pid), timeout_seconds=10)
        if not exited:
            terminate_pid(int(pid))
            wait_for_process_exit(int(pid), timeout_seconds=2)

    if session_id:
        store.set_session_state(int(session_id), "stopped")
    registry.set_recording_state(project_path, "stopped", None, None)
    print("Recording stopped.")
    return 0


def cmd_status(args) -> int:
    ctx_home = default_ctx_home()
    registry = Registry(ctx_home)
    project_path = resolve_project_path(args, registry)
    project_row = registry.get_project(project_path)
    if not project_row:
        print(f"Project not found: {project_path}")
        return 1

    store = ProjectStore(project_path)
    snapshot = store.status_snapshot(recent_limit=RECENT_EVENTS_DEFAULT)
    project = snapshot["project"]
    if project is None:
        print(f"Project DB missing project row: {project_path}")
        return 1

    used = int(snapshot["storage_used_bytes"])
    cap = int(project["storage_cap_bytes"] or DEFAULT_CAP_BYTES)
    print(f"Project: {project_path}")
    print(f"Name: {project['display_name'] or '(none)'}")
    print(f"Recording: {project['recording_state']}")
    print(f"Last updated: {project['last_updated_at'] or 'never'}")
    print(f"Storage: {human_bytes(used)} / {human_bytes(cap)}")
    print(f"Effective changed files: {snapshot.get('effective_changed_files', 0)}")

    active = snapshot["active_session"]
    if active:
        print(f"Active session: {active['id']} ({active['agent']})")
    else:
        print("Active session: none")

    source_rows = snapshot["source_status"]
    if source_rows:
        print("Sources:")
        for row in source_rows:
            detail = row["detail"] or ""
            print(f"- {row['source']}: {row['status']} {detail}".rstrip())

    events = snapshot["events"]
    if events:
        print("Recent events:")
        for row in events:
            effective = "effective" if int(row["is_effective"] or 0) == 1 else "reverted"
            print(
                f"- [{row['created_at']}] {row['event_type']} ({row['source']}, {effective}): {row['summary']}"
            )

    last_revert = snapshot.get("last_revert")
    if last_revert:
        print(f"Last revert: {last_revert['created_at']} - {last_revert['summary']}")

    return 0


def cmd_where(args) -> int:
    ctx_home = default_ctx_home()
    registry = Registry(ctx_home)
    project_path = resolve_project_path(args, registry)
    row = registry.get_project(project_path)
    if not row:
        # Project may not be in registry yet; derive default location.
        _, db_path, logs_path = project_memory_paths(project_path)
        print(f"DB: {db_path}")
        print(f"Logs: {logs_path}")
        return 0

    db_path = row["db_path"] or str(project_memory_paths(project_path)[1])
    logs_path = row["logs_path"] or str(project_memory_paths(project_path)[2])
    print(f"DB: {db_path}")
    print(f"Logs: {logs_path}")
    return 0


def cmd_delete(args) -> int:
    ctx_home = default_ctx_home()
    registry = Registry(ctx_home)
    project_path = resolve_project_path(args, registry)
    row = registry.get_project(project_path)
    if not row:
        print(f"Project not found: {project_path}")
        return 1
    if row["recording_state"] == "recording":
        print("Stop recording before delete.")
        return 1

    registry.set_project_deleted(project_path, True)
    store = ProjectStore(project_path)
    store.set_project_deleted(True)
    print(f"Soft deleted project context: {project_path}")
    return 0


def cmd_purge(args) -> int:
    if not args.force:
        print("Refusing purge without --force.")
        return 1

    ctx_home = default_ctx_home()
    registry = Registry(ctx_home)
    project_path = resolve_project_path(args, registry)
    row = registry.get_project(project_path)
    if row and row["recording_state"] == "recording":
        print("Stop recording before purge.")
        return 1

    memory_root, _db_path, _logs_path = project_memory_paths(project_path)
    if memory_root.exists():
        shutil.rmtree(memory_root)
    registry.remove_project(project_path)
    print(f"Purged project context: {project_path}")
    return 0


def cmd_list(_args) -> int:
    ctx_home = default_ctx_home()
    registry = Registry(ctx_home)
    rows = registry.list_projects(include_deleted=False)
    if not rows:
        print("No projects registered.")
        return 0
    for row in rows:
        name = row["display_name"] or "(none)"
        print(f"{row['path']} | name={name} | state={row['recording_state']}")
    return 0


def cmd_adapter_configure(args) -> int:
    adapter = args.adapter.lower().strip()
    if adapter not in {"cursor", "claude"}:
        print("Adapter must be 'cursor' or 'claude'.")
        return 1

    ctx_home = default_ctx_home()
    registry = Registry(ctx_home)
    registry.set_adapter_log_path(adapter, normalize_path(args.log_path))
    print(f"Configured {adapter} log path: {normalize_path(args.log_path)}")
    print(f"Config file: {registry.config_path}")
    return 0


def cmd_vector_enable(args) -> int:
    ctx_home = default_ctx_home()
    registry = Registry(ctx_home)
    project_path = resolve_project_path(args, registry)
    row = registry.get_project(project_path)
    if not row:
        store = ProjectStore(project_path)
        _memory_root, db_path, logs_path = project_memory_paths(project_path)
        registry.upsert_project(project_path, getattr(args, "name", None), db_path, logs_path)
    registry.set_vector_enabled(project_path, True)
    store = ProjectStore(project_path)
    store.set_feature("vector_enabled", "true")
    print(f"Vector search feature flag enabled for project: {project_path}")
    return 0


def cmd_recorder_run(args) -> int:
    interval_seconds = float(os.environ.get("CTX_RECORDER_INTERVAL", "2.0"))
    recorder = Recorder(
        project_path=normalize_path(args.path),
        session_id=int(args.session_id),
        agent=args.agent,
        registry_home=normalize_path(args.ctx_home),
        interval_seconds=interval_seconds,
    )
    return recorder.run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ctx", description="Local project context memory CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_start = subparsers.add_parser("start", help="Start recording context for a project")
    p_start.add_argument("--name", default=None)
    p_start.add_argument("--path", default=None)
    p_start.add_argument("--agent", default="auto", choices=["cursor", "claude", "auto"])
    p_start.set_defaults(func=cmd_start)

    p_stop = subparsers.add_parser("stop", help="Stop active recording for a project")
    p_stop.add_argument("--path", default=None)
    p_stop.add_argument("--name", default=None)
    p_stop.set_defaults(func=cmd_stop)

    p_status = subparsers.add_parser("status", help="Show project recording status")
    p_status.add_argument("--path", default=None)
    p_status.add_argument("--name", default=None)
    p_status.set_defaults(func=cmd_status)

    p_where = subparsers.add_parser("where", help="Print local memory storage paths")
    p_where.add_argument("--path", default=None)
    p_where.add_argument("--name", default=None)
    p_where.set_defaults(func=cmd_where)

    p_delete = subparsers.add_parser("delete", help="Soft delete project context")
    p_delete.add_argument("--path", default=None)
    p_delete.add_argument("--name", default=None)
    p_delete.set_defaults(func=cmd_delete)

    p_purge = subparsers.add_parser("purge", help="Permanently delete project context")
    p_purge.add_argument("--path", default=None)
    p_purge.add_argument("--name", default=None)
    p_purge.add_argument("--force", action="store_true")
    p_purge.set_defaults(func=cmd_purge)

    p_list = subparsers.add_parser("list", help="List active projects")
    p_list.set_defaults(func=cmd_list)

    p_adapter = subparsers.add_parser("adapter", help="Adapter management")
    adapter_sub = p_adapter.add_subparsers(dest="adapter_command", required=True)
    p_adapter_config = adapter_sub.add_parser("configure", help="Configure adapter source")
    p_adapter_config.add_argument("adapter", choices=["cursor", "claude"])
    p_adapter_config.add_argument("--log-path", required=True)
    p_adapter_config.set_defaults(func=cmd_adapter_configure)

    p_vector = subparsers.add_parser("vector", help="Vector feature toggles")
    vector_sub = p_vector.add_subparsers(dest="vector_command", required=True)
    p_vector_enable = vector_sub.add_parser("enable", help="Enable vector feature flag")
    p_vector_enable.add_argument("--path", default=None)
    p_vector_enable.add_argument("--name", default=None)
    p_vector_enable.set_defaults(func=cmd_vector_enable)

    p_recorder = subparsers.add_parser("_recorder_run")
    p_recorder.add_argument("--path", required=True)
    p_recorder.add_argument("--session-id", required=True)
    p_recorder.add_argument("--agent", required=True)
    p_recorder.add_argument("--ctx-home", required=True)
    p_recorder.set_defaults(func=cmd_recorder_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
