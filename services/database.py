"""SQLite storage for Dwarfy's inventory and ledger."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite


def utc_now_text() -> str:
    """Store timestamps in one predictable UTC format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
                status TEXT NOT NULL CHECK (status IN ('available', 'sold', 'voided')),
                buyer_user_id TEXT,
                buyer_display_name TEXT,
                buyer_character_name TEXT,
                buyer_character_level INTEGER,
                buy_price_roll_detail TEXT,
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
        await self.db.commit()

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
    ) -> dict[str, Any]:
        """Create an available listing after a successful player sale."""
        now = utc_now_text()
        cursor = await self.db.execute(
            """
            INSERT INTO listings (
                listing_id, item_name, rarity, source, category, tags,
                seller_user_id, seller_display_name, seller_character_name,
                seller_character_level, sell_roll, seller_payout, cost_basis,
                status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'available', ?)
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
                seller_payout,
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
            entry_type="seller_payout",
            listing_id=listing_id,
            item_name=item_name,
            cash_change=-seller_payout,
            inventory_cost_change=seller_payout,
            profit_change=0,
            notes=f"Paid seller for {item_name}.",
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
    ) -> bool:
        """Mark an available listing as sold and write the receipt to ledger."""
        listing = await self.get_listing(listing_id)
        if listing is None or listing["status"] != "available":
            return False

        now = utc_now_text()
        cursor = await self.db.execute(
            """
            UPDATE listings
            SET status = 'sold',
                buyer_user_id = ?,
                buyer_display_name = ?,
                buyer_character_name = ?,
                buyer_character_level = ?,
                buy_price_roll_detail = ?,
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
                buy_price_roll_detail,
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
            SET status = 'voided', voided_at = ?, void_reason = ?
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
        return stats
