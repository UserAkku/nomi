"""Runtime configuration, read from NOMI_* environment variables or a .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# The one model Nomi is built and evaluated with. It is a 2.5 GB Q4_K_M
# build that leaves headroom on an 8 GB laptop; see HACKATHON.md for why.
DEFAULT_MODEL = "qwen3:4b-instruct-2507-q4_K_M"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NOMI_", env_file=".env", extra="ignore")

    # --- local language model (Ollama) ---
    llm_enabled: bool = True
    llm_model: str = DEFAULT_MODEL
    ollama_host: str = Field(
        default="http://localhost:11434",
        validation_alias=AliasChoices("NOMI_OLLAMA_HOST", "OLLAMA_HOST"),
    )
    llm_num_ctx: int = 8192
    llm_timeout_s: float = 240.0
    llm_max_claims: int = 6

    # --- storage ---
    cache_dir: Path = Path.home() / ".cache" / "nomi"

    # --- GitHub API ---
    github_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("NOMI_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"),
    )

    # --- sandbox for dynamic reproduction ---
    sandbox_enabled: bool = True  # still requires a reachable Docker daemon
    sandbox_image: str = "python:3.12-slim"
    sandbox_timeout_s: int = 60
    sandbox_memory: str = "512m"
    sandbox_cpus: str = "1.0"

    # --- GitHub App server ---
    app_id: str | None = None
    app_private_key_path: Path | None = None
    webhook_secret: SecretStr | None = None
    dry_run: bool = True  # never post to GitHub unless explicitly disabled
    output_dir: Path = Path("nomi-reports")

    @property
    def token(self) -> str | None:
        return self.github_token.get_secret_value() if self.github_token else None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
