"""The panel's conversations: earlier turns and the panel context reach the agents as data."""
from __future__ import annotations

from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage

from app.config import settings
from app.main import app
from tests.test_api import sse_events


def human(messages) -> str:
    return "\n".join(m.content for m in messages if isinstance(m, HumanMessage))


def turn(client, headers, message, **body) -> list[dict]:
    with client.stream("POST", "/runs", json={"message": message, "harness": "assistant", **body},
                       headers=headers) as r:
        assert r.status_code == 200, r.read()
        return sse_events(r)


def test_a_conversation_carries_earlier_turns_and_the_panel_context(fake_llm, fake_mcp, signer):
    scripts, calls = fake_llm
    answers = iter(["node-3 has the T4s.", "node-3 is healthy."])
    scripts["worker"] = lambda m: next(answers)
    with TestClient(app) as client:
        me = {"Authorization": f"Bearer {signer('alice')}"}
        conv = client.post("/conversations", json={"title": "nodes"}, headers=me).json()
        assert conv["id"].startswith("conv-")
        turn(client, me, "which node has T4s?", conversation_id=conv["id"])
        turn(client, me, "is it healthy?", conversation_id=conv["id"],
             context={"page": "nodes", "node": "node-3"})
        second = human(calls[-1][1])
        # The second turn's agent sees the first turn and where the user is, fenced as data.
        assert second.startswith("is it healthy?")
        assert "[UNTRUSTED DATA from earlier in this conversation" in second
        assert "User: which node has T4s?\nAnswer: node-3 has the T4s." in second
        assert "[UNTRUSTED DATA from the panel (where the user is now)" in second and '"node": "node-3"' in second
        got = client.get(f"/conversations/{conv['id']}", headers=me).json()
        assert [(t["message"], t["answer"], t["status"]) for t in got["turns"]] == [
            ("which node has T4s?", "node-3 has the T4s.", "ok"), ("is it healthy?", "node-3 is healthy.", "ok")]
        assert got["turns"][1]["context"] == {"page": "nodes", "node": "node-3"}
        [listed] = client.get("/conversations", headers=me).json()
        assert listed["id"] == conv["id"] and listed["turns"] == 2 and listed["title"] == "nodes"


def test_only_the_last_turns_go_back_to_the_agents(fake_llm, fake_mcp, signer, monkeypatch):
    monkeypatch.setattr(settings, "conversation_turns", 1)
    scripts, calls = fake_llm
    scripts["worker"] = lambda m: "ok"
    with TestClient(app) as client:
        me = {"Authorization": f"Bearer {signer('alice')}"}
        cid = client.post("/conversations", headers=me).json()["id"]
        for q in ("first question", "second question", "third question"):
            turn(client, me, q, conversation_id=cid)
        last = human(calls[-1][1])
        assert "second question" in last and "first question" not in last


def test_conversations_are_their_owners_alone_and_delete_hides_them(fake_llm, fake_mcp, signer):
    scripts, _ = fake_llm
    scripts["worker"] = lambda m: "fine"
    with TestClient(app) as client:
        alice = {"Authorization": f"Bearer {signer('alice')}"}
        bob = {"Authorization": f"Bearer {signer('bob')}"}
        cid = client.post("/conversations", headers=alice).json()["id"]
        turn(client, alice, "is it up?", conversation_id=cid)
        assert client.get(f"/conversations/{cid}", headers=bob).status_code == 404
        assert client.get("/conversations", headers=bob).json() == []
        r = client.post("/runs", json={"message": "x", "harness": "assistant", "conversation_id": cid}, headers=bob)
        assert r.status_code == 404
        assert client.delete(f"/conversations/{cid}", headers=bob).status_code == 404
        assert client.delete(f"/conversations/{cid}", headers=alice).status_code == 204
        assert client.get("/conversations", headers=alice).json() == []
        assert client.get(f"/conversations/{cid}", headers=alice).status_code == 404
        r = client.post("/runs", json={"message": "x", "harness": "assistant", "conversation_id": cid}, headers=alice)
        assert r.status_code == 404
        assert client.delete(f"/conversations/{cid}", headers=alice).status_code == 404


def test_the_panel_context_is_checked(signer):
    with TestClient(app) as client:
        me = {"Authorization": f"Bearer {signer('alice')}"}
        r = client.post("/runs", json={"message": "x", "harness": "assistant",
                                       "context": {"page": "nodes", "sql": "drop table"}}, headers=me)
        assert r.status_code == 422
        r = client.post("/runs", json={"message": "x", "conversation_id": "conv-nothex"}, headers=me)
        assert r.status_code == 422


def test_the_planner_gets_the_conversation_too(fake_llm, fake_mcp, signer):
    scripts, calls = fake_llm
    scripts["planner"] = lambda m: {"goal": "g", "tasks": [{"id": "t1", "agent": "assistant", "task": "check node-3"}]}
    scripts["worker"] = lambda m: "healthy"
    with TestClient(app) as client:
        me = {"Authorization": f"Bearer {signer('alice')}"}
        cid = client.post("/conversations", headers=me).json()["id"]
        for q in ("which node has T4s?", "and is it healthy?"):
            with client.stream("POST", "/runs", json={"message": q, "conversation_id": cid}, headers=me) as r:
                sse_events(r)
        planner_calls = [c for c in calls if c[0] == "planner"]
        assert "User: which node has T4s?" in human(planner_calls[-1][1])
        assert "say those specifics in the tasks" in human(planner_calls[-1][1])
