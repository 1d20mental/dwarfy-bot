from __future__ import annotations

import asyncio
import tempfile
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
    min_apl=None,
    max_apl=None,
    page="",
    item_type="",
    attunement="",
    display_detail="",
    short_description="",
    rules_text="",
    item_tags="",
    variant_options="",
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
        min_apl=min_apl,
        max_apl=max_apl,
        session_eligible=session_eligible,
        dwarfy_sell_eligible=dwarfy_sell_eligible,
        variant_type=variant_type,
        variant_instructions=variant_instructions,
        page=page,
        item_type=item_type,
        attunement=attunement,
        display_detail=display_detail,
        short_description=short_description,
        rules_text=rules_text,
        item_tags=item_tags,
        variant_options=variant_options,
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
            result = select_loot_item(
                cache=cache,
                rarity="Uncommon",
                consumable=False,
                apl=3,
                tag="storage",
                used_permanent_names=set(),
            )

        self.assertEqual(result.selection.item.name, "Storage")
        self.assertIsNone(result.note)

    def test_tag_fallback_uses_full_pool_when_no_tagged_rows_remain(self):
        cache = make_cache([item("Storage"), item("Defense")])

        with patch("services.loot.random.randint", return_value=2):
            result = select_loot_item(
                cache=cache,
                rarity="Uncommon",
                consumable=False,
                apl=3,
                tag="undead",
                used_permanent_names=set(),
            )

        self.assertEqual(result.selection.item.name, "Defense")
        self.assertIn("No tagged Uncommon permanent items", result.note)

    def test_permanent_duplicate_avoidance_and_audit_count(self):
        cache = make_cache([item("Already Picked", weight=5), item("Alternative", weight=2)])

        with patch("services.loot.random.randint", return_value=1):
            result = select_loot_item(
                cache=cache,
                rarity="Uncommon",
                consumable=False,
                apl=3,
                tag=None,
                used_permanent_names={"already picked"},
            )

        self.assertEqual(result.selection.item.name, "Alternative")
        self.assertEqual(result.selection.eligible_entry_count, 1)
        self.assertEqual(result.selection.total_weight, 2)

    def test_consumables_may_repeat(self):
        cache = make_cache([item("Potion", consumable=True)])
        used = {"potion"}

        with patch("services.loot.random.randint", return_value=1):
            first = select_loot_item(
                cache=cache,
                rarity="Uncommon",
                consumable=True,
                apl=3,
                tag=None,
                used_permanent_names=used,
            )
            second = select_loot_item(
                cache=cache,
                rarity="Uncommon",
                consumable=True,
                apl=3,
                tag=None,
                used_permanent_names=used,
            )

        self.assertEqual(first.selection.item.name, "Potion")
        self.assertEqual(second.selection.item.name, "Potion")

    def test_apl_9_common_permanent_falls_back_to_uncommon(self):
        cache = make_cache([
            item("Uncommon Permanent", roll_rarity="Uncommon", min_apl=5, max_apl=10),
            item("Rare Permanent", rarity="Rare", roll_rarity="Rare", min_apl=5, max_apl=10),
        ])

        with patch("services.loot.random.randint", return_value=1):
            result = select_loot_item(
                cache=cache,
                rarity="Common",
                consumable=False,
                apl=9,
                tag=None,
                used_permanent_names=set(),
            )

        self.assertEqual(result.selection.item.name, "Uncommon Permanent")
        self.assertEqual(result.selected_rarity, "Uncommon")

    def test_fallback_preserves_slot_type_and_apl_filter(self):
        cache = make_cache([
            item("Wrong APL", roll_rarity="Uncommon", min_apl=1, max_apl=4),
            item("Consumable Only", roll_rarity="Uncommon", consumable=True, min_apl=5, max_apl=10),
            item("Correct Permanent", roll_rarity="Rare", rarity="Rare", min_apl=5, max_apl=10),
        ])

        with patch("services.loot.random.randint", return_value=1):
            result = select_loot_item(
                cache=cache,
                rarity="Common",
                consumable=False,
                apl=9,
                tag=None,
                used_permanent_names=set(),
            )

        self.assertEqual(result.selection.item.name, "Correct Permanent")
        self.assertEqual(result.selected_rarity, "Rare")

    def test_sessionloot_players_7_apl_9_fills_all_slots_without_no_match(self):
        cache = make_cache([
            item("Fallback Permanent", roll_rarity="Uncommon", min_apl=5, max_apl=10),
            item("Common Consumable", roll_rarity="Common", consumable=True, min_apl=5, max_apl=10),
        ])
        rolls = [70, 60, 50, 40, 30, 20, 10]
        rolls.extend([10, 1, 10, 1, 10, 1, 10, 1, 10, 1, 10, 1, 10, 1])

        with patch("services.loot.random.randint", side_effect=rolls):
            output = build_session_loot_output(cache=cache, players=7, apl=9)

        for label in ("Permanent 1:", "Permanent 2:", "Permanent 3:", "Consumable 1:", "Consumable 2:", "Consumable 3:", "Consumable 4:"):
            self.assertIn(label, output)
        self.assertIn("Permanent 1: 10 -> Common, fallback to Uncommon -> Fallback Permanent", output)
        self.assertIn("Consumable 4:", output)
        self.assertNotIn("NO MATCH FOUND", output)

    def test_staff_review_does_not_attempt_weighted_roll(self):
        cache = make_cache([])

        with patch("services.loot.random.randint", return_value=50), patch(
            "services.loot.pick_weighted_item"
        ) as pick:
            output = build_session_loot_output(cache=cache, players=2, apl=3)

        pick.assert_not_called()
        self.assertIn("STAFF REVIEW NEEDED", output)
        self.assertNotIn("NO MATCH FOUND", output)


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
    def test_listing_id_parser_accepts_common_copy_paste_forms(self):
        from cogs.dwarfy import parse_listing_id

        self.assertEqual(parse_listing_id("DWF-00007"), "DWF-00007")
        self.assertEqual(parse_listing_id("dwf-7"), "DWF-00007")
        self.assertEqual(parse_listing_id("`DWF-00007`"), "DWF-00007")
        self.assertEqual(parse_listing_id("DWF-00007 - Staff of the Adder"), "DWF-00007")
        self.assertEqual(parse_listing_id("DWF-00007 \u2014 Staff of the Adder \u2014 Uncommon"), "DWF-00007")

    def test_listing_id_parser_rejects_item_names_without_ids(self):
        from cogs.dwarfy import parse_listing_id

        self.assertIsNone(parse_listing_id("Staff of the Adder"))

    def test_browse_rarity_filter_uses_discord_choices(self):
        from cogs.dwarfy import BROWSE_RARITY_CHOICES, BROWSE_RARITY_VALUES

        self.assertEqual(
            [choice.value for choice in BROWSE_RARITY_CHOICES],
            ["Common", "Uncommon", "Rare", "Very Rare", "Legendary"],
        )
        self.assertEqual(
            BROWSE_RARITY_VALUES,
            {"Common", "Uncommon", "Rare", "Very Rare", "Legendary"},
        )

    def test_sell_item_autocomplete_returns_unique_clean_names(self):
        cache = make_cache([
            item("Ring of Protection", rarity="Rare", roll_rarity="Uncommon"),
            item("Ring of Protection", rarity="Rare", roll_rarity="Rare"),
            item("Potion of Healing", consumable=True),
            item("Blocked", dwarfy_sell_eligible=False),
        ])

        self.assertEqual(cache.autocomplete_sell_item_names("ring"), ["Ring of Protection"])

    def test_ring_of_protection_sells_as_clean_name_with_enriched_data(self):
        row = item(
            "Ring of Protection",
            rarity="Rare",
            roll_rarity="Uncommon",
            page="294",
            item_type="Ring",
            display_detail="Rare Ring, requires attunement",
            short_description="You gain a bonus to AC and saving throws.",
        )
        cache = make_cache([row, item("Ring of Protection", rarity="Rare", roll_rarity="Rare", page="294", item_type="Ring", display_detail="Rare Ring, requires attunement", short_description="You gain a bonus to AC and saving throws.")])

        match = cache.match_item("Ring of Protection", for_sell=True)

        self.assertEqual(match.item.name, "Ring of Protection")
        self.assertEqual(match.item.rarity, "Rare")
        self.assertEqual(match.item.roll_rarity, "Uncommon")
        self.assertEqual(match.item.page, "294")

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

    def test_conflicting_duplicate_sell_records_are_ambiguous(self):
        cache = make_cache([
            item("Same Name", source="DMG 2024"),
            item("Same Name", source="XGE"),
        ])

        match = cache.match_item("Same Name", for_sell=True)

        self.assertIsNone(match.item)
        self.assertIn("conflict", match.message)

    def test_dwarfy_sell_eligible_false_rejects_sale(self):
        from cogs.dwarfy import sell_validation_error

        error = sell_validation_error(item("Blocked", dwarfy_sell_eligible=False))

        self.assertIn("Dwarfy Sell Eligible=FALSE", error)

    def test_dwarfy_sell_details_resolve_listing_name(self):
        from cogs.dwarfy import resolved_listing_name

        self.assertEqual(resolved_listing_name("+1 Weapon", "Longsword"), "+1 Weapon (Longsword)")

    def test_variant_options_are_parsed_and_autocompleted(self):
        cache = make_cache([
            item("+1 Weapon", variant_type="Generic Weapon", variant_options="Longsword, Rapier, Longbow"),
        ])

        self.assertEqual(
            cache.autocomplete_variant_options(item_name="+1 Weapon", query="long"),
            ["Longsword", "Longbow"],
        )

    def test_pasted_item_text_is_rejected_but_parentheses_names_are_allowed(self):
        from services.sheets import looks_like_pasted_detail_text, looks_like_pasted_item_text

        self.assertTrue(looks_like_pasted_item_text("Ring of Protection requires attunement. You gain a bonus."))
        self.assertTrue(looks_like_pasted_detail_text("Requires attunement while wearing this item. You gain a bonus."))
        self.assertFalse(looks_like_pasted_item_text("Ring of Mind Shielding (empty)"))

    def test_generic_template_detection(self):
        from services.sheets import is_generic_template_item

        self.assertTrue(is_generic_template_item(item("+1 Weapon", variant_type="Generic Weapon")))
        self.assertFalse(is_generic_template_item(item("Ring of Protection", variant_type="Specific Item")))

    def test_item_detail_summary_uses_enriched_display_detail(self):
        from services.sheets import item_detail_summary

        self.assertEqual(
            item_detail_summary(item("Ring", rarity="Rare", item_type="Ring", display_detail="Rare Ring, requires attunement")),
            "Rare Ring, requires attunement",
        )

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
            "listing_display_name": "+1 Weapon (Longsword)",
            "item_clean_name": "+1 Weapon",
            "variant": "Longsword",
            "variant_type": "Generic Weapon",
            "variant_instructions": "Choose any valid weapon when awarded.",
            "sell_roll": 14,
            "seller_payout": 200,
            "receipt_text": "Adventure Log Receipt:\nItem: +1 Weapon (Longsword)",
        }

        output = Dwarfy._format_inspect(object.__new__(Dwarfy), listing)

        self.assertIn("Item: +1 Weapon (Longsword)", output)
        self.assertIn("Variant: Longsword", output)
        self.assertIn("Variant instructions: Choose any valid weapon when awarded.", output)
        self.assertIn("Stored Adventure Log Receipt:", output)

    def test_database_stores_receipt_text_and_listing_display_name(self):
        from services.database import DwarfyDatabase

        async def run_case():
            with tempfile.TemporaryDirectory() as folder:
                db = DwarfyDatabase(f"{folder}/dwarfy.sqlite")
                await db.connect()
                try:
                    row = await db.create_listing(
                        item_name="+1 Weapon (Longsword)",
                        rarity="Uncommon",
                        source="DMG 2024",
                        category="Weapon",
                        tags="weapon",
                        seller_user_id="123",
                        seller_display_name="Seller",
                        seller_character_name="Rhett",
                        seller_character_level=9,
                        sell_roll=14,
                        seller_payout=200,
                        item_clean_name="+1 Weapon",
                        listing_display_name="+1 Weapon (Longsword)",
                        base_item_name="+1 Weapon",
                        variant="Longsword",
                        details="Inscribed.",
                        receipt_text="Adventure Log Receipt:\nItem: +1 Weapon (Longsword)",
                    )
                    fetched = await db.get_listing(row["listing_id"])
                finally:
                    await db.close()
                return fetched

        fetched = asyncio.run(run_case())

        self.assertEqual(fetched["listing_display_name"], "+1 Weapon (Longsword)")
        self.assertIn("Adventure Log Receipt", fetched["receipt_text"])

    def test_existing_buy_pricing_still_uses_floor(self):
        self.assertEqual(possible_final_price_range("Uncommon", 320), (320, 600))
        with patch("services.pricing.random.randint", return_value=1):
            roll = roll_buy_price("Uncommon", 400)
        self.assertEqual(roll.final_price, 400)
