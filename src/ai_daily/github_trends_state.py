from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
    field_validator,
)


CURRENT_SCHEMA_VERSION = 1


def require_aware_timestamp(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return timestamp


class GitHubRepositorySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository_id: int = Field(gt=0)
    full_name: str = Field(min_length=3, max_length=200)
    url: HttpUrl
    description: str = Field(min_length=1, max_length=500)
    created_at: datetime
    stars: int = Field(ge=0)
    language: str | None = Field(default=None, max_length=100)
    pushed_at: datetime
    topics: tuple[str, ...] = ()
    readme_url: HttpUrl
    readme_size: int = Field(ge=0)

    _created_at_is_aware = field_validator("created_at")(
        require_aware_timestamp
    )
    _pushed_at_is_aware = field_validator("pushed_at")(
        require_aware_timestamp
    )


class GitHubCandidateSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sampled_at: datetime
    repositories: dict[str, GitHubRepositorySnapshot] = Field(
        default_factory=dict
    )

    _sampled_at_is_aware = field_validator("sampled_at")(
        require_aware_timestamp
    )


class GitHubTrendsState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = CURRENT_SCHEMA_VERSION
    baseline: GitHubCandidateSnapshot | None = None
    latest: GitHubCandidateSnapshot | None = None

    @property
    def baseline_at(self) -> datetime | None:
        return None if self.baseline is None else self.baseline.sampled_at

    @property
    def baseline_repositories(self) -> dict[str, GitHubRepositorySnapshot]:
        return {} if self.baseline is None else self.baseline.repositories

    @property
    def latest_at(self) -> datetime | None:
        return None if self.latest is None else self.latest.sampled_at

    @property
    def latest_repositories(self) -> dict[str, GitHubRepositorySnapshot]:
        return {} if self.latest is None else self.latest.repositories

    @property
    def has_baseline(self) -> bool:
        return self.baseline is not None and bool(self.baseline.repositories)

    @classmethod
    def load(cls, path: str | Path) -> "GitHubTrendsState":
        state_path = Path(path)
        if not state_path.exists():
            return cls()
        try:
            return cls.model_validate_json(state_path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError):
            return cls()

    def save(self, path: str | Path) -> None:
        state_path = Path(path)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = state_path.with_suffix(state_path.suffix + ".tmp")
        payload = self.model_dump(mode="json")
        try:
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_path, state_path)
        finally:
            temporary_path.unlink(missing_ok=True)


class GitHubTrendsStateFileStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> GitHubTrendsState:
        return GitHubTrendsState.load(self._path)

    def save(self, state: GitHubTrendsState) -> None:
        state.save(self._path)
