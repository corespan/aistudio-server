# Third-Party Notices — aistudio sandbox MCP server
Inventory of the third-party Python packages resolved by `requirements.txt`
(the pinned output of `uv pip compile requirements.in`).
Generated: 2026-11-03 · Packages: 34 · Reciprocal: 1
> Regenerate with `make third-party`. CI fails if this file drifts from
> `requirements.txt`. Do not edit the table by hand.

## Scope
This file covers the **sandbox MCP server** (`sandbox-mcp/`, compose profile `mcp`)
Python dependency tree only. It does **not** cover:

- the sandbox image it starts (`python:3.11-slim` by default) — pulled from
  Docker Hub, under its own licences;
- gVisor (`runsc`, Apache-2.0), which the site installs on the Docker host;
- the sandbox-mcp image's Debian packages — see `../sbom/` (`make sbom-mcp`).

## Redistribution obligations
The packages below carry notice or source-availability obligations that
survive redistribution of a **binary** artifact. If CoreSpan ships a
prebuilt server image, wheel, or installer containing them, the
corresponding licence text must travel with it and, for LGPL, users must
retain the ability to substitute their own build of the library.

### certifi 2026.10.23 — `MPL-2.0`

MPL-2.0 is file-level copyleft. Unmodified, so MPL Section 3.2 is satisfied by shipping the licence text and pointing at the upstream source. Do not patch the bundled CA bundle without re-reading Section 3.3.

## Full inventory

| Package | Version | Licence | Project |
| --- | --- | --- | --- |
| `annotated-types` | 0.8.0 | MIT | https://github.com/annotated-types/annotated-types |
| `anyio` | 4.15.1 | MIT | https://pypi.org/project/anyio/ |
| `attrs` | 26.1.0 | MIT | https://pypi.org/project/attrs/ |
| `certifi` | 2026.10.23 | MPL-2.0 ⚠️ | https://github.com/certifi/python-certifi |
| `cffi` | 2.1.1 | MIT-0 | https://pypi.org/project/cffi/ |
| `charset-normalizer` | 3.5.2 | MIT | https://pypi.org/project/charset-normalizer/ |
| `click` | 8.5.0 | BSD-3-Clause | https://pypi.org/project/click/ |
| `cryptography` | 50.0.2 | Apache-2.0 OR BSD-3-Clause | https://pypi.org/project/cryptography/ |
| `docker` | 7.2.0 | Apache-2.0 | https://github.com/docker/docker-py |
| `h11` | 0.16.0 | MIT | https://github.com/python-hyper/h11 |
| `httpcore` | 1.0.9 | BSD-3-Clause | https://www.encode.io/httpcore/ |
| `httpx` | 0.28.1 | BSD-3-Clause | https://github.com/encode/httpx |
| `httpx-sse` | 0.4.3 | MIT | https://github.com/florimondmanca/httpx-sse |
| `idna` | 3.20 | BSD-3-Clause | https://pypi.org/project/idna/ |
| `jsonschema` | 4.26.0 | MIT | https://github.com/python-jsonschema/jsonschema |
| `jsonschema-specifications` | 2025.9.1 | MIT | https://github.com/python-jsonschema/jsonschema-specifications |
| `mcp` | 1.30.0 | MIT | https://modelcontextprotocol.io |
| `pycparser` | 3.12 | BSD-3-Clause | https://github.com/eliben/pycparser |
| `pydantic` | 2.14.2 | MIT | https://github.com/pydantic/pydantic |
| `pydantic-core` | 2.50.2 | MIT | https://github.com/pydantic/pydantic |
| `pydantic-settings` | 2.15.0 | MIT | https://github.com/pydantic/pydantic-settings |
| `pyjwt` | 2.15.1 | MIT | https://github.com/jpadilla/pyjwt |
| `python-dotenv` | 1.2.4 | BSD-3-Clause | https://pypi.org/project/python-dotenv/ |
| `python-multipart` | 0.0.32 | Apache-2.0 | https://github.com/Kludex/python-multipart |
| `referencing` | 0.37.0 | MIT | https://github.com/python-jsonschema/referencing |
| `requests` | 2.34.2 | Apache-2.0 | https://pypi.org/project/requests/ |
| `rpds-py` | 2026.9.1 | MIT | https://github.com/crate-py/rpds |
| `sse-starlette` | 3.6.0 | BSD-3-Clause | https://github.com/sysid/sse-starlette |
| `starlette` | 1.10.0 | BSD-3-Clause | https://github.com/Kludex/starlette |
| `structlog` | 26.1.0 | MIT OR Apache-2.0 | https://pypi.org/project/structlog/ |
| `typing-extensions` | 4.16.0 | PSF-2.0 | https://pypi.org/project/typing-extensions/ |
| `typing-inspection` | 0.4.4 | MIT | https://github.com/pydantic/typing-inspection |
| `urllib3` | 2.8.0 | MIT | https://pypi.org/project/urllib3/ |
| `uvicorn` | 0.54.0 | BSD-3-Clause | https://uvicorn.dev/ |

---

Licence strings are taken from each project's PyPI metadata (`license_expression`, falling back to trove classifiers). Where a project's metadata is ambiguous, the upstream LICENSE file governs.
