"""The run log: one row per run and one per stream event (plan, every tool call, every agent),
and the panel's conversations, whose turns are runs.

Lives in the agent_runner database next to LangGraph's checkpoint tables. With no DATABASE_URL
runs and events aren't kept; conversations live in memory, so the chat still works.

Deleting a conversation hides it from its owner; its runs and events stay, as the audit log of
what the agents did.
"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime

from psycopg.types.json import Jsonb

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id     text PRIMARY KEY,
    sub        text NOT NULL,
    message    text NOT NULL,
    status     text NOT NULL DEFAULT 'running',
    answer     text,
    created_at timestamptz NOT NULL DEFAULT now(),
    ended_at   timestamptz
);
CREATE TABLE IF NOT EXISTS run_events (
    run_id text NOT NULL REFERENCES runs ON DELETE CASCADE,
    seq    integer NOT NULL,
    type   text NOT NULL,
    task   text,
    agent  text,
    data   jsonb NOT NULL,
    at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS conversations (
    id         text PRIMARY KEY,
    sub        text NOT NULL,
    title      text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz
);
CREATE INDEX IF NOT EXISTS conversations_sub ON conversations (sub, updated_at);
ALTER TABLE runs ADD COLUMN IF NOT EXISTS conversation_id text REFERENCES conversations;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS context jsonb;
CREATE INDEX IF NOT EXISTS runs_conversation ON runs (conversation_id, created_at);
"""


class RunLog:
    def __init__(self, pool=None):
        self.pool = pool
        self.mem_convs: dict = {}       # without a database: id -> conversation
        self.mem_turns: dict = {}       # run id -> turn of a conversation

    async def setup(self) -> None:
        if self.pool:
            async with self.pool.connection() as c:
                # One statement per execute: the pool prepares every statement (the checkpointer
                # needs prepare_threshold=0), and a prepared statement holds only one.
                for statement in filter(str.strip, _SCHEMA.split(";")):
                    await c.execute(statement)

    async def start(self, run_id: str, sub: str, message: str, conversation_id: str | None = None,
                    context: dict | None = None) -> None:
        if self.pool:
            async with self.pool.connection() as c:
                await c.execute("INSERT INTO runs (run_id, sub, message, conversation_id, context)"
                                " VALUES (%s, %s, %s, %s, %s)",
                                (run_id, sub, message, conversation_id, Jsonb(context) if context else None))
                if conversation_id:
                    await c.execute("UPDATE conversations SET updated_at = now() WHERE id = %s",
                                    (conversation_id,))
        elif conversation_id:
            now = datetime.now(UTC)
            self.mem_turns[run_id] = {"run_id": run_id, "message": message, "answer": None, "status": "running",
                                      "context": context, "created_at": now}
            self.mem_convs[conversation_id]["turns"].append(run_id)
            self.mem_convs[conversation_id]["updated_at"] = now

    async def event(self, run_id: str, event: dict) -> None:
        if self.pool:
            async with self.pool.connection() as c:
                await c.execute(
                    "INSERT INTO run_events (run_id, seq, type, task, agent, data)"
                    " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (run_id, event["seq"], event["type"], event.get("task"), event.get("agent"),
                     Jsonb(event)))

    async def finish(self, run_id: str, status: str, answer: str | None) -> None:
        if run_id in self.mem_turns:
            self.mem_turns[run_id] |= {"status": status, "answer": answer}
        if self.pool:
            async with self.pool.connection() as c:
                await c.execute("UPDATE runs SET status = %s, answer = %s, ended_at = now()"
                                " WHERE run_id = %s", (status, answer, run_id))

    async def get(self, run_id: str) -> dict | None:
        if not self.pool:
            return None
        async with self.pool.connection() as c:
            cur = await c.execute("SELECT run_id, sub, message, status FROM runs WHERE run_id = %s",
                                  (run_id,))
            return await cur.fetchone()

    async def events(self, run_id: str) -> list[dict]:
        if not self.pool:
            return []
        async with self.pool.connection() as c:
            cur = await c.execute("SELECT data FROM run_events WHERE run_id = %s ORDER BY seq",
                                  (run_id,))
            return [row["data"] for row in await cur.fetchall()]

    # --- conversations: the panel's chats; each turn is a run ----------------------------------

    async def create_conversation(self, sub: str, title: str) -> dict:
        conv = {"id": "conv-" + secrets.token_hex(8), "sub": sub, "title": title,
                "created_at": datetime.now(UTC), "updated_at": datetime.now(UTC)}
        if self.pool:
            async with self.pool.connection() as c:
                await c.execute("INSERT INTO conversations (id, sub, title) VALUES (%s, %s, %s)",
                                (conv["id"], sub, title))
        else:
            self.mem_convs[conv["id"]] = {**conv, "deleted": False, "turns": []}
        return conv

    async def conversations(self, sub: str) -> list[dict]:
        """The caller's conversations, newest first, with how many turns each has."""
        if not self.pool:
            return [{k: c[k] for k in ("id", "title", "created_at", "updated_at")} | {"turns": len(c["turns"])}
                    for c in sorted(self.mem_convs.values(), key=lambda c: c["updated_at"], reverse=True)
                    if c["sub"] == sub and not c["deleted"]]
        async with self.pool.connection() as c:
            cur = await c.execute(
                "SELECT c.id, c.title, c.created_at, c.updated_at, count(r.run_id) AS turns FROM conversations c"
                " LEFT JOIN runs r ON r.conversation_id = c.id WHERE c.sub = %s AND c.deleted_at IS NULL"
                " GROUP BY c.id ORDER BY c.updated_at DESC", (sub,))
            return await cur.fetchall()

    async def conversation(self, sub: str, conv_id: str) -> dict | None:
        """One of the caller's conversations with its turns, or None (someone else's, deleted, or none)."""
        if not self.pool:
            c = self.mem_convs.get(conv_id)
            if c is None or c["sub"] != sub or c["deleted"]:
                return None
            return {k: c[k] for k in ("id", "title", "created_at", "updated_at")} | {
                "turns": [self.mem_turns[r] for r in c["turns"]]}
        async with self.pool.connection() as c:
            cur = await c.execute("SELECT id, title, created_at, updated_at FROM conversations"
                                  " WHERE id = %s AND sub = %s AND deleted_at IS NULL", (conv_id, sub))
            conv = await cur.fetchone()
            if conv is None:
                return None
            cur = await c.execute("SELECT run_id, message, answer, status, context, created_at FROM runs"
                                  " WHERE conversation_id = %s ORDER BY created_at", (conv_id,))
            return {**conv, "turns": await cur.fetchall()}

    async def delete_conversation(self, sub: str, conv_id: str) -> bool:
        """Hidden from its owner for good; its runs and events stay as the audit log."""
        if not self.pool:
            c = self.mem_convs.get(conv_id)
            if c is None or c["sub"] != sub or c["deleted"]:
                return False
            c["deleted"] = True
            return True
        async with self.pool.connection() as c:
            cur = await c.execute("UPDATE conversations SET deleted_at = now()"
                                  " WHERE id = %s AND sub = %s AND deleted_at IS NULL", (conv_id, sub))
            return cur.rowcount == 1

    async def history(self, conv_id: str, turns: int) -> list[dict]:
        """The last ``turns`` finished turns, oldest first: what a new turn's agents are given."""
        if not self.pool:
            done = [self.mem_turns[r] for r in self.mem_convs[conv_id]["turns"] if self.mem_turns[r]["answer"]]
        else:
            async with self.pool.connection() as c:
                cur = await c.execute("SELECT message, answer FROM runs WHERE conversation_id = %s"
                                      " AND answer IS NOT NULL ORDER BY created_at DESC LIMIT %s", (conv_id, turns))
                done = list(reversed(await cur.fetchall()))
        return [{"message": t["message"], "answer": t["answer"]} for t in done[-turns:]]
