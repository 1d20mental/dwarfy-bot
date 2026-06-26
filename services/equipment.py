"""Mundane equipment costs used to resolve sheet Base Cost formulas."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR


@dataclass(frozen=True)
class BaseCostResolution:
    """Result of turning a sheet Base Cost cell into an integer gp value."""

    base_price: int | None
    detail: str = ""
    error: str = ""
    needs_variant: bool = False
    recognized: bool = False


ARMOR_COSTS_GP = {
    "padded": Decimal("5"),
    "leather": Decimal("10"),
    "studded leather": Decimal("45"),
    "hide": Decimal("10"),
    "chain shirt": Decimal("50"),
    "scale mail": Decimal("50"),
    "breastplate": Decimal("400"),
    "half plate": Decimal("750"),
    "ring mail": Decimal("30"),
    "chain mail": Decimal("75"),
    "splint": Decimal("200"),
    "plate": Decimal("1500"),
    "shield": Decimal("10"),
}

WEAPON_COSTS_GP = {
    "club": Decimal("0.1"),
    "dagger": Decimal("2"),
    "greatclub": Decimal("0.2"),
    "handaxe": Decimal("5"),
    "javelin": Decimal("0.5"),
    "light hammer": Decimal("2"),
    "mace": Decimal("5"),
    "quarterstaff": Decimal("0.2"),
    "sickle": Decimal("1"),
    "spear": Decimal("1"),
    "light crossbow": Decimal("25"),
    "dart": Decimal("0.05"),
    "shortbow": Decimal("25"),
    "sling": Decimal("0.1"),
    "battleaxe": Decimal("10"),
    "flail": Decimal("10"),
    "glaive": Decimal("20"),
    "greataxe": Decimal("30"),
    "greatsword": Decimal("50"),
    "halberd": Decimal("20"),
    "lance": Decimal("10"),
    "longsword": Decimal("15"),
    "maul": Decimal("10"),
    "morningstar": Decimal("15"),
    "pike": Decimal("5"),
    "rapier": Decimal("25"),
    "scimitar": Decimal("25"),
    "shortsword": Decimal("10"),
    "trident": Decimal("5"),
    "war pick": Decimal("5"),
    "warhammer": Decimal("15"),
    "whip": Decimal("2"),
    "blowgun": Decimal("10"),
    "hand crossbow": Decimal("75"),
    "heavy crossbow": Decimal("50"),
    "longbow": Decimal("50"),
    "net": Decimal("1"),
}

AMMUNITION_COSTS_GP = {
    "arrow": Decimal("0.05"),  # 1gp per 20
    "bolt": Decimal("0.05"),  # 1gp per 20
    "bullet": Decimal("0.002"),  # 4cp per 20 sling bullets
    "needle": Decimal("0.04"),  # 1gp per 25 blowgun needles
}

GEAR_COSTS_GP = {
    "arcane focus": Decimal("10"),
    "crystal": Decimal("10"),
    "orb": Decimal("20"),
    "rod": Decimal("10"),
    "staff": Decimal("5"),
    "wand": Decimal("10"),
    "druidic focus": Decimal("1"),
    "sprig of mistletoe": Decimal("1"),
    "totem": Decimal("1"),
    "wooden staff": Decimal("5"),
    "yew wand": Decimal("10"),
    "holy symbol": Decimal("5"),
    "amulet": Decimal("5"),
    "emblem": Decimal("5"),
    "reliquary": Decimal("5"),
    "spellbook": Decimal("50"),
    "book": Decimal("25"),
    "common clothes": Decimal("0.5"),
    "costume clothes": Decimal("5"),
    "fine clothes": Decimal("15"),
    "traveler's clothes": Decimal("2"),
    "travellers clothes": Decimal("2"),
    "bagpipes": Decimal("30"),
    "drum": Decimal("6"),
    "dulcimer": Decimal("25"),
    "flute": Decimal("2"),
    "lute": Decimal("35"),
    "lyre": Decimal("30"),
    "horn": Decimal("3"),
    "pan flute": Decimal("12"),
    "shawm": Decimal("2"),
    "viol": Decimal("30"),
}

GROUP_COSTS = {
    "armor": ARMOR_COSTS_GP,
    "shield": {"shield": Decimal("10")},
    "weapon": WEAPON_COSTS_GP,
    "ammunition": AMMUNITION_COSTS_GP,
    "gear": GEAR_COSTS_GP,
    "focus": GEAR_COSTS_GP,
    "instrument": GEAR_COSTS_GP,
}

LEADING_GP_RE = re.compile(
    r"^\s*(?P<amount>\d[\d,]*(?:\.\d+)?)\s*(?:gp|gold)?\b(?P<rest>.*)$",
    re.IGNORECASE,
)
STATIC_GP_RE = re.compile(
    r"^\s*(?P<amount>\d[\d,]*(?:\.\d+)?)\s*(?:gp|gold)?\s*$",
    re.IGNORECASE,
)
FIXED_ADDON_RE = re.compile(
    r"(?:plus\s+|add\s+|in\s+addition\s+to\s+)?(?P<amount>\d[\d,]*(?:\.\d+)?)\s*(?:gp|gold)\b",
    re.IGNORECASE,
)
QUANTITY_RE = re.compile(r"\b(?P<count>\d+)\s*(?:x|pieces?\s+of|arrows?|bolts?|bullets?|needles?)\b", re.IGNORECASE)


def _clean(value: object) -> str:
    return str(value or "").strip()


def _parse_decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", "").strip())
    except InvalidOperation:
        return None


def _whole_positive_gp(number: Decimal | None) -> int | None:
    if number is None or number != number.to_integral_value() or number < 1:
        return None
    return int(number)


def _floor_gp(number: Decimal) -> int:
    return int(number.to_integral_value(rounding=ROUND_FLOOR))


def parse_static_base_price(value: object) -> int | None:
    """Parse a plain whole-gp Base Price value."""
    text = _clean(value)
    if not text:
        return None
    match = STATIC_GP_RE.match(text)
    if match is None:
        return None
    return _whole_positive_gp(_parse_decimal(match.group("amount")))


def base_cost_has_price_signal(value: object) -> bool:
    """Return True when a cell has a positive leading gp amount.

    This includes formulas such as ``400gp (plus cost of armor)``. It is used
    for autocomplete so formula-priced items are not treated as blank-price
    rows.
    """
    text = _clean(value)
    if not text:
        return False
    if parse_static_base_price(text) is not None:
        return True
    match = LEADING_GP_RE.match(text)
    if match is None:
        return False
    return _whole_positive_gp(_parse_decimal(match.group("amount"))) is not None


def _addon_text(rest: str) -> str:
    text = " ".join(rest.strip().split())
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text


def _addon_norm(value: str) -> str:
    text = value.casefold()
    text = text.replace("+", " plus ")
    text = re.sub(r"[^a-z0-9.' ]+", " ", text)
    return " ".join(text.split())


def _required_groups(addon: str) -> tuple[str, ...]:
    text = _addon_norm(addon)
    groups: list[str] = []
    if "shield" in text:
        groups.append("shield")
    if "armor" in text or "armour" in text:
        groups.append("armor")
    if "weapon" in text:
        groups.append("weapon")
    if "ammunition" in text or "arrow" in text or "bolt" in text or "bullet" in text or "needle" in text:
        groups.append("ammunition")
    if "focus" in text or "holy symbol" in text:
        groups.append("focus")
    if "instrument" in text:
        groups.append("instrument")
    if "clothes" in text or "gear" in text or "item" in text:
        groups.append("gear")

    # If the sheet only says "plus cost of base item", try all known mundane
    # groups instead of rejecting a useful row.
    if not groups and "cost" in text:
        groups.extend(["armor", "weapon", "ammunition", "gear"])

    seen: set[str] = set()
    unique: list[str] = []
    for group in groups:
        if group not in seen:
            seen.add(group)
            unique.append(group)
    return tuple(unique)


def base_cost_variant_groups(value: object) -> tuple[str, ...]:
    """Return mundane equipment groups needed by a Base Cost formula."""
    text = _clean(value)
    match = LEADING_GP_RE.match(text)
    if match is None:
        return ()
    addon = _addon_text(match.group("rest"))
    if not addon or FIXED_ADDON_RE.search(addon):
        return ()
    return _required_groups(addon)


def base_cost_requires_variant(value: object) -> bool:
    return bool(base_cost_variant_groups(value))


def _extract_quantity(text: str) -> int | None:
    match = QUANTITY_RE.search(text)
    if match is None:
        return None
    try:
        return max(1, int(match.group("count")))
    except ValueError:
        return None


def _normalize_equipment_text(value: str) -> str:
    text = value.casefold()
    text = text.replace("'", "")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\b\d+\b", " ", text)
    return " ".join(text.split())


def _equipment_key_candidates(variant: str) -> list[str]:
    text = _normalize_equipment_text(variant)
    candidates = [text]

    replacements = {
        "studded leather armor": "studded leather",
        "leather armor": "leather",
        "hide armor": "hide",
        "scale mail armor": "scale mail",
        "half plate armor": "half plate",
        "plate armor": "plate",
        "splint armor": "splint",
        "traveler clothes": "travelers clothes",
        "travellers clothes": "travelers clothes",
        "crossbow bolts": "bolt",
        "crossbow bolt": "bolt",
        "sling bullets": "bullet",
        "sling bullet": "bullet",
        "blowgun needles": "needle",
        "blowgun needle": "needle",
        "arrows": "arrow",
        "arrow": "arrow",
        "bolts": "bolt",
        "bolt": "bolt",
        "bullets": "bullet",
        "bullet": "bullet",
        "needles": "needle",
        "needle": "needle",
    }
    if text in replacements:
        candidates.append(replacements[text])

    for word, key in (
        ("arrow", "arrow"),
        ("bolt", "bolt"),
        ("bullet", "bullet"),
        ("needle", "needle"),
        ("half plate", "half plate"),
        ("studded leather", "studded leather"),
        ("chain shirt", "chain shirt"),
        ("scale mail", "scale mail"),
        ("ring mail", "ring mail"),
        ("chain mail", "chain mail"),
        ("breastplate", "breastplate"),
        ("plate", "plate"),
        ("shield", "shield"),
    ):
        if word in text:
            candidates.append(key)

    return list(dict.fromkeys(candidates))


def _lookup_equipment_cost(
    variant: str,
    groups: tuple[str, ...],
    *,
    quantity_override: int | None,
) -> tuple[Decimal | None, str | None]:
    variant_quantity = _extract_quantity(variant)
    quantity = quantity_override or variant_quantity or 1
    candidates = _equipment_key_candidates(variant)

    for group in groups:
        costs = GROUP_COSTS.get(group, {})
        for candidate in candidates:
            if candidate in costs:
                cost = costs[candidate]
                if group == "ammunition":
                    cost *= Decimal(quantity)
                return cost, candidate
    return None, None


def resolve_base_cost(value: object, *, variant: str | None = None) -> BaseCostResolution:
    """Resolve a Base Cost cell to whole gp.

    Plain numeric cells resolve immediately. Formula cells use the submitted
    variant to add mundane equipment cost.
    """
    text = _clean(value)
    if not text:
        return BaseCostResolution(None, error="Base Cost is blank.")

    static_price = parse_static_base_price(text)
    if static_price is not None:
        return BaseCostResolution(static_price, detail=f"{static_price}gp", recognized=True)

    match = LEADING_GP_RE.match(text)
    if match is None:
        return BaseCostResolution(None, error=f"Base Cost {text!r} is not a recognized gp value.")

    base = _whole_positive_gp(_parse_decimal(match.group("amount")))
    if base is None:
        return BaseCostResolution(None, error=f"Base Cost {text!r} does not start with a positive whole gp value.")

    addon = _addon_text(match.group("rest"))
    if not addon:
        return BaseCostResolution(base, detail=f"{base}gp", recognized=True)

    fixed = FIXED_ADDON_RE.search(addon)
    if fixed is not None and "cost" not in _addon_norm(addon):
        amount = _parse_decimal(fixed.group("amount"))
        if amount is None:
            return BaseCostResolution(None, error=f"Could not parse fixed add-on in {text!r}.", recognized=True)
        total = base + _floor_gp(amount)
        return BaseCostResolution(
            total,
            detail=f"{base}gp + {_floor_gp(amount)}gp = {total}gp",
            recognized=True,
        )

    groups = _required_groups(addon)
    if not groups:
        return BaseCostResolution(
            None,
            error=f"Could not tell what mundane cost to add from {addon!r}.",
            recognized=True,
        )
    if not variant:
        return BaseCostResolution(
            None,
            error="This Base Cost needs a concrete variant before Dwarfy can price it.",
            needs_variant=True,
            recognized=True,
        )

    quantity_override = _extract_quantity(addon)
    equipment_cost, equipment_key = _lookup_equipment_cost(
        variant,
        groups,
        quantity_override=quantity_override,
    )
    if equipment_cost is None or equipment_key is None:
        group_text = ", ".join(groups)
        return BaseCostResolution(
            None,
            error=f"Dwarfy does not know the mundane {group_text} cost for variant {variant!r}.",
            needs_variant=True,
            recognized=True,
        )

    equipment_gp = _floor_gp(equipment_cost)
    total = base + equipment_gp
    return BaseCostResolution(
        total,
        detail=f"{base}gp + {variant} {equipment_gp}gp = {total}gp",
        recognized=True,
    )
