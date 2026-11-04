# Sandbox

`sandbox-mcp` gives agents one tool, `sandbox_run`: run a short Python (standard library) or shell
program in a disposable container under [gVisor](https://gvisor.dev), with no network, and get
back its exit code, output and the text files it wrote. Agents use it to compute over data they
already fetched: sort, aggregate, check numbers.

```
agent-runner ──JWT──> agentgateway /mcp/sandbox ──> sandbox-mcp ──> sandbox-docker-proxy ──> dockerd
                                                                               (containers only)  │
                                                              one container per call, runtime=runsc
```

It runs in the `mcp` compose profile. The `analyst` harness lists `sandbox_run`, and every role
may call it: it changes nothing outside a sandbox that is gone afterwards.

It also serves five workspace tools for the coding workflow ([Workspaces](#workspaces)).

## Every run

Fixed in `sandbox-mcp/app/sandbox.py`, never chosen by the caller:

| | |
|---|---|
| Runtime | `runsc` (gVisor): the program talks to gVisor's user-space kernel, not the host's |
| Network | none |
| User | nobody (65534), no capabilities, `no-new-privileges` |
| Limits | 1 CPU, 512 MB (no swap), 128 processes, `timeout_seconds` up to 60, then killed |
| Input | the program and `files` (plain names only, 2 MB in all), in `/tmp/work` |
| Output | stdout and stderr (20,000 characters each), and text files in `out/` (64,000 in all) |
| Afterwards | the container is removed, also when anything failed |

At most 4 sandboxes run at once, 2 per user. Output is fenced as untrusted data, like every MCP
tool result.

## Workspaces

A workspace is a checkout of a repo that lives across calls. The coding workflow's steps (clone,
Claude Code, a check, the diff) each run in their own container, and state carries between them
as snapshots. Only the runner calls these tools, as workflow steps, with the caller's JWT. They
are never offered to a model; operators and admins may call them, viewers can't.

| Tool | Does | Keeps the tree |
|---|---|---|
| `workspace_open(repo, ref, base?)` | Clones `repo` at `ref` through agentgateway-sandbox. The diff base is `ref`, or its merge base with `base` (a PR against its target) | yes: snapshot 0 |
| `workspace_claude(id, prompt, model, tools, max_turns, timeout_seconds)` | Runs `claude -p --output-format stream-json` with exactly these tools. Each stream line goes out as an MCP progress notification. Returns the final `result` event (usage, cost, turns) | yes: the next snapshot |
| `workspace_exec(id, command, timeout_seconds)` | The workflow's own fixed command, such as a test run. No token | no |
| `workspace_diff(id)` | The diff against the base, commits and new files included, the changed files, and the snapshot number it was taken on | no |
| `workspace_files(id, paths, snapshot?)` | File contents (base64, with git modes) from a snapshot, read in Python with no container: what the runner commits after your approval. A path ending in `/` means everything under it; `.git` never comes back | no |
| `workspace_close(id)` | Deletes the snapshots. Workspaces not closed expire after 24 hours | |

A workspace belongs to the JWT `sub` that opened it; anyone else is told it doesn't exist. One
step runs at a time per user (it spends that user's Claude subscription), two in all.

**Each step's container** (`sandbox-mcp/app/workspace.py`):

| | |
|---|---|
| Image | `aistudio-workspace:local` (`sandbox-mcp/workspace/Dockerfile`, 1.3 GB): Node, a pinned Claude Code, git, make, Python, the main app's test dependencies (on `PATH`) and agent-runner's (`/opt/runner-venv`), and PostgreSQL 15 with `with-postgres <command>`, which starts a throwaway database for one check. `make mcp-up` builds it |
| Runtime | `runsc`, as the node user (1000), no capabilities, `no-new-privileges` |
| Network | `aistudio-sandbox-egress`, an internal network where agentgateway-sandbox (`172.30.51.10`) is the only other thing. It serves `/llm/anthropic` and read-only git for `platform.yaml`'s `repos`, nothing else ([agent-runner.md](agent-runner.md#the-anthropic-route)). gVisor can't use Docker's DNS, so the address comes in as a hosts entry |
| Limits | 2 CPUs, 2 GB, 512 processes, `timeout_seconds` up to 900, then killed |
| Env | an explicit allowlist (`workspace.ENV`), checked by a test: nothing from sandbox-mcp's own environment. The caller's JWT goes only to the clone and the Claude step, never to a check running the repo's code |
| Afterwards | the container is removed, also when anything failed |

**Snapshots.** gVisor keeps writes to a container's own filesystem in its memory, so Docker
can't copy them out; a mount is outside that. A step that keeps the tree gets an anonymous
volume at `/out`, writes `/work` there as `snap.tgz` when its command ends, and sandbox-mcp
copies the file out (Docker's archive API) once the container exits; the volume goes with the
container. Not stdout: anything a step leaves running could write into it (P4 saw cut
snapshots that way). sandbox-mcp refuses a snapshot that isn't a regular file, has any entry
outside `work/`, or is over 200 MB, and stores the rest in the `sandbox-workspaces` volume. The
next step's container gets it back before it starts. The aistudio-server repo is about 13 MB.

Claude Code ignores the repo's own `.claude/settings.json` permission rules in a workspace (it
hasn't been trusted), so a repo can't widen the tools its agent was given.

**Git is read-only.** The gateway has routes for the two clone and fetch endpoints of each repo,
matched exactly, and drops the JWT before GitHub. A push finds no route. Pull requests come from
the runner after your approval (P5 of the coding workflow plan), never from a sandbox.

## Setup

gVisor goes on the Docker host. It needs no KVM: its default platform (systrap) runs on any
Linux kernel, including inside a VM.

```bash
R=20261012   # a pinned release
U=https://storage.googleapis.com/gvisor/releases/release/$R/x86_64
curl -fsSO $U/gvisor.tar.bz2 && curl -fsS $U/gvisor.tar.bz2.sha512 | cut -d' ' -f1 \
  | sed 's/$/  gvisor.tar.bz2/' | sha512sum -c -
tar -xjf gvisor.tar.bz2
sudo install -m 0755 runsc containerd-shim-runsc-v1 /usr/local/bin/
sudo cp /etc/docker/daemon.json /etc/docker/daemon.json.bak   # if it exists
sudo runsc install                  # adds "runsc" to daemon.json, keeps other runtimes
sudo systemctl reload docker        # running containers keep running
docker run --rm --runtime=runsc python:3.11-slim dmesg | head -1   # "Starting gVisor..."
```

`runsc install` also fetches its sidecar binaries from the same release. Then `make mcp-up`,
which pulls the sandbox image: sandbox-mcp's Docker proxy can't pull images.

A host without gVisor can set `SANDBOX_RUNTIME=runc` in `.env`. That is a plain container, with
the host kernel's attack surface: only for a trusted test install.

## The Docker API

sandbox-mcp reaches Docker through its own `docker-socket-proxy`, on a network only the two of
them join. The proxy allows containers (create, start, wait, logs, copy, kill, remove) and
nothing else: no exec, images, networks, volumes or build. That still lets it create *any*
container, privileged ones included, so whoever controls sandbox-mcp controls the host. That is
why it is small, holds no LLM, runs read-only as a non-root user, and only agentgateway reaches
it. Model output reaches it only as data it passes on (Claude Code's stream lines, as progress)
or checks (snapshot entries must stay under `work/`); it never acts on it.

## Not yet

- A per-run network: workspaces of different runs share `aistudio-sandbox-egress` today.
- A token scoped to one run for the Claude step, in place of the caller's JWT.
- Files by reference between tasks; today files go in and come back as text.
- Snapshots for faster starts. A run takes about half a second on the test host already.
