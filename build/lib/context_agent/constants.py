"""Shared constants for context-agent."""

DEFAULT_CAP_BYTES = 500 * 1024 * 1024
COMPACTION_THRESHOLD_RATIO = 0.85
DEDUPE_WINDOW_SECONDS = 30
SUMMARY_MAX_CHARS = 500
RECENT_EVENTS_DEFAULT = 5
RECORDING_STATES = {"recording", "stopped", "stopping"}
SUPPORTED_MCP_CLIENTS = ("cursor", "claude", "codex")
SUPPORTED_ADAPTERS = ("cursor", "claude", "codex")
EVENT_TYPES = {
    "user_intent",
    "agent_plan",
    "code_change",
    "revert",
    "decision_made",
    "tool_use",
    "test_result",
    "error_seen",
    "task_status",
    "handoff",
}
HIGH_VALUE_EVENT_TYPES = {"decision_made", "handoff", "error_seen", "tool_use", "revert"}
DELETED_FILE_HASH = "__deleted__"
PROJECT_MEMORY_DIR = ".context-memory"
PROJECT_DB_FILE = "context.db"
PROJECT_LOG_DIR = "logs"
REGISTRY_DB_FILE = "registry.db"
CONFIG_FILE = "config.toml"
