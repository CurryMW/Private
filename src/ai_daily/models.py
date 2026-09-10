from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class Category(StrEnum):
    MODEL = "模型发布"
    RESEARCH = "研究突破"
    OPEN_SOURCE = "开源工具"
    PARADIGM = "AI 范式"


class VerificationStatus(StrEnum):
    CONFIRMED = "已确认更新"
    UNVERIFIED = "未经第一方确认"


class EvidenceSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str = Field(min_length=2, max_length=100)
    url: HttpUrl
    excerpt: str = Field(min_length=3, max_length=6000)


class Candidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=12, max_length=64)
    title: str = Field(min_length=3, max_length=300)
    summary: str = Field(default="", max_length=6000)
    source: str = Field(min_length=2, max_length=100)
    url: HttpUrl
    published_at: datetime
    source_kind: str = Field(min_length=2, max_length=30)
    relevance_score: float | None = Field(default=None, ge=0, le=1)
    authority_score: float | None = Field(default=None, ge=0, le=1)
    event_id: str | None = Field(default=None, min_length=12, max_length=64)
    organization_id: str | None = Field(default=None, min_length=1, max_length=253)
    verification_status: VerificationStatus | None = None
    evidence: tuple[EvidenceSource, ...] = ()

    @model_validator(mode="after")
    def validate_evidence_assessment(self) -> "Candidate":
        if self.verification_status is None:
            if (
                self.event_id is not None
                or self.organization_id is not None
                or self.evidence
            ):
                raise ValueError("ungraded candidate cannot contain evidence assessment")
            return self
        if self.event_id is None or not self.evidence:
            raise ValueError("graded candidate requires an event and evidence")
        if (
            self.verification_status is VerificationStatus.CONFIRMED
            and self.organization_id is None
        ):
            raise ValueError("confirmed candidate requires an organization")
        if (
            self.verification_status is VerificationStatus.UNVERIFIED
            and len(self.evidence) < 2
        ):
            raise ValueError("unverified candidate requires two evidence sources")
        return self


class SearchLead(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=12, max_length=64)
    title: str = Field(min_length=3, max_length=300)
    snippet: str = Field(default="", max_length=6000)
    source: str = Field(min_length=2, max_length=100)
    url: HttpUrl
    published_at: datetime
    relevance_score: float | None = Field(default=None, ge=0, le=1)
    authority_score: float | None = Field(default=None, ge=0, le=1)

    def as_candidate(self) -> Candidate:
        return Candidate(
            id=self.id,
            title=self.title,
            summary=self.snippet,
            source=self.source,
            url=self.url,
            published_at=self.published_at,
            source_kind="baidu_search",
            relevance_score=self.relevance_score,
            authority_score=self.authority_score,
        )


class DigestItem(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    category: Category
    source: str = Field(min_length=2, max_length=100)
    summary: str = Field(min_length=10, max_length=600)
    impact: str = Field(min_length=10, max_length=600)
    url: HttpUrl


class Digest(BaseModel):
    overview: str = Field(min_length=10, max_length=800)
    items: list[DigestItem] = Field(min_length=1, max_length=8)
    trends: list[str] = Field(min_length=2, max_length=3)
