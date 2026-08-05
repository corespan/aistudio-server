from pydantic_settings import BaseSettings
from functools import lru_cache
from urllib.parse import quote


class Settings(BaseSettings):
    """
    Application configuration for aistudio-server.

    All values are read from environment variables. For local development,
    copy .env.example to .env -- docker-compose loads it automatically.
    """

    # PostgreSQL
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USERNAME: str = "aistudio"
    POSTGRES_PASSWORD: str = "aistudio"
    POSTGRES_DATABASE: str = "aistudio"

    # RabbitMQ / Celery
    RABBITMQ_URL: str = "localhost"
    RABBITMQ_PORT: int = 5672
    RABBITMQ_USERNAME: str = "aistudio"
    RABBITMQ_PASSWORD: str = "aistudio"

    # Workload Registry (GCP Artifact Registry — public read)
    GCP_REGISTRY_URL: str = "us-docker.pkg.dev"
    GCP_PROJECT_ID: str = "aimlworkbench"
    GCP_REPOSITORY: str = "aistudio"
    WORKLOAD_IMAGE_TAG: str = "2.3.0-nvidia"
    JUPYTER_IMAGE_TAG: str = "2.2.0-nvidia"

    # Model Storage
    MODEL_STORAGE_MODE: str = "huggingface"
    MODEL_LOCAL_PATH: str = "/home/ubuntu/models"
    MODEL_GCS_BUCKET: str = ""

    # GPU node paths
    # NODE_RESULTS_PATH — where benchmark output is written on the GPU node.
    #   Bind-mounted into the workload container as /results.
    #   Each run creates /results/<run_id>/ with benchmark_result.json, summary.json, logs/.
    NODE_RESULTS_PATH: str = "/results"

    # NODE_JUPYTER_DATA_PATH — where Jupyter notebooks are stored on the GPU node.
    #   Bind-mounted into the Jupyter container as /data.
    #   Each session creates /data/<workload_id>/ containing the user's notebooks.
    #   Use any writable directory on the GPU node; it does not need to be shared storage.
    #   Example: /home/ubuntu/aistudio-jupyter  or  /tmp/jupyter-data
    NODE_JUPYTER_DATA_PATH: str = "/data"

    # SSH
    SSH_KEY_PATH: str = "~/.ssh/id_rsa"
    SSH_DEFAULT_USER: str = "ubuntu"

    # Server
    PORT: int = 8001

    # Nginx reverse proxy (hides internal GPU node IPs from clients)
    # When enabled, each Jupyter instance gets a path-based route:
    #   {PROXY_BASE_URL}/jupyter/{GPU_TYPE}/{task_id}/  →  http://node-ip:port
    # No DNS setup required — uses the existing public domain.
    NGINX_ENABLED: bool = False
    # Public base URL used to construct the jupyter_url returned by the API.
    # e.g. http://your-domain.com  (no trailing slash)
    PROXY_BASE_URL: str = ""
    # Directory where per-instance nginx location blocks are written.
    # This directory is included by /etc/nginx/conf.d/aistudio-jupyter.conf
    NGINX_CONF_DIR: str = "/etc/nginx/jupyter-locations"
    # Command used to reload nginx after writing/removing a config file.
    # When running inside docker: "ssh -i /root/.ssh/id_rsa -o StrictHostKeyChecking=no user@host sudo nginx -s reload"
    NGINX_RELOAD_CMD: str = "nginx -s reload"

    # Persistent Jupyter Assistant URL
    # Pre-configured Jupyter Lab instance that is always running.
    # The UI renders this directly — no container is created at runtime.
    # Override via environment variable JUPYTER_ASSISTANT_URL.
    JUPYTER_ASSISTANT_URL: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache
def get_settings():
    return Settings()


settings = get_settings()


# Computed URL helpers (plain functions, compatible with Pydantic v2)

def _pg_authority() -> str:
    s = settings
    user = quote(s.POSTGRES_USERNAME, safe="")
    pwd  = quote(s.POSTGRES_PASSWORD, safe="")
    return "%s:%s@%s:%d/%s" % (user, pwd, s.POSTGRES_HOST, s.POSTGRES_PORT, s.POSTGRES_DATABASE)

def get_database_url() -> str:
    return "postgresql+asyncpg://" + _pg_authority()

def get_sync_database_url() -> str:
    return "postgresql://" + _pg_authority()

def get_celery_broker_url() -> str:
    s = settings
    user = quote(s.RABBITMQ_USERNAME, safe="")
    pwd  = quote(s.RABBITMQ_PASSWORD, safe="")
    return "amqp://%s:%s@%s:%d/" % (user, pwd, s.RABBITMQ_URL, s.RABBITMQ_PORT)

def get_celery_result_backend() -> str:
    return "db+postgresql://" + _pg_authority()

def get_workload_registry() -> str:
    s = settings
    return "%s/%s/%s" % (s.GCP_REGISTRY_URL, s.GCP_PROJECT_ID, s.GCP_REPOSITORY)
