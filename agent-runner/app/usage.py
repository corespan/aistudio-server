"""The usage record: one row per Claude turn, and one per limit hit (docs/agent-runner.md#usage-and-limits).

The limits are checked against this table, so it is the record of what the subscription spent.
Counts only, no prompt text; kept indefinitely. With no DATABASE_URL it lives in memory.

Each Claude step writes a row per turn as the turn ends, then one ``adjust`` row: Claude Code's
final totals minus the turns' sum (its stream undercounts output, P0). So a step's rows add up to
exactly what Claude Code reported, and that is what the Langfuse spans show too.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from app.config import settings

KEYS = {"input": "input_tokens", "output": "output_tokens", "cache_read": "cache_read_input_tokens",
        "cache_write": "cache_creation_input_tokens"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    sub         text NOT NULL,
    run_id      text NOT NULL,
    task        text NOT NULL,
    agent       text NOT NULL,
    model       text NOT NULL,
    message_id  text,
    kind        text NOT NULL,
    input       bigint NOT NULL,
    output      bigint NOT NULL,
    cache_read  bigint NOT NULL,
    cache_write bigint NOT NULL,
    weighted    double precision NOT NULL,
    cost_usd    double precision
);
CREATE INDEX IF NOT EXISTS usage_sub_ts ON usage (sub, ts);
CREATE TABLE IF NOT EXISTS limit_events (
    ts      timestamptz NOT NULL DEFAULT now(),
    sub     text NOT NULL,
    run_id  text NOT NULL,
    task    text,
    name    text NOT NULL,
    value   double precision NOT NULL,
    cap     double precision NOT NULL,
    action  text NOT NULL
);
"""
_COLUMNS = ("ts", "sub", "run_id", "task", "agent", "model", "message_id", "kind", *KEYS, "weighted", "cost_usd")


def weighted(row: dict) -> float:
    """The limit unit: cache reads count a tenth, as they cost. Same formula as sandbox-mcp's."""
    return row["input"] + row["cache_write"] + row["output"] + 0.1 * row["cache_read"]


def counts(api_usage: dict) -> dict:
    """Claude's usage object as our columns."""
    return {k: int(api_usage.get(v) or 0) for k, v in KEYS.items()}


class Usage:
    def __init__(self, pool=None):
        self.pool = pool
        self.rows: list[dict] = []        # in memory, without a database
        self.limits: list[dict] = []

    async def setup(self) -> None:
        if self.pool:
            async with self.pool.connection() as c:
                for statement in filter(str.strip, _SCHEMA.split(";")):
                    await c.execute(statement)

    async def add(self, *, sub: str, run_id: str, task: str, agent: str, model: str, kind: str,
                  tokens: dict, message_id: str | None = None, cost_usd: float | None = None) -> dict:
        row = {"ts": datetime.now(UTC), "sub": sub, "run_id": run_id, "task": task, "agent": agent,
               "model": model, "message_id": message_id, "kind": kind, **tokens,
               "weighted": weighted(tokens), "cost_usd": cost_usd}
        if self.pool:
            async with self.pool.connection() as c:
                await c.execute(f"INSERT INTO usage ({', '.join(_COLUMNS[1:])}) "
                                f"VALUES ({', '.join(['%s'] * (len(_COLUMNS) - 1))})",
                                [row[k] for k in _COLUMNS[1:]])
        else:
            self.rows.append(row)
        return row

    async def limit(self, *, sub: str, run_id: str, task: str | None, name: str, value: float,
                    cap: float, action: str) -> None:
        event = {"ts": datetime.now(UTC), "sub": sub, "run_id": run_id, "task": task, "name": name,
                 "value": value, "cap": cap, "action": action}
        if self.pool:
            async with self.pool.connection() as c:
                await c.execute("INSERT INTO limit_events (sub, run_id, task, name, value, cap, action)"
                                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                                (sub, run_id, task, name, value, cap, action))
        else:
            self.limits.append(event)

    async def _since(self, sub: str, since: datetime) -> list[dict]:
        if not self.pool:
            return [r for r in self.rows if r["sub"] == sub and r["ts"] >= since]
        async with self.pool.connection() as c:
            cur = await c.execute(f"SELECT {', '.join(_COLUMNS)} FROM usage WHERE sub = %s AND ts >= %s"
                                  " ORDER BY ts", (sub, since))
            return await cur.fetchall()

    async def windows(self, sub: str) -> dict:
        """Spend in the rolling 5-hour and 24-hour windows, what is left, and when the oldest
        usage in each drops out (the earliest the window frees up)."""
        now = datetime.now(UTC)
        rows = await self._since(sub, now - timedelta(hours=24))
        out = {}
        for name, hours, cap in (("5h", 5, settings.claude_window_5h_weighted),
                                 ("day", 24, settings.claude_day_weighted)):
            win = [r for r in rows if r["ts"] >= now - timedelta(hours=hours)]
            used = sum(r["weighted"] for r in win)
            out[name] = {"used": round(used), "cap": cap, "left": max(round(cap - used), 0),
                         "frees_from": (win[0]["ts"] + timedelta(hours=hours)).isoformat() if win else None}
        return out

    async def report(self, sub: str, since: datetime, by: str) -> list[dict]:
        """Totals per day, run or agent."""
        key = {"day": lambda r: r["ts"].date().isoformat(), "run": lambda r: r["run_id"],
               "agent": lambda r: r["agent"]}[by]
        totals: dict = defaultdict(lambda: {**dict.fromkeys(KEYS, 0), "weighted": 0.0, "cost_usd": 0.0,
                                           "turns": 0})
        for r in await self._since(sub, since):
            t = totals[key(r)]
            for k in KEYS:
                t[k] += r[k]
            t["weighted"] += r["weighted"]
            t["cost_usd"] += r["cost_usd"] or 0.0
            t["turns"] += r["kind"] == "turn"
        return [{by: k, **v, "weighted": round(v["weighted"]), "cost_usd": round(v["cost_usd"], 4)}
                for k, v in totals.items()]


store = Usage()     # main.py swaps in the database-backed one at start
