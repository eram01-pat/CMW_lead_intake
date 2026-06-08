from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
import json


@dataclass
class Tender:
    id: str
    source_id: str
    source_name: str
    title: str
    description: str
    category: str
    reference_no: str
    detail_url: str
    status: str
    posted_date: Optional[date]
    closing_date: Optional[date]
    raw: dict
    first_seen_at: datetime = field(default_factory=datetime.utcnow)
    last_seen_at: datetime = field(default_factory=datetime.utcnow)

    def raw_json(self) -> str:
        return json.dumps(self.raw, default=str)


@dataclass
class Match:
    tender_id: str
    matched_keywords: list[str]
    categories: list[int]
    top_tier: int
    score: float
    confidence: str       # "High" | "Medium" | "Review"
    relevance_label: Optional[str] = None  # from LLM pass; None if disabled
    created_at: datetime = field(default_factory=datetime.utcnow)

    def matched_keywords_json(self) -> str:
        return json.dumps(self.matched_keywords)

    def categories_json(self) -> str:
        return json.dumps(self.categories)
