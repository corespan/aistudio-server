"""run_command answers instead of raising when a binary cannot be started."""
import asyncio

from app.tools import _shell


def test_a_binary_the_kernel_refuses_is_an_answer(monkeypatch):
    async def refused(*a, **kw):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(_shell.asyncio, "create_subprocess_exec", refused)
    code, out, err = asyncio.run(_shell.run_command("tcpdump", "-c", "1"))
    assert (code, out) == (126, "") and "permission denied" in err


def test_a_timed_out_command_keeps_its_output():
    import sys

    script = "import sys, time; print('pkt 1', flush=True); time.sleep(30)"
    code, out, err = asyncio.run(_shell.run_command(sys.executable, "-c", script, timeout=2))
    assert code == 124 and "pkt 1" in out and "timed out" in err


def test_output_is_capped(monkeypatch):
    import sys

    monkeypatch.setattr(_shell, "_MAX_BYTES", 1000)
    code, out, _ = asyncio.run(_shell.run_command(sys.executable, "-c", "print('x' * 100000)"))
    assert code == 0 and len(out) < 1100 and out.endswith("(output truncated)")
