.PHONY: up down logs migrate seed seed-real seed-h100 seed-mi210 seed-t4 seed-benchmarks wait-api test spec shell benchmark \
        compliance deps-lock third-party sbom check-models check-node-env \
        mcp-init mcp-up mcp-token mcp-check mcp-test sbom-mcp

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
# mint.py runs in the aistudio-mcp image, so the host needs no Python packages.
# As the host user: the service drops every capability, so root in it cannot write gateway/, and
# the key should belong to whoever runs make anyway.
MCP_MINT = docker compose --profile mcp run --rm --no-deps --user "$(shell id -u):$(shell id -g)" \
	-v "$(CURDIR)/gateway:/gateway-rw" aistudio-mcp python /gateway-rw/mint.py

mcp-init:
	docker compose --profile mcp build aistudio-mcp
	$(MCP_MINT) init

mcp-up:
	@test -f gateway/jwks.json || $(MAKE) mcp-init
	docker compose --profile mcp up --build -d

mcp-token:
	@$(MCP_MINT) $(or $(ROLE),viewer) $(or $(SUB),local-$(or $(ROLE),viewer)) --ttl $(or $(TTL),3600)

mcp-check:
	MINT='$(MCP_MINT)' bash gateway/check.sh

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
	@# Needs only docker: builds aistudio-mcp, pulls agentgateway + docker-socket-proxy.
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
