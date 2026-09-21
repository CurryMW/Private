import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import yaml
from dotenv import load_dotenv
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


PRODUCTION_MODEL = "gpt-5.6-luna"
DEFAULT_AI_BASE_URL = "https://api.teamorouter.cn/v1"


def validate_ai_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError("AI_BASE_URL must be an HTTPS URL")
    return base_url


class BaiduSearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixed_queries: list[str] = Field(default_factory=list)
    rotating_queries: list[str] = Field(default_factory=list)
    first_party_domains: list[str] = Field(default_factory=list)
    trusted_domains: list[str] = Field(default_factory=list)

    @field_validator("fixed_queries", "rotating_queries")
    @classmethod
    def normalize_queries(cls, values: list[str]) -> list[str]:
        normalized = [" ".join(value.split()) for value in values]
        if any(not value or len(value) > 72 for value in normalized):
            raise ValueError("search queries must contain 1 to 72 characters")
        if len(set(normalized)) != len(normalized):
            raise ValueError("search queries must be unique within each group")
        return normalized

    @field_validator("first_party_domains", "trusted_domains")
    @classmethod
    def normalize_domains(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().casefold().rstrip(".") for value in values]
        if any(
            not value
            or "/" in value
            or ":" in value
            or value.startswith(".")
            for value in normalized
        ):
            raise ValueError("evidence domains must be host names")
        return normalized

    @model_validator(mode="after")
    def validate_query_plan(self) -> "BaiduSearchConfig":
        if len(self.fixed_queries) != 16:
            raise ValueError("a complete search plan requires 16 fixed queries")
        if len(self.rotating_queries) < 4:
            raise ValueError("a complete search plan requires at least 4 rotating queries")
        return self

    def queries_for(self, ordinal: int) -> list[str]:
        start = (ordinal * 4) % len(self.rotating_queries)
        rotating = [
            self.rotating_queries[(start + offset) % len(self.rotating_queries)]
            for offset in range(4)
        ]
        return [*self.fixed_queries, *rotating]


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baidu_search: BaiduSearchConfig


def load_source_config(path: Path) -> SourceConfig:
    with path.open(encoding="utf-8") as source_file:
        payload = yaml.safe_load(source_file)
    return SourceConfig.model_validate(payload)


class Settings(BaseModel):
    ai_api_key: SecretStr
    ai_model: str
    ai_base_url: str = DEFAULT_AI_BASE_URL
    baidu_search_api_key: SecretStr | None = None
    dingtalk_webhook: SecretStr
    dingtalk_access_token: SecretStr | None = None
    window_hours: int = Field(default=168, gt=0)
    event_dedupe_days: int = Field(default=3, gt=0)
    max_items: int = Field(default=8, gt=0, le=8)
    timezone: str = "Asia/Shanghai"
    dry_run: bool = False
    state_path: Path = Path(".state/sent.json")
    delivery_state_path: Path = Path(".state/deliveries.json")
    baidu_usage_state_path: Path = Path(".state/baidu-search-usage.json")
    enforce_daily_once: bool = False

    @field_validator("ai_api_key", "dingtalk_webhook")
    @classmethod
    def reject_empty_secrets(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("credential must not be empty")
        return value

    @field_validator("ai_model")
    @classmethod
    def validate_single_model(cls, value: str) -> str:
        model = value.strip()
        if not model:
            raise ValueError("AI_MODEL must not be empty")
        if model != PRODUCTION_MODEL:
            raise ValueError(f"AI_MODEL must be {PRODUCTION_MODEL}")
        return model

    @field_validator("ai_base_url")
    @classmethod
    def validate_model_base_url(cls, value: str) -> str:
        return validate_ai_base_url(value)

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
        load_dotenv(dotenv_path=Path.cwd() / ".env")
        source: Mapping[str, str] = os.environ
    else:
        source = env

    return Settings(
        ai_api_key=_required(source, "AI_API_KEY"),
        ai_model=_required(source, "AI_MODEL"),
        ai_base_url=source.get("AI_BASE_URL", DEFAULT_AI_BASE_URL),
        baidu_search_api_key=_optional(source, "BAIDU_SEARCH_API_KEY"),
        dingtalk_webhook=_required(source, "DINGTALK_WEBHOOK"),
        dingtalk_access_token=_optional(source, "DINGTALK_ACCESS_TOKEN"),
        window_hours=source.get("WINDOW_HOURS", "168"),
        event_dedupe_days=source.get("EVENT_DEDUPE_DAYS", "3"),
        max_items=source.get("MAX_ITEMS", "8"),
        timezone=source.get("TIMEZONE", "Asia/Shanghai"),
        dry_run=_parse_bool(source.get("DRY_RUN")),
        state_path=source.get("STATE_PATH", ".state/sent.json"),
        delivery_state_path=source.get(
            "DELIVERY_STATE_PATH", ".state/deliveries.json"
        ),
        baidu_usage_state_path=source.get(
            "BAIDU_USAGE_STATE_PATH", ".state/baidu-search-usage.json"
        ),
        enforce_daily_once=_parse_bool(source.get("ENFORCE_DAILY_ONCE")),
    )
