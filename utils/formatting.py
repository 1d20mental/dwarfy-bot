"""Small formatting helpers shared by the Discord cogs."""

from __future__ import annotations

import textwrap

DISCORD_MESSAGE_LIMIT = 2_000
SAFE_MESSAGE_LIMIT = 1_900


def gp(amount: int | None) -> str:
    """Format a gold-piece amount with commas."""
    if amount is None:
        return "0gp"
    return f"{amount:,}gp"


def mention_user(user_id: str | None, fallback: str | None = None) -> str:
    """Return a Discord mention when an ID exists, otherwise a readable name."""
    if user_id:
        return f"<@{user_id}>"
    return fallback or "Unknown player"


def character_label(name: str | None, level: int | None) -> str:
    """Format a character name and level for public audit text."""
    clean_name = (name or "Unknown Character").strip()
    if level is None:
        return clean_name
    return f"{clean_name} ({level})"


def price_range_text(low: int, high: int) -> str:
    """Show a fixed price or a range depending on the floor math."""
    if low == high:
        return gp(low)
    return f"{gp(low)}-{gp(high)}"


def split_message(text: str, limit: int = SAFE_MESSAGE_LIMIT) -> list[str]:
    """Split long plain-text output into Discord-sized chunks.

    Discord rejects messages over 2,000 characters. This helper splits on blank
    lines first, then falls back to line wrapping for unusually long sections.
    """
    text = text.strip()
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""

    for part in text.split("\n\n"):
        part = part.strip()
        if not part:
            continue

        if len(part) > limit:
            wrapped_parts = textwrap.wrap(
                part,
                width=limit,
                break_long_words=False,
                replace_whitespace=False,
            )
        else:
            wrapped_parts = [part]

        for wrapped in wrapped_parts:
            candidate = f"{current}\n\n{wrapped}".strip() if current else wrapped
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = wrapped

    if current:
        chunks.append(current)

    return chunks


async def send_text_response(interaction, text: str, ephemeral: bool = False) -> None:
    """Send one or more plain-text messages for an interaction."""
    chunks = split_message(text)

    if not interaction.response.is_done():
        await interaction.response.send_message(chunks[0], ephemeral=ephemeral)
        chunks = chunks[1:]

    for chunk in chunks:
        await interaction.followup.send(chunk, ephemeral=ephemeral)
