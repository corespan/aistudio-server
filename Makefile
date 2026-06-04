.PHONY: up down logs migrate seed test spec shell benchmark

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

setup: up
	@echo "Waiting for services to start..."
	@sleep 5
	$(MAKE) migrate
	$(MAKE) seed
	@echo ""
	@echo "AIStudio Server is ready!"
	@echo "  API:         http://localhost:8001"
	@echo "  Swagger:     http://localhost:8001/docs"
	@echo "  RabbitMQ:    http://localhost:15672"

test:
	docker compose exec api pytest tests/ -v

spec:
	docker compose exec api python -c "from app.main import app; import json; print(json.dumps(app.openapi(), indent=2))" > api_contract/openapi.json
	@echo "OpenAPI spec exported to api_contract/openapi.json"

shell:
	docker compose exec api python

benchmark:
	@echo "Starting a benchmark (edit the curl below with your model and node IP):"
	curl -s -X POST http://localhost:8001/api/v1/benchmarks/start \
	  -H "Content-Type: application/json" \
	  -d '{"model_name":"llama3-8b-instruct","node_ips":["localhost"],"config":{"concurrency":4,"input_tokens":512,"output_tokens":512}}' | python -m json.tool
