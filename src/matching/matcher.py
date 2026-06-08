"""
Tiered keyword matching engine.

Tier 1 — high precision: literal/stemmed hit → High confidence (auto-flag)
Tier 2 — disambiguate:   hit must pass disqualifiers → Medium confidence
Tier 3 — broad net:      hit must pass disqualifiers AND LLM says yes (or goes to Review)

Scoring:
  score = tier_weight + keyword_count * keyword_bonus + category_diversity * cat_bonus
  mapped to confidence: score >= high_threshold → High, >= medium_threshold → Medium, else Review
"""

import logging
import re
from typing import Optional

import yaml

from src.matching.normalize import keyword_pattern, normalize
from src.storage.models import Match, Tender

logger = logging.getLogger(__name__)


def load_keywords(keywords_path: str) -> list[dict]:
    with open(keywords_path) as f:
        data = yaml.safe_load(f)
    keywords = data.get("keywords", [])
    # Pre-compile each keyword's regex pattern
    for kw in keywords:
        kw["_pattern"] = keyword_pattern(kw["keyword"])
    return keywords


def load_disqualifiers(disqualifiers_path: str) -> list[dict]:
    with open(disqualifiers_path) as f:
        data = yaml.safe_load(f)
    disqs = data.get("disqualifiers", [])
    for d in disqs:
        d["_pattern"] = keyword_pattern(d["term"])
    return disqs


def _check_disqualifiers(text: str, disqualifiers: list[dict]) -> tuple[bool, str]:
    """
    Returns (should_suppress, action).
    - suppress: remove the match entirely
    - downgrade: reduce confidence one band
    """
    norm = normalize(text)
    for d in disqualifiers:
        if d["_pattern"].search(norm):
            return True, d["action"]
    return False, ""


def _confidence_from_score(score: float, thresholds: dict) -> str:
    if score >= thresholds["high"]:
        return "High"
    if score >= thresholds["medium"]:
        return "Medium"
    return "Review"


def _downgrade_confidence(confidence: str) -> str:
    order = ["High", "Medium", "Review"]
    idx = order.index(confidence)
    return order[min(idx + 1, len(order) - 1)]


def match_tender(
    tender: Tender,
    keywords: list[dict],
    disqualifiers: list[dict],
    tier_weights: dict,
    keyword_bonus: float,
    category_diversity_bonus: float,
    confidence_thresholds: dict,
    relevance_label: Optional[str] = None,
    relevance_bonus: float = 0,
) -> Optional[Match]:
    """
    Run the full matching logic for a single tender.
    Returns a Match if any keyword hits; None if no match.
    """
    search_text = f"{tender.title} {tender.description}"
    norm_text = normalize(search_text)

    matched: list[dict] = []
    for kw in keywords:
        if kw["_pattern"].search(norm_text):
            matched.append(kw)

    if not matched:
        return None

    # Split by tier
    tiers_hit = {kw["tier"] for kw in matched}
    top_tier = min(tiers_hit)  # lowest number = highest priority

    # Check disqualifiers against full text
    hits_suppress, action = _check_disqualifiers(search_text, disqualifiers)

    # Tier-1 hits are never fully suppressed — a strong specific signal (fleet wash,
    # graffiti, parkade) survives even if the tender also mentions janitorial work.
    # Suppress only applies to Tier-2/3-only matches.
    if hits_suppress and action == "suppress" and top_tier > 1:
        logger.debug("Tender %s suppressed by disqualifier", tender.id)
        return None
    if hits_suppress and action == "suppress" and top_tier == 1:
        # Treat suppress as downgrade when Tier-1 hit is present
        action = "downgrade"

    # Compute score
    tier_weight = tier_weights.get(str(top_tier), tier_weights.get(top_tier, 1))
    categories_hit = list({kw["category"] for kw in matched})
    score = (
        tier_weight
        + len(matched) * keyword_bonus
        + (len(categories_hit) - 1) * category_diversity_bonus
    )

    # LLM relevance bonus
    if relevance_label == "yes" and top_tier >= 2:
        score += relevance_bonus

    # Map to confidence
    confidence = _confidence_from_score(score, confidence_thresholds)

    # Downgrade on disqualifier "downgrade" action
    if hits_suppress and action == "downgrade":
        confidence = _downgrade_confidence(confidence)

    # Tier-3-only with LLM disabled → always Review regardless of score
    if tiers_hit == {3} and relevance_label is None:
        confidence = "Review"

    # Tier-3-only where LLM said "no"
    if tiers_hit == {3} and relevance_label == "no":
        return None

    matched_keyword_strs = [kw["keyword"] for kw in matched]

    return Match(
        tender_id=tender.id,
        matched_keywords=matched_keyword_strs,
        categories=categories_hit,
        top_tier=top_tier,
        score=score,
        confidence=confidence,
        relevance_label=relevance_label,
    )
