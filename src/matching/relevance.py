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
CMW dispatches mobile crews and equipment directly to client sites.

CMW'S SERVICE LINES:

1. Fleet Washing — trucks, buses, transit vehicles, heavy equipment, trailers, \
tankers, garbage/refuse trucks, municipal fleet, construction equipment. \
Includes exterior wash, undercarriage, degreasing, sanitization, decal removal.

2. Commercial Property Cleaning — pressure/power washing and soft washing of \
building exteriors, facades, brick, stone, concrete, masonry, storefronts, \
canopies, and parking structures (underground garages, parkades, parking lots). \
Includes catch basin cleaning, dumpster pad washing, garage bin washing, \
dock door washing, bus shelter washing, and post-construction exterior cleaning.

3. Commercial Disinfection & Sanitization — disinfection of vehicles, \
facilities, commercial interiors and surfaces.

4. Window Washing — exterior window cleaning for commercial buildings \
and properties.

5. Graffiti Removal — removal of graffiti and vandalism from any surface \
(buildings, vehicles, fencing, signage).

6. Line Painting & Pavement Marking — parking lots, roads, commercial spaces; \
fresh markings, re-striping, custom stenciling.

7. Interior Warehouse Cleaning — floors, walls, ceilings, racking, beams, \
vents, hard-to-reach areas, high dusting. Commercial and industrial warehouses, \
distribution centres, manufacturing facilities.

8. Cold Storage & Temperature-Controlled Facility Cleaning — floors, walls, \
ceilings, loading areas, interior surfaces of refrigerated or frozen facilities.

9. Decal Removal — lettering, graphics, adhesives, and residue from vehicles, \
equipment, windows, and commercial surfaces.

OUT OF SCOPE — answer NO only if the tender is exclusively about:
- Residential cleaning (houses, condos, apartments)
- Interior office/janitorial/housekeeping services
- Waste or garbage collection, hazardous waste disposal
- Snow removal or landscaping
- HVAC or duct cleaning
- Carpet, upholstery, or dry cleaning
- Pest control
- Sewer, plumbing, or drain construction/repair
- Roofing
- Asbestos or mold remediation
- Medical or biohazard cleaning
- Food-service kitchen cleaning
- Interior vehicle detailing (carpet, upholstery)

DECISION RULES:
- YES: tender clearly involves one or more CMW service lines.
- MAYBE: tender is vague, bundles CMW work with out-of-scope work, or involves \
a facility type (transit depot, public works yard, arena, community centre) \
where CMW services are plausible but not explicitly stated. When in doubt, \
answer MAYBE — it is better to flag a borderline opportunity than miss it.
- NO: tender is exclusively out-of-scope with no plausible CMW angle.\
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
