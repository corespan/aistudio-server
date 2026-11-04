"""LLM access by logical model name, under the install's in-flight cap."""
from __future__ import annotations

import asyncio

from langchain_openai import ChatOpenAI

from app.config import settings

_inflight = asyncio.Semaphore(settings.max_llm_inflight)

_MODELS = {"planner": lambda: settings.model_planner, "worker": lambda: settings.model_worker,
           "summarizer": lambda: settings.model_summarizer}


def chat(role: str, token: str):
    """A chat model for a logical role (planner, worker, summarizer), called with the caller's JWT.

    Tests replace this.
    """
    return ChatOpenAI(base_url=settings.llm_base_url, api_key=token, max_retries=1,
                      model=_MODELS[role](), temperature=settings.llm_temperature,
                      timeout=settings.llm_timeout_seconds, max_tokens=settings.llm_max_tokens,
                      reasoning_effort=settings.llm_reasoning_effort or None)


async def ainvoke(model, messages, budget=None):
    """One LLM call within the in-flight cap; its tokens are charged to the run's budget."""
    async with _inflight:
        response = await model.ainvoke(messages)
    usage = getattr(response, "usage_metadata", None) or {}
    if budget is not None:
        budget.tokens += int(usage.get("total_tokens") or 0)
    return response
