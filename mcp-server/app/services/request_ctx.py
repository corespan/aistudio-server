"""The verified caller of the tool call being served; mcp_server._bridge sets it."""
from __future__ import annotations

import contextvars

caller: contextvars.ContextVar = contextvars.ContextVar("mcp_caller", default=None)
