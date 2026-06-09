"""
LLM relevance adjudication (Anthropic API).

Called for every new tender in the LLM-first pipeline.
Returns 'yes', 'no', or 'maybe' based on whether Canadian Mobile Wash
could plausibly bid on the work described.
"""

import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a bid-screening assistant for Canadian Mobile Wash (CMW), a \
commercial mobile washing company operating across southern Ontario. \
CMW dispatches mobile crews and equipment directly to client sites.

CMW'S SERVICES (what they do):
- Fleet washing — trucks, buses, transit vehicles, municipal fleet, tankers, \
garbage/refuse trucks, trailers
- Commercial vehicle washing — all commercial and heavy vehicles
- Pressure washing / power washing — any commercial surface
- Underground parking garage cleaning
- Parking lot washing
- Property maintenance washing
- Graffiti removal — from buildings, vehicles, fencing, signage
- Warehouse and industrial cleaning — floors, walls, ceilings, racking, beams, \
vents, high dusting
- Exterior building washing — facades, brick, stone, concrete, masonry, \
storefronts, canopies
- Sanitization and washout services — vehicles, facilities, commercial surfaces
- Heavy equipment cleaning and degreasing
- Concrete and surface cleaning
- Window cleaning (commercial)
- Dock door washing
- Garage area washing
- Garage bin washing
- Bus shelter washing
- Line painting — parking lots, roads, commercial spaces, re-striping, \
stenciling (offered through a partnering service — still flag these opportunities)
- Decal removal — lettering, graphics, adhesives from vehicles and surfaces

OUT OF SCOPE — answer NO if the tender is exclusively about:
- Residential cleaning (houses, condos, apartments)
- Interior janitorial, office cleaning, or housekeeping services
- Waste collection or garbage removal
- Hazardous waste disposal
- Snow removal or landscaping
- HVAC or duct cleaning
- Carpet cleaning
- Pest control
- Sewer or plumbing services
- Roofing
- Asbestos or mold remediation
- Medical or biohazard cleaning
- Food service or commercial kitchen cleaning
- Construction, renovation, or capital works (CMW cleans facilities, \
does not build or renovate them)
- Design, engineering, or consulting services
- Procurement of vehicles, apparatus, or equipment (fire trucks, sweeper \
machines, fleet vehicles — buying/leasing hardware, not a cleaning service)
- Cooperative purchasing agreements, standing offers, or vendor-of-record \
arrangements for goods (e.g. Canoe, Sourcewell, cooperative procurement notices)

DECISION RULES:
- YES: tender clearly involves one or more CMW services listed above.
- MAYBE: the tender is vague, bundles CMW work with out-of-scope work, or is \
for ongoing operations/maintenance at a facility where CMW services are \
plausible (transit depot, public works yard, operations centre, arena, \
community centre) but the cleaning scope is not explicitly stated. \
When in doubt, answer MAYBE — missing a real opportunity is worse than \
flagging a borderline one.
- NO: tender is exclusively out-of-scope with no plausible CMW angle.\
"""

_USER_TEMPLATE = """\
Tender title: {title}

Description:
{description}

Bid categories: {categories}

Note: if the description is empty or uninformative, base your decision on the \
title and bid categories alone.

Could Canadian Mobile Wash plausibly bid on this tender?
Answer with:
DECISION: yes / no / maybe
REASON: one sentence (max 20 words) explaining why — only include this line if DECISION is yes or maybe\
"""


def adjudicate(
    title: str,
    description: str,
    bid_categories: list[str],
    model: str,
    max_tokens: int,
) -> tuple[Optional[str], Optional[str]]:
    """
    Returns (decision, reason) where decision is 'yes', 'no', 'maybe', or None on error.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.debug("ANTHROPIC_API_KEY not set; skipping LLM pass")
        return None, None

    categories_text = ", ".join(bid_categories) if bid_categories else "None provided"
    description_text = description.strip() if description.strip() else "No description available."

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    for attempt in range(4):
        try:
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
            raw = message.content[0].text.strip()
            decision, reason = _parse_response(raw, title)
            logger.info("LLM relevance [%s]: %s", decision.upper(), title[:80])
            time.sleep(2.0)
            return decision, reason
        except anthropic.RateLimitError:
            wait = 10 * (2 ** attempt)  # 10s, 20s, 40s, 80s
            logger.warning("Rate limited — waiting %ds before retry (attempt %d/4)", wait, attempt + 1)
            time.sleep(wait)
        except Exception as exc:
            logger.warning("LLM relevance pass failed: %s", exc)
            return None, None

    logger.warning("LLM relevance gave up after 4 rate-limit retries for %r", title)
    return None, None


def _parse_response(raw: str, title: str) -> tuple[str, Optional[str]]:
    """Parse DECISION/REASON lines from LLM response. Falls back gracefully."""
    decision = "maybe"
    reason: Optional[str] = None
    for line in raw.splitlines():
        line = line.strip()
        if line.lower().startswith("decision:"):
            val = line.split(":", 1)[1].strip().lower().rstrip(".")
            if val in ("yes", "no", "maybe"):
                decision = val
            else:
                logger.warning("Unexpected DECISION value %r for %r — treating as maybe", val, title)
        elif line.lower().startswith("reason:"):
            reason = line.split(":", 1)[1].strip()
    if reason is None:
        # Model returned a single word — treat whole response as decision
        single = raw.strip().lower().rstrip(".")
        if single in ("yes", "no", "maybe"):
            decision = single
    return decision, reason
