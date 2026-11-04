"""The HTTP API: auth, streaming a run, other callers' runs."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import app


def sse_events(response) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in response.iter_lines() if line.startswith("data: ")]


def test_a_run_streams_to_done_and_belongs_to_its_caller(fake_llm, fake_mcp, signer):
    scripts, _ = fake_llm
    scripts["worker"] = lambda m: "the api is up"
    with TestClient(app) as client:
        alice = {"Authorization": f"Bearer {signer('alice')}"}
        with client.stream("POST", "/runs", json={"message": "is it up?", "harness": "assistant"},
                           headers=alice) as r:
            assert r.status_code == 200
            events = sse_events(r)
        assert events[-1]["type"] == "done" and events[-1]["status"] == "ok"
        run_id = events[0]["run_id"]

        bob = {"Authorization": f"Bearer {signer('bob')}"}
        assert client.get(f"/runs/{run_id}", headers=bob).status_code == 404
        assert client.delete(f"/runs/{run_id}", headers=bob).status_code == 404

        # Reattach after the end replays the stream.
        with client.stream("GET", f"/runs/{run_id}", headers=alice) as r:
            assert sse_events(r)[-1]["type"] == "done"


def test_tokens_are_checked(signer):
    with TestClient(app) as client:
        assert client.post("/runs", json={"message": "x"}).status_code == 401
        gateway_only = signer(aud=("agentgateway", "mcp:aistudio"))
        r = client.post("/runs", json={"message": "x"}, headers={"Authorization": f"Bearer {gateway_only}"})
        assert r.status_code == 401
        assert client.get("/healthz").status_code == 200


def test_unknown_harness_is_refused(signer):
    with TestClient(app) as client:
        r = client.post("/runs", json={"message": "x", "harness": "root"},
                        headers={"Authorization": f"Bearer {signer()}"})
        assert r.status_code == 422


def test_harnesses_are_filtered_by_the_callers_tools(fake_mcp, signer):
    fake_mcp["tools"] = [t for t in fake_mcp["tools"] if t["name"] == "summary"]
    with TestClient(app) as client:
        r = client.get("/harnesses", headers={"Authorization": f"Bearer {signer()}"})
        assert {h["name"] for h in r.json()} == {"analyst", "assistant"}
        assert all(h["tools"] == ["summary"] for h in r.json())
