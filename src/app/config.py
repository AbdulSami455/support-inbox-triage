import os
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Settings:
    session_secret: str
    webhook_secret: str
    demo_password: str
    cookie_secure: bool
    # AI (OpenRouter, OpenAI-compatible API)
    openrouter_api_key: str
    openrouter_model: str
    openrouter_base_url: str
    db_path: str  # local SQLite file (on Cloudflare the D1 binding is used instead)
    pbkdf2_iterations: int = 200_000  # lowered on Workers' free plan CPU budget

    def validate(self) -> None:
        if len(self.session_secret) < 16:
            raise RuntimeError("SESSION_SECRET must be at least 16 characters")
        if not self.webhook_secret:
            raise RuntimeError("WEBHOOK_SECRET is required")
        if not self.demo_password:
            raise RuntimeError("DEMO_PASSWORD is required")


def load_settings(get: Callable[[str], str | None] = os.environ.get) -> Settings:
    """Build settings from any key lookup: os.environ locally, the Worker env on Cloudflare."""

    def _env(name: str, default: str = "") -> str:
        return str(get(name) or default).strip()

    return Settings(
        session_secret=_env("SESSION_SECRET"),
        webhook_secret=_env("WEBHOOK_SECRET"),
        demo_password=_env("DEMO_PASSWORD"),
        cookie_secure=_env("COOKIE_SECURE", "false").lower() == "true",
        openrouter_api_key=_env("OPENROUTER_API_KEY"),
        openrouter_model=_env("OPENROUTER_MODEL", "nvidia/nemotron-3.5-super-vl:free"),
        openrouter_base_url=_env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        db_path=_env("DB_PATH", "data/inbox.db"),
        pbkdf2_iterations=int(_env("PBKDF2_ITERATIONS", "200000")),
    )
