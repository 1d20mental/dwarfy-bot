"""Mundane equipment costs used to resolve sheet Base Cost formulas."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR


@dataclass(frozen=True)
class BaseCostResolution:
    """Result of turning a sheet Base Cost cell into an integer gp value."""

    base_price: int | None
    detail: str = ""
    error: str = ""
    needs_variant: bool = False
    recognized: bool = False
    mundane_item_name: str = ""
    mundane_lookup_key: str = ""
    mundane_cost_gp: Decimal | None = None
    craft_cost_gp: Decimal | None = None
    craft_cost_dtp: int | None = None


@dataclass(frozen=True)
class MundaneItem:
    """One row from the Mundane Item Reference tab."""

    lookup_key: str
    item_name: str
    category: str = ""
    variant_group: str = ""
    cost_gp_raw: str = ""
    cost_gp: Decimal | None = None
    cost_mode: str = ""
    formula_surcharge_gp: Decimal | None = None
    cost_base_required: bool = False
    cost_base_group_required: str = ""
    eligible_as_magic_variant_base: bool = True


@dataclass(frozen=True)
class PricingTemplateRule:
    """One row from the Pricing Template Rules tab."""

    rule_key: str
    bot_item_name_pattern: str
    variant_required: bool = False
    allowed_variant_groups: str = ""
    cost_mode: str = ""
    magic_surcharge_gp: Decimal | None = None
    base_cost_formula: str = ""
    craft_gp_formula: str = ""
    craft_dtp_formula: str = ""
    display_name_rule: str = ""
    example_variant: str = ""
    notes: str = ""


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

SUGGESTED_AMMUNITION_VARIANTS = ("20 arrows", "20 bolts", "10 bullets")
SUGGESTED_WEAPON_VARIANTS = (
    "Longsword",
    "Rapier",
    "Shortsword",
    "Scimitar",
    "Battleaxe",
    "Warhammer",
    "Longbow",
    "Shortbow",
    "Light Crossbow",
    "Heavy Crossbow",
)
SUGGESTED_ARMOR_VARIANTS = (
    "Leather Armor",
    "Studded Leather Armor",
    "Chain Shirt",
    "Scale Mail",
    "Breastplate",
    "Half Plate",
    "Chain Mail",
    "Splint Armor",
    "Plate Armor",
)
SUGGESTED_SHIELD_VARIANTS = ("Shield",)

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


def normalize_lookup_key(value: object) -> str:
    """Normalize names/lookup keys for exact-ish sheet comparisons."""
    text = _clean(value).casefold()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


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


def _ceil_int(number: Decimal) -> int:
    return int(number.to_integral_value(rounding=ROUND_CEILING))


def decimal_from_cell(value: object) -> Decimal | None:
    """Parse a numeric sheet cell into Decimal, accepting gp text."""
    text = _clean(value)
    if not text:
        return None
    text = text.casefold().replace(",", "").replace("gp", "").strip()
    return _parse_decimal(text)


def bool_from_cell(value: object, *, default: bool = False) -> bool:
    text = _clean(value).casefold()
    if not text:
        return default
    return text in {"true", "yes", "y", "1"}


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


def suggested_variants_for_base_cost(value: object) -> tuple[str, ...]:
    """Return useful Discord autocomplete variants for a Base Cost formula."""
    suggestions: list[str] = []
    for group in base_cost_variant_groups(value):
        if group == "ammunition":
            suggestions.extend(SUGGESTED_AMMUNITION_VARIANTS)
        elif group == "shield":
            suggestions.extend(SUGGESTED_SHIELD_VARIANTS)
        elif group == "armor":
            suggestions.extend(SUGGESTED_ARMOR_VARIANTS)
        elif group == "weapon":
            suggestions.extend(SUGGESTED_WEAPON_VARIANTS)
    return tuple(dict.fromkeys(suggestions))


def _base_item_name(item_name: str) -> str:
    """Strip a final parenthetical like '(any)' for rule matching."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", _clean(item_name)).strip()


def pricing_rule_for_item(
    item_name: str,
    rules: list[PricingTemplateRule] | tuple[PricingTemplateRule, ...],
) -> PricingTemplateRule | None:
    """Find the best pricing template rule for a Bot Items row."""
    name = normalize_lookup_key(item_name)
    base_name = normalize_lookup_key(_base_item_name(item_name))
    exact_pattern: list[PricingTemplateRule] = []
    exact_key: list[PricingTemplateRule] = []
    base_matches: list[PricingTemplateRule] = []
    for rule in rules:
        pattern = normalize_lookup_key(rule.bot_item_name_pattern)
        key = normalize_lookup_key(rule.rule_key)
        if pattern and pattern == name:
            exact_pattern.append(rule)
        elif key and key == name:
            exact_key.append(rule)
        elif (pattern and pattern == base_name) or (key and key == base_name):
            base_matches.append(rule)
    for matches in (exact_pattern, exact_key, base_matches):
        if matches:
            return matches[-1]
    return None


def item_requires_template_variant(item: object, rules: list[PricingTemplateRule]) -> bool:
    """Return True if the sheet/rules mark a Bot Item as a template."""
    rule = pricing_rule_for_item(getattr(item, "name", ""), rules)
    if rule and rule.variant_required:
        return True
    variant_type = _clean(getattr(item, "variant_type", "")).casefold()
    if variant_type and variant_type != "specific item":
        return True
    if _clean(getattr(item, "variant_instructions", "")) or _clean(getattr(item, "variant_options", "")):
        return True
    return base_cost_requires_variant(getattr(item, "base_price_text", ""))


def _mundane_group_set(item: MundaneItem) -> set[str]:
    text = " ".join(
        (
            item.variant_group,
            item.category,
            item.lookup_key,
            item.item_name,
        )
    ).casefold()
    groups: set[str] = set()
    for group in ("weapon", "firearm", "ammunition", "armor", "shield", "barding", "general"):
        if group in text:
            groups.add(group)
    if "shield" in normalize_lookup_key(item.item_name):
        groups.add("shield")
    return groups


def _allowed_group_sets(rule: PricingTemplateRule | None, base_price_text: str) -> tuple[set[str], set[str], set[str]]:
    """Return allowed groups, excluded groups, and excluded normalized names."""
    raw = _clean(rule.allowed_variant_groups if rule else "").casefold()
    allowed: set[str] = set()
    excluded_groups: set[str] = set()
    excluded_names: set[str] = set()

    if "weapon_or_ammunition" in raw:
        allowed.update({"weapon", "firearm", "ammunition"})
    if "weapon" in raw:
        allowed.update({"weapon", "firearm"})
    if "firearm" in raw:
        allowed.add("firearm")
    if "ammunition" in raw:
        allowed.add("ammunition")
    if "armor" in raw:
        allowed.add("armor")
    if "shield" in raw:
        allowed.add("shield")
    if "general" in raw:
        allowed.add("general")

    if "no shield" in raw or "exclude shield" in raw or "armor only" in raw:
        excluded_groups.add("shield")
        allowed.discard("shield")
    if "exclude hide" in raw:
        excluded_names.add("hide armor")
        excluded_names.add("hide")

    if not allowed:
        for group in base_cost_variant_groups(base_price_text):
            if group == "weapon":
                allowed.update({"weapon", "firearm"})
            elif group == "armor":
                allowed.update({"armor", "shield"})
            else:
                allowed.add(group)

    return allowed, excluded_groups, excluded_names


def _is_variant_allowed(
    mundane: MundaneItem,
    *,
    rule: PricingTemplateRule | None,
    base_price_text: str,
) -> tuple[bool, str]:
    allowed, excluded_groups, excluded_names = _allowed_group_sets(rule, base_price_text)
    groups = _mundane_group_set(mundane)
    item_key = normalize_lookup_key(mundane.item_name)
    lookup_key = normalize_lookup_key(mundane.lookup_key)

    if item_key in excluded_names or lookup_key in excluded_names:
        return False, f"{mundane.item_name} is explicitly excluded for this template."
    if groups & excluded_groups:
        group_text = ", ".join(sorted(groups & excluded_groups))
        return False, f"{mundane.item_name} is a {group_text} variant, which this template excludes."
    if allowed and not (groups & allowed):
        return False, f"{mundane.item_name} is {mundane.variant_group or mundane.category}, but this template requires {', '.join(sorted(allowed))}."
    return True, ""


def find_mundane_variant(
    variant: str | None,
    mundane_items: list[MundaneItem] | tuple[MundaneItem, ...],
) -> tuple[MundaneItem | None, str]:
    """Resolve a user variant by exact Item Name or Lookup Key."""
    query = normalize_lookup_key(variant)
    if not query:
        return None, "A concrete variant is required."

    matches = [
        item
        for item in mundane_items
        if item.eligible_as_magic_variant_base
        and query in {normalize_lookup_key(item.item_name), normalize_lookup_key(item.lookup_key)}
    ]
    if not matches:
        return None, f"`{variant}` was not found in Mundane Item Reference."

    unique_keys = {(normalize_lookup_key(item.lookup_key), normalize_lookup_key(item.item_name)) for item in matches}
    if len(unique_keys) > 1:
        names = ", ".join(sorted(item.item_name for item in matches)[:8])
        return None, f"`{variant}` is ambiguous in Mundane Item Reference. Matching rows: {names}."

    return matches[0], ""


def _leading_surcharge(value: object) -> Decimal | None:
    text = _clean(value)
    match = LEADING_GP_RE.match(text)
    if match is None:
        return None
    return _parse_decimal(match.group("amount"))


def _craft_gp_from_text(value: str, total_base_cost: Decimal) -> Decimal | None:
    text = _clean(value).casefold()
    if not text:
        return None
    number = decimal_from_cell(text)
    if number is not None:
        return number
    if "total cost" in text and "/ 2" in text:
        return total_base_cost / Decimal(2)
    return None


def _craft_dtp_from_text(value: str, total_base_cost: Decimal) -> int | None:
    text = _clean(value).casefold()
    if not text:
        return None
    number = decimal_from_cell(text)
    if number is not None:
        return max(1, _ceil_int(number))
    if "total cost" in text and "/ 25" in text:
        return max(1, _ceil_int(total_base_cost / Decimal(25)))
    return None


def resolve_reference_base_cost(
    *,
    item: object,
    variant: str | None,
    mundane_items: list[MundaneItem] | tuple[MundaneItem, ...],
    pricing_rules: list[PricingTemplateRule] | tuple[PricingTemplateRule, ...],
) -> BaseCostResolution:
    """Resolve Dwarfy pricing using Bot Items plus the mundane reference tabs."""
    base_price_text = _clean(getattr(item, "base_price_text", ""))
    rule = pricing_rule_for_item(getattr(item, "name", ""), pricing_rules)
    cost_mode = _clean(rule.cost_mode if rule else "").upper()
    requires_variant = bool(rule and rule.variant_required) or base_cost_requires_variant(base_price_text)

    if not rule and not mundane_items:
        return resolve_base_cost(base_price_text, variant=variant)

    if not cost_mode:
        cost_mode = "ADD_MUNDANE_COST" if requires_variant else "FIXED_GP"

    if cost_mode == "FIXED_GP" and not requires_variant:
        return resolve_base_cost(base_price_text, variant=variant)

    if requires_variant and not variant:
        return BaseCostResolution(
            None,
            error="This template needs a concrete mundane variant before Dwarfy can price it.",
            needs_variant=True,
            recognized=True,
        )

    mundane, error = find_mundane_variant(variant, mundane_items)
    if mundane is None:
        return BaseCostResolution(None, error=error, needs_variant=True, recognized=True)

    allowed, allowed_error = _is_variant_allowed(mundane, rule=rule, base_price_text=base_price_text)
    if not allowed:
        return BaseCostResolution(None, error=allowed_error, needs_variant=True, recognized=True)

    if mundane.cost_gp is None:
        return BaseCostResolution(
            None,
            error=f"`{mundane.item_name}` does not have a numeric mundane cost.",
            needs_variant=True,
            recognized=True,
        )

    if cost_mode == "MULTIPLY_MUNDANE_COST":
        multiplier = Decimal(4)
        craft_multiplier = Decimal(2)
        total = mundane.cost_gp * multiplier
        craft_gp = mundane.cost_gp * craft_multiplier
        craft_dtp = max(1, _ceil_int(craft_gp / Decimal(25)))
        return BaseCostResolution(
            _floor_gp(total),
            detail=f"{mundane.item_name} {mundane.cost_gp:g}gp x {multiplier:g} = {_floor_gp(total)}gp",
            recognized=True,
            mundane_item_name=mundane.item_name,
            mundane_lookup_key=mundane.lookup_key,
            mundane_cost_gp=mundane.cost_gp,
            craft_cost_gp=craft_gp,
            craft_cost_dtp=craft_dtp,
        )

    surcharge = (rule.magic_surcharge_gp if rule and rule.magic_surcharge_gp is not None else None)
    if surcharge is None:
        surcharge = _leading_surcharge(base_price_text)
    if surcharge is None:
        return BaseCostResolution(
            None,
            error=f"Could not determine the magic surcharge from Base Cost {base_price_text!r}.",
            needs_variant=True,
            recognized=True,
        )

    total = surcharge + mundane.cost_gp
    craft_gp = _craft_gp_from_text(getattr(item, "craft_cost_gp_text", ""), total)
    if craft_gp is None and rule:
        craft_gp = _craft_gp_from_text(rule.craft_gp_formula, total)
    craft_dtp = _craft_dtp_from_text(getattr(item, "craft_cost_dtp_text", ""), total)
    if craft_dtp is None and rule:
        craft_dtp = _craft_dtp_from_text(rule.craft_dtp_formula, total)

    return BaseCostResolution(
        _floor_gp(total),
        detail=f"{surcharge:g}gp + {mundane.item_name} {mundane.cost_gp:g}gp = {_floor_gp(total)}gp",
        recognized=True,
        mundane_item_name=mundane.item_name,
        mundane_lookup_key=mundane.lookup_key,
        mundane_cost_gp=mundane.cost_gp,
        craft_cost_gp=craft_gp,
        craft_cost_dtp=craft_dtp,
    )


def suggested_variants_from_reference(
    *,
    item: object,
    mundane_items: list[MundaneItem] | tuple[MundaneItem, ...],
    pricing_rules: list[PricingTemplateRule] | tuple[PricingTemplateRule, ...],
    query: str = "",
    limit: int = 25,
) -> list[str]:
    """Suggest variant names from Mundane Item Reference for a Bot Item."""
    rule = pricing_rule_for_item(getattr(item, "name", ""), pricing_rules)
    allowed_groups, excluded_groups, excluded_names = _allowed_group_sets(
        rule,
        getattr(item, "base_price_text", ""),
    )
    query_norm = normalize_lookup_key(query)
    suggestions: list[str] = []
    seen: set[str] = set()
    for mundane in mundane_items:
        if not mundane.eligible_as_magic_variant_base:
            continue
        groups = _mundane_group_set(mundane)
        if groups & excluded_groups:
            continue
        if allowed_groups and not (groups & allowed_groups):
            continue
        if normalize_lookup_key(mundane.item_name) in excluded_names or normalize_lookup_key(mundane.lookup_key) in excluded_names:
            continue
        key = normalize_lookup_key(mundane.item_name)
        if not key or key in seen:
            continue
        if query_norm and query_norm not in key and query_norm not in normalize_lookup_key(mundane.lookup_key):
            continue
        seen.add(key)
        suggestions.append(mundane.item_name)
        if len(suggestions) >= limit:
            break
    return suggestions


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
