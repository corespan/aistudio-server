.PHONY: up down logs migrate seed seed-real seed-h100 seed-mi210 seed-t4 seed-benchmarks wait-api test spec shell benchmark

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
	@echo "  Demo UI:     http://localhost:3000"

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

benchmark:
	@echo "Starting a benchmark (edit the curl below with your model and node IP):"
	curl -s -X POST http://localhost:8002/api/v1/benchmarks/start \
	  -H "Content-Type: application/json" \
	  -d '{"model_name":"llama3-8b-instruct","node_ips":["localhost"],"config":{"concurrency":4,"input_tokens":512,"output_tokens":512}}' | python -m json.tool
