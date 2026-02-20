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

        out = self.run_ctx(["init", "--path", str(self.project)])
        self.assertIn("Initialized project integration", out.stdout)

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
        self.assertIn(".context-memory/", (self.project / ".gitignore").read_text(encoding="utf-8"))

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
                {"name": "ping", "arguments": {"client": "cursor"}},
            )
            self._mcp_request(
                proc,
                4,
                "tools/call",
                {
                    "name": "append_event",
                    "arguments": {
                        "client": "cursor",
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
        self.assertIn(payload["checks"]["cursor_mcp"]["status"], {"connected", "degraded", "unavailable"})

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
