"""SQLite storage for Dwarfy's inventory and ledger."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite


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
                void_reason TEXT
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
                void_reason TEXT
            )
            """
        )
        await self._migrate_listings_table()
        await self._migrate_classifieds_table()
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
            "min_apl": "ALTER TABLE classifieds ADD COLUMN min_apl INTEGER",
            "minimum_tier": "ALTER TABLE classifieds ADD COLUMN minimum_tier INTEGER",
        }
        for column, statement in migrations.items():
            if column not in existing_columns:
                await self.db.execute(statement)

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

    async def void_listing(self, listing_id: str, reason: str) -> dict[str, Any] | None:
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
            SET status = 'voided', item_status = 'voided', voided_at = ?, void_reason = ?
            WHERE UPPER(listing_id) = UPPER(?)
            """,
            (utc_now_text(), reason.strip(), listing_id),
        )
        await self.add_ledger_entry(
            entry_type="void",
            listing_id=listing["listing_id"],
            item_name=listing["item_name"],
            cash_change=0,
            inventory_cost_change=inventory_reversal,
            profit_change=profit_reversal,
            notes=f"Voided listing that was {previous_status}: {reason.strip()}",
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
                void_reason = ?
            WHERE status = 'available'
              AND COALESCE(item_status, 'inventory') = 'inventory'
              AND stock_source = 'owner_stock'
            """,
            (now, clean_reason),
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
                )
                """
            )
            params.extend([like, like, like, like, like, like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 50)))
        cursor = await self.db.execute(
            f"""
            SELECT ledger.*,
                   listings.status AS listing_status,
                   listings.seller_display_name,
                   listings.buyer_display_name
            FROM ledger
            LEFT JOIN listings ON UPPER(ledger.listing_id) = UPPER(listings.listing_id)
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
                status, created_at, expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
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
                f"{seller_character_name} at {asking_price}gp buyer price with {broker_fee}gp seller-paid commission."
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
    ) -> bool:
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
        await self.db.commit()
        return True

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

    async def void_classified(self, classified_id: str, reason: str) -> dict[str, Any] | None:
        classified = await self.get_classified(classified_id)
        if classified is None or classified["status"] == "voided":
            return None
        await self.db.execute(
            """
            UPDATE classifieds
            SET status = 'voided', voided_at = ?, void_reason = ?
            WHERE UPPER(classified_id) = UPPER(?)
            """,
            (utc_now_text(), reason.strip(), classified_id),
        )
        await self.add_ledger_entry(
            entry_type="classified_void",
            listing_id=classified["classified_id"],
            item_name=classified["item_name"],
            cash_change=0,
            inventory_cost_change=0,
            profit_change=0,
            notes=f"Voided classified: {reason.strip()}",
            commit=False,
        )
        await self.db.commit()
        return await self.get_classified(classified_id)
