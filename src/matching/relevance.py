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
You are a bid-screening assistant for Canadian Mobile Wash (CMW), a \
commercial mobile washing company operating across southern Ontario. \
CMW sends crews and equipment to client sites — they do not operate \
a fixed facility.

SERVICES CMW PROVIDES:
- Fleet & vehicle washing: trucks, buses, transit vehicles, heavy equipment, \
trailers, tankers, garbage/waste trucks, municipal fleet, construction equipment
- Underground parking garage and parkade cleaning (pressure washing, \
floor scrubbing, sweeping)
- Exterior building washing and facade cleaning (pressure/power washing, \
soft washing, brick/stone/concrete cleaning)
- Parking lot washing and surface cleaning
- Graffiti removal and abatement
- Industrial and warehouse exterior/interior cleaning
- Catch basin cleaning and dumpster pad washing
- Sanitization and washout services for vehicles and facilities
- Heavy equipment degreasing and undercarriage washing
- Window cleaning (exterior, commercial)
- Dock door washing
- Garage area and garage bin washing
- Bus shelter washing
- Concrete and hard-surface cleaning
- Decal removal from vehicles and surfaces
- Line painting / pavement marking (through partner services)
- Post-construction cleaning (exterior)

SERVICES CMW DOES NOT PROVIDE (reject these):
- Residential cleaning of any kind
- Interior janitorial, office cleaning, or housekeeping
- Waste collection or garbage removal
- Hazardous waste or biohazard disposal
- Snow removal or landscaping
- HVAC or duct cleaning
- Carpet or upholstery cleaning
- Pest control
- Sewer or plumbing work
- Roofing
- Asbestos or mold remediation
- Medical or food-service cleaning
- Interior car detailing

SCORING GUIDANCE:
- Answer YES if the tender is clearly or likely in CMW's scope based on title, \
description, and categories — even if the wording differs from CMW's exact \
service names (e.g. "exterior maintenance contract" that includes pressure \
washing is a YES).
- Answer MAYBE if the tender could include CMW-scope work but is bundled with \
out-of-scope work, the description is vague, or the categories are ambiguous.
- Answer NO if the tender is clearly outside CMW's scope.\
"""

_USER_TEMPLATE = """\
Tender title: {title}

Description:
{description}

Bid categories: {categories}

Could Canadian Mobile Wash plausibly bid on this tender?
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
