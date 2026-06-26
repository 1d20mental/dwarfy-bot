"""Dwarfy Bot entry point.

Run this file with Python 3.11+ after creating a real `.env` file. Do not put
real Discord tokens or Google credentials in the repository.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import discord
from discord.ext import commands
from dotenv import load_dotenv

from services.database import DwarfyDatabase
from services.sheets import SheetCache


def _optional_int(value: str | None) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    return int(value)


@dataclass(frozen=True)
class BotConfig:
    discord_token: str
    guild_id: int | None
    admin_role_names: set[str]
    dwarfy_sell_channel_id: int | None
    dwarfy_shop_channel_id: int | None
    dwarfy_classified_channel_id: int | None
    session_loot_channel_id: int | None
    death_unresolved_log_channel_id: int | None
    google_sheet_id: str
    google_service_account_file: str
    bot_items_tab: str
    monster_components_tab: str
    database_path: str

    @classmethod
    def from_env(cls) -> "BotConfig":
        admin_roles = {
            role.strip().casefold()
            for role in os.getenv("ADMIN_ROLE_NAMES", "Admin,Moderator,DM,Loot Manager").split(",")
            if role.strip()
        }
        return cls(
            discord_token=os.getenv("DISCORD_TOKEN", "").strip(),
            guild_id=_optional_int(os.getenv("GUILD_ID")),
            admin_role_names=admin_roles,
            dwarfy_sell_channel_id=_optional_int(os.getenv("DWARFY_SELL_CHANNEL_ID")),
            dwarfy_shop_channel_id=_optional_int(os.getenv("DWARFY_SHOP_CHANNEL_ID")),
            dwarfy_classified_channel_id=_optional_int(os.getenv("DWARFY_CLASSIFIED_CHANNEL_ID")),
            session_loot_channel_id=_optional_int(os.getenv("SESSION_LOOT_CHANNEL_ID")),
            death_unresolved_log_channel_id=_optional_int(os.getenv("DEATH_UNRESOLVED_LOG_CHANNEL_ID")),
            google_sheet_id=os.getenv("GOOGLE_SHEET_ID", "").strip(),
            google_service_account_file=os.getenv(
                "GOOGLE_SERVICE_ACCOUNT_FILE",
                "service-account.json",
            ).strip(),
            bot_items_tab=os.getenv("BOT_ITEMS_TAB", "Bot Items").strip(),
            monster_components_tab=os.getenv(
                "MONSTER_COMPONENTS_TAB",
                "Monster Components",
            ).strip(),
            database_path=os.getenv("DATABASE_PATH", "data/dwarfy.sqlite").strip(),
        )


class DwarfyBot(commands.Bot):
    def __init__(self, config: BotConfig) -> None:
        intents = discord.Intents.default()
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
        )
        self.config = config
        self.db = DwarfyDatabase(config.database_path)
        self.sheet_cache = SheetCache(
            sheet_id=config.google_sheet_id,
            service_account_file=config.google_service_account_file,
            bot_items_tab=config.bot_items_tab,
            monster_components_tab=config.monster_components_tab,
        )
        self._ready_logged = False

    async def setup_hook(self) -> None:
        await self.db.connect()
        print(f"[database] Ready at {self.config.database_path}")

        try:
            await asyncio.to_thread(self.sheet_cache.reload)
        except Exception as exc:
            print(f"[sheets] Could not load Google Sheet data: {exc}")
            print("[sheets] The bot will still start, but sheet-dependent commands will be unavailable.")
        else:
            print(f"[sheets] Loaded {len(self.sheet_cache.items)} Bot Items rows.")
            print(f"[sheets] Loaded {len(self.sheet_cache.components)} Monster Components rows.")
            print(f"[sheets] Loaded {len(self.sheet_cache.mundane_items)} Mundane Item Reference rows.")
            print(f"[sheets] Loaded {len(self.sheet_cache.pricing_rules)} Pricing Template Rules rows.")
            if self.sheet_cache.warnings:
                print("[sheets] Validation warnings:")
                for warning in self.sheet_cache.warnings:
                    print(f"  - {warning}")

        await self.load_extension("cogs.dwarfy")
        await self.load_extension("cogs.sessionloot")

        if self.config.guild_id is not None:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"[discord] Synced {len(synced)} guild slash commands to {self.config.guild_id}.")
        else:
            synced = await self.tree.sync()
            print(f"[discord] Synced {len(synced)} global slash commands.")

    async def close(self) -> None:
        await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        if self._ready_logged:
            return
        self._ready_logged = True
        user = self.user
        print(f"[discord] Logged in as {user} (ID: {user.id if user else 'unknown'}).")


async def main() -> None:
    load_dotenv()
    config = BotConfig.from_env()
    if not config.discord_token:
        raise SystemExit("DISCORD_TOKEN is not set. Create a real .env file first.")

    async with DwarfyBot(config) as bot:
        await bot.start(config.discord_token)


if __name__ == "__main__":
    asyncio.run(main())
