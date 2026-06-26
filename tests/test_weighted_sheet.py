from __future__ import annotations

import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.loot import (
    LootSlot,
    WeightedSelection,
    _format_item_slot,
    build_session_loot_output,
    pick_weighted_item,
    select_loot_item,
)
from services.pricing import (
    base_price_for_rarity,
    direct_sell_price,
    possible_final_price_range,
    roll_broker_price,
    roll_buy_price,
)
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

        self.assertIn("Source: **HGtMH**", output)
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

        for label in ("**Permanent 1**", "**Permanent 2**", "**Permanent 3**", "**Consumable 1**", "**Consumable 2**", "**Consumable 3**", "**Consumable 4**"):
            self.assertIn(label, output)
        self.assertIn("Roll: `10` -> **Common** (fallback to **Uncommon**)", output)
        self.assertIn("Item: **Fallback Permanent**", output)
        self.assertIn("**Consumable 4**", output)
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

        self.assertIn("Creature Type: **Beast**", output)
        self.assertIn("Component Roll: `63`", output)

    def test_monster_component_uses_command_override(self):
        row = item("Monster Component Parcel", consumable=True, loot_type="Monster Component", creature_type="Beast")

        output = self.format_monster(row, creature_type="Aberration")

        self.assertIn("Creature Type: **Aberration**", output)

    def test_monster_component_falls_back_to_random_creature_type(self):
        row = item("Monster Component Parcel", consumable=True, loot_type="Monster Component", creature_type="")

        with patch("services.sheets.random.choice", return_value="Beast"):
            output = self.format_monster(row)

        self.assertIn("Creature Type: **Beast**", output)


class DwarfySaleMechanicTests(unittest.TestCase):
    def test_direct_sell_pays_40_percent_and_does_not_roll(self):
        with patch("services.pricing.random.randint") as randint:
            result = direct_sell_price("Rare")

        randint.assert_not_called()
        self.assertEqual(result.base_price, 4000)
        self.assertEqual(result.payout_percent, 40)
        self.assertEqual(result.seller_payout, 1600)
        self.assertEqual(result.roll, 0)

    def test_direct_sell_receipt_has_no_dtp_or_gold_cost(self):
        from cogs.dwarfy import build_sell_receipt

        receipt = build_sell_receipt(
            activity="Sell Magic Item directly to Dwarfy's Shop",
            character="Baehotin",
            level=13,
            seller_mention="@Player",
            seller_display_name="Player",
            listing_name="Ring of Protection",
            base_item_name="Ring of Protection",
            variant=None,
            listing_id="DWF-00001",
            rarity="Rare",
            item_detail="Rare Ring",
            source="DMG 2024",
            page="294",
            base_price=4000,
            dtp_cost=0,
            gold_cost=0,
            seller_payout=1600,
            status="Final, no takebacks",
        )

        self.assertIn("DTP spent: 0", receipt)
        self.assertIn("Gold spent: 0gp", receipt)
        self.assertNotIn("Broker roll:", receipt)

    def test_broker_roll_table(self):
        cases = [
            (20, 100, 4000, "Excellent buyer"),
            (18, 60, 2400, "Strong buyer"),
            (12, 50, 2000, "Fair buyer"),
            (7, 30, 1200, "Weak buyer"),
            (3, 20, 800, "Poor buyer"),
            (1, 0, 0, "Disaster"),
        ]

        for roll, percent, payout, text in cases:
            with self.subTest(roll=roll), patch("services.pricing.random.randint", return_value=roll):
                result = roll_broker_price("Rare")

            self.assertEqual(result.roll, roll)
            self.assertEqual(result.payout_percent, percent)
            self.assertEqual(result.seller_payout, payout)
            self.assertIn(text, result.result_text)

    def test_broker_result_line_is_scannable_near_top(self):
        from cogs.dwarfy import broker_sale_result_line

        with patch("services.pricing.random.randint", return_value=18):
            result = broker_sale_result_line(roll_broker_price("Rare"))

        self.assertEqual(
            result,
            "🎲 Broker roll: 18 - Strong buyer, 60% of base price - payout 2,400gp.",
        )

    def test_actual_rarity_is_used_for_direct_and_broker_pricing(self):
        row = item(name="Rare Rolled Low", rarity="Rare", roll_rarity="Uncommon")

        self.assertEqual(direct_sell_price(row.rarity).seller_payout, 1600)
        with patch("services.pricing.random.randint", return_value=20):
            self.assertEqual(roll_broker_price(row.rarity).seller_payout, 4000)

    def test_database_records_direct_sale_inventory(self):
        from services.database import DwarfyDatabase

        async def run_case():
            with tempfile.TemporaryDirectory() as folder:
                db = DwarfyDatabase(f"{folder}/dwarfy.sqlite")
                await db.connect()
                try:
                    row = await db.create_listing(
                        item_name="Ring of Protection",
                        rarity="Rare",
                        source="DMG 2024",
                        category="Ring",
                        tags="ring",
                        seller_user_id="123",
                        seller_display_name="Seller",
                        seller_character_name="Baehotin",
                        seller_character_level=13,
                        sell_roll=0,
                        seller_payout=1600,
                        sale_method="direct",
                        sale_percent=40,
                        dtp_cost=0,
                        gold_cost=0,
                        item_status="inventory",
                        receipt_text="Adventure Log Receipt",
                    )
                    available = await db.list_available_listings()
                finally:
                    await db.close()
                return row, available

        row, available = asyncio.run(run_case())

        self.assertEqual(row["sale_method"], "direct")
        self.assertEqual(row["seller_payout"], 1600)
        self.assertEqual(row["cost_basis"], 1600)
        self.assertEqual(row["dtp_cost"], 0)
        self.assertEqual(row["gold_cost"], 0)
        self.assertEqual(row["item_status"], "inventory")
        self.assertEqual([listing["listing_id"] for listing in available], [row["listing_id"]])

    def test_database_records_successful_broker_sale_inventory(self):
        from services.database import DwarfyDatabase

        async def run_case():
            with tempfile.TemporaryDirectory() as folder:
                db = DwarfyDatabase(f"{folder}/dwarfy.sqlite")
                await db.connect()
                try:
                    row = await db.create_listing(
                        item_name="Ring of Protection",
                        rarity="Rare",
                        source="DMG 2024",
                        category="Ring",
                        tags="ring",
                        seller_user_id="123",
                        seller_display_name="Seller",
                        seller_character_name="Baehotin",
                        seller_character_level=13,
                        sell_roll=18,
                        seller_payout=2400,
                        sale_method="broker",
                        sale_percent=60,
                        dtp_cost=5,
                        gold_cost=25,
                        broker_roll=18,
                        broker_result="Strong buyer, 60% of base price",
                        item_status="inventory",
                    )
                    available = await db.list_available_listings()
                finally:
                    await db.close()
                return row, available

        row, available = asyncio.run(run_case())

        self.assertEqual(row["sale_method"], "broker")
        self.assertEqual(row["broker_roll"], 18)
        self.assertEqual(row["broker_result"], "Strong buyer, 60% of base price")
        self.assertEqual(row["dtp_cost"], 5)
        self.assertEqual(row["gold_cost"], 25)
        self.assertEqual([listing["listing_id"] for listing in available], [row["listing_id"]])

    def test_lost_broker_item_is_not_buyable_inventory(self):
        from services.database import DwarfyDatabase

        async def run_case():
            with tempfile.TemporaryDirectory() as folder:
                db = DwarfyDatabase(f"{folder}/dwarfy.sqlite")
                await db.connect()
                try:
                    row = await db.create_listing(
                        item_name="Lost Ring",
                        rarity="Rare",
                        source="DMG 2024",
                        category="Ring",
                        tags="ring",
                        seller_user_id="123",
                        seller_display_name="Seller",
                        seller_character_name="Baehotin",
                        seller_character_level=13,
                        sell_roll=1,
                        seller_payout=0,
                        sale_method="broker",
                        sale_percent=0,
                        dtp_cost=5,
                        gold_cost=25,
                        broker_roll=1,
                        broker_result="Disaster. The item is lost during brokerage.",
                        item_status="lost",
                    )
                    available = await db.list_available_listings()
                    sold = await db.mark_listing_sold(
                        listing_id=row["listing_id"],
                        buyer_user_id="456",
                        buyer_display_name="Buyer",
                        buyer_character_name="Rhett",
                        buyer_character_level=9,
                        buy_price_roll_detail="test",
                        final_sale_price=4000,
                        realized_profit=4000,
                    )
                finally:
                    await db.close()
                return available, sold

        available, sold = asyncio.run(run_case())

        self.assertEqual(available, [])
        self.assertFalse(sold)

    def test_legacy_listing_with_null_sale_method_remains_buyable(self):
        from services.database import DwarfyDatabase

        async def run_case():
            with tempfile.TemporaryDirectory() as folder:
                db = DwarfyDatabase(f"{folder}/dwarfy.sqlite")
                await db.connect()
                try:
                    row = await db.create_listing(
                        item_name="Legacy Staff",
                        rarity="Uncommon",
                        source="DMG 2024",
                        category="Staff",
                        tags="staff",
                        seller_user_id="123",
                        seller_display_name="Seller",
                        seller_character_name="Baehotin",
                        seller_character_level=13,
                        sell_roll=14,
                        seller_payout=200,
                        sale_method=None,
                        item_status=None,
                    )
                    available = await db.list_available_listings()
                    sold = await db.mark_listing_sold(
                        listing_id=row["listing_id"],
                        buyer_user_id="456",
                        buyer_display_name="Buyer",
                        buyer_character_name="Rhett",
                        buyer_character_level=9,
                        buy_price_roll_detail="test",
                        final_sale_price=400,
                        realized_profit=200,
                    )
                finally:
                    await db.close()
                return available, sold

        available, sold = asyncio.run(run_case())

        self.assertEqual(len(available), 1)
        self.assertTrue(sold)

    def test_inspect_shows_sale_method_and_broker_audit(self):
        from cogs.dwarfy import Dwarfy

        listing = {
            "listing_id": "DWF-00001",
            "item_name": "Ring of Protection",
            "rarity": "Rare",
            "source": "DMG 2024",
            "page": "294",
            "category": "Ring",
            "tags": "ring",
            "seller_user_id": "123",
            "seller_display_name": "Seller",
            "seller_character_name": "Baehotin",
            "seller_character_level": 13,
            "cost_basis": 2400,
            "status": "available",
            "sale_method": "broker",
            "dtp_cost": 5,
            "gold_cost": 25,
            "broker_roll": 18,
            "broker_result": "Strong buyer, 60% of base price",
            "item_status": "inventory",
            "sell_roll": 18,
            "seller_payout": 2400,
            "adventure_log_receipt": "Adventure Log Receipt:\nBroker roll: 18",
        }

        output = Dwarfy._format_inspect(object.__new__(Dwarfy), listing)

        self.assertIn("Sale method: broker", output)
        self.assertIn("DTP cost: 5", output)
        self.assertIn("Gold cost: 25gp", output)
        self.assertIn("Broker roll: 18", output)
        self.assertIn("Broker result: Strong buyer, 60% of base price", output)
        self.assertIn("Item status: inventory", output)
        self.assertIn("Stored Adventure Log Receipt:", output)

    def test_shared_validation_rejects_consumables_and_artifacts(self):
        from cogs.dwarfy import sell_validation_error

        self.assertIn("Consumable=TRUE", sell_validation_error(item("Potion", consumable=True)))
        self.assertIn("cannot price", sell_validation_error(item("Artifact", rarity="Artifact")))


class DwarfyBuyHagglingTests(unittest.TestCase):
    def test_nat_20_applies_20_percent_discount(self):
        with patch("services.pricing.random.randint", side_effect=[6, 20]):
            result = roll_buy_price("Uncommon", 100)

        self.assertEqual(result.rolled_price, 600)
        self.assertEqual(result.haggling_roll, 20)
        self.assertEqual(result.discount_percent, 20)
        self.assertEqual(result.discounted_price, 480)
        self.assertEqual(result.final_price, 480)

    def test_rolls_16_to_19_apply_10_percent_discount(self):
        for haggling_roll in (16, 19):
            with self.subTest(haggling_roll=haggling_roll), patch(
                "services.pricing.random.randint",
                side_effect=[6, haggling_roll],
            ):
                result = roll_buy_price("Uncommon", 100)

            self.assertEqual(result.discount_percent, 10)
            self.assertEqual(result.discounted_price, 540)
            self.assertEqual(result.final_price, 540)

    def test_roll_15_applies_5_percent_discount(self):
        with patch("services.pricing.random.randint", side_effect=[4, 15]):
            result = roll_buy_price("Uncommon", 100)

        self.assertEqual(result.discount_percent, 5)
        self.assertEqual(result.discounted_price, 380)
        self.assertEqual(result.final_price, 380)

    def test_rolls_2_to_14_apply_no_discount(self):
        for haggling_roll in (2, 14):
            with self.subTest(haggling_roll=haggling_roll), patch(
                "services.pricing.random.randint",
                side_effect=[4, haggling_roll],
            ):
                result = roll_buy_price("Uncommon", 100)

            self.assertEqual(result.discount_percent, 0)
            self.assertEqual(result.discounted_price, 400)
            self.assertEqual(result.final_price, 400)
            self.assertEqual(result.haggling_result, "Dwarfy does not budge.")

    def test_nat_1_applies_no_discount_and_includes_insult(self):
        with patch("services.pricing.random.randint", side_effect=[4, 1]), patch(
            "services.pricing.random.choice",
            return_value="No discount. The item is magical. Your bargaining was not.",
        ):
            result = roll_buy_price("Common", 25)

        self.assertEqual(result.haggling_roll, 1)
        self.assertEqual(result.discount_percent, 0)
        self.assertEqual(result.discounted_price, 50)
        self.assertEqual(result.final_price, 50)
        self.assertEqual(result.insult_line, "No discount. The item is magical. Your bargaining was not.")

    def test_nat_1_does_not_increase_price_or_create_debt_by_itself(self):
        with patch("services.pricing.random.randint", side_effect=[4, 1]), patch(
            "services.pricing.random.choice",
            return_value="Full price. I would explain why, but then I would have to charge tutoring rates.",
        ):
            result = roll_buy_price("Common", 25)

        debt_owed = max(0, result.final_price - 100)
        self.assertEqual(result.final_price, result.rolled_price)
        self.assertEqual(debt_owed, 0)

    def test_discount_never_lowers_final_price_below_cost_basis(self):
        with patch("services.pricing.random.randint", side_effect=[2, 20]):
            result = roll_buy_price("Uncommon", 300)

        self.assertEqual(result.rolled_price, 200)
        self.assertEqual(result.discounted_price, 160)
        self.assertEqual(result.final_price, 300)
        self.assertTrue(result.cost_basis_floor_applied)

    def test_possible_buy_price_range_includes_best_haggling_discount(self):
        self.assertEqual(possible_final_price_range("Common", 0), (16, 70))
        self.assertEqual(possible_final_price_range("Uncommon", 320), (320, 600))

    def test_buy_receipt_has_no_dtp_or_shop_expense(self):
        from cogs.dwarfy import build_buy_receipt

        listing = {
            "listing_id": "DWF-00001",
            "rarity": "Uncommon",
            "source": "DMG 2024",
            "page": "234",
            "cost_basis": 100,
        }
        with patch("services.pricing.random.randint", side_effect=[6, 20]):
            result = roll_buy_price("Uncommon", 100)

        receipt = build_buy_receipt(
            buyer="@Buyer",
            buyer_character="Jimmy (1)",
            listing=listing,
            item_name="Bag of Holding",
            origin_text="@Seller as Rebecca (1)",
            buy_roll=result,
            gold_available=1000,
        )

        self.assertNotIn("Downtime cost", receipt)
        self.assertNotIn("Shop expense", receipt)
        self.assertNotIn("Shop expense: 100gp", receipt)
        self.assertIn("Discounted price: 480gp", receipt)

    def test_buy_receipt_shows_haggling_roll_and_result(self):
        from cogs.dwarfy import build_buy_receipt

        listing = {
            "listing_id": "DWF-00002",
            "rarity": "Rare",
            "source": "DMG 2024",
            "page": "294",
            "cost_basis": 1000,
        }
        with patch("services.pricing.random.randint", side_effect=[3, 4, 18]):
            result = roll_buy_price("Rare", 1000)

        receipt = build_buy_receipt(
            buyer="@Buyer",
            buyer_character="Jimmy (1)",
            listing=listing,
            item_name="Ring of Protection",
            origin_text="@Seller as Rebecca (1)",
            buy_roll=result,
            gold_available=7000,
        )

        self.assertIn("Xanathar price roll: 2d10 x 1,000gp = (3 + 4) x 1,000gp = 7000gp", receipt)
        self.assertIn("Dwarfy haggling roll: 18", receipt)
        self.assertIn("Haggling result: Strong haggling, 10% discount", receipt)
        self.assertIn("Final item price: 6,300gp", receipt)

    def test_buy_haggling_result_line_highlights_cost_basis_floor(self):
        from cogs.dwarfy import buy_haggling_result_line

        with patch("services.pricing.random.randint", side_effect=[2, 20]):
            result = roll_buy_price("Uncommon", 300)

        line = buy_haggling_result_line(result, 300)

        self.assertIn("Dwarfy haggling roll: 20", line)
        self.assertIn("Final item price: 300gp", line)
        self.assertIn("**Dwarfy will not sell below his 300gp cost basis.**", line)

    def test_database_stores_buy_haggling_audit_and_marks_sold(self):
        from services.database import DwarfyDatabase

        async def run_case():
            with tempfile.TemporaryDirectory() as folder:
                db = DwarfyDatabase(f"{folder}/dwarfy.sqlite")
                await db.connect()
                try:
                    row = await db.create_listing(
                        item_name="Bag of Holding",
                        rarity="Uncommon",
                        source="DMG 2024",
                        category="Wondrous item",
                        tags="storage",
                        seller_user_id="123",
                        seller_display_name="Seller",
                        seller_character_name="Rebecca",
                        seller_character_level=1,
                        sell_roll=0,
                        seller_payout=240,
                        sale_method="direct",
                        item_status="inventory",
                    )
                    sold = await db.mark_listing_sold(
                        listing_id=row["listing_id"],
                        buyer_user_id="456",
                        buyer_display_name="Buyer",
                        buyer_character_name="Jimmy",
                        buyer_character_level=1,
                        buy_price_roll_detail="1d6 x 100gp = 6 x 100gp = 600gp",
                        final_sale_price=540,
                        realized_profit=300,
                        buyer_gold_available=1000,
                        buy_base_asking_price=600,
                        buy_haggling_roll=18,
                        buy_haggling_result="Strong haggling, 10% discount",
                        buy_discount_percent=10,
                        buy_discounted_price=540,
                        buy_final_item_price=540,
                        buy_dwarfy_profit=300,
                        buy_receipt_text="Dwarfy Buy Receipt:\nDwarfy haggling roll: 18",
                    )
                    fetched = await db.get_listing(row["listing_id"])
                    available = await db.list_available_listings()
                finally:
                    await db.close()
                return sold, fetched, available

        sold, fetched, available = asyncio.run(run_case())

        self.assertTrue(sold)
        self.assertEqual(fetched["status"], "sold")
        self.assertEqual(fetched["item_status"], "sold")
        self.assertEqual(fetched["buy_haggling_roll"], 18)
        self.assertEqual(fetched["buy_discount_percent"], 10)
        self.assertEqual(fetched["buy_final_item_price"], 540)
        self.assertEqual(fetched["buy_dwarfy_profit"], 300)
        self.assertIn("Dwarfy Buy Receipt", fetched["buy_receipt_text"])
        self.assertEqual(available, [])


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

    def test_browse_output_shows_all_matching_listings_under_cap(self):
        from cogs.dwarfy import build_browse_output

        rows = []
        for number in range(1, 38):
            rows.append(
                (
                    {
                        "listing_id": f"DWF-{number:05d}",
                        "listing_display_name": f"Uncommon Item {number}",
                        "item_name": f"Uncommon Item {number}",
                        "rarity": "Uncommon",
                        "source": "DMG 2024",
                        "seller_user_id": "",
                        "seller_display_name": "Dwarfy Stock",
                        "seller_character_name": "Dwarfy Stock",
                        "seller_character_level": 0,
                        "stock_source": "owner_stock",
                    },
                    160,
                    600,
                )
            )

        output = build_browse_output(rows)

        self.assertIn("currently has 37 matching magic items", output)
        self.assertIn("DWF-00037", output)
        self.assertIn("Showing all 37 matching listings.", output)
        self.assertNotIn("Showing 10 of 37", output)

    def test_browse_output_caps_extremely_large_results(self):
        from cogs.dwarfy import BROWSE_LISTING_CAP, build_browse_output

        rows = []
        for number in range(1, BROWSE_LISTING_CAP + 6):
            rows.append(
                (
                    {
                        "listing_id": f"DWF-{number:05d}",
                        "listing_display_name": f"Item {number}",
                        "item_name": f"Item {number}",
                        "rarity": "Uncommon",
                        "source": "DMG 2024",
                        "seller_user_id": "",
                        "seller_display_name": "Dwarfy Stock",
                        "seller_character_name": "Dwarfy Stock",
                        "seller_character_level": 0,
                        "stock_source": "owner_stock",
                    },
                    160,
                    600,
                )
            )

        output = build_browse_output(rows)

        self.assertIn(f"Showing first {BROWSE_LISTING_CAP} of {BROWSE_LISTING_CAP + 5}", output)
        self.assertIn(f"DWF-{BROWSE_LISTING_CAP:05d}", output)
        self.assertNotIn(f"DWF-{BROWSE_LISTING_CAP + 1:05d}", output)

    def test_browse_embed_paginates_matching_listings(self):
        from cogs.dwarfy import build_browse_embed, browse_page_count

        rows = []
        for number in range(1, 13):
            rows.append(
                (
                    {
                        "listing_id": f"DWF-{number:05d}",
                        "listing_display_name": f"Item {number}",
                        "item_name": f"Item {number}",
                        "rarity": "Uncommon",
                        "source": "DMG 2024",
                        "seller_user_id": "",
                        "seller_display_name": "Dwarfy Stock",
                        "seller_character_name": "Dwarfy Stock",
                        "seller_character_level": 0,
                        "stock_source": "owner_stock",
                    },
                    160,
                    600,
                )
            )

        self.assertEqual(browse_page_count(rows), 2)
        first_page = build_browse_embed(rows, page_index=0)
        second_page = build_browse_embed(rows, page_index=1)

        self.assertEqual(first_page.title, "Dwarfy's Shop")
        self.assertEqual(len(first_page.fields), 10)
        self.assertIn("DWF-00001", first_page.fields[0].name)
        self.assertIn("Page 1 of 2", first_page.footer.text)
        self.assertEqual(len(second_page.fields), 2)
        self.assertIn("DWF-00011", second_page.fields[0].name)
        self.assertIn("Page 2 of 2", second_page.footer.text)

    def test_browse_embed_uses_safety_cap(self):
        from cogs.dwarfy import BROWSE_LISTING_CAP, build_browse_embed, browse_page_count

        rows = []
        for number in range(1, BROWSE_LISTING_CAP + 6):
            rows.append(
                (
                    {
                        "listing_id": f"DWF-{number:05d}",
                        "listing_display_name": f"Item {number}",
                        "item_name": f"Item {number}",
                        "rarity": "Uncommon",
                        "source": "DMG 2024",
                        "seller_user_id": "",
                        "seller_display_name": "Dwarfy Stock",
                        "seller_character_name": "Dwarfy Stock",
                        "seller_character_level": 0,
                        "stock_source": "owner_stock",
                    },
                    160,
                    600,
                )
            )

        self.assertEqual(browse_page_count(rows), 10)
        last_page = build_browse_embed(rows, page_index=9)

        self.assertIn("Showing first 100", last_page.description)
        self.assertEqual(len(last_page.fields), 10)
        self.assertIn("DWF-00100", last_page.fields[-1].name)
        self.assertNotIn("DWF-00101", "\n".join(field.name for field in last_page.fields))

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

    def test_random_stock_resolves_ammunition_any_to_stack_size(self):
        from cogs.dwarfy import resolve_random_stock_identity

        row = item(
            "Unbreakable Ammunition (Unbreakable Arrow) (any)",
            consumable=True,
            variant_type="Generic Ammunition",
            variant_options="Unbreakable Arrow, Unbreakable Bolt, Unbreakable Bullet",
            item_type="Ammunition",
        )

        with patch("cogs.dwarfy.random.choice", return_value="Unbreakable Arrow"):
            listing_name, variant, note = resolve_random_stock_identity(row)

        self.assertEqual(listing_name, "Unbreakable Ammunition (20 arrows)")
        self.assertEqual(variant, "20 arrows")
        self.assertIn("Random variant: 20 arrows.", note)
        self.assertNotIn("(any)", listing_name)

    def test_random_stock_resolves_bolts_and_bullets_to_stack_sizes(self):
        from cogs.dwarfy import resolve_random_stock_identity

        row = item(
            "Unbreakable Ammunition (any)",
            consumable=True,
            variant_type="Generic Ammunition",
            variant_options="Unbreakable Arrow, Unbreakable Bolt, Unbreakable Bullet",
            item_type="Ammunition",
        )

        with patch("cogs.dwarfy.random.choice", return_value="Unbreakable Bolt"):
            bolt_name, bolt_variant, _note = resolve_random_stock_identity(row)
        with patch("cogs.dwarfy.random.choice", return_value="Unbreakable Bullet"):
            bullet_name, bullet_variant, _note = resolve_random_stock_identity(row)

        self.assertEqual(bolt_name, "Unbreakable Ammunition (20 bolts)")
        self.assertEqual(bolt_variant, "20 bolts")
        self.assertEqual(bullet_name, "Unbreakable Ammunition (10 bullets)")
        self.assertEqual(bullet_variant, "10 bullets")

    def test_random_stock_uses_fallback_variants_when_options_are_absent(self):
        from cogs.dwarfy import resolve_random_stock_identity

        weapon = item("+1 Weapon", variant_type="Generic Weapon")
        ammunition = item("Ammunition of Slaying", consumable=True, variant_type="Generic Ammunition")

        with patch("cogs.dwarfy.random.choice", return_value="Rapier"):
            weapon_name, weapon_variant, _note = resolve_random_stock_identity(weapon)
        with patch("cogs.dwarfy.random.choice", return_value="10 bullets"):
            ammo_name, ammo_variant, _note = resolve_random_stock_identity(ammunition)

        self.assertEqual(weapon_name, "+1 Weapon (Rapier)")
        self.assertEqual(weapon_variant, "Rapier")
        self.assertEqual(ammo_name, "Ammunition of Slaying (10 bullets)")
        self.assertEqual(ammo_variant, "10 bullets")

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

    def test_owner_stock_origin_text_is_not_a_fake_seller(self):
        from cogs.dwarfy import listing_origin_text

        self.assertEqual(
            listing_origin_text(
                {
                    "stock_source": "owner_stock",
                    "seller_user_id": "",
                    "seller_display_name": "Dwarfy Stock",
                    "seller_character_name": "Dwarfy Stock",
                    "seller_character_level": 0,
                }
            ),
            "Dwarfy stock",
        )

    def test_owner_stock_rarity_table_has_expected_shape(self):
        from cogs.dwarfy import stock_rarity_from_roll

        self.assertEqual(stock_rarity_from_roll(20, consumable=False), "Common")
        self.assertEqual(stock_rarity_from_roll(21, consumable=False), "Uncommon")
        self.assertEqual(stock_rarity_from_roll(94, consumable=False), "Very Rare")
        self.assertEqual(stock_rarity_from_roll(100, consumable=False), "Legendary")
        self.assertEqual(stock_rarity_from_roll(96, consumable=True), "Very Rare")

    def test_random_stock_defaults_to_25_items(self):
        from cogs.dwarfy import DEFAULT_RANDOM_CONSUMABLE_COUNT, DEFAULT_RANDOM_PERMANENT_COUNT

        self.assertEqual(DEFAULT_RANDOM_PERMANENT_COUNT, 10)
        self.assertEqual(DEFAULT_RANDOM_CONSUMABLE_COUNT, 15)

    def test_owner_stock_item_pool_uses_nearest_rarity_fallback(self):
        from cogs.dwarfy import stock_item_pool

        cache = make_cache(
            [
                item("Uncommon Permanent", rarity="Uncommon", roll_rarity="Uncommon", consumable=False),
                item("Monster Trigger", rarity="Common", roll_rarity="Common", loot_type="Monster Component"),
            ]
        )

        selected_rarity, pool = stock_item_pool(cache=cache, rarity="Common", consumable=False, apl=9)

        self.assertEqual(selected_rarity, "Uncommon")
        self.assertEqual([entry.name for entry in pool], ["Uncommon Permanent"])

    def test_owner_stock_autocomplete_is_unique_and_allows_consumables(self):
        from cogs.dwarfy import Dwarfy

        cache = make_cache(
            [
                item("Bag of Holding", consumable=False),
                item("Bag of Holding", consumable=False, min_apl=5),
                item("Potion of Healing", consumable=True),
                item("Forbidden Item", allowed=False),
                item("Monster Trigger", loot_type="Monster Component"),
            ]
        )
        cog = object.__new__(Dwarfy)
        cog.bot = SimpleNamespace(sheet_cache=cache)

        names = cog._stock_item_autocomplete_names("")

        self.assertEqual(names, ["Bag of Holding", "Potion of Healing"])

    def test_owner_stock_clear_only_voids_owner_stock(self):
        from services.database import DwarfyDatabase

        async def run_case():
            with tempfile.TemporaryDirectory() as folder:
                db = DwarfyDatabase(f"{folder}/dwarfy.sqlite")
                await db.connect()
                try:
                    owner_row = await db.create_listing(
                        item_name="Owner Bag",
                        rarity="Uncommon",
                        source="DMG 2024",
                        category="Wondrous item",
                        tags="storage",
                        seller_user_id="",
                        seller_display_name="Dwarfy Stock",
                        seller_character_name="Dwarfy Stock",
                        seller_character_level=0,
                        sell_roll=0,
                        seller_payout=0,
                        cost_basis=160,
                        stock_source="owner_stock",
                        stock_batch_id="STOCK-TEST",
                        item_status="inventory",
                        ledger_entry_type="owner_stock_item",
                        ledger_cash_change=0,
                        ledger_inventory_cost_change=160,
                    )
                    player_row = await db.create_listing(
                        item_name="Player Bag",
                        rarity="Uncommon",
                        source="DMG 2024",
                        category="Wondrous item",
                        tags="storage",
                        seller_user_id="123",
                        seller_display_name="Seller",
                        seller_character_name="Rhett",
                        seller_character_level=9,
                        sell_roll=0,
                        seller_payout=160,
                    )

                    before = await db.list_available_listings()
                    summary = await db.clear_owner_stock(
                        reason="test reset",
                        stocked_by_user_id="1",
                        stocked_by_display_name="Owner",
                    )
                    owner_after = await db.get_listing(owner_row["listing_id"])
                    player_after = await db.get_listing(player_row["listing_id"])
                    after = await db.list_available_listings()
                finally:
                    await db.close()
                return before, summary, owner_after, player_after, after

        before, summary, owner_after, player_after, after = asyncio.run(run_case())

        self.assertEqual(len(before), 2)
        self.assertEqual(summary, {"count": 1, "cost_basis": 160})
        self.assertEqual(owner_after["status"], "voided")
        self.assertEqual(player_after["status"], "available")
        self.assertEqual([row["item_name"] for row in after], ["Player Bag"])

    def test_buy_receipt_can_show_owner_stock_origin(self):
        from cogs.dwarfy import build_buy_receipt

        listing = {
            "listing_id": "DWF-00077",
            "rarity": "Uncommon",
            "source": "DMG 2024",
            "page": "234",
            "cost_basis": 160,
        }
        with patch("services.pricing.random.randint", side_effect=[6, 14]):
            result = roll_buy_price("Uncommon", 160)

        receipt = build_buy_receipt(
            buyer="@Buyer",
            buyer_character="Jimmy (1)",
            listing=listing,
            item_name="Bag of Holding",
            origin_text="Dwarfy stock",
            buy_roll=result,
            gold_available=1000,
        )

        self.assertIn("Original source: Dwarfy stock", receipt)


class ClassifiedsTests(unittest.TestCase):
    def test_classified_id_parser_accepts_pasted_lines(self):
        from cogs.dwarfy import parse_classified_id

        self.assertEqual(parse_classified_id("DWC-17 - Bag of Holding"), "DWC-00017")
        self.assertEqual(parse_classified_id("`dwc 00042`"), "DWC-00042")

    def test_classified_fee_is_buyer_paid_twenty_percent(self):
        from cogs.dwarfy import classified_fee_for_price

        self.assertEqual(classified_fee_for_price(410), 82)
        self.assertEqual(classified_fee_for_price(1), 0)

    def test_classified_trade_log_shows_seller_fee_and_buyer_total(self):
        from cogs.dwarfy import build_classified_trade_log

        row = {
            "classified_id": "DWC-00001",
            "item_name": "Tentacle Rod",
            "listing_display_name": "Tentacle Rod",
            "seller_user_id": "123",
            "seller_display_name": "Seller",
            "seller_character_name": "Beto Dread",
            "seller_character_level": 9,
            "asking_price": 410,
            "broker_fee": 82,
            "buyer_total": 492,
        }

        output = build_classified_trade_log(row, buyer="@Buyer", buyer_character="Azaez (3)")

        self.assertIn("@Buyer as Azaez (3) pays 492gp total", output)
        self.assertIn("410gp to <@123> as Beto Dread (9)", output)
        self.assertIn("82gp to Dwarfy's Shop", output)

    def test_classified_browse_output_is_private_copyable_text(self):
        from cogs.dwarfy import build_classified_browse_output

        row = {
            "classified_id": "DWC-00001",
            "item_name": "+1 Weapon (Longsword)",
            "listing_display_name": "+1 Weapon (Longsword)",
            "rarity": "Uncommon",
            "asking_price": 160,
            "broker_fee": 32,
            "buyer_total": 192,
        }

        output = build_classified_browse_output([row])

        self.assertIn("Dwarfy's Classifieds has 1 open posting", output)
        self.assertIn("DWC-00001 - +1 Weapon (Longsword) - Uncommon", output)
        self.assertIn("Buyer total: 192gp", output)

    def test_classified_database_flow_records_fee_ledger_and_export(self):
        from services.database import DwarfyDatabase

        async def run_case():
            with tempfile.TemporaryDirectory() as folder:
                db = DwarfyDatabase(f"{folder}/dwarfy.sqlite")
                await db.connect()
                try:
                    row = await db.create_classified(
                        item_name="Tentacle Rod",
                        item_clean_name="Tentacle Rod",
                        listing_display_name="Tentacle Rod",
                        rarity="Rare",
                        source="DMG 2024",
                        category="Rod",
                        tags="rod",
                        seller_user_id="123",
                        seller_display_name="Seller",
                        seller_character_name="Beto Dread",
                        seller_character_level=9,
                        asking_price=410,
                        broker_fee=82,
                        buyer_total=492,
                    )
                    open_before = await db.list_open_classifieds()
                    sold = await db.mark_classified_sold(
                        classified_id=row["classified_id"],
                        buyer_user_id="456",
                        buyer_display_name="Buyer",
                        buyer_character_name="Azaez",
                        buyer_character_level=3,
                        trade_log_text="trade log",
                    )
                    fetched = await db.get_classified(row["classified_id"])
                    history = await db.history_entries(
                        listing_id=row["classified_id"],
                        entry_type="classified_fee",
                    )
                    exported = await db.export_table("classifieds")
                finally:
                    await db.close()
                return row, open_before, sold, fetched, history, exported

        row, open_before, sold, fetched, history, exported = asyncio.run(run_case())

        self.assertEqual(row["classified_id"], "DWC-00001")
        self.assertEqual([entry["classified_id"] for entry in open_before], ["DWC-00001"])
        self.assertTrue(sold)
        self.assertEqual(fetched["status"], "sold")
        self.assertEqual(fetched["buyer_character_name"], "Azaez")
        self.assertEqual(history[0]["cash_change"], 82)
        self.assertEqual(history[0]["profit_change"], 82)
        self.assertEqual(exported[0]["trade_log_text"], "trade log")

    def test_void_classified_keeps_record_and_removes_open_listing(self):
        from services.database import DwarfyDatabase

        async def run_case():
            with tempfile.TemporaryDirectory() as folder:
                db = DwarfyDatabase(f"{folder}/dwarfy.sqlite")
                await db.connect()
                try:
                    row = await db.create_classified(
                        item_name="Potion",
                        item_clean_name="Potion",
                        rarity="Common",
                        source="DMG 2024",
                        category="Potion",
                        tags="potion",
                        seller_user_id="123",
                        seller_display_name="Seller",
                        seller_character_name="Seller PC",
                        seller_character_level=1,
                        asking_price=50,
                        broker_fee=10,
                        buyer_total=60,
                    )
                    voided = await db.void_classified(row["classified_id"], "test cleanup")
                    open_after = await db.list_open_classifieds()
                finally:
                    await db.close()
                return voided, open_after

        voided, open_after = asyncio.run(run_case())

        self.assertEqual(voided["status"], "voided")
        self.assertEqual(voided["void_reason"], "test cleanup")
        self.assertEqual(open_after, [])

    def test_inspect_embed_uses_polished_card_fields(self):
        from cogs.dwarfy import build_inspect_embed

        listing = {
            "listing_id": "DWF-00001",
            "listing_display_name": "Ring of Protection",
            "item_name": "Ring of Protection",
            "rarity": "Rare",
            "source": "DMG 2024",
            "page": "294",
            "seller_user_id": "123",
            "seller_display_name": "Seller",
            "seller_character_name": "Beto",
            "seller_character_level": 9,
            "cost_basis": 1600,
            "status": "available",
            "sale_method": "direct",
            "short_description": "A protective ring.",
            "created_at": "2026-06-26T00:00:00+00:00",
        }

        embed = build_inspect_embed(listing)

        self.assertEqual(embed.title, "Ring of Protection")
        field_names = [field.name for field in embed.fields]
        self.assertIn("Listing", field_names)
        self.assertIn("Price on Buy", field_names)
        self.assertIn("Sale Method", field_names)


class HelpCommandTests(unittest.TestCase):
    def test_help_overview_mentions_core_player_flows(self):
        from cogs.dwarfy import build_help_embed

        embed = build_help_embed()
        text = "\n".join([embed.title or "", embed.description or ""] + [field.value for field in embed.fields])

        self.assertIn("/sessionloot", text)
        self.assertIn("/dwarfy browse", text)
        self.assertIn("/dwarfy sell", text)
        self.assertIn("/dwarfy classified_post", text)

    def test_all_help_topics_build_clean_embeds(self):
        from cogs.dwarfy import HELP_TOPIC_CHOICES, build_help_embed

        for choice in HELP_TOPIC_CHOICES:
            with self.subTest(topic=choice.value):
                embed = build_help_embed(choice.value)

            self.assertTrue(embed.title)
            self.assertGreaterEqual(len(embed.fields), 1)

    def test_invalid_help_topic_falls_back_to_overview(self):
        from cogs.dwarfy import build_help_embed

        self.assertEqual(build_help_embed("bogus").title, "Dwarfy Bot Help")

    def test_help_channel_topic_explains_privacy(self):
        from cogs.dwarfy import build_help_embed

        embed = build_help_embed("channels")
        text = "\n".join(field.value for field in embed.fields)

        self.assertIn("Wrong-channel errors are private", text)
        self.assertIn("Completed sales", text)
