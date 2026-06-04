from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """
    Application configuration for aistudio-server (open source).

    All values are read from environment variables. For local development,
    copy .env.example to .env — docker-compose loads it automatically.
    """

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USERNAME: str = "aistudio"
    POSTGRES_PASSWORD: str = "aistudio"
    POSTGRES_DATABASE: str = "aistudio"

    # ── RabbitMQ / Celery ─────────────────────────────────────────────────────
    RABBITMQ_URL: str = "localhost"
    RABBITMQ_PORT: int = 5672
    RABBITMQ_USERNAME: str = "aistudio"
    RABBITMQ_PASSWORD: str = "aistudio"

    # ── Workload Registry (replaces Nexus) ────────────────────────────────────
    # Public GCR registry where pre-built workload images are hosted.
    WORKLOAD_REGISTRY: str = "gcr.io/aistudio-oss"
    WORKLOAD_IMAGE_TAG: str = "latest"

    # ── Model Storage ─────────────────────────────────────────────────────────
    # How models are accessed on the GPU node:
    #   "local"       -> mount MODEL_LOCAL_PATH into the container
    #   "huggingface" -> use HF cache (~/.cache/huggingface)
    #   "gcs"         -> set GCS_BUCKET env in container
    MODEL_STORAGE_MODE: str = "huggingface"
    MODEL_LOCAL_PATH: str = "/home/ubuntu/models"
    MODEL_GCS_BUCKET: str = ""

    # ── SSH ────────────────────────────────────────────────────────────────────
    SSH_KEY_PATH: str = "~/.ssh/id_rsa"
    SSH_DEFAULT_USER: str = "ubuntu"

    # ── Server ────────────────────────────────────────────────────────────────
    PORT: int = 8001

    # ── Computed properties ───────────────────────────────────────────────────
    @property
    def database_url(self) -> str:
        return (
            "postgresql+asyncpg://%s:%s@%s:%d/%s"
            % (self.POSTGRES_USERNAME, self.POSTGRES_PASSWORD,
               self.POSTGRES_HOST, self.POSTGRES_PORT, self.POSTGRES_DATABASE)
        )

    @property
    def sync_database_url(self) -> str:
        return (
            "postgresql://%s:%s@%s:%d/%s"
            % (self.POSTGRES_USERNAME, self.POSTGRES_PASSWORD,
               self.POSTGRES_HOST, self.POSTGRES_PORT, self.POSTGRES_DATABASE)
        )

    @property
    def celery_broker_url(self) -> str:
        return (
            "amqp://%s:%s@%s:%d/"
            % (self.RABBITMQ_USERNAME, self.RABBITMQ_PASSWORD,
               self.RABBITMQ_URL, self.RABBITMQ_PORT)
        )

    @property
    def celery_result_backend(self) -> str:
        return (
            "db+postgresql://%s:%s@%s:%d/%s"
            % (self.POSTGRES_USERNAME, self.POSTGRES_PASSWORD,
               self.POSTGRES_HOST, self.POSTGRES_PORT, self.POSTGRES_DATABASE)
        )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
