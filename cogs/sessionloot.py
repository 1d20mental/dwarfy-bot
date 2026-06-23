"""Top-level /sessionloot command."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from services.loot import (
    InvalidCreatureTypeError,
    LootError,
    build_session_loot_output,
)
from utils.formatting import send_text_response


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
        players="Number of players, 1-20.",
        apl="Average party level, 1-20.",
        tag="Optional tag filter, such as undead.",
        creature_type="Optional monster component creature type.",
    )
    async def sessionloot(
        self,
        interaction: discord.Interaction,
        players: app_commands.Range[int, 1, 20],
        apl: app_commands.Range[int, 1, 20],
        tag: str | None = None,
        creature_type: str | None = None,
    ) -> None:
        if not await self._require_channel(interaction):
            return
        if not self.bot.sheet_cache.loaded:
            await interaction.response.send_message(
                "Google Sheet data is not loaded yet. Ask an admin/mod to run `/dwarfy reload`, or check the bot terminal logs.",
                ephemeral=True,
            )
            return

        try:
            output = build_session_loot_output(
                cache=self.bot.sheet_cache,
                players=int(players),
                apl=int(apl),
                tag=tag,
                creature_type=creature_type,
            )
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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SessionLoot(bot))
