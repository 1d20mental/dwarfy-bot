"""Dice and pricing rules for Dwarfy's Shop.

This module has no Discord or database imports. Keeping the math here makes it
easy to test and easy for a new maintainer to adjust house rules later.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


BASE_PRICES = {
    "Common": 100,
    "Uncommon": 400,
    "Rare": 4_000,
    "Very Rare": 40_000,
    "Legendary": 200_000,
}


BUY_PRICE_RANGES = {
    "Common": (20, 70, "(1d6 + 1) x 10gp"),
    "Uncommon": (100, 600, "1d6 x 100gp"),
    "Rare": (2_000, 20_000, "2d10 x 1,000gp"),
    "Very Rare": (20_000, 50_000, "(1d4 + 1) x 10,000gp"),
    "Legendary": (50_000, 300_000, "2d6 x 25,000gp"),
}


@dataclass(frozen=True)
class SellRoll:
    roll: int
    result_text: str
    payout_percent: int
    base_price: int
    seller_payout: int


@dataclass(frozen=True)
class BuyRoll:
    roll_detail: str
    rolled_price: int
    final_price: int
    realized_profit: int


def is_supported_rarity(rarity: str) -> bool:
    """Return True when Dwarfy can price this rarity in version 1."""
    return rarity in BASE_PRICES


def base_price_for_rarity(rarity: str) -> int:
    """Return the fixed base price for a supported rarity."""
    if rarity not in BASE_PRICES:
        raise ValueError(f"Unsupported rarity: {rarity}")
    return BASE_PRICES[rarity]


def direct_sell_price(rarity: str) -> SellRoll:
    """Return the guaranteed direct-sale payout.

    Direct sale is intentionally boring: no DTP, no gold fee, no d20 roll, and
    a fixed 40% payout.
    """
    base_price = base_price_for_rarity(rarity)
    payout = (base_price * 40) // 100
    return SellRoll(
        roll=0,
        result_text="Guaranteed direct sale, 40% of base price",
        payout_percent=40,
        base_price=base_price,
        seller_payout=payout,
    )


def roll_broker_price(rarity: str) -> SellRoll:
    """Roll the downtime brokered player-to-shop sale result."""
    base_price = base_price_for_rarity(rarity)
    roll = random.randint(1, 20)

    if roll == 20:
        percent = 100
        result = "Excellent buyer, 100% of base price"
    elif roll >= 16:
        percent = 60
        result = "Strong buyer, 60% of base price"
    elif roll >= 10:
        percent = 50
        result = "Fair buyer, 50% of base price"
    elif roll >= 6:
        percent = 30
        result = "Weak buyer, 30% of base price"
    elif roll >= 2:
        percent = 20
        result = "Poor buyer, 20% of base price"
    else:
        percent = 0
        result = "Disaster. The item is lost during brokerage."

    payout = (base_price * percent) // 100
    return SellRoll(
        roll=roll,
        result_text=result,
        payout_percent=percent,
        base_price=base_price,
        seller_payout=payout,
    )


def roll_sell_price(rarity: str) -> SellRoll:
    """Backward-compatible name for the brokered sale roll."""
    return roll_broker_price(rarity)


def buy_price_formula(rarity: str) -> str:
    """Return the asking price formula shown in inspect/buy output."""
    if rarity not in BUY_PRICE_RANGES:
        raise ValueError(f"Unsupported rarity: {rarity}")
    return BUY_PRICE_RANGES[rarity][2]


def possible_final_price_range(rarity: str, cost_basis: int) -> tuple[int, int]:
    """Return the lowest and highest possible final buy price.

    The floor rule means the shop never sells below its cost basis.
    """
    if rarity not in BUY_PRICE_RANGES:
        raise ValueError(f"Unsupported rarity: {rarity}")
    roll_min, roll_max, _formula = BUY_PRICE_RANGES[rarity]
    return max(roll_min, cost_basis), max(roll_max, cost_basis)


def roll_buy_price(rarity: str, cost_basis: int) -> BuyRoll:
    """Roll the shop-to-player asking price and apply Dwarfy's floor."""
    if rarity == "Common":
        d6 = random.randint(1, 6)
        rolled = (d6 + 1) * 10
        detail = f"(1d6 + 1) x 10gp = ({d6} + 1) x 10gp = {rolled}gp"
    elif rarity == "Uncommon":
        d6 = random.randint(1, 6)
        rolled = d6 * 100
        detail = f"1d6 x 100gp = {d6} x 100gp = {rolled}gp"
    elif rarity == "Rare":
        d10_a = random.randint(1, 10)
        d10_b = random.randint(1, 10)
        rolled = (d10_a + d10_b) * 1_000
        detail = f"2d10 x 1,000gp = ({d10_a} + {d10_b}) x 1,000gp = {rolled}gp"
    elif rarity == "Very Rare":
        d4 = random.randint(1, 4)
        rolled = (d4 + 1) * 10_000
        detail = f"(1d4 + 1) x 10,000gp = ({d4} + 1) x 10,000gp = {rolled}gp"
    elif rarity == "Legendary":
        d6_a = random.randint(1, 6)
        d6_b = random.randint(1, 6)
        rolled = (d6_a + d6_b) * 25_000
        detail = f"2d6 x 25,000gp = ({d6_a} + {d6_b}) x 25,000gp = {rolled}gp"
    else:
        raise ValueError(f"Unsupported rarity: {rarity}")

    final_price = max(rolled, cost_basis)
    return BuyRoll(
        roll_detail=detail,
        rolled_price=rolled,
        final_price=final_price,
        realized_profit=final_price - cost_basis,
    )
