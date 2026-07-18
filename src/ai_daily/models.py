from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class Category(StrEnum):
    MODEL = "模型发布"
    RESEARCH = "研究突破"
    OPEN_SOURCE = "开源工具"
    PARADIGM = "AI 范式"


class Candidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=12, max_length=64)
    title: str = Field(min_length=3, max_length=300)
    summary: str = Field(default="", max_length=6000)
    source: str = Field(min_length=2, max_length=100)
    url: HttpUrl
    published_at: datetime
    source_kind: str = Field(min_length=2, max_length=30)


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
