"""run_command answers instead of raising when a binary cannot be started."""
import asyncio

from app.tools import _shell


def test_a_binary_the_kernel_refuses_is_an_answer(monkeypatch):
    async def refused(*a, **kw):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(_shell.asyncio, "create_subprocess_exec", refused)
    code, out, err = asyncio.run(_shell.run_command("tcpdump", "-c", "1"))
    assert (code, out) == (126, "") and "permission denied" in err
