"""SQLite storage for Dwarfy's inventory and ledger."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

STANDING_MIN_PRICE_GP = 500
STANDING_PAIR_WINDOW_DAYS = 30
STANDING_TIERS = [
    {
        "tier_key": "counter_stranger",
        "display_name": "Counter Stranger",
        "min_commission_gp": 0,
        "min_qualified_sales": 0,
        "min_unique_buyers": 0,
        "commission_bps": 2000,
        "sort_order": 0,
        "public_flavor": "Dwarfy knows your face. Maybe.",
    },
    {
        "tier_key": "copper_regular",
        "display_name": "Copper Regular",
        "min_commission_gp": 2500,
        "min_qualified_sales": 3,
        "min_unique_buyers": 2,
        "commission_bps": 1750,
        "sort_order": 1,
        "public_flavor": "You have appeared in the books without causing a fire.",
    },
    {
        "tier_key": "silver_ledgerhand",
        "display_name": "Silver Ledgerhand",
        "min_commission_gp": 10000,
        "min_qualified_sales": 8,
        "min_unique_buyers": 4,
        "commission_bps": 1500,
        "sort_order": 2,
        "public_flavor": "Dwarfy trusts your coin to clink correctly.",
    },
    {
        "tier_key": "gold_broker",
        "display_name": "Gold Broker",
        "min_commission_gp": 25000,
        "min_qualified_sales": 15,
        "min_unique_buyers": 7,
        "commission_bps": 1250,
        "sort_order": 3,
        "public_flavor": "Your name has a warm little drawer in the vault.",
    },
    {
        "tier_key": "mithral_partner",
        "display_name": "Mithral Partner",
        "min_commission_gp": 60000,
        "min_qualified_sales": 30,
        "min_unique_buyers": 12,
        "commission_bps": 1000,
        "sort_order": 4,
        "public_flavor": "Dwarfy says you are not a customer. You are overhead.",
    },
    {
        "tier_key": "vault_partner",
        "display_name": "Vault Partner",
        "min_commission_gp": 125000,
        "min_qualified_sales": 50,
        "min_unique_buyers": 20,
        "commission_bps": 500,
        "sort_order": 5,
        "public_flavor": "The Ledger remembers, and now it nods first.",
    },
]


def utc_now_text() -> str:
    """Store timestamps in one predictable UTC format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_utc_text(value: str | None) -> datetime:
    """Parse stored timestamps and assume UTC when old data is naive."""
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class DwarfyDatabase:
    """Thin async wrapper around SQLite.

    The database owns the live shop state. Google Sheets remains the master item
    reference, but every player sale and shop purchase is recorded here.
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.connection: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.create_tables()

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self.connection is None:
            raise RuntimeError("Database is not connected.")
        return self.connection

    async def create_tables(self) -> None:
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS listings (
                id INTEGER PRIMARY KEY,
                listing_id TEXT UNIQUE,
                item_name TEXT NOT NULL,
                rarity TEXT NOT NULL,
                source TEXT,
                category TEXT,
                tags TEXT,
                seller_user_id TEXT NOT NULL,
                seller_display_name TEXT NOT NULL,
                seller_character_name TEXT NOT NULL,
                seller_character_level INTEGER NOT NULL,
                sell_roll INTEGER NOT NULL,
                seller_payout INTEGER NOT NULL,
                cost_basis INTEGER NOT NULL,
                item_clean_name TEXT,
                listing_display_name TEXT,
                base_item_name TEXT,
                variant TEXT,
                details TEXT,
                variant_details TEXT,
                variant_type TEXT,
                variant_instructions TEXT,
                item_type TEXT,
                attunement TEXT,
                page TEXT,
                min_apl INTEGER,
                minimum_tier INTEGER,
                base_price INTEGER,
                display_detail TEXT,
                short_description TEXT,
                rules_text TEXT,
                json_notes TEXT,
                item_tags TEXT,
                receipt_text TEXT,
                sale_method TEXT,
                sale_percent INTEGER,
                dtp_cost INTEGER,
                gold_cost INTEGER,
                broker_roll INTEGER,
                broker_result TEXT,
                item_status TEXT,
                adventure_log_receipt TEXT,
                stock_source TEXT,
                stock_batch_id TEXT,
                stocked_by_user_id TEXT,
                stocked_by_display_name TEXT,
                stock_notes TEXT,
                seller_user_display TEXT,
                seller_character TEXT,
                seller_level INTEGER,
                status TEXT NOT NULL CHECK (status IN ('available', 'sold', 'voided')),
                buyer_user_id TEXT,
                buyer_display_name TEXT,
                buyer_character_name TEXT,
                buyer_character_level INTEGER,
                buyer_gold_available INTEGER,
                debt_owed INTEGER,
                debt_fine INTEGER,
                debt_total INTEGER,
                debt_status TEXT,
                buy_price_roll_detail TEXT,
                buy_base_asking_price INTEGER,
                buy_haggling_roll INTEGER,
                buy_haggling_result TEXT,
                buy_discount_percent INTEGER,
                buy_discounted_price INTEGER,
                buy_final_item_price INTEGER,
                buy_dwarfy_profit INTEGER,
                buy_receipt_text TEXT,
                final_sale_price INTEGER,
                realized_profit INTEGER,
                created_at TEXT NOT NULL,
                sold_at TEXT,
                voided_at TEXT,
                void_reason TEXT,
                voided_by_user_id TEXT,
                voided_by_display_name TEXT
            )
            """
        )
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS ledger (
                id INTEGER PRIMARY KEY,
                entry_type TEXT NOT NULL,
                listing_id TEXT,
                item_name TEXT,
                cash_change INTEGER NOT NULL,
                inventory_cost_change INTEGER NOT NULL,
                profit_change INTEGER NOT NULL,
                notes TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS characters (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                user_display_name TEXT NOT NULL,
                character_name TEXT NOT NULL COLLATE NOCASE,
                character_level INTEGER NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                is_retired INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                retired_at TEXT,
                UNIQUE(user_id, character_name)
            )
            """
        )
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS classifieds (
                id INTEGER PRIMARY KEY,
                classified_id TEXT UNIQUE,
                item_name TEXT NOT NULL,
                item_clean_name TEXT,
                listing_display_name TEXT,
                base_item_name TEXT,
                variant TEXT,
                details TEXT,
                rarity TEXT NOT NULL,
                source TEXT,
                category TEXT,
                tags TEXT,
                variant_type TEXT,
                variant_instructions TEXT,
                item_type TEXT,
                attunement TEXT,
                page TEXT,
                min_apl INTEGER,
                minimum_tier INTEGER,
                display_detail TEXT,
                short_description TEXT,
                rules_text TEXT,
                json_notes TEXT,
                item_tags TEXT,
                seller_user_id TEXT NOT NULL,
                seller_display_name TEXT NOT NULL,
                seller_character_name TEXT NOT NULL,
                seller_character_level INTEGER NOT NULL,
                asking_price INTEGER NOT NULL,
                broker_fee INTEGER NOT NULL,
                buyer_total INTEGER NOT NULL,
                commission_bps_locked INTEGER,
                seller_tier_key_at_listing TEXT,
                seller_standing_gp_at_listing INTEGER,
                status TEXT NOT NULL CHECK (status IN ('open', 'sold', 'voided')),
                buyer_user_id TEXT,
                buyer_display_name TEXT,
                buyer_character_name TEXT,
                buyer_character_level INTEGER,
                trade_log_text TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                returned_at TEXT,
                return_notice_sent_at TEXT,
                sold_at TEXT,
                voided_at TEXT,
                void_reason TEXT,
                voided_by_user_id TEXT,
                voided_by_display_name TEXT
            )
            """
        )
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS dwarfy_standing_tiers (
                tier_id INTEGER PRIMARY KEY,
                tier_key TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                min_commission_gp INTEGER NOT NULL DEFAULT 0,
                min_qualified_sales INTEGER NOT NULL DEFAULT 0,
                min_unique_buyers INTEGER NOT NULL DEFAULT 0,
                commission_bps INTEGER NOT NULL,
                sort_order INTEGER NOT NULL,
                public_flavor TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS dwarfy_standing_users (
                discord_user_id TEXT PRIMARY KEY,
                lifetime_commission_gp INTEGER NOT NULL DEFAULT 0,
                qualified_sales_count INTEGER NOT NULL DEFAULT 0,
                unique_buyer_count INTEGER NOT NULL DEFAULT 0,
                current_tier_key TEXT NOT NULL DEFAULT 'counter_stranger',
                fraud_flag_count INTEGER NOT NULL DEFAULT 0,
                last_qualified_sale_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS dwarfy_standing_ledger (
                standing_ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_id TEXT NOT NULL,
                character_name TEXT,
                classified_listing_id TEXT,
                item_name TEXT,
                event_type TEXT NOT NULL,
                amount_gp INTEGER NOT NULL,
                eligible INTEGER NOT NULL DEFAULT 1,
                eligibility_status TEXT NOT NULL DEFAULT 'eligible',
                reason TEXT,
                buyer_user_id TEXT,
                buyer_character_name TEXT,
                pair_credit_multiplier REAL NOT NULL DEFAULT 1.0,
                commission_gp_original INTEGER,
                commission_gp_awarded INTEGER,
                created_by_discord_user_id TEXT,
                created_at TEXT NOT NULL,
                reverses_standing_ledger_id INTEGER
            )
            """
        )
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS dwarfy_standing_flags (
                flag_id INTEGER PRIMARY KEY AUTOINCREMENT,
                classified_listing_id TEXT,
                standing_ledger_id INTEGER,
                seller_user_id TEXT,
                buyer_user_id TEXT,
                flag_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                details_json TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                resolved_by_discord_user_id TEXT,
                resolution_notes TEXT
            )
            """
        )
        await self._migrate_listings_table()
        await self._migrate_classifieds_table()
        await self._seed_standing_tiers()
        await self.db.commit()

    async def _ensure_default_character(self, user_id: str) -> None:
        """Make sure one active character is marked default when possible."""
        cursor = await self.db.execute(
            """
            SELECT id FROM characters
            WHERE user_id = ? AND is_retired = 0 AND is_default = 1
            LIMIT 1
            """,
            (user_id,),
        )
        if await cursor.fetchone():
            return
        cursor = await self.db.execute(
            """
            SELECT id FROM characters
            WHERE user_id = ? AND is_retired = 0
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        if row:
            await self.db.execute(
                "UPDATE characters SET is_default = 1 WHERE id = ?",
                (row["id"],),
            )

    async def save_character(
        self,
        *,
        user_id: str,
        user_display_name: str,
        character_name: str,
        character_level: int,
        make_default: bool = False,
    ) -> dict[str, Any]:
        """Create or update one user's character registration."""
        now = utc_now_text()
        name = character_name.strip()
        await self.db.execute(
            """
            INSERT INTO characters (
                user_id, user_display_name, character_name, character_level,
                is_default, is_retired, created_at, updated_at, retired_at
            )
            VALUES (?, ?, ?, ?, 0, 0, ?, ?, NULL)
            ON CONFLICT(user_id, character_name) DO UPDATE SET
                user_display_name = excluded.user_display_name,
                character_level = excluded.character_level,
                is_retired = 0,
                retired_at = NULL,
                updated_at = excluded.updated_at
            """,
            (user_id, user_display_name, name, int(character_level), now, now),
        )
        if make_default:
            await self.db.execute(
                "UPDATE characters SET is_default = 0 WHERE user_id = ?",
                (user_id,),
            )
            await self.db.execute(
                """
                UPDATE characters
                SET is_default = 1
                WHERE user_id = ? AND character_name = ?
                """,
                (user_id, name),
            )
        await self._ensure_default_character(user_id)
        await self.db.commit()
        row = await self.get_character(user_id=user_id, character_name=name)
        return row or {}

    async def get_character(self, *, user_id: str, character_name: str) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            """
            SELECT * FROM characters
            WHERE user_id = ? AND character_name = ?
            """,
            (user_id, character_name.strip()),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_characters(
        self,
        *,
        user_id: str,
        include_retired: bool = False,
    ) -> list[dict[str, Any]]:
        where = "user_id = ?"
        if not include_retired:
            where += " AND is_retired = 0"
        cursor = await self.db.execute(
            f"""
            SELECT * FROM characters
            WHERE {where}
            ORDER BY is_default DESC, is_retired ASC, character_name COLLATE NOCASE ASC
            """,
            (user_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def set_default_character(self, *, user_id: str, character_name: str) -> dict[str, Any] | None:
        row = await self.get_character(user_id=user_id, character_name=character_name)
        if row is None or int(row["is_retired"]):
            return None
        await self.db.execute(
            "UPDATE characters SET is_default = 0 WHERE user_id = ?",
            (user_id,),
        )
        now = utc_now_text()
        await self.db.execute(
            """
            UPDATE characters
            SET is_default = 1, updated_at = ?
            WHERE id = ?
            """,
            (now, row["id"]),
        )
        await self.db.commit()
        return await self.get_character(user_id=user_id, character_name=character_name)

    async def retire_character(self, *, user_id: str, character_name: str) -> dict[str, Any] | None:
        row = await self.get_character(user_id=user_id, character_name=character_name)
        if row is None or int(row["is_retired"]):
            return None
        now = utc_now_text()
        await self.db.execute(
            """
            UPDATE characters
            SET is_default = 0, is_retired = 1, updated_at = ?, retired_at = ?
            WHERE id = ?
            """,
            (now, now, row["id"]),
        )
        await self._ensure_default_character(user_id)
        await self.db.commit()
        return await self.get_character(user_id=user_id, character_name=character_name)

    async def _migrate_listings_table(self) -> None:
        """Add new nullable columns without touching existing listing data."""
        cursor = await self.db.execute("PRAGMA table_info(listings)")
        existing_columns = {row["name"] for row in await cursor.fetchall()}
        migrations = {
            "item_clean_name": "ALTER TABLE listings ADD COLUMN item_clean_name TEXT",
            "listing_display_name": "ALTER TABLE listings ADD COLUMN listing_display_name TEXT",
            "base_item_name": "ALTER TABLE listings ADD COLUMN base_item_name TEXT",
            "variant": "ALTER TABLE listings ADD COLUMN variant TEXT",
            "details": "ALTER TABLE listings ADD COLUMN details TEXT",
            "variant_details": "ALTER TABLE listings ADD COLUMN variant_details TEXT",
            "variant_type": "ALTER TABLE listings ADD COLUMN variant_type TEXT",
            "variant_instructions": "ALTER TABLE listings ADD COLUMN variant_instructions TEXT",
            "item_type": "ALTER TABLE listings ADD COLUMN item_type TEXT",
            "attunement": "ALTER TABLE listings ADD COLUMN attunement TEXT",
            "page": "ALTER TABLE listings ADD COLUMN page TEXT",
            "min_apl": "ALTER TABLE listings ADD COLUMN min_apl INTEGER",
            "minimum_tier": "ALTER TABLE listings ADD COLUMN minimum_tier INTEGER",
            "base_price": "ALTER TABLE listings ADD COLUMN base_price INTEGER",
            "display_detail": "ALTER TABLE listings ADD COLUMN display_detail TEXT",
            "short_description": "ALTER TABLE listings ADD COLUMN short_description TEXT",
            "rules_text": "ALTER TABLE listings ADD COLUMN rules_text TEXT",
            "json_notes": "ALTER TABLE listings ADD COLUMN json_notes TEXT",
            "item_tags": "ALTER TABLE listings ADD COLUMN item_tags TEXT",
            "receipt_text": "ALTER TABLE listings ADD COLUMN receipt_text TEXT",
            "sale_method": "ALTER TABLE listings ADD COLUMN sale_method TEXT",
            "sale_percent": "ALTER TABLE listings ADD COLUMN sale_percent INTEGER",
            "dtp_cost": "ALTER TABLE listings ADD COLUMN dtp_cost INTEGER",
            "gold_cost": "ALTER TABLE listings ADD COLUMN gold_cost INTEGER",
            "broker_roll": "ALTER TABLE listings ADD COLUMN broker_roll INTEGER",
            "broker_result": "ALTER TABLE listings ADD COLUMN broker_result TEXT",
            "item_status": "ALTER TABLE listings ADD COLUMN item_status TEXT",
            "adventure_log_receipt": "ALTER TABLE listings ADD COLUMN adventure_log_receipt TEXT",
            "stock_source": "ALTER TABLE listings ADD COLUMN stock_source TEXT",
            "stock_batch_id": "ALTER TABLE listings ADD COLUMN stock_batch_id TEXT",
            "stocked_by_user_id": "ALTER TABLE listings ADD COLUMN stocked_by_user_id TEXT",
            "stocked_by_display_name": "ALTER TABLE listings ADD COLUMN stocked_by_display_name TEXT",
            "stock_notes": "ALTER TABLE listings ADD COLUMN stock_notes TEXT",
            "seller_user_display": "ALTER TABLE listings ADD COLUMN seller_user_display TEXT",
            "seller_character": "ALTER TABLE listings ADD COLUMN seller_character TEXT",
            "seller_level": "ALTER TABLE listings ADD COLUMN seller_level INTEGER",
            "buyer_gold_available": "ALTER TABLE listings ADD COLUMN buyer_gold_available INTEGER",
            "debt_owed": "ALTER TABLE listings ADD COLUMN debt_owed INTEGER",
            "debt_fine": "ALTER TABLE listings ADD COLUMN debt_fine INTEGER",
            "debt_total": "ALTER TABLE listings ADD COLUMN debt_total INTEGER",
            "debt_status": "ALTER TABLE listings ADD COLUMN debt_status TEXT",
            "buy_base_asking_price": "ALTER TABLE listings ADD COLUMN buy_base_asking_price INTEGER",
            "buy_haggling_roll": "ALTER TABLE listings ADD COLUMN buy_haggling_roll INTEGER",
            "buy_haggling_result": "ALTER TABLE listings ADD COLUMN buy_haggling_result TEXT",
            "buy_discount_percent": "ALTER TABLE listings ADD COLUMN buy_discount_percent INTEGER",
            "buy_discounted_price": "ALTER TABLE listings ADD COLUMN buy_discounted_price INTEGER",
            "buy_final_item_price": "ALTER TABLE listings ADD COLUMN buy_final_item_price INTEGER",
            "buy_dwarfy_profit": "ALTER TABLE listings ADD COLUMN buy_dwarfy_profit INTEGER",
            "buy_receipt_text": "ALTER TABLE listings ADD COLUMN buy_receipt_text TEXT",
            "voided_by_user_id": "ALTER TABLE listings ADD COLUMN voided_by_user_id TEXT",
            "voided_by_display_name": "ALTER TABLE listings ADD COLUMN voided_by_display_name TEXT",
        }
        for column, statement in migrations.items():
            if column not in existing_columns:
                await self.db.execute(statement)

    async def _migrate_classifieds_table(self) -> None:
        """Add classifieds escrow columns without rebuilding the table."""
        cursor = await self.db.execute("PRAGMA table_info(classifieds)")
        existing_columns = {row["name"] for row in await cursor.fetchall()}
        migrations = {
            "expires_at": "ALTER TABLE classifieds ADD COLUMN expires_at TEXT",
            "returned_at": "ALTER TABLE classifieds ADD COLUMN returned_at TEXT",
            "return_notice_sent_at": "ALTER TABLE classifieds ADD COLUMN return_notice_sent_at TEXT",
            "voided_by_user_id": "ALTER TABLE classifieds ADD COLUMN voided_by_user_id TEXT",
            "voided_by_display_name": "ALTER TABLE classifieds ADD COLUMN voided_by_display_name TEXT",
            "min_apl": "ALTER TABLE classifieds ADD COLUMN min_apl INTEGER",
            "minimum_tier": "ALTER TABLE classifieds ADD COLUMN minimum_tier INTEGER",
            "commission_bps_locked": "ALTER TABLE classifieds ADD COLUMN commission_bps_locked INTEGER",
            "seller_tier_key_at_listing": "ALTER TABLE classifieds ADD COLUMN seller_tier_key_at_listing TEXT",
            "seller_standing_gp_at_listing": "ALTER TABLE classifieds ADD COLUMN seller_standing_gp_at_listing INTEGER",
        }
        for column, statement in migrations.items():
            if column not in existing_columns:
                await self.db.execute(statement)

        await self.db.execute(
            """
            UPDATE classifieds
            SET commission_bps_locked = 2000
            WHERE commission_bps_locked IS NULL
            """
        )
        await self.db.execute(
            """
            UPDATE classifieds
            SET seller_tier_key_at_listing = 'counter_stranger'
            WHERE seller_tier_key_at_listing IS NULL
            """
        )
        await self.db.execute(
            """
            UPDATE classifieds
            SET seller_standing_gp_at_listing = 0
            WHERE seller_standing_gp_at_listing IS NULL
            """
        )

        cursor = await self.db.execute(
            """
            SELECT id, created_at FROM classifieds
            WHERE expires_at IS NULL
            """
        )
        rows = await cursor.fetchall()
        for row in rows:
            created_at = _parse_utc_text(row["created_at"])
            expires_at = (created_at + timedelta(days=30)).isoformat(timespec="seconds")
            await self.db.execute(
                "UPDATE classifieds SET expires_at = ? WHERE id = ?",
                (expires_at, row["id"]),
            )

    async def _seed_standing_tiers(self) -> None:
        """Install the default Dwarfy Standing ladder without overwriting edits."""
        now = utc_now_text()
        for tier in STANDING_TIERS:
            await self.db.execute(
                """
                INSERT OR IGNORE INTO dwarfy_standing_tiers (
                    tier_key, display_name, min_commission_gp,
                    min_qualified_sales, min_unique_buyers, commission_bps,
                    sort_order, public_flavor, active, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    tier["tier_key"],
                    tier["display_name"],
                    tier["min_commission_gp"],
                    tier["min_qualified_sales"],
                    tier["min_unique_buyers"],
                    tier["commission_bps"],
                    tier["sort_order"],
                    tier["public_flavor"],
                    now,
                    now,
                ),
            )

    async def standing_tiers(self) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            """
            SELECT * FROM dwarfy_standing_tiers
            WHERE active = 1
            ORDER BY sort_order ASC, tier_id ASC
            """
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def _standing_tier_by_key(self, tier_key: str | None) -> dict[str, Any]:
        key = tier_key or "counter_stranger"
        cursor = await self.db.execute(
            "SELECT * FROM dwarfy_standing_tiers WHERE tier_key = ? AND active = 1",
            (key,),
        )
        row = await cursor.fetchone()
        if row:
            return dict(row)
        tiers = await self.standing_tiers()
        return tiers[0]

    async def determine_standing_tier(
        self,
        *,
        lifetime_commission_gp: int,
        qualified_sales_count: int,
        unique_buyer_count: int,
    ) -> dict[str, Any]:
        """Return the best active tier that the summary satisfies."""
        best: dict[str, Any] | None = None
        for tier in await self.standing_tiers():
            if (
                int(lifetime_commission_gp) >= int(tier["min_commission_gp"])
                and int(qualified_sales_count) >= int(tier["min_qualified_sales"])
                and int(unique_buyer_count) >= int(tier["min_unique_buyers"])
            ):
                best = tier
        if best is not None:
            return best
        tiers = await self.standing_tiers()
        return tiers[0]

    async def recalc_user_standing(self, discord_user_id: str) -> dict[str, Any]:
        """Rebuild one user's cached Dwarfy Standing from the immutable ledger."""
        now = utc_now_text()
        cursor = await self.db.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN eligible = 1 THEN amount_gp ELSE 0 END), 0) AS lifetime_commission_gp,
                COALESCE(SUM(CASE WHEN eligible = 1 AND amount_gp > 0 THEN 1 ELSE 0 END), 0) AS qualified_sales_count,
                COUNT(DISTINCT CASE WHEN eligible = 1 AND amount_gp > 0 THEN buyer_user_id ELSE NULL END) AS unique_buyer_count,
                MAX(CASE WHEN eligible = 1 AND amount_gp > 0 THEN created_at ELSE NULL END) AS last_qualified_sale_at
            FROM dwarfy_standing_ledger
            WHERE discord_user_id = ?
            """,
            (discord_user_id,),
        )
        row = dict(await cursor.fetchone())
        tier = await self.determine_standing_tier(
            lifetime_commission_gp=int(row["lifetime_commission_gp"] or 0),
            qualified_sales_count=int(row["qualified_sales_count"] or 0),
            unique_buyer_count=int(row["unique_buyer_count"] or 0),
        )
        await self.db.execute(
            """
            INSERT INTO dwarfy_standing_users (
                discord_user_id, lifetime_commission_gp, qualified_sales_count,
                unique_buyer_count, current_tier_key, fraud_flag_count,
                last_qualified_sale_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
            ON CONFLICT(discord_user_id) DO UPDATE SET
                lifetime_commission_gp = excluded.lifetime_commission_gp,
                qualified_sales_count = excluded.qualified_sales_count,
                unique_buyer_count = excluded.unique_buyer_count,
                current_tier_key = excluded.current_tier_key,
                last_qualified_sale_at = excluded.last_qualified_sale_at,
                updated_at = excluded.updated_at
            """,
            (
                discord_user_id,
                int(row["lifetime_commission_gp"] or 0),
                int(row["qualified_sales_count"] or 0),
                int(row["unique_buyer_count"] or 0),
                tier["tier_key"],
                row["last_qualified_sale_at"],
                now,
                now,
            ),
        )
        return await self.get_user_standing(discord_user_id)

    async def get_user_standing(self, discord_user_id: str) -> dict[str, Any]:
        """Return a user's cached standing summary, creating it if needed."""
        cursor = await self.db.execute(
            "SELECT * FROM dwarfy_standing_users WHERE discord_user_id = ?",
            (discord_user_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return await self.recalc_user_standing(discord_user_id)

        summary = dict(row)
        current_tier = await self._standing_tier_by_key(summary.get("current_tier_key"))
        tiers = await self.standing_tiers()
        next_tier = None
        for tier in tiers:
            if int(tier["sort_order"]) > int(current_tier["sort_order"]):
                next_tier = tier
                break
        summary["current_tier"] = current_tier
        summary["next_tier"] = next_tier
        return summary

    async def current_standing_commission_bps(self, discord_user_id: str) -> int:
        summary = await self.get_user_standing(discord_user_id)
        return int(summary["current_tier"]["commission_bps"])

    async def _standing_pair_sale_count(
        self,
        seller_user_id: str,
        buyer_user_id: str,
        *,
        now_text: str,
    ) -> int:
        window_start = (
            _parse_utc_text(now_text) - timedelta(days=STANDING_PAIR_WINDOW_DAYS)
        ).isoformat(timespec="seconds")
        cursor = await self.db.execute(
            """
            SELECT COUNT(*) AS count
            FROM dwarfy_standing_ledger
            WHERE event_type = 'classified_commission'
              AND amount_gp > 0
              AND created_at >= ?
              AND (
                    (discord_user_id = ? AND buyer_user_id = ?)
                    OR (discord_user_id = ? AND buyer_user_id = ?)
                  )
            """,
            (window_start, seller_user_id, buyer_user_id, buyer_user_id, seller_user_id),
        )
        row = await cursor.fetchone()
        return int(row["count"] or 0)

    async def _create_standing_flag(
        self,
        *,
        classified_listing_id: str,
        standing_ledger_id: int | None,
        seller_user_id: str,
        buyer_user_id: str,
        flag_type: str,
        severity: str,
        details: str,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO dwarfy_standing_flags (
                classified_listing_id, standing_ledger_id, seller_user_id,
                buyer_user_id, flag_type, severity, details_json, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                classified_listing_id,
                standing_ledger_id,
                seller_user_id,
                buyer_user_id,
                flag_type,
                severity,
                details,
                utc_now_text(),
            ),
        )

    async def award_classified_standing(
        self,
        classified: dict[str, Any],
        *,
        buyer_user_id: str,
        buyer_character_name: str,
    ) -> dict[str, Any]:
        """Award seller Dwarfy Standing for a completed classified sale."""
        seller_user_id = str(classified["seller_user_id"])
        commission_gp = int(classified.get("broker_fee") or 0)
        asking_price = int(classified.get("asking_price") or 0)
        now = utc_now_text()
        before = await self.get_user_standing(seller_user_id)

        eligible = True
        status = "eligible"
        reason = "Classified commission generated for Dwarfy."
        multiplier = 1.0
        flag: dict[str, Any] | None = None

        if seller_user_id == str(buyer_user_id):
            eligible = False
            status = "same_user"
            reason = "Buyer and seller are the same Discord user."
        elif asking_price < STANDING_MIN_PRICE_GP:
            eligible = False
            status = "below_minimum_price"
            reason = f"Sale below standing minimum of {STANDING_MIN_PRICE_GP}gp."
        elif commission_gp <= 0:
            eligible = False
            status = "no_commission"
            reason = "Dwarfy kept no commission from this sale."
        else:
            pair_count = await self._standing_pair_sale_count(
                seller_user_id,
                str(buyer_user_id),
                now_text=now,
            )
            if pair_count == 1:
                multiplier = 0.5
                status = "reduced_by_pair_limit"
                reason = f"Second sale between this buyer/seller pair within {STANDING_PAIR_WINDOW_DAYS} days."
            elif pair_count >= 2:
                multiplier = 0.0
                eligible = False
                status = "same_pair_limit"
                reason = f"Third or later sale between this buyer/seller pair within {STANDING_PAIR_WINDOW_DAYS} days."
                flag = {
                    "flag_type": "same_pair_repeat",
                    "severity": "medium",
                    "details": reason,
                }

        awarded = int(commission_gp * multiplier) if eligible else 0
        cursor = await self.db.execute(
            """
            INSERT INTO dwarfy_standing_ledger (
                discord_user_id, character_name, classified_listing_id, item_name,
                event_type, amount_gp, eligible, eligibility_status, reason,
                buyer_user_id, buyer_character_name, pair_credit_multiplier,
                commission_gp_original, commission_gp_awarded, created_at
            )
            VALUES (?, ?, ?, ?, 'classified_commission', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                seller_user_id,
                classified.get("seller_character_name"),
                classified.get("classified_id"),
                classified.get("item_name"),
                awarded,
                1 if eligible and awarded > 0 else 0,
                status,
                reason,
                str(buyer_user_id),
                buyer_character_name,
                multiplier,
                commission_gp,
                awarded,
                now,
            ),
        )
        ledger_id = int(cursor.lastrowid)
        if flag:
            await self._create_standing_flag(
                classified_listing_id=str(classified.get("classified_id")),
                standing_ledger_id=ledger_id,
                seller_user_id=seller_user_id,
                buyer_user_id=str(buyer_user_id),
                flag_type=flag["flag_type"],
                severity=flag["severity"],
                details=flag["details"],
            )
        after = await self.recalc_user_standing(seller_user_id)
        return {
            "standing_ledger_id": ledger_id,
            "standing_gain_gp": awarded,
            "commission_gp_original": commission_gp,
            "eligibility_status": status,
            "reason": reason,
            "pair_credit_multiplier": multiplier,
            "before": before,
            "after": after,
            "promoted": before["current_tier"]["tier_key"] != after["current_tier"]["tier_key"],
        }

    async def create_listing(
        self,
        *,
        item_name: str,
        rarity: str,
        source: str,
        category: str,
        tags: str,
        seller_user_id: str,
        seller_display_name: str,
        seller_character_name: str,
        seller_character_level: int,
        sell_roll: int,
        seller_payout: int,
        item_clean_name: str | None = None,
        listing_display_name: str | None = None,
        base_item_name: str | None = None,
        variant: str | None = None,
        details: str | None = None,
        variant_details: str | None = None,
        variant_type: str | None = None,
        variant_instructions: str | None = None,
        item_type: str | None = None,
        attunement: str | None = None,
        page: str | None = None,
        min_apl: int | None = None,
        minimum_tier: int | None = None,
        base_price: int | None = None,
        display_detail: str | None = None,
        short_description: str | None = None,
        rules_text: str | None = None,
        json_notes: str | None = None,
        item_tags: str | None = None,
        receipt_text: str | None = None,
        sale_method: str | None = None,
        sale_percent: int | None = None,
        dtp_cost: int | None = None,
        gold_cost: int | None = None,
        broker_roll: int | None = None,
        broker_result: str | None = None,
        item_status: str | None = "inventory",
        adventure_log_receipt: str | None = None,
        stock_source: str | None = None,
        stock_batch_id: str | None = None,
        stocked_by_user_id: str | None = None,
        stocked_by_display_name: str | None = None,
        stock_notes: str | None = None,
        seller_user_display: str | None = None,
        cost_basis: int | None = None,
        ledger_entry_type: str = "seller_payout",
        ledger_cash_change: int | None = None,
        ledger_inventory_cost_change: int | None = None,
        ledger_notes: str | None = None,
    ) -> dict[str, Any]:
        """Create an available listing after a successful player sale."""
        now = utc_now_text()
        listing_cost_basis = seller_payout if cost_basis is None else cost_basis
        cursor = await self.db.execute(
            """
            INSERT INTO listings (
                listing_id, item_name, rarity, source, category, tags,
                seller_user_id, seller_display_name, seller_character_name,
                seller_character_level, sell_roll, seller_payout, cost_basis,
                item_clean_name, listing_display_name, base_item_name, variant, details,
                variant_details, variant_type, variant_instructions,
                item_type, attunement, page, min_apl, minimum_tier, base_price, display_detail, short_description,
                rules_text, json_notes, item_tags, receipt_text,
                sale_method, sale_percent, dtp_cost, gold_cost,
                broker_roll, broker_result, item_status, adventure_log_receipt,
                stock_source, stock_batch_id, stocked_by_user_id, stocked_by_display_name,
                stock_notes,
                seller_user_display, seller_character, seller_level,
                status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'available', ?)
            """,
            (
                None,
                item_name,
                rarity,
                source,
                category,
                tags,
                seller_user_id,
                seller_display_name,
                seller_character_name,
                seller_character_level,
                sell_roll,
                seller_payout,
                listing_cost_basis,
                item_clean_name,
                listing_display_name,
                base_item_name,
                variant,
                details,
                variant_details,
                variant_type,
                variant_instructions,
                item_type,
                attunement,
                page,
                min_apl,
                minimum_tier,
                base_price,
                display_detail,
                short_description,
                rules_text,
                json_notes,
                item_tags,
                receipt_text,
                sale_method,
                sale_percent,
                dtp_cost,
                gold_cost,
                broker_roll,
                broker_result,
                item_status,
                adventure_log_receipt or receipt_text,
                stock_source,
                stock_batch_id,
                stocked_by_user_id,
                stocked_by_display_name,
                stock_notes,
                seller_user_display,
                seller_character_name,
                seller_character_level,
                now,
            ),
        )
        row_id = cursor.lastrowid
        listing_id = f"DWF-{row_id:05d}"
        await self.db.execute(
            "UPDATE listings SET listing_id = ? WHERE id = ?",
            (listing_id, row_id),
        )
        await self.add_ledger_entry(
            entry_type=ledger_entry_type,
            listing_id=listing_id,
            item_name=item_name,
            cash_change=-seller_payout if ledger_cash_change is None else ledger_cash_change,
            inventory_cost_change=listing_cost_basis if ledger_inventory_cost_change is None else ledger_inventory_cost_change,
            profit_change=0,
            notes=ledger_notes or f"Paid seller for {item_name}.",
            commit=False,
        )
        await self.db.commit()
        return await self.get_listing(listing_id) or {}

    async def add_ledger_entry(
        self,
        *,
        entry_type: str,
        listing_id: str | None,
        item_name: str | None,
        cash_change: int,
        inventory_cost_change: int,
        profit_change: int,
        notes: str,
        commit: bool = True,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO ledger (
                entry_type, listing_id, item_name, cash_change,
                inventory_cost_change, profit_change, notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry_type,
                listing_id,
                item_name,
                cash_change,
                inventory_cost_change,
                profit_change,
                notes,
                utc_now_text(),
            ),
        )
        if commit:
            await self.db.commit()

    async def get_listing(self, listing_id: str) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM listings WHERE UPPER(listing_id) = UPPER(?)",
            (listing_id.strip(),),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_available_listings(self) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            """
            SELECT * FROM listings
            WHERE status = 'available'
              AND COALESCE(item_status, 'inventory') = 'inventory'
            ORDER BY id ASC
            """
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def mark_listing_sold(
        self,
        *,
        listing_id: str,
        buyer_user_id: str,
        buyer_display_name: str,
        buyer_character_name: str,
        buyer_character_level: int,
        buy_price_roll_detail: str,
        final_sale_price: int,
        realized_profit: int,
        buyer_gold_available: int | None = None,
        debt_owed: int | None = None,
        debt_fine: int | None = None,
        debt_total: int | None = None,
        debt_status: str | None = None,
        buy_base_asking_price: int | None = None,
        buy_haggling_roll: int | None = None,
        buy_haggling_result: str | None = None,
        buy_discount_percent: int | None = None,
        buy_discounted_price: int | None = None,
        buy_final_item_price: int | None = None,
        buy_dwarfy_profit: int | None = None,
        buy_receipt_text: str | None = None,
    ) -> bool:
        """Mark an available listing as sold and write the receipt to ledger."""
        listing = await self.get_listing(listing_id)
        if listing is None or listing["status"] != "available":
            return False
        if (listing.get("item_status") or "inventory") != "inventory":
            return False

        now = utc_now_text()
        cursor = await self.db.execute(
            """
            UPDATE listings
            SET status = 'sold',
                item_status = 'sold',
                buyer_user_id = ?,
                buyer_display_name = ?,
                buyer_character_name = ?,
                buyer_character_level = ?,
                buyer_gold_available = ?,
                debt_owed = ?,
                debt_fine = ?,
                debt_total = ?,
                debt_status = ?,
                buy_price_roll_detail = ?,
                buy_base_asking_price = ?,
                buy_haggling_roll = ?,
                buy_haggling_result = ?,
                buy_discount_percent = ?,
                buy_discounted_price = ?,
                buy_final_item_price = ?,
                buy_dwarfy_profit = ?,
                buy_receipt_text = ?,
                final_sale_price = ?,
                realized_profit = ?,
                sold_at = ?
            WHERE UPPER(listing_id) = UPPER(?) AND status = 'available'
            """,
            (
                buyer_user_id,
                buyer_display_name,
                buyer_character_name,
                buyer_character_level,
                buyer_gold_available,
                debt_owed,
                debt_fine,
                debt_total,
                debt_status,
                buy_price_roll_detail,
                buy_base_asking_price,
                buy_haggling_roll,
                buy_haggling_result,
                buy_discount_percent,
                buy_discounted_price,
                buy_final_item_price,
                buy_dwarfy_profit,
                buy_receipt_text,
                final_sale_price,
                realized_profit,
                now,
                listing_id,
            ),
        )
        if cursor.rowcount != 1:
            await self.db.rollback()
            return False

        await self.add_ledger_entry(
            entry_type="buyer_payment",
            listing_id=listing["listing_id"],
            item_name=listing["item_name"],
            cash_change=final_sale_price,
            inventory_cost_change=-int(listing["cost_basis"]),
            profit_change=realized_profit,
            notes=f"Sold {listing['item_name']} to buyer.",
            commit=False,
        )
        await self.db.commit()
        return True

    async def void_listing(
        self,
        listing_id: str,
        reason: str,
        *,
        voided_by_user_id: str | None = None,
        voided_by_display_name: str | None = None,
    ) -> dict[str, Any] | None:
        """Mark a listing as voided without deleting its audit history."""
        listing = await self.get_listing(listing_id)
        if listing is None or listing["status"] == "voided":
            return None

        previous_status = listing["status"]
        realized_profit = int(listing["realized_profit"] or 0)
        inventory_reversal = -int(listing["cost_basis"]) if previous_status == "available" else 0
        profit_reversal = -realized_profit if previous_status == "sold" else 0

        await self.db.execute(
            """
            UPDATE listings
            SET status = 'voided',
                item_status = 'voided',
                voided_at = ?,
                void_reason = ?,
                voided_by_user_id = ?,
                voided_by_display_name = ?
            WHERE UPPER(listing_id) = UPPER(?)
            """,
            (
                utc_now_text(),
                reason.strip(),
                voided_by_user_id,
                voided_by_display_name,
                listing_id,
            ),
        )
        await self.add_ledger_entry(
            entry_type="void",
            listing_id=listing["listing_id"],
            item_name=listing["item_name"],
            cash_change=0,
            inventory_cost_change=inventory_reversal,
            profit_change=profit_reversal,
            notes=(
                f"Voided listing that was {previous_status}"
                f"{f' by {voided_by_display_name} ({voided_by_user_id})' if voided_by_display_name else ''}: "
                f"{reason.strip()}"
            ),
            commit=False,
        )
        await self.db.commit()
        return await self.get_listing(listing_id)

    async def clear_owner_stock(
        self,
        *,
        reason: str,
        stocked_by_user_id: str,
        stocked_by_display_name: str,
    ) -> dict[str, int]:
        """Void available owner-stock listings without touching player inventory."""
        cursor = await self.db.execute(
            """
            SELECT COUNT(*) AS count, COALESCE(SUM(cost_basis), 0) AS cost_basis
            FROM listings
            WHERE status = 'available'
              AND COALESCE(item_status, 'inventory') = 'inventory'
              AND stock_source = 'owner_stock'
            """
        )
        summary = await cursor.fetchone()
        count = int(summary["count"])
        cost_basis = int(summary["cost_basis"])
        if count == 0:
            return {"count": 0, "cost_basis": 0}

        now = utc_now_text()
        clean_reason = reason.strip() or "Owner stock reset."
        await self.db.execute(
            """
            UPDATE listings
            SET status = 'voided',
                item_status = 'voided',
                voided_at = ?,
                void_reason = ?,
                voided_by_user_id = ?,
                voided_by_display_name = ?
            WHERE status = 'available'
              AND COALESCE(item_status, 'inventory') = 'inventory'
              AND stock_source = 'owner_stock'
            """,
            (now, clean_reason, stocked_by_user_id, stocked_by_display_name),
        )
        await self.add_ledger_entry(
            entry_type="owner_stock_clear",
            listing_id=None,
            item_name=None,
            cash_change=0,
            inventory_cost_change=-cost_basis,
            profit_change=0,
            notes=(
                f"Owner stock reset by {stocked_by_display_name} "
                f"({stocked_by_user_id}): {clean_reason}"
            ),
            commit=False,
        )
        await self.db.commit()
        return {"count": count, "cost_basis": cost_basis}

    async def shop_stats(self) -> dict[str, Any]:
        """Return all numbers used by /dwarfy stats."""
        stats: dict[str, Any] = {}

        for status in ("available", "sold", "voided"):
            cursor = await self.db.execute(
                "SELECT COUNT(*) AS count FROM listings WHERE status = ?",
                (status,),
            )
            stats[f"{status}_count"] = int((await cursor.fetchone())["count"])

        cursor = await self.db.execute(
            """
            SELECT COALESCE(SUM(seller_payout), 0) AS total
            FROM listings
            WHERE status IN ('available', 'sold')
            """
        )
        stats["total_paid_to_sellers"] = int((await cursor.fetchone())["total"])

        cursor = await self.db.execute(
            """
            SELECT COALESCE(SUM(final_sale_price), 0) AS total
            FROM listings
            WHERE status = 'sold'
            """
        )
        stats["total_received_from_buyers"] = int((await cursor.fetchone())["total"])

        cursor = await self.db.execute(
            """
            SELECT COALESCE(SUM(realized_profit), 0) AS total
            FROM listings
            WHERE status = 'sold'
            """
        )
        stats["realized_profit"] = int((await cursor.fetchone())["total"])

        cursor = await self.db.execute(
            """
            SELECT COALESCE(SUM(cost_basis), 0) AS total
            FROM listings
            WHERE status = 'available'
            """
        )
        stats["available_inventory_cost_basis"] = int((await cursor.fetchone())["total"])

        cursor = await self.db.execute(
            "SELECT COALESCE(SUM(cash_change), 0) AS total FROM ledger"
        )
        stats["business_cash_flow"] = int((await cursor.fetchone())["total"])

        cursor = await self.db.execute(
            """
            SELECT listing_id, item_name, realized_profit
            FROM listings
            WHERE status = 'sold'
            ORDER BY realized_profit DESC, id ASC
            LIMIT 1
            """
        )
        best = await cursor.fetchone()
        stats["most_profitable_flip"] = dict(best) if best else None

        cursor = await self.db.execute(
            """
            SELECT COUNT(*) AS count
            FROM listings
            WHERE status = 'available'
              AND COALESCE(item_status, 'inventory') = 'inventory'
              AND stock_source = 'owner_stock'
            """
        )
        stats["owner_stock_available_count"] = int((await cursor.fetchone())["count"])

        cursor = await self.db.execute(
            """
            SELECT COUNT(*) AS count
            FROM listings
            WHERE status = 'available'
              AND COALESCE(item_status, 'inventory') = 'inventory'
              AND COALESCE(stock_source, '') != 'owner_stock'
            """
        )
        stats["player_stock_available_count"] = int((await cursor.fetchone())["count"])

        cursor = await self.db.execute(
            """
            SELECT listing_id, item_name, cost_basis
            FROM listings
            WHERE status = 'available'
            ORDER BY cost_basis DESC, id ASC
            LIMIT 1
            """
        )
        row = await cursor.fetchone()
        stats["most_expensive_available_item"] = dict(row) if row else None

        cursor = await self.db.execute(
            """
            SELECT listing_id, item_name, created_at
            FROM listings
            WHERE status = 'available'
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """
        )
        row = await cursor.fetchone()
        stats["oldest_unsold_item"] = dict(row) if row else None

        cursor = await self.db.execute(
            """
            SELECT seller_display_name, COUNT(*) AS count, COALESCE(SUM(seller_payout), 0) AS total
            FROM listings
            WHERE seller_user_id != ''
            GROUP BY seller_user_id, seller_display_name
            ORDER BY count DESC, total DESC
            LIMIT 1
            """
        )
        row = await cursor.fetchone()
        stats["top_seller"] = dict(row) if row else None

        cursor = await self.db.execute(
            """
            SELECT buyer_display_name, COUNT(*) AS count, COALESCE(SUM(final_sale_price), 0) AS total
            FROM listings
            WHERE status = 'sold' AND buyer_user_id IS NOT NULL
            GROUP BY buyer_user_id, buyer_display_name
            ORDER BY count DESC, total DESC
            LIMIT 1
            """
        )
        row = await cursor.fetchone()
        stats["top_buyer"] = dict(row) if row else None
        return stats

    async def owner_stock_status(self) -> dict[str, Any]:
        """Return freshness information for current owner-stocked inventory."""
        cursor = await self.db.execute(
            """
            SELECT COUNT(*) AS count,
                   MIN(created_at) AS oldest_created_at,
                   MAX(created_at) AS newest_created_at,
                   MAX(stock_batch_id) AS latest_batch_id
            FROM listings
            WHERE status = 'available'
              AND COALESCE(item_status, 'inventory') = 'inventory'
              AND stock_source = 'owner_stock'
            """
        )
        row = await cursor.fetchone()
        return dict(row) if row else {
            "count": 0,
            "oldest_created_at": None,
            "newest_created_at": None,
            "latest_batch_id": None,
        }

    async def resolve_listing_debt(self, listing_id: str, reason: str) -> dict[str, Any] | None:
        """Mark a sold listing's debt consequence as resolved."""
        listing = await self.get_listing(listing_id)
        if listing is None or not listing.get("debt_total"):
            return None
        await self.db.execute(
            """
            UPDATE listings
            SET debt_status = 'resolved'
            WHERE UPPER(listing_id) = UPPER(?)
            """,
            (listing_id,),
        )
        await self.add_ledger_entry(
            entry_type="debt_resolved",
            listing_id=listing["listing_id"],
            item_name=listing["item_name"],
            cash_change=0,
            inventory_cost_change=0,
            profit_change=0,
            notes=f"Debt resolved: {reason.strip()}",
            commit=False,
        )
        await self.db.commit()
        return await self.get_listing(listing_id)

    async def history_entries(
        self,
        *,
        limit: int = 20,
        listing_id: str | None = None,
        entry_type: str | None = None,
        status: str | None = None,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent ledger entries with optional listing filters."""
        clauses: list[str] = []
        params: list[Any] = []
        if listing_id:
            clauses.append("UPPER(ledger.listing_id) = UPPER(?)")
            params.append(listing_id)
        if entry_type:
            clauses.append("ledger.entry_type = ?")
            params.append(entry_type.strip())
        if status:
            clauses.append("listings.status = ?")
            params.append(status.strip())
        if search:
            like = f"%{search.strip()}%"
            clauses.append(
                """
                (
                    ledger.item_name LIKE ?
                    OR ledger.notes LIKE ?
                    OR ledger.listing_id LIKE ?
                    OR listings.seller_display_name LIKE ?
                    OR listings.buyer_display_name LIKE ?
                    OR listings.seller_character_name LIKE ?
                    OR listings.buyer_character_name LIKE ?
                    OR classifieds.seller_display_name LIKE ?
                    OR classifieds.buyer_display_name LIKE ?
                    OR classifieds.seller_character_name LIKE ?
                    OR classifieds.buyer_character_name LIKE ?
                )
                """
            )
            params.extend([like, like, like, like, like, like, like, like, like, like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 50)))
        cursor = await self.db.execute(
            f"""
            SELECT ledger.*,
                   listings.status AS listing_status,
                   listings.seller_display_name AS listing_seller_display_name,
                   listings.seller_character_name AS listing_seller_character_name,
                   listings.seller_character_level AS listing_seller_character_level,
                   listings.buyer_display_name AS listing_buyer_display_name,
                   listings.buyer_character_name AS listing_buyer_character_name,
                   listings.buyer_character_level AS listing_buyer_character_level,
                   listings.voided_by_display_name AS listing_voided_by_display_name,
                   classifieds.seller_display_name AS classified_seller_display_name,
                   classifieds.seller_character_name AS classified_seller_character_name,
                   classifieds.seller_character_level AS classified_seller_character_level,
                   classifieds.buyer_display_name AS classified_buyer_display_name,
                   classifieds.buyer_character_name AS classified_buyer_character_name,
                   classifieds.buyer_character_level AS classified_buyer_character_level,
                   classifieds.voided_by_display_name AS classified_voided_by_display_name
            FROM ledger
            LEFT JOIN listings ON UPPER(ledger.listing_id) = UPPER(listings.listing_id)
            LEFT JOIN classifieds ON UPPER(ledger.listing_id) = UPPER(classifieds.classified_id)
            {where}
            ORDER BY ledger.id DESC
            LIMIT ?
            """,
            params,
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def export_table(self, table_name: str) -> list[dict[str, Any]]:
        """Return rows for a known export table."""
        if table_name not in {"listings", "ledger", "classifieds"}:
            raise ValueError(f"Unsupported export table: {table_name}")
        cursor = await self.db.execute(f"SELECT * FROM {table_name} ORDER BY id ASC")
        return [dict(row) for row in await cursor.fetchall()]

    async def create_classified(
        self,
        *,
        item_name: str,
        item_clean_name: str,
        rarity: str,
        source: str,
        category: str,
        tags: str,
        seller_user_id: str,
        seller_display_name: str,
        seller_character_name: str,
        seller_character_level: int,
        asking_price: int,
        broker_fee: int,
        buyer_total: int,
        commission_bps_locked: int = 2000,
        seller_tier_key_at_listing: str = "counter_stranger",
        seller_standing_gp_at_listing: int = 0,
        listing_display_name: str | None = None,
        base_item_name: str | None = None,
        variant: str | None = None,
        details: str | None = None,
        variant_type: str | None = None,
        variant_instructions: str | None = None,
        item_type: str | None = None,
        attunement: str | None = None,
        page: str | None = None,
        min_apl: int | None = None,
        minimum_tier: int | None = None,
        display_detail: str | None = None,
        short_description: str | None = None,
        rules_text: str | None = None,
        json_notes: str | None = None,
        item_tags: str | None = None,
    ) -> dict[str, Any]:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat(timespec="seconds")
        expires_at = (now_dt + timedelta(days=30)).isoformat(timespec="seconds")
        cursor = await self.db.execute(
            """
            INSERT INTO classifieds (
                classified_id, item_name, item_clean_name, listing_display_name,
                base_item_name, variant, details, rarity, source, category, tags,
                variant_type, variant_instructions, item_type, attunement, page,
                min_apl, minimum_tier, display_detail, short_description, rules_text, json_notes, item_tags,
                seller_user_id, seller_display_name, seller_character_name,
                seller_character_level, asking_price, broker_fee, buyer_total,
                commission_bps_locked, seller_tier_key_at_listing, seller_standing_gp_at_listing,
                status, created_at, expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (
                None,
                item_name,
                item_clean_name,
                listing_display_name,
                base_item_name,
                variant,
                details,
                rarity,
                source,
                category,
                tags,
                variant_type,
                variant_instructions,
                item_type,
                attunement,
                page,
                min_apl,
                minimum_tier,
                display_detail,
                short_description,
                rules_text,
                json_notes,
                item_tags,
                seller_user_id,
                seller_display_name,
                seller_character_name,
                seller_character_level,
                asking_price,
                broker_fee,
                buyer_total,
                commission_bps_locked,
                seller_tier_key_at_listing,
                seller_standing_gp_at_listing,
                now,
                expires_at,
            ),
        )
        row_id = cursor.lastrowid
        classified_id = f"DWC-{row_id:05d}"
        await self.db.execute(
            "UPDATE classifieds SET classified_id = ? WHERE id = ?",
            (classified_id, row_id),
        )
        await self.add_ledger_entry(
            entry_type="classified_post",
            listing_id=classified_id,
            item_name=item_name,
            cash_change=0,
            inventory_cost_change=0,
            profit_change=0,
            notes=(
                f"Classified posted by {seller_display_name} as "
                f"{seller_character_name} at {asking_price}gp buyer price with {broker_fee}gp "
                f"seller-paid commission locked at {commission_bps_locked} bps."
            ),
            commit=False,
        )
        await self.db.commit()
        return await self.get_classified(classified_id) or {}

    async def get_classified(self, classified_id: str) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM classifieds WHERE UPPER(classified_id) = UPPER(?)",
            (classified_id.strip(),),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_open_classifieds(self) -> list[dict[str, Any]]:
        now = utc_now_text()
        cursor = await self.db.execute(
            """
            SELECT * FROM classifieds
            WHERE status = 'open'
              AND returned_at IS NULL
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY id ASC
            """,
            (now,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def expired_open_classifieds(self, *, limit: int = 25) -> list[dict[str, Any]]:
        """Return open classifieds whose 30-day escrow has expired."""
        now = utc_now_text()
        cursor = await self.db.execute(
            """
            SELECT * FROM classifieds
            WHERE status = 'open'
              AND returned_at IS NULL
              AND expires_at IS NOT NULL
              AND expires_at <= ?
            ORDER BY expires_at ASC, id ASC
            LIMIT ?
            """,
            (now, max(1, min(int(limit), 100))),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def classified_return_notices_pending(self, *, limit: int = 25) -> list[dict[str, Any]]:
        """Return returned classifieds whose public return notice has not posted."""
        cursor = await self.db.execute(
            """
            SELECT * FROM classifieds
            WHERE returned_at IS NOT NULL
              AND return_notice_sent_at IS NULL
            ORDER BY returned_at ASC, id ASC
            LIMIT ?
            """,
            (max(1, min(int(limit), 100)),),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def mark_classified_sold(
        self,
        *,
        classified_id: str,
        buyer_user_id: str,
        buyer_display_name: str,
        buyer_character_name: str,
        buyer_character_level: int,
        trade_log_text: str,
    ) -> dict[str, Any] | bool:
        classified = await self.get_classified(classified_id)
        if classified is None or classified["status"] != "open":
            return False
        now = utc_now_text()
        if classified.get("returned_at"):
            return False
        if classified.get("expires_at") and classified["expires_at"] <= now:
            return False
        cursor = await self.db.execute(
            """
            UPDATE classifieds
            SET status = 'sold',
                buyer_user_id = ?,
                buyer_display_name = ?,
                buyer_character_name = ?,
                buyer_character_level = ?,
                trade_log_text = ?,
                sold_at = ?
            WHERE UPPER(classified_id) = UPPER(?)
              AND status = 'open'
              AND returned_at IS NULL
              AND (expires_at IS NULL OR expires_at > ?)
            """,
            (
                buyer_user_id,
                buyer_display_name,
                buyer_character_name,
                buyer_character_level,
                trade_log_text,
                now,
                classified_id,
                now,
            ),
        )
        if cursor.rowcount != 1:
            await self.db.rollback()
            return False
        await self.add_ledger_entry(
            entry_type="classified_fee",
            listing_id=classified["classified_id"],
            item_name=classified["item_name"],
            cash_change=int(classified["broker_fee"]),
            inventory_cost_change=0,
            profit_change=int(classified["broker_fee"]),
            notes=f"Seller-paid classified commission for {classified['item_name']}.",
            commit=False,
        )
        standing_result = await self.award_classified_standing(
            classified,
            buyer_user_id=buyer_user_id,
            buyer_character_name=buyer_character_name,
        )
        await self.db.commit()
        sold = await self.get_classified(classified["classified_id"]) or {}
        sold["_standing_result"] = standing_result
        return sold

    async def return_expired_classified(self, classified_id: str) -> dict[str, Any] | None:
        """Mark an expired open classified as returned to the seller."""
        classified = await self.get_classified(classified_id)
        now = utc_now_text()
        if (
            classified is None
            or classified["status"] != "open"
            or classified.get("returned_at")
            or not classified.get("expires_at")
            or classified["expires_at"] > now
        ):
            return None
        reason = "30-day classified hold expired; item returned to seller."
        cursor = await self.db.execute(
            """
            UPDATE classifieds
            SET status = 'voided',
                returned_at = ?,
                voided_at = ?,
                void_reason = ?
            WHERE UPPER(classified_id) = UPPER(?)
              AND status = 'open'
              AND returned_at IS NULL
            """,
            (now, now, reason, classified_id),
        )
        if cursor.rowcount != 1:
            await self.db.rollback()
            return None
        await self.add_ledger_entry(
            entry_type="classified_return",
            listing_id=classified["classified_id"],
            item_name=classified["item_name"],
            cash_change=0,
            inventory_cost_change=0,
            profit_change=0,
            notes=reason,
            commit=False,
        )
        await self.db.commit()
        return await self.get_classified(classified_id)

    async def mark_classified_return_notice_sent(self, classified_id: str) -> None:
        """Record that the public return notice has been posted."""
        await self.db.execute(
            """
            UPDATE classifieds
            SET return_notice_sent_at = ?
            WHERE UPPER(classified_id) = UPPER(?)
            """,
            (utc_now_text(), classified_id),
        )
        await self.db.commit()

    async def void_classified(
        self,
        classified_id: str,
        reason: str,
        *,
        voided_by_user_id: str | None = None,
        voided_by_display_name: str | None = None,
    ) -> dict[str, Any] | None:
        classified = await self.get_classified(classified_id)
        if classified is None or classified["status"] == "voided":
            return None
        await self.db.execute(
            """
            UPDATE classifieds
            SET status = 'voided',
                voided_at = ?,
                void_reason = ?,
                voided_by_user_id = ?,
                voided_by_display_name = ?
            WHERE UPPER(classified_id) = UPPER(?)
            """,
            (utc_now_text(), reason.strip(), voided_by_user_id, voided_by_display_name, classified_id),
        )
        await self.add_ledger_entry(
            entry_type="classified_void",
            listing_id=classified["classified_id"],
            item_name=classified["item_name"],
            cash_change=0,
            inventory_cost_change=0,
            profit_change=0,
            notes=(
                f"Voided classified"
                f"{f' by {voided_by_display_name} ({voided_by_user_id})' if voided_by_display_name else ''}: "
                f"{reason.strip()}"
            ),
            commit=False,
        )
        await self.db.commit()
        return await self.get_classified(classified_id)
