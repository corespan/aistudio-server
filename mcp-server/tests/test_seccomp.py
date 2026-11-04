"""mcp-server/seccomp.json: Docker's default profile minus the file-handle syscalls. Offline."""
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def test_the_profile_never_allows_opening_files_by_handle():
    profile = json.loads((_ROOT / "mcp-server" / "seccomp.json").read_text(encoding="utf-8"))
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    names = {n for rule in profile["syscalls"] for n in rule["names"]}
    assert not names & {"open_by_handle_at", "name_to_handle_at"}
    assert {"read", "openat", "execve", "socket"} <= names   # still the default profile


def test_aistudio_mcp_runs_under_it():
    assert "seccomp=./mcp-server/seccomp.json" in (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
