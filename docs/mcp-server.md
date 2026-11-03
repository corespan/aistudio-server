# MCP Server

AI Studio ships an [MCP](https://modelcontextprotocol.io) tool server so an MCP client (Claude Code,
Claude Desktop, VS Code, Cursor, …) can read benchmark results, follow tasks, start runs and
diagnose the platform. It is tools only: no model and no reasoning loop; the client brings those.

It runs in Docker Compose under the `mcp` profile and is reached **only through
[agentgateway](https://agentgateway.dev)**:

```
MCP client ──Bearer JWT──▶ agentgateway :8050 /mcp/aistudio ──same JWT──▶ aistudio-mcp :8081 /mcp ──▶ api :8001
                           verify JWT (gateway/jwks.json)        verify JWT again                    (internal network)
                           role → tool rules (CEL)               role backstop, write switch,
                                                                 result fence, audit log
```

`aistudio-mcp` publishes no port; the gateway is the only way in. Both check the same ES256 JWT
against the same key (`gateway/jwks.json`), so a caller that bypasses the gateway still gets nothing.

## Set up

```bash
make mcp-init        # build the image, make gateway/dev-key.pem + gateway/jwks.json (gitignored)
make mcp-up          # docker compose --profile mcp up --build -d  (api, worker, … plus the three MCP services)
make mcp-check       # per role: initialize, tools/list, two tool calls through the gateway
```

`make mcp-init` writes a fresh signing key each time it runs. After re-running it, restart the
gateway (`docker compose --profile mcp restart agentgateway`): agentgateway reads `jwks.json` only
at start, and every token minted with the old key stops working.

| Service | Port | Purpose |
|---|---|---|
| `agentgateway` | **8050** (`AGW_PORT`) | The MCP front door: JWT check and role → tool rules |
| `aistudio-mcp` | 8081, not published | The tools |
| `aistudio-docker-proxy` | 2375, internal network only | Read-only Docker API for the container tools |

## Connect a client

Mint a token for the role the client should have (default lifetime one hour):

```bash
make -s mcp-token ROLE=operator SUB=alice TTL=86400
```

Claude Code:

```bash
claude mcp add --transport http aistudio http://localhost:8050/mcp/aistudio \
  --header "Authorization: Bearer $(make -s mcp-token ROLE=viewer TTL=86400)"
```

Any client that speaks streamable HTTP takes the same URL and `Authorization` header. A token is a
bearer credential for its role until it expires: keep lifetimes short and `gateway/dev-key.pem` on
this host. Whoever holds that key can mint `admin`.

Browser clients on another origin work too: the gateway answers CORS for any origin (no cookies,
so a page needs the token itself) and exposes `mcp-session-id`. It drops the `Origin` header before
aistudio-mcp, whose DNS-rebinding check only knows its compose name. To limit which sites can call
it, narrow `allowOrigins` (or drop the `cors` policy) in `gateway/config.yaml` and restart the gateway.

## Roles

The role is the JWT's `roles.aistudio` claim. Each tool has a minimum role
(`mcp-server/app/policy.py`); the gateway rules in `gateway/config.yaml` are generated from it. A test fails if they drift
(`cd mcp-server && python -m app.manifest ../gateway/config.yaml`).

| Role | Tools |
|---|---|
| `viewer` | `workload_history`, `throughput_compare`, `check_service_health`, `platform_catalog`, `list_benchmarks`, `get_benchmark`, `summary`, `model_config`, `get_task_status`, `get_task_logs` |
| `operator` | viewer + `start_workload`, `start_jupyter`, and `stop_workload` for tasks they started (when writes are on) |
| `admin` | operator + `stop_workload` for any task, and the host probes: containers (`list_containers`, `docker_logs`, `inspect_container`, `container_diff`, `timeline`), files and git (`read_file`, `list_dir`, `grep_logs`, `git_log`, `git_show`), network (`network_check`, `list_listening_ports`, `tcpdump_capture`, `check_tls_cert`, `check_clock_skew`), `frontend_check`, `queue_status`, and GPU-node probes over SSH (`check_ssh_connectivity`, `check_node_docker`, `node_docker_logs`, `node_exec`) |

## Writes

`start_workload`, `start_jupyter` and `stop_workload` exist only with `MCP_WRITES_ENABLED=true` in
`.env`; otherwise they are not registered at all, so they are not in `tools/list`. They act as soon
as they are called. `stop_workload` stops and removes a Jupyter notebook (`jup-…`) or permanently
deletes a benchmark run's results (`wl-…`). An identical write repeated within
`MCP_WRITE_DEDUP_SECONDS` (default 600) returns the first result instead of running again.

**Who may stop a task.** Only the user who started it, or an admin. `start_workload` and
`start_jupyter` send the token's `sub` as `created_by`, which aistudio-server stores on the
workload (`workloads.created_by`) and returns from the task's status route; `stop_workload` reads
it back from there. A task with no creator on record (started before this column existed, or
through the UI or REST API without one) can be stopped through MCP only by an admin. The rule binds
MCP callers only: the REST API has no authentication, so a direct caller can write any
`created_by` and delete anything on port 8002.

## Settings

Compose sets the MCP server's environment in `docker-compose.yml`; these come from `.env`:

| Setting | Default | What it decides |
|---|---|---|
| `AGW_PORT` | `8050` | The host port of the gateway |
| `MCP_WRITES_ENABLED` | `false` | Whether the three write tools exist |
| `NODE_ALLOWED_CIDRS` | empty | GPU-node subnets the SSH probes may reach. Empty = node probes off |
| `SSH_KEY_PATH`, `SSH_DEFAULT_USER` | as for the worker | How the node probes log in |
| `SSH_HOST_KEY_POLICY` | `tofu` | `tofu` records a node's key on first contact; `strict` refuses unknown nodes |

`mcp-server/.env.example` lists every setting, for running the server outside Compose.

## Logs

One line per tool call (`mcp.tool.call`: tool, ok, ms, sub, role, client_id), and for a write one
more before it runs (`mcp.tool.write`, with its arguments):

```bash
docker compose --profile mcp logs -f aistudio-mcp agentgateway
```

## What it does not protect

The gateway guards the MCP door only. The REST API on port 8002 has no authentication of its own,
so keep that port off untrusted networks whether or not the MCP server runs.

## The read-only boundary

Every tool except the three writes reads. "Read-only" is not a property a binary or a path has on its own,
so four gates enforce it — each closing a hole a coarser check leaves open.

### 1. Node commands — per command, not per binary

A binary allowlist is **not** sufficient: several read-only-looking binaries carry a write
or exec mode, with no shell metacharacter involved for a metacharacter check to catch.

| Binary | The form that is not read-only |
|---|---|
| `numactl` | `numactl PROG ARGS` **runs PROG** — one entry grants every command on the node |
| `ip` | `ip link set eth0 down` takes the node off the network |
| `ethtool` | `-s` reconfigures the NIC |
| `ss` | `-K` kills sockets |
| `nvidia-smi` | `-pl` sets power limits, `-r` resets a GPU, `-e` toggles ECC |
| `dcgmi` | `config --set` changes node state |
| `sensors` | `-s` applies a config file |
| `cat` | unrestricted, reads the node's private keys and cloud credentials |

So `app/tools/ssh_probes.py` works in two tiers: `_SAFE_ANY_ARGS` for binaries with no
write or exec mode (`lscpu`, `nproc`, `free`, `df`, `lspci`, …), and `_SAFE_PREFIXES` for
those that have one, allowed only as explicit read-only prefixes (`nvidia-smi topo`,
`ip -s link show`, `numactl --hardware`, …). A prefix still refuses a write flag appended
after it (`ss -tn -K`, `nvidia-smi -q -r`): `_WRITE_FLAGS` lists them per binary.

`cat` is confined to `/sys` and `/proc`, with the path normalised first so
`/sys/../root/.ssh/id_rsa` does not slip through, and with `root`, `cwd`, `fd`, `environ`,
`cmdline`, `mem` and friends refused as path parts: `/proc/self/root/home/u/.ssh/id_rsa`
starts with `/proc/` and is the node's `/` to the kernel.

Shell chaining, globs and escapes are rejected before any of it, and the node's shell is
handed `shlex.join(tokens)`, so it runs exactly what the gate checked, quoted. A refusal
names the forms that binary allows.

**To extend:** add a prefix to the table. Do not grow a CLI grammar, and do not add a
binary to `_SAFE_ANY_ARGS` without checking its manpage for a write or exec mode.

### 2. Container reads

`container_diff` reads through the Docker archive endpoint, which `READABLE_ROOTS` does
not cover. `CONTAINER_READABLE_ROOTS` bounds where it looks, and `repo_path` is
**required** and must name the same file. Roots alone are not enough — `/app` is a root
and so is `/app/.env`; requiring the pair is what stops a diff being used as a general
file reader.

### 3. Host reads

`read_file`, `list_dir`, `grep_logs`, `git_log` and `git_show` resolve every path with
`realpath` and require it inside `READABLE_ROOTS`, so symlinks cannot escape (`grep_logs`
resolves every file it walks, not just the starting path). Secret-named files
(`_SECRET_NAMES`, case-insensitive) are refused, and `git_show` takes only a plain
revision (no `HEAD:.env` blob spec) and excludes the same names as pathspecs, so a commit
or stash that once held a secret does not print it. Names are not contents: keep secrets
out of those roots. Compose mounts only the tracked source, never the project root and
its `.env`.

### 5. Fetched bodies

`network_check(url=…)` returns the start of the response body, and `frontend_check` loads
the page. Both refuse link-local and loopback targets (the cloud metadata service lives
at `169.254.169.254`) and the Docker API host, whose responses carry every container's
env. Private ranges stay reachable: internal services are what these tools diagnose.

### 4. Argv splicing

No shell is involved anywhere, so caller text cannot inject a second command. What does
bite is a value spliced into argv where a leading `-` turns it into an option:

| Call site | What the option would do |
|---|---|
| `tcpdump_capture(bpf_filter=…)` | `-w` writes a file, `-z` runs a command — in a container holding `NET_RAW` |
| `git_show(ref=…)` | `--output=FILE` makes a read-only tool write |
| `network_check(host=…)` → ping | `-f` floods |

`app/tools/_shell.py:option_like()` is the shared guard.

### What the gates do not cover

- **Prompt injection.** Tool output is third-party text and can carry planted
  instructions. `app/fence.py` wraps every result, louder when the text pattern-matches
  an attempt. A mitigation, not a guarantee — treat tool output as data.
- **Symlinks inside a container.** `_container_path_allowed` is textual; there is no
  `realpath` over the archive API.
- **Host keys.** Node probes trust on first use by default
  (`SSH_HOST_KEY_POLICY=tofu`, recorded in `SSH_KNOWN_HOSTS`), `strict` to refuse any node
  not already recorded, and a changed key is always refused. Targets must be an IP, never
  loopback, link-local or unspecified, and inside `NODE_ALLOWED_CIDRS`; with it empty the node
  probes are off, because they connect with the mounted key.
  The SSH user is always `NODE_SSH_USER` (else `SSH_DEFAULT_USER`): no probe lets the caller pick another account.
  An admin accepts a re-imaged node by deleting its line from the file.
- **DNS rebinding on fetched URLs.** The target check resolves once; a name whose answer
  changes before the request is not caught.
