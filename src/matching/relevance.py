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
CMW sends mobile crews and equipment to client sites.

SERVICES CMW PROVIDES — answer YES or MAYBE for tenders involving these:
- Fleet & vehicle washing: trucks, buses, transit vehicles, heavy equipment, \
trailers, tankers, garbage/refuse trucks, municipal fleet, construction equipment
- Underground parking garage and parkade cleaning (pressure washing, \
floor scrubbing, sweeping, line painting)
- Exterior building washing and facade cleaning (pressure/power washing, \
soft washing, brick, stone, concrete, masonry)
- Parking lot washing, surface cleaning, and line painting / pavement marking
- Graffiti removal and abatement from any surface
- Interior warehouse cleaning: floors, walls, racking, beams, vents, \
hard-to-reach areas, high dusting — industrial and commercial warehouses only
- Cold storage and temperature-controlled facility cleaning
- Commercial disinfection and sanitization services (vehicles, facilities)
- Catch basin cleaning, dumpster pad washing, garage bin washing
- Heavy equipment degreasing and undercarriage washing
- Window cleaning (commercial buildings and properties)
- Dock door washing
- Bus shelter washing
- Decal removal from vehicles, equipment, and commercial surfaces
- Post-construction exterior cleaning

SERVICES CMW DOES NOT PROVIDE — answer NO for tenders involving only these:
- Residential property cleaning of any kind
- Interior office cleaning, janitorial services, or housekeeping
- Waste collection, garbage removal, or hazardous waste disposal
- Snow removal or landscaping / grounds maintenance
- HVAC or duct cleaning
- Carpet, upholstery, or dry cleaning
- Pest control
- Sewer, plumbing, or drain repair work
- Roofing
- Asbestos or mold remediation
- Medical, biohazard, or food-service kitchen cleaning
- Interior vehicle detailing (upholstery, carpet)

IMPORTANT NUANCES:
- Interior warehouse and cold storage cleaning IS in scope; office/janitorial is NOT.
- A tender that bundles CMW-scope work with out-of-scope work should be MAYBE, \
not NO — CMW may be able to bid on the relevant portion.
- Vague titles like "Facility Maintenance Services" or "General Cleaning Contract" \
should be MAYBE unless the description clarifies scope.
- Municipal tenders for fleet maintenance facilities, transit depots, public works \
yards, and arenas often include vehicle or building washing — lean toward MAYBE \
if the description is unclear.\
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
