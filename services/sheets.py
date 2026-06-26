"""Google Sheets loading, normalization, and item lookup."""

from __future__ import annotations

import difflib
import random
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

import gspread
from google.oauth2.service_account import Credentials

from services.equipment import (
    MundaneItem,
    PricingTemplateRule,
    base_cost_has_price_signal,
    bool_from_cell,
    decimal_from_cell,
    parse_static_base_price,
    pricing_rule_for_item,
    resolve_reference_base_cost,
    suggested_variants_for_base_cost,
    suggested_variants_from_reference,
)


SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

RARITY_NAMES = {
    "common": "Common",
    "uncommon": "Uncommon",
    "rare": "Rare",
    "very rare": "Very Rare",
    "legendary": "Legendary",
    "artifact": "Artifact",
    "none": "None",
}

SUPPORTED_LOOT_TYPES = {"Item", "Monster Component"}
TRUE_TEXT = {"true", "yes", "y", "1"}
FALSE_TEXT = {"false", "no", "n", "0"}
BASE_PRICE_COLUMNS = ("Base Price", "Base Cost", "Item Base Price", "Dwarfy Base Price")


@dataclass(frozen=True)
class SheetItem:
    name: str
    rarity: str
    consumable: bool
    allowed: bool
    loot_type: str
    source: str
    category: str
    tags: tuple[str, ...]
    min_apl: int | None
    max_apl: int | None
    notes: str
    base_price: int | None = None
    base_price_text: str = ""
    roll_rarity: str = ""
    weight: int = 1
    session_eligible: bool = False
    creature_type: str = ""
    source_code: str = ""
    source_name: str = ""
    alternate_sources: str = ""
    dwarfy_sell_eligible: bool | None = None
    variant_type: str = ""
    variant_instructions: str = ""
    page: str = ""
    item_type: str = ""
    attunement: str = ""
    display_detail: str = ""
    short_description: str = ""
    rules_text: str = ""
    json_notes: str = ""
    item_tags: str = ""
    variant_options: str = ""
    json_source_key: str = ""
    json_match_status: str = ""
    power_band: str = ""
    tier: str = ""
    craft_cost_gp_text: str = ""
    craft_cost_dtp_text: str = ""
    bastion_facility: str = ""
    tool: str = ""
    consumable_1d3_roll: str = ""
    quantity_roll_recommendation: str = ""

    @property
    def tags_text(self) -> str:
        return ", ".join(self.tags)

    @property
    def variant_option_list(self) -> tuple[str, ...]:
        return tuple(option.strip() for option in self.variant_options.split(",") if option.strip())

    @property
    def source_with_page(self) -> str:
        if self.source and self.page:
            page = self.page.strip()
            page_text = page if page.casefold().startswith("p") else f"p. {page}"
            return f"{self.source}, {page_text}"
        return self.source or "Unknown"


@dataclass(frozen=True)
class MonsterComponent:
    creature_type: str
    roll: str
    component: str
    examples: str


@dataclass(frozen=True)
class ItemMatch:
    item: SheetItem | None
    choices: tuple[SheetItem, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class ComponentRoll:
    creature_type: str
    d100: int | None
    component: str
    examples: str
    note: str | None = None


def _clean(value: object) -> str:
    return str(value or "").strip()


def _header_key(value: object) -> str:
    return _clean(value).casefold()


def normalize_rarity(value: object) -> str:
    """Normalize common rarity casing while preserving unknown values."""
    text = " ".join(_clean(value).split())
    if not text:
        return ""
    return RARITY_NAMES.get(text.casefold(), text.title())


def parse_bool(value: object) -> bool:
    """Parse TRUE/FALSE-ish cells used by older required sheet columns."""
    text = _clean(value).casefold()
    return text in TRUE_TEXT


def parse_optional_bool(value: object) -> bool | None:
    """Parse a TRUE/FALSE cell and return None for blank or invalid text."""
    text = _clean(value).casefold()
    if not text:
        return None
    if text in TRUE_TEXT:
        return True
    if text in FALSE_TEXT:
        return False
    return None


def parse_optional_int(value: object) -> int | None:
    text = _clean(value)
    if not text:
        return None
    return int(float(text))


def parse_base_price(value: object, *, row_number: int, warnings: list[str]) -> int | None:
    """Parse the sheet Base Price column used by Dwarfy's shop economy."""
    text = _clean(value)
    if not text:
        return None

    plain_price = parse_static_base_price(text)
    if plain_price is not None:
        return plain_price

    # Formula values such as "400gp (plus cost of armor)" are resolved later,
    # once the command knows the concrete variant.
    if base_cost_has_price_signal(text):
        return None

    warnings.append(f"Bot Items row {row_number} has invalid Base Price {text!r}; Dwarfy pricing disabled.")
    return None


def parse_weight(value: object, *, row_number: int, warnings: list[str]) -> int:
    """Parse the Weight column.

    Bad weights warn and become 1 so a sheet typo never crashes reload.
    """
    text = _clean(value)
    if not text:
        return 1

    try:
        number = Decimal(text)
    except InvalidOperation:
        warnings.append(f"Bot Items row {row_number} has invalid Weight {text!r}; defaulted to 1.")
        return 1

    if number != number.to_integral_value() or number < 1:
        warnings.append(f"Bot Items row {row_number} has invalid Weight {text!r}; defaulted to 1.")
        return 1

    return int(number)


def parse_session_eligible(
    value: object,
    *,
    row_number: int,
    roll_rarity: str,
    header_exists: bool,
    warnings: list[str],
) -> bool:
    """Parse Session Eligible with the workbook's inference rules."""
    if not header_exists:
        return bool(roll_rarity)

    text = _clean(value)
    if not text:
        return bool(roll_rarity)

    parsed = parse_optional_bool(text)
    if parsed is None:
        warnings.append(
            f"Bot Items row {row_number} has invalid Session Eligible {text!r}; row is ineligible."
        )
        return False
    return parsed


def parse_dwarfy_sell_eligible(
    value: object,
    *,
    row_number: int,
    header_exists: bool,
    warnings: list[str],
) -> bool | None:
    """Parse optional Dwarfy Sell Eligible.

    None means the column/cell did not express an opinion.
    """
    if not header_exists:
        return None

    text = _clean(value)
    if not text:
        return None

    parsed = parse_optional_bool(text)
    if parsed is None:
        warnings.append(
            f"Bot Items row {row_number} has invalid Dwarfy Sell Eligible {text!r}; defaulted to FALSE."
        )
        return False
    return parsed


def normalize_loot_type(value: object) -> str:
    text = " ".join(_clean(value).split())
    if not text:
        return "Item"
    lookup = {name.casefold(): name for name in SUPPORTED_LOOT_TYPES}
    return lookup.get(text.casefold(), text.title())


def parse_tags(value: object) -> tuple[str, ...]:
    return tuple(
        tag.strip().casefold()
        for tag in _clean(value).split(",")
        if tag.strip()
    )


def infer_creature_type(item_name: str) -> str:
    """Extract a final parenthetical creature type from a trigger item name."""
    match = re.search(r"\(([^()]+)\)\s*$", item_name)
    return match.group(1).strip() if match else ""


def _header_map(headers: list[str]) -> dict[str, int]:
    return {_header_key(header): index for index, header in enumerate(headers)}


def _has_header(headers: dict[str, int], column: str) -> bool:
    return column.casefold() in headers


def _cell(row: list[str], headers: dict[str, int], column: str) -> str:
    index = headers.get(column.casefold())
    if index is None or index >= len(row):
        return ""
    return _clean(row[index])


def _first_cell(row: list[str], headers: dict[str, int], columns: tuple[str, ...]) -> str:
    for column in columns:
        value = _cell(row, headers, column)
        if value:
            return value
    return ""


def _first_header(headers: dict[str, int], columns: tuple[str, ...]) -> str | None:
    for column in columns:
        if _has_header(headers, column):
            return column
    return None


def _score_match(query: str, candidate: str) -> float:
    query_norm = query.casefold().strip()
    candidate_norm = candidate.casefold().strip()
    ratio = difflib.SequenceMatcher(None, query_norm, candidate_norm).ratio()
    if query_norm and query_norm in candidate_norm:
        ratio = max(ratio, 0.82)
    return ratio


def _critical_match_key(item: SheetItem) -> tuple[str, str, bool, bool, bool | None]:
    return (
        item.name.casefold().strip(),
        item.rarity,
        item.consumable,
        item.allowed,
        item.dwarfy_sell_eligible,
    )


def _sell_match_key(item: SheetItem) -> tuple[object, ...]:
    """Fields that must agree before duplicate sell rows collapse together."""
    return (
        item.name.casefold().strip(),
        item.rarity,
        item.source,
        item.consumable,
        item.allowed,
        item.dwarfy_sell_eligible,
        item.base_price,
        item.base_price_text.casefold().strip(),
        item.item_type,
        item.attunement,
        item.page,
        item.display_detail,
        item.short_description,
        item.rules_text,
        item.variant_type,
        item.variant_instructions,
        item.variant_options,
    )


def item_has_dwarfy_base_cost(item: SheetItem) -> bool:
    """Return True if the row has a numeric or formula Base Cost value."""
    return item.base_price is not None or base_cost_has_price_signal(item.base_price_text)


GENERIC_ITEM_NAMES = {
    "+1 weapon",
    "+2 weapon",
    "+3 weapon",
    "+1 armor",
    "+2 armor",
    "+3 armor",
    "+1 ammunition",
    "+2 ammunition",
    "+3 ammunition",
    "adamantine armor",
    "adamantine weapon",
    "armor of resistance",
    "ammunition of slaying",
    "enspelled weapon",
    "enspelled armor",
    "dragon slayer",
    "flame tongue",
    "frost brand",
    "holy avenger",
    "vorpal weapon",
}

GENERIC_NAME_MARKERS = (
    "(any)",
    "any weapon",
    "any armor",
    "any medium or heavy armor",
    "any melee weapon",
    "any ranged weapon",
)

PASTED_ITEM_MARKERS = (
    "requires attunement",
    "you gain",
    "while wearing",
    "dungeon master's guide",
    "pg.",
    "page",
)


def is_generic_template_item(item: SheetItem) -> bool:
    """Return True for sheet rows that need player/DM variant resolution."""
    variant_type = item.variant_type.casefold().strip()
    if variant_type and variant_type != "specific item":
        return True
    if item.variant_instructions.strip() or item.variant_options.strip():
        return True

    name_norm = item.name.casefold().strip()
    if name_norm in GENERIC_ITEM_NAMES:
        return True
    return any(marker in name_norm for marker in GENERIC_NAME_MARKERS)


def looks_like_pasted_item_text(text: str) -> bool:
    """Catch common cases where a player pasted a full item entry."""
    clean = _clean(text)
    if len(clean) > 100:
        return True
    lowered = clean.casefold()
    if any(marker in lowered for marker in PASTED_ITEM_MARKERS):
        return True

    # Two or more sentence periods is a good signal for pasted prose. A single
    # period can still appear in odd but valid names, so keep this conservative.
    return len(re.findall(r"\.\s+", clean)) >= 2 or clean.count(".") >= 3


def looks_like_pasted_detail_text(text: str) -> bool:
    """Reject full rules blocks in details while allowing short custom notes."""
    clean = _clean(text)
    lowered = clean.casefold()
    if len(clean) > 500:
        return True
    return any(marker in lowered for marker in PASTED_ITEM_MARKERS[:4])


def item_detail_summary(item: SheetItem) -> str:
    """Create a compact player-facing item detail line."""
    if item.display_detail:
        return item.display_detail

    parts = [item.rarity]
    if item.item_type:
        parts.append(item.item_type)
    summary = " ".join(part for part in parts if part).strip()
    if item.attunement:
        attunement = item.attunement.strip()
        if attunement.casefold() in TRUE_TEXT:
            summary = f"{summary}, requires attunement" if summary else "requires attunement"
        elif attunement.casefold() not in FALSE_TEXT:
            summary = f"{summary}, {attunement}" if summary else attunement
    return summary or item.rarity or "Magic item"


def _unique_items_by_name(items: Iterable[SheetItem]) -> tuple[SheetItem, ...]:
    seen: set[str] = set()
    unique: list[SheetItem] = []
    for item in items:
        key = item.name.casefold().strip()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return tuple(unique)


def _parse_roll_range(value: str) -> tuple[int, int]:
    text = value.strip().replace("\u2013", "-")
    if re.fullmatch(r"\d+", text):
        number = int(text)
        return number, number

    match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", text)
    if not match:
        raise ValueError(f"Could not parse roll value: {value}")

    low = int(match.group(1))
    high = int(match.group(2))
    if low > high:
        low, high = high, low
    return low, high


class SheetCache:
    """In-memory copy of the Google Sheet.

    The bot reads Sheets on startup and on /dwarfy reload, then all commands use
    this cache. That keeps player commands fast and avoids hammering Google.
    """

    def __init__(
        self,
        *,
        sheet_id: str,
        service_account_file: str,
        bot_items_tab: str,
        monster_components_tab: str,
    ) -> None:
        self.sheet_id = sheet_id
        self.service_account_file = service_account_file
        self.bot_items_tab = bot_items_tab
        self.monster_components_tab = monster_components_tab
        self.items: list[SheetItem] = []
        self.components: list[MonsterComponent] = []
        self.mundane_items: list[MundaneItem] = []
        self.pricing_rules: list[PricingTemplateRule] = []
        self.warnings: list[str] = []
        self.loaded = False

    def reload(self) -> None:
        """Load both configured runtime tabs from Google Sheets."""
        self.loaded = False
        self.items = []
        self.components = []
        self.mundane_items = []
        self.pricing_rules = []
        self.warnings = []

        if not self.sheet_id:
            raise RuntimeError("GOOGLE_SHEET_ID is not set.")

        credential_path = Path(self.service_account_file)
        if not credential_path.exists():
            raise RuntimeError(
                f"Google service account file was not found: {credential_path}"
            )

        credentials = Credentials.from_service_account_file(
            credential_path,
            scopes=SHEETS_SCOPES,
        )
        client = gspread.authorize(credentials)
        spreadsheet = client.open_by_key(self.sheet_id)

        self.items = self._load_bot_items(spreadsheet)
        self.components = self._load_monster_components(spreadsheet)
        self.mundane_items = self._load_mundane_items(spreadsheet)
        self.pricing_rules = self._load_pricing_template_rules(spreadsheet)
        self.loaded = True

    def _load_bot_items(self, spreadsheet) -> list[SheetItem]:
        values = spreadsheet.worksheet(self.bot_items_tab).get_all_values()
        if not values:
            self.warnings.append(f"{self.bot_items_tab} is empty.")
            return []

        headers = _header_map(values[0])
        expected = [
            "Item Name",
            "Rarity",
            "Roll Rarity",
            "Weight",
            "Consumable",
            "Allowed",
            "Loot Type",
            "Creature Type",
            "Source",
            "Source Code",
            "Source Name",
            "Category",
            "Tags",
            "Min APL",
            "Max APL",
            "Session Eligible",
        ]
        for column in expected:
            if column.casefold() not in headers:
                self.warnings.append(
                    f"{self.bot_items_tab} is missing expected column: {column}"
                )

        has_roll_rarity = _has_header(headers, "Roll Rarity")
        has_weight = _has_header(headers, "Weight")
        has_session_eligible = _has_header(headers, "Session Eligible")
        has_dwarfy_sell_eligible = _has_header(headers, "Dwarfy Sell Eligible")
        base_price_column = _first_header(headers, BASE_PRICE_COLUMNS)
        if base_price_column is None:
            self.warnings.append(
                f"{self.bot_items_tab} is missing Base Price. Dwarfy sell, broker, and owner stock will not suggest items."
            )

        items: list[SheetItem] = []
        for row_number, row in enumerate(values[1:], start=2):
            name = _cell(row, headers, "Item Name")
            if not name:
                continue

            try:
                min_apl = parse_optional_int(_cell(row, headers, "Min APL"))
                max_apl = parse_optional_int(_cell(row, headers, "Max APL"))
            except ValueError:
                self.warnings.append(
                    f"{self.bot_items_tab} row {row_number} has an invalid APL value."
                )
                min_apl = None
                max_apl = None

            loot_type = normalize_loot_type(_cell(row, headers, "Loot Type"))
            if loot_type not in SUPPORTED_LOOT_TYPES:
                self.warnings.append(
                    f"{self.bot_items_tab} row {row_number} has unsupported Loot Type: {loot_type}"
                )
                continue

            rarity = normalize_rarity(_cell(row, headers, "Rarity"))
            roll_rarity = (
                normalize_rarity(_cell(row, headers, "Roll Rarity"))
                if has_roll_rarity
                else rarity
            )
            creature_type = _cell(row, headers, "Creature Type")
            if loot_type == "Monster Component" and not creature_type:
                creature_type = infer_creature_type(name)

            weight = (
                parse_weight(_cell(row, headers, "Weight"), row_number=row_number, warnings=self.warnings)
                if has_weight
                else 1
            )
            base_price_text = _cell(row, headers, base_price_column) if base_price_column else ""
            base_price = (
                parse_base_price(base_price_text, row_number=row_number, warnings=self.warnings)
                if base_price_column
                else None
            )
            session_eligible = parse_session_eligible(
                _cell(row, headers, "Session Eligible"),
                row_number=row_number,
                roll_rarity=roll_rarity,
                header_exists=has_session_eligible,
                warnings=self.warnings,
            )
            dwarfy_sell_eligible = parse_dwarfy_sell_eligible(
                _cell(row, headers, "Dwarfy Sell Eligible"),
                row_number=row_number,
                header_exists=has_dwarfy_sell_eligible,
                warnings=self.warnings,
            )

            items.append(
                SheetItem(
                    name=name,
                    rarity=rarity,
                    roll_rarity=roll_rarity,
                    weight=weight,
                    consumable=parse_bool(_cell(row, headers, "Consumable")),
                    allowed=parse_bool(_cell(row, headers, "Allowed")),
                    loot_type=loot_type,
                    creature_type=creature_type,
                    source=_cell(row, headers, "Source"),
                    source_code=_cell(row, headers, "Source Code"),
                    source_name=_cell(row, headers, "Source Name"),
                    alternate_sources=_cell(row, headers, "Alternate Sources"),
                    category=_cell(row, headers, "Category"),
                    tags=parse_tags(_cell(row, headers, "Tags")),
                    min_apl=min_apl,
                    max_apl=max_apl,
                    session_eligible=session_eligible,
                    dwarfy_sell_eligible=dwarfy_sell_eligible,
                    base_price=base_price,
                    base_price_text=base_price_text,
                    variant_type=_cell(row, headers, "Variant Type"),
                    variant_instructions=_cell(row, headers, "Variant Instructions"),
                    page=_cell(row, headers, "Page"),
                    item_type=_cell(row, headers, "Item Type"),
                    attunement=_cell(row, headers, "Attunement"),
                    display_detail=_cell(row, headers, "Display Detail"),
                    short_description=_cell(row, headers, "Short Description"),
                    rules_text=_cell(row, headers, "Rules Text"),
                    json_notes=_cell(row, headers, "JSON Notes"),
                    item_tags=_cell(row, headers, "Item Tags"),
                    variant_options=_cell(row, headers, "Variant Options"),
                    json_source_key=_cell(row, headers, "JSON Source Key"),
                    json_match_status=_cell(row, headers, "JSON Match Status"),
                    power_band=_cell(row, headers, "Power Band"),
                    tier=_cell(row, headers, "Tier"),
                    craft_cost_gp_text=_cell(row, headers, "Craft Cost GP"),
                    craft_cost_dtp_text=_cell(row, headers, "Craft Cost DTP"),
                    bastion_facility=_cell(row, headers, "Bastion Facility"),
                    tool=_cell(row, headers, "Tool"),
                    consumable_1d3_roll=_cell(row, headers, "Consumable 1d3 Roll"),
                    quantity_roll_recommendation=_cell(row, headers, "Quantity Roll Recommendation"),
                    notes=_cell(row, headers, "Notes"),
                )
            )

        return items

    def _load_monster_components(self, spreadsheet) -> list[MonsterComponent]:
        values = spreadsheet.worksheet(self.monster_components_tab).get_all_values()
        if not values:
            self.warnings.append(f"{self.monster_components_tab} is empty.")
            return []

        headers = _header_map(values[0])
        expected = ["Creature Type", "Roll", "Component", "Examples"]
        for column in expected:
            if column.casefold() not in headers:
                self.warnings.append(
                    f"{self.monster_components_tab} is missing expected column: {column}"
                )

        components: list[MonsterComponent] = []
        for row in values[1:]:
            creature_type = _cell(row, headers, "Creature Type")
            component = _cell(row, headers, "Component")
            if not creature_type or not component:
                continue
            components.append(
                MonsterComponent(
                    creature_type=creature_type,
                    roll=_cell(row, headers, "Roll"),
                    component=component,
                    examples=_cell(row, headers, "Examples"),
                )
            )
        return components

    def _load_mundane_items(self, spreadsheet) -> list[MundaneItem]:
        """Load optional Mundane Item Reference rows for template pricing."""
        try:
            values = spreadsheet.worksheet("Mundane Item Reference").get_all_values()
        except gspread.exceptions.WorksheetNotFound:
            return []
        if not values:
            return []

        headers = _header_map(values[0])
        items: list[MundaneItem] = []
        for row in values[1:]:
            lookup_key = _cell(row, headers, "Lookup Key")
            item_name = _cell(row, headers, "Item Name")
            if not lookup_key and not item_name:
                continue
            items.append(
                MundaneItem(
                    lookup_key=lookup_key,
                    item_name=item_name,
                    category=_cell(row, headers, "Category"),
                    variant_group=_cell(row, headers, "Variant Group"),
                    cost_gp_raw=_cell(row, headers, "Cost GP Raw"),
                    cost_gp=decimal_from_cell(_cell(row, headers, "Cost GP Numeric")),
                    cost_mode=_cell(row, headers, "Cost Mode"),
                    formula_surcharge_gp=decimal_from_cell(_cell(row, headers, "Formula Surcharge GP")),
                    cost_base_required=bool_from_cell(_cell(row, headers, "Cost Base Required")),
                    cost_base_group_required=_cell(row, headers, "Cost Base Group Required"),
                    eligible_as_magic_variant_base=bool_from_cell(
                        _cell(row, headers, "Eligible as Magic Variant Base"),
                        default=True,
                    ),
                    attack_type=_first_cell(row, headers, ("Attack Type", "Weapon Range", "Range Type")),
                    damage_type=_first_cell(row, headers, ("Damage Type", "Damage")),
                    properties=_first_cell(row, headers, ("Properties", "Weapon Properties")),
                )
            )
        return items

    def _load_pricing_template_rules(self, spreadsheet) -> list[PricingTemplateRule]:
        """Load optional Pricing Template Rules rows for template pricing."""
        try:
            values = spreadsheet.worksheet("Pricing Template Rules").get_all_values()
        except gspread.exceptions.WorksheetNotFound:
            return []
        if not values:
            return []

        headers = _header_map(values[0])
        rules: list[PricingTemplateRule] = []
        for row in values[1:]:
            rule_key = _cell(row, headers, "Rule Key")
            pattern = _cell(row, headers, "Bot Item Name Pattern")
            if not rule_key and not pattern:
                continue
            rules.append(
                PricingTemplateRule(
                    rule_key=rule_key,
                    bot_item_name_pattern=pattern,
                    variant_required=bool_from_cell(_cell(row, headers, "Variant Required")),
                    allowed_variant_groups=_cell(row, headers, "Allowed Variant Groups"),
                    cost_mode=_cell(row, headers, "Cost Mode"),
                    magic_surcharge_gp=decimal_from_cell(_cell(row, headers, "Magic Surcharge GP")),
                    base_cost_formula=_cell(row, headers, "Base Cost Formula"),
                    craft_gp_formula=_cell(row, headers, "Craft GP Formula"),
                    craft_dtp_formula=_cell(row, headers, "Craft DTP Formula"),
                    display_name_rule=_cell(row, headers, "Display Name Rule"),
                    example_variant=_cell(row, headers, "Example Variant"),
                    notes=_cell(row, headers, "Notes"),
                )
            )
        return rules

    def _sell_preference_score(self, item: SheetItem) -> int:
        score = 0
        if self.item_has_dwarfy_pricing(item):
            score += 8
        if item.allowed:
            score += 4
        if not item.consumable:
            score += 2
        if item.dwarfy_sell_eligible is not False:
            score += 1
        return score

    def _canonical_exact_match(self, query_norm: str, *, for_sell: bool = False) -> ItemMatch:
        exact_matches = [
            item for item in self.items if item.name.casefold().strip() == query_norm
        ]
        if not exact_matches:
            return ItemMatch(item=None)

        candidates = exact_matches
        if for_sell:
            best_score = max(self._sell_preference_score(item) for item in exact_matches)
            candidates = [
                item for item in exact_matches if self._sell_preference_score(item) == best_score
            ]
            critical_keys = {_sell_match_key(item) for item in candidates}
            conflict_fields = (
                "Rarity, Source, item detail, Consumable, Allowed, "
                "Dwarfy Sell Eligible, or variant data"
            )
        else:
            critical_keys = {_critical_match_key(item) for item in candidates}
            conflict_fields = "Rarity, Consumable, Allowed, and Dwarfy Sell Eligible"

        if len(critical_keys) > 1:
            display_name = exact_matches[0].name
            return ItemMatch(
                item=None,
                message=(
                    f"Multiple Bot Items rows named `{display_name}` conflict on Dwarfy-critical data. "
                    f"Ask a maintainer to align {conflict_fields} before selling it."
                ),
            )

        return ItemMatch(item=candidates[0])

    def match_item(self, query: str, *, for_sell: bool = False) -> ItemMatch:
        """Find an item by exact match first, then by difflib fuzzy matching."""
        query_norm = query.casefold().strip()
        exact = self._canonical_exact_match(query_norm, for_sell=for_sell)
        if exact.item is not None or exact.message:
            return exact

        best_by_name: dict[str, tuple[float, SheetItem]] = {}
        for item in self.items:
            if for_sell:
                if item.consumable:
                    continue
                if not self.item_has_dwarfy_pricing(item):
                    continue
            score = _score_match(query, item.name)
            if score < 0.58:
                continue
            key = item.name.casefold().strip()
            if key not in best_by_name or score > best_by_name[key][0]:
                best_by_name[key] = (score, item)

        scored = sorted(best_by_name.values(), key=lambda pair: pair[0], reverse=True)
        if not scored:
            return ItemMatch(item=None, message="No matching item was found.")

        top_score, top_item = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0

        if top_score >= 0.86 and top_score - second_score >= 0.08:
            return self._canonical_exact_match(top_item.name.casefold().strip(), for_sell=for_sell)
        if len(scored) == 1 and top_score >= 0.65:
            return self._canonical_exact_match(top_item.name.casefold().strip(), for_sell=for_sell)

        choices = _unique_items_by_name(item for _score, item in scored[:8])
        return ItemMatch(
            item=None,
            choices=choices,
            message="Multiple possible item matches were found.",
        )

    def autocomplete_sell_item_names(self, query: str, *, limit: int = 25) -> list[str]:
        """Return unique clean names suitable for Discord item autocomplete."""
        query_norm = query.casefold().strip()
        seen: set[str] = set()
        starts: list[str] = []
        contains: list[str] = []

        for item in self.items:
            if not item.allowed or item.consumable or item.dwarfy_sell_eligible is False:
                continue
            if not self.item_has_dwarfy_pricing(item):
                continue
            key = item.name.casefold().strip()
            if key in seen:
                continue
            if query_norm and query_norm not in key:
                continue
            seen.add(key)
            if query_norm and key.startswith(query_norm):
                starts.append(item.name)
            else:
                contains.append(item.name)

        return (starts + contains)[:limit]

    def autocomplete_variant_options(
        self,
        *,
        item_name: str,
        query: str,
        limit: int = 25,
        for_sell: bool = True,
    ) -> list[str]:
        """Return suggested variants for the selected item.

        Duplicate sheet rows often share a clean item name across APL or roll
        bands, so collect options by exact item name instead of relying on one
        canonical match.
        """
        item_norm = item_name.casefold().strip()
        if not item_norm:
            return []

        candidates = [
            item
            for item in self.items
            if item.name.casefold().strip() == item_norm
        ]
        if not candidates:
            match = self.match_item(item_name, for_sell=for_sell)
            candidates = [match.item] if match.item is not None else []

        query_norm = query.casefold().strip()
        options: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            if item is None or not item.allowed or item.loot_type != "Item":
                continue
            if for_sell:
                if item.consumable or item.dwarfy_sell_eligible is False or not self.item_has_dwarfy_pricing(item):
                    continue
            reference_options = tuple(
                suggested_variants_from_reference(
                    item=item,
                    mundane_items=self.mundane_items,
                    pricing_rules=self.pricing_rules,
                    query=query,
                    limit=limit,
                )
            )
            base_cost_options = () if self.mundane_items else suggested_variants_for_base_cost(item.base_price_text)
            for option in item.variant_option_list + reference_options + base_cost_options:
                key = option.casefold().strip()
                if not key or key in seen:
                    continue
                if query_norm and query_norm not in key:
                    continue
                seen.add(key)
                options.append(option)
        return options[:limit]

    def pricing_rule_for_item(self, item: SheetItem) -> PricingTemplateRule | None:
        return pricing_rule_for_item(item.name, self.pricing_rules)

    def item_has_dwarfy_pricing(self, item: SheetItem) -> bool:
        return item_has_dwarfy_base_cost(item) or self.pricing_rule_for_item(item) is not None

    def item_requires_pricing_variant(self, item: SheetItem) -> bool:
        rule = self.pricing_rule_for_item(item)
        return bool(rule and rule.variant_required)

    def resolve_base_cost_for_item(self, item: SheetItem, variant: str | None = None):
        return resolve_reference_base_cost(
            item=item,
            variant=variant,
            mundane_items=self.mundane_items,
            pricing_rules=self.pricing_rules,
        )

    def loot_pool(self, *, rarity: str, consumable: bool, apl: int) -> list[SheetItem]:
        """Return session-eligible items matching roll rarity, slot type, and APL."""
        pool: list[SheetItem] = []
        for item in self.items:
            if not item.allowed:
                continue
            if not item.session_eligible:
                continue
            if not item.roll_rarity:
                continue
            if item.consumable != consumable:
                continue
            if item.roll_rarity != rarity:
                continue
            if item.min_apl is not None and item.min_apl > apl:
                continue
            if item.max_apl is not None and item.max_apl < apl:
                continue
            pool.append(item)
        return pool

    def available_creature_types(self) -> list[str]:
        types = sorted({component.creature_type for component in self.components})
        return types

    def has_creature_type(self, creature_type: str) -> bool:
        wanted = creature_type.casefold().strip()
        return any(
            component.creature_type.casefold().strip() == wanted
            for component in self.components
        )

    def _components_for_type(self, creature_type: str) -> list[MonsterComponent]:
        wanted = creature_type.casefold().strip()
        return [
            component
            for component in self.components
            if component.creature_type.casefold().strip() == wanted
        ]

    def roll_monster_component(self, creature_type: str | None = None) -> ComponentRoll:
        """Roll one monster component for the requested or random creature type."""
        creature_types = self.available_creature_types()
        if not creature_types:
            raise RuntimeError("Monster Components sheet data is not loaded.")

        chosen_type = creature_type.strip() if creature_type else random.choice(creature_types)
        rows = self._components_for_type(chosen_type)
        if not rows:
            raise ValueError(f"Unknown creature type: {chosen_type}")

        try:
            parsed = [(_parse_roll_range(row.roll), row) for row in rows]
        except ValueError:
            row = random.choice(rows)
            d100 = random.randint(1, 100)
            return ComponentRoll(
                creature_type=row.creature_type,
                d100=d100,
                component=row.component,
                examples=row.examples,
                note=(
                    f"The {row.creature_type} component roll table has an unreadable "
                    "Roll cell, so a random row was used."
                ),
            )

        d100 = random.randint(1, 100)
        for (low, high), row in parsed:
            if low <= d100 <= high:
                return ComponentRoll(
                    creature_type=row.creature_type,
                    d100=d100,
                    component=row.component,
                    examples=row.examples,
                )

        row = random.choice(rows)
        return ComponentRoll(
            creature_type=row.creature_type,
            d100=d100,
            component=row.component,
            examples=row.examples,
            note=(
                f"No {row.creature_type} component row matched roll {d100}, "
                "so a random row was used."
            ),
        )


def format_item_choices(items: Iterable[SheetItem]) -> str:
    """Return a short unique list of item names for fuzzy-match errors."""
    return "\n".join(
        f"- {item.name} ({item.rarity}, {item.source or 'No source'})"
        for item in _unique_items_by_name(items)
    )
