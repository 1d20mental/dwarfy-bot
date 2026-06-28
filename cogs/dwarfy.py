"""Slash commands for Dwarfy's Shop."""

from __future__ import annotations

import asyncio
import csv
import io
import random
import re
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from services.database import STANDING_MIN_PRICE_GP as DATABASE_STANDING_MIN_PRICE_GP
from services.database import STANDING_TIERS
from services.equipment import (
    BaseCostResolution,
    base_cost_requires_variant,
    base_cost_variant_groups,
    resolve_base_cost,
)
from services.pricing import (
    buy_price_formula,
    direct_sell_price,
    possible_final_price_range,
    roll_broker_price,
    roll_buy_price,
)
from services.loot import RARITY_ORDER, pick_weighted_item
from services.sheets import (
    format_item_choices,
    is_generic_template_item,
    item_minimum_tier,
    item_has_dwarfy_base_cost,
    item_detail_summary,
    looks_like_pasted_detail_text,
    looks_like_pasted_item_text,
    normalize_rarity,
)
from utils.formatting import (
    DISCORD_MESSAGE_LIMIT,
    character_label,
    gp,
    mention_user,
    price_range_text,
    send_text_response,
    split_message,
)


LISTING_ID_RE = re.compile(r"\bDWF\s*[-\u2010-\u2015\u2212]?\s*(\d+)\b", re.IGNORECASE)
CLASSIFIED_ID_RE = re.compile(r"\bDWC\s*[-\u2010-\u2015\u2212]?\s*(\d+)\b", re.IGNORECASE)
MESSAGE_LINK_RE = re.compile(
    r"discord(?:app)?\.com/channels/(?:\d+|@me)/(?P<channel>\d{15,25})/(?P<message>\d{15,25})",
    re.IGNORECASE,
)
MESSAGE_ID_PAIR_RE = re.compile(r"\b(?P<channel>\d{15,25})\D+(?P<message>\d{15,25})\b")
CHARACTER_AUTOCOMPLETE_LABEL_RE = re.compile(
    r"\s+\((?P<level>\d{1,2})\)(?:\s+-\s+(?:active|registered|retired))?\s*$",
    re.IGNORECASE,
)
BROWSE_RARITY_CHOICES = [
    app_commands.Choice(name="Common", value="Common"),
    app_commands.Choice(name="Uncommon", value="Uncommon"),
    app_commands.Choice(name="Rare", value="Rare"),
    app_commands.Choice(name="Very Rare", value="Very Rare"),
    app_commands.Choice(name="Legendary", value="Legendary"),
]
BROWSE_RARITY_VALUES = {choice.value for choice in BROWSE_RARITY_CHOICES}
CHARACTER_ACTION_CHOICES = [
    app_commands.Choice(name="Add or update", value="save"),
    app_commands.Choice(name="List", value="list"),
    app_commands.Choice(name="Standing", value="standing"),
    app_commands.Choice(name="Set active", value="set_active"),
    app_commands.Choice(name="Retire", value="retire"),
]
HELP_TOPIC_CHOICES = [
    app_commands.Choice(name="Overview", value="overview"),
    app_commands.Choice(name="Session Loot", value="sessionloot"),
    app_commands.Choice(name="Characters", value="characters"),
    app_commands.Choice(name="Shop Browse / Inspect / Buy", value="shop"),
    app_commands.Choice(name="Sell / Broker", value="sell"),
    app_commands.Choice(name="Classifieds", value="classifieds"),
    app_commands.Choice(name="Dwarfy Standing", value="standing"),
    app_commands.Choice(name="Owner Stock", value="owner"),
    app_commands.Choice(name="Admin Tools", value="admin"),
    app_commands.Choice(name="Channels & Privacy", value="channels"),
]
HELP_TOPIC_VALUES = {choice.value for choice in HELP_TOPIC_CHOICES}
BROWSE_LISTING_CAP = 100
BROWSE_PAGE_SIZE = 10
DEFAULT_RANDOM_PERMANENT_COUNT = 10
DEFAULT_RANDOM_CONSUMABLE_COUNT = 15
CLASSIFIED_DEFAULT_COMMISSION_BPS = 2000
STANDING_MIN_PRICE_GP = DATABASE_STANDING_MIN_PRICE_GP
CLASSIFIED_HOLD_DAYS = 30
CLASSIFIED_RETURN_CHECK_HOURS = 24
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


def parse_classified_id(value: str) -> str | None:
    """Extract and normalize a Dwarfy Classifieds ID."""
    match = CLASSIFIED_ID_RE.search(value.strip().strip("`"))
    if match is None:
        return None
    number = int(match.group(1))
    return f"DWC-{number:05d}"


def parse_message_reference(value: str) -> tuple[int, int] | None:
    """Extract a channel ID and message ID from a Discord message link."""
    text = value.strip().strip("<>")
    match = MESSAGE_LINK_RE.search(text) or MESSAGE_ID_PAIR_RE.search(text)
    if match is None:
        return None
    return int(match.group("channel")), int(match.group("message"))


def edited_message_content(
    original: str,
    find: str,
    replace: str,
    *,
    replace_all: bool = False,
) -> str | None:
    """Return safely edited message content, or None when the text is absent."""
    needle = find.strip()
    if not needle:
        raise ValueError("Find text cannot be blank.")
    if needle not in original:
        return None
    edited = original.replace(needle, replace, -1 if replace_all else 1)
    if len(edited) > DISCORD_MESSAGE_LIMIT:
        raise ValueError("Edited message would be over Discord's 2,000 character limit.")
    return edited


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


def classified_display_name(classified: dict[str, Any]) -> str:
    """Use the classified display name, falling back to base item names."""
    return (
        classified.get("listing_display_name")
        or classified.get("item_name")
        or classified.get("item_clean_name")
        or "Unknown item"
    )


def clean_character_name(name: str) -> str:
    """Normalize user-entered character names without changing their spelling."""
    clean = re.sub(r"\s+", " ", name).strip()
    return CHARACTER_AUTOCOMPLETE_LABEL_RE.sub("", clean).strip()


def compact_character_name(name: str) -> str:
    """Normalize whitespace only, preserving legacy saved labels for cleanup."""
    return re.sub(r"\s+", " ", name).strip()


def character_choice_label(character: dict[str, Any]) -> str:
    """Build a compact autocomplete label for one registered character."""
    suffix = " - active" if int(character.get("is_default") or 0) else ""
    return f"{character['character_name']} ({character['character_level']}){suffix}"


def build_character_list_output(characters: list[dict[str, Any]]) -> str:
    """Build the private character registry list response."""
    if not characters:
        return (
            "You do not have any registered Dwarfy characters yet.\n\n"
            "Use `/dwarfy character action:Add or update name:<character> level:<level>` to register one."
        )
    lines = ["Your registered Dwarfy characters:", ""]
    for character in characters:
        status = "active" if int(character.get("is_default") or 0) else "registered"
        if int(character.get("is_retired") or 0):
            status = "retired"
        lines.append(f"* {character['character_name']} ({character['character_level']}) - {status}")
    lines.extend(
        [
            "",
            "Character fields on Dwarfy commands autocomplete these names. Registration is a convenience, not a character-sheet check.",
        ]
    )
    return "\n".join(lines)


def classified_post_headline(
    seller: str,
    character: str,
    level: int,
    item_name: str,
) -> str:
    """Build the first public line for a classified posting."""
    return f"{seller} as {character_label(character, int(level))} posts {item_name} on Dwarfy's Classifieds."


def source_with_page(source: str | None, page: str | None) -> str:
    if source and page:
        clean_page = page.strip()
        page_text = clean_page if clean_page.casefold().startswith("p") else f"p. {clean_page}"
        return f"{source}, {page_text}"
    return source or "Unknown"


def minimum_tier_for_min_apl(min_apl: int | None) -> int:
    """Map sheet Min APL to the server's minimum item tier."""
    if min_apl is None:
        return 1
    apl = int(min_apl)
    if apl <= 4:
        return 1
    if apl <= 10:
        return 2
    if apl <= 16:
        return 3
    return 4


def minimum_level_for_tier(tier: int) -> int:
    """Return the first character level in a D&D tier."""
    return {1: 1, 2: 5, 3: 11, 4: 17}.get(int(tier), 1)


def character_tier(level: int) -> int:
    """Return a character's D&D tier from level."""
    if int(level) <= 4:
        return 1
    if int(level) <= 10:
        return 2
    if int(level) <= 16:
        return 3
    return 4


def minimum_tier_text(*, min_apl: int | None = None, minimum_tier: int | None = None) -> str:
    """Return a player-facing minimum tier label."""
    tier = int(minimum_tier) if minimum_tier else minimum_tier_for_min_apl(min_apl)
    return f"Tier {tier} (Level {minimum_level_for_tier(tier)}+)"


def record_minimum_tier(record: dict[str, Any]) -> int:
    """Return the minimum tier stored on a listing/classified row."""
    stored_tier = record.get("minimum_tier")
    if stored_tier not in (None, ""):
        return int(stored_tier)
    min_apl = record.get("min_apl")
    return minimum_tier_for_min_apl(int(min_apl)) if min_apl not in (None, "") else 1


def record_minimum_tier_text(record: dict[str, Any]) -> str:
    """Return the minimum tier text for a listing/classified row."""
    return minimum_tier_text(minimum_tier=record_minimum_tier(record))


def sheet_item_minimum_tier_text(sheet_item: Any) -> str:
    """Return the minimum tier text for a SheetItem."""
    return minimum_tier_text(minimum_tier=item_minimum_tier(sheet_item))


def record_base_price(record: dict[str, Any]) -> int | None:
    """Return the stored Dwarfy Base Price for a listing when present."""
    value = record.get("base_price")
    if value in (None, ""):
        return None
    return int(value)


def resolve_sheet_item_base_price(
    sheet_item: Any,
    variant: str | None = None,
    cache: Any | None = None,
) -> BaseCostResolution:
    """Resolve a sheet row's Dwarfy base price for a specific variant."""
    if cache is not None and hasattr(cache, "resolve_base_cost_for_item"):
        return cache.resolve_base_cost_for_item(sheet_item, variant)
    if sheet_item.base_price is not None:
        price = int(sheet_item.base_price)
        return BaseCostResolution(price, detail=f"{price}gp", recognized=True)
    return resolve_base_cost(getattr(sheet_item, "base_price_text", ""), variant=variant)


def base_cost_error_text(sheet_item: Any, resolution: BaseCostResolution) -> str:
    """Build a friendly ephemeral message for unresolved Base Cost formulas."""
    raw = getattr(sheet_item, "base_price_text", "").strip()
    if resolution.needs_variant:
        return (
            f"`{sheet_item.name}` has Base Cost `{raw}`. Add a concrete variant such as "
            "Breastplate, Plate Armor, Longsword, Warhammer, Shield, arrows, bolts, or bullets "
            "so Dwarfy can add the mundane item cost."
        )
    if raw:
        return (
            f"`{sheet_item.name}` has Base Cost `{raw}`, but Dwarfy could not resolve it: "
            f"{resolution.error} It can still be posted in classifieds."
        )
    return (
        f"`{sheet_item.name}` does not have a Base Price in the sheet. "
        "It cannot be sold directly, brokered, or stocked by Dwarfy, but it can still be posted in classifieds."
    )


def tier_warning_text(record: dict[str, Any], *, item_name: str, character: str, level: int) -> str:
    """Warn when a buyer's level is below the item's minimum server tier."""
    required_tier = record_minimum_tier(record)
    if character_tier(int(level)) >= required_tier:
        return ""
    return (
        f"**Tier Warning: {item_name} is Minimum {record_minimum_tier_text(record)}. "
        f"{character} may not use this item under server rules until Tier {required_tier}.**"
    )


def enrich_record_tier_from_cache(record: dict[str, Any], cache: Any) -> dict[str, Any]:
    """Fill tier/base-price fields from the live sheet cache when possible.

    Tier is intentionally refreshed from the sheet because server item tier is
    policy data. That keeps older listings from displaying stale or Min APL-only
    tier values after moderators correct the sheet.
    """
    needs_base_price = record.get("base_price") in (None, "")
    if not getattr(cache, "loaded", False):
        return record

    for name in (
        record.get("item_clean_name"),
        record.get("base_item_name"),
        record.get("item_name"),
        record.get("listing_display_name"),
    ):
        if not name:
            continue
        match = cache.match_item(str(name), for_sell=False)
        if match.item is None:
            continue
        enriched = dict(record)
        enriched["min_apl"] = match.item.min_apl
        enriched["minimum_tier"] = item_minimum_tier(match.item)
        if needs_base_price:
            resolution = resolve_sheet_item_base_price(
                match.item,
                str(record.get("variant") or record.get("variant_details") or "").strip() or None,
                cache,
            )
            enriched["base_price"] = resolution.base_price
        return enriched
    return record


def parse_utc_datetime(value: str | None) -> datetime | None:
    """Parse the SQLite UTC timestamp format used by Dwarfy."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def age_text(value: str | None) -> str:
    """Return a compact human-readable age for a stored UTC timestamp."""
    parsed = parse_utc_datetime(value)
    if parsed is None:
        return "unknown"
    seconds = max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def timestamp_text(value: str | None) -> str:
    """Return a simple UTC timestamp for Discord audit text."""
    parsed = parse_utc_datetime(value)
    if parsed is None:
        return "unknown"
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


def is_classified_expired(classified: dict[str, Any]) -> bool:
    """Return True if an open classified is past its escrow deadline."""
    expires_at = parse_utc_datetime(classified.get("expires_at"))
    if expires_at is None:
        return False
    return expires_at <= datetime.now(timezone.utc)


def classified_status_text(classified: dict[str, Any]) -> str:
    """Return player-facing classified status."""
    if classified.get("returned_at"):
        return "Returned to seller"
    if classified.get("status") == "sold":
        return "Sold"
    if classified.get("status") == "voided":
        return "Voided"
    if is_classified_expired(classified):
        return "Expired, return pending"
    return "Open, held by Dwarfy"


def classified_hold_text(classified: dict[str, Any]) -> str:
    """Return the 30-day escrow line for one classified."""
    return (
        f"Held by Dwarfy until {timestamp_text(classified.get('expires_at'))}. "
        "The seller cannot use, sell, trade, or withdraw it during the hold."
    )


def rarity_color(rarity: str | None) -> discord.Color:
    return discord.Color(RARITY_COLORS.get(rarity or "", 0xC9A227))


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
    elif buy_roll.cost_basis_exception_applied:
        line += f" **Natural 20 exception: Dwarfy lets it go below his {gp(cost_basis)} cost basis.**"
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
                    f"Source: {listing['source'] or 'Unknown'} | Minimum Tier: "
                    f"{record_minimum_tier_text(listing)} | Price on buy: {price_range_text(low, high)} | "
                    f"Origin: {origin}"
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
        age_label = "Stocked" if listing.get("stock_source") == "owner_stock" else "Listed"
        age_line = f"{age_label}: {age_text(listing.get('created_at'))}"
        if listing.get("stock_batch_id"):
            age_line += f" | Batch: {listing['stock_batch_id']}"
        field_name = truncate_text(f"{listing['listing_id']} - {display_name}", 256)
        field_value = (
            f"{listing['rarity']} | {listing.get('source') or 'Unknown'} | "
            f"Minimum Tier: {record_minimum_tier_text(listing)} | "
            f"Price on buy: {price_range_text(low, high)}\n"
            f"Origin: {origin}\n"
            f"{age_line}"
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


def listing_rules_text(listing: dict[str, Any]) -> str:
    """Return private item rules/details text for inspect."""
    lines = [
        f"{listing_display_name(listing)}",
        f"Rarity: {listing.get('rarity') or 'Unknown'}",
        f"Minimum Tier: {record_minimum_tier_text(listing)}",
        f"Source: {source_with_page(listing.get('source'), listing.get('page'))}",
    ]
    if listing.get("display_detail"):
        lines.append(f"Detail: {listing['display_detail']}")
    if listing.get("short_description"):
        lines.extend(["", listing["short_description"]])
    if listing.get("rules_text"):
        lines.extend(["", "Rules Text:", listing["rules_text"]])
    if listing.get("json_notes"):
        lines.extend(["", f"JSON Notes: {listing['json_notes']}"])
    if listing.get("variant_instructions"):
        lines.extend(["", f"Variant instructions: {listing['variant_instructions']}"])
    return "\n".join(lines)


def listing_receipt_text(listing: dict[str, Any]) -> str:
    """Return stored receipt text for inspect buttons."""
    receipt = listing.get("buy_receipt_text") or listing.get("adventure_log_receipt") or listing.get("receipt_text")
    if receipt:
        return receipt
    return f"No stored receipt text found for {listing.get('listing_id', 'this listing')}."


def build_inspect_embed(listing: dict[str, Any]) -> discord.Embed:
    """Build the polished private listing inspect card."""
    display_name = listing_display_name(listing)
    base_price = record_base_price(listing)
    price_on_buy = "Base Price missing"
    if base_price is not None:
        low, high = possible_final_price_range(base_price, int(listing["cost_basis"]))
        price_on_buy = price_range_text(low, high)
    status = listing.get("status") or "unknown"
    item_status = listing.get("item_status") or ("inventory" if status == "available" else status)
    embed = discord.Embed(
        title=display_name,
        description=listing.get("short_description") or listing.get("display_detail") or "",
        color=rarity_color(listing.get("rarity")),
    )
    embed.add_field(name="Listing", value=listing["listing_id"], inline=True)
    embed.add_field(name="Rarity", value=listing.get("rarity") or "Unknown", inline=True)
    embed.add_field(name="Price on Buy", value=price_on_buy, inline=True)
    embed.add_field(name="Base Price", value=gp(base_price) if base_price is not None else "missing", inline=True)
    embed.add_field(name="Minimum Tier", value=record_minimum_tier_text(listing), inline=True)
    embed.add_field(name="Source", value=source_with_page(listing.get("source"), listing.get("page")), inline=True)
    embed.add_field(name="Origin", value=truncate_text(listing_origin_text(listing), 1024), inline=False)
    embed.add_field(name="Status", value=f"{status} / {item_status}", inline=True)
    embed.add_field(name="Sale Method", value=listing.get("sale_method") or "unknown/legacy", inline=True)
    embed.add_field(name="Cost Basis", value=gp(int(listing.get("cost_basis") or 0)), inline=True)
    if listing.get("variant") or listing.get("variant_details"):
        embed.add_field(name="Variant", value=listing.get("variant") or listing.get("variant_details"), inline=True)
    if listing.get("details"):
        embed.add_field(name="Notes", value=truncate_text(listing["details"], 1024), inline=False)
    if listing.get("stock_source") == "owner_stock":
        embed.add_field(name="Stock Age", value=age_text(listing.get("created_at")), inline=True)
        embed.add_field(name="Batch", value=listing.get("stock_batch_id") or "none", inline=True)
    if status == "sold":
        buyer = mention_user(listing.get("buyer_user_id"), listing.get("buyer_display_name"))
        buyer_character = character_label(listing.get("buyer_character_name"), listing.get("buyer_character_level"))
        embed.add_field(name="Buyer", value=f"{buyer} as {buyer_character}", inline=False)
        embed.add_field(name="Final Sale", value=gp(int(listing.get("final_sale_price") or 0)), inline=True)
        embed.add_field(name="Profit", value=gp(int(listing.get("realized_profit") or 0)), inline=True)
    if listing.get("debt_total"):
        embed.add_field(
            name="Debt Consequence",
            value=f"{gp(int(listing.get('debt_total') or 0))} ({listing.get('debt_status') or 'unpaid'})",
            inline=False,
        )
    embed.set_footer(text=f"Stocked {age_text(listing.get('created_at'))}")
    return embed


class BuyListingModal(discord.ui.Modal, title="Buy Dwarfy Listing"):
    """Modal opened by the inspect Buy button."""

    def __init__(self, cog: Any, listing_id: str) -> None:
        super().__init__()
        self.cog = cog
        self.listing_id = listing_id
        self.character = discord.ui.TextInput(label="Character name", max_length=100)
        self.level = discord.ui.TextInput(label="Character level", placeholder="1-20", max_length=2)
        self.gold = discord.ui.TextInput(label="Gold available", placeholder="0", max_length=10)
        self.add_item(self.character)
        self.add_item(self.level)
        self.add_item(self.gold)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            level = int(str(self.level.value).strip())
            gold = int(str(self.gold.value).strip())
        except ValueError:
            await interaction.response.send_message("Level and gold must be whole numbers.", ephemeral=True)
            return
        if not 1 <= level <= 20:
            await interaction.response.send_message("Level must be between 1 and 20.", ephemeral=True)
            return
        if not 0 <= gold <= 10_000_000:
            await interaction.response.send_message("Gold must be between 0 and 10,000,000gp.", ephemeral=True)
            return
        await self.cog._buy_listing_from_id(
            interaction,
            listing_id=self.listing_id,
            character=str(self.character.value).strip(),
            level=level,
            gold=gold,
        )


class InspectView(discord.ui.View):
    """Private controls for inspecting a single listing."""

    def __init__(self, cog: Any, listing: dict[str, Any], *, owner_id: int) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.listing = listing
        self.owner_id = owner_id
        if listing.get("status") != "available" or (listing.get("item_status") or "inventory") != "inventory":
            button = self.button_by_id("dwarfy_inspect_buy")
            if button is not None:
                button.disabled = True

    def button_by_id(self, custom_id: str) -> discord.ui.Button | None:
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id == custom_id:
                return child
        return None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("This inspect card belongs to the person who opened it.", ephemeral=True)
        return False

    @discord.ui.button(label="Show Rules", style=discord.ButtonStyle.secondary, custom_id="dwarfy_inspect_rules")
    async def show_rules(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await send_text_response(interaction, listing_rules_text(self.listing), ephemeral=True)

    @discord.ui.button(label="Show Receipt", style=discord.ButtonStyle.secondary, custom_id="dwarfy_inspect_receipt")
    async def show_receipt(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await send_text_response(interaction, listing_receipt_text(self.listing), ephemeral=True)

    @discord.ui.button(label="Buy This", style=discord.ButtonStyle.primary, custom_id="dwarfy_inspect_buy")
    async def buy_this(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(BuyListingModal(self.cog, self.listing["listing_id"]))


class BrokerConfirmView(discord.ui.View):
    """Private numbered confirmation for irreversible broker sales."""

    def __init__(
        self,
        cog: Any,
        *,
        owner_id: int,
        context: dict[str, Any],
        character: str,
        level: int,
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.owner_id = owner_id
        self.context = context
        self.character = character
        self.level = level
        self.completed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("This broker prompt belongs to the person who opened it.", ephemeral=True)
        return False

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="[1] Yes - spend 5 DTP and 25gp", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if self.completed:
            await interaction.response.send_message("This broker prompt has already been used.", ephemeral=True)
            return
        self.completed = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Broker sale confirmed. Dwarfy is rolling now...",
            view=self,
        )
        try:
            await self.cog._complete_broker_sale(
                interaction,
                context=self.context,
                character=self.character,
                level=self.level,
            )
        except Exception:
            traceback.print_exc()
            await interaction.followup.send(
                "Dwarfy hit an error after confirmation. Staff should check the bot logs before retrying; "
                "if a listing was created, use `/dwarfy inspect` or `/dwarfy void` to clean it up.",
                ephemeral=True,
            )
            return
        await interaction.followup.send("Broker sale complete. Public receipt posted.", ephemeral=True)

    @discord.ui.button(label="[2] No - cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if self.completed:
            await interaction.response.send_message("This broker prompt has already been used.", ephemeral=True)
            return
        self.completed = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Broker sale cancelled. No DTP or gold was spent, and no broker roll was made.",
            view=self,
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
            if item.loot_type == "Item"
            and (
                cache.item_has_dwarfy_pricing(item)
                if hasattr(cache, "item_has_dwarfy_pricing")
                else item_has_dwarfy_base_cost(item)
            )
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


def random_variant_from_base_cost(sheet_item: Any) -> str | None:
    """Choose a variant from the Base Cost formula when sheet metadata is thin."""
    groups = base_cost_variant_groups(getattr(sheet_item, "base_price_text", ""))
    if "ammunition" in groups:
        return random.choice(RANDOM_AMMUNITION_VARIANTS)
    if "shield" in groups:
        return "Shield"
    if "armor" in groups:
        return random.choice(RANDOM_ARMOR_VARIANTS)
    if "weapon" in groups:
        return random.choice(RANDOM_WEAPON_VARIANTS)
    return None


def resolve_random_stock_identity(sheet_item: Any) -> tuple[str, str | None, str | None]:
    """Return listing name, variant, and note text for a random stock item."""
    if not is_generic_template_item(sheet_item) and not base_cost_requires_variant(
        getattr(sheet_item, "base_price_text", "")
    ):
        return sheet_item.name, None, None

    variant = random_variant_from_sheet_item(sheet_item) or random_variant_from_base_cost(sheet_item)
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
        f"Minimum Tier: {record_minimum_tier_text(listing)}",
        f"Source: {source_with_page(listing.get('source'), listing.get('page'))}",
        f"Original source: {origin_text}",
        "",
        f"Dwarfy base price: {gp(buy_roll.rolled_price)}",
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
    elif buy_roll.cost_basis_exception_applied:
        lines.append(
            f"Natural 20 exception: Dwarfy lets this sale go below his {gp(int(listing['cost_basis']))} cost basis."
        )

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


def sell_validation_error(sheet_item, cache: Any | None = None) -> str | None:
    """Return an ephemeral validation error for /dwarfy sell, or None."""
    if not sheet_item.allowed:
        return f"`{sheet_item.name}` exists in the sheet, but Allowed is FALSE."
    if sheet_item.dwarfy_sell_eligible is False:
        return f"`{sheet_item.name}` is marked Dwarfy Sell Eligible=FALSE and cannot be sold to Dwarfy."
    has_pricing = item_has_dwarfy_base_cost(sheet_item)
    if cache is not None and hasattr(cache, "item_has_dwarfy_pricing"):
        has_pricing = cache.item_has_dwarfy_pricing(sheet_item)
    if not has_pricing:
        return (
            f"`{sheet_item.name}` does not have a Base Price in the sheet. "
            "It cannot be sold directly or brokered through Dwarfy, but it can still be posted in classifieds."
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
    minimum_tier: str,
    base_price: int,
    dtp_cost: int,
    gold_cost: int,
    seller_payout: int,
    status: str,
    roll_label: str | None = None,
    roll_value: int | None = None,
    details: str | None = None,
    variant_instructions: str | None = None,
    base_cost_detail: str | None = None,
) -> str:
    """Build the copyable Adventure Log Receipt stored with successful sales."""
    lines = [
        "Adventure Log Receipt:",
        f"Activity: {activity}",
        f"Character: {character.strip()} ({level})",
        f"Seller: {seller_mention}",
        f"Item: {listing_name}",
    ]
    if variant:
        lines.extend([f"Base item: {base_item_name}", f"Variant: {variant}"])
    if listing_id:
        lines.append(f"Listing: {listing_id}")
    lines.extend(
        [
            f"Rarity: {rarity}",
            f"Minimum Tier: {minimum_tier}",
            f"Item detail: {item_detail}",
            f"Source: {source_with_page(source, page)}",
            f"Base price: {gp(base_price)}",
        ]
    )
    if base_cost_detail:
        lines.append(f"Base cost resolved: {base_cost_detail}")
    lines.extend(
        [
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


def _public_variant_lines(variant_block: str) -> list[str]:
    block = variant_block.strip()
    if not block:
        return []
    return ["", "Details:", block]


def build_direct_sale_public_output(
    *,
    seller: str,
    seller_character: str,
    listing_name: str,
    listing_id: str,
    sale: Any,
    minimum_tier: str,
    base_cost_detail: str,
    variant_block: str,
) -> str:
    """Build the concise public direct-sale receipt."""
    lines = [
        "**Dwarfy Direct Sale**",
        f"{seller} as {seller_character} sold {listing_name} to Dwarfy's Shop.",
        "",
        f"Listing: {listing_id}",
        f"Seller payout / cost basis: {gp(sale.seller_payout)}",
        f"Base price: {gp(sale.base_price)}",
        "Rate: 40%",
        "Cost: 0 DTP, 0gp",
        f"Minimum Tier: {minimum_tier}",
        "Status: Final, no takebacks",
    ]
    if base_cost_detail:
        lines.insert(6, f"Base cost resolved: {base_cost_detail}")
    lines.extend(_public_variant_lines(variant_block))
    lines.extend(["", "Adventure log: Record this sale manually."])
    return "\n".join(lines)


def build_broker_sale_public_output(
    *,
    seller: str,
    seller_character: str,
    listing_name: str,
    listing_id: str | None,
    broker_roll: Any,
    minimum_tier: str,
    base_cost_detail: str,
    variant_block: str,
    disaster_message: str | None = None,
) -> str:
    """Build the concise public broker receipt."""
    lost = broker_roll.roll == 1
    lines = [
        "**Dwarfy Brokered Sale**",
        broker_sale_result_line(broker_roll),
        f"{seller} as {seller_character} brokered {listing_name} through Dwarfy's Shop.",
        "",
        f"Listing: {listing_id if listing_id else 'none - item lost'}",
        f"Seller payout / cost basis: {gp(broker_roll.seller_payout)}",
        f"Base price: {gp(broker_roll.base_price)}",
        "Cost: 5 DTP, 25gp",
        f"Minimum Tier: {minimum_tier}",
        f"Status: {'Item lost, final, no takebacks' if lost else 'Final, no takebacks'}",
    ]
    if base_cost_detail:
        lines.insert(7, f"Base cost resolved: {base_cost_detail}")
    if lost:
        lines.insert(-1, "Inventory: not added to Dwarfy inventory")
    lines.extend(_public_variant_lines(variant_block))
    if disaster_message:
        lines.extend(["", disaster_message])
    lines.extend(["", "Adventure log: Record this brokerage manually."])
    return "\n".join(lines)


def commission_bps_text(commission_bps: int | None) -> str:
    """Display a basis-point commission as a tidy percentage."""
    bps = CLASSIFIED_DEFAULT_COMMISSION_BPS if commission_bps is None else int(commission_bps)
    whole = bps // 100
    fraction = bps % 100
    if fraction == 0:
        return f"{whole}%"
    return f"{whole}.{str(fraction).rstrip('0')}%"


def classified_fee_for_price(
    asking_price: int,
    commission_bps: int = CLASSIFIED_DEFAULT_COMMISSION_BPS,
) -> int:
    """Dwarfy's classifieds commission, withheld from the seller payout."""
    return (int(asking_price) * int(commission_bps)) // 10_000


def classified_buyer_price(classified: dict[str, Any]) -> int:
    """Return the price the buyer pays for a classified posting."""
    return int(classified.get("asking_price") or 0)


def classified_commission(classified: dict[str, Any]) -> int:
    """Return Dwarfy's seller-paid classified commission."""
    stored_fee = classified.get("broker_fee")
    if stored_fee is not None:
        return int(stored_fee)
    return classified_fee_for_price(
        classified_buyer_price(classified),
        int(classified.get("commission_bps_locked") or CLASSIFIED_DEFAULT_COMMISSION_BPS),
    )


def classified_seller_net(classified: dict[str, Any]) -> int:
    """Return the seller's proceeds after Dwarfy withholds commission."""
    return max(0, classified_buyer_price(classified) - classified_commission(classified))


def standing_progress_text(summary: dict[str, Any]) -> str:
    """Return a compact progress line for Dwarfy Standing."""
    current = summary["current_tier"]
    next_tier = summary.get("next_tier")
    if not next_tier:
        return f"Top tier reached: {current['display_name']}."
    return (
        f"{gp(int(summary['lifetime_commission_gp']))} / {gp(int(next_tier['min_commission_gp']))} "
        f"toward {next_tier['display_name']}; "
        f"sales {int(summary['qualified_sales_count'])}/{int(next_tier['min_qualified_sales'])}; "
        f"unique buyers {int(summary['unique_buyer_count'])}/{int(next_tier['min_unique_buyers'])}."
    )


def build_standing_output(user_label: str, summary: dict[str, Any]) -> str:
    """Build the private Dwarfy Standing summary."""
    current = summary["current_tier"]
    next_tier = summary.get("next_tier")
    lines = [
        f"Dwarfy Standing for {user_label}",
        "",
        f"Tier: {current['display_name']}",
        f"Current classifieds commission: {commission_bps_text(current['commission_bps'])}",
        f"Classified commission generated: {gp(int(summary['lifetime_commission_gp']))}",
        f"Qualified sales: {int(summary['qualified_sales_count'])}",
        f"Unique buyers: {int(summary['unique_buyer_count'])}",
        f"Progress: {standing_progress_text(summary)}",
    ]
    if next_tier:
        lines.append(f"Next tier commission: {commission_bps_text(next_tier['commission_bps'])}")
    lines.extend(["", f'"{current.get("public_flavor") or "The Ledger remembers."}"', "The Ledger remembers."])
    return "\n".join(lines)


def build_standing_tiers_output(tiers: list[dict[str, Any]]) -> str:
    """Build the help text for the Dwarfy Standing ladder."""
    lines = ["Dwarfy Standing Tiers", ""]
    for tier in tiers:
        lines.append(
            f"{tier['display_name']} - {commission_bps_text(tier['commission_bps'])} commission"
        )
        lines.append(
            f"Requires: {gp(int(tier['min_commission_gp']))} commission, "
            f"{int(tier['min_qualified_sales'])} sale(s), "
            f"{int(tier['min_unique_buyers'])} unique buyer(s)."
        )
        if tier.get("public_flavor"):
            lines.append(f'"{tier["public_flavor"]}"')
        lines.append("")
    lines.append("Only completed classifieds where Dwarfy keeps seller-paid commission increase standing.")
    return "\n".join(lines).strip()


def standing_receipt_lines(result: dict[str, Any] | None) -> list[str]:
    """Return public receipt lines for a classified standing award."""
    if not result:
        return []
    after = result["after"]
    current = after["current_tier"]
    lines = [
        f"Dwarfy Standing: +{gp(int(result['standing_gain_gp']))}",
        f"Standing: {current['display_name']} - {standing_progress_text(after)}",
        f"Current commission on new listings: {commission_bps_text(current['commission_bps'])}",
    ]
    if result.get("promoted"):
        before = result["before"]["current_tier"]
        lines.append(f"Promotion: {before['display_name']} -> {current['display_name']}")
    if result.get("eligibility_status") != "eligible":
        lines.append(f"Standing note: {result.get('reason')}")
    elif result.get("pair_credit_multiplier") == 0.5:
        lines.append("Standing note: repeated buyer/seller pair within 30 days; 50% credit applied.")
    lines.append("The Ledger remembers.")
    return lines


def build_classified_embed(classified: dict[str, Any]) -> discord.Embed:
    """Build a private/public classified item card."""
    name = classified_display_name(classified)
    embed = discord.Embed(
        title=f"{classified['classified_id']} - {name}",
        description=classified.get("short_description") or classified.get("display_detail") or "",
        color=rarity_color(classified.get("rarity")),
    )
    seller = mention_user(classified.get("seller_user_id"), classified.get("seller_display_name"))
    seller_character = character_label(
        classified.get("seller_character_name"),
        classified.get("seller_character_level"),
    )
    embed.add_field(name="Rarity", value=classified.get("rarity") or "Unknown", inline=True)
    embed.add_field(name="Minimum Tier", value=record_minimum_tier_text(classified), inline=True)
    embed.add_field(name="Source", value=source_with_page(classified.get("source"), classified.get("page")), inline=True)
    embed.add_field(name="Status", value=classified_status_text(classified), inline=True)
    embed.add_field(name="Seller", value=f"{seller} as {seller_character}", inline=False)
    embed.add_field(name="Buyer Price", value=gp(classified_buyer_price(classified)), inline=True)
    embed.add_field(name="Seller Receives", value=gp(classified_seller_net(classified)), inline=True)
    embed.add_field(
        name="Dwarfy Commission",
        value=(
            f"{gp(classified_commission(classified))} "
            f"({commission_bps_text(classified.get('commission_bps_locked'))})"
        ),
        inline=True,
    )
    embed.add_field(name="Escrow Hold", value=classified_hold_text(classified), inline=False)
    if classified.get("status") == "sold":
        buyer = mention_user(classified.get("buyer_user_id"), classified.get("buyer_display_name"))
        buyer_character = character_label(classified.get("buyer_character_name"), classified.get("buyer_character_level"))
        embed.add_field(name="Buyer", value=f"{buyer} as {buyer_character}", inline=False)
    if classified.get("status") == "voided":
        embed.add_field(name="Voided By", value=classified.get("voided_by_display_name") or "unknown", inline=True)
        embed.add_field(name="Void Reason", value=classified.get("void_reason") or "none recorded", inline=False)
    if classified.get("variant"):
        embed.add_field(name="Variant", value=classified["variant"], inline=True)
    if classified.get("details"):
        embed.add_field(name="Notes", value=truncate_text(classified["details"], 1024), inline=False)
    embed.set_footer(text=f"Posted {age_text(classified.get('created_at'))}")
    return embed


def build_classified_browse_output(classifieds: list[dict[str, Any]]) -> str:
    """Build copyable classifieds browse text."""
    lines = [f"Dwarfy's Classifieds has {len(classifieds)} open posting{'s' if len(classifieds) != 1 else ''}:", ""]
    for classified in classifieds:
        seller = mention_user(classified.get("seller_user_id"), classified.get("seller_display_name"))
        seller_character = character_label(
            classified.get("seller_character_name"),
            classified.get("seller_character_level"),
        )
        lines.extend(
            [
                f"{classified['classified_id']} - {classified_display_name(classified)} - {classified['rarity']}",
                (
                    f"Buyer price: {gp(classified_buyer_price(classified))} | "
                    f"Minimum Tier: {record_minimum_tier_text(classified)} | "
                    f"Seller receives: {gp(classified_seller_net(classified))} | "
                    f"Dwarfy commission: {gp(classified_commission(classified))} "
                    f"({commission_bps_text(classified.get('commission_bps_locked'))})"
                ),
                f"Seller: {seller} as {seller_character}",
                classified_hold_text(classified),
                "",
            ]
        )
    return "\n".join(lines)


def build_classified_trade_log(
    classified: dict[str, Any],
    *,
    buyer: str,
    buyer_character: str,
) -> str:
    """Build the copyable trade-log text for a classified sale."""
    seller = mention_user(classified.get("seller_user_id"), classified.get("seller_display_name"))
    seller_character = character_label(
        classified.get("seller_character_name"),
        classified.get("seller_character_level"),
    )
    item_name = classified_display_name(classified)
    buyer_price = classified_buyer_price(classified)
    commission = classified_commission(classified)
    seller_net = classified_seller_net(classified)
    return (
        "Dwarfy Classifieds Trade Log\n\n"
        f"Buyer post:\n"
        f"{buyer} as {buyer_character} pays {gp(buyer_price)} to {seller} as {seller_character} for {item_name}.\n\n"
        f"Seller post:\n"
        f"{seller} as {seller_character} receives {gp(seller_net)} after Dwarfy withholds "
        f"{gp(commission)} from the sale of {item_name}.\n\n"
        f"Dwarfy fee record:\n"
        f"Dwarfy's Shop receives {gp(commission)} from {seller} as {seller_character} "
        f"for brokering {item_name}.\n\n"
        "Trade status: Final once both players update their logs."
    )


def build_classified_return_notice(classified: dict[str, Any]) -> str:
    """Build the public notice when a classified escrow expires."""
    seller = mention_user(classified.get("seller_user_id"), classified.get("seller_display_name"))
    seller_character = character_label(
        classified.get("seller_character_name"),
        classified.get("seller_character_level"),
    )
    return (
        "Dwarfy Classifieds Return Notice\n\n"
        f"{classified['classified_id']} - {classified_display_name(classified)} has reached the end of its "
        f"{CLASSIFIED_HOLD_DAYS}-day classified hold.\n\n"
        f"Dwarfy returns the item to {seller} as {seller_character}.\n"
        "No sale occurred.\n"
        "No broker fee was charged.\n\n"
        "Status: Returned to seller."
    )


def _help_field(embed: discord.Embed, name: str, lines: list[str]) -> None:
    """Add a help field without risking Discord's field length limit."""
    value = "\n".join(lines)
    embed.add_field(name=name, value=truncate_text(value, 1024), inline=False)


def build_help_embed(topic: str | None = None) -> discord.Embed:
    """Build the private `/dwarfy help` guide."""
    selected = (topic or "overview").casefold().strip()
    if selected not in HELP_TOPIC_VALUES:
        selected = "overview"

    embed = discord.Embed(color=discord.Color(0xC9A227))
    embed.set_footer(text="Dwarfy never checks character sheets. Players still update gold, DTP, and logs manually.")

    if selected == "sessionloot":
        embed.title = "Dwarfy Help - Session Loot"
        embed.description = "`/sessionloot` rolls a complete public loot package from the Google Sheet."
        _help_field(
            embed,
            "Command",
            [
                "`/sessionloot mode:Player session loot players:<1-20> apl:<1-20>`",
                "`/sessionloot mode:DM incentive loot pool apl:<level> new_hire_players:<0-10>`",
                "Optional: `tag` and `creature_type` for player sessions; `tag` also works for DM pools.",
                "If the session loot channel is configured, use it there.",
            ],
        )
        _help_field(
            embed,
            "What It Rolls",
            [
                "Loot priority rolls for generic Player 1, Player 2, etc.",
                "Permanent slots: `players // 2`.",
                "Consumable slots: remaining players.",
                "Rarity comes from the DMG tier d100 table.",
                "Final item selection uses the sheet's `Weight` column.",
            ],
        )
        _help_field(
            embed,
            "DM Incentive Mode",
            [
                "Always rolls one baseline permanent option and one consumable option.",
                "The DM chooses one item total from the permanent and consumable options.",
                "New Hires add one extra permanent option per qualifying new player.",
                "Jump Start and Tour de Tiers each add one extra permanent option when they apply.",
                "The bot does not output XP, GP, or DTP for this mode.",
            ],
        )
        _help_field(
            embed,
            "Sheet Rules",
            [
                "`Roll Rarity` controls session loot rarity.",
                "`Base Price` controls Dwarfy pricing.",
                "`Allowed=FALSE` and `Session Eligible=FALSE` block session loot.",
                "If a rolled rarity has no pool, the bot fills from the nearest valid fallback rarity.",
            ],
        )
        return embed

    if selected == "characters":
        embed.title = "Dwarfy Help - Characters"
        embed.description = "Registering characters makes Dwarfy command autocomplete cleaner. It does not check character sheets."
        _help_field(
            embed,
            "Register",
            [
                "`/dwarfy character action:Add or update name:<character> level:<1-20>` saves or restores one character.",
                "`make_active:True` marks that character as your active/default Dwarfy character.",
                "The first registered active character becomes active automatically.",
            ],
        )
        _help_field(
            embed,
            "Manage",
            [
                "`/dwarfy character action:List` privately lists your registered characters.",
                "`/dwarfy character action:Set active name:<character>` changes your active character.",
                "`/dwarfy character action:Retire name:<character>` hides an old character from autocomplete.",
                "Run Add or update again to update a saved level.",
            ],
        )
        _help_field(
            embed,
            "Where It Helps",
            [
                "Character fields on sell, broker, buy, and classifieds commands autocomplete your active characters.",
                "Successful transactions also remember the submitted character name and level for future autocomplete.",
            ],
        )
        return embed

    if selected == "shop":
        embed.title = "Dwarfy Help - Shop Browsing & Buying"
        embed.description = "Use these in the Dwarfy shop channel."
        _help_field(
            embed,
            "Browse And Inspect",
            [
                "`/dwarfy browse` shows available Dwarfy inventory privately.",
                "Use `rarity`, `max_price`, or `search` to narrow it.",
                "`/dwarfy inspect listing:DWF-00001` shows a private item card.",
                "Inspect buttons can show rules, show receipt, or start buying.",
            ],
        )
        _help_field(
            embed,
            "Buy",
            [
                "`/dwarfy buy listing:DWF-00001 character:<name> level:<1-20> gold:<amount>`",
                "Buying uses the item listing ID only.",
                "Dwarfy uses the sheet Base Price, then rolls a d20 haggling discount.",
                "Dwarfy normally keeps his cost-basis floor, but a natural 20 can break it.",
                "If your level is below the item's minimum tier, the receipt shows a bold server-rule warning.",
            ],
        )
        _help_field(
            embed,
            "Debt Warning",
            [
                "If declared gold is too low, the sale still completes.",
                "The character owes the shortfall plus a 5,000gp fine.",
                "That character is jailed/unplayable until resolved by staff.",
            ],
        )
        return embed

    if selected == "sell":
        embed.title = "Dwarfy Help - Selling To Dwarfy"
        embed.description = "Use these in the Dwarfy sell channel. The sheet controls which priced magic items are sellable."
        _help_field(
            embed,
            "Direct Sale",
            [
                "`/dwarfy sell character:<name> level:<1-20> item:<item>`",
                "No DTP cost.",
                "No gold cost.",
                "No roll.",
                "Pays 40% of the base price and creates a Dwarfy inventory listing.",
            ],
        )
        _help_field(
            embed,
            "Brokered Sale",
            [
                "`/dwarfy broker character:<name> level:<1-20> item:<item>`",
                "Costs 5 DTP and 25gp manually.",
                "After you submit, Dwarfy privately asks `[1] Yes` or `[2] No` before rolling.",
                "Rolls 1d20 for payout.",
                "Can pay more than direct sale.",
                "Natural 1 loses the item and creates no buyable inventory.",
            ],
        )
        _help_field(
            embed,
            "Generic Items",
            [
                "Use `variant` for templates like `+1 Weapon` or `Adamantine Armor`.",
                "Example: `item:+1 Weapon variant:Longsword`.",
                "Use `details` only for custom notes, not full rules text.",
            ],
        )
        return embed

    if selected == "classifieds":
        embed.title = "Dwarfy Help - Classifieds"
        embed.description = "Player-to-player magic item postings. Dwarfy withholds his fee from the seller."
        _help_field(
            embed,
            "Post A Classified",
            [
                "`/dwarfy classified_post character:<name> level:<1-20> item:<item> price:<gp>`",
                "The price is what the buyer pays.",
                "When it sells, Dwarfy withholds the seller's locked Dwarfy Standing commission.",
                "The seller receives the buyer price minus Dwarfy's commission.",
                "Completed classified commissions increase the seller's Dwarfy Standing.",
                "The item does not enter Dwarfy inventory.",
                f"Dwarfy holds the item for {CLASSIFIED_HOLD_DAYS} days; the seller cannot use, sell, trade, or withdraw it during that hold.",
            ],
        )
        _help_field(
            embed,
            "Browse And Buy",
            [
                "`/dwarfy classified_browse` privately shows open postings.",
                "`/dwarfy classified_inspect classified:DWC-00001` shows one posting.",
                "`/dwarfy classified_buy classified:DWC-00001 character:<name> level:<1-20>`",
                "The buy command posts copyable trade-log text for both players.",
            ],
        )
        _help_field(
            embed,
            "IDs",
            [
                "Dwarfy inventory listings use `DWF-00001`.",
                "Classified postings use `DWC-00001`.",
            ],
        )
        return embed

    if selected == "standing":
        embed.title = "Dwarfy Help - Dwarfy Standing"
        embed.description = "Dwarfy Standing lowers future classified commission for sellers who make Dwarfy money."
        _help_field(
            embed,
            "How It Works",
            [
                "Only completed classified sales count.",
                "Standing gain equals the commission Dwarfy actually keeps from the seller.",
                "Standing belongs to your Discord user, not one character.",
                "A classified locks your current commission when posted; old listings do not change if you rank up.",
                f"Sales below {gp(STANDING_MIN_PRICE_GP)} do not grant standing.",
                "`/dwarfy character action:Standing` privately shows your progress.",
            ],
        )
        _help_field(embed, "Tier Ladder", build_standing_tiers_output(STANDING_TIERS).splitlines())
        return embed

    if selected == "owner":
        embed.title = "Dwarfy Help - Owner Stock"
        embed.description = "Owner-only tools for manually stocking Dwarfy's inventory."
        _help_field(
            embed,
            "Stock Commands",
            [
                "`/dwarfy stock_add` adds a specific sheet item.",
                "`/dwarfy stock_random` creates a weighted random batch.",
                "`/dwarfy stock_clear confirm:True` voids current owner stock only.",
                "`/dwarfy stock_gold` records shop gold in the ledger.",
            ],
        )
        _help_field(
            embed,
            "Random Stock",
            [
                "Defaults to 10 permanent and 15 consumable items.",
                "Uses sheet weights and an owner-stock rarity table.",
                "Generic items get variants when possible.",
                "Ammunition stocks as 20 arrows, 20 bolts, or 10 bullets.",
            ],
        )
        return embed

    if selected == "admin":
        embed.title = "Dwarfy Help - Admin Tools"
        embed.description = "These require one of the configured admin/mod role names."
        _help_field(
            embed,
            "Audit And Maintenance",
            [
                "`/dwarfy reload` refreshes the Google Sheet cache.",
                "`/dwarfy stats` shows the shop dashboard.",
                "`/dwarfy history` shows recent ledger entries.",
                "`/dwarfy export` sends CSV exports for listings, ledger, and classifieds.",
            ],
        )
        _help_field(
            embed,
            "Corrections",
            [
                "`/dwarfy edit_post` corrects exact text in a Dwarfy bot message without rerolling.",
                "`/dwarfy void listing:DWF-00001 reason:<why>` voids a shop listing.",
                "`/dwarfy classified_void classified:DWC-00001 reason:<why>` voids a classified.",
                "`/dwarfy debt_resolve listing:DWF-00001 reason:<how>` marks debt resolved.",
                "`/dwarfy restock_status` checks owner-stock freshness.",
            ],
        )
        return embed

    if selected == "channels":
        embed.title = "Dwarfy Help - Channels & Privacy"
        embed.description = "Dwarfy uses channel restrictions so audit trails stay readable."
        _help_field(
            embed,
            "Channel Rules",
            [
                "`/dwarfy sell` and `/dwarfy broker` use the configured sell channel.",
                "Shop commands use the configured shop channel.",
                "Classified commands use the configured classifieds channel, falling back to the shop channel if none is set.",
                "`/sessionloot` uses the session loot channel only if one is configured.",
                "Wrong-channel errors are private.",
            ],
        )
        _help_field(
            embed,
            "Privacy",
            [
                "Browse, inspect, help, stats, history, export, and reload are private.",
                "Completed sales, buys, broker results, classifieds, and session loot are public.",
                "Public outputs are plain text where audit/search matters.",
            ],
        )
        return embed

    embed.title = "Dwarfy Bot Help"
    embed.description = "A slash-command-only D&D shop and session loot bot."
    _help_field(
        embed,
        "Start Here",
        [
            "`/dwarfy ping` checks whether the shop is open.",
            "`/dwarfy help topic:<topic>` shows a focused guide.",
            "`/dwarfy character action:Add or update name:<name> level:<level>` saves character names for autocomplete.",
            "`/sessionloot mode:Player session loot players:<count> apl:<level>` rolls public session loot.",
        ],
    )
    _help_field(
        embed,
        "Player Shop Commands",
        [
            "`/dwarfy character` - save, list, activate, or retire your characters.",
            "`/dwarfy browse` - privately browse Dwarfy inventory.",
            "`/dwarfy inspect` - view item details and buttons.",
            "`/dwarfy buy` - buy by `DWF-` listing ID.",
            "`/dwarfy sell` - direct 40% sale to Dwarfy.",
            "`/dwarfy broker` - rolled downtime sale to Dwarfy.",
        ],
    )
    _help_field(
        embed,
        "Player Classifieds",
        [
            "`/dwarfy classified_post` - post a player-to-player sale.",
            "`/dwarfy classified_browse` - privately browse postings.",
            "`/dwarfy classified_buy` - buy by `DWC-` classified ID and get trade-log text.",
            "`/dwarfy character action:Standing` - check your Dwarfy Standing.",
        ],
    )
    _help_field(
        embed,
        "Admin / Owner",
        [
            "Admins: `reload`, `stats`, `history`, `export`, `void`, `debt_resolve`.",
            "Owner: `stock_add`, `stock_random`, `stock_clear`, `stock_gold`.",
            "Use topic choices for details.",
        ],
    )
    return embed


class Dwarfy(commands.GroupCog, name="dwarfy"):
    """Commands under the /dwarfy group."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        super().__init__()

    async def cog_load(self) -> None:
        self._classified_return_loop.start()

    def cog_unload(self) -> None:
        self._classified_return_loop.cancel()

    def _classified_channel_id(self) -> int | None:
        """Return the dedicated classified channel, falling back to shop."""
        return (
            self.bot.config.dwarfy_classified_channel_id
            or self.bot.config.dwarfy_shop_channel_id
        )

    async def _require_classified_channel(self, interaction: discord.Interaction) -> bool:
        channel_id = self._classified_channel_id()
        if channel_id is None:
            await interaction.response.send_message(
                "Dwarfy Classifieds is not configured yet. Set DWARFY_CLASSIFIED_CHANNEL_ID or DWARFY_SHOP_CHANNEL_ID in `.env`.",
                ephemeral=True,
            )
            return False
        if interaction.channel_id != channel_id:
            await interaction.response.send_message(
                "Dwarfy points at the classifieds board. Please use this command in the configured Dwarfy Classifieds channel.",
                ephemeral=True,
            )
            return False
        return True

    async def _classified_notice_channel(self) -> discord.abc.Messageable | None:
        channel_id = self._classified_channel_id()
        if channel_id is None:
            return None
        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        return channel if hasattr(channel, "send") else None

    async def _process_expired_classifieds(self) -> None:
        """Return expired classifieds and post any missing return notices."""
        expired_rows = await self.bot.db.expired_open_classifieds()
        for row in expired_rows:
            returned = await self.bot.db.return_expired_classified(row["classified_id"])
            if returned:
                print(f"[classifieds] Returned expired classified {returned['classified_id']}.")

        pending_notices = await self.bot.db.classified_return_notices_pending()
        if not pending_notices:
            return
        channel = await self._classified_notice_channel()
        if channel is None:
            print("[classifieds] Return notices pending, but no classified/shop channel is configured.")
            return
        for row in pending_notices:
            await channel.send(build_classified_return_notice(row))
            await self.bot.db.mark_classified_return_notice_sent(row["classified_id"])

    @tasks.loop(hours=CLASSIFIED_RETURN_CHECK_HOURS)
    async def _classified_return_loop(self) -> None:
        try:
            await self._process_expired_classifieds()
        except Exception as exc:
            print(f"[classifieds] Expired classified return check failed: {exc}")

    @_classified_return_loop.before_loop
    async def _before_classified_return_loop(self) -> None:
        await self.bot.wait_until_ready()

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

    async def _require_shop_channel(self, interaction: discord.Interaction) -> bool:
        """Require the configured Dwarfy shop channel for inventory browsing and buying."""
        return await self._require_channel(
            interaction,
            self.bot.config.dwarfy_shop_channel_id,
            "DWARFY_SHOP_CHANNEL_ID",
        )

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

    @app_commands.command(name="help", description="Show a private guide to Dwarfy Bot commands.")
    @app_commands.describe(topic="Optional help topic.")
    @app_commands.choices(topic=HELP_TOPIC_CHOICES)
    async def help(self, interaction: discord.Interaction, topic: str | None = None) -> None:
        await interaction.response.send_message(
            embed=build_help_embed(topic),
            ephemeral=True,
        )

    async def _remember_character(
        self,
        interaction: discord.Interaction,
        character: str,
        level: int,
    ) -> None:
        """Remember successfully used character names without interrupting transactions."""
        name = clean_character_name(character)
        if not name:
            return
        try:
            await self.bot.db.save_character(
                user_id=str(interaction.user.id),
                user_display_name=_display_name(interaction.user),
                character_name=name,
                character_level=int(level),
            )
        except Exception as exc:
            print(f"[characters] Could not remember character {name!r}: {exc}")

    async def _character_name_choices(
        self,
        interaction: discord.Interaction,
        current: str,
        *,
        include_retired: bool = False,
    ) -> list[app_commands.Choice[str]]:
        query = current.casefold().strip()
        rows = await self.bot.db.list_characters(
            user_id=str(interaction.user.id),
            include_retired=include_retired,
        )
        choices: list[app_commands.Choice[str]] = []
        for row in rows:
            name = row["character_name"]
            searchable = f"{name} {row['character_level']}".casefold()
            if query and query not in searchable:
                continue
            choices.append(
                app_commands.Choice(
                    name=character_choice_label(row)[:100],
                    value=name[:100],
                )
            )
            if len(choices) >= 25:
                break
        return choices

    @app_commands.command(name="character", description="Save, list, activate, or retire your Dwarfy characters.")
    @app_commands.describe(
        action="What to do with your character registry.",
        name="Character name for add/update, set active, or retire.",
        level="Character level for add/update.",
        make_active="Mark this character as your active/default character.",
        include_retired="When listing, include retired characters.",
    )
    @app_commands.choices(action=CHARACTER_ACTION_CHOICES)
    async def character(
        self,
        interaction: discord.Interaction,
        action: str,
        name: str | None = None,
        level: int | None = None,
        make_active: bool = False,
        include_retired: bool = False,
    ) -> None:
        if action == "list":
            rows = await self.bot.db.list_characters(
                user_id=str(interaction.user.id),
                include_retired=include_retired,
            )
            await interaction.response.send_message(build_character_list_output(rows), ephemeral=True)
            return

        if action == "standing":
            summary = await self.bot.db.get_user_standing(str(interaction.user.id))
            await interaction.response.send_message(
                build_standing_output(interaction.user.mention, summary),
                ephemeral=True,
            )
            return

        raw_character_name = compact_character_name(name or "")
        character_name = clean_character_name(raw_character_name)
        if not character_name:
            await interaction.response.send_message(
                "Give me a character name for that character action.",
                ephemeral=True,
            )
            return

        if action == "save":
            if level is None or level < 1 or level > 20:
                await interaction.response.send_message(
                    "Give me a character level from 1 to 20 for Add or update.",
                    ephemeral=True,
                )
                return
            row = await self.bot.db.save_character(
                user_id=str(interaction.user.id),
                user_display_name=_display_name(interaction.user),
                character_name=character_name,
                character_level=int(level),
                make_default=make_active,
            )
            active_text = "yes" if int(row.get("is_default") or 0) else "no"
            await interaction.response.send_message(
                (
                    "Character saved.\n\n"
                    f"Name: {row['character_name']}\n"
                    f"Level: {row['character_level']}\n"
                    f"Active: {active_text}\n\n"
                    "Dwarfy command character fields will autocomplete this name."
                ),
                ephemeral=True,
            )
            return

        if action in {"set_active", "retire"} and raw_character_name != character_name:
            exact_legacy_row = await self.bot.db.get_character(
                user_id=str(interaction.user.id),
                character_name=raw_character_name,
            )
            if exact_legacy_row is not None:
                character_name = raw_character_name

        if action == "set_active":
            row = await self.bot.db.set_default_character(
                user_id=str(interaction.user.id),
                character_name=character_name,
            )
            if row is None:
                await interaction.response.send_message(
                    "I could not find an active registered character with that name.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                f"Active Dwarfy character set to {row['character_name']} ({row['character_level']}).",
                ephemeral=True,
            )
            return

        if action == "retire":
            row = await self.bot.db.retire_character(
                user_id=str(interaction.user.id),
                character_name=character_name,
            )
            if row is None:
                await interaction.response.send_message(
                    "I could not find an active registered character with that name.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                f"Retired {row['character_name']} ({row['character_level']}) from Dwarfy autocomplete.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message("Choose one of the listed character actions.", ephemeral=True)

    @character.autocomplete("name")
    async def character_name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._character_name_choices(interaction, current, include_retired=True)

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
        validation_error = sell_validation_error(sheet_item, self.bot.sheet_cache)
        if validation_error:
            await interaction.response.send_message(validation_error, ephemeral=True)
            return None

        variant_clean = (variant or "").strip() or None
        details_clean = (details or "").strip() or None
        is_template = (
            is_generic_template_item(sheet_item)
            or base_cost_requires_variant(getattr(sheet_item, "base_price_text", ""))
            or self.bot.sheet_cache.item_requires_pricing_variant(sheet_item)
        )

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

        base_cost_resolution = resolve_sheet_item_base_price(sheet_item, variant_clean, self.bot.sheet_cache)
        if base_cost_resolution.base_price is None:
            await interaction.response.send_message(
                base_cost_error_text(sheet_item, base_cost_resolution),
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
        if sheet_item.base_price is None and base_cost_resolution.detail:
            variant_lines.append(f"* Base cost resolved: {base_cost_resolution.detail}")

        return {
            "sheet_item": sheet_item,
            "variant_clean": variant_clean,
            "details_clean": details_clean,
            "base_price": int(base_cost_resolution.base_price),
            "base_cost_detail": base_cost_resolution.detail if sheet_item.base_price is None else "",
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
            min_apl=sheet_item.min_apl,
            minimum_tier=item_minimum_tier(sheet_item),
            base_price=context["base_price"],
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
            if not item_has_dwarfy_base_cost(sheet_item):
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
        return self.bot.sheet_cache.autocomplete_variant_options(
            item_name=item_name,
            query=current,
            for_sell=False,
        )

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

        variant_clean = (variant or "").strip() or None
        notes_clean = (notes or "").strip() or None
        is_template = (
            is_generic_template_item(sheet_item)
            or base_cost_requires_variant(getattr(sheet_item, "base_price_text", ""))
            or self.bot.sheet_cache.item_requires_pricing_variant(sheet_item)
        )
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

        base_cost_resolution = resolve_sheet_item_base_price(sheet_item, variant_clean, self.bot.sheet_cache)
        if base_cost_resolution.base_price is None:
            await interaction.response.send_message(
                base_cost_error_text(sheet_item, base_cost_resolution),
                ephemeral=True,
            )
            return None

        return {
            "sheet_item": sheet_item,
            "variant_clean": variant_clean,
            "notes_clean": notes_clean,
            "listing_name": resolved_listing_name(sheet_item.name, variant_clean),
            "base_price": int(base_cost_resolution.base_price),
            "base_cost_detail": base_cost_resolution.detail if sheet_item.base_price is None else "",
        }

    async def _resolve_classified_context(
        self,
        interaction: discord.Interaction,
        *,
        item: str,
        variant: str | None,
        details: str | None,
    ) -> dict[str, Any] | None:
        """Validate a player-posted classified item."""
        if not await self._require_classified_channel(interaction):
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
                f"`{sheet_item.name}` is Allowed=FALSE and cannot be posted to Dwarfy's Classifieds.",
                ephemeral=True,
            )
            return None
        if sheet_item.loot_type != "Item":
            await interaction.response.send_message(
                f"`{sheet_item.name}` is Loot Type={sheet_item.loot_type}; classifieds can only post actual items.",
                ephemeral=True,
            )
            return None
        variant_clean = (variant or "").strip() or None
        details_clean = (details or "").strip() or None
        is_template = (
            is_generic_template_item(sheet_item)
            or base_cost_requires_variant(getattr(sheet_item, "base_price_text", ""))
            or self.bot.sheet_cache.item_requires_pricing_variant(sheet_item)
        )
        if variant_clean and not is_template:
            await interaction.response.send_message(
                "Variant is only used for generic/template items. This item is already specific.",
                ephemeral=True,
            )
            return None
        if details_clean and not is_template and looks_like_pasted_detail_text(details_clean):
            await interaction.response.send_message(
                "Use details only for short custom trade notes, not full item rules text.",
                ephemeral=True,
            )
            return None

        variant_note = ""
        if variant_clean and sheet_item.variant_option_list:
            option_names = {option.casefold() for option in sheet_item.variant_option_list}
            if variant_clean.casefold() not in option_names:
                variant_note = "Variant note: This variant was not in the sheet's suggested options."

        return {
            "sheet_item": sheet_item,
            "variant_clean": variant_clean,
            "details_clean": details_clean,
            "listing_name": resolved_listing_name(sheet_item.name, variant_clean),
            "variant_note": variant_note,
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
        base_price: int,
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
            min_apl=sheet_item.min_apl,
            minimum_tier=item_minimum_tier(sheet_item),
            base_price=base_price,
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
        base_price = context["base_price"]
        default_cost_basis = direct_sell_price(base_price).seller_payout
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
                    base_price=base_price,
                    batch_id=batch_id,
                )
            )

        listing_ids = ", ".join(row["listing_id"] for row in rows)
        output = (
            "Owner stock added.\n\n"
            f"Item: {context['listing_name']}\n"
            f"Quantity: {len(rows)}\n"
            f"Rarity: {sheet_item.rarity}\n"
            f"Minimum Tier: {sheet_item_minimum_tier_text(sheet_item)}\n"
            f"Source: {source_with_page(sheet_item.source, sheet_item.page)}\n"
            f"Base price: {gp(base_price)}\n"
            f"{('Base cost resolved: ' + context['base_cost_detail'] + chr(10)) if context['base_cost_detail'] else ''}"
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
        permanent_count="Permanent item count. Defaults to 10.",
        consumable_count="Consumable item count. Defaults to 15.",
        apl="APL band to use for sheet eligibility. Defaults to 10.",
        tag="Optional tag preference.",
        clear_first="Void existing owner stock before adding the new batch.",
    )
    async def stock_random(
        self,
        interaction: discord.Interaction,
        permanent_count: int = DEFAULT_RANDOM_PERMANENT_COUNT,
        consumable_count: int = DEFAULT_RANDOM_CONSUMABLE_COUNT,
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
        rarity_counts: dict[str, int] = {}
        type_counts = {"Permanent": 0, "Consumable": 0}
        total_cost_basis = 0

        async def add_random_slot(index: int, *, consumable: bool) -> None:
            nonlocal total_cost_basis
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
            listing_name, variant, variant_note = resolve_random_stock_identity(sheet_item)
            base_cost_resolution = resolve_sheet_item_base_price(sheet_item, variant, self.bot.sheet_cache)
            if base_cost_resolution.base_price is None:
                slot_type = "Consumable" if consumable else "Permanent"
                audit_lines.append(
                    f"{slot_type} {index}: d100 {d100} -> {rolled_rarity} -> skipped, {sheet_item.name} could not resolve Base Cost."
                )
                return
            if not consumable:
                selected_permanent_names.add(sheet_item.name.casefold())
            cost_basis = direct_sell_price(base_cost_resolution.base_price).seller_payout
            stock_note = f"Random owner stock batch {batch_id}."
            if variant_note:
                stock_note = f"{stock_note} {variant_note}"
            if sheet_item.base_price is None and base_cost_resolution.detail:
                stock_note = f"{stock_note} Base cost resolved: {base_cost_resolution.detail}."
            row = await self._create_owner_stock_listing(
                interaction,
                sheet_item=sheet_item,
                listing_name=listing_name,
                variant=variant,
                notes=stock_note,
                cost_basis=cost_basis,
                base_price=int(base_cost_resolution.base_price),
                batch_id=batch_id,
            )
            created.append(row)
            rarity_counts[sheet_item.rarity] = rarity_counts.get(sheet_item.rarity, 0) + 1
            type_counts["Consumable" if consumable else "Permanent"] += 1
            total_cost_basis += cost_basis
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
            f"Permanent / Consumable: {type_counts['Permanent']} / {type_counts['Consumable']}",
            f"Total cost basis added: {gp(total_cost_basis)}",
            "Rarity breakdown: "
            + (
                ", ".join(f"{rarity}: {rarity_counts[rarity]}" for rarity in RARITY_ORDER if rarity_counts.get(rarity))
                if rarity_counts
                else "none"
            ),
        ]
        notable = sorted(
            created,
            key=lambda row: (RARITY_ORDER.index(row["rarity"]) if row["rarity"] in RARITY_ORDER else -1, int(row["cost_basis"])),
            reverse=True,
        )[:5]
        if notable:
            lines.extend(["", "Notable stock:"])
            lines.extend(
                f"* {row['listing_id']} - {listing_display_name(row)} ({row['rarity']})"
                for row in notable
            )
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

    @app_commands.command(name="sell", description="Sell a magic item to Dwarfy's Shop.")
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
        sale = direct_sell_price(context["base_price"])
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
            minimum_tier=sheet_item_minimum_tier_text(sheet_item),
            base_price=sale.base_price,
            base_cost_detail=context["base_cost_detail"],
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
        await self._remember_character(interaction, character, int(level))
        output = build_direct_sale_public_output(
            seller=context["seller"],
            seller_character=context["seller_character"],
            listing_name=context["listing_name"],
            listing_id=listing["listing_id"],
            sale=sale,
            minimum_tier=sheet_item_minimum_tier_text(sheet_item),
            base_cost_detail=context["base_cost_detail"],
            variant_block=context["variant_block"],
        )
        await send_text_response(interaction, output)

    async def _send_public_broker_output(self, interaction: discord.Interaction, output: str) -> None:
        """Post broker audit output publicly even when confirmation was private."""
        chunks = split_message(output)
        try:
            for chunk in chunks:
                await interaction.followup.send(chunk, ephemeral=False)
            return
        except Exception:
            traceback.print_exc()

        if interaction.channel is None:
            raise RuntimeError("Could not post broker receipt: interaction channel is unavailable.")

        for chunk in chunks:
            await interaction.channel.send(chunk)

    async def _complete_broker_sale(
        self,
        interaction: discord.Interaction,
        *,
        context: dict[str, Any],
        character: str,
        level: int,
    ) -> None:
        """Roll and finalize broker sale after the private confirmation prompt."""
        sheet_item = context["sheet_item"]
        broker_roll = roll_broker_price(context["base_price"])

        if broker_roll.roll == 1:
            build_sell_receipt(
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
                minimum_tier=sheet_item_minimum_tier_text(sheet_item),
                base_price=broker_roll.base_price,
                base_cost_detail=context["base_cost_detail"],
                dtp_cost=5,
                gold_cost=25,
                roll_label="Broker roll",
                roll_value=broker_roll.roll,
                seller_payout=0,
                status="Item lost, final, no takebacks",
                details=context["details_clean"],
                variant_instructions=sheet_item.variant_instructions,
            )
            output = build_broker_sale_public_output(
                seller=context["seller"],
                seller_character=context["seller_character"],
                listing_name=context["listing_name"],
                listing_id=None,
                broker_roll=broker_roll,
                minimum_tier=sheet_item_minimum_tier_text(sheet_item),
                base_cost_detail=context["base_cost_detail"],
                variant_block=context["variant_block"],
                disaster_message=random.choice(DISASTER_MESSAGES),
            )
            await self._remember_character(interaction, character, int(level))
            await self._send_public_broker_output(interaction, output)
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
            minimum_tier=sheet_item_minimum_tier_text(sheet_item),
            base_price=broker_roll.base_price,
            base_cost_detail=context["base_cost_detail"],
            dtp_cost=5,
            gold_cost=25,
            roll_label="Broker roll",
            roll_value=broker_roll.roll,
            seller_payout=broker_roll.seller_payout,
            status="Final, no takebacks",
            details=context["details_clean"],
            variant_instructions=sheet_item.variant_instructions,
        )
        listing, _receipt = await self._create_inventory_sale_listing(
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
        await self._remember_character(interaction, character, int(level))
        output = build_broker_sale_public_output(
            seller=context["seller"],
            seller_character=context["seller_character"],
            listing_name=context["listing_name"],
            listing_id=listing["listing_id"],
            broker_roll=broker_roll,
            minimum_tier=sheet_item_minimum_tier_text(sheet_item),
            base_cost_detail=context["base_cost_detail"],
            variant_block=context["variant_block"],
        )
        await self._send_public_broker_output(interaction, output)

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

        prompt = (
            "**Confirm Brokered Sale**\n\n"
            f"Item: **{context['listing_name']}**\n"
            f"Character: **{context['seller_character']}**\n"
            "Cost if you continue: **5 DTP and 25gp**\n"
            "Dwarfy will roll a flat d20 after confirmation. The sale is final, and a natural 1 loses the item.\n\n"
            "Choose one:"
        )
        await interaction.response.send_message(
            prompt,
            view=BrokerConfirmView(
                self,
                owner_id=interaction.user.id,
                context=context,
                character=character,
                level=int(level),
            ),
            ephemeral=True,
        )

    @sell.autocomplete("character")
    async def sell_character_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._character_name_choices(interaction, current)

    @broker.autocomplete("character")
    async def broker_character_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._character_name_choices(interaction, current)

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
        if not await self._require_shop_channel(interaction):
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
            listing = enrich_record_tier_from_cache(listing, self.bot.sheet_cache)
            if rarity_filter and listing["rarity"] != rarity_filter:
                continue
            searchable = " ".join(
                str(listing.get(field) or "")
                for field in ("listing_display_name", "item_name", "item_clean_name", "source", "category", "tags", "details")
            ).casefold()
            if search_filter and search_filter not in searchable:
                continue
            base_price = record_base_price(listing)
            if base_price is None:
                continue
            low, high = possible_final_price_range(
                base_price,
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
        if not await self._require_shop_channel(interaction):
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
        row = enrich_record_tier_from_cache(row, self.bot.sheet_cache)

        await interaction.response.send_message(
            embed=build_inspect_embed(row),
            view=InspectView(self, row, owner_id=interaction.user.id),
            ephemeral=True,
        )

    def _format_inspect(self, listing: dict[str, Any]) -> str:
        display_name = listing_display_name(listing)
        seller = mention_user(listing.get("seller_user_id"), listing.get("seller_display_name"))
        seller_character = character_label(
            listing.get("seller_character_name") or listing.get("seller_character"),
            listing.get("seller_character_level") or listing.get("seller_level"),
        )
        base_price = record_base_price(listing)
        price_range = "Base Price missing"
        price_formula = "Base Price missing"
        if base_price is not None:
            low, high = possible_final_price_range(
                base_price,
                int(listing["cost_basis"]),
            )
            price_range = price_range_text(low, high)
            price_formula = buy_price_formula(base_price)

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
                f"Minimum Tier: {record_minimum_tier_text(listing)}",
                f"Source: {source_with_page(listing.get('source'), listing.get('page'))}",
                f"Category: {listing['category'] or 'none'}",
                f"Tags: {listing['tags'] or 'none'}",
                f"Item detail: {listing.get('display_detail') or listing.get('item_type') or 'none'}",
                f"Short description: {listing.get('short_description') or 'none'}",
                f"Sale method: {listing.get('sale_method') or 'unknown/legacy'}",
                f"Original source: {listing_origin_text(listing)}",
                f"Base price: {gp(base_price) if base_price is not None else 'missing'}",
                f"Seller payout: {gp(int(listing.get('seller_payout') or 0))}",
                f"Dwarfy cost basis: {gp(int(listing['cost_basis']))}",
                f"DTP cost: {listing.get('dtp_cost') if listing.get('dtp_cost') is not None else 'unknown'}",
                f"Gold cost: {gp(int(listing.get('gold_cost') or 0)) if listing.get('gold_cost') is not None else 'unknown'}",
                f"Buying price formula: {price_formula}",
                f"Possible final price: {price_range}",
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
                    f"Voided by: {listing.get('voided_by_display_name') or 'unknown'}",
                    f"Void reason: {listing['void_reason'] or 'No reason recorded.'}",
                ]
            )

        stored_receipt = listing.get("adventure_log_receipt") or listing.get("receipt_text")
        if stored_receipt:
            lines.extend(["", "Stored Adventure Log Receipt:", stored_receipt])
        if listing.get("buy_receipt_text"):
            lines.extend(["", "Stored Buy Receipt:", listing["buy_receipt_text"]])

        return "\n".join(lines)

    async def _post_debt_log(
        self,
        *,
        buyer: str,
        buyer_character: str,
        item_name: str,
        listing_id: str,
        debt_owed: int,
        debt_fine: int,
        debt_total: int,
    ) -> None:
        """Post a debt consequence to the configured unresolved-log channel."""
        channel_id = self.bot.config.death_unresolved_log_channel_id
        if channel_id is None:
            return
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            await channel.send(
                (
                    f"{buyer} as {buyer_character} was permanently affected by defaulting on a "
                    "Dwarfy's Shop magic item contract.\n\n"
                    "Outcome: jailed debt consequence.\n"
                    "Status: unresolved.\n"
                    "DTP Required: no.\n"
                    f"Notes: Listing {listing_id} for {item_name}. "
                    f"Price shortfall {gp(debt_owed)} + contract-default fine {gp(debt_fine)} "
                    f"= {gp(debt_total)} total. Character is unplayable until resolved. "
                    "The item remains theirs but cannot be sold or traded until the debt is paid."
                )
            )
            await self.bot.db.add_ledger_entry(
                entry_type="debt_logged",
                listing_id=listing_id,
                item_name=item_name,
                cash_change=0,
                inventory_cost_change=0,
                profit_change=0,
                notes=f"Posted unresolved debt log for {buyer_character}: {gp(debt_total)}.",
            )
        except Exception as exc:
            print(f"[dwarfy] Could not post debt log for {listing_id}: {exc}")

    async def _buy_listing_from_id(
        self,
        interaction: discord.Interaction,
        *,
        listing_id: str,
        character: str,
        level: int,
        gold: int,
    ) -> None:
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
        row = enrich_record_tier_from_cache(row, self.bot.sheet_cache)
        if record_base_price(row) is None:
            await interaction.response.send_message(
                f"`{row['listing_id']}` is missing a Base Price and cannot be bought until the sheet/listing is corrected.",
                ephemeral=True,
            )
            return
        await self._finish_buy(interaction, row=row, character=character, level=level, gold=gold)

    async def _finish_buy(
        self,
        interaction: discord.Interaction,
        *,
        row: dict[str, Any],
        character: str,
        level: int,
        gold: int,
    ) -> None:
        """Finalize a Dwarfy-owned inventory purchase."""
        base_price = record_base_price(row)
        if base_price is None:
            await interaction.response.send_message(
                f"`{row['listing_id']}` is missing a Base Price and cannot be bought until the sheet/listing is corrected.",
                ephemeral=True,
            )
            return
        buy_roll = roll_buy_price(base_price, int(row["cost_basis"]))
        item_name = listing_display_name(row)
        gold_available = int(gold)
        cost_basis = int(row["cost_basis"])
        buyer = interaction.user.mention
        buyer_character = character_label(character, int(level))
        origin = listing_origin_text(row)
        tier_warning = tier_warning_text(
            row,
            item_name=item_name,
            character=buyer_character,
            level=int(level),
        )
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
        await self._remember_character(interaction, character, int(level))

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
            await self._post_debt_log(
                buyer=buyer,
                buyer_character=buyer_character,
                item_name=item_name,
                listing_id=row["listing_id"],
                debt_owed=debt_owed,
                debt_fine=debt_fine,
                debt_total=debt_total,
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
            f"{tier_warning + chr(10) + chr(10) if tier_warning else ''}"
            f"{buy_haggling_result_line(buy_roll, cost_basis)}\n\n"
            f"{payment_lines}\n\n"
            f"{receipt}{debt_block}\n\n"
            "Adventure log reminder:\n"
            "Record this downtime activity manually on the character's adventure log."
        )
        await send_text_response(interaction, output)

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
        if not await self._require_shop_channel(interaction):
            return
        listing_id = parse_listing_id(listing)
        if listing_id is None:
            await interaction.response.send_message(
                "Please buy by listing ID only, like `DWF-00017`.",
                ephemeral=True,
            )
            return
        await self._buy_listing_from_id(
            interaction,
            listing_id=listing_id,
            character=character,
            level=int(level),
            gold=int(gold),
        )

    @buy.autocomplete("character")
    async def buy_character_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._character_name_choices(interaction, current)

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

    @app_commands.command(name="classified_post", description="Post a player-to-player magic item sale through Dwarfy.")
    @app_commands.describe(
        character="Your character's name.",
        level="Your character's level.",
        item="Clean item name from Bot Items.",
        price="Gold the buyer pays before Dwarfy withholds seller commission.",
        variant="Optional identity for generic/template items, such as Longsword.",
        details="Optional trade notes, not full item rules text.",
    )
    async def classified_post(
        self,
        interaction: discord.Interaction,
        character: str,
        level: app_commands.Range[int, 1, 20],
        item: str,
        price: app_commands.Range[int, 1, 100_000_000],
        variant: str | None = None,
        details: str | None = None,
    ) -> None:
        context = await self._resolve_classified_context(
            interaction,
            item=item,
            variant=variant,
            details=details,
        )
        if context is None:
            return

        sheet_item = context["sheet_item"]
        asking_price = int(price)
        standing_summary = await self.bot.db.get_user_standing(str(interaction.user.id))
        standing_tier = standing_summary["current_tier"]
        commission_bps = int(standing_tier["commission_bps"])
        broker_fee = classified_fee_for_price(asking_price, commission_bps)
        buyer_total = asking_price
        row = await self.bot.db.create_classified(
            item_name=context["listing_name"],
            item_clean_name=sheet_item.name,
            listing_display_name=context["listing_name"],
            base_item_name=sheet_item.name if context["variant_clean"] else None,
            variant=context["variant_clean"],
            details=context["details_clean"],
            rarity=sheet_item.rarity,
            source=sheet_item.source,
            category=sheet_item.category,
            tags=sheet_item.tags_text,
            variant_type=sheet_item.variant_type or None,
            variant_instructions=sheet_item.variant_instructions or None,
            item_type=sheet_item.item_type or None,
            attunement=sheet_item.attunement or None,
            page=sheet_item.page or None,
            min_apl=sheet_item.min_apl,
            minimum_tier=item_minimum_tier(sheet_item),
            display_detail=sheet_item.display_detail or None,
            short_description=sheet_item.short_description or None,
            rules_text=sheet_item.rules_text or None,
            json_notes=sheet_item.json_notes or None,
            item_tags=sheet_item.item_tags or None,
            seller_user_id=str(interaction.user.id),
            seller_display_name=_display_name(interaction.user),
            seller_character_name=character.strip(),
            seller_character_level=int(level),
            asking_price=asking_price,
            broker_fee=broker_fee,
            buyer_total=buyer_total,
            commission_bps_locked=commission_bps,
            seller_tier_key_at_listing=standing_tier["tier_key"],
            seller_standing_gp_at_listing=int(standing_summary["lifetime_commission_gp"]),
        )
        await self._remember_character(interaction, character, int(level))

        lines = [
            classified_post_headline(
                interaction.user.mention,
                character,
                int(level),
                classified_display_name(row),
            ),
            "",
            "Dwarfy Classifieds Posting:",
            f"Posting: {row['classified_id']}",
            f"Seller: {interaction.user.mention} as {character_label(character, int(level))}",
            f"Item: {classified_display_name(row)}",
            f"Rarity: {sheet_item.rarity}",
            f"Minimum Tier: {sheet_item_minimum_tier_text(sheet_item)}",
            f"Source: {source_with_page(sheet_item.source, sheet_item.page)}",
            f"Buyer price: {gp(asking_price)}",
            f"Dwarfy commission: {gp(broker_fee)} ({commission_bps_text(commission_bps)}, withheld from seller payout)",
            f"Seller receives if sold: {gp(asking_price - broker_fee)}",
            f"Dwarfy Standing: {standing_tier['display_name']} ({standing_progress_text(standing_summary)})",
            f"Escrow: {classified_hold_text(row)}",
            "Status: Open, held by Dwarfy",
        ]
        if context["variant_note"]:
            lines.append(context["variant_note"])
        if context["details_clean"]:
            lines.append(f"Notes: {context['details_clean']}")
        if sheet_item.variant_instructions and not context["variant_clean"]:
            lines.append(f"Variant instructions: {sheet_item.variant_instructions}")
        lines.extend(
            [
                "",
                "Seller reminder: this item is in Dwarfy's custody during the hold. Do not use, sell, trade, or withdraw it unless it is returned.",
                "A buyer can run `/dwarfy classified_buy` with the posting ID to generate the trade-log text.",
            ]
        )
        await send_text_response(interaction, "\n".join(lines))

    @classified_post.autocomplete("character")
    async def classified_post_character_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._character_name_choices(interaction, current)

    @classified_post.autocomplete("item")
    async def classified_post_item_autocomplete(
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

    @classified_post.autocomplete("variant")
    async def classified_post_variant_autocomplete(
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

    @app_commands.command(name="classified_browse", description="Privately browse open Dwarfy Classifieds postings.")
    @app_commands.describe(
        rarity="Optional rarity filter.",
        search="Search item name, seller, source, category, tags, or notes.",
    )
    @app_commands.choices(rarity=BROWSE_RARITY_CHOICES)
    async def classified_browse(
        self,
        interaction: discord.Interaction,
        rarity: str | None = None,
        search: str | None = None,
    ) -> None:
        if not await self._require_classified_channel(interaction):
            return
        rarity_filter = normalize_rarity(rarity) if rarity else None
        if rarity_filter and rarity_filter not in BROWSE_RARITY_VALUES:
            await interaction.response.send_message(
                "Choose one of the supported rarity filters: Common, Uncommon, Rare, Very Rare, or Legendary.",
                ephemeral=True,
            )
            return
        search_filter = search.casefold().strip() if search else None
        rows = await self.bot.db.list_open_classifieds()
        filtered: list[dict[str, Any]] = []
        for row in rows:
            row = enrich_record_tier_from_cache(row, self.bot.sheet_cache)
            if rarity_filter and row["rarity"] != rarity_filter:
                continue
            searchable = " ".join(
                str(row.get(field) or "")
                for field in (
                    "classified_id",
                    "listing_display_name",
                    "item_name",
                    "item_clean_name",
                    "source",
                    "category",
                    "tags",
                    "seller_display_name",
                    "seller_character_name",
                    "details",
                )
            ).casefold()
            if search_filter and search_filter not in searchable:
                continue
            filtered.append(row)

        if not filtered:
            await interaction.response.send_message(
                "Dwarfy's Classifieds has no open postings matching those filters.",
                ephemeral=True,
            )
            return
        await send_text_response(interaction, build_classified_browse_output(filtered), ephemeral=True)

    @app_commands.command(name="classified_inspect", description="Privately inspect one Dwarfy Classifieds posting.")
    @app_commands.describe(classified="Classified ID, such as DWC-00017.")
    async def classified_inspect(self, interaction: discord.Interaction, classified: str) -> None:
        if not await self._require_classified_channel(interaction):
            return
        classified_id = parse_classified_id(classified)
        if classified_id is None:
            await interaction.response.send_message(
                "Please inspect by classified ID, like `DWC-00017`.",
                ephemeral=True,
            )
            return
        row = await self.bot.db.get_classified(classified_id)
        if row is None:
            await interaction.response.send_message(
                f"I could not find classified posting `{classified_id}`.",
                ephemeral=True,
            )
            return
        row = enrich_record_tier_from_cache(row, self.bot.sheet_cache)
        await interaction.response.send_message(embed=build_classified_embed(row), ephemeral=True)

    @app_commands.command(name="classified_buy", description="Buy a player-posted item from Dwarfy's Classifieds.")
    @app_commands.describe(
        classified="Classified ID, such as DWC-00017.",
        character="Your character's name.",
        level="Your character's level.",
    )
    async def classified_buy(
        self,
        interaction: discord.Interaction,
        classified: str,
        character: str,
        level: app_commands.Range[int, 1, 20],
    ) -> None:
        if not await self._require_classified_channel(interaction):
            return
        classified_id = parse_classified_id(classified)
        if classified_id is None:
            await interaction.response.send_message(
                "Please buy by classified ID, like `DWC-00017`.",
                ephemeral=True,
            )
            return
        row = await self.bot.db.get_classified(classified_id)
        if row is None:
            await interaction.response.send_message(
                f"I could not find classified posting `{classified_id}`.",
                ephemeral=True,
            )
            return
        row = enrich_record_tier_from_cache(row, self.bot.sheet_cache)
        if row["status"] != "open":
            status_text = classified_status_text(row).casefold()
            await interaction.response.send_message(
                f"`{row['classified_id']}` is already {status_text} and cannot be bought.",
                ephemeral=True,
            )
            return
        if is_classified_expired(row):
            await interaction.response.send_message(
                f"`{row['classified_id']}` has reached the end of its {CLASSIFIED_HOLD_DAYS}-day hold and is being returned to the seller.",
                ephemeral=True,
            )
            return
        if str(row.get("seller_user_id")) == str(interaction.user.id):
            await interaction.response.send_message(
                "Dwarfy will not let you buy your own classified posting.",
                ephemeral=True,
            )
            return
        buyer = interaction.user.mention
        buyer_character = character_label(character, int(level))
        trade_log = build_classified_trade_log(row, buyer=buyer, buyer_character=buyer_character)
        tier_warning = tier_warning_text(
            row,
            item_name=classified_display_name(row),
            character=buyer_character,
            level=int(level),
        )
        sold = await self.bot.db.mark_classified_sold(
            classified_id=row["classified_id"],
            buyer_user_id=str(interaction.user.id),
            buyer_display_name=_display_name(interaction.user),
            buyer_character_name=character.strip(),
            buyer_character_level=int(level),
            trade_log_text=trade_log,
        )
        if not sold:
            await interaction.response.send_message(
                "That classified posting is no longer open. Please browse again.",
                ephemeral=True,
            )
            return
        await self._remember_character(interaction, character, int(level))
        standing_result = sold.get("_standing_result") if isinstance(sold, dict) else None
        standing_lines = standing_receipt_lines(standing_result)

        output = (
            f"{buyer} buys {classified_display_name(row)} through Dwarfy's Classifieds.\n\n"
            f"{tier_warning + chr(10) + chr(10) if tier_warning else ''}"
            "Dwarfy Classifieds Receipt:\n"
            f"Posting: {row['classified_id']}\n"
            f"Item: {classified_display_name(row)}\n"
            f"Rarity: {row['rarity']}\n"
            f"Minimum Tier: {record_minimum_tier_text(row)}\n"
            f"Source: {source_with_page(row.get('source'), row.get('page'))}\n"
            f"Buyer price: {gp(classified_buyer_price(row))}\n"
            f"Dwarfy commission: {gp(classified_commission(row))} "
            f"({commission_bps_text(row.get('commission_bps_locked'))}, withheld from seller payout)\n"
            f"Seller receives: {gp(classified_seller_net(row))}\n"
            f"{chr(10).join(standing_lines) + chr(10) if standing_lines else ''}"
            "Status: Final once both players update their logs.\n\n"
            "Copyable Trade Log:\n"
            f"{trade_log}"
        )
        await send_text_response(interaction, output)

    async def _classified_id_choices(
        self,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        query = current.casefold().strip()
        rows = await self.bot.db.list_open_classifieds()
        choices: list[app_commands.Choice[str]] = []
        for row in rows:
            display_name = classified_display_name(row)
            label = f"{row['classified_id']} - {display_name} ({gp(classified_buyer_price(row))})"
            searchable = f"{row['classified_id']} {display_name} {row['rarity']} {row.get('seller_display_name')}".casefold()
            if query and query not in searchable:
                continue
            choices.append(app_commands.Choice(name=label[:100], value=row["classified_id"]))
            if len(choices) >= 25:
                break
        return choices

    @classified_inspect.autocomplete("classified")
    async def classified_inspect_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._classified_id_choices(current)

    @classified_buy.autocomplete("classified")
    async def classified_buy_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._classified_id_choices(current)

    @classified_buy.autocomplete("character")
    async def classified_buy_character_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._character_name_choices(interaction, current)

    @app_commands.command(name="classified_void", description="Admin/mod: void a Dwarfy Classifieds posting.")
    @app_commands.describe(
        classified="Classified ID, such as DWC-00017.",
        reason="Why this posting is being voided.",
    )
    async def classified_void(self, interaction: discord.Interaction, classified: str, reason: str) -> None:
        if not await self._require_admin(interaction):
            return
        classified_id = parse_classified_id(classified)
        if classified_id is None:
            await interaction.response.send_message("Use a classified ID like `DWC-00017`.", ephemeral=True)
            return
        row = await self.bot.db.void_classified(
            classified_id,
            reason,
            voided_by_user_id=str(interaction.user.id),
            voided_by_display_name=_display_name(interaction.user),
        )
        if row is None:
            await interaction.response.send_message(
                f"I could not find classified posting `{classified_id}` to void.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Dwarfy classified voided: {row['classified_id']} - {classified_display_name(row)}\n"
            f"Voided by: {_display_name(interaction.user)}\n"
            f"Reason: {reason}",
            ephemeral=True,
        )

    @classified_void.autocomplete("classified")
    async def classified_void_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._classified_id_choices(current)

    @app_commands.command(name="stats", description="Show Dwarfy's inventory and ledger stats.")
    async def stats(self, interaction: discord.Interaction) -> None:
        if not await self._require_admin(interaction):
            return

        stats = await self.bot.db.shop_stats()
        best = stats["most_profitable_flip"]
        best_text = (
            f"{best['listing_id']} - {best['item_name']} - {gp(int(best['realized_profit']))}"
            if best
            else "none"
        )
        expensive = stats.get("most_expensive_available_item")
        expensive_text = (
            f"{expensive['listing_id']} - {expensive['item_name']} - {gp(int(expensive['cost_basis']))}"
            if expensive
            else "none"
        )
        oldest = stats.get("oldest_unsold_item")
        oldest_text = (
            f"{oldest['listing_id']} - {oldest['item_name']} - stocked {age_text(oldest['created_at'])}"
            if oldest
            else "none"
        )
        top_seller = stats.get("top_seller")
        top_seller_text = (
            f"{top_seller['seller_display_name']} - {top_seller['count']} item(s), {gp(int(top_seller['total']))} paid"
            if top_seller
            else "none"
        )
        top_buyer = stats.get("top_buyer")
        top_buyer_text = (
            f"{top_buyer['buyer_display_name']} - {top_buyer['count']} item(s), {gp(int(top_buyer['total']))} spent"
            if top_buyer
            else "none"
        )
        embed = discord.Embed(
            title="Dwarfy's Shop Dashboard",
            color=discord.Color(0xC9A227),
        )
        embed.add_field(
            name="Inventory",
            value=(
                f"Available: {stats['available_count']}\n"
                f"Owner stock: {stats['owner_stock_available_count']}\n"
                f"Player stock: {stats['player_stock_available_count']}\n"
                f"Available cost basis: {gp(stats['available_inventory_cost_basis'])}"
            ),
            inline=True,
        )
        embed.add_field(
            name="Transactions",
            value=(
                f"Sold: {stats['sold_count']}\n"
                f"Voided: {stats['voided_count']}\n"
                f"Paid to sellers: {gp(stats['total_paid_to_sellers'])}\n"
                f"Received from buyers: {gp(stats['total_received_from_buyers'])}"
            ),
            inline=True,
        )
        embed.add_field(
            name="Profit",
            value=(
                f"Realized profit: {gp(stats['realized_profit'])}\n"
                f"Business cash flow: {gp(stats['business_cash_flow'])}\n"
                f"Best flip: {best_text}"
            ),
            inline=False,
        )
        embed.add_field(name="Most Expensive Available", value=expensive_text, inline=False)
        embed.add_field(name="Oldest Unsold", value=oldest_text, inline=False)
        embed.add_field(name="Top Seller", value=top_seller_text, inline=True)
        embed.add_field(name="Top Buyer", value=top_buyer_text, inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="history", description="Admin/mod: show recent Dwarfy ledger history.")
    @app_commands.describe(
        limit="Number of rows to show, max 50.",
        listing="Optional listing/classified ID.",
        entry_type="Optional ledger entry type.",
        status="Optional DWF listing status: available, sold, or voided.",
        search="Optional search text.",
    )
    async def history(
        self,
        interaction: discord.Interaction,
        limit: int = 20,
        listing: str | None = None,
        entry_type: str | None = None,
        status: str | None = None,
        search: str | None = None,
    ) -> None:
        if not await self._require_admin(interaction):
            return
        normalized_listing = None
        if listing:
            normalized_listing = parse_listing_id(listing) or parse_classified_id(listing) or listing.strip()
        rows = await self.bot.db.history_entries(
            limit=limit,
            listing_id=normalized_listing,
            entry_type=(entry_type or "").strip() or None,
            status=(status or "").strip() or None,
            search=(search or "").strip() or None,
        )
        if not rows:
            await interaction.response.send_message("No matching ledger history found.", ephemeral=True)
            return
        lines = ["Dwarfy ledger history", ""]
        for row in rows:
            listing_text = row.get("listing_id") or "none"
            seller_display = row.get("listing_seller_display_name") or row.get("classified_seller_display_name")
            seller_character_name = row.get("listing_seller_character_name") or row.get("classified_seller_character_name")
            seller_character_level = row.get("listing_seller_character_level") or row.get("classified_seller_character_level")
            buyer_display = row.get("listing_buyer_display_name") or row.get("classified_buyer_display_name")
            buyer_character_name = row.get("listing_buyer_character_name") or row.get("classified_buyer_character_name")
            buyer_character_level = row.get("listing_buyer_character_level") or row.get("classified_buyer_character_level")
            voided_by = row.get("listing_voided_by_display_name") or row.get("classified_voided_by_display_name")
            audit_bits: list[str] = []
            if seller_display:
                audit_bits.append(
                    f"Seller: {seller_display} as {character_label(seller_character_name, seller_character_level)}"
                )
            if buyer_display:
                audit_bits.append(
                    f"Buyer: {buyer_display} as {character_label(buyer_character_name, buyer_character_level)}"
                )
            if voided_by:
                audit_bits.append(f"Voided by: {voided_by}")
            lines.extend(
                [
                    f"#{row['id']} | {row['created_at']} | {row['entry_type']} | {listing_text}",
                    (
                        f"Item: {row.get('item_name') or 'none'} | Cash: {gp(int(row['cash_change']))} | "
                        f"Inventory: {gp(int(row['inventory_cost_change']))} | Profit: {gp(int(row['profit_change']))}"
                    ),
                    f"Parties: {' | '.join(audit_bits) if audit_bits else 'none recorded'}",
                    f"Notes: {row['notes']}",
                    "",
                ]
            )
        await send_text_response(interaction, "\n".join(lines), ephemeral=True)

    @app_commands.command(name="edit_post", description="Admin/mod: correct text in one Dwarfy bot message.")
    @app_commands.describe(
        message_link="Discord message link for the Dwarfy Bot post to edit.",
        find="Exact text to replace. Use a small unique phrase.",
        replace="Replacement text.",
        reason="Private audit reason for the correction.",
        replace_all="Replace every occurrence instead of only the first.",
    )
    async def edit_post(
        self,
        interaction: discord.Interaction,
        message_link: str,
        find: str,
        replace: str,
        reason: str | None = None,
        replace_all: bool = False,
    ) -> None:
        if not await self._require_admin(interaction):
            return

        parsed = parse_message_reference(message_link)
        if parsed is None:
            await interaction.response.send_message(
                "Paste a Discord message link, or a channel ID and message ID.",
                ephemeral=True,
            )
            return
        channel_id, message_id = parsed
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                await interaction.response.send_message(
                    "I could not access that channel. Make sure Dwarfy can see it.",
                    ephemeral=True,
                )
                return
        fetch_message = getattr(channel, "fetch_message", None)
        if fetch_message is None:
            await interaction.response.send_message(
                "That channel type does not support message editing.",
                ephemeral=True,
            )
            return

        try:
            message = await fetch_message(message_id)
        except discord.NotFound:
            await interaction.response.send_message("I could not find that message.", ephemeral=True)
            return
        except discord.Forbidden:
            await interaction.response.send_message(
                "I cannot read messages in that channel.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"Discord would not let me fetch that message: {exc}",
                ephemeral=True,
            )
            return

        if self.bot.user is None or message.author.id != self.bot.user.id:
            await interaction.response.send_message(
                "I can only edit messages posted by Dwarfy Bot.",
                ephemeral=True,
            )
            return
        if not message.content:
            await interaction.response.send_message(
                "That Dwarfy message has no plain-text content to edit.",
                ephemeral=True,
            )
            return
        try:
            new_content = edited_message_content(
                message.content,
                find,
                replace,
                replace_all=replace_all,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if new_content is None:
            await interaction.response.send_message(
                "I could not find that exact text in the message. Use a smaller exact phrase.",
                ephemeral=True,
            )
            return

        try:
            await message.edit(content=new_content)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I do not have permission to edit that message.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"Discord would not let me edit that message: {exc}",
                ephemeral=True,
            )
            return

        audit_reason = (reason or "No reason provided.").strip()
        await self.bot.db.add_ledger_entry(
            entry_type="post_edit",
            listing_id=None,
            item_name=None,
            cash_change=0,
            inventory_cost_change=0,
            profit_change=0,
            notes=(
                f"Edited Dwarfy message {message_id} in channel {channel_id}. "
                f"Reason: {audit_reason}"
            ),
        )
        await interaction.response.send_message(
            (
                "Dwarfy post edited.\n\n"
                f"Message ID: {message_id}\n"
                f"Channel ID: {channel_id}\n"
                f"Reason: {audit_reason}"
            ),
            ephemeral=True,
        )

    @app_commands.command(name="export", description="Admin/mod: export Dwarfy SQLite tables as CSV files.")
    async def export(self, interaction: discord.Interaction) -> None:
        if not await self._require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        files: list[discord.File] = []
        for table_name in ("listings", "ledger", "classifieds"):
            rows = await self.bot.db.export_table(table_name)
            output = io.StringIO()
            if rows:
                writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
            else:
                output.write("id\n")
            files.append(
                discord.File(
                    io.BytesIO(output.getvalue().encode("utf-8")),
                    filename=f"dwarfy-{table_name}.csv",
                )
            )
        await interaction.followup.send("Dwarfy export ready.", files=files, ephemeral=True)

    @app_commands.command(name="restock_status", description="Admin/mod: check owner-stock freshness.")
    async def restock_status(self, interaction: discord.Interaction) -> None:
        if not await self._require_admin(interaction):
            return
        status = await self.bot.db.owner_stock_status()
        count = int(status.get("count") or 0)
        if count == 0:
            await interaction.response.send_message(
                "Dwarfy has no current owner-stocked inventory. Run `/dwarfy stock_random clear_first:True` when ready.",
                ephemeral=True,
            )
            return
        output = (
            "Dwarfy restock status\n\n"
            f"* Owner-stocked listings: {count}\n"
            f"* Oldest owner stock: {age_text(status.get('oldest_created_at'))}\n"
            f"* Newest owner stock: {age_text(status.get('newest_created_at'))}\n"
            f"* Latest batch: {status.get('latest_batch_id') or 'none'}\n\n"
            "Refresh suggestion: run `/dwarfy stock_random clear_first:True` when the shelf feels stale."
        )
        await interaction.response.send_message(output, ephemeral=True)

    @app_commands.command(name="debt_resolve", description="Admin/mod: mark a Dwarfy debt consequence as resolved.")
    @app_commands.describe(
        listing="Sold listing ID with debt, such as DWF-00017.",
        reason="How the debt was resolved.",
    )
    async def debt_resolve(self, interaction: discord.Interaction, listing: str, reason: str) -> None:
        if not await self._require_admin(interaction):
            return
        listing_id = parse_listing_id(listing)
        if listing_id is None:
            await interaction.response.send_message("Use a listing ID like `DWF-00017`.", ephemeral=True)
            return
        row = await self.bot.db.resolve_listing_debt(listing_id, reason)
        if row is None:
            await interaction.response.send_message(
                f"I could not find unresolved debt for `{listing_id}`.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Dwarfy debt resolved for {row['listing_id']} - {listing_display_name(row)}.\nReason: {reason}",
            ephemeral=True,
        )

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

        row = await self.bot.db.void_listing(
            listing_id,
            reason,
            voided_by_user_id=str(interaction.user.id),
            voided_by_display_name=_display_name(interaction.user),
        )
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
            f"Voided by: {_display_name(interaction.user)}\n"
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
            f"* Mundane Item Reference rows loaded: {len(self.bot.sheet_cache.mundane_items)}\n"
            f"* Pricing Template Rules rows loaded: {len(self.bot.sheet_cache.pricing_rules)}\n"
            f"* Validation warnings:\n{warning_text}"
        )
        await interaction.followup.send(output, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Dwarfy(bot))
