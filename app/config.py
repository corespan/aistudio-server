from pydantic_settings import BaseSettings
from functools import lru_cache


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
    WORKLOAD_IMAGE_TAG: str = "1.0.0"

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

def get_database_url():
    s = settings
    return "postgresql+asyncpg://%s:%s@%s:%d/%s" % (
        s.POSTGRES_USERNAME, s.POSTGRES_PASSWORD,
        s.POSTGRES_HOST, s.POSTGRES_PORT, s.POSTGRES_DATABASE)

def get_sync_database_url():
    s = settings
    return "postgresql://%s:%s@%s:%d/%s" % (
        s.POSTGRES_USERNAME, s.POSTGRES_PASSWORD,
        s.POSTGRES_HOST, s.POSTGRES_PORT, s.POSTGRES_DATABASE)

def get_celery_broker_url():
    s = settings
    return "amqp://%s:%s@%s:%d/" % (
        s.RABBITMQ_USERNAME, s.RABBITMQ_PASSWORD,
        s.RABBITMQ_URL, s.RABBITMQ_PORT)

def get_celery_result_backend():
    s = settings
    return "db+postgresql://%s:%s@%s:%d/%s" % (
        s.POSTGRES_USERNAME, s.POSTGRES_PASSWORD,
        s.POSTGRES_HOST, s.POSTGRES_PORT, s.POSTGRES_DATABASE)

def get_workload_registry():
    s = settings
    return "%s/%s/%s/%s" % (
        s.GCP_REGISTRY_URL, s.GCP_PROJECT_ID,
        s.GCP_REPOSITORY, s.GCP_IMAGE_PATH)
