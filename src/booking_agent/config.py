"""Settings from environment variables (prefix BA_) or a local .env file, plus the composition root."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from booking_agent.catalog import DEFAULT_CATALOG, ServiceCatalog
from booking_agent.clock import Clock, SystemClock
from booking_agent.limits import Limits
from booking_agent.model import ChatModel
from booking_agent.ports import Repository
from booking_agent.prompts import load_prompts
from booking_agent.provider.client import ProviderAPI
from booking_agent.provider.scenario import Scenario, default_history

if TYPE_CHECKING:
    from booking_agent.agent import Agent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BA_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    database_url: str | None = None  # e.g. mssql+pyodbc://... for Azure SQL; default SQLite in data_dir
    trace_dir: Path = Path("traces")  # one JSONL file per trace; read by the admin page
    seed: int = 7
    demo_user: str = "u1"
    demo_mode: bool = True  # Disable in production; supply a provider and identity resolver.

    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openrouter_api_key: SecretStr | None = Field(default=None, validation_alias="OPENROUTER_API_KEY")
    llm_model: str = "gpt-6-luna"  # any chat model on the API at llm_base_url
    memory_model: str | None = None  # BA_MEMORY_MODEL; model for the memory_update job, default llm_model
    prompts_file: Path | None = None  # defaults to the packaged prompts.toml
    llm_price_in_per_m: float = 0.10  # USD per 1M input tokens, gpt-6-luna list price (Nov 2026)
    llm_price_out_per_m: float = 0.50  # USD per 1M output tokens
    llm_reasoning_effort: str | None = "none"  # gpt-6-luna allows tool calls on chat completions only with "none"

    access_code: SecretStr | None = None
    admin_code: SecretStr | None = None  # enables /admin; separate from the user access code
    watcher_interval_s: float = 30.0
    log_level: str = "INFO"

    def api_key(self) -> str | None:
        if self.llm_api_key is not None:
            return self.llm_api_key.get_secret_value()
        key = self.openrouter_api_key if "openrouter" in self.llm_base_url else self.openai_api_key
        return key.get_secret_value() if key is not None else None


def build_model(settings: Settings, model_name: str | None = None) -> ChatModel:
    from booking_agent.adapters.openai import OpenAIChatModel

    key = settings.api_key()
    if key is None:
        raise RuntimeError("Set OPENAI_API_KEY (or OPENROUTER_API_KEY with BA_LLM_BASE_URL) to run the agent.")
    return OpenAIChatModel(
        api_key=key,
        model=model_name or settings.llm_model,
        base_url=settings.llm_base_url,
        reasoning_effort=settings.llm_reasoning_effort,
        price_in_per_m=settings.llm_price_in_per_m,
        price_out_per_m=settings.llm_price_out_per_m,
    )


def choose_memory_model(settings: Settings, main_model: ChatModel) -> ChatModel:
    """The main model, unless BA_MEMORY_MODEL names a different one (then a second client is built)."""
    if settings.memory_model is None or settings.memory_model == settings.llm_model:
        return main_model
    return build_model(settings, settings.memory_model)


def build(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    scenario: Scenario | None = None,
    model: ChatModel | None = None,
    memory_model: ChatModel | None = None,
    limits: Limits | None = None,
    provider: ProviderAPI | None = None,
    repository: Repository | None = None,
    catalog: ServiceCatalog = DEFAULT_CATALOG,
) -> tuple[Agent, ProviderAPI]:
    from booking_agent.agent import Agent

    settings = settings or Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    clock = clock or SystemClock()
    scenario = scenario or Scenario(seed=settings.seed, seed_bookings=default_history(settings.demo_user))
    if provider is None:
        if not settings.demo_mode:
            raise RuntimeError("Supply a booking provider when demo_mode is disabled")
        from booking_agent.provider.mock import MockProvider

        provider = MockProvider(settings.database_url or (settings.data_dir / "provider.sqlite3"), clock, scenario)
    if repository is None:
        from booking_agent.store import Store

        repository = Store(settings.database_url or (settings.data_dir / "agent.sqlite3"))
    main_model = model or build_model(settings)
    agent = Agent(
        store=repository,
        provider=provider,
        model=main_model,
        memory_model=memory_model or choose_memory_model(settings, main_model),
        prompts=load_prompts(settings.prompts_file),
        clock=clock,
        limits=limits,
        trace_dir=settings.trace_dir,
        catalog=catalog,
    )
    return agent, provider
