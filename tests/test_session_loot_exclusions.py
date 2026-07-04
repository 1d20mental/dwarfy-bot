from __future__ import annotations

import unittest

from services.sheets import SheetCache, SheetItem


class SessionLootExclusionTests(unittest.TestCase):
    def test_manifold_tool_is_hard_excluded_from_session_loot(self):
        row = SheetItem(
            name="Manifold Tool",
            rarity="Common",
            consumable=False,
            allowed=True,
            loot_type="Item",
            source="EFA",
            category="Wondrous item",
            tags=(),
            min_apl=5,
            max_apl=10,
            notes="",
            roll_rarity="Common",
            session_eligible=True,
        )
        cache = SheetCache(
            sheet_id="test",
            service_account_file="test",
            bot_items_tab="Bot Items",
            monster_components_tab="Monster Components",
        )
        cache.loaded = True
        cache.items = [row]

        self.assertEqual(cache.loot_pool(rarity="Common", consumable=False, apl=5), [])
        self.assertEqual(cache.items, [row])


if __name__ == "__main__":
    unittest.main()
