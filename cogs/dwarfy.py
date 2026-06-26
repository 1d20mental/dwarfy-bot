"""Slash commands for Dwarfy's Shop."""

from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from services.pricing import (
    buy_price_formula,
    direct_sell_price,
    is_supported_rarity,
    possible_final_price_range,
    roll_broker_price,
    roll_buy_price,
)
from services.loot import RARITY_ORDER, pick_weighted_item
from services.sheets import (
    format_item_choices,
    is_generic_template_item,
    item_detail_summary,
    looks_like_pasted_detail_text,
    looks_like_pasted_item_text,
    normalize_rarity,
)
from utils.formatting import (
    character_label,
    gp,
    mention_user,
    price_range_text,
    send_text_response,
)


LISTING_ID_RE = re.compile(r"\bDWF\s*[-\u2010-\u2015\u2212]?\s*(\d+)\b", re.IGNORECASE)
BROWSE_RARITY_CHOICES = [
    app_commands.Choice(name="Common", value="Common"),
    app_commands.Choice(name="Uncommon", value="Uncommon"),
    app_commands.Choice(name="Rare", value="Rare"),
    app_commands.Choice(name="Very Rare", value="Very Rare"),
    app_commands.Choice(name="Legendary", value="Legendary"),
]
BROWSE_RARITY_VALUES = {choice.value for choice in BROWSE_RARITY_CHOICES}
BROWSE_LISTING_CAP = 100
BROWSE_PAGE_SIZE = 10
RARITY_COLORS = {
    "Common": 0x8A8F98,
    "Uncommon": 0x2ECC71,
    "Rare": 0x3498DB,
    "Very Rare": 0x9B59B6,
    "Legendary": 0xF1C40F,
}

DISASTER_MESSAGES = [
    "The broker never returns. The rented desk is empty, the references were false, and the item is unrecoverable.",
    "The buyer's promissory note turns out to be theatre paper, and the courier has already vanished into the crowd.",
    "The appraiser insists on one final private inspection. By sunset, both appraiser and item are gone.",
    "A forged guild seal, a fake contract, and one very convincing handshake leave nothing behind but embarrassment.",
]

STOCK_PERMANENT_RARITY_TABLE = [
    (1, 20, "Common"),
    (21, 70, "Uncommon"),
    (71, 93, "Rare"),
    (94, 99, "Very Rare"),
    (100, 100, "Legendary"),
]
STOCK_CONSUMABLE_RARITY_TABLE = [
    (1, 30, "Common"),
    (31, 75, "Uncommon"),
    (76, 95, "Rare"),
    (96, 100, "Very Rare"),
]
RANDOM_AMMUNITION_VARIANTS = ["20 arrows", "20 bolts", "10 bullets"]
RANDOM_WEAPON_VARIANTS = [
    "Longsword",
    "Rapier",
    "Shortsword",
    "Scimitar",
    "Battleaxe",
    "Warhammer",
    "Longbow",
    "Shortbow",
    "Light Crossbow",
    "Heavy Crossbow",
]
RANDOM_ARMOR_VARIANTS = [
    "Leather Armor",
    "Studded Leather Armor",
    "Chain Shirt",
    "Scale Mail",
    "Breastplate",
    "Half Plate",
    "Chain Mail",
    "Splint Armor",
    "Plate Armor",
]


def _display_name(user: discord.abc.User) -> str:
    return getattr(user, "display_name", user.name)


def parse_listing_id(value: str) -> str | None:
    """Extract and normalize a Dwarfy listing ID from user input.

    Discord users often paste the whole browse line or a smart punctuation
    variant. This still keeps buying by listing ID only, but makes copy/paste
    less fussy.
    """
    match = LISTING_ID_RE.search(value.strip().strip("`"))
    if match is None:
        return None
    number = int(match.group(1))
    return f"DWF-{number:05d}"


def _valid_listing_id(value: str) -> bool:
    return parse_listing_id(value) is not None


def resolved_listing_name(item_name: str, variant: str | None = None) -> str:
    """Return the player-facing listing name, including variant identity text."""
    clean_variant = (variant or "").strip()
    if clean_variant:
        return f"{item_name} ({clean_variant})"
    return item_name


def listing_display_name(listing: dict[str, Any]) -> str:
    """Use the new display name column, with old rows still readable."""
    return (
        listing.get("listing_display_name")
        or listing.get("item_name")
        or listing.get("item_clean_name")
        or "Unknown item"
    )


def source_with_page(source: str | None, page: str | None) -> str:
    if source and page:
        clean_page = page.strip()
        page_text = clean_page if clean_page.casefold().startswith("p") else f"p. {clean_page}"
        return f"{source}, {page_text}"
    return source or "Unknown"


def broker_sale_result_line(sale: Any) -> str:
    """Put the broker d20 result near the top of the public receipt."""
    return f"🎲 Broker roll: {sale.roll} - {sale.result_text} - payout {gp(sale.seller_payout)}."


def buy_haggling_result_line(buy_roll: Any, cost_basis: int) -> str:
    """Put the buy haggling result near the top of the public receipt."""
    line = (
        f"🎲 Dwarfy haggling roll: {buy_roll.haggling_roll} - "
        f"{buy_roll.haggling_result} Final item price: {gp(buy_roll.final_price)}."
    )
    if buy_roll.cost_basis_floor_applied:
        line += f" **Dwarfy will not sell below his {gp(cost_basis)} cost basis.**"
    return line


def discount_text(discount_percent: int) -> str:
    """Return a human-readable discount line for buy receipts."""
    if discount_percent <= 0:
        return "none"
    return f"{discount_percent}%"


def listing_origin_text(listing: dict[str, Any]) -> str:
    """Return the readable source of a listing."""
    if listing.get("stock_source") == "owner_stock":
        return "Dwarfy stock"
    seller = mention_user(listing.get("seller_user_id"), listing.get("seller_display_name"))
    seller_character = character_label(
        listing.get("seller_character_name") or listing.get("seller_character"),
        listing.get("seller_character_level") or listing.get("seller_level"),
    )
    return f"{seller} as {seller_character}"


def truncate_text(value: str, limit: int) -> str:
    """Return a Discord-safe shortened label."""
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def build_browse_output(
    listings_with_prices: list[tuple[dict[str, Any], int, int]],
    *,
    cap: int = BROWSE_LISTING_CAP,
) -> str:
    """Build the private shop browse text.

    Browse is intentionally private, so showing the full matching shelf is more
    useful than forcing users through pages. The cap is a guardrail for very
    large shops; send_text_response will still split this into Discord-sized
    messages.
    """
    shown = listings_with_prices[:cap]
    total = len(listings_with_prices)
    lines = [
        f"Dwarfy's Shop currently has {total} matching magic item{'s' if total != 1 else ''} for sale:",
        "",
    ]
    for listing, low, high in shown:
        display_name = listing_display_name(listing)
        origin = listing_origin_text(listing)
        lines.extend(
            [
                f"{listing['listing_id']} \u2014 {display_name} \u2014 {listing['rarity']}",
                (
                    f"Source: {listing['source'] or 'Unknown'} | Price on buy: "
                    f"{price_range_text(low, high)} | Origin: {origin}"
                ),
                "",
            ]
        )

    if total > cap:
        lines.append(f"Showing first {cap} of {total} matching listings.")
        lines.append("Use rarity, max_price, or search filters to narrow the list.")
    else:
        lines.append(f"Showing all {total} matching listings.")
    return "\n".join(lines)


def browse_page_count(
    listings_with_prices: list[tuple[dict[str, Any], int, int]],
    *,
    page_size: int = BROWSE_PAGE_SIZE,
    cap: int = BROWSE_LISTING_CAP,
) -> int:
    """Return the number of embed pages needed for a browse result."""
    shown_count = min(len(listings_with_prices), cap)
    return max(1, (shown_count + page_size - 1) // page_size)


def browse_embed_color(page_entries: list[tuple[dict[str, Any], int, int]]) -> discord.Color:
    """Pick a stable embed accent color from the rarity mix on a page."""
    rarities = {listing.get("rarity") for listing, _low, _high in page_entries}
    if len(rarities) == 1:
        rarity = next(iter(rarities))
        return discord.Color(RARITY_COLORS.get(rarity, 0xC9A227))
    return discord.Color(0xC9A227)


def build_browse_embed(
    listings_with_prices: list[tuple[dict[str, Any], int, int]],
    *,
    page_index: int = 0,
    page_size: int = BROWSE_PAGE_SIZE,
    cap: int = BROWSE_LISTING_CAP,
) -> discord.Embed:
    """Build one polished private browse page."""
    shown = listings_with_prices[:cap]
    total = len(listings_with_prices)
    page_total = browse_page_count(listings_with_prices, page_size=page_size, cap=cap)
    safe_page = min(max(page_index, 0), page_total - 1)
    start = safe_page * page_size
    end = start + page_size
    page_entries = shown[start:end]

    if total > cap:
        description = (
            f"{total} matching listings. Showing first {cap}; use filters to narrow further."
        )
    else:
        description = f"{total} matching listing{'s' if total != 1 else ''}."

    embed = discord.Embed(
        title="Dwarfy's Shop",
        description=description,
        color=browse_embed_color(page_entries),
    )
    for listing, low, high in page_entries:
        display_name = listing_display_name(listing)
        origin = listing_origin_text(listing)
        field_name = truncate_text(f"{listing['listing_id']} - {display_name}", 256)
        field_value = (
            f"{listing['rarity']} | {listing.get('source') or 'Unknown'} | "
            f"Price on buy: {price_range_text(low, high)}\n"
            f"Origin: {origin}"
        )
        embed.add_field(
            name=field_name,
            value=truncate_text(field_value, 1024),
            inline=False,
        )

    embed.set_footer(text=f"Page {safe_page + 1} of {page_total}")
    return embed


class BrowseView(discord.ui.View):
    """Private button controls for browsing Dwarfy inventory."""

    def __init__(
        self,
        listings_with_prices: list[tuple[dict[str, Any], int, int]],
        *,
        owner_id: int,
        page_size: int = BROWSE_PAGE_SIZE,
        cap: int = BROWSE_LISTING_CAP,
    ) -> None:
        super().__init__(timeout=600)
        self.listings_with_prices = listings_with_prices
        self.owner_id = owner_id
        self.page_size = page_size
        self.cap = cap
        self.page_index = 0
        self.sync_buttons()

    @property
    def page_total(self) -> int:
        return browse_page_count(
            self.listings_with_prices,
            page_size=self.page_size,
            cap=self.cap,
        )

    def current_embed(self) -> discord.Embed:
        return build_browse_embed(
            self.listings_with_prices,
            page_index=self.page_index,
            page_size=self.page_size,
            cap=self.cap,
        )

    def button_by_id(self, custom_id: str) -> discord.ui.Button | None:
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id == custom_id:
                return child
        return None

    def sync_buttons(self) -> None:
        previous_button = self.button_by_id("dwarfy_browse_previous")
        next_button = self.button_by_id("dwarfy_browse_next")
        show_all_button = self.button_by_id("dwarfy_browse_show_all")
        if previous_button is not None:
            previous_button.disabled = self.page_index <= 0
        if next_button is not None:
            next_button.disabled = self.page_index >= self.page_total - 1
        if show_all_button is not None:
            show_all_button.disabled = len(self.listings_with_prices) <= self.page_size

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "This browse panel belongs to the person who opened it.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="Previous",
        style=discord.ButtonStyle.secondary,
        custom_id="dwarfy_browse_previous",
    )
    async def previous_page(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.page_index = max(0, self.page_index - 1)
        self.sync_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(
        label="Next",
        style=discord.ButtonStyle.primary,
        custom_id="dwarfy_browse_next",
    )
    async def next_page(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.page_index = min(self.page_total - 1, self.page_index + 1)
        self.sync_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(
        label="Show All",
        style=discord.ButtonStyle.secondary,
        custom_id="dwarfy_browse_show_all",
    )
    async def show_all(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await send_text_response(
            interaction,
            build_browse_output(self.listings_with_prices, cap=self.cap),
            ephemeral=True,
        )


def new_stock_batch_id() -> str:
    """Return a compact batch ID for owner-stock operations."""
    return datetime.now(timezone.utc).strftime("STOCK-%Y%m%d-%H%M%S")


def stock_rarity_from_roll(d100: int, *, consumable: bool) -> str:
    """Roll on Dwarfy's owner-stock rarity table."""
    table = STOCK_CONSUMABLE_RARITY_TABLE if consumable else STOCK_PERMANENT_RARITY_TABLE
    for low, high, rarity in table:
        if low <= d100 <= high:
            return rarity
    return table[-1][2]


def stock_rarity_fallback_order(rarity: str) -> list[str]:
    """Return rarity fallback order for stock generation."""
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


def stock_item_pool(
    *,
    cache: Any,
    rarity: str,
    consumable: bool,
    apl: int,
    tag: str | None = None,
) -> tuple[str | None, list[Any]]:
    """Return a stockable item pool, using nearest-rarity fallback if needed."""
    tag_norm = tag.casefold().strip() if tag else None
    for candidate_rarity in stock_rarity_fallback_order(rarity):
        pool = [
            item
            for item in cache.loot_pool(
                rarity=candidate_rarity,
                consumable=consumable,
                apl=apl,
            )
            if item.loot_type == "Item" and is_supported_rarity(item.rarity)
        ]
        if tag_norm:
            tagged = [item for item in pool if tag_norm in item.tags]
            if tagged:
                return candidate_rarity, tagged
        elif pool:
            return candidate_rarity, pool

    if tag_norm:
        return stock_item_pool(cache=cache, rarity=rarity, consumable=consumable, apl=apl)
    return None, []


def _item_search_text(sheet_item: Any) -> str:
    """Collect item text used for simple template category detection."""
    return " ".join(
        str(part or "")
        for part in (
            sheet_item.name,
            sheet_item.category,
            sheet_item.tags_text,
            sheet_item.variant_type,
            sheet_item.variant_instructions,
            sheet_item.item_type,
        )
    ).casefold()


def item_is_ammunition_template(sheet_item: Any) -> bool:
    """Return True when a generic stock item should resolve to ammunition."""
    text = _item_search_text(sheet_item)
    return any(word in text for word in ("ammunition", "arrow", "bolt", "bullet"))


def item_is_weapon_template(sheet_item: Any) -> bool:
    """Return True when a generic stock item should resolve to a weapon."""
    text = _item_search_text(sheet_item)
    return "weapon" in text or "blade" in text or "bow" in text


def item_is_armor_template(sheet_item: Any) -> bool:
    """Return True when a generic stock item should resolve to armor."""
    text = _item_search_text(sheet_item)
    return "armor" in text or "armour" in text


def item_is_shield_template(sheet_item: Any) -> bool:
    """Return True when a generic stock item should resolve to a shield."""
    return "shield" in _item_search_text(sheet_item)


def clean_random_stock_base_name(sheet_item: Any) -> str:
    """Remove unresolved template markers from a random-stock display name."""
    name = re.sub(r"\s*\((?:any|any [^)]+)\)\s*$", "", sheet_item.name, flags=re.IGNORECASE).strip()
    if item_is_ammunition_template(sheet_item):
        name = re.sub(
            r"\s*\([^)]*(?:arrow|bolt|bullet|ammunition)[^)]*\)\s*$",
            "",
            name,
            flags=re.IGNORECASE,
        ).strip()
    return name or sheet_item.name


def ammunition_stack_variant(option: str) -> str | None:
    """Normalize an ammunition option into the server's stock stack sizes."""
    lowered = option.casefold()
    if "arrow" in lowered:
        return "20 arrows"
    if "bolt" in lowered:
        return "20 bolts"
    if "bullet" in lowered:
        return "10 bullets"
    return None


def random_variant_from_sheet_item(sheet_item: Any) -> str | None:
    """Choose a concrete variant for a generic/template item."""
    options = list(sheet_item.variant_option_list)
    if options:
        chosen = random.choice(options)
        if item_is_ammunition_template(sheet_item):
            return ammunition_stack_variant(chosen) or chosen
        return chosen

    if item_is_ammunition_template(sheet_item):
        return random.choice(RANDOM_AMMUNITION_VARIANTS)
    if item_is_shield_template(sheet_item):
        return "Shield"
    if item_is_armor_template(sheet_item):
        return random.choice(RANDOM_ARMOR_VARIANTS)
    if item_is_weapon_template(sheet_item):
        return random.choice(RANDOM_WEAPON_VARIANTS)
    return None


def resolve_random_stock_identity(sheet_item: Any) -> tuple[str, str | None, str | None]:
    """Return listing name, variant, and note text for a random stock item."""
    if not is_generic_template_item(sheet_item):
        return sheet_item.name, None, None

    variant = random_variant_from_sheet_item(sheet_item)
    if not variant:
        return (
            sheet_item.name,
            None,
            "Random stock selected a template item; no automatic variant was available.",
        )

    base_name = clean_random_stock_base_name(sheet_item)
    return resolved_listing_name(base_name, variant), variant, f"Random variant: {variant}."


def build_buy_receipt(
    *,
    buyer: str,
    buyer_character: str,
    listing: dict[str, Any],
    item_name: str,
    origin_text: str,
    buy_roll: Any,
    gold_available: int,
) -> str:
    """Build the public buy receipt and the stored buy audit text."""
    lines = [
        "Dwarfy Buy Receipt:",
        f"Buyer: {buyer} as {buyer_character}",
        f"Listing: {listing['listing_id']}",
        f"Item: {item_name}",
        f"Rarity: {listing['rarity']}",
        f"Source: {source_with_page(listing.get('source'), listing.get('page'))}",
        f"Original source: {origin_text}",
        "",
        f"Xanathar price roll: {buy_roll.roll_detail}",
        f"Base asking price: {gp(buy_roll.rolled_price)}",
        f"Dwarfy haggling roll: {buy_roll.haggling_roll}",
        f"Haggling result: {buy_roll.haggling_result}",
        f"Discount: {discount_text(buy_roll.discount_percent)}",
    ]
    if buy_roll.discount_percent:
        lines.append(f"Discounted price: {gp(buy_roll.discounted_price)}")
    if buy_roll.insult_line:
        lines.append(f'Dwarfy says: "{buy_roll.insult_line}"')
    if buy_roll.cost_basis_floor_applied:
        lines.append(f"Floor: Dwarfy will not sell below his {gp(int(listing['cost_basis']))} cost basis.")

    lines.extend(
        [
            "",
            f"Dwarfy cost basis: {gp(int(listing['cost_basis']))}",
            f"Final item price: {gp(buy_roll.final_price)}",
            f"Declared gold available: {gp(gold_available)}",
            f"Dwarfy profit: {gp(buy_roll.realized_profit)}",
            "",
            "Sale status: Final, no takebacks",
        ]
    )
    return "\n".join(lines)


def sell_validation_error(sheet_item) -> str | None:
    """Return an ephemeral validation error for /dwarfy sell, or None."""
    if not sheet_item.allowed:
        return f"`{sheet_item.name}` exists in the sheet, but Allowed is FALSE."
    if sheet_item.consumable:
        return f"`{sheet_item.name}` is marked Consumable=TRUE. Dwarfy only buys permanent magic items."
    if sheet_item.dwarfy_sell_eligible is False:
        return f"`{sheet_item.name}` is marked Dwarfy Sell Eligible=FALSE and cannot be sold to Dwarfy."
    if not is_supported_rarity(sheet_item.rarity):
        return (
            f"Dwarfy cannot price `{sheet_item.rarity}` items in version 1. "
            "Supported rarities: Common, Uncommon, Rare, Very Rare, Legendary."
        )
    return None


def _variant_receipt_lines(
    *,
    variant: str | None,
    variant_type: str,
    variant_instructions: str,
    details: str | None = None,
) -> list[str]:
    lines: list[str] = []
    if variant:
        lines.append(f"* Variant: {variant}")
    if variant_type:
        lines.append(f"* Variant type: {variant_type}")
    if variant_instructions:
        lines.append(f"* Variant instructions: {variant_instructions}")
    if details:
        lines.append(f"* Notes: {details}")
    return lines


def build_sell_receipt(
    *,
    activity: str,
    character: str,
    level: int,
    seller_mention: str,
    seller_display_name: str,
    listing_name: str,
    base_item_name: str,
    variant: str | None,
    listing_id: str | None,
    rarity: str,
    item_detail: str,
    source: str,
    page: str,
    base_price: int,
    dtp_cost: int,
    gold_cost: int,
    seller_payout: int,
    status: str,
    roll_label: str | None = None,
    roll_value: int | None = None,
    details: str | None = None,
    variant_instructions: str | None = None,
) -> str:
    """Build the copyable Adventure Log Receipt stored with successful sales."""
    lines = [
        "Adventure Log Receipt:",
        f"Activity: {activity}",
        f"Character: {character.strip()} ({level})",
        f"Seller: {seller_mention} / {seller_display_name}",
        f"Item: {listing_name}",
    ]
    if variant:
        lines.extend([f"Base item: {base_item_name}", f"Variant: {variant}"])
    if listing_id:
        lines.append(f"Listing: {listing_id}")
    lines.extend(
        [
            f"Rarity: {rarity}",
            f"Item detail: {item_detail}",
            f"Source: {source_with_page(source, page)}",
            f"Base price: {gp(base_price)}",
            f"DTP spent: {dtp_cost}",
            f"Gold spent: {gp(gold_cost)}",
        ]
    )
    if roll_label and roll_value is not None:
        lines.append(f"{roll_label}: {roll_value}")
    lines.extend(
        [
            f"Seller payout: {gp(seller_payout)}",
            f"Sale status: {status}",
        ]
    )
    if details:
        lines.append(f"Notes: {details}")
    if variant_instructions and not variant:
        lines.append(f"Variant instructions: {variant_instructions}")
    return "\n".join(lines)


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

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.guild and interaction.guild.owner_id == interaction.user.id:
            return True
        role_names = {
            role.name.casefold()
            for role in getattr(interaction.user, "roles", [])
        }
        return "owner" in role_names

    async def _require_owner(self, interaction: discord.Interaction) -> bool:
        if self._is_owner(interaction):
            return True
        await interaction.response.send_message(
            "Only the Discord server owner or someone with the Owner role can use that command.",
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

    async def _resolve_sale_context(
        self,
        interaction: discord.Interaction,
        *,
        character: str,
        level: int,
        item: str,
        variant: str | None,
        details: str | None,
    ) -> dict[str, Any] | None:
        """Validate shared sell/broker inputs and return normalized sale data."""
        if not await self._require_channel(
            interaction,
            self.bot.config.dwarfy_sell_channel_id,
            "DWARFY_SELL_CHANNEL_ID",
        ):
            return None
        if not await self._require_sheet_cache(interaction):
            return None

        if looks_like_pasted_item_text(item):
            await interaction.response.send_message(
                "Use only the clean item name in the item field. The bot already knows the item data.",
                ephemeral=True,
            )
            return None

        match = self.bot.sheet_cache.match_item(item, for_sell=True)
        if match.choices:
            await interaction.response.send_message(
                f"{match.message or 'I found multiple possible item matches.'} Please run the command again with one exact item name:\n"
                f"{format_item_choices(match.choices)}",
                ephemeral=True,
            )
            return None
        if match.item is None:
            await interaction.response.send_message(
                match.message or f"I could not find `{item}` in the cached Bot Items sheet.",
                ephemeral=True,
            )
            return None

        sheet_item = match.item
        validation_error = sell_validation_error(sheet_item)
        if validation_error:
            await interaction.response.send_message(validation_error, ephemeral=True)
            return None

        variant_clean = (variant or "").strip() or None
        details_clean = (details or "").strip() or None
        is_template = is_generic_template_item(sheet_item)

        if variant_clean and not is_template:
            await interaction.response.send_message(
                "Variant is only used for generic/template items like +1 Weapon, Adamantine Armor, or Ammunition of Slaying. This item is already specific. Run the command again without variant.",
                ephemeral=True,
            )
            return None
        if details_clean and not is_template and looks_like_pasted_detail_text(details_clean):
            await interaction.response.send_message(
                "Use only the item name in the item field. The bot already knows the item data. Put only custom notes in details.",
                ephemeral=True,
            )
            return None

        variant_note = ""
        if variant_clean and sheet_item.variant_option_list:
            option_names = {option.casefold() for option in sheet_item.variant_option_list}
            if variant_clean.casefold() not in option_names:
                variant_note = "Variant note: This variant was not in the sheet's suggested options."

        variant_lines = _variant_receipt_lines(
            variant=variant_clean,
            variant_type=sheet_item.variant_type,
            variant_instructions=sheet_item.variant_instructions,
            details=details_clean,
        )
        if variant_note:
            variant_lines.append(f"* {variant_note}")

        return {
            "sheet_item": sheet_item,
            "variant_clean": variant_clean,
            "details_clean": details_clean,
            "variant_block": ("\n" + "\n".join(variant_lines)) if variant_lines else "",
            "listing_name": resolved_listing_name(sheet_item.name, variant_clean),
            "seller": interaction.user.mention,
            "seller_display": _display_name(interaction.user),
            "seller_character": character_label(character, int(level)),
            "item_detail": item_detail_summary(sheet_item),
        }

    async def _create_inventory_sale_listing(
        self,
        interaction: discord.Interaction,
        *,
        context: dict[str, Any],
        character: str,
        level: int,
        sale_method: str,
        sale_percent: int,
        dtp_cost: int,
        gold_cost: int,
        broker_roll: int | None,
        broker_result: str | None,
        seller_payout: int,
        receipt_preview: str,
    ) -> tuple[dict[str, Any], str]:
        """Create a buyable Dwarfy inventory listing and finalize its receipt."""
        sheet_item = context["sheet_item"]
        listing = await self.bot.db.create_listing(
            item_name=context["listing_name"],
            rarity=sheet_item.rarity,
            source=sheet_item.source,
            category=sheet_item.category,
            tags=sheet_item.tags_text,
            seller_user_id=str(interaction.user.id),
            seller_display_name=_display_name(interaction.user),
            seller_character_name=character.strip(),
            seller_character_level=int(level),
            sell_roll=broker_roll or 0,
            seller_payout=seller_payout,
            item_clean_name=sheet_item.name,
            listing_display_name=context["listing_name"],
            base_item_name=sheet_item.name if context["variant_clean"] else None,
            variant=context["variant_clean"],
            details=context["details_clean"],
            variant_details=context["variant_clean"],
            variant_type=sheet_item.variant_type or None,
            variant_instructions=sheet_item.variant_instructions or None,
            item_type=sheet_item.item_type or None,
            attunement=sheet_item.attunement or None,
            page=sheet_item.page or None,
            display_detail=sheet_item.display_detail or None,
            short_description=sheet_item.short_description or None,
            rules_text=sheet_item.rules_text or None,
            json_notes=sheet_item.json_notes or None,
            item_tags=sheet_item.item_tags or None,
            receipt_text=receipt_preview,
            sale_method=sale_method,
            sale_percent=sale_percent,
            dtp_cost=dtp_cost,
            gold_cost=gold_cost,
            broker_roll=broker_roll,
            broker_result=broker_result,
            item_status="inventory",
            adventure_log_receipt=receipt_preview,
            seller_user_display=context["seller_display"],
        )
        receipt = receipt_preview.replace("{listing_id}", listing["listing_id"])
        await self.bot.db.db.execute(
            """
            UPDATE listings
            SET receipt_text = ?, adventure_log_receipt = ?
            WHERE listing_id = ?
            """,
            (receipt, receipt, listing["listing_id"]),
        )
        await self.bot.db.db.commit()
        listing["receipt_text"] = receipt
        listing["adventure_log_receipt"] = receipt
        return listing, receipt

    def _stock_item_autocomplete_names(self, current: str) -> list[str]:
        """Return allowed clean item names for owner stock autocomplete."""
        query_norm = current.casefold().strip()
        seen: set[str] = set()
        starts: list[str] = []
        contains: list[str] = []
        for sheet_item in self.bot.sheet_cache.items:
            if not sheet_item.allowed or sheet_item.loot_type != "Item":
                continue
            if not is_supported_rarity(sheet_item.rarity):
                continue
            key = sheet_item.name.casefold().strip()
            if key in seen:
                continue
            if query_norm and query_norm not in key:
                continue
            seen.add(key)
            if query_norm and key.startswith(query_norm):
                starts.append(sheet_item.name)
            else:
                contains.append(sheet_item.name)
        return (starts + contains)[:25]

    def _stock_variant_options(self, item_name: str, current: str) -> list[str]:
        match = self.bot.sheet_cache.match_item(item_name, for_sell=False)
        if match.item is None:
            return []
        query_norm = current.casefold().strip()
        options: list[str] = []
        seen: set[str] = set()
        for option in match.item.variant_option_list:
            key = option.casefold()
            if key in seen:
                continue
            if query_norm and query_norm not in key:
                continue
            seen.add(key)
            options.append(option)
        return options[:25]

    async def _resolve_stock_context(
        self,
        interaction: discord.Interaction,
        *,
        item: str,
        variant: str | None,
        notes: str | None,
    ) -> dict[str, Any] | None:
        """Validate owner-stock item input."""
        if not await self._require_owner(interaction):
            return None
        if not await self._require_sheet_cache(interaction):
            return None
        if looks_like_pasted_item_text(item):
            await interaction.response.send_message(
                "Use only the clean item name in the item field. The bot already knows the item data.",
                ephemeral=True,
            )
            return None

        match = self.bot.sheet_cache.match_item(item, for_sell=False)
        if match.choices:
            await interaction.response.send_message(
                f"{match.message or 'I found multiple possible item matches.'} Please run the command again with one exact item name:\n"
                f"{format_item_choices(match.choices)}",
                ephemeral=True,
            )
            return None
        if match.item is None:
            await interaction.response.send_message(
                match.message or f"I could not find `{item}` in the cached Bot Items sheet.",
                ephemeral=True,
            )
            return None

        sheet_item = match.item
        if not sheet_item.allowed:
            await interaction.response.send_message(
                f"`{sheet_item.name}` is Allowed=FALSE and cannot be stocked.",
                ephemeral=True,
            )
            return None
        if sheet_item.loot_type != "Item":
            await interaction.response.send_message(
                f"`{sheet_item.name}` is Loot Type={sheet_item.loot_type}; owner stock can only add actual items.",
                ephemeral=True,
            )
            return None
        if not is_supported_rarity(sheet_item.rarity):
            await interaction.response.send_message(
                f"Dwarfy cannot price `{sheet_item.rarity}` items in version 1.",
                ephemeral=True,
            )
            return None

        variant_clean = (variant or "").strip() or None
        notes_clean = (notes or "").strip() or None
        is_template = is_generic_template_item(sheet_item)
        if variant_clean and not is_template:
            await interaction.response.send_message(
                "Variant is only used for generic/template items. This item is already specific.",
                ephemeral=True,
            )
            return None
        if notes_clean and not is_template and looks_like_pasted_detail_text(notes_clean):
            await interaction.response.send_message(
                "Use notes only for short custom stock notes, not full item rules text.",
                ephemeral=True,
            )
            return None

        return {
            "sheet_item": sheet_item,
            "variant_clean": variant_clean,
            "notes_clean": notes_clean,
            "listing_name": resolved_listing_name(sheet_item.name, variant_clean),
        }

    async def _create_owner_stock_listing(
        self,
        interaction: discord.Interaction,
        *,
        sheet_item: Any,
        listing_name: str,
        variant: str | None,
        notes: str | None,
        cost_basis: int,
        batch_id: str,
    ) -> dict[str, Any]:
        """Create a Dwarfy-owned stock listing."""
        return await self.bot.db.create_listing(
            item_name=listing_name,
            rarity=sheet_item.rarity,
            source=sheet_item.source,
            category=sheet_item.category,
            tags=sheet_item.tags_text,
            seller_user_id="",
            seller_display_name="Dwarfy Stock",
            seller_character_name="Dwarfy Stock",
            seller_character_level=0,
            sell_roll=0,
            seller_payout=0,
            cost_basis=cost_basis,
            item_clean_name=sheet_item.name,
            listing_display_name=listing_name,
            base_item_name=sheet_item.name if variant else None,
            variant=variant,
            details=notes,
            variant_details=variant,
            variant_type=sheet_item.variant_type or None,
            variant_instructions=sheet_item.variant_instructions or None,
            item_type=sheet_item.item_type or None,
            attunement=sheet_item.attunement or None,
            page=sheet_item.page or None,
            display_detail=sheet_item.display_detail or None,
            short_description=sheet_item.short_description or None,
            rules_text=sheet_item.rules_text or None,
            json_notes=sheet_item.json_notes or None,
            item_tags=sheet_item.item_tags or None,
            sale_method="owner_stock",
            sale_percent=0,
            item_status="inventory",
            stock_source="owner_stock",
            stock_batch_id=batch_id,
            stocked_by_user_id=str(interaction.user.id),
            stocked_by_display_name=_display_name(interaction.user),
            stock_notes=notes,
            seller_user_display="Dwarfy Stock",
            ledger_entry_type="owner_stock_item",
            ledger_cash_change=0,
            ledger_inventory_cost_change=cost_basis,
            ledger_notes=f"Owner stocked {listing_name} with cost basis {gp(cost_basis)}.",
        )

    @app_commands.command(name="stock_add", description="Owner only: add one item to Dwarfy's inventory.")
    @app_commands.describe(
        item="Clean item name from Bot Items.",
        quantity="How many copies to add. Defaults to 1.",
        cost_basis="Optional Dwarfy cost basis. Defaults to the 40% direct-sale payout.",
        variant="Optional identity for generic/template items, such as Longsword.",
        notes="Optional stock note for staff audit.",
    )
    async def stock_add(
        self,
        interaction: discord.Interaction,
        item: str,
        quantity: int = 1,
        cost_basis: int | None = None,
        variant: str | None = None,
        notes: str | None = None,
    ) -> None:
        context = await self._resolve_stock_context(
            interaction,
            item=item,
            variant=variant,
            notes=notes,
        )
        if context is None:
            return

        if quantity < 1 or quantity > 25:
            await interaction.response.send_message(
                "`quantity` must be between 1 and 25.",
                ephemeral=True,
            )
            return
        if cost_basis is not None and (cost_basis < 0 or cost_basis > 10_000_000):
            await interaction.response.send_message(
                "`cost_basis` must be between 0 and 10,000,000gp.",
                ephemeral=True,
            )
            return

        sheet_item = context["sheet_item"]
        default_cost_basis = direct_sell_price(sheet_item.rarity).seller_payout
        final_cost_basis = default_cost_basis if cost_basis is None else int(cost_basis)
        batch_id = new_stock_batch_id()
        rows: list[dict[str, Any]] = []
        for _ in range(int(quantity)):
            rows.append(
                await self._create_owner_stock_listing(
                    interaction,
                    sheet_item=sheet_item,
                    listing_name=context["listing_name"],
                    variant=context["variant_clean"],
                    notes=context["notes_clean"],
                    cost_basis=final_cost_basis,
                    batch_id=batch_id,
                )
            )

        listing_ids = ", ".join(row["listing_id"] for row in rows)
        output = (
            "Owner stock added.\n\n"
            f"Item: {context['listing_name']}\n"
            f"Quantity: {len(rows)}\n"
            f"Rarity: {sheet_item.rarity}\n"
            f"Source: {source_with_page(sheet_item.source, sheet_item.page)}\n"
            f"Cost basis each: {gp(final_cost_basis)}\n"
            f"Batch: {batch_id}\n"
            f"Listings: {listing_ids}"
        )
        if context["notes_clean"]:
            output += f"\nNotes: {context['notes_clean']}"
        await send_text_response(interaction, output, ephemeral=True)

    @stock_add.autocomplete("item")
    async def stock_add_item_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not self.bot.sheet_cache.loaded:
            return []
        return [
            app_commands.Choice(name=name[:100], value=name[:100])
            for name in self._stock_item_autocomplete_names(current)
        ]

    @stock_add.autocomplete("variant")
    async def stock_add_variant_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not self.bot.sheet_cache.loaded:
            return []
        item_name = getattr(interaction.namespace, "item", "") or ""
        return [
            app_commands.Choice(name=name[:100], value=name[:100])
            for name in self._stock_variant_options(item_name, current)
        ]

    @app_commands.command(name="stock_gold", description="Owner only: record gold added to Dwarfy's ledger.")
    @app_commands.describe(
        amount="Gold amount to add to Dwarfy's ledger.",
        reason="Optional audit reason.",
    )
    async def stock_gold(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 100_000_000],
        reason: str | None = None,
    ) -> None:
        if not await self._require_owner(interaction):
            return
        note = (reason or "Owner added shop gold.").strip()
        await self.bot.db.add_ledger_entry(
            entry_type="owner_gold",
            listing_id=None,
            item_name=None,
            cash_change=int(amount),
            inventory_cost_change=0,
            profit_change=0,
            notes=f"Owner gold added by {_display_name(interaction.user)} ({interaction.user.id}): {note}",
        )
        await interaction.response.send_message(
            f"Owner gold added to Dwarfy's ledger: {gp(int(amount))}\nReason: {note}",
            ephemeral=True,
        )

    @app_commands.command(name="stock_clear", description="Owner only: void all owner-stocked inventory.")
    @app_commands.describe(
        confirm="Must be True to clear owner-stocked inventory.",
        reason="Audit reason for the reset.",
    )
    async def stock_clear(
        self,
        interaction: discord.Interaction,
        confirm: bool,
        reason: str | None = None,
    ) -> None:
        if not await self._require_owner(interaction):
            return
        if not confirm:
            await interaction.response.send_message(
                "No stock was cleared. Run again with confirm=True to reset owner-stocked items.",
                ephemeral=True,
            )
            return
        summary = await self.bot.db.clear_owner_stock(
            reason=(reason or "Owner stock reset.").strip(),
            stocked_by_user_id=str(interaction.user.id),
            stocked_by_display_name=_display_name(interaction.user),
        )
        await interaction.response.send_message(
            (
                "Owner stock reset complete.\n\n"
                f"Listings voided: {summary['count']}\n"
                f"Inventory cost basis removed: {gp(summary['cost_basis'])}\n"
                "Player-sold inventory was not changed."
            ),
            ephemeral=True,
        )

    @app_commands.command(name="stock_random", description="Owner only: add random weighted shop stock.")
    @app_commands.describe(
        permanent_count="Permanent item count. Defaults to 20.",
        consumable_count="Consumable item count. Defaults to 30.",
        apl="APL band to use for sheet eligibility. Defaults to 10.",
        tag="Optional tag preference.",
        clear_first="Void existing owner stock before adding the new batch.",
    )
    async def stock_random(
        self,
        interaction: discord.Interaction,
        permanent_count: int = 20,
        consumable_count: int = 30,
        apl: int = 10,
        tag: str | None = None,
        clear_first: bool = False,
    ) -> None:
        if not await self._require_owner(interaction):
            return
        if not await self._require_sheet_cache(interaction):
            return
        if not 0 <= permanent_count <= 100 or not 0 <= consumable_count <= 100:
            await interaction.response.send_message(
                "`permanent_count` and `consumable_count` must each be between 0 and 100.",
                ephemeral=True,
            )
            return
        if not 1 <= apl <= 20:
            await interaction.response.send_message(
                "`apl` must be between 1 and 20.",
                ephemeral=True,
            )
            return
        if permanent_count == 0 and consumable_count == 0:
            await interaction.response.send_message(
                "Choose at least one permanent or consumable item to stock.",
                ephemeral=True,
            )
            return

        clear_summary = {"count": 0, "cost_basis": 0}
        if clear_first:
            clear_summary = await self.bot.db.clear_owner_stock(
                reason="Owner random restock.",
                stocked_by_user_id=str(interaction.user.id),
                stocked_by_display_name=_display_name(interaction.user),
            )

        batch_id = new_stock_batch_id()
        created: list[dict[str, Any]] = []
        audit_lines: list[str] = []
        selected_permanent_names: set[str] = set()

        async def add_random_slot(index: int, *, consumable: bool) -> None:
            d100 = random.randint(1, 100)
            rolled_rarity = stock_rarity_from_roll(d100, consumable=consumable)
            selected_rarity, pool = stock_item_pool(
                cache=self.bot.sheet_cache,
                rarity=rolled_rarity,
                consumable=consumable,
                apl=int(apl),
                tag=tag,
            )
            if not pool or selected_rarity is None:
                slot_type = "Consumable" if consumable else "Permanent"
                audit_lines.append(
                    f"{slot_type} {index}: d100 {d100} -> {rolled_rarity} -> skipped, no eligible sheet pool."
                )
                return

            final_pool = pool
            if not consumable:
                without_duplicates = [
                    item for item in pool if item.name.casefold() not in selected_permanent_names
                ]
                if without_duplicates:
                    final_pool = without_duplicates

            selection = pick_weighted_item(final_pool)
            sheet_item = selection.item
            if not consumable:
                selected_permanent_names.add(sheet_item.name.casefold())
            cost_basis = direct_sell_price(sheet_item.rarity).seller_payout
            listing_name, variant, variant_note = resolve_random_stock_identity(sheet_item)
            stock_note = f"Random owner stock batch {batch_id}."
            if variant_note:
                stock_note = f"{stock_note} {variant_note}"
            row = await self._create_owner_stock_listing(
                interaction,
                sheet_item=sheet_item,
                listing_name=listing_name,
                variant=variant,
                notes=stock_note,
                cost_basis=cost_basis,
                batch_id=batch_id,
            )
            created.append(row)
            fallback_text = "" if selected_rarity == rolled_rarity else f", fallback to {selected_rarity}"
            slot_type = "Consumable" if consumable else "Permanent"
            audit_lines.append(
                (
                    f"{slot_type} {index}: d100 {d100} -> {rolled_rarity}{fallback_text} -> "
                    f"{row['listing_id']} {listing_name} "
                    f"(ticket {selection.ticket}/{selection.total_weight})"
                )
            )

        for index in range(1, int(permanent_count) + 1):
            await add_random_slot(index, consumable=False)
        for index in range(1, int(consumable_count) + 1):
            await add_random_slot(index, consumable=True)

        lines = [
            "Random owner stock complete.",
            "",
            f"Batch: {batch_id}",
            f"APL filter: {int(apl)}",
            f"Tag preference: {tag.strip() if tag else 'none'}",
            f"Listings created: {len(created)}",
        ]
        if clear_first:
            lines.extend(
                [
                    f"Previous owner-stock listings voided: {clear_summary['count']}",
                    f"Previous owner-stock cost basis removed: {gp(clear_summary['cost_basis'])}",
                ]
            )
        lines.extend(["", "Audit:"])
        lines.extend(audit_lines[:60])
        if len(audit_lines) > 60:
            lines.append(f"...and {len(audit_lines) - 60} more audit lines.")
        await send_text_response(interaction, "\n".join(lines), ephemeral=True)

    @app_commands.command(name="sell", description="Sell a permanent magic item to Dwarfy's Shop.")
    @app_commands.describe(
        character="Your character's name.",
        level="Your character's level.",
        item="The clean magic item name from Bot Items.",
        variant="Optional identity for generic/template items, such as Longsword.",
        details="Optional custom notes, not item rules text.",
    )
    async def sell(
        self,
        interaction: discord.Interaction,
        character: str,
        level: app_commands.Range[int, 1, 20],
        item: str,
        variant: str | None = None,
        details: str | None = None,
    ) -> None:
        context = await self._resolve_sale_context(
            interaction,
            character=character,
            level=int(level),
            item=item,
            variant=variant,
            details=details,
        )
        if context is None:
            return

        sheet_item = context["sheet_item"]
        sale = direct_sell_price(sheet_item.rarity)
        receipt_preview = build_sell_receipt(
            activity="Sell Magic Item directly to Dwarfy's Shop",
            character=character,
            level=int(level),
            seller_mention=context["seller"],
            seller_display_name=context["seller_display"],
            listing_name=context["listing_name"],
            base_item_name=sheet_item.name,
            variant=context["variant_clean"],
            listing_id="{listing_id}",
            rarity=sheet_item.rarity,
            item_detail=context["item_detail"],
            source=sheet_item.source,
            page=sheet_item.page,
            base_price=sale.base_price,
            dtp_cost=0,
            gold_cost=0,
            seller_payout=sale.seller_payout,
            status="Final, no takebacks",
            details=context["details_clean"],
            variant_instructions=sheet_item.variant_instructions,
        )
        listing, receipt = await self._create_inventory_sale_listing(
            interaction,
            context=context,
            character=character,
            level=int(level),
            sale_method="direct",
            sale_percent=sale.payout_percent,
            dtp_cost=0,
            gold_cost=0,
            broker_roll=None,
            broker_result=None,
            seller_payout=sale.seller_payout,
            receipt_preview=receipt_preview,
        )
        output = (
            f"{context['seller']} declares that {context['seller_character']} owns {context['listing_name']} "
            "and sells it directly to Dwarfy's Shop.\n\n"
            f"Dwarfy's Shop buys {context['listing_name']} from {context['seller']} as {context['seller_character']} "
            f"for {gp(sale.seller_payout)} and adds it to magic inventory as {listing['listing_id']}.\n\n"
            "Sell Magic Item:\n\n"
            "Method: Direct sale\n"
            "DTP cost: 0\n"
            "Gold cost: 0gp\n"
            "Roll: none\n"
            f"Result: {sale.result_text}\n"
            f"Base price: {gp(sale.base_price)}\n"
            f"Seller payout: {gp(sale.seller_payout)}\n"
            f"Dwarfy's cost basis: {gp(sale.seller_payout)}\n"
            "Future sale price: rolled when purchased, never below Dwarfy's cost basis\n"
            f"Sale status: Final, no takebacks{context['variant_block']}\n\n"
            f"{receipt}\n\n"
            "Adventure log reminder:\n"
            "Record this downtime activity manually on the character's adventure log."
        )
        await send_text_response(interaction, output)

    @app_commands.command(name="broker", description="Broker a magic item through Dwarfy's Shop for a rolled payout.")
    @app_commands.describe(
        character="Your character's name.",
        level="Your character's level.",
        item="The clean magic item name from Bot Items.",
        variant="Optional identity for generic/template items, such as Longsword.",
        details="Optional custom notes, not item rules text.",
    )
    async def broker(
        self,
        interaction: discord.Interaction,
        character: str,
        level: app_commands.Range[int, 1, 20],
        item: str,
        variant: str | None = None,
        details: str | None = None,
    ) -> None:
        context = await self._resolve_sale_context(
            interaction,
            character=character,
            level=int(level),
            item=item,
            variant=variant,
            details=details,
        )
        if context is None:
            return

        sheet_item = context["sheet_item"]
        broker_roll = roll_broker_price(sheet_item.rarity)
        declaration = (
            f"{context['seller']} declares that {context['seller_character']} owns {context['listing_name']} "
            "and spends 5 DTP and 25gp to broker it through Dwarfy's Shop."
        )

        if broker_roll.roll == 1:
            receipt = build_sell_receipt(
                activity="Failed Broker Magic Item through Dwarfy's Shop",
                character=character,
                level=int(level),
                seller_mention=context["seller"],
                seller_display_name=context["seller_display"],
                listing_name=context["listing_name"],
                base_item_name=sheet_item.name,
                variant=context["variant_clean"],
                listing_id="none, item lost",
                rarity=sheet_item.rarity,
                item_detail=context["item_detail"],
                source=sheet_item.source,
                page=sheet_item.page,
                base_price=broker_roll.base_price,
                dtp_cost=5,
                gold_cost=25,
                roll_label="Broker roll",
                roll_value=broker_roll.roll,
                seller_payout=0,
                status="Item lost, final, no takebacks",
                details=context["details_clean"],
                variant_instructions=sheet_item.variant_instructions,
            )
            output = (
                f"{declaration}\n\n"
                f"{broker_sale_result_line(broker_roll)}\n\n"
                "Broker Magic Item downtime:\n\n"
                "Method: Brokered sale\n"
                "DTP cost: 5\n"
                "Gold cost: 25gp\n"
                "Flat d20 roll: 1\n"
                f"Result: {broker_roll.result_text}\n"
                f"Base price: {gp(broker_roll.base_price)}\n"
                "Seller payout: 0gp\n"
                "Dwarfy's cost basis: 0gp\n"
                "Inventory status: Not added to Dwarfy inventory\n"
                f"Sale status: Final, no takebacks{context['variant_block']}\n\n"
                f"{random.choice(DISASTER_MESSAGES)}\n\n"
                f"{receipt}\n\n"
                "Adventure log reminder:\n"
                "Record this downtime activity manually on the character's adventure log."
            )
            await send_text_response(interaction, output)
            return

        receipt_preview = build_sell_receipt(
            activity="Broker Magic Item through Dwarfy's Shop",
            character=character,
            level=int(level),
            seller_mention=context["seller"],
            seller_display_name=context["seller_display"],
            listing_name=context["listing_name"],
            base_item_name=sheet_item.name,
            variant=context["variant_clean"],
            listing_id="{listing_id}",
            rarity=sheet_item.rarity,
            item_detail=context["item_detail"],
            source=sheet_item.source,
            page=sheet_item.page,
            base_price=broker_roll.base_price,
            dtp_cost=5,
            gold_cost=25,
            roll_label="Broker roll",
            roll_value=broker_roll.roll,
            seller_payout=broker_roll.seller_payout,
            status="Final, no takebacks",
            details=context["details_clean"],
            variant_instructions=sheet_item.variant_instructions,
        )
        listing, receipt = await self._create_inventory_sale_listing(
            interaction,
            context=context,
            character=character,
            level=int(level),
            sale_method="broker",
            sale_percent=broker_roll.payout_percent,
            dtp_cost=5,
            gold_cost=25,
            broker_roll=broker_roll.roll,
            broker_result=broker_roll.result_text,
            seller_payout=broker_roll.seller_payout,
            receipt_preview=receipt_preview,
        )
        output = (
            f"{declaration}\n\n"
            f"{broker_sale_result_line(broker_roll)}\n\n"
            f"{context['seller']} as {context['seller_character']} brokers {context['listing_name']} "
            f"through Dwarfy's Shop for {gp(broker_roll.seller_payout)}.\n"
            f"Dwarfy's Shop receives {context['listing_name']} and adds it to magic inventory as {listing['listing_id']}.\n\n"
            "Broker Magic Item downtime:\n\n"
            "Method: Brokered sale\n"
            "DTP cost: 5\n"
            "Gold cost: 25gp\n"
            f"Flat d20 roll: {broker_roll.roll}\n"
            f"Result: {broker_roll.result_text}\n"
            f"Base price: {gp(broker_roll.base_price)}\n"
            f"Seller payout: {gp(broker_roll.seller_payout)}\n"
            f"Dwarfy's cost basis: {gp(broker_roll.seller_payout)}\n"
            "Future sale price: rolled when purchased, never below Dwarfy's cost basis\n"
            f"Sale status: Final, no takebacks{context['variant_block']}\n\n"
            f"{receipt}\n\n"
            "Adventure log reminder:\n"
            "Record this downtime activity manually on the character's adventure log."
        )
        await send_text_response(interaction, output)

    @sell.autocomplete("item")
    async def sell_item_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not self.bot.sheet_cache.loaded:
            return []
        return [
            app_commands.Choice(name=name[:100], value=name[:100])
            for name in self.bot.sheet_cache.autocomplete_sell_item_names(current)
        ]

    @broker.autocomplete("item")
    async def broker_item_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self.sell_item_autocomplete(interaction, current)

    @sell.autocomplete("variant")
    async def sell_variant_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not self.bot.sheet_cache.loaded:
            return []
        item_name = getattr(interaction.namespace, "item", "") or ""
        return [
            app_commands.Choice(name=name[:100], value=name[:100])
            for name in self.bot.sheet_cache.autocomplete_variant_options(
                item_name=item_name,
                query=current,
            )
        ]

    @broker.autocomplete("variant")
    async def broker_variant_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self.sell_variant_autocomplete(interaction, current)

    @app_commands.command(name="browse", description="Browse magic items Dwarfy has for sale.")
    @app_commands.describe(
        rarity="Optional rarity filter.",
        max_price="Only show listings whose maximum possible final price is this amount or lower.",
        search="Search item name, source, category, or tags.",
    )
    @app_commands.choices(rarity=BROWSE_RARITY_CHOICES)
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
        if rarity_filter and rarity_filter not in BROWSE_RARITY_VALUES:
            await interaction.response.send_message(
                "Choose one of the supported rarity filters: Common, Uncommon, Rare, Very Rare, or Legendary.",
                ephemeral=True,
            )
            return
        search_filter = search.casefold().strip() if search else None

        filtered: list[tuple[dict[str, Any], int, int]] = []
        for listing in listings:
            if rarity_filter and listing["rarity"] != rarity_filter:
                continue
            searchable = " ".join(
                str(listing.get(field) or "")
                for field in ("listing_display_name", "item_name", "item_clean_name", "source", "category", "tags", "details")
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

        view = BrowseView(filtered, owner_id=interaction.user.id)
        await interaction.response.send_message(
            embed=view.current_embed(),
            view=view if view.page_total > 1 else None,
            ephemeral=True,
        )

    @app_commands.command(name="inspect", description="Inspect one Dwarfy listing.")
    @app_commands.describe(listing="Listing ID, such as DWF-00017.")
    async def inspect(self, interaction: discord.Interaction, listing: str) -> None:
        if not await self._require_channel(
            interaction,
            self.bot.config.dwarfy_shop_channel_id,
            "DWARFY_SHOP_CHANNEL_ID",
        ):
            return
        listing_id = parse_listing_id(listing)
        if listing_id is None:
            await interaction.response.send_message(
                "Please inspect by listing ID, like `DWF-00017`.",
                ephemeral=True,
            )
            return

        row = await self.bot.db.get_listing(listing_id)
        if row is None:
            await interaction.response.send_message(
                f"I could not find listing `{listing_id}`.",
                ephemeral=True,
            )
            return

        await send_text_response(interaction, self._format_inspect(row), ephemeral=True)

    def _format_inspect(self, listing: dict[str, Any]) -> str:
        display_name = listing_display_name(listing)
        seller = mention_user(listing.get("seller_user_id"), listing.get("seller_display_name"))
        seller_character = character_label(
            listing.get("seller_character_name") or listing.get("seller_character"),
            listing.get("seller_character_level") or listing.get("seller_level"),
        )
        low, high = possible_final_price_range(
            listing["rarity"],
            int(listing["cost_basis"]),
        )

        lines = [
            f"Listing: {listing['listing_id']}",
            f"Item: {display_name}",
        ]
        if listing.get("item_clean_name"):
            lines.append(f"Clean item name: {listing['item_clean_name']}")
        if listing.get("base_item_name"):
            lines.append(f"Base item: {listing['base_item_name']}")
        if listing.get("variant") or listing.get("variant_details"):
            lines.append(f"Variant: {listing.get('variant') or listing.get('variant_details')}")
        if listing.get("details"):
            lines.append(f"Notes: {listing['details']}")

        lines.extend(
            [
                f"Rarity: {listing['rarity']}",
                f"Source: {source_with_page(listing.get('source'), listing.get('page'))}",
                f"Category: {listing['category'] or 'none'}",
                f"Tags: {listing['tags'] or 'none'}",
                f"Item detail: {listing.get('display_detail') or listing.get('item_type') or 'none'}",
                f"Short description: {listing.get('short_description') or 'none'}",
                f"Sale method: {listing.get('sale_method') or 'unknown/legacy'}",
                f"Original source: {listing_origin_text(listing)}",
                f"Seller payout: {gp(int(listing.get('seller_payout') or 0))}",
                f"Dwarfy cost basis: {gp(int(listing['cost_basis']))}",
                f"DTP cost: {listing.get('dtp_cost') if listing.get('dtp_cost') is not None else 'unknown'}",
                f"Gold cost: {gp(int(listing.get('gold_cost') or 0)) if listing.get('gold_cost') is not None else 'unknown'}",
                f"Buying price formula: {buy_price_formula(listing['rarity'])}",
                f"Possible final price: {price_range_text(low, high)}",
                f"Item status: {listing.get('item_status') or ('inventory' if listing['status'] == 'available' else listing['status'])}",
                f"Status: {listing['status']}",
            ]
        )
        if listing.get("broker_roll"):
            lines.append(f"Broker roll: {listing['broker_roll']}")
        elif listing.get("sell_roll") and not listing.get("sale_method"):
            lines.append(f"Legacy sell roll: {listing['sell_roll']}")
        if listing.get("broker_result"):
            lines.append(f"Broker result: {listing['broker_result']}")
        if listing.get("variant_type"):
            lines.append(f"Variant type: {listing['variant_type']}")
        if listing.get("variant_instructions"):
            lines.append(f"Variant instructions: {listing['variant_instructions']}")
        if listing.get("stock_source") == "owner_stock":
            lines.append(f"Stock batch: {listing.get('stock_batch_id') or 'none'}")
            lines.append(f"Stock notes: {listing.get('stock_notes') or 'none'}")

        if listing["status"] == "sold":
            buyer = mention_user(listing.get("buyer_user_id"), listing.get("buyer_display_name"))
            buyer_character = character_label(
                listing.get("buyer_character_name"),
                listing.get("buyer_character_level"),
            )
            lines.extend(
                [
                    f"Buyer: {buyer} as {buyer_character}",
                    f"Final sale price: {gp(int(listing['final_sale_price'] or 0))}",
                    f"Realized Dwarfy profit: {gp(int(listing['realized_profit'] or 0))}",
                    f"Sold at: {listing['sold_at']}",
                ]
            )
            if listing.get("buy_haggling_roll") is not None:
                lines.extend(
                    [
                        f"Buy base asking price: {gp(int(listing.get('buy_base_asking_price') or 0))}",
                        f"Buy haggling roll: {listing['buy_haggling_roll']}",
                        f"Buy haggling result: {listing.get('buy_haggling_result') or 'none'}",
                        f"Buy discount: {discount_text(int(listing.get('buy_discount_percent') or 0))}",
                        f"Buy discounted price: {gp(int(listing.get('buy_discounted_price') or 0))}",
                    ]
                )
            if listing.get("debt_total"):
                lines.extend(
                    [
                        f"Declared gold available: {gp(int(listing.get('buyer_gold_available') or 0))}",
                        f"Debt shortfall: {gp(int(listing.get('debt_owed') or 0))}",
                        f"Fine: {gp(int(listing.get('debt_fine') or 0))}",
                        f"Total debt: {gp(int(listing.get('debt_total') or 0))}",
                        f"Debt status: {listing.get('debt_status') or 'unpaid'}",
                    ]
                )
        elif listing["status"] == "voided":
            lines.extend(
                [
                    f"Voided at: {listing['voided_at']}",
                    f"Void reason: {listing['void_reason'] or 'No reason recorded.'}",
                ]
            )

        stored_receipt = listing.get("adventure_log_receipt") or listing.get("receipt_text")
        if stored_receipt:
            lines.extend(["", "Stored Adventure Log Receipt:", stored_receipt])
        if listing.get("buy_receipt_text"):
            lines.extend(["", "Stored Buy Receipt:", listing["buy_receipt_text"]])

        return "\n".join(lines)

    @app_commands.command(name="buy", description="Buy an available magic item from Dwarfy.")
    @app_commands.describe(
        listing="Listing ID only, such as DWF-00017.",
        character="Your character's name.",
        level="Your character's level.",
        gold="How much gold this character currently has available.",
    )
    async def buy(
        self,
        interaction: discord.Interaction,
        listing: str,
        character: str,
        level: app_commands.Range[int, 1, 20],
        gold: app_commands.Range[int, 0, 10_000_000],
    ) -> None:
        if not await self._require_channel(
            interaction,
            self.bot.config.dwarfy_shop_channel_id,
            "DWARFY_SHOP_CHANNEL_ID",
        ):
            return
        listing_id = parse_listing_id(listing)
        if listing_id is None:
            await interaction.response.send_message(
                "Please buy by listing ID only, like `DWF-00017`.",
                ephemeral=True,
            )
            return

        row = await self.bot.db.get_listing(listing_id)
        if row is None:
            await interaction.response.send_message(
                f"I could not find listing `{listing_id}`.",
                ephemeral=True,
            )
            return
        if row["status"] != "available":
            await interaction.response.send_message(
                f"`{row['listing_id']}` is already {row['status']} and cannot be bought.",
                ephemeral=True,
            )
            return
        if (row.get("item_status") or "inventory") != "inventory":
            await interaction.response.send_message(
                f"`{row['listing_id']}` is not in Dwarfy's buyable inventory.",
                ephemeral=True,
            )
            return

        buy_roll = roll_buy_price(row["rarity"], int(row["cost_basis"]))
        item_name = listing_display_name(row)
        gold_available = int(gold)
        cost_basis = int(row["cost_basis"])
        buyer = interaction.user.mention
        buyer_character = character_label(character, int(level))
        origin = listing_origin_text(row)
        debt_owed = max(0, buy_roll.final_price - gold_available)
        debt_fine = 5_000 if debt_owed else 0
        debt_total = debt_owed + debt_fine
        debt_status = "unpaid" if debt_total else None
        receipt = build_buy_receipt(
            buyer=buyer,
            buyer_character=buyer_character,
            listing=row,
            item_name=item_name,
            origin_text=origin,
            buy_roll=buy_roll,
            gold_available=gold_available,
        )
        sold = await self.bot.db.mark_listing_sold(
            listing_id=row["listing_id"],
            buyer_user_id=str(interaction.user.id),
            buyer_display_name=_display_name(interaction.user),
            buyer_character_name=character.strip(),
            buyer_character_level=int(level),
            buy_price_roll_detail=buy_roll.roll_detail,
            final_sale_price=buy_roll.final_price,
            realized_profit=buy_roll.realized_profit,
            buyer_gold_available=gold_available,
            debt_owed=debt_owed,
            debt_fine=debt_fine,
            debt_total=debt_total,
            debt_status=debt_status,
            buy_base_asking_price=buy_roll.rolled_price,
            buy_haggling_roll=buy_roll.haggling_roll,
            buy_haggling_result=buy_roll.haggling_result,
            buy_discount_percent=buy_roll.discount_percent,
            buy_discounted_price=buy_roll.discounted_price,
            buy_final_item_price=buy_roll.final_price,
            buy_dwarfy_profit=buy_roll.realized_profit,
            buy_receipt_text=receipt,
        )
        if not sold:
            await interaction.response.send_message(
                "That listing is no longer available. Please browse again.",
                ephemeral=True,
            )
            return

        debt_block = ""
        if debt_total:
            debt_block = (
                "\nDwarfy debt consequence:\n\n"
                f"* Declared gold available: {gp(gold_available)}\n"
                f"* Price shortfall: {gp(debt_owed)}\n"
                f"* Contract default fine: {gp(debt_fine)}\n"
                f"* Total debt to clear: {gp(debt_total)}\n"
                "* Character status: Jailed and unplayable until the debt is paid.\n"
                f"* Item status: {item_name} is still yours, but it cannot be sold or traded until the debt is paid.\n\n"
                "Dwarfy already owned the item, prepared the sale, signed the shop ledger, "
                "and the collectors are painfully punctual."
            )
        if debt_total:
            payment_lines = (
                f"{buyer} as {buyer_character} cannot cover the {gp(buy_roll.final_price)} final price for {item_name}.\n"
                f"Dwarfy's Shop records the sale for {item_name}; the item transfers, and the debt is now enforceable."
            )
        else:
            payment_lines = (
                f"{buyer} as {buyer_character} pays {gp(buy_roll.final_price)} to Dwarfy's Shop for {item_name}.\n"
                f"Dwarfy's Shop receives {gp(buy_roll.final_price)} from {buyer} as {buyer_character} for {item_name}."
            )

        output = (
            f"{buyer} buys {item_name} from Dwarfy's Shop.\n\n"
            f"{buy_haggling_result_line(buy_roll, cost_basis)}\n\n"
            f"{payment_lines}\n\n"
            f"{receipt}{debt_block}\n\n"
            "Adventure log reminder:\n"
            "Record this downtime activity manually on the character's adventure log."
        )
        await send_text_response(interaction, output)

    @buy.autocomplete("listing")
    async def buy_listing_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        listings = await self.bot.db.list_available_listings()
        query = current.casefold().strip()
        choices: list[app_commands.Choice[str]] = []
        for listing in listings:
            listing_id = listing["listing_id"]
            display_name = listing_display_name(listing)
            label = f"{listing_id} - {display_name} ({listing['rarity']})"
            searchable = f"{listing_id} {display_name} {listing['rarity']}".casefold()
            if query and query not in searchable:
                continue
            choices.append(app_commands.Choice(name=label[:100], value=listing_id))
            if len(choices) >= 25:
                break
        return choices

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
        listing_id = parse_listing_id(listing)
        if listing_id is None:
            await interaction.response.send_message(
                "Please void by listing ID, like `DWF-00017`.",
                ephemeral=True,
            )
            return

        row = await self.bot.db.void_listing(listing_id, reason)
        if row is None:
            await interaction.response.send_message(
                f"I could not find an active listing `{listing_id}` to void.",
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
