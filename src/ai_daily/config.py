import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, urlsplit

import yaml
from dotenv import load_dotenv
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)


class RssSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    url: HttpUrl


class ArxivConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categories: list[str] = Field(min_length=1)
    max_results: int = Field(gt=0)


class HuggingFaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    limit_per_day: int = Field(gt=0)


Repository = Annotated[str, Field(pattern=r"^[^/\s]+/[^/\s]+$")]


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rss: list[RssSource] = Field(default_factory=list)
    arxiv: ArxivConfig | None = None
    huggingface_daily_papers: HuggingFaceConfig | None = None
    github_repositories: list[Repository] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_configured_source(self) -> "SourceConfig":
        huggingface_enabled = (
            self.huggingface_daily_papers is not None
            and self.huggingface_daily_papers.enabled
        )
        if not (
            self.rss
            or self.arxiv is not None
            or huggingface_enabled
            or self.github_repositories
        ):
            raise ValueError("at least one source must be configured")
        return self


def load_source_config(path: Path) -> SourceConfig:
    with path.open(encoding="utf-8") as source_file:
        payload = yaml.safe_load(source_file)
    return SourceConfig.model_validate(payload)


class Settings(BaseModel):
    ai_api_key: SecretStr
    ai_base_url: str = "https://apiclaude.cc/v1"
    ai_model: str = "claude-sonnet-4-6"
    dingtalk_webhook: SecretStr
    dingtalk_access_token: SecretStr | None = None
    window_hours: int = Field(default=36, gt=0)
    fallback_window_hours: int = Field(default=168, gt=0)
    max_items: int = Field(default=8, gt=0, le=8)
    timezone: str = "Asia/Shanghai"
    dry_run: bool = False
    state_path: Path = Path(".state/sent.json")
    delivery_state_path: Path = Path(".state/deliveries.json")
    enforce_daily_once: bool = False
    github_token: SecretStr | None = None

    @field_validator("ai_api_key", "dingtalk_webhook")
    @classmethod
    def reject_empty_secrets(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("credential must not be empty")
        return value

    @model_validator(mode="after")
    def validate_dingtalk_webhook(self) -> "Settings":
        webhook = self.dingtalk_webhook.get_secret_value()
        parsed = urlsplit(webhook)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise ValueError("DINGTALK_WEBHOOK must be an HTTPS URL")

        query = parse_qs(parsed.query)
        token = self.dingtalk_access_token
        has_separate_token = token is not None and bool(token.get_secret_value().strip())
        if "access_token" not in query and not has_separate_token:
            raise ValueError("DINGTALK_ACCESS_TOKEN is required for a base webhook")
        return self

    @model_validator(mode="after")
    def validate_content_windows(self) -> "Settings":
        if self.fallback_window_hours < self.window_hours:
            raise ValueError("fallback window must cover primary window")
        return self


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _optional(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None or not value.strip():
        return None
    return value


def _parse_bool(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    if env is None:
        load_dotenv()
        source: Mapping[str, str] = os.environ
    else:
        source = env

    return Settings(
        ai_api_key=_required(source, "AI_API_KEY"),
        ai_base_url=source.get("AI_BASE_URL", "https://apiclaude.cc/v1"),
        ai_model=source.get("AI_MODEL", "claude-sonnet-4-6"),
        dingtalk_webhook=_required(source, "DINGTALK_WEBHOOK"),
        dingtalk_access_token=_optional(source, "DINGTALK_ACCESS_TOKEN"),
        window_hours=source.get("WINDOW_HOURS", "36"),
        fallback_window_hours=source.get("FALLBACK_WINDOW_HOURS", "168"),
        max_items=source.get("MAX_ITEMS", "8"),
        timezone=source.get("TIMEZONE", "Asia/Shanghai"),
        dry_run=_parse_bool(source.get("DRY_RUN")),
        state_path=source.get("STATE_PATH", ".state/sent.json"),
        delivery_state_path=source.get(
            "DELIVERY_STATE_PATH", ".state/deliveries.json"
        ),
        enforce_daily_once=_parse_bool(source.get("ENFORCE_DAILY_ONCE")),
        github_token=_optional(source, "GITHUB_TOKEN"),
    )
