"""Google Sheets loading, normalization, and item lookup."""

from __future__ import annotations

import difflib
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import gspread
from google.oauth2.service_account import Credentials


SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

RARITY_NAMES = {
    "common": "Common",
    "uncommon": "Uncommon",
    "rare": "Rare",
    "very rare": "Very Rare",
    "legendary": "Legendary",
    "artifact": "Artifact",
}

SUPPORTED_LOOT_TYPES = {"Item", "Monster Component"}


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

    @property
    def tags_text(self) -> str:
        return ", ".join(self.tags)


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
    """Parse the TRUE/FALSE style cells used by the sheet."""
    text = _clean(value).casefold()
    return text in {"true", "yes", "y", "1"}


def parse_optional_int(value: object) -> int | None:
    text = _clean(value)
    if not text:
        return None
    return int(float(text))


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


def _header_map(headers: list[str]) -> dict[str, int]:
    return {_header_key(header): index for index, header in enumerate(headers)}


def _cell(row: list[str], headers: dict[str, int], column: str) -> str:
    index = headers.get(column.casefold())
    if index is None or index >= len(row):
        return ""
    return _clean(row[index])


def _score_match(query: str, candidate: str) -> float:
    query_norm = query.casefold().strip()
    candidate_norm = candidate.casefold().strip()
    ratio = difflib.SequenceMatcher(None, query_norm, candidate_norm).ratio()
    if query_norm and query_norm in candidate_norm:
        ratio = max(ratio, 0.82)
    return ratio


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
        self.warnings: list[str] = []
        self.loaded = False

    def reload(self) -> None:
        """Load both configured tabs from Google Sheets."""
        self.loaded = False
        self.items = []
        self.components = []
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
            "Consumable",
            "Allowed",
            "Loot Type",
            "Source",
            "Category",
            "Tags",
            "Min APL",
            "Max APL",
            "Notes",
        ]
        for column in expected:
            if column.casefold() not in headers:
                self.warnings.append(
                    f"{self.bot_items_tab} is missing expected column: {column}"
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

            items.append(
                SheetItem(
                    name=name,
                    rarity=normalize_rarity(_cell(row, headers, "Rarity")),
                    consumable=parse_bool(_cell(row, headers, "Consumable")),
                    allowed=parse_bool(_cell(row, headers, "Allowed")),
                    loot_type=loot_type,
                    source=_cell(row, headers, "Source"),
                    category=_cell(row, headers, "Category"),
                    tags=parse_tags(_cell(row, headers, "Tags")),
                    min_apl=min_apl,
                    max_apl=max_apl,
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

    def match_item(self, query: str) -> ItemMatch:
        """Find an item by exact match first, then by difflib fuzzy matching."""
        query_norm = query.casefold().strip()
        exact_matches = [
            item for item in self.items if item.name.casefold().strip() == query_norm
        ]
        if exact_matches:
            return ItemMatch(item=exact_matches[0])

        scored = sorted(
            (
                (_score_match(query, item.name), item)
                for item in self.items
                if _score_match(query, item.name) >= 0.58
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )
        if not scored:
            return ItemMatch(item=None, message="No matching item was found.")

        top_score, top_item = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0

        if top_score >= 0.86 and top_score - second_score >= 0.08:
            return ItemMatch(item=top_item)
        if len(scored) == 1 and top_score >= 0.65:
            return ItemMatch(item=top_item)

        choices = tuple(item for _score, item in scored[:8])
        return ItemMatch(
            item=None,
            choices=choices,
            message="Multiple possible item matches were found.",
        )

    def loot_pool(self, *, rarity: str, consumable: bool, apl: int) -> list[SheetItem]:
        """Return allowed items matching rarity, consumable flag, and APL."""
        pool: list[SheetItem] = []
        for item in self.items:
            if not item.allowed:
                continue
            if item.consumable != consumable:
                continue
            if item.rarity != rarity:
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
    """Return a short list of item names for fuzzy-match errors."""
    return "\n".join(f"- {item.name} ({item.rarity}, {item.source or 'No source'})" for item in items)
