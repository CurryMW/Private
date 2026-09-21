import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from urllib.parse import urlsplit

from ai_daily.config import BaiduSearchConfig
from ai_daily.filtering import canonicalize_url
from ai_daily.models import (
    Candidate,
    EvidenceSource,
    SearchLead,
    VerificationStatus,
)
from ai_daily.state import SentState


SITE_CANDIDATE_LIMIT = 3
MODEL_CANDIDATE_LIMIT = 40
_AI_WORDS = {
    "ai",
    "agent",
    "inference",
    "llm",
    "model",
    "training",
}
_AI_PHRASES = {
    "artificial intelligence",
    "machine learning",
    "人工智能",
    "大模型",
    "智能体",
    "机器学习",
    "模型",
    "推理",
}
_CLAIM_ALIASES = {
    "ai": ("artificial intelligence", "人工智能", " ai "),
    "agent": ("agent", "智能体"),
    "dataset": ("dataset", "数据集"),
    "inference": ("inference", "推理"),
    "llm": ("large language model", "llm", "大语言模型", "大模型"),
    "model": ("model", "模型"),
    "open-source": ("open source", "open-source", "开源"),
    "release": (
        "available",
        "launch",
        "launched",
        "publish",
        "published",
        "release",
        "released",
        "上线",
        "发布",
        "推出",
    ),
    "research": ("research", "研究"),
    "tool": ("tool", "工具"),
    "training": ("training", "训练"),
}
_LATIN_CLAIM_STOPWORDS = {
    "about",
    "from",
    "more",
    "new",
    "news",
    "now",
    "official",
    "product",
    "the",
    "this",
    "today",
    "update",
    "with",
}
_GENERIC_LATIN_CLAIM_WORDS = _LATIN_CLAIM_STOPWORDS | {
    word
    for aliases in _CLAIM_ALIASES.values()
    for alias in aliases
    for word in re.findall(r"[a-z]+", alias)
}
_CJK_CLAIM_STOP_PHRASES = (
    "人工智能",
    "大语言模型",
    "机器学习",
    "大模型",
    "开发团队",
    "智能体",
    "行业动态",
    "正式发布",
    "现已发布",
    "推理",
    "训练",
    "模型",
    "工具",
    "研究",
    "机构",
    "官方",
    "发布",
    "推出",
    "上线",
    "正式",
    "现已",
    "已经",
    "更新",
    "新增",
    "增加",
    "支持",
    "能力",
    "说明",
    "开放",
    "行业",
    "动态",
    "详情",
    "访问",
    "官网",
    "更多",
    "消息",
    "报道",
    "来源",
    "声称",
    "即将",
    "开发",
    "团队",
    "某",
    "该",
    "由",
    "的",
    "了",
    "并",
    "新",
)
_POSITIVE_RELEASE_MARKERS = (
    "added",
    "adds",
    "available now",
    "has launched",
    "has released",
    "is released",
    "launched",
    "publish",
    "published",
    "release",
    "released",
    "supports",
    "增加",
    "新增",
    "支持",
    "正式发布",
    "现已发布",
    "已经发布",
    "已正式发布",
    "已发布",
    "现已上线",
    "上线",
    "发布",
    "推出",
)
_NEGATIVE_RELEASE_MARKERS = (
    "cancelled",
    "canceled",
    "delayed",
    "denied",
    "discontinued",
    "has not launched",
    "has not released",
    "not launched",
    "not released",
    "no longer supports",
    "removed",
    "取消发布",
    "否认发布",
    "尚未上线",
    "尚未发布",
    "延期发布",
    "推迟发布",
    "未上线",
    "未发布",
    "不再支持",
    "停止支持",
    "移除",
    "下线",
)


def prepare_search_candidates(
    leads: Iterable[SearchLead],
    *,
    now: datetime,
    window_hours: int,
    event_dedupe_days: int,
    sent_state: SentState,
    search_config: BaiduSearchConfig,
) -> list[Candidate]:
    eligible = _eligible_leads(
        leads,
        now=now,
        window_hours=window_hours,
        sent_state=sent_state,
        event_dedupe_days=event_dedupe_days,
    )
    site_limited = _limit_leads_by_site(
        eligible,
        configured_domains=[
            *search_config.first_party_domains,
            *search_config.trusted_domains,
        ],
    )
    candidates = [
        candidate
        for event_leads in _group_events(site_limited).values()
        if (
            candidate := _grade_event(
                event_leads,
                first_party_domains=search_config.first_party_domains,
                trusted_domains=search_config.trusted_domains,
            )
        )
        is not None
    ]
    candidates.sort(
        key=lambda candidate: (
            candidate.published_at,
            candidate.relevance_score or 0,
            candidate.authority_score or 0,
            str(candidate.url),
        ),
        reverse=True,
    )
    return candidates[:MODEL_CANDIDATE_LIMIT]


def _eligible_leads(
    leads: Iterable[SearchLead],
    *,
    now: datetime,
    window_hours: int,
    sent_state: SentState,
    event_dedupe_days: int,
) -> list[SearchLead]:
    cutoff = now - timedelta(hours=window_hours)
    by_url: dict[str, SearchLead] = {}
    for lead in leads:
        if lead.published_at.tzinfo is None or lead.published_at.utcoffset() is None:
            continue
        if not cutoff <= lead.published_at <= now:
            continue
        if not _is_ai_related(lead):
            continue
        if sent_state.is_repeated_event(
            lead.title,
            lead.snippet,
            now,
            event_dedupe_days=event_dedupe_days,
        ):
            continue
        canonical_url = canonicalize_url(str(lead.url))
        if sent_state.was_sent_since(
            canonical_url,
            now - timedelta(days=30),
        ):
            continue
        existing = by_url.get(canonical_url)
        if existing is None or lead.published_at > existing.published_at:
            by_url[canonical_url] = lead
    return sorted(
        by_url.values(),
        key=lambda lead: (lead.published_at, str(lead.url)),
        reverse=True,
    )


def _limit_leads_by_site(
    leads: Iterable[SearchLead],
    *,
    configured_domains: Iterable[str],
) -> list[SearchLead]:
    site_counts: dict[str, int] = defaultdict(int)
    limited: list[SearchLead] = []
    for lead in leads:
        host = _host(str(lead.url))
        site = _matched_domain(host, configured_domains) or host
        if site_counts[site] >= SITE_CANDIDATE_LIMIT:
            continue
        site_counts[site] += 1
        limited.append(lead)
    return limited


def _group_events(leads: Iterable[SearchLead]) -> dict[str, list[SearchLead]]:
    ordered = sorted(
        leads,
        key=lambda lead: (_event_basis(lead.title), str(lead.url)),
    )
    bases = [_event_basis(lead.title) for lead in ordered]
    parents = list(range(len(ordered)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for left in range(len(ordered)):
        for right in range(left + 1, len(ordered)):
            if _same_event(ordered[left].title, ordered[right].title):
                left_root = root(left)
                right_root = root(right)
                parents[right_root] = left_root

    events: dict[str, list[SearchLead]] = {}
    for index, lead in enumerate(ordered):
        key = bases[root(index)]
        events.setdefault(key, []).append(lead)
    for event_leads in events.values():
        event_leads.sort(
            key=lambda lead: (
                lead.published_at,
                lead.relevance_score or 0,
                lead.authority_score or 0,
                str(lead.url),
            ),
            reverse=True,
        )
    return events


def _grade_event(
    leads: list[SearchLead],
    *,
    first_party_domains: list[str],
    trusted_domains: list[str],
) -> Candidate | None:
    first_party = [
        lead
        for lead in leads
        if _matches_configured_domain(_host(str(lead.url)), first_party_domains)
        and _excerpt_supports_title(lead.title, lead.snippet)
    ]
    if first_party:
        evidence_leads = _one_per_site(first_party, first_party_domains)
        selected = evidence_leads[0]
        status = VerificationStatus.CONFIRMED
        organization_id = _matched_domain(
            _host(str(selected.url)), first_party_domains
        )
    else:
        trusted = [
            lead
            for lead in leads
            if _matches_configured_domain(_host(str(lead.url)), trusted_domains)
            and _excerpt_supports_title(lead.title, lead.snippet)
        ]
        evidence_leads = _corroborating_evidence(trusted, trusted_domains)
        if not evidence_leads:
            return None
        selected = evidence_leads[0]
        status = VerificationStatus.UNVERIFIED
        organization_id = None

    event_basis = _event_basis(selected.title)
    candidate_payload = selected.as_candidate().model_dump()
    candidate_payload.update(
        {
            "event_id": hashlib.sha256(event_basis.encode("utf-8")).hexdigest()[:24],
            "organization_id": organization_id,
            "verification_status": status,
            "evidence": tuple(
                EvidenceSource(
                    source=lead.source,
                    url=lead.url,
                    excerpt=lead.snippet,
                )
                for lead in evidence_leads
            ),
        }
    )
    return Candidate.model_validate(candidate_payload)


def _one_per_site(
    leads: Iterable[SearchLead],
    configured_domains: Iterable[str],
) -> list[SearchLead]:
    selected: list[SearchLead] = []
    seen: set[str] = set()
    for lead in leads:
        host = _host(str(lead.url))
        site = _matched_domain(host, configured_domains) or host
        if site in seen:
            continue
        selected.append(lead)
        seen.add(site)
    return selected


def _corroborating_evidence(
    leads: Iterable[SearchLead],
    configured_domains: Iterable[str],
) -> list[SearchLead]:
    independent = _one_per_site(leads, configured_domains)
    for left_index, left in enumerate(independent):
        for right in independent[left_index + 1 :]:
            if not _excerpts_agree(left.snippet, right.snippet):
                continue
            return [
                lead
                for lead in independent
                if _excerpts_agree(left.snippet, lead.snippet)
                and _excerpts_agree(right.snippet, lead.snippet)
            ]
    return []


def _excerpt_supports_title(title: str, excerpt: str) -> bool:
    if not excerpt.strip() or not _release_claims_agree(title, excerpt):
        return False
    title_markers = _claim_markers(title)
    excerpt_markers = _claim_markers(excerpt)
    if "release" in title_markers and "release" not in excerpt_markers:
        return False
    title_identity = _distinctive_markers(title)
    if title_identity and title_identity.isdisjoint(
        _distinctive_markers(excerpt)
    ):
        return False
    title_values = _hard_values(title)
    excerpt_values = _hard_values(excerpt)
    if title_values and not title_values.issubset(excerpt_values):
        return False
    return len(title_markers & excerpt_markers) >= 2


def _excerpts_agree(left: str, right: str) -> bool:
    if not _release_claims_agree(left, right):
        return False
    left_values = _hard_values(left)
    right_values = _hard_values(right)
    if left_values and right_values and left_values.isdisjoint(right_values):
        return False
    left_polarity = _claim_polarity(left)
    right_polarity = _claim_polarity(right)
    if (left_polarity is None or right_polarity is None) and not (
        left_values & right_values
    ):
        return False
    return len(_claim_markers(left) & _claim_markers(right)) >= 2


def _release_claims_agree(left: str, right: str) -> bool:
    left_polarity = _claim_polarity(left)
    right_polarity = _claim_polarity(right)
    return (
        not left_polarity
        or not right_polarity
        or left_polarity == right_polarity
    )


def _claim_polarity(text: str) -> str | None:
    normalized = " ".join(text.casefold().split())
    if any(marker in normalized for marker in _NEGATIVE_RELEASE_MARKERS):
        return "negative"
    if any(marker in normalized for marker in _POSITIVE_RELEASE_MARKERS):
        return "positive"
    return None


def _hard_values(text: str) -> set[str]:
    values = re.findall(
        r"(?<![a-z0-9])(?:v(?:ersion)?\s*)?\d+(?:\.\d+)*(?:\s*(?:%|[kmgb]|万|亿))?",
        text.casefold(),
    )
    return {
        re.sub(r"^(?:v(?:ersion)?)?\s*", "", value).replace(" ", "")
        for value in values
    }


def _distinctive_markers(text: str) -> set[str]:
    normalized = text.casefold()
    latin_markers = {
        token
        for token in re.findall(r"[a-z][a-z0-9._-]*", normalized)
        if token not in _GENERIC_LATIN_CLAIM_WORDS
    }
    cjk_text = normalized
    for phrase in _CJK_CLAIM_STOP_PHRASES:
        cjk_text = cjk_text.replace(phrase, " ")
    cjk_markers = set(re.findall(r"[\u3400-\u9fff]{2,}", cjk_text))
    return _hard_values(normalized) | latin_markers | cjk_markers


def _claim_markers(text: str) -> set[str]:
    normalized = f" {' '.join(text.casefold().split())} "
    markers = {
        canonical
        for canonical, aliases in _CLAIM_ALIASES.items()
        if any(_contains_alias(normalized, alias) for alias in aliases)
    }
    markers.update(_distinctive_markers(normalized))
    return markers


def _contains_alias(text: str, alias: str) -> bool:
    if re.fullmatch(r"[a-z][a-z -]*", alias):
        return bool(
            re.search(
                rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])",
                text,
            )
        )
    return alias in text


def _same_event(left_title: str, right_title: str) -> bool:
    left_identity = _distinctive_markers(left_title)
    right_identity = _distinctive_markers(right_title)
    compatible_identity = (
        not left_identity
        or not right_identity
        or left_identity.issubset(right_identity)
        or right_identity.issubset(left_identity)
    )
    if not compatible_identity:
        return False
    if (
        SequenceMatcher(
            None,
            _event_basis(left_title),
            _event_basis(right_title),
        ).ratio()
        >= 0.78
    ):
        return True
    shared_identity = left_identity & right_identity
    shared_claim = _claim_markers(left_title) & _claim_markers(right_title)
    return bool(shared_identity) and len(shared_claim) >= 2


def _is_ai_related(lead: SearchLead) -> bool:
    text = f"{lead.title} {lead.snippet}".casefold()
    return any(phrase in text for phrase in _AI_PHRASES) or any(
        re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text)
        for word in _AI_WORDS
    )


def _event_basis(title: str) -> str:
    return "".join(re.findall(r"\w+", title.casefold(), flags=re.UNICODE))


def _matches_configured_domain(host: str, domains: Iterable[str]) -> bool:
    return _matched_domain(host, domains) is not None


def _matched_domain(host: str, domains: Iterable[str]) -> str | None:
    return next(
        (
            domain
            for domain in domains
            if host == domain or host.endswith(f".{domain}")
        ),
        None,
    )


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").casefold().rstrip(".")
