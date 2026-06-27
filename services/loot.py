"""Session loot rolling rules."""

from __future__ import annotations

import random
from dataclasses import dataclass

from services.sheets import SheetCache, SheetItem


RARITY_TABLES = {
    1: [
        (1, 55, "Common"),
        (56, 91, "Uncommon"),
        (92, 100, "Rare"),
    ],
    2: [
        (1, 29, "Common"),
        (30, 79, "Uncommon"),
        (80, 97, "Rare"),
        (98, 100, "Very Rare"),
    ],
    3: [
        (1, 10, "Common"),
        (11, 33, "Uncommon"),
        (34, 70, "Rare"),
        (71, 93, "Very Rare"),
        (94, 100, "Legendary"),
    ],
    4: [
        (1, 20, "Rare"),
        (21, 64, "Very Rare"),
        (65, 100, "Legendary"),
    ],
}

RARITY_ORDER = ["Common", "Uncommon", "Rare", "Very Rare", "Legendary"]
MAX_DM_LOOT_OPTIONS = 20


class LootError(Exception):
    """Base class for session loot problems that should be shown to the user."""


class InvalidCreatureTypeError(LootError):
    def __init__(self, creature_type: str, available: list[str]) -> None:
        self.creature_type = creature_type
        self.available = available
        super().__init__(f"Invalid creature type: {creature_type}")


@dataclass(frozen=True)
class WeightedSelection:
    item: SheetItem
    ticket: int
    total_weight: int
    eligible_entry_count: int


@dataclass(frozen=True)
class LootSlot:
    label: str
    d100: int
    rarity: str
    selection: WeightedSelection | None
    fallback_note: str | None = None
    selected_rarity: str | None = None
    staff_review_reason: str | None = None

    @property
    def item(self) -> SheetItem | None:
        return self.selection.item if self.selection else None


@dataclass(frozen=True)
class LootSelectionResult:
    selection: WeightedSelection | None
    note: str | None
    selected_rarity: str | None
    staff_review_reason: str | None = None


def tier_for_apl(apl: int) -> tuple[int, str]:
    if 1 <= apl <= 4:
        return 1, "Tier 1 (APL 1-4)"
    if 5 <= apl <= 10:
        return 2, "Tier 2 (APL 5-10)"
    if 11 <= apl <= 16:
        return 3, "Tier 3 (APL 11-16)"
    if 17 <= apl <= 20:
        return 4, "Tier 4 (APL 17-20)"
    raise LootError("APL must be between 1 and 20.")


def rarity_from_roll(tier: int, d100: int) -> str:
    for low, high, rarity in RARITY_TABLES[tier]:
        if low <= d100 <= high:
            return rarity
    raise LootError(f"No rarity table result for roll {d100}.")


def pick_weighted_item(pool: list[SheetItem]) -> WeightedSelection:
    """Pick one item using stable weighted ticket ranges."""
    if not pool:
        raise LootError("Cannot pick a weighted item from an empty pool.")

    total_weight = sum(item.weight for item in pool)
    ticket = random.randint(1, total_weight)
    cumulative = 0
    for item in pool:
        cumulative += item.weight
        if ticket <= cumulative:
            return WeightedSelection(
                item=item,
                ticket=ticket,
                total_weight=total_weight,
                eligible_entry_count=len(pool),
            )

    # This should be unreachable unless item weights are mutated mid-loop.
    return WeightedSelection(
        item=pool[-1],
        ticket=ticket,
        total_weight=total_weight,
        eligible_entry_count=len(pool),
    )


def _tags_text(item: SheetItem) -> str:
    return item.tags_text or "none"


def _apply_tag_filter(
    *,
    pool: list[SheetItem],
    rarity: str,
    consumable: bool,
    tag: str | None,
) -> tuple[list[SheetItem], str | None]:
    if not tag:
        return pool, None

    tag_norm = tag.casefold().strip()
    tagged_pool = [item for item in pool if tag_norm in item.tags]
    if tagged_pool:
        return tagged_pool, None

    slot_word = "consumable" if consumable else "permanent"
    fallback_word = "consumables" if consumable else "permanent items"
    note = (
        f'Note: No tagged {rarity} {slot_word} items found for tag "{tag}"; '
        f"used all allowed {rarity} {fallback_word} instead."
    )
    return pool, note


def _rarity_fallback_order(rarity: str) -> list[str]:
    """Return rolled rarity, then nearest valid rarity buckets."""
    if rarity not in RARITY_ORDER:
        return [rarity]

    index = RARITY_ORDER.index(rarity)
    ordered = [rarity]
    for distance in range(1, len(RARITY_ORDER)):
        higher = index + distance
        lower = index - distance
        if higher < len(RARITY_ORDER):
            ordered.append(RARITY_ORDER[higher])
        if lower >= 0:
            ordered.append(RARITY_ORDER[lower])
    return ordered


def _pool_with_optional_tag(
    *,
    cache: SheetCache,
    rarity: str,
    consumable: bool,
    apl: int,
    tag: str | None,
    require_tag: bool,
) -> list[SheetItem]:
    pool = cache.loot_pool(rarity=rarity, consumable=consumable, apl=apl)
    if not pool or not tag or not require_tag:
        return pool

    tag_norm = tag.casefold().strip()
    return [item for item in pool if tag_norm in item.tags]


def _find_fallback_pool(
    *,
    cache: SheetCache,
    rarity: str,
    consumable: bool,
    apl: int,
    tag: str | None,
    require_tag: bool,
) -> tuple[str | None, list[SheetItem]]:
    for candidate_rarity in _rarity_fallback_order(rarity):
        pool = _pool_with_optional_tag(
            cache=cache,
            rarity=candidate_rarity,
            consumable=consumable,
            apl=apl,
            tag=tag,
            require_tag=require_tag,
        )
        if pool:
            return candidate_rarity, pool
    return None, []


def _tag_fallback_note(
    *,
    rolled_rarity: str,
    selected_rarity: str,
    consumable: bool,
    tag: str,
) -> str:
    slot_word = "consumable" if consumable else "permanent"
    rarity_note = (
        f" or fallback rarity {selected_rarity}"
        if selected_rarity != rolled_rarity
        else ""
    )
    return (
        f'Note: No tagged {rolled_rarity} {slot_word} items{rarity_note} '
        f'found for tag "{tag}"; used the untagged eligible pool instead.'
    )


def select_loot_item(
    *,
    cache: SheetCache,
    rarity: str,
    consumable: bool,
    apl: int,
    tag: str | None,
    used_permanent_names: set[str],
) -> LootSelectionResult:
    """Build the final pool and select an item with weighted randomness."""
    selected_rarity: str | None
    final_pool: list[SheetItem]
    fallback_note: str | None = None

    if tag:
        selected_rarity, final_pool = _find_fallback_pool(
            cache=cache,
            rarity=rarity,
            consumable=consumable,
            apl=apl,
            tag=tag,
            require_tag=True,
        )
        if selected_rarity is None:
            selected_rarity, final_pool = _find_fallback_pool(
                cache=cache,
                rarity=rarity,
                consumable=consumable,
                apl=apl,
                tag=tag,
                require_tag=False,
            )
            if selected_rarity is not None:
                fallback_note = _tag_fallback_note(
                    rolled_rarity=rarity,
                    selected_rarity=selected_rarity,
                    consumable=consumable,
                    tag=tag,
                )
    else:
        selected_rarity, final_pool = _find_fallback_pool(
            cache=cache,
            rarity=rarity,
            consumable=consumable,
            apl=apl,
            tag=None,
            require_tag=False,
        )

    if selected_rarity is None or not final_pool:
        consumable_text = "TRUE" if consumable else "FALSE"
        reason = (
            f"No Allowed=TRUE, Session Eligible=TRUE, Consumable={consumable_text} "
            f"items found for APL {apl} in any supported fallback rarity."
        )
        print(f"Session loot staff review: rolled {rarity}; {reason}")
        return LootSelectionResult(None, None, None, reason)

    if not consumable:
        unused_pool = [
            item for item in final_pool if item.name.casefold() not in used_permanent_names
        ]
        if unused_pool:
            final_pool = unused_pool

    selection = pick_weighted_item(final_pool)
    if not consumable:
        used_permanent_names.add(selection.item.name.casefold())
    return LootSelectionResult(selection, fallback_note, selected_rarity)


def _weighted_audit_line(selection: WeightedSelection) -> str:
    return (
        f"Pool: {selection.eligible_entry_count} eligible entries | "
        f"Total weight {selection.total_weight} | "
        f"Weighted ticket {selection.ticket}/{selection.total_weight}"
    )


def _variant_lines(item: SheetItem) -> list[str]:
    lines: list[str] = []
    if item.variant_type:
        lines.append(f"Variant: {item.variant_type}")
    if item.variant_instructions:
        lines.append(f"Instructions: {item.variant_instructions}")
    return lines


def _slot_heading(slot: LootSlot, item_name: str) -> str:
    selected_rarity = slot.selected_rarity or slot.rarity
    if selected_rarity != slot.rarity:
        return f"{slot.label}: {slot.d100} -> {slot.rarity}, fallback to {selected_rarity} -> {item_name}"
    return f"{slot.label}: {slot.d100} -> {slot.rarity} -> {item_name}"


def _slot_roll_line(slot: LootSlot) -> str:
    """Return a compact, scannable roll line for one loot slot."""
    selected_rarity = slot.selected_rarity or slot.rarity
    if selected_rarity != slot.rarity:
        return f"Roll: `{slot.d100}` -> **{slot.rarity}** (fallback to **{selected_rarity}**)"
    return f"Roll: `{slot.d100}` -> **{slot.rarity}**"


def _item_detail_lines(*, source: str, tags: str) -> list[str]:
    """Return item source/tag detail lines with consistent emphasis."""
    return [
        f"Source: **{source}**",
        f"Tags: {tags}",
    ]


def _component_display_name(creature_type: str, component: str) -> str:
    """Make generic essence rolls specific enough for public loot output."""
    component_clean = component.strip()
    creature_clean = creature_type.strip()
    if (
        component_clean
        and creature_clean
        and "essence" in component_clean.casefold()
        and creature_clean.casefold() not in component_clean.casefold()
    ):
        return f"{creature_clean} {component_clean}"
    return component_clean


def _format_item_slot(
    *,
    cache: SheetCache,
    slot: LootSlot,
    consumable: bool,
    apl: int,
    creature_type: str | None,
) -> str:
    if slot.selection is None:
        if slot.staff_review_reason:
            return (
                f"**{slot.label}**\n"
                f"{_slot_roll_line(slot)}\n"
                "Item: **STAFF REVIEW NEEDED**\n"
                "This slot could not be filled from the current sheet. Staff should check the bot logs and sheet filters."
            )
        return (
            f"**{slot.label}**\n"
            f"{_slot_roll_line(slot)}\n"
            "Item: **STAFF REVIEW NEEDED**\n"
            "This slot could not be filled from the current sheet."
        )

    item = slot.selection.item
    if item.loot_type == "Monster Component":
        selected_creature_type = creature_type or item.creature_type or None
        try:
            component = cache.roll_monster_component(selected_creature_type)
        except (RuntimeError, ValueError) as exc:
            return (
                f"**{slot.label}**\n"
                f"{_slot_roll_line(slot)}\n"
                "Item: **Monster Component**\n"
                f"{_weighted_audit_line(slot.selection)}\n"
                f"Reason: {exc}"
            )
        component_roll = component.d100 if component.d100 is not None else "random"
        component_name = _component_display_name(component.creature_type, component.component)
        lines = [
            f"**{slot.label}**",
            _slot_roll_line(slot),
            "Item: **Monster Component**",
            _weighted_audit_line(slot.selection),
            f"Creature Type: **{component.creature_type}** | Component Roll: `{component_roll}`",
            f"Component: **{component_name}**",
            f"Examples: {component.examples or 'none'}",
        ]
        if component.note:
            lines.append(f"Note: {component.note}")
        return "\n".join(lines)

    lines = [
        f"**{slot.label}**",
        _slot_roll_line(slot),
        f"Item: **{item.name}**",
        _weighted_audit_line(slot.selection),
    ]
    lines.extend(_item_detail_lines(source=item.source_with_page, tags=_tags_text(item)))
    lines.extend(_variant_lines(item))
    return "\n".join(lines)


def build_session_loot_output(
    *,
    cache: SheetCache,
    players: int,
    apl: int,
    tag: str | None = None,
    creature_type: str | None = None,
) -> str:
    """Roll all loot and return a public plain-text report."""
    if not cache.loaded:
        raise LootError("Google Sheet data is not loaded.")
    if not 1 <= players <= 20:
        raise LootError("Players must be between 1 and 20.")
    if not 1 <= apl <= 20:
        raise LootError("APL must be between 1 and 20.")

    if creature_type and not cache.has_creature_type(creature_type):
        raise InvalidCreatureTypeError(creature_type, cache.available_creature_types())

    tier, tier_text = tier_for_apl(apl)
    permanent_slots = players // 2
    consumable_slots = players - permanent_slots
    total_slots = players
    tag_clean = tag.strip() if tag else None
    creature_clean = creature_type.strip() if creature_type else None

    priority_rolls = sorted(
        ((index, random.randint(1, 100)) for index in range(1, players + 1)),
        key=lambda pair: pair[1],
        reverse=True,
    )

    used_permanent_names: set[str] = set()
    permanent: list[LootSlot] = []
    consumable: list[LootSlot] = []
    fallback_notes: list[str] = []

    for index in range(1, permanent_slots + 1):
        d100 = random.randint(1, 100)
        rarity = rarity_from_roll(tier, d100)
        result = select_loot_item(
            cache=cache,
            rarity=rarity,
            consumable=False,
            apl=apl,
            tag=tag_clean,
            used_permanent_names=used_permanent_names,
        )
        if result.note and result.note not in fallback_notes:
            fallback_notes.append(result.note)
        permanent.append(
            LootSlot(
                f"Permanent {index}",
                d100,
                rarity,
                result.selection,
                result.note,
                result.selected_rarity,
                result.staff_review_reason,
            )
        )

    for index in range(1, consumable_slots + 1):
        d100 = random.randint(1, 100)
        rarity = rarity_from_roll(tier, d100)
        result = select_loot_item(
            cache=cache,
            rarity=rarity,
            consumable=True,
            apl=apl,
            tag=tag_clean,
            used_permanent_names=used_permanent_names,
        )
        if result.note and result.note not in fallback_notes:
            fallback_notes.append(result.note)
        consumable.append(
            LootSlot(
                f"Consumable {index}",
                d100,
                rarity,
                result.selection,
                result.note,
                result.selected_rarity,
                result.staff_review_reason,
            )
        )

    lines = [
        "\U0001F381 **Session Loot**",
        "",
        f"**Party:** {players} player{'s' if players != 1 else ''} | APL {apl} | {tier_text}",
        f"**Slots:** {total_slots} total | {permanent_slots} permanent | {consumable_slots} consumable",
        f"**Filters:** Tag `{tag_clean or 'none'}` | Creature `{creature_clean or 'random'}`",
        "",
        "\U0001F3B2 **Loot Priority**",
        "",
    ]
    lines.extend(
        f"{rank}. **Player {player_index}** - {roll}"
        for rank, (player_index, roll) in enumerate(priority_rolls, start=1)
    )

    lines.extend(["", "\U0001F6E1\ufe0f **Permanent Loot**"])
    if permanent:
        for slot in permanent:
            lines.append("")
            lines.append(
                _format_item_slot(
                    cache=cache,
                    slot=slot,
                    consumable=False,
                    apl=apl,
                    creature_type=creature_clean,
                )
            )
    else:
        lines.append("No permanent slots for this player count.")

    lines.extend(["", "\U0001F9EA **Consumable Loot**"])
    if consumable:
        for slot in consumable:
            lines.append("")
            lines.append(
                _format_item_slot(
                    cache=cache,
                    slot=slot,
                    consumable=True,
                    apl=apl,
                    creature_type=creature_clean,
                )
            )
    else:
        lines.append("No consumable slots for this player count.")

    if fallback_notes:
        lines.extend(["", "\U0001F4CC **Tag Fallback Notes**"])
        lines.extend(fallback_notes)

    return "\n".join(lines)


def dm_incentive_option_count(
    *,
    new_hire_players: int = 0,
    jump_start: bool = False,
    tour_de_tiers: bool = False,
    extra_options: int = 0,
) -> int:
    """Return how many permanent item options a DM should roll.

    DM incentive loot creates a menu of options, but the DM chooses one item from
    the final list. The base option represents the normal DM loot item.
    """
    count = 1
    count += max(0, int(new_hire_players or 0))
    count += 1 if jump_start else 0
    count += 1 if tour_de_tiers else 0
    count += max(0, int(extra_options or 0))
    return count


def _dm_trigger_lines(
    *,
    new_hire_players: int,
    jump_start: bool,
    tour_de_tiers: bool,
    extra_options: int,
) -> list[str]:
    lines = [
        "Base DM loot option: +1",
        f"New Hires: {new_hire_players} qualifying new player{'s' if new_hire_players != 1 else ''} -> +{new_hire_players}",
        f"Jump Start: {'yes -> +1' if jump_start else 'no -> +0'}",
        f"Tour de Tiers: {'yes -> +1' if tour_de_tiers else 'no -> +0'}",
    ]
    if extra_options:
        lines.append(f"Staff-approved extra options: +{extra_options}")
    return lines


def build_dm_incentive_loot_output(
    *,
    cache: SheetCache,
    apl: int,
    tag: str | None = None,
    new_hire_players: int = 0,
    jump_start: bool = False,
    tour_de_tiers: bool = False,
    extra_options: int = 0,
) -> str:
    """Roll a DM incentive permanent-item option pool.

    This intentionally does not output XP, GP, or DTP; it only handles the extra
    loot-choice menu created by DM incentives.
    """
    if not cache.loaded:
        raise LootError("Google Sheet data is not loaded.")
    if not 1 <= apl <= 20:
        raise LootError("APL/character level must be between 1 and 20.")
    if not 0 <= int(new_hire_players or 0) <= 10:
        raise LootError("New Hires qualifying player count must be between 0 and 10.")
    if not 0 <= int(extra_options or 0) <= 10:
        raise LootError("Extra options must be between 0 and 10.")

    tag_clean = tag.strip() if tag else None
    tier, tier_text = tier_for_apl(apl)
    option_count = dm_incentive_option_count(
        new_hire_players=int(new_hire_players or 0),
        jump_start=jump_start,
        tour_de_tiers=tour_de_tiers,
        extra_options=int(extra_options or 0),
    )
    if option_count > MAX_DM_LOOT_OPTIONS:
        raise LootError(f"DM incentive loot can roll at most {MAX_DM_LOOT_OPTIONS} options at once.")

    used_permanent_names: set[str] = set()
    options: list[LootSlot] = []
    fallback_notes: list[str] = []

    for index in range(1, option_count + 1):
        d100 = random.randint(1, 100)
        rarity = rarity_from_roll(tier, d100)
        result = select_loot_item(
            cache=cache,
            rarity=rarity,
            consumable=False,
            apl=apl,
            tag=tag_clean,
            used_permanent_names=used_permanent_names,
        )
        if result.note and result.note not in fallback_notes:
            fallback_notes.append(result.note)
        options.append(
            LootSlot(
                f"Option {index}",
                d100,
                rarity,
                result.selection,
                result.note,
                result.selected_rarity,
                result.staff_review_reason,
            )
        )

    lines = [
        "\U0001F381 **DM Incentive Loot Pool**",
        "",
        f"**DM Character Level/APL:** {apl} | {tier_text}",
        f"**Options:** {option_count} permanent item option{'s' if option_count != 1 else ''} | Choose **one** item.",
        f"**Filter:** Tag `{tag_clean or 'none'}`",
        "",
        "\U0001F4CB **Qualifying Triggers**",
    ]
    lines.extend(
        _dm_trigger_lines(
            new_hire_players=int(new_hire_players or 0),
            jump_start=jump_start,
            tour_de_tiers=tour_de_tiers,
            extra_options=int(extra_options or 0),
        )
    )

    lines.extend(["", "\U0001F6E1\ufe0f **Permanent Item Options**"])
    for slot in options:
        lines.append("")
        lines.append(
            _format_item_slot(
                cache=cache,
                slot=slot,
                consumable=False,
                apl=apl,
                creature_type=None,
            )
        )

    if fallback_notes:
        lines.extend(["", "\U0001F4CC **Tag Fallback Notes**"])
        lines.extend(fallback_notes)

    lines.extend(
        [
            "",
            "\U0001F4DD **Reminder**",
            "Choose one item from this pool and record the chosen item in the session log.",
            "DM incentives only apply to games that last 3+ hours.",
        ]
    )
    return "\n".join(lines)
