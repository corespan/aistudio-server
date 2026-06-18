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

    # Workload Registry (GCP Artifact Registry)
    GCP_REGISTRY_URL: str = "us-docker.pkg.dev"
    GCP_PROJECT_ID: str = "aimlworkbench"
    GCP_REPOSITORY: str = "workbench-registry"
    GCP_IMAGE_PATH: str = "services/workloads"
    WORKLOAD_IMAGE_TAG: str = "2.3.0-nvidia"

    # Model Storage
    MODEL_STORAGE_MODE: str = "huggingface"
    MODEL_LOCAL_PATH: str = "/home/ubuntu/models"
    MODEL_GCS_BUCKET: str = ""

    # SSH
    SSH_KEY_PATH: str = "~/.ssh/id_rsa"
    SSH_DEFAULT_USER: str = "ubuntu"

    # Server
    PORT: int = 8001

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
    return "%s/%s/%s/%s" % (
        s.GCP_REGISTRY_URL, s.GCP_PROJECT_ID,
        s.GCP_REPOSITORY, s.GCP_IMAGE_PATH)
