"""Configuration loader — reads .env and exposes typed settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_ENV_FILE = Path(__file__).parent / ".env"
load_dotenv(_ENV_FILE)


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


@dataclass(frozen=True)
class TargetConfig:
    """Upstream (target) API configuration."""

    api_key: str = field(default_factory=lambda: _env("TARGET_API_KEY"))
    api_base: str = field(default_factory=lambda: _env("TARGET_API_BASE", "https://api.openai.com/v1"))
    model: str = field(default_factory=lambda: _env("TARGET_MODEL", "gpt-4o"))
    max_retries: int = field(default_factory=lambda: int(_env("TARGET_MAX_RETRIES", "2")))
    timeout: float = field(default_factory=lambda: float(_env("TARGET_TIMEOUT", "300")))

    @property
    def chat_url(self) -> str:
        return f"{self.api_base.rstrip('/')}/chat/completions"

    @property
    def models_url(self) -> str:
        return f"{self.api_base.rstrip('/')}/models"

    def validate(self) -> None:
        if not self.api_key:
            raise ValueError("TARGET_API_KEY is required")


@dataclass(frozen=True)
class ProxyConfig:
    """Local proxy server configuration."""

    host: str = field(default_factory=lambda: _env("PROXY_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_env("PROXY_PORT", "8080")))
    # If set, the proxy rejects requests whose x-api-key / Bearer token doesn't match.
    auth_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "info"))


def load() -> tuple[TargetConfig, ProxyConfig]:
    target = TargetConfig()
    proxy = ProxyConfig()
    target.validate()
    return target, proxy
