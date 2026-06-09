"""
Optional LLM relevance pass (Anthropic API).

Only runs when:
  - settings.yaml: llm.enabled = true
  - ANTHROPIC_API_KEY is set in the environment

Called for any tender that hits a keyword match, to confirm or reject it.
Title + description + bid categories are all sent for context.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ── Prompts ───────────────────────────────────────────────────────────────────
# TODO: finalize system prompt with CMW — placeholder below

_SYSTEM_PROMPT = """\
You are a procurement analyst helping Canadian Mobile Wash (CMW) identify \
relevant tender opportunities. CMW is a commercial mobile washing company \
that provides exterior cleaning services across Ontario.

[PROMPT TO BE FINALIZED WITH CMW]\
"""

_USER_TEMPLATE = """\
Tender title: {title}

Description:
{description}

Bid categories: {categories}

Based on the above, could CMW plausibly bid on this tender?
Answer with exactly one word: yes, no, or maybe.\
"""


def adjudicate(
    title: str,
    description: str,
    bid_categories: list[str],
    model: str,
    max_tokens: int,
) -> Optional[str]:
    """
    Returns 'yes', 'no', 'maybe', or None on error/API key missing.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.debug("ANTHROPIC_API_KEY not set; skipping LLM pass")
        return None

    categories_text = ", ".join(bid_categories) if bid_categories else "None provided"
    description_text = description.strip() if description.strip() else "No description available."

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
                        description=description_text[:2000],
                        categories=categories_text,
                    ),
                }
            ],
        )
        answer = message.content[0].text.strip().lower().rstrip(".")
        if answer not in ("yes", "no", "maybe"):
            logger.warning("Unexpected LLM answer %r for %r — treating as maybe", answer, title)
            return "maybe"
        logger.info("LLM relevance [%s]: %s", answer.upper(), title[:80])
        return answer
    except Exception as exc:
        logger.warning("LLM relevance pass failed: %s", exc)
        return None
