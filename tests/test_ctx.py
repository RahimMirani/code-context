from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CtxIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.ctx_home = self.base / "ctx-home"
        self.env = os.environ.copy()
        self.env["CTX_HOME"] = str(self.ctx_home)
        self.env["CTX_RECORDER_INTERVAL"] = "0.25"
        self.project = self.base / "project-a"
        self.project.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tempdir.cleanup()

    def run_ctx(self, args: list[str], expected: int = 0, input_text: str | None = None):
        cmd = [sys.executable, "-m", "context_agent.cli"] + args
        result = subprocess.run(
            cmd,
            cwd=ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            input=input_text,
        )
        if result.returncode != expected:
            raise AssertionError(
                f"Command failed: {' '.join(cmd)}\n"
                f"code={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
            )
        return result

    def _mcp_write(self, proc: subprocess.Popen, payload: dict) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        proc.stdin.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii") + encoded)
        proc.stdin.flush()

    def _mcp_read(self, proc: subprocess.Popen) -> dict:
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = proc.stdout.read(1)
            if not chunk:
                raise AssertionError("MCP server closed stdout unexpectedly")
            header += chunk
        header_text = header.decode("ascii", errors="ignore")
        length = None
        for line in header_text.split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1].strip())
                break
        if length is None:
            raise AssertionError(f"Missing Content-Length header: {header_text}")
        body = proc.stdout.read(length)
        if not body:
            raise AssertionError("Missing MCP response body")
        return json.loads(body.decode("utf-8"))

    def _mcp_request(self, proc: subprocess.Popen, request_id: int, method: str, params: dict | None = None) -> dict:
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._mcp_write(proc, payload)
        response = self._mcp_read(proc)
        self.assertEqual(response.get("id"), request_id)
        if "error" in response:
            raise AssertionError(f"MCP error: {response['error']}")
        return response

    def _mcp_request_jsonl(self, proc: subprocess.Popen, request_id: int, method: str, params: dict | None = None) -> dict:
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        proc.stdin.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            raise AssertionError("MCP server closed stdout unexpectedly (jsonl)")
        response = json.loads(line)
        self.assertEqual(response.get("id"), request_id)
        if "error" in response:
            raise AssertionError(f"MCP error (jsonl): {response['error']}")
        return response

    def test_start_stop_where_status_delete_purge(self):
        out = self.run_ctx(["start", "--path", str(self.project), "--name", "demo", "--agent", "auto"])
        self.assertIn("Recording started.", out.stdout)

        where = self.run_ctx(["where", "--path", str(self.project)])
        self.assertIn("DB:", where.stdout)
        self.assertIn("Logs:", where.stdout)

        status = self.run_ctx(["status", "--path", str(self.project)])
        self.assertIn("Recording: recording", status.stdout)

        stop = self.run_ctx(["stop", "--path", str(self.project)])
        self.assertIn("Recording stopped.", stop.stdout)

        delete = self.run_ctx(["delete", "--path", str(self.project)])
        self.assertIn("Soft deleted", delete.stdout)

        purge = self.run_ctx(["purge", "--path", str(self.project), "--force"])
        self.assertIn("Purged project context", purge.stdout)
        self.assertFalse((self.project / ".context-memory").exists())

    def test_where_name_ambiguity(self):
        p1 = self.base / "project-1"
        p2 = self.base / "project-2"
        p1.mkdir()
        p2.mkdir()
        self.run_ctx(["start", "--path", str(p1), "--name", "same"])
        self.run_ctx(["stop", "--path", str(p1)])
        self.run_ctx(["start", "--path", str(p2), "--name", "same"])
        self.run_ctx(["stop", "--path", str(p2)])

        result = self.run_ctx(["where", "--name", "same"], expected=2)
        self.assertIn("ambiguous", result.stdout.lower())

    def test_sessions_list_and_resume(self):
        (self.project / "README.md").write_text("# Demo\n", encoding="utf-8")
        self.run_ctx(["start", "--path", str(self.project), "--name", "resume-demo", "--agent", "auto"])
        time.sleep(0.8)
        self.run_ctx(["stop", "--path", str(self.project)])

        self.run_ctx(["start", "--path", str(self.project), "--name", "resume-demo", "--agent", "auto"])
        time.sleep(0.8)
        self.run_ctx(["stop", "--path", str(self.project)])

        sessions_out = self.run_ctx(["sessions", "--path", str(self.project)])
        self.assertIn("id=", sessions_out.stdout)
        lines = [line for line in sessions_out.stdout.splitlines() if line.strip().startswith("- id=")]
        self.assertGreaterEqual(len(lines), 2)
        first_session_id = int(lines[-1].split("id=")[1].split(" ", 1)[0])

        resume_out = self.run_ctx(["resume", "--path", str(self.project), "--session-id", str(first_session_id)])
        self.assertIn("Resumed session", resume_out.stdout)

        status = self.run_ctx(["status", "--path", str(self.project)])
        self.assertIn(f"Active session: {first_session_id}", status.stdout)

        self.run_ctx(["stop", "--path", str(self.project)])

        db_path = self.project / ".context-memory" / "context.db"
        with sqlite3.connect(db_path) as conn:
            snapshots = conn.execute(
                "SELECT summary FROM events WHERE source = 'ctx:startup' ORDER BY id ASC"
            ).fetchall()
            self.assertGreaterEqual(len(snapshots), 3)
            latest = snapshots[-1][0]
            self.assertIn("Repo snapshot", latest)
            self.assertIn(str(self.project.resolve()), latest)

    def test_delete_single_session(self):
        self.run_ctx(["start", "--path", str(self.project), "--name", "delete-session", "--agent", "auto"])
        time.sleep(0.8)
        self.run_ctx(["stop", "--path", str(self.project)])

        self.run_ctx(["start", "--path", str(self.project), "--name", "delete-session", "--agent", "auto"])
        time.sleep(0.8)
        self.run_ctx(["stop", "--path", str(self.project)])

        sessions_out = self.run_ctx(["sessions", "--path", str(self.project)])
        lines = [line for line in sessions_out.stdout.splitlines() if line.strip().startswith("- id=")]
        self.assertGreaterEqual(len(lines), 2)
        newest_id = int(lines[0].split("id=")[1].split(" ", 1)[0])

        delete_out = self.run_ctx(["delete", "--path", str(self.project), "--session-id", str(newest_id)])
        self.assertIn(f"Deleted session {newest_id}", delete_out.stdout)

        sessions_after = self.run_ctx(["sessions", "--path", str(self.project)])
        self.assertNotIn(f"id={newest_id} ", sessions_after.stdout)

    def test_adapter_summary_ingest_no_raw_storage(self):
        log_path = self.base / "cursor.log"
        log_path.write_text("", encoding="utf-8")
        self.run_ctx(["adapter", "configure", "cursor", "--log-path", str(log_path)])
        self.run_ctx(["start", "--path", str(self.project), "--name", "demo-ingest", "--agent", "cursor"])

        payload = {
            "event_type": "decision_made",
            "summary": "Use repository pattern for provider abstraction.",
            "files_touched": ["src/repository.py"],
            "tool_name": "pytest",
            "purpose": "validate behavior",
            "result": "success",
            "raw_prompt": "THIS SHOULD NEVER BE STORED",
        }
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")

        time.sleep(1.5)
        self.run_ctx(["stop", "--path", str(self.project)])

        db_path = self.project / ".context-memory" / "context.db"
        with sqlite3.connect(db_path) as conn:
            event = conn.execute(
                "SELECT event_type, summary FROM events WHERE event_type = 'decision_made' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(event)
            self.assertEqual(event[0], "decision_made")
            self.assertIn("repository pattern", event[1].lower())

            cols = conn.execute("PRAGMA table_info(events)").fetchall()
            col_names = {row[1] for row in cols}
            self.assertNotIn("raw_prompt", col_names)

    def test_file_revert_event_and_effective_state(self):
        tracked = self.project / "tracked.txt"
        tracked.write_text("v1", encoding="utf-8")
        self.run_ctx(["start", "--path", str(self.project), "--name", "revert-demo", "--agent", "auto"])
        time.sleep(0.8)

        tracked.write_text("v2", encoding="utf-8")
        time.sleep(0.8)
        tracked.write_text("v1", encoding="utf-8")
        time.sleep(0.8)

        self.run_ctx(["stop", "--path", str(self.project)])

        db_path = self.project / ".context-memory" / "context.db"
        with sqlite3.connect(db_path) as conn:
            revert = conn.execute(
                "SELECT event_type, summary FROM events WHERE event_type = 'revert' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(revert)
            self.assertIn("reverted", revert[1].lower())

            clean = conn.execute(
                "SELECT is_clean FROM file_state WHERE path = 'tracked.txt'"
            ).fetchone()
            self.assertIsNotNone(clean)
            self.assertEqual(int(clean[0]), 1)

        status = self.run_ctx(["status", "--path", str(self.project)])
        self.assertIn("Last revert:", status.stdout)

    def test_init_writes_project_local_configs_and_gitignore(self):
        cursor_dir = self.project / ".cursor"
        cursor_dir.mkdir(parents=True, exist_ok=True)
        (cursor_dir / "mcp.json").write_text(
            json.dumps({"mcpServers": {"other": {"command": "other"}}, "custom": True}),
            encoding="utf-8",
        )
        claude_dir = self.project / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "settings.local.json").write_text(
            json.dumps(
                {
                    "mcpServers": {"other": {"command": "other"}},
                    "hooks": {
                        "Foo": [],
                        "UserPromptSubmit": [{"type": "command", "command": "ctx hook ingest --project-path /tmp/x --event UserPromptSubmit"}],
                        "PreToolUse": [
                            {"type": "command", "command": "ctx hook ingest --project-path /tmp/x --event PreToolUse"},
                            {"matcher": {"tools": ["BashTool"]}, "hooks": [{"type": "command", "command": "echo old"}]},
                        ],
                    },
                    "custom": 1,
                }
            ),
            encoding="utf-8",
        )
        codex_dir = self.project / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        (codex_dir / "config.toml").write_text(
            '\n'.join(
                [
                    'model = "gpt-5.3-codex"',
                    "",
                    '[mcp_servers."other"]',
                    'command = "other"',
                    "",
                    '[mcp_servers."ctx-memory"]',
                    'command = "ctx"',
                    'args = ["mcp", "serve", "--project-path", "/tmp/old"]',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (self.project / ".cursor" / "rules").mkdir(parents=True, exist_ok=True)
        (self.project / ".cursor" / "rules" / "overall.md").write_text(
            "custom cursor content\n",
            encoding="utf-8",
        )
        (self.project / ".claude" / "Claude.md").write_text(
            "custom claude content\n",
            encoding="utf-8",
        )
        (self.project / "AGENTS.md").write_text(
            "custom codex content\n",
            encoding="utf-8",
        )

        out = self.run_ctx(["init", "--path", str(self.project)])
        self.assertIn("Initialized project integration", out.stdout)
        self.assertIn("Codex config:", out.stdout)
        out_second = self.run_ctx(["init", "--path", str(self.project)])
        self.assertIn("Initialized project integration", out_second.stdout)

        cursor_cfg = json.loads((self.project / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
        self.assertTrue(cursor_cfg.get("custom"))
        self.assertIn("ctx-memory", cursor_cfg.get("mcpServers", {}))
        self.assertIn("other", cursor_cfg.get("mcpServers", {}))

        claude_cfg = json.loads((self.project / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        self.assertEqual(claude_cfg.get("custom"), 1)
        self.assertIn("ctx-memory", claude_cfg.get("mcpServers", {}))
        self.assertIn("hooks", claude_cfg)
        self.assertIn("UserPromptSubmit", claude_cfg["hooks"])
        self.assertIn("PreToolUse", claude_cfg["hooks"])
        self.assertIn("PostToolUse", claude_cfg["hooks"])
        self.assertIn("Stop", claude_cfg["hooks"])
        for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"):
            entries = claude_cfg["hooks"][event]
            self.assertTrue(isinstance(entries, list) and len(entries) >= 1)
            first = entries[0]
            self.assertIsInstance(first, dict)
            self.assertIn("hooks", first)
            self.assertTrue(isinstance(first["hooks"], list) and len(first["hooks"]) >= 1)
        # Tool hooks should include a ctx entry with string matcher format expected by Claude.
        def _has_ctx_tool_entry(entries, event_name: str) -> bool:
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if entry.get("matcher") != "*":
                    continue
                hooks = entry.get("hooks")
                if not isinstance(hooks, list):
                    continue
                for hook in hooks:
                    if (
                        isinstance(hook, dict)
                        and hook.get("type") == "command"
                        and isinstance(hook.get("command"), str)
                        and f"--event {event_name}" in hook.get("command")
                        and "ctx hook ingest --project-path" in hook.get("command")
                    ):
                        return True
            return False

        self.assertTrue(_has_ctx_tool_entry(claude_cfg["hooks"]["PreToolUse"], "PreToolUse"))
        self.assertTrue(_has_ctx_tool_entry(claude_cfg["hooks"]["PostToolUse"], "PostToolUse"))
        # Legacy direct command entries should be removed.
        for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"):
            for entry in claude_cfg["hooks"][event]:
                self.assertNotEqual(entry.get("type"), "command")

        codex_text = (self.project / ".codex" / "config.toml").read_text(encoding="utf-8")
        self.assertIn('model = "gpt-5.3-codex"', codex_text)
        self.assertIn('[mcp_servers."other"]', codex_text)
        self.assertIn('[mcp_servers."ctx-memory"]', codex_text)
        self.assertIn(f'--project-path", "{self.project.resolve()}"', codex_text)
        self.assertEqual(codex_text.count('[mcp_servers."ctx-memory"]'), 1)
        cursor_rules = (self.project / ".cursor" / "rules" / "overall.md").read_text(encoding="utf-8")
        self.assertIn("custom cursor content", cursor_rules)
        self.assertIn("ctx-memory-rules:cursor", cursor_rules)
        self.assertEqual(cursor_rules.count("ctx-memory-rules:cursor"), 1)
        claude_rules = (self.project / ".claude" / "Claude.md").read_text(encoding="utf-8")
        self.assertIn("custom claude content", claude_rules)
        self.assertIn("ctx-memory-rules:claude", claude_rules)
        self.assertEqual(claude_rules.count("ctx-memory-rules:claude"), 1)
        codex_rules = (self.project / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("custom codex content", codex_rules)
        self.assertIn("ctx-memory-rules:codex", codex_rules)
        self.assertIn('{"client":"codex"}', codex_rules)
        self.assertEqual(codex_rules.count("ctx-memory-rules:codex"), 1)
        self.assertIn(".context-memory/", (self.project / ".gitignore").read_text(encoding="utf-8"))

    def test_init_codex_invalid_toml_force_overwrite(self):
        codex_dir = self.project / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        (codex_dir / "config.toml").write_text('model = "broken\n', encoding="utf-8")

        failed = self.run_ctx(["init", "--path", str(self.project)], expected=1)
        self.assertIn("Invalid TOML", failed.stdout)

        out = self.run_ctx(["init", "--path", str(self.project), "--force"])
        self.assertIn("Initialized project integration", out.stdout)
        codex_text = (self.project / ".codex" / "config.toml").read_text(encoding="utf-8")
        self.assertIn('[mcp_servers."ctx-memory"]', codex_text)

    def test_codex_agent_and_adapter_configuration(self):
        log_path = self.base / "codex.log"
        log_path.write_text("", encoding="utf-8")

        cfg = self.run_ctx(["adapter", "configure", "codex", "--log-path", str(log_path)])
        self.assertIn("Configured codex log path", cfg.stdout)

        self.run_ctx(["start", "--path", str(self.project), "--name", "codex-agent", "--agent", "codex"])
        self.run_ctx(["stop", "--path", str(self.project)])

        db_path = self.project / ".context-memory" / "context.db"
        with sqlite3.connect(db_path) as conn:
            session = conn.execute("SELECT agent FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
            self.assertIsNotNone(session)
            self.assertEqual(session[0], "codex")

    def test_rules_command_applies_specific_tool_only(self):
        out = self.run_ctx(["rules", "codex", "--path", str(self.project)])
        self.assertIn("Codex rules:", out.stdout)
        self.assertTrue((self.project / "AGENTS.md").exists())
        self.assertFalse((self.project / ".cursor" / "rules" / "overall.md").exists())
        self.assertFalse((self.project / ".claude" / "Claude.md").exists())

        codex_rules = (self.project / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("ctx-memory-rules:codex", codex_rules)
        self.assertIn('{"client":"codex"}', codex_rules)

        out_second = self.run_ctx(["rules", "codex", "--path", str(self.project)])
        self.assertIn("already present", out_second.stdout)

    def test_mcp_server_append_event_and_doctor(self):
        self.run_ctx(["init", "--path", str(self.project)])
        self.run_ctx(["start", "--path", str(self.project), "--name", "mcp-demo", "--agent", "auto"])

        proc = subprocess.Popen(
            [sys.executable, "-m", "context_agent.cli", "mcp", "serve", "--project-path", str(self.project)],
            cwd=ROOT,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            init_resp = self._mcp_request(proc, 1, "initialize", {"clientInfo": {"name": "test", "version": "1"}})
            self.assertIn("result", init_resp)

            tools_resp = self._mcp_request(proc, 2, "tools/list", {})
            tools = tools_resp["result"]["tools"]
            tool_names = {tool["name"] for tool in tools}
            self.assertIn("append_event", tool_names)
            self.assertIn("get_context", tool_names)

            self._mcp_request(
                proc,
                3,
                "tools/call",
                {"name": "ping", "arguments": {"client": "codex"}},
            )
            self._mcp_request(
                proc,
                4,
                "tools/call",
                {
                    "name": "append_event",
                    "arguments": {
                        "client": "codex",
                        "event_type": "decision_made",
                        "summary": "Use MCP tool events for continuity.",
                        "files_touched": ["src/a.py"],
                        "decision": True,
                    },
                },
            )
            context_resp = self._mcp_request(
                proc,
                5,
                "tools/call",
                {"name": "get_context", "arguments": {"max_events": 5}},
            )
            content_blob = context_resp["result"]["content"][0]["text"]
            parsed = json.loads(content_blob)
            self.assertEqual(Path(parsed["project"]).resolve(), self.project.resolve())
            self.assertGreaterEqual(len(parsed["recent_events"]), 1)
        finally:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

        doctor = self.run_ctx(["doctor", "--path", str(self.project), "--json"])
        payload = json.loads(doctor.stdout)
        self.assertIn("checks", payload)
        self.assertIn("cursor_mcp", payload["checks"])
        self.assertIn("codex_mcp", payload["checks"])
        self.assertIn(payload["checks"]["codex_mcp"]["status"], {"connected", "degraded", "unavailable"})
        self.assertIn(payload["checks"]["cursor_mcp"]["status"], {"connected", "degraded", "unavailable"})

        db_path = self.project / ".context-memory" / "context.db"
        with sqlite3.connect(db_path) as conn:
            source = conn.execute(
                "SELECT source FROM events WHERE source LIKE 'mcp:%' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(source)
            self.assertEqual(source[0], "mcp:codex")

        self.run_ctx(["stop", "--path", str(self.project)])

    def test_mcp_server_jsonl_transport(self):
        self.run_ctx(["init", "--path", str(self.project)])
        self.run_ctx(["start", "--path", str(self.project), "--name", "mcp-jsonl", "--agent", "auto"])

        proc = subprocess.Popen(
            [sys.executable, "-m", "context_agent.cli", "mcp", "serve", "--project-path", str(self.project)],
            cwd=ROOT,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            init_resp = self._mcp_request_jsonl(proc, 1, "initialize", {"clientInfo": {"name": "cursor", "version": "1"}})
            self.assertIn("result", init_resp)
            ping_resp = self._mcp_request_jsonl(
                proc,
                2,
                "tools/call",
                {"name": "ping", "arguments": {"client": "cursor"}},
            )
            blob = ping_resp["result"]["content"][0]["text"]
            parsed = json.loads(blob)
            self.assertTrue(parsed["pong"])
            self.assertEqual(parsed["client"], "cursor")
        finally:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

        self.run_ctx(["stop", "--path", str(self.project)])

    def test_hook_ingest_records_summary_only_event(self):
        self.run_ctx(["init", "--path", str(self.project)])
        self.run_ctx(["start", "--path", str(self.project), "--name", "hook-demo", "--agent", "auto"])
        hook_payload = {
            "summary": "User asked to refactor auth middleware.",
            "files_touched": ["src/auth.py"],
            "raw_prompt": "this must not be stored",
        }
        out = self.run_ctx(
            [
                "hook",
                "ingest",
                "--project-path",
                str(self.project),
                "--event",
                "UserPromptSubmit",
            ],
            input_text=json.dumps(hook_payload),
        )
        self.assertIn("Hook event ingested", out.stdout)
        self.run_ctx(["stop", "--path", str(self.project)])

        db_path = self.project / ".context-memory" / "context.db"
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT event_type, source, summary FROM events WHERE source = 'hook:claude' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "user_intent")
            self.assertEqual(row[1], "hook:claude")
            self.assertIn("refactor auth middleware", row[2].lower())
            cols = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
            self.assertNotIn("raw_prompt", cols)


if __name__ == "__main__":
    unittest.main()
