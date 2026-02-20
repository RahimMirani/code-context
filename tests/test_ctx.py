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

    def run_ctx(self, args: list[str], expected: int = 0):
        cmd = [sys.executable, "-m", "context_agent.cli"] + args
        result = subprocess.run(
            cmd,
            cwd=ROOT,
            env=self.env,
            capture_output=True,
            text=True,
        )
        if result.returncode != expected:
            raise AssertionError(
                f"Command failed: {' '.join(cmd)}\n"
                f"code={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
            )
        return result

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

        time.sleep(3.5)
        self.run_ctx(["stop", "--path", str(self.project)])

        db_path = self.project / ".context-memory" / "context.db"
        self.assertTrue(db_path.exists())
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
        time.sleep(1.0)

        tracked.write_text("v2", encoding="utf-8")
        time.sleep(1.0)
        tracked.write_text("v1", encoding="utf-8")
        time.sleep(1.0)

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


if __name__ == "__main__":
    unittest.main()
