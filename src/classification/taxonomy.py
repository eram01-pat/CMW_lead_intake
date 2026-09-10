"""
Industry taxonomy for tender classification.

Two levels:
  - industry  — the fine-grained trade / sector a tender belongs to
  - group     — the roll-up bucket the industry sits in (for summary reporting)

Every tender is assigned exactly one primary industry from INDUSTRIES, and
optionally one secondary industry when the scope genuinely spans two trades.

Bump TAXONOMY_VERSION whenever the list below changes: cached classifications
carry the version they were produced under, and `--force` / a version bump is
how you re-run everything against a revised taxonomy.
"""

TAXONOMY_VERSION = "1.0"

# Sentinel used in the LLM response schema when there is no secondary industry.
# (JSON Schema enums are cleaner with a string sentinel than a nullable enum.)
NO_SECONDARY = "None"

UNCLASSIFIED = "Other / Unclassified"

# group -> ordered list of industries in that group
TAXONOMY: dict[str, list[str]] = {
    "Cleaning & Facility Services": [
        "Fleet & Vehicle Washing",
        "Pressure Washing & Exterior Cleaning",
        "Janitorial & Custodial Services",
        "Window Cleaning",
        "Waste Collection & Recycling",
        "Pest Control",
        "Facility Operations & Maintenance",
    ],
    "Building Trades & Construction": [
        "Painting & Coatings",
        "HVAC & Mechanical",
        "Electrical",
        "Plumbing",
        "Roofing",
        "Flooring",
        "Masonry, Concrete & Structural Restoration",
        "Doors, Windows & Glazing",
        "Elevators & Escalators",
        "Fire Protection & Life Safety",
        "General Construction & Renovation",
        "Demolition & Hazardous Materials Abatement",
    ],
    "Infrastructure & Public Works": [
        "Road & Highway Construction",
        "Hardscaping, Paving & Sidewalks",
        "Line Painting & Pavement Marking",
        "Bridges & Structures",
        "Water & Wastewater Treatment",
        "Sewer, Stormwater & Watermain",
        "Utilities & Streetlighting",
        "Traffic Systems & Signals",
        "Winter Maintenance & Snow Removal",
    ],
    "Grounds & Environment": [
        "Landscaping & Grounds Maintenance",
        "Tree Care & Arboriculture",
        "Parks, Playgrounds & Sports Facilities",
        "Fencing & Site Furnishings",
        "Environmental Services & Remediation",
    ],
    "Professional & Technical Services": [
        "Engineering & Design Consulting",
        "Architecture & Planning",
        "Surveying & Geotechnical",
        "Legal, Audit & Financial Services",
        "Staffing, HR & Training",
        "Marketing, Communications & Printing",
        "Research, Studies & Policy Consulting",
    ],
    "Technology & Communications": [
        "IT Hardware & Software",
        "Telecommunications & Networking",
        "Audio Visual & Broadcast",
        "Security Systems & Surveillance",
    ],
    "Goods & Equipment Supply": [
        "Vehicles & Heavy Equipment",
        "Furniture & Fixtures",
        "Materials & Industrial Supplies",
        "Uniforms, Apparel & PPE",
        "Food & Catering Supplies",
    ],
    "People & Community Services": [
        "Transportation & Transit Services",
        "Security Guard Services",
        "Health, Medical & Social Services",
        "Education & Recreation Programs",
        "Event & Facility Rental Services",
    ],
    "Other": [
        UNCLASSIFIED,
    ],
}

# Flat, stable ordering — this is the order presented to the model and the
# order used to break ties in the report.
INDUSTRIES: list[str] = [ind for inds in TAXONOMY.values() for ind in inds]

INDUSTRY_TO_GROUP: dict[str, str] = {
    ind: group for group, inds in TAXONOMY.items() for ind in inds
}

GROUPS: list[str] = list(TAXONOMY.keys())

# Short disambiguating hints for industries that are easy to confuse.
# Only industries that actually need a hint appear here.
INDUSTRY_HINTS: dict[str, str] = {
    "Fleet & Vehicle Washing": "washing/cleaning vehicles, buses, transit fleet, heavy equipment",
    "Pressure Washing & Exterior Cleaning": "power washing, building exterior washing, graffiti removal, parking garage cleaning",
    "Janitorial & Custodial Services": "interior cleaning, housekeeping, caretaking",
    "Facility Operations & Maintenance": "bundled multi-trade building maintenance with no single dominant trade",
    "Masonry, Concrete & Structural Restoration": "brick, block, concrete repair, building envelope restoration, parking structure rehab",
    "General Construction & Renovation": "new builds, additions, multi-trade renovations, general contracting",
    "Hardscaping, Paving & Sidewalks": "asphalt/concrete paving, curbs, sidewalks, interlock, retaining walls, parking lot resurfacing",
    "Road & Highway Construction": "roadworks, reconstruction of streets, intersections, road resurfacing programs",
    "Line Painting & Pavement Marking": "pavement markings, road line painting, sports field line marking",
    "Utilities & Streetlighting": "hydro, gas, streetlight and pole infrastructure",
    "Environmental Services & Remediation": "contaminated soil, spill response, environmental monitoring, waste diversion studies",
    "Engineering & Design Consulting": "engineering studies, detailed design, contract administration",
    "Vehicles & Heavy Equipment": "purchase or lease of vehicles, apparatus, machinery — supply only, not services",
    "Materials & Industrial Supplies": "supply of goods, parts, aggregate, chemicals, consumables",
    "Transportation & Transit Services": "student busing, transit operation, shuttle and passenger services",
    UNCLASSIFIED: "use ONLY when the title genuinely gives no usable signal",
}


def group_for(industry: str) -> str:
    """Return the roll-up group for an industry, defaulting to 'Other'."""
    return INDUSTRY_TO_GROUP.get(industry, "Other")


def normalize_industry(value: str | None) -> str:
    """
    Coerce a model-supplied industry onto the taxonomy.

    Exact match first, then case-insensitive match; anything unrecognised
    falls back to UNCLASSIFIED so the report never invents a category.
    """
    if not value:
        return UNCLASSIFIED
    value = value.strip()
    if value in INDUSTRY_TO_GROUP:
        return value
    lowered = value.lower()
    for industry in INDUSTRIES:
        if industry.lower() == lowered:
            return industry
    return UNCLASSIFIED


def taxonomy_prompt_block() -> str:
    """Render the taxonomy as the system-prompt category list."""
    lines: list[str] = []
    for group, industries in TAXONOMY.items():
        lines.append(f"{group}:")
        for industry in industries:
            hint = INDUSTRY_HINTS.get(industry)
            lines.append(f"  - {industry}" + (f"  ({hint})" if hint else ""))
        lines.append("")
    return "\n".join(lines).rstrip()
