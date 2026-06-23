"""Slash commands for Dwarfy's Shop."""

from __future__ import annotations

import asyncio
import random
import re
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from services.pricing import (
    buy_price_formula,
    is_supported_rarity,
    possible_final_price_range,
    roll_buy_price,
    roll_sell_price,
)
from services.sheets import format_item_choices, normalize_rarity
from utils.formatting import (
    character_label,
    gp,
    mention_user,
    price_range_text,
    send_text_response,
)


LISTING_ID_RE = re.compile(r"^DWF-\d+$", re.IGNORECASE)

DISASTER_MESSAGES = [
    "The broker never returns. The rented desk is empty, the references were false, and the item is unrecoverable.",
    "The buyer's promissory note turns out to be theatre paper, and the courier has already vanished into the crowd.",
    "The appraiser insists on one final private inspection. By sunset, both appraiser and item are gone.",
    "A forged guild seal, a fake contract, and one very convincing handshake leave nothing behind but embarrassment.",
]


def _display_name(user: discord.abc.User) -> str:
    return getattr(user, "display_name", user.name)


def _valid_listing_id(value: str) -> bool:
    return bool(LISTING_ID_RE.fullmatch(value.strip()))


class Dwarfy(commands.GroupCog, name="dwarfy"):
    """Commands under the /dwarfy group."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        super().__init__()

    def _is_admin_or_mod(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        role_names = {
            role.name.casefold()
            for role in getattr(member, "roles", [])
        }
        return bool(role_names.intersection(self.bot.config.admin_role_names))

    async def _require_admin(self, interaction: discord.Interaction) -> bool:
        if self._is_admin_or_mod(interaction):
            return True
        await interaction.response.send_message(
            "Only a configured admin/mod role can use that command.",
            ephemeral=True,
        )
        return False

    async def _require_channel(
        self,
        interaction: discord.Interaction,
        channel_id: int | None,
        env_name: str,
    ) -> bool:
        if channel_id is None:
            await interaction.response.send_message(
                f"This command is not configured yet. Set {env_name} in `.env`.",
                ephemeral=True,
            )
            return False
        if interaction.channel_id != channel_id:
            await interaction.response.send_message(
                "Dwarfy points at the correct counter. Please use this command in the configured channel.",
                ephemeral=True,
            )
            return False
        return True

    async def _require_sheet_cache(self, interaction: discord.Interaction) -> bool:
        if self.bot.sheet_cache.loaded:
            return True
        await interaction.response.send_message(
            "Google Sheet data is not loaded yet. Ask an admin/mod to run `/dwarfy reload`, or check the bot terminal logs.",
            ephemeral=True,
        )
        return False

    @app_commands.command(name="ping", description="Check whether Dwarfy's Shop is open.")
    async def ping(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("Dwarfy's Shop is open.")

    @app_commands.command(name="sell", description="Sell a permanent magic item to Dwarfy's Shop.")
    @app_commands.describe(
        character="Your character's name.",
        level="Your character's level.",
        item="The magic item you are selling.",
    )
    async def sell(
        self,
        interaction: discord.Interaction,
        character: str,
        level: app_commands.Range[int, 1, 20],
        item: str,
    ) -> None:
        if not await self._require_channel(
            interaction,
            self.bot.config.dwarfy_sell_channel_id,
            "DWARFY_SELL_CHANNEL_ID",
        ):
            return
        if not await self._require_sheet_cache(interaction):
            return

        match = self.bot.sheet_cache.match_item(item)
        if match.choices:
            await interaction.response.send_message(
                "I found multiple possible item matches. Please run the command again with one exact item name:\n"
                f"{format_item_choices(match.choices)}",
                ephemeral=True,
            )
            return
        if match.item is None:
            await interaction.response.send_message(
                f"I could not find `{item}` in the cached Bot Items sheet.",
                ephemeral=True,
            )
            return

        sheet_item = match.item
        if not sheet_item.allowed:
            await interaction.response.send_message(
                f"`{sheet_item.name}` exists in the sheet, but Allowed is FALSE.",
                ephemeral=True,
            )
            return
        if sheet_item.consumable:
            await interaction.response.send_message(
                f"`{sheet_item.name}` is marked Consumable=TRUE. Dwarfy only buys permanent magic items.",
                ephemeral=True,
            )
            return
        if not is_supported_rarity(sheet_item.rarity):
            await interaction.response.send_message(
                f"Dwarfy cannot price `{sheet_item.rarity}` items in version 1. Supported rarities: Common, Uncommon, Rare, Very Rare, Legendary.",
                ephemeral=True,
            )
            return

        seller = interaction.user.mention
        seller_character = character_label(character, int(level))
        sell_roll = roll_sell_price(sheet_item.rarity)
        declaration = (
            f"{seller} declares that {seller_character} owns {sheet_item.name} and spends 5 DTP and 25gp "
            "to sell it through Dwarfy's Shop."
        )

        if sell_roll.roll == 1:
            output = (
                f"{declaration}\n"
                f"{seller} as {seller_character} receives 0gp for {sheet_item.name}.\n\n"
                "Sell Magic Item downtime:\n\n"
                "* DTP cost: 5\n"
                "* Gold cost: 25gp\n"
                "* Flat d20 roll: 1\n"
                "* Result: Sale disaster\n\n"
                f"{random.choice(DISASTER_MESSAGES)}\n\n"
                "Dwarfy's Shop receives no inventory.\n"
                "Dwarfy's Shop makes 0gp.\n"
                "Sale status: Final. The item is lost.\n\n"
                "Adventure log reminder:\n"
                "Record this downtime activity manually on the character's adventure log."
            )
            await send_text_response(interaction, output)
            return

        listing = await self.bot.db.create_listing(
            item_name=sheet_item.name,
            rarity=sheet_item.rarity,
            source=sheet_item.source,
            category=sheet_item.category,
            tags=sheet_item.tags_text,
            seller_user_id=str(interaction.user.id),
            seller_display_name=_display_name(interaction.user),
            seller_character_name=character.strip(),
            seller_character_level=int(level),
            sell_roll=sell_roll.roll,
            seller_payout=sell_roll.seller_payout,
        )

        output = (
            f"{declaration}\n"
            f"{seller} as {seller_character} sells {sheet_item.name} to Dwarfy's Shop for {gp(sell_roll.seller_payout)}.\n"
            f"Dwarfy's Shop receives {sheet_item.name} from {seller} as {seller_character} and adds it to magic inventory as {listing['listing_id']}.\n\n"
            "Sell Magic Item downtime:\n\n"
            "* DTP cost: 5\n"
            "* Gold cost: 25gp\n"
            f"* Flat d20 roll: {sell_roll.roll}\n"
            f"* Result: {sell_roll.result_text}\n"
            f"* Base price: {gp(sell_roll.base_price)}\n"
            f"* Seller payout: {gp(sell_roll.seller_payout)}\n"
            f"* Dwarfy's cost basis: {gp(sell_roll.seller_payout)}\n"
            "* Future sale price: rolled when purchased, never below Dwarfy's cost basis\n"
            "* Sale status: Final, no takebacks\n\n"
            "Adventure log reminder:\n"
            "Record this downtime activity manually on the character's adventure log."
        )
        await send_text_response(interaction, output)

    @app_commands.command(name="browse", description="Browse magic items Dwarfy has for sale.")
    @app_commands.describe(
        rarity="Optional rarity filter.",
        max_price="Only show listings whose maximum possible final price is this amount or lower.",
        search="Search item name, source, category, or tags.",
    )
    async def browse(
        self,
        interaction: discord.Interaction,
        rarity: str | None = None,
        max_price: int | None = None,
        search: str | None = None,
    ) -> None:
        if not await self._require_channel(
            interaction,
            self.bot.config.dwarfy_shop_channel_id,
            "DWARFY_SHOP_CHANNEL_ID",
        ):
            return
        if max_price is not None and max_price < 0:
            await interaction.response.send_message(
                "`max_price` must be 0 or higher.",
                ephemeral=True,
            )
            return

        listings = await self.bot.db.list_available_listings()
        rarity_filter = normalize_rarity(rarity) if rarity else None
        search_filter = search.casefold().strip() if search else None

        filtered: list[tuple[dict[str, Any], int, int]] = []
        for listing in listings:
            if rarity_filter and listing["rarity"] != rarity_filter:
                continue
            searchable = " ".join(
                str(listing.get(field) or "")
                for field in ("item_name", "source", "category", "tags")
            ).casefold()
            if search_filter and search_filter not in searchable:
                continue
            low, high = possible_final_price_range(
                listing["rarity"],
                int(listing["cost_basis"]),
            )
            if max_price is not None and high > max_price:
                continue
            filtered.append((listing, low, high))

        if not filtered:
            await interaction.response.send_message(
                "Dwarfy's Shop has no available magic items matching those filters.",
                ephemeral=True,
            )
            return

        shown = filtered[:10]
        lines = [
            f"Dwarfy's Shop currently has {len(filtered)} magic item{'s' if len(filtered) != 1 else ''} for sale:",
            "",
        ]
        for listing, low, high in shown:
            seller = mention_user(listing["seller_user_id"], listing["seller_display_name"])
            seller_character = character_label(
                listing["seller_character_name"],
                listing["seller_character_level"],
            )
            lines.extend(
                [
                    f"{listing['listing_id']} \u2014 {listing['item_name']} \u2014 {listing['rarity']}",
                    (
                        f"Source: {listing['source'] or 'Unknown'} | Price on buy: "
                        f"{price_range_text(low, high)} | Original seller: {seller} as {seller_character}"
                    ),
                    "",
                ]
            )

        lines.append(f"Showing {len(shown)} of {len(filtered)} matching listings.")
        if len(filtered) > 10:
            lines.append("Use rarity, max_price, or search filters to narrow the list.")

        await send_text_response(interaction, "\n".join(lines))

    @app_commands.command(name="inspect", description="Inspect one Dwarfy listing.")
    @app_commands.describe(listing="Listing ID, such as DWF-00017.")
    async def inspect(self, interaction: discord.Interaction, listing: str) -> None:
        if not await self._require_channel(
            interaction,
            self.bot.config.dwarfy_shop_channel_id,
            "DWARFY_SHOP_CHANNEL_ID",
        ):
            return
        if not _valid_listing_id(listing):
            await interaction.response.send_message(
                "Please inspect by listing ID, like `DWF-00017`.",
                ephemeral=True,
            )
            return

        row = await self.bot.db.get_listing(listing)
        if row is None:
            await interaction.response.send_message(
                f"I could not find listing `{listing.upper()}`.",
                ephemeral=True,
            )
            return

        await send_text_response(interaction, self._format_inspect(row))

    def _format_inspect(self, listing: dict[str, Any]) -> str:
        seller = mention_user(listing["seller_user_id"], listing["seller_display_name"])
        seller_character = character_label(
            listing["seller_character_name"],
            listing["seller_character_level"],
        )
        low, high = possible_final_price_range(
            listing["rarity"],
            int(listing["cost_basis"]),
        )

        lines = [
            f"Listing: {listing['listing_id']}",
            f"Item: {listing['item_name']}",
            f"Rarity: {listing['rarity']}",
            f"Source: {listing['source'] or 'Unknown'}",
            f"Category: {listing['category'] or 'none'}",
            f"Tags: {listing['tags'] or 'none'}",
            f"Original seller: {seller} as {seller_character}",
            f"Dwarfy cost basis: {gp(int(listing['cost_basis']))}",
            f"Buying price formula: {buy_price_formula(listing['rarity'])}",
            f"Possible final price: {price_range_text(low, high)}",
            f"Status: {listing['status']}",
        ]

        if listing["status"] == "sold":
            buyer = mention_user(listing["buyer_user_id"], listing["buyer_display_name"])
            buyer_character = character_label(
                listing["buyer_character_name"],
                listing["buyer_character_level"],
            )
            lines.extend(
                [
                    f"Buyer: {buyer} as {buyer_character}",
                    f"Final sale price: {gp(int(listing['final_sale_price'] or 0))}",
                    f"Realized Dwarfy profit: {gp(int(listing['realized_profit'] or 0))}",
                    f"Sold at: {listing['sold_at']}",
                ]
            )
        elif listing["status"] == "voided":
            lines.extend(
                [
                    f"Voided at: {listing['voided_at']}",
                    f"Void reason: {listing['void_reason'] or 'No reason recorded.'}",
                ]
            )

        return "\n".join(lines)

    @app_commands.command(name="buy", description="Buy an available magic item from Dwarfy.")
    @app_commands.describe(
        listing="Listing ID only, such as DWF-00017.",
        character="Your character's name.",
        level="Your character's level.",
    )
    async def buy(
        self,
        interaction: discord.Interaction,
        listing: str,
        character: str,
        level: app_commands.Range[int, 1, 20],
    ) -> None:
        if not await self._require_channel(
            interaction,
            self.bot.config.dwarfy_shop_channel_id,
            "DWARFY_SHOP_CHANNEL_ID",
        ):
            return
        if not _valid_listing_id(listing):
            await interaction.response.send_message(
                "Please buy by listing ID only, like `DWF-00017`.",
                ephemeral=True,
            )
            return

        row = await self.bot.db.get_listing(listing)
        if row is None:
            await interaction.response.send_message(
                f"I could not find listing `{listing.upper()}`.",
                ephemeral=True,
            )
            return
        if row["status"] != "available":
            await interaction.response.send_message(
                f"`{row['listing_id']}` is already {row['status']} and cannot be bought.",
                ephemeral=True,
            )
            return

        buy_roll = roll_buy_price(row["rarity"], int(row["cost_basis"]))
        sold = await self.bot.db.mark_listing_sold(
            listing_id=row["listing_id"],
            buyer_user_id=str(interaction.user.id),
            buyer_display_name=_display_name(interaction.user),
            buyer_character_name=character.strip(),
            buyer_character_level=int(level),
            buy_price_roll_detail=buy_roll.roll_detail,
            final_sale_price=buy_roll.final_price,
            realized_profit=buy_roll.realized_profit,
        )
        if not sold:
            await interaction.response.send_message(
                "That listing is no longer available. Please browse again.",
                ephemeral=True,
            )
            return

        buyer = interaction.user.mention
        buyer_character = character_label(character, int(level))
        seller = mention_user(row["seller_user_id"], row["seller_display_name"])
        seller_character = character_label(
            row["seller_character_name"],
            row["seller_character_level"],
        )

        output = (
            f"{buyer} as {buyer_character} spends 5 DTP and 100gp seeking to buy a magic item from Dwarfy's Shop.\n"
            f"{buyer} as {buyer_character} pays {gp(buy_roll.final_price)} to Dwarfy's Shop for {row['item_name']}.\n"
            f"Dwarfy's Shop receives {gp(buy_roll.final_price)} from {buyer} as {buyer_character} for {row['item_name']}.\n\n"
            "Buying Magic Item receipt:\n\n"
            f"* Listing: {row['listing_id']}\n"
            f"* Item: {row['item_name']}\n"
            f"* Rarity: {row['rarity']}\n"
            f"* Source: {row['source'] or 'Unknown'}\n"
            f"* Original seller: {seller} as {seller_character}\n"
            f"* Dwarfy's cost basis: {gp(int(row['cost_basis']))}\n"
            f"* Asking price roll: {buy_roll.roll_detail}\n"
            f"* Final item price: {gp(buy_roll.final_price)}\n"
            f"* Realized Dwarfy profit: {gp(buy_roll.realized_profit)}\n"
            "* Purchase status: Final, no takebacks\n\n"
            "Adventure log reminder:\n"
            "Record this downtime activity manually on the character's adventure log."
        )
        await send_text_response(interaction, output)

    @app_commands.command(name="stats", description="Show Dwarfy's inventory and ledger stats.")
    async def stats(self, interaction: discord.Interaction) -> None:
        if not await self._require_admin(interaction):
            return

        stats = await self.bot.db.shop_stats()
        best = stats["most_profitable_flip"]
        best_text = (
            f"{best['listing_id']} \u2014 {best['item_name']} \u2014 {gp(int(best['realized_profit']))}"
            if best
            else "none"
        )
        output = (
            "Dwarfy's Shop stats\n\n"
            f"* Available inventory count: {stats['available_count']}\n"
            f"* Sold listing count: {stats['sold_count']}\n"
            f"* Voided listing count: {stats['voided_count']}\n"
            f"* Total paid to sellers: {gp(stats['total_paid_to_sellers'])}\n"
            f"* Total received from buyers: {gp(stats['total_received_from_buyers'])}\n"
            f"* Realized profit from completed flips: {gp(stats['realized_profit'])}\n"
            f"* Available inventory cost basis: {gp(stats['available_inventory_cost_basis'])}\n"
            f"* Business cash flow from magic-item transactions: {gp(stats['business_cash_flow'])}\n"
            f"* Most profitable completed flip: {best_text}"
        )
        await send_text_response(interaction, output, ephemeral=True)

    @app_commands.command(name="void", description="Void a listing for correction or abuse cleanup.")
    @app_commands.describe(
        listing="Listing ID, such as DWF-00017.",
        reason="Why this listing is being voided.",
    )
    async def void(self, interaction: discord.Interaction, listing: str, reason: str) -> None:
        if not await self._require_admin(interaction):
            return
        if not _valid_listing_id(listing):
            await interaction.response.send_message(
                "Please void by listing ID, like `DWF-00017`.",
                ephemeral=True,
            )
            return

        row = await self.bot.db.void_listing(listing, reason)
        if row is None:
            await interaction.response.send_message(
                f"I could not find an active listing `{listing.upper()}` to void.",
                ephemeral=True,
            )
            return

        public_channels = {
            channel_id
            for channel_id in (
                self.bot.config.dwarfy_sell_channel_id,
                self.bot.config.dwarfy_shop_channel_id,
            )
            if channel_id is not None
        }
        should_be_ephemeral = interaction.channel_id not in public_channels
        output = (
            f"Dwarfy listing voided: {row['listing_id']}\n"
            f"Item: {row['item_name']}\n"
            f"Reason: {row['void_reason']}\n"
            "The record was not deleted."
        )
        await send_text_response(interaction, output, ephemeral=should_be_ephemeral)

    @app_commands.command(name="reload", description="Reload Google Sheet data.")
    async def reload(self, interaction: discord.Interaction) -> None:
        if not await self._require_admin(interaction):
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await asyncio.to_thread(self.bot.sheet_cache.reload)
        except Exception as exc:
            await interaction.followup.send(
                f"Google Sheet reload failed: {exc}",
                ephemeral=True,
            )
            return

        warnings = self.bot.sheet_cache.warnings
        warning_text = "\n".join(f"- {warning}" for warning in warnings) if warnings else "none"
        output = (
            "Google Sheet cache reloaded.\n\n"
            f"* Bot Items rows loaded: {len(self.bot.sheet_cache.items)}\n"
            f"* Monster Component rows loaded: {len(self.bot.sheet_cache.components)}\n"
            f"* Validation warnings:\n{warning_text}"
        )
        await interaction.followup.send(output, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Dwarfy(bot))
