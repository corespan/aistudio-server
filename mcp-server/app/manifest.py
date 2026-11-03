"""Write / check the tool manifest and agentgateway's role x tool rules."""
import argparse
import json
import sys
from pathlib import Path

from .policy import ROLES, manifest

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "tool-manifest.json"

BEGIN = "# BEGIN generated: python -m app.manifest <this file> (from aistudio mcp-server app/policy.py)"
END = "# END generated"


def render() -> str:
    return json.dumps(manifest(), indent=2) + "\n"


def _q(names) -> str:
    return "[" + ", ".join(f"'{n}'" for n in names) + "]"


def cel_rules() -> list:
    """One allow rule per minimum role."""
    tools = manifest()["tools"]
    return [f"jwt.roles.aistudio in {_q(ROLES[i:])} && mcp.tool.name in "
            f"{_q(t['name'] for t in tools if t['minRole'] == role)}"
            for i, role in enumerate(ROLES) if any(t["minRole"] == role for t in tools)]


def render_block(indent: str) -> str:
    lines = [f"{indent}{BEGIN}"] + [f'{indent}- "{r}"' for r in cel_rules()] + [f"{indent}{END}"]
    return "\n".join(lines)


def rewrite(text: str) -> str:
    """Replace the generated block in ``text`` (keeps the markers' indent)."""
    start = text.index(BEGIN)
    indent = text[text.rindex("\n", 0, start) + 1:start]
    end = text.index(END, start) + len(END)
    return text[:start - len(indent)] + render_block(indent) + text[end:]


def _read(path) -> str:
    return Path(path).read_text(encoding="utf-8") if Path(path).exists() else ""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--check", action="store_true", help="fail if anything generated is stale")
    p.add_argument("configs", nargs="*", help="agentgateway config.yaml files with generated blocks")
    a = p.parse_args()
    wanted = {MANIFEST_PATH: render()}
    for path in a.configs:
        with open(path, encoding="utf-8") as f:
            wanted[Path(path)] = rewrite(f.read())
    stale = [path for path, text in wanted.items() if _read(path) != text]
    if a.check:
        for path in stale:
            print(f"{path.name} is stale: run 'python -m app.manifest'", file=sys.stderr)
        if not stale:
            print("tool manifest and gateway rules are up to date")
        return 1 if stale else 0
    for path in stale:
        path.write_text(wanted[path], encoding="utf-8", newline="\n")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
