from __future__ import annotations

import unittest
from unittest.mock import patch

from services.loot import (
    LootSlot,
    WeightedSelection,
    _format_item_slot,
    build_session_loot_output,
    pick_weighted_item,
    select_loot_item,
)
from services.pricing import base_price_for_rarity, possible_final_price_range, roll_buy_price
from services.sheets import SheetCache, SheetItem


class FakeWorksheet:
    def __init__(self, values):
        self._values = values

    def get_all_values(self):
        return self._values


class FakeSpreadsheet:
    def __init__(self, values):
        self._values = values

    def worksheet(self, _name):
        return FakeWorksheet(self._values)


def make_cache(items=None, components=None):
    cache = SheetCache(
        sheet_id="test",
        service_account_file="service-account.json",
        bot_items_tab="Bot Items",
        monster_components_tab="Monster Components",
    )
    cache.items = list(items or [])
    cache.components = list(components or [])
    cache.loaded = True
    return cache


def item(
    name="Test Item",
    *,
    rarity="Uncommon",
    roll_rarity="Uncommon",
    weight=1,
    consumable=False,
    allowed=True,
    session_eligible=True,
    tags=(),
    source="DMG 2024",
    source_code="xdmg",
    loot_type="Item",
    creature_type="",
    dwarfy_sell_eligible=None,
    variant_type="",
    variant_instructions="",
):
    return SheetItem(
        name=name,
        rarity=rarity,
        roll_rarity=roll_rarity,
        weight=weight,
        consumable=consumable,
        allowed=allowed,
        loot_type=loot_type,
        creature_type=creature_type,
        source=source,
        source_code=source_code,
        source_name="Dungeon Master's Guide (2024)",
        alternate_sources="",
        category="Wondrous item",
        tags=tuple(tag.casefold() for tag in tags),
        min_apl=None,
        max_apl=None,
        session_eligible=session_eligible,
        dwarfy_sell_eligible=dwarfy_sell_eligible,
        variant_type=variant_type,
        variant_instructions=variant_instructions,
        notes="",
    )


class SheetParsingTests(unittest.TestCase):
    def load_items(self, rows):
        cache = make_cache()
        return cache, cache._load_bot_items(FakeSpreadsheet(rows))

    def test_roll_rarity_drives_session_pool_but_rarity_drives_pricing(self):
        row = item(name="Rare Rolled Low", rarity="Rare", roll_rarity="Uncommon")
        cache = make_cache([row])

        pool = cache.loot_pool(rarity="Uncommon", consumable=False, apl=3)

        self.assertEqual(pool, [row])
        self.assertEqual(base_price_for_rarity(row.rarity), 4000)

    def test_blank_roll_rarity_is_excluded_from_session_loot(self):
        row = item(name="No Roll", rarity="Rare", roll_rarity="")
        cache = make_cache([row])

        self.assertEqual(cache.loot_pool(rarity="Rare", consumable=False, apl=3), [])

    def test_session_eligible_false_is_excluded(self):
        row = item(name="Disabled", session_eligible=False)
        cache = make_cache([row])

        self.assertEqual(cache.loot_pool(rarity="Uncommon", consumable=False, apl=3), [])

    def test_missing_weight_header_defaults_to_one(self):
        rows = [
            ["Item Name", "Rarity", "Roll Rarity", "Consumable", "Allowed", "Session Eligible"],
            ["Bag", "Uncommon", "Uncommon", "FALSE", "TRUE", "TRUE"],
        ]
        _cache, items = self.load_items(rows)

        self.assertEqual(items[0].weight, 1)

    def test_blank_weight_defaults_to_one(self):
        rows = [
            ["Item Name", "Rarity", "Roll Rarity", "Weight", "Consumable", "Allowed", "Session Eligible"],
            ["Bag", "Uncommon", "Uncommon", "", "FALSE", "TRUE", "TRUE"],
        ]
        _cache, items = self.load_items(rows)

        self.assertEqual(items[0].weight, 1)

    def test_invalid_weights_warn_and_default_to_one(self):
        rows = [
            ["Item Name", "Rarity", "Roll Rarity", "Weight", "Consumable", "Allowed", "Session Eligible"],
            ["Zero", "Uncommon", "Uncommon", "0", "FALSE", "TRUE", "TRUE"],
            ["Negative", "Uncommon", "Uncommon", "-3", "FALSE", "TRUE", "TRUE"],
            ["Fraction", "Uncommon", "Uncommon", "1.5", "FALSE", "TRUE", "TRUE"],
            ["Text", "Uncommon", "Uncommon", "abc", "FALSE", "TRUE", "TRUE"],
        ]
        cache, items = self.load_items(rows)

        self.assertEqual([loaded.weight for loaded in items], [1, 1, 1, 1])
        self.assertEqual(len([warning for warning in cache.warnings if "invalid Weight" in warning]), 4)

    def test_source_is_preserved_and_source_code_is_reference_only(self):
        row = item(name="Heliana Thing", source="HGtMH", source_code="hgtmh")
        selection = WeightedSelection(row, ticket=1, total_weight=1, eligible_entry_count=1)
        slot = LootSlot("Permanent 1", 84, "Uncommon", selection)
        output = _format_item_slot(
            cache=make_cache([row]),
            slot=slot,
            consumable=False,
            apl=3,
            creature_type=None,
        )

        self.assertIn("Source: HGtMH", output)
        self.assertNotIn("hgtmh", output)


class WeightedSelectionTests(unittest.TestCase):
    def test_weighted_selection_is_deterministic_when_randint_is_patched(self):
        pool = [item("A", weight=2), item("B", weight=3)]

        with patch("services.loot.random.randint", return_value=3):
            selection = pick_weighted_item(pool)

        self.assertEqual(selection.item.name, "B")
        self.assertEqual(selection.ticket, 3)
        self.assertEqual(selection.total_weight, 5)
        self.assertEqual(selection.eligible_entry_count, 2)

    def test_weight_ticket_ranges_for_5_2_1(self):
        pool = [item("First", weight=5), item("Second", weight=2), item("Third", weight=1)]

        expectations = {
            1: "First",
            5: "First",
            6: "Second",
            7: "Second",
            8: "Third",
        }
        for ticket, expected_name in expectations.items():
            with self.subTest(ticket=ticket), patch("services.loot.random.randint", return_value=ticket):
                self.assertEqual(pick_weighted_item(pool).item.name, expected_name)

    def test_pool_with_fewer_than_100_rows_works(self):
        pool = [item(f"Item {index}") for index in range(5)]

        with patch("services.loot.random.randint", return_value=5):
            selection = pick_weighted_item(pool)

        self.assertEqual(selection.item.name, "Item 4")

    def test_pool_with_more_than_2000_rows_works(self):
        pool = [item(f"Item {index}") for index in range(2001)]

        with patch("services.loot.random.randint", return_value=2001):
            selection = pick_weighted_item(pool)

        self.assertEqual(selection.item.name, "Item 2000")
        self.assertEqual(selection.eligible_entry_count, 2001)

    def test_large_pools_do_not_exclude_entries_by_size(self):
        cache = make_cache([item(f"Item {index}") for index in range(2500)])

        pool = cache.loot_pool(rarity="Uncommon", consumable=False, apl=3)

        self.assertEqual(len(pool), 2500)

    def test_tag_filtering_still_works(self):
        cache = make_cache([
            item("Storage", tags=("utility", "storage")),
            item("Defense", tags=("defense",)),
        ])

        with patch("services.loot.random.randint", return_value=1):
            selection, note = select_loot_item(
                cache=cache,
                rarity="Uncommon",
                consumable=False,
                apl=3,
                tag="storage",
                used_permanent_names=set(),
            )

        self.assertEqual(selection.item.name, "Storage")
        self.assertIsNone(note)

    def test_tag_fallback_uses_full_pool_when_no_tagged_rows_remain(self):
        cache = make_cache([item("Storage"), item("Defense")])

        with patch("services.loot.random.randint", return_value=2):
            selection, note = select_loot_item(
                cache=cache,
                rarity="Uncommon",
                consumable=False,
                apl=3,
                tag="undead",
                used_permanent_names=set(),
            )

        self.assertEqual(selection.item.name, "Defense")
        self.assertIn("No tagged Uncommon permanent items", note)

    def test_permanent_duplicate_avoidance_and_audit_count(self):
        cache = make_cache([item("Already Picked", weight=5), item("Alternative", weight=2)])

        with patch("services.loot.random.randint", return_value=1):
            selection, _note = select_loot_item(
                cache=cache,
                rarity="Uncommon",
                consumable=False,
                apl=3,
                tag=None,
                used_permanent_names={"already picked"},
            )

        self.assertEqual(selection.item.name, "Alternative")
        self.assertEqual(selection.eligible_entry_count, 1)
        self.assertEqual(selection.total_weight, 2)

    def test_consumables_may_repeat(self):
        cache = make_cache([item("Potion", consumable=True)])
        used = {"potion"}

        with patch("services.loot.random.randint", return_value=1):
            first, _ = select_loot_item(
                cache=cache,
                rarity="Uncommon",
                consumable=True,
                apl=3,
                tag=None,
                used_permanent_names=used,
            )
            second, _ = select_loot_item(
                cache=cache,
                rarity="Uncommon",
                consumable=True,
                apl=3,
                tag=None,
                used_permanent_names=used,
            )

        self.assertEqual(first.item.name, "Potion")
        self.assertEqual(second.item.name, "Potion")

    def test_no_match_found_does_not_attempt_weighted_roll(self):
        cache = make_cache([])

        with patch("services.loot.random.randint", return_value=50), patch(
            "services.loot.pick_weighted_item"
        ) as pick:
            output = build_session_loot_output(cache=cache, players=2, apl=3)

        pick.assert_not_called()
        self.assertIn("NO MATCH FOUND", output)


class MonsterComponentTests(unittest.TestCase):
    def components(self):
        from services.sheets import MonsterComponent

        return [
            MonsterComponent("Beast", "01-100", "Claw / talon", "Beast item"),
            MonsterComponent("Aberration", "01-100", "Eye", "Aberration item"),
        ]

    def format_monster(self, row, creature_type=None):
        selection = WeightedSelection(row, ticket=1, total_weight=1, eligible_entry_count=1)
        slot = LootSlot("Consumable 1", 74, "Uncommon", selection)
        with patch("services.sheets.random.randint", return_value=63):
            return _format_item_slot(
                cache=make_cache([row], self.components()),
                slot=slot,
                consumable=True,
                apl=3,
                creature_type=creature_type,
            )

    def test_monster_component_uses_sheet_row_creature_type(self):
        row = item("Monster Component Parcel", consumable=True, loot_type="Monster Component", creature_type="Beast")

        output = self.format_monster(row)

        self.assertIn("Creature Type: Beast", output)
        self.assertIn("Component Roll: 63", output)

    def test_monster_component_uses_command_override(self):
        row = item("Monster Component Parcel", consumable=True, loot_type="Monster Component", creature_type="Beast")

        output = self.format_monster(row, creature_type="Aberration")

        self.assertIn("Creature Type: Aberration", output)

    def test_monster_component_falls_back_to_random_creature_type(self):
        row = item("Monster Component Parcel", consumable=True, loot_type="Monster Component", creature_type="")

        with patch("services.sheets.random.choice", return_value="Beast"):
            output = self.format_monster(row)

        self.assertIn("Creature Type: Beast", output)


class MatchingAndDwarfyTests(unittest.TestCase):
    def test_fuzzy_suggestions_contain_unique_item_names(self):
        cache = make_cache([
            item("Bag of Holding", source="DMG 2024"),
            item("Bag of Holding", source="XGE"),
            item("Bagpipes of Haunting", source="DMG 2024"),
        ])

        match = cache.match_item("bag")

        self.assertEqual([choice.name for choice in match.choices], ["Bag of Holding", "Bagpipes of Haunting"])

    def test_conflicting_duplicate_exact_records_are_ambiguous(self):
        cache = make_cache([
            item("Bag of Holding", rarity="Uncommon"),
            item("Bag of Holding", rarity="Rare"),
        ])

        match = cache.match_item("bag of holding")

        self.assertIsNone(match.item)
        self.assertIn("conflict", match.message)

    def test_dwarfy_sell_eligible_false_rejects_sale(self):
        from cogs.dwarfy import sell_validation_error

        error = sell_validation_error(item("Blocked", dwarfy_sell_eligible=False))

        self.assertIn("Dwarfy Sell Eligible=FALSE", error)

    def test_dwarfy_sell_details_resolve_listing_name(self):
        from cogs.dwarfy import resolved_listing_name

        self.assertEqual(resolved_listing_name("+1 Weapon", "Longsword"), "+1 Weapon (Longsword)")

    def test_inspect_displays_resolved_variant_listing_name(self):
        from cogs.dwarfy import Dwarfy

        listing = {
            "listing_id": "DWF-00001",
            "item_name": "+1 Weapon (Longsword)",
            "rarity": "Uncommon",
            "source": "DMG 2024",
            "category": "Weapon",
            "tags": "weapon",
            "seller_user_id": "123",
            "seller_display_name": "Seller",
            "seller_character_name": "Rhett",
            "seller_character_level": 9,
            "cost_basis": 200,
            "status": "available",
            "variant_details": "Longsword",
            "variant_type": "Generic Weapon",
            "variant_instructions": "Choose any valid weapon when awarded.",
        }

        output = Dwarfy._format_inspect(object.__new__(Dwarfy), listing)

        self.assertIn("Item: +1 Weapon (Longsword)", output)
        self.assertIn("Variant details: Longsword", output)
        self.assertIn("Variant instructions: Choose any valid weapon when awarded.", output)

    def test_existing_buy_pricing_still_uses_floor(self):
        self.assertEqual(possible_final_price_range("Uncommon", 320), (320, 600))
        with patch("services.pricing.random.randint", return_value=1):
            roll = roll_buy_price("Uncommon", 400)
        self.assertEqual(roll.final_price, 400)
