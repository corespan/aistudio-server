"""Every aistudio-server path the MCP tools call is one the server serves.

The route table is read from aistudio-server's own routers (app/routers/*.py) as text, because both
projects have a package called `app`.
"""
import pathlib
import re

import httpx
import pytest

from app.tools import aistudio

ROOT = pathlib.Path(__file__).resolve().parents[2]
ROUTERS = ROOT / "app" / "routers"

pytestmark = pytest.mark.skipif(not ROUTERS.exists(), reason="run from the repo, not the mcp-server image")


def _served():
    paths = []
    for src in ROUTERS.glob("*.py"):
        paths += re.findall(r'@router\.\w+\(\s*"([^"]+)"', src.read_text(encoding="utf-8"))
    return [re.compile("^" + re.sub(r"\{[^}]*\}", "[^/]+", p) + "$") for p in paths]


def _called():
    paths = set()
    for src in (ROOT / "mcp-server" / "app").rglob("*.py"):
        for raw in re.findall(r'f?"(/api/v1/[^"?]*)', src.read_text(encoding="utf-8")):
            paths.add(re.sub(r"\{[^}]*\}", "x", raw))       # an f-string placeholder = one path segment
    paths.discard("/api/v1/x")                              # platform_catalog's f"/api/v1/{k}": expanded below
    return paths | {f"/api/v1/{k}" for k in aistudio._CATALOG_KINDS} | {"/health"}


def test_every_path_the_tools_call_is_served():
    served = _served()
    assert len(served) > 20, "could not read aistudio-server's routers"
    missing = sorted(p for p in _called() if not any(r.match(p) for r in served))
    assert not missing, f"not served by aistudio-server (app/routers): {missing}"


class _Recorder:
    """Stands in for the tools' HTTP client: records the path, answers 404."""

    def __init__(self):
        self.paths = []

    def _not_found(self, path):
        self.paths.append(path)
        return httpx.Response(404, request=httpx.Request("GET", "http://aistudio" + path))

    async def get(self, path, **_):
        return self._not_found(path)

    def stream(self, method, path, **_):
        resp = self._not_found(path)

        class _Ctx:
            async def __aenter__(self):
                return resp

            async def __aexit__(self, *exc):
                return False
        return _Ctx()


@pytest.mark.parametrize("task_id, prefix", [("jup-20260101-abc123", "/api/v1/jupyter/instances/"),
                                             ("wl-20260101-abc123", "/api/v1/benchmarks/")])
async def test_task_status_and_logs_reach_the_route_for_their_kind(monkeypatch, task_id, prefix):
    rec = _Recorder()
    monkeypatch.setattr(aistudio, "_client", rec)
    assert "not found" in await aistudio.get_task_status.coroutine(task_id=task_id)
    assert "not found" in await aistudio.get_task_logs.coroutine(task_id=task_id)
    assert rec.paths == [f"{prefix}{task_id}/status", f"{prefix}{task_id}/logs/stream"]
    served = _served()
    assert all(any(r.match(p) for r in served) for p in rec.paths)
