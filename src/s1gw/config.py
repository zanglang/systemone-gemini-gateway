"""Environment configuration (BRIEF section 5)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


class Settings(BaseSettings):
    """Settings are read from the environment (case-insensitive field names)."""

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    gemini_api_key: str = Field(min_length=1)
    gemini_base_url: str = "https://generativelanguage.googleapis.com"
    gemini_model: str = "gemini-decision-1"
    gemini_image_resolution: Literal["low", "medium", "high", "ultra_high"] | None = None
    inbound_api_keys: str = ""
    upstream_timeout_seconds: float = Field(default=30, gt=0)
    upstream_max_retries: int = Field(default=2, ge=0)
    max_body_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    advertised_models: str = "jev-latest,gemini-decision-1"
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
    otel_service_name: str = "systemone-gemini-gateway"
    otel_metrics_exporter: str = "prometheus"
    otel_exporter_otlp_endpoint: str | None = None
    otel_exporter_otlp_protocol: str = "http/protobuf"
    metrics_port: int = 9464
    port: int = 8080
    pod_name: str | None = None
    pod_namespace: str | None = None

    @field_validator(
        "gemini_image_resolution",
        "otel_exporter_otlp_endpoint",
        "pod_name",
        "pod_namespace",
        mode="before",
    )
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("otel_metrics_exporter")
    @classmethod
    def _check_exporters(cls, value: str) -> str:
        allowed = {"otlp", "prometheus", "none"}
        bad = [name for name in _csv(value) if name not in allowed]
        if bad:
            raise ValueError(f"unsupported exporter(s) {bad}; use otlp, prometheus or none")
        return value

    @property
    def inbound_keys(self) -> list[str]:
        return _csv(self.inbound_api_keys)

    @property
    def models(self) -> list[str]:
        return _csv(self.advertised_models)

    @property
    def metrics_exporters(self) -> set[str]:
        return set(_csv(self.otel_metrics_exporter)) - {"none"}

    @property
    def secrets(self) -> list[str]:
        return [self.gemini_api_key, *self.inbound_keys]
