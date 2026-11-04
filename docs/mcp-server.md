# MCP Server

AI Studio ships an [MCP](https://modelcontextprotocol.io) tool server so an MCP client (Claude Code,
Claude Desktop, VS Code, Cursor, …) can read benchmark results, follow tasks, start runs and
diagnose the platform. It is tools only: no model and no reasoning loop; the client brings those.

It runs in Docker Compose under the `mcp` profile and is reached **only through
[agentgateway](https://agentgateway.dev)**:

```
MCP client ──HTTPS + Keycloak token──▶ agentgateway :8050 /mcp/aistudio ──same token──▶ aistudio-mcp :8081 /mcp ──▶ api :8001
     │                                verify token (Keycloak keys)          verify token again          (internal network)
     │ login (browser, PKCE)          role → tool rules (CEL)               role backstop, write switch,
     └──────────────────────────────▶ /realms/aistudio ──▶ keycloak :8080    result fence, audit log
```

Users log in to [Keycloak](https://www.keycloak.org) (Apache-2.0) from their MCP client; there are no
shared tokens to hand out. `aistudio-mcp` publishes no port, and the gateway is the only way in. Both
check the same Keycloak token (ES256, audience `aistudio-mcp`), so a caller that bypasses the gateway
still gets nothing.

## Set up

```bash
make mcp-init        # build the image; Keycloak passwords; local CA + gateway TLS cert (all gitignored)
make mcp-up          # renew the TLS cert if needed, then docker compose --profile mcp up --build -d
make mcp-user NAME=alice ROLE=operator     # prints alice's one-time password
make mcp-check       # per role over HTTPS: Keycloak login, initialize, tools/list, two tool calls
```

`MCP_PUBLIC_HOST` (in `.env`) is the name or IP clients use for the gateway and its login pages; it
becomes Keycloak's issuer, so it must be the one address everyone uses. Left blank, `make` fills in
this host's first address and writes it to `.env`; check it is the right one. After changing it, run
`make mcp-up`.

| Service | Port | Purpose |
|---|---|---|
| `agentgateway` | **8050** (`AGW_PORT`) | The MCP front door: TLS, token check, role → tool rules; Keycloak's login pages |
| `keycloak` | 8080 internal; admin console on **127.0.0.1:8180** | Users, roles, logins |
| `aistudio-mcp` | 8081, not published | The tools |
| `aistudio-docker-proxy` | 2375, internal network only | Read-only Docker API for the container tools |

### TLS

The gateway serves HTTPS only. `make mcp-init` creates a small local CA in `MCP_SECRETS_DIR`
(`~/.aistudio-mcp` unless set; `ca.key` there, `tls/ca.pem`, valid 10 years) and uses it to sign the
gateway's certificate (`tls/cert.pem`, `tls/key.pem`, valid 1 year) for `MCP_PUBLIC_HOST` plus every
name in `MCP_TLS_HOSTS`. That directory is outside the repo because `api` and `worker` mount the repo;
it also holds `keycloak.env`. Nothing in it is tied to one machine:

- `MCP_TLS_HOSTS` (in `.env` or the environment, space separated) adds names and IPs, e.g.
  `MCP_TLS_HOSTS="mcp.example.com 10.0.0.5"`. Left blank, `make` detects this host's name and
  addresses (`hostname`, `hostname -I`) at run time, plus `localhost`.
- The CA can only sign the names it was created with (X.509 name constraints): trusting `ca.pem`
  trusts this gateway and nothing else, even if `ca.key` leaks.
- `make mcp-up` reissues the certificate when it is missing, expires within 30 days, or lacks one of
  those names, keeping the names it already had and the same CA, so clients need no change. Names
  the CA was not made for are left out (a new docker bridge IP, say); a new `MCP_PUBLIC_HOST` needs a
  new CA: delete `ca.key` and `tls/` in `MCP_SECRETS_DIR`, run `make mcp-tls`, and give clients the
  new `ca.pem`.
- Clients trust `tls/ca.pem` once; copy it to each client machine. Never copy `ca.key`.
- Deployments from before `MCP_SECRETS_DIR`: `make mcp-up` moves `gateway/keycloak.env` there and
  deletes `gateway/tls` (that CA could sign any name), so clients must trust the new `ca.pem` and
  should remove the old "AIStudio MCP CA" from their trust store.

### Users

While the realm has no AIStudio admin (a fresh install), `make mcp-up` creates one:
`MCP_ADMIN_USER` from `.env` (default `aistudio-admin`) with the admin role and a one-time password,
printed once and kept in `first-admin.txt` in `MCP_SECRETS_DIR` (owner only). Keycloak asks for a new
password at the first login; delete the file after that. Once any admin exists, nothing is created.
That admin, or anyone on this host, adds the rest:

```bash
make mcp-user NAME=alice ROLE=viewer      # new user: prints a one-time password, changed at first login
make mcp-user NAME=alice ROLE=admin       # change the role; ROLE=none removes it
```

The admin console listens on this host only. From your machine: `ssh -L 8180:localhost:8180 <host>`,
then http://localhost:8180, user `admin`, password `KC_BOOTSTRAP_ADMIN_PASSWORD` in
`keycloak.env` in `MCP_SECRETS_DIR`. There you can disable a user (cut off at their next token refresh, within
10 minutes), reset passwords, or set a role (user → Attributes → AIStudio role).
`make mcp-check` keeps three users of its own, `mcp-check-viewer`, `-operator` and `-admin`,
whose passwords are random and replaced on every run.

Keycloak keeps its data in the stack's one PostgreSQL (`postgres`), in its own `keycloak` database
owned by its own `keycloak` role; `make mcp-up` creates both (`make mcp-db`, `gateway/keycloak/db.sql`)
and sets the role's password from `keycloak.env`. Neither role may connect to the other's database,
but the image's `aistudio` superuser (the API's login) can reach both. Back up `pgdata` and you have
both.

Keycloak imports `gateway/keycloak/aistudio-realm.json` only when the realm does not exist yet, so
later edits to that file reach a running deployment only through the admin console (or by dropping
the `keycloak` database, which also drops every user).

## Connect a client

Claude Code:

```bash
export NODE_EXTRA_CA_CERTS=/path/to/aistudio-ca.pem   # tls/ca.pem in MCP_SECRETS_DIR, copied from the server
claude mcp add --transport http aistudio https://<MCP_PUBLIC_HOST>:8050/mcp/aistudio
```

The first call opens a browser to log in (in Claude Code, `/mcp` → aistudio → Authenticate). The
gateway's 401 points the client at Keycloak (`/realms/aistudio`), which is the authorization server it
talks to: the client registers itself there (dynamic client registration) and Keycloak asks the user
once to allow it. `make mcp-up` limits that registration to clients whose every URL is on `localhost`
or `127.0.0.1`, so no one can register a client that sends login codes elsewhere, and refuses claim
mappers on them, so no client can set its own `roles.aistudio`. Registration needs no login, and
Keycloak stops accepting new clients at 200: `make mcp-prune` removes self-registered clients no one
is logged in through (a removed client's next login fails until it is removed and added again in
that client, e.g. `claude mcp remove` / `add`).

MCP Inspector registers with a `client_uri` on github.com, which that limit refuses, so give it the
pre-registered public client instead: server → Settings → OAuth Settings → Client ID
`aistudio-mcp-clients` (PKCE, `localhost`/`127.0.0.1` redirects on any port), then connect. Its login
runs in the browser, so the browser must trust `ca.pem` too (Windows: install it under Trusted Root
Certification Authorities), and start Inspector with `NODE_EXTRA_CA_CERTS` pointing at it. Tokens live
10 minutes and refresh while the user's Keycloak session lasts (8 hours idle, 1 day at most).

Any client that speaks streamable HTTP and MCP's OAuth login works the same way, once it trusts the
CA: `curl --cacert ca.pem` (Windows' curl also needs `--ssl-no-revoke`: a local CA publishes no
revocation list), or the machine's trust store (Windows: Trusted Root Certification Authorities;
Ubuntu: copy to `/usr/local/share/ca-certificates/aistudio-ca.crt`, then `sudo update-ca-certificates`).
For scripts, `make -s mcp-token ROLE=viewer` prints a token of the matching `mcp-check-*` user.

Browser clients on another origin work too: the gateway answers CORS for any origin (no cookies,
so a page needs the token itself) and exposes `mcp-session-id`. It drops the `Origin` header before
aistudio-mcp, whose DNS-rebinding check only knows its compose name. To limit which sites can call
it, set `browser: false` in `agent-runner/platform/packs/aistudio/connectors/aistudio.yaml` (or
narrow `allowOrigins` in `agent-runner/app/registry.py`) and run `make registry`.

## Roles

The role is the Keycloak user's `aistudio_role` attribute, sent as the token's `roles.aistudio`
claim. Each tool has a minimum role (`mcp-server/app/policy.py`); the gateway rules are generated
from it into the aistudio connector file, and `make registry` renders `gateway/config.yaml` from
that. A test fails if either drifts
(`cd mcp-server && python -m app.manifest ../agent-runner/platform/packs/aistudio/connectors/aistudio.yaml`).

| Role | Tools |
|---|---|
| `viewer` | `workload_history`, `throughput_compare`, `check_service_health`, `platform_catalog`, `list_benchmarks`, `get_benchmark`, `summary`, `model_config`, `get_task_status`, `get_task_logs` |
| `operator` | viewer + `start_workload`, `start_jupyter`, and `stop_jupyter` / `delete_run_results` for tasks they started (when writes are on) |
| `admin` | operator + `stop_jupyter` / `delete_run_results` for any task, and the host probes: containers (`list_containers`, `docker_logs`, `inspect_container`, `container_diff`, `timeline`), files and git (`read_file`, `list_dir`, `grep_logs`, `git_log`, `git_show`), network (`network_check`, `list_listening_ports`, `tcpdump_capture`, `check_tls_cert`, `check_clock_skew`), `frontend_check`, `queue_status`, and GPU-node probes over SSH (`check_ssh_connectivity`, `check_node_docker`, `node_docker_logs`, `node_exec`) |

## Writes

`start_workload`, `start_jupyter`, `stop_jupyter` and `delete_run_results` exist only with
`MCP_WRITES_ENABLED=true` in `.env`; otherwise they are not registered at all, so they are not in
`tools/list`. They act as soon as they are called. `stop_jupyter` stops and removes a Jupyter
notebook (`jup-…` ids only); `delete_run_results` permanently deletes a benchmark run's recorded
results (`wl-…` ids only) and cancels nothing. An identical write retried within
`MCP_WRITE_DEDUP_SECONDS` (default 60) returns the first result instead of running again, and a
retry that arrives while the first call is still running waits for it. A refusal or an API error
comes back as a tool error (`isError`), never as a result.

**Who may stop a task.** Only the user who started it, or an admin. `start_workload` and
`start_jupyter` send the token's `sub` (the Keycloak user id) as `created_by`, which aistudio-server stores on the
workload (`workloads.created_by`) and returns from the task's status route; `stop_jupyter` and
`delete_run_results` read it back from there. The same rule covers a notebook's URL: only its
creator or an admin sees it in `get_task_status`, `workload_history` and `get_task_logs`, since the
URL is a way onto the GPU node. A task with no creator on record (started before this column existed, or
through the UI or REST API without one) can be stopped through MCP only by an admin. The rule binds
MCP callers only: the REST API has no authentication, so a direct caller can write any
`created_by` and delete anything on port 8002.

## Settings

Compose sets the MCP server's environment in `docker-compose.yml`; these come from `.env`:

| Setting | Default | What it decides |
|---|---|---|
| `AGW_PORT` | `8050` | The host port of the gateway (HTTPS) |
| `MCP_PUBLIC_HOST` | this host's first address | The name or IP clients use; Keycloak's issuer |
| `MCP_TLS_HOSTS` | this host's name and IPs | More names and IPs in the gateway's TLS certificate |
| `MCP_WRITES_ENABLED` | `false` | Whether the four write tools exist |
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

Every result, and every error, has credentials masked before the model sees it (`app/fence.py`:
private keys, URL passwords, bearer tokens and JWTs, HF/GitHub/AWS/Slack tokens, `*_PASSWORD=` style
values). That catches what a probe reads by accident; it is not a reason to point one at secrets.
The prompt-injection warning in the same file is a heuristic, not a control.

## The read-only boundary

Every tool except the four writes reads. "Read-only" is not a property a binary or a path has on its own,
so four gates enforce it — each closing a hole a coarser check leaves open.

### 1. Node commands — per option, not per binary

A binary allowlist is **not** sufficient, and neither is a list of write flags: several
read-only-looking binaries carry a write or exec mode, with no shell metacharacter involved for
a metacharacter check to catch, and the flag nobody listed is the one that gets used.

| Binary | The form that is not read-only |
|---|---|
| `numactl` | `numactl PROG ARGS` **runs PROG** — one entry grants every command on the node |
| `ip` | `ip link set eth0 down` takes the node off the network |
| `ethtool` | `-s` reconfigures the NIC |
| `ss` | `-K` kills sockets |
| `nvidia-smi` | `-pl` sets power limits, `-r` resets a GPU, `-e` toggles ECC |
| `dcgmi` | `config --set` changes node state |
| `sensors` | `-s` applies a config file |
| `nvcc` | `-ccbin PROG` / `--run` execute programs, `-o` writes files |
| `lstopo-no-graphics` | a plain argument is an output file it writes |
| `cat` | unrestricted, reads the node's private keys and cloud credentials |

So `app/tools/ssh_probes.py` allowlists **options**: `_SPECS` maps each command form (binary,
or binary + subcommand such as `nvidia-smi topo`) to the options it may take, every one of
which only prints, and to what its other arguments may look like (GPU ids, device names, field
lists: never a `/`, so never a file name). Anything else is refused, so an option nobody thought
of fails closed. `ip` takes an object and nothing but a show verb (`ip -s link show eth0`). A
binary is matched by its bare name, so `/tmp/x/lscpu` never runs, and `nvcc` takes only
`--version`.

`cat` is confined to `/sys` and `/proc`, with the path normalised first so
`/sys/../root/.ssh/id_rsa` does not slip through, and with `root`, `cwd`, `fd`, `environ`,
`cmdline`, `mem` and friends refused as path parts: `/proc/self/root/home/u/.ssh/id_rsa`
starts with `/proc/` and is the node's `/` to the kernel.

Shell chaining, globs and escapes are rejected before any of it, and the node's shell is
handed `shlex.join(tokens)`, so it runs exactly what the gate checked, quoted. A refusal
names the forms that binary allows.

**To extend:** add an option to that form's `_SPECS` entry, after checking the manpage that it
neither writes, executes nor takes a file name. Never add an argument pattern that allows `/`.

### 2. Container reads

`container_diff` reads through the Docker archive endpoint, which `READABLE_ROOTS` does
not cover. `CONTAINER_READABLE_ROOTS` bounds where it looks, and `repo_path` is
**required** and must name the same file. Roots alone are not enough — `/app` is a root
and so is `/app/.env`; requiring the pair is what stops a diff being used as a general
file reader. The roots are where each service's code runs from (`/app` and `/repo` in aistudio-mcp,
`/AIStudio/app` and `/AIStudio/alembic` in the api and worker, whose `/AIStudio` is the whole
checkout, `.env` included), and a symlink is not followed.

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

`network_check` (DNS, TCP, ping and `url=`) and `check_tls_cert` refuse link-local and
loopback targets (the cloud metadata service lives at `169.254.169.254`) and the Docker API
host, whose responses carry every container's env. Each resolves the name once, checks every
address, and connects to the checked address, so a DNS answer that changes in between
(rebinding) cannot reach a refused one. `frontend_check` applies the same check to every request
the page makes, redirects and subresources included (its browser resolves names itself, so it
does not get the pinning). Private ranges stay reachable: internal services are what these
tools diagnose.

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
