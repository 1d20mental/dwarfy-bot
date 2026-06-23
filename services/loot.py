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


class LootError(Exception):
    """Base class for session loot problems that should be shown to the user."""


class InvalidCreatureTypeError(LootError):
    def __init__(self, creature_type: str, available: list[str]) -> None:
        self.creature_type = creature_type
        self.available = available
        super().__init__(f"Invalid creature type: {creature_type}")


@dataclass(frozen=True)
class LootSlot:
    label: str
    d100: int
    rarity: str
    item: SheetItem | None
    fallback_note: str | None = None


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


def _tags_text(item: SheetItem) -> str:
    return item.tags_text or "none"


def _choose_item(
    *,
    cache: SheetCache,
    rarity: str,
    consumable: bool,
    apl: int,
    tag: str | None,
    used_permanent_names: set[str],
) -> tuple[SheetItem | None, str | None]:
    pool = cache.loot_pool(rarity=rarity, consumable=consumable, apl=apl)
    if not pool:
        return None, None

    fallback_note = None
    chosen_pool = pool
    if tag:
        tag_norm = tag.casefold().strip()
        tagged_pool = [item for item in pool if tag_norm in item.tags]
        if tagged_pool:
            chosen_pool = tagged_pool
        else:
            slot_word = "consumable" if consumable else "permanent"
            fallback_word = "consumables" if consumable else "permanent items"
            fallback_note = (
                f'Note: No tagged {rarity} {slot_word} items found for tag "{tag}"; '
                f"used all allowed {rarity} {fallback_word} instead."
            )

    if not consumable:
        unused_pool = [
            item for item in chosen_pool if item.name.casefold() not in used_permanent_names
        ]
        if unused_pool:
            chosen_pool = unused_pool

    item = random.choice(chosen_pool)
    if not consumable:
        used_permanent_names.add(item.name.casefold())
    return item, fallback_note


def _format_item_slot(
    *,
    cache: SheetCache,
    slot: LootSlot,
    consumable: bool,
    apl: int,
    creature_type: str | None,
) -> str:
    if slot.item is None:
        consumable_text = "TRUE" if consumable else "FALSE"
        return (
            f"{slot.label}: {slot.d100} -> {slot.rarity} -> NO MATCH FOUND\n"
            f"Reason: No Allowed=TRUE, Consumable={consumable_text}, "
            f"{slot.rarity} items found for APL {apl}."
        )

    if slot.item.loot_type == "Monster Component":
        try:
            component = cache.roll_monster_component(creature_type)
        except (RuntimeError, ValueError) as exc:
            return (
                f"{slot.label}: {slot.d100} -> {slot.rarity} -> Monster Component\n"
                f"Reason: {exc}"
            )
        component_roll = component.d100 if component.d100 is not None else "random"
        lines = [
            f"{slot.label}: {slot.d100} -> {slot.rarity} -> Monster Component",
            (
                f"Creature Type: {component.creature_type} | Component Roll: "
                f"{component_roll} | Component: {component.component}"
            ),
            f"Examples: {component.examples or 'none'}",
        ]
        if component.note:
            lines.append(f"Note: {component.note}")
        return "\n".join(lines)

    return (
        f"{slot.label}: {slot.d100} -> {slot.rarity} -> {slot.item.name}\n"
        f"Source: {slot.item.source or 'Unknown'} | Tags: {_tags_text(slot.item)}"
    )


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
        item, note = _choose_item(
            cache=cache,
            rarity=rarity,
            consumable=False,
            apl=apl,
            tag=tag_clean,
            used_permanent_names=used_permanent_names,
        )
        if note and note not in fallback_notes:
            fallback_notes.append(note)
        permanent.append(LootSlot(f"Permanent {index}", d100, rarity, item, note))

    for index in range(1, consumable_slots + 1):
        d100 = random.randint(1, 100)
        rarity = rarity_from_roll(tier, d100)
        item, note = _choose_item(
            cache=cache,
            rarity=rarity,
            consumable=True,
            apl=apl,
            tag=tag_clean,
            used_permanent_names=used_permanent_names,
        )
        if note and note not in fallback_notes:
            fallback_notes.append(note)
        consumable.append(LootSlot(f"Consumable {index}", d100, rarity, item, note))

    lines = [
        "\U0001F381 Session Loot",
        "",
        f"Players: {players}",
        f"APL: {apl}",
        f"DMG Tier: {tier_text}",
        f"Total Slots: {total_slots}",
        f"Permanent Slots: {permanent_slots}",
        f"Consumable Slots: {consumable_slots}",
        f"Tag Filter: {tag_clean or 'none'}",
        f"Creature Type: {creature_clean or 'random'}",
        "",
        "Loot Priority Rolls",
        "",
    ]
    lines.extend(
        f"{rank}. Player {player_index}: {roll}"
        for rank, (player_index, roll) in enumerate(priority_rolls, start=1)
    )

    lines.extend(["", "Permanent Loot"])
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

    lines.extend(["", "Consumable Loot"])
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
        lines.extend(["", "Tag Fallback Notes"])
        lines.extend(fallback_notes)

    return "\n".join(lines)
