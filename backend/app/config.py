from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime configuration. Secrets are intentionally absent from the mocked slice."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    data_dir: Path = Field(
        default=REPOSITORY_ROOT / "data",
        validation_alias=AliasChoices("PET_FUSION_DATA_DIR", "DATA_DIR"),
    )
    app_db_path: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("PET_FUSION_APP_DB_PATH", "APP_DB_PATH"),
    )
    checkpoint_db_path: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("PET_FUSION_CHECKPOINT_DB_PATH", "CHECKPOINT_DB_PATH"),
    )
    run_inline: bool = Field(
        default=False,
        validation_alias=AliasChoices("RUN_INLINE", "PET_FUSION_RUN_INLINE"),
    )
    fake_generator: bool = Field(
        default=True,
        validation_alias=AliasChoices("FAKE_GENERATOR", "PET_FUSION_FAKE_GENERATOR"),
    )
    cors_origins: str = Field(
        default="http://localhost:5173",
        validation_alias=AliasChoices("CORS_ORIGINS", "PET_FUSION_CORS_ORIGINS"),
    )
    max_upload_bytes: int = Field(
        default=25 * 1024 * 1024,
        ge=1,
        validation_alias=AliasChoices("MAX_UPLOAD_BYTES", "PET_FUSION_MAX_UPLOAD_BYTES"),
    )
    max_total_upload_bytes: int = Field(
        default=75 * 1024 * 1024,
        ge=1,
        validation_alias=AliasChoices(
            "MAX_TOTAL_UPLOAD_BYTES", "PET_FUSION_MAX_TOTAL_UPLOAD_BYTES"
        ),
    )
    max_image_pixels: int = Field(
        default=40_000_000,
        ge=1,
        validation_alias=AliasChoices("MAX_IMAGE_PIXELS", "PET_FUSION_MAX_IMAGE_PIXELS"),
    )
    max_total_image_pixels: int = Field(
        default=80_000_000,
        ge=1,
        validation_alias=AliasChoices(
            "MAX_TOTAL_IMAGE_PIXELS", "PET_FUSION_MAX_TOTAL_IMAGE_PIXELS"
        ),
    )
    worker_poll_seconds: float = Field(
        default=0.5,
        gt=0,
        validation_alias=AliasChoices("WORKER_POLL_SECONDS", "PET_FUSION_WORKER_POLL_SECONDS"),
    )
    worker_lease_seconds: int = Field(
        default=60,
        ge=5,
        validation_alias=AliasChoices("WORKER_LEASE_SECONDS", "PET_FUSION_WORKER_LEASE_SECONDS"),
    )

    @property
    def resolved_app_db_path(self) -> Path:
        return (self.app_db_path or self.data_dir / "app.sqlite3").resolve()

    @property
    def resolved_checkpoint_db_path(self) -> Path:
        return (
            self.checkpoint_db_path or self.data_dir / "langgraph-checkpoints.sqlite3"
        ).resolve()

    @property
    def asset_dir(self) -> Path:
        return (self.data_dir / "assets").resolve()

    @property
    def parsed_cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
