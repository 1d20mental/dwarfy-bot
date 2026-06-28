"""Top-level /sessionloot command."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from services.loot import (
    InvalidCreatureTypeError,
    LootError,
    build_dm_incentive_loot_output,
    build_session_loot_output,
)
from utils.formatting import send_text_response


SESSIONLOOT_MODE_CHOICES = [
    app_commands.Choice(name="Player session loot", value="player"),
    app_commands.Choice(name="DM incentive loot pool", value="dm"),
]


def sessionloot_tag_choices(cache, query: str, *, limit: int = 25) -> list[str]:
    """Return unique session loot tags from the cached Bot Items sheet."""
    query_norm = query.casefold().strip()
    tags: set[str] = set()
    for item in cache.items:
        if not item.allowed or not item.session_eligible:
            continue
        tags.update(item.tags)

    starts = sorted(tag for tag in tags if query_norm and tag.startswith(query_norm))
    contains = sorted(
        tag
        for tag in tags
        if (not query_norm or query_norm in tag) and tag not in starts
    )
    return (starts + contains)[:limit]


def sessionloot_creature_type_choices(cache, query: str, *, limit: int = 25) -> list[str]:
    """Return creature type choices from the cached Monster Components sheet."""
    query_norm = query.casefold().strip()
    creature_types = cache.available_creature_types()
    starts = [
        creature_type
        for creature_type in creature_types
        if query_norm and creature_type.casefold().startswith(query_norm)
    ]
    contains = [
        creature_type
        for creature_type in creature_types
        if (not query_norm or query_norm in creature_type.casefold()) and creature_type not in starts
    ]
    return (starts + contains)[:limit]


class SessionLoot(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _require_channel(self, interaction: discord.Interaction) -> bool:
        channel_id = self.bot.config.session_loot_channel_id
        if channel_id is None:
            return True
        if interaction.channel_id == channel_id:
            return True
        await interaction.response.send_message(
            "Please use `/sessionloot` in the configured session loot channel.",
            ephemeral=True,
        )
        return False

    @app_commands.command(name="sessionloot", description="Roll complete session loot from the Google Sheet.")
    @app_commands.describe(
        mode="Player session loot or DM incentive loot pool. Defaults to player session.",
        players="Player mode: number of players, 1-20.",
        apl="Player mode: average party level. DM mode: DM character level.",
        tag="Optional tag filter, such as undead.",
        creature_type="Optional monster component creature type.",
        new_hire_players="DM mode: qualifying New Hires players. Each adds one extra permanent option.",
        jump_start="DM mode: True if this game qualifies for the Jump Start extra permanent option.",
        tour_de_tiers="DM mode: True if this completes Tour de Tiers for the month.",
        extra_options="DM mode: staff-approved extra permanent options, if any.",
    )
    @app_commands.choices(mode=SESSIONLOOT_MODE_CHOICES)
    async def sessionloot(
        self,
        interaction: discord.Interaction,
        mode: str | None = None,
        players: int | None = None,
        apl: int | None = None,
        tag: str | None = None,
        creature_type: str | None = None,
        new_hire_players: int = 0,
        jump_start: bool = False,
        tour_de_tiers: bool = False,
        extra_options: int = 0,
    ) -> None:
        if not await self._require_channel(interaction):
            return
        if not self.bot.sheet_cache.loaded:
            await interaction.response.send_message(
                "Google Sheet data is not loaded yet. Ask an admin/mod to run `/dwarfy reload`, or check the bot terminal logs.",
                ephemeral=True,
            )
            return

        selected_mode = (mode or "player").casefold().strip()
        try:
            if selected_mode == "dm":
                if apl is None:
                    raise LootError("DM incentive mode needs `apl` filled with the DM character level.")
                output = build_dm_incentive_loot_output(
                    cache=self.bot.sheet_cache,
                    apl=int(apl),
                    tag=tag,
                    new_hire_players=int(new_hire_players or 0),
                    jump_start=bool(jump_start),
                    tour_de_tiers=bool(tour_de_tiers),
                    extra_options=int(extra_options or 0),
                )
            elif selected_mode == "player":
                if players is None or apl is None:
                    raise LootError("Player session mode needs both `players` and `apl`.")
                output = build_session_loot_output(
                    cache=self.bot.sheet_cache,
                    players=int(players),
                    apl=int(apl),
                    tag=tag,
                    creature_type=creature_type,
                )
            else:
                raise LootError("Mode must be `Player session loot` or `DM incentive loot pool`.")
        except InvalidCreatureTypeError as exc:
            available = ", ".join(exc.available) or "none loaded"
            await interaction.response.send_message(
                f"`{exc.creature_type}` is not a valid creature type. Available creature types: {available}",
                ephemeral=True,
            )
            return
        except LootError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await send_text_response(interaction, output)

    @sessionloot.autocomplete("tag")
    async def tag_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not self.bot.sheet_cache.loaded:
            return []
        return [
            app_commands.Choice(name=tag[:100], value=tag[:100])
            for tag in sessionloot_tag_choices(self.bot.sheet_cache, current)
        ]

    @sessionloot.autocomplete("creature_type")
    async def creature_type_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not self.bot.sheet_cache.loaded:
            return []
        return [
            app_commands.Choice(name=creature_type[:100], value=creature_type[:100])
            for creature_type in sessionloot_creature_type_choices(self.bot.sheet_cache, current)
        ]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SessionLoot(bot))
