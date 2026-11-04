.PHONY: up down logs migrate seed seed-real seed-h100 seed-mi210 seed-t4 seed-benchmarks wait-api test spec shell benchmark \
        compliance deps-lock third-party sbom check-models check-node-env \
        mcp-env mcp-init mcp-up mcp-db mcp-rabbit mcp-tls mcp-user mcp-prune mcp-token mcp-check mcp-test sbom-mcp

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f api worker

migrate:
	docker compose exec api alembic upgrade head

seed:
	docker compose exec api python -m app.services.catalog_seeder

wait-api:
	@echo "Waiting for API to accept requests..."
	@for i in $$(seq 1 30); do \
		curl -sf http://localhost:8002/health > /dev/null 2>&1 && echo "  API is ready." && exit 0; \
		echo "  [$$i/30] not ready yet, retrying in 3s..."; \
		sleep 3; \
	done; \
	echo "ERROR: API did not become ready after 90s." && exit 1

setup: up
	@echo "Waiting for services to start..."
	@sleep 5
	$(MAKE) migrate
	$(MAKE) seed
	$(MAKE) wait-api
	$(MAKE) seed-benchmarks
	@echo ""
	@echo "AIStudio Server is ready!"
	@echo "  API:         http://localhost:8002"
	@echo "  Swagger:     http://localhost:8002/docs"
	@echo "  RabbitMQ:    http://localhost:15672"

seed-real:
	docker compose exec api python scripts/seed_rtx5090_results.py

seed-h100:
	docker compose exec api python scripts/seed_h100_results.py

seed-mi210:
	docker compose exec api python scripts/seed_mi210_results.py

seed-t4:
	docker compose exec api python scripts/seed_t4_results.py

seed-gpu-specs:
	docker compose exec api python scripts/seed_gpu_specs.py

seed-benchmarks:
	$(MAKE) seed-gpu-specs
	$(MAKE) seed-real
	$(MAKE) seed-h100
	$(MAKE) seed-mi210
	$(MAKE) seed-t4

test:
	docker compose exec api pytest tests/ -v

spec:
	docker compose exec api python -c "from app.main import app; import json; print(json.dumps(app.openapi(), indent=2))" > api_contract/openapi.json
	@echo "OpenAPI spec exported to api_contract/openapi.json"

shell:
	docker compose exec api python

# ── MCP server (behind agentgateway) — see docs/mcp-server.md ─────────────────
# The address clients use for the gateway (and Keycloak's login pages): MCP_PUBLIC_HOST from the
# environment or .env, else this host's first address. mcp-init/mcp-up write it to .env for compose.
MCP_PUBLIC_HOST ?= $(shell sed -n 's/^MCP_PUBLIC_HOST=//p' .env 2>/dev/null)
ifeq ($(strip $(MCP_PUBLIC_HOST)),)
MCP_PUBLIC_HOST := $(firstword $(shell hostname -I 2>/dev/null) localhost)
endif
AGW_PORT ?= $(or $(shell sed -n 's/^AGW_PORT=//p' .env 2>/dev/null),8050)
export MCP_PUBLIC_HOST

# Keycloak passwords, the CA key and the gateway's TLS files live outside the repo, which api and
# worker mount read-write. Pinned in .env so compose and every later make use the same place.
MCP_SECRETS_DIR ?= $(or $(shell sed -n 's/^MCP_SECRETS_DIR=//p' .env 2>/dev/null),$(HOME)/.aistudio-mcp)
export MCP_SECRETS_DIR

# Names and IPs in the gateway's TLS cert: MCP_TLS_HOSTS from the environment or .env, else this
# host's name and addresses, detected at run time. MCP_PUBLIC_HOST is always added.
MCP_TLS_HOSTS ?= $(shell sed -n 's/^MCP_TLS_HOSTS=//p' .env 2>/dev/null)
ifeq ($(strip $(MCP_TLS_HOSTS)),)
MCP_TLS_HOSTS := $(shell hostname) $(shell hostname -I 2>/dev/null) localhost 127.0.0.1
endif

# The AIStudio admin make mcp-up creates when the realm has none (kc.py first_admin).
MCP_ADMIN_USER ?= $(or $(shell sed -n 's/^MCP_ADMIN_USER=//p' .env 2>/dev/null),aistudio-admin)

# mint.py (TLS) and kc.py (Keycloak) run in the aistudio-mcp image, so the host needs no Python packages.
# As the host user: the service drops every capability, so root in it cannot write the secrets, and
# the keys should belong to whoever runs make anyway.
MCP_RUN = docker compose --profile mcp run --rm --no-deps --user "$(shell id -u):$(shell id -g)" \
	-e MCP_TLS_HOSTS="$(MCP_PUBLIC_HOST) $(MCP_TLS_HOSTS)" -e MCP_PUBLIC_URL="https://$(MCP_PUBLIC_HOST):$(AGW_PORT)" \
	-e MCP_SECRETS=/secrets -e MCP_ADMIN_USER="$(MCP_ADMIN_USER)" \
	-v "$(MCP_SECRETS_DIR):/secrets" -v "$(CURDIR)/gateway:/gateway:ro" aistudio-mcp python
MCP_MINT = $(MCP_RUN) /gateway/mint.py
MCP_KC = $(MCP_RUN) /gateway/kc.py

mcp-env:
	@touch .env
	@for v in MCP_PUBLIC_HOST MCP_SECRETS_DIR; do grep -q "^$$v=" .env || echo "$$v=" >> .env; done
	@sed -i -e 's|^MCP_PUBLIC_HOST=$$|MCP_PUBLIC_HOST=$(MCP_PUBLIC_HOST)|' \
		-e 's|^MCP_SECRETS_DIR=$$|MCP_SECRETS_DIR=$(MCP_SECRETS_DIR)|' .env
	@mkdir -p -m 700 "$(MCP_SECRETS_DIR)"
	@# These lived in gateway/ before: keep the Keycloak passwords (its database has them) and
	@# retire the old CA (it could sign any name).
	@if [ -f gateway/keycloak.env ] && [ ! -f "$(MCP_SECRETS_DIR)/keycloak.env" ]; then \
		mv gateway/keycloak.env "$(MCP_SECRETS_DIR)/" && echo "moved gateway/keycloak.env to $(MCP_SECRETS_DIR)"; fi
	@if [ -d gateway/tls ] && [ ! -e "$(MCP_SECRETS_DIR)/tls" ]; then rm -rf gateway/tls && \
		echo "removed the old CA in gateway/tls: clients must trust the new $(MCP_SECRETS_DIR)/tls/ca.pem"; fi
	@test -s "$(MCP_SECRETS_DIR)/rabbitmq_password" || { umask 077; \
		head -c 24 /dev/urandom | base64 | tr -d '/+=\n' > "$(MCP_SECRETS_DIR)/rabbitmq_password"; }
	@echo "MCP public address: https://$(MCP_PUBLIC_HOST):$(AGW_PORT); secrets in $(MCP_SECRETS_DIR) (both in .env)"

mcp-init: mcp-env
	docker compose --profile mcp build aistudio-mcp
	$(MCP_KC) env
	$(MCP_MINT) tls

mcp-up: mcp-env
	@test -f "$(MCP_SECRETS_DIR)/keycloak.env" -a -f "$(MCP_SECRETS_DIR)/tls/ca.pem" || $(MAKE) mcp-init
	$(MCP_MINT) tls --if-needed
	$(MAKE) mcp-db
	$(MAKE) mcp-rabbit
	@# --remove-orphans: containers of services since removed (keycloak-db, pgadmin) go too.
	docker compose --profile mcp up --build -d --remove-orphans
	docker compose --profile mcp restart agentgateway
	$(MCP_KC) setup

mcp-rabbit:
	@# queue_status's own RabbitMQ user: monitoring tag, vhost access, no configure/write/read, so it
	@# lists queues but cannot publish or consume tasks. The password goes in on stdin, not argv.
	docker compose up -d --wait rabbitmq
	@docker compose exec -T rabbitmq sh -c 'read -r pw; \
		{ rabbitmqctl -q add_user aistudio-mcp "$$pw" 2>/dev/null || rabbitmqctl -q change_password aistudio-mcp "$$pw"; } && \
		rabbitmqctl -q set_user_tags aistudio-mcp monitoring && \
		rabbitmqctl -q set_permissions -p / aistudio-mcp "^$$" "^$$" "^$$"' < "$(MCP_SECRETS_DIR)/rabbitmq_password"
	@echo "rabbitmq: user aistudio-mcp (monitoring only)"

mcp-db:
	@# Keycloak's role and database in the shared postgres; the password goes in on stdin, not argv.
	docker compose up -d --wait postgres
	@{ printf "\\set pw '%s'\n" "$$(sed -n 's/^KC_DB_PASSWORD=//p' "$(MCP_SECRETS_DIR)/keycloak.env")"; \
		cat gateway/keycloak/db.sql; } | docker compose exec -T postgres psql -q -v ON_ERROR_STOP=1 -U aistudio -d postgres
	@echo "postgres: database keycloak, owned by role keycloak"

mcp-tls:
	@# Reissue for exactly MCP_PUBLIC_HOST + MCP_TLS_HOSTS with the same CA, so clients keep trusting it.
	$(MCP_MINT) tls
	-docker compose --profile mcp restart agentgateway

mcp-user:
	@# make mcp-user NAME=alice ROLE=viewer|operator|admin|none (a new user gets a one-time password)
	@$(MCP_KC) user $(NAME) $(ROLE)

mcp-prune:
	@# Remove self-registered clients no one is logged in through (anyone can register; Keycloak stops at 200).
	@$(MCP_KC) prune

mcp-token:
	@# A real Keycloak login token for the mcp-check-ROLE user, for curl and scripts.
	@$(MCP_KC) token $(or $(ROLE),viewer)

mcp-check:
	TOKEN='$(MCP_KC) token' bash gateway/check.sh

mcp-test:
	cd mcp-server && python -m pytest -q

# ── Licence compliance ────────────────────────────────────────────────────────
# These targets mirror .github/workflows/compliance.yml. Run `make compliance`
# before cutting a release — see RELEASE.md.

deps-lock:
	@# requirements.in holds the ranges we intend; requirements.txt is the pinned
	@# resolution that actually gets installed. Unpinned deps mean two installs a
	@# month apart resolve different transitive sets with different licences, and
	@# make benchmark numbers non-reproducible.
	uv pip compile requirements.in --python-version 3.11 -o requirements.txt
	uv pip compile requirements.in --python-version 3.11 --generate-hashes -o requirements.lock.txt
	@# --python-platform linux: the image is Linux; compiled on Windows, docker would pin pywin32.
	cd mcp-server && uv pip compile requirements.in --python-version 3.11 --python-platform linux -o requirements.txt
	cd mcp-server && uv pip compile requirements.in --python-version 3.11 --python-platform linux --generate-hashes -o requirements.lock.txt
	@echo "Now run 'make third-party' — the inventory must match the new pins."

third-party:
	python3 scripts/generate_third_party_notices.py

sbom:
	@# Needs syft and pull access to the workload registry:
	@#   gcloud auth configure-docker us-docker.pkg.dev
	./scripts/generate_sbom.sh

sbom-mcp:
	@# Needs only docker: builds aistudio-mcp, pulls agentgateway, docker-socket-proxy, keycloak.
	./scripts/generate_sbom.sh mcp

check-models:
	python3 scripts/check_model_access.py

check-node-env:
	@# Verifies HF_TOKEN reaches a GPU node the same way the worker sees it:
	@# a NON-INTERACTIVE ssh command. Testing with an interactive login instead
	@# is how you end up believing a ~/.bashrc export works when it does not.
	@test -n "$(NODE)" || (echo "usage: make check-node-env NODE=<host-or-user@host>" && exit 1)
	@ssh -o BatchMode=yes $(NODE) '\
		if [ -f $$HOME/.aistudio/env ]; then \
			set -a; . $$HOME/.aistudio/env; set +a; \
			if [ -n "$${HF_TOKEN:-}" ]; then \
				echo "  ok — HF_TOKEN loaded from ~/.aistudio/env (length $${#HF_TOKEN})"; \
			else \
				echo "  FAIL — ~/.aistudio/env exists but sets no HF_TOKEN"; exit 1; \
			fi; \
		else \
			echo "  ~/.aistudio/env not found."; \
			if [ -n "$${HF_TOKEN:-}" ]; then \
				echo "  NOTE — HF_TOKEN is set in the non-interactive environment by some"; \
				echo "         other means, so gated models will work."; \
			else \
				echo "  No HF_TOKEN. Gated models will fail unless weights are already cached."; \
				echo "  See MODEL-LICENSES.md."; exit 1; \
			fi; \
		fi'

compliance:
	@echo "── LICENSE ─────────────────────────────────────────────────────────"
	@test $$(wc -c < LICENSE) -ge 10000 \
		|| (echo "FAIL: LICENSE is only $$(wc -c < LICENSE) bytes — not the full Apache-2.0 text" && exit 1)
	@grep -qF "3. Grant of Patent License" LICENSE || (echo "FAIL: LICENSE missing section 3" && exit 1)
	@echo "  ok — $$(wc -c < LICENSE) bytes, all sections present"
	@echo "── Required files ──────────────────────────────────────────────────"
	@for f in NOTICE THIRD-PARTY-NOTICES.md mcp-server/THIRD-PARTY-NOTICES.md MODEL-LICENSES.md; do \
		test -f $$f && echo "  ok — $$f" || (echo "  FAIL — missing $$f" && exit 1); \
	done
	@echo "── Pinned dependencies ─────────────────────────────────────────────"
	@# Matches any requirement line (non-comment, non-indented — uv puts the
	@# "# via" provenance on indented lines) carrying a range operator rather
	@# than ==. Deliberately not written as a package-name character class:
	@# extras like `uvicorn[standard]>=0.30.0` are exactly what requirements.in
	@# contains and are the most likely thing to be pasted in here by mistake,
	@# and bracket-inside-bracket-expression is easy to get subtly wrong.
	@for r in requirements.txt mcp-server/requirements.txt; do \
		! grep -qE '^[^#[:space:]].*(>=|<=|~=|!=|<|>)' $$r \
			|| (echo "  FAIL — unpinned requirement in $$r; run 'make deps-lock'" && \
			    grep -nE '^[^#[:space:]].*(>=|<=|~=|!=|<|>)' $$r && exit 1) || exit 1; \
		grep -qE '^[^#[:space:]].*==' $$r \
			|| (echo "  FAIL — $$r has no pinned entries at all" && exit 1) || exit 1; \
		echo "  ok — $$r pinned ($$(grep -cE '^[^#[:space:]]+==' $$r) packages)"; \
	done
	@echo "── Third-party inventory ───────────────────────────────────────────"
	@python3 scripts/generate_third_party_notices.py --check
	@echo ""
	@echo "Compliance checks passed."
	@echo "Not covered here (require credentials or docker): 'make sbom', 'make sbom-mcp', 'make check-models'."

benchmark:
	@echo "Starting a benchmark (edit the curl below with your model and node IP):"
	curl -s -X POST http://localhost:8002/api/v1/benchmarks/start \
	  -H "Content-Type: application/json" \
	  -d '{"model_name":"llama3-8b-instruct","node_ips":["localhost"],"config":{"concurrency":4,"input_tokens":512,"output_tokens":512}}' | python -m json.tool
