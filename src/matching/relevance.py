"""
Optional LLM relevance pass (Anthropic API).

Only runs when:
  - settings.yaml: llm.enabled = true
  - ANTHROPIC_API_KEY is set in the environment

Only called for Tier-2 and Tier-3 candidates to keep cost bounded.
Never called for every tender.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a procurement analyst for Canadian Mobile Wash (CMW),
a company that provides: fleet washing (trucks, buses, heavy equipment),
parking-structure and parkade cleaning, pressure washing and building exterior
washing, graffiti removal, and dumpster/catch-basin washing.

Your job is to read a tender's title and description and decide whether
CMW's services are plausibly in scope — even if the tender bundles them
with other work."""

_USER_TEMPLATE = """Tender title: {title}

Tender description:
{description}

Question: Is exterior washing, pressure washing, fleet washing, parkade/parking-structure
cleaning, or graffiti removal plausibly within the scope of this tender?

Answer with exactly one word: yes, no, or maybe."""


def adjudicate(
    title: str,
    description: str,
    model: str,
    max_tokens: int,
) -> Optional[str]:
    """
    Returns 'yes', 'no', 'maybe', or None on error.
    Caller decides what to do with 'maybe' (treated as Medium confidence).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.debug("ANTHROPIC_API_KEY not set; skipping LLM pass")
        return None

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": _USER_TEMPLATE.format(
                        title=title,
                        description=description[:2000],  # stay within token budget
                    ),
                }
            ],
        )
        answer = message.content[0].text.strip().lower().rstrip(".")
        if answer not in ("yes", "no", "maybe"):
            logger.warning("Unexpected LLM answer: %r — treating as maybe", answer)
            return "maybe"
        return answer
    except Exception as exc:
        logger.warning("LLM relevance pass failed: %s", exc)
        return None
