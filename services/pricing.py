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
    haggling_roll: int
    haggling_result: str
    discount_percent: int
    discounted_price: int
    final_price: int
    realized_profit: int
    insult_line: str | None = None

    @property
    def cost_basis_floor_applied(self) -> bool:
        """Return True when Dwarfy refused to sell below what he paid."""
        return self.final_price > self.discounted_price

    @property
    def cost_basis_exception_applied(self) -> bool:
        """Return True when a nat 20 let the buyer beat Dwarfy's floor."""
        return self.haggling_roll == 20 and self.realized_profit < 0


DWARFY_NAT1_INSULTS = [
    "A miracle. The fool had money after all.",
    "No discount, but I did include a free lesson in quitting while behind.",
    "That haggle was so bad I almost charged you for making me hear it.",
    "Congratulations. You paid full price with the confidence of someone who thinks soup is a financial strategy.",
    "Real gold. Shame about the brain attached to it.",
    "No discount. But I will remember this when I need cheering up.",
    "You tried. That is not worth anything, but it was loud.",
    "Full price. And I want you to know your haggle made the register sad.",
    "Deal. Full price. May this item serve you better than your mouth just did.",
    "Every coin is here. Somehow, despite your best efforts.",
    "No discount, champ. But I did enjoy watching you lose a fight with arithmetic.",
    "There goes another hero, bravely overpaying in public.",
    "Full price. I would explain why, but then I would have to charge tutoring rates.",
    "I accept your money and reject whatever that negotiation was.",
    "No discount. The item is magical. Your bargaining was not.",
]


def is_supported_rarity(rarity: str) -> bool:
    """Return True when Dwarfy can price this rarity in version 1."""
    return rarity in BASE_PRICES


def base_price_for_rarity(rarity: str) -> int:
    """Return the fixed base price for a supported rarity."""
    if rarity not in BASE_PRICES:
        raise ValueError(f"Unsupported rarity: {rarity}")
    return BASE_PRICES[rarity]


def _validate_base_price(base_price: int) -> int:
    """Return a positive whole-number base price or raise a clear error."""
    price = int(base_price)
    if price < 1:
        raise ValueError("Base price must be at least 1gp.")
    return price


def direct_sell_price(base_price: int) -> SellRoll:
    """Return the guaranteed direct-sale payout.

    Direct sale is intentionally boring: no DTP, no gold fee, no d20 roll, and
    a fixed 40% payout.
    """
    base_price = _validate_base_price(base_price)
    payout = (base_price * 40) // 100
    return SellRoll(
        roll=0,
        result_text="Guaranteed direct sale, 40% of base price",
        payout_percent=40,
        base_price=base_price,
        seller_payout=payout,
    )


def roll_broker_price(base_price: int) -> SellRoll:
    """Roll the downtime brokered player-to-shop sale result."""
    base_price = _validate_base_price(base_price)
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


def roll_sell_price(base_price: int) -> SellRoll:
    """Backward-compatible name for the brokered sale roll."""
    return roll_broker_price(base_price)


def buy_price_formula(base_price: int) -> str:
    """Return the asking price formula shown in inspect/buy output."""
    base_price = _validate_base_price(base_price)
    return f"Base Price = {base_price}gp, then Dwarfy haggling may discount it"


def possible_final_price_range(base_price: int, cost_basis: int) -> tuple[int, int]:
    """Return the lowest and highest possible final buy price.

    Haggling can reduce the low end by up to 20%. A natural 20 is the only time
    Dwarfy lets the final price drop below his cost basis.
    """
    base_price = _validate_base_price(base_price)
    best_discounted_min = (base_price * 80) // 100
    return best_discounted_min, max(base_price, int(cost_basis))


def roll_buy_price(base_price: int, cost_basis: int) -> BuyRoll:
    """Roll Dwarfy haggling against the sheet's fixed Base Price."""
    rolled = _validate_base_price(base_price)
    detail = f"Base Price = {rolled}gp"

    haggling_roll = random.randint(1, 20)
    insult_line = None
    if haggling_roll == 20:
        discount_percent = 20
        haggling_result = "Masterful haggling, 20% discount"
    elif haggling_roll >= 16:
        discount_percent = 10
        haggling_result = "Strong haggling, 10% discount"
    elif haggling_roll == 15:
        discount_percent = 5
        haggling_result = "Barely successful haggling, 5% discount"
    elif haggling_roll == 1:
        discount_percent = 0
        haggling_result = "Dwarfy is offended. No discount."
        insult_line = random.choice(DWARFY_NAT1_INSULTS)
    else:
        discount_percent = 0
        haggling_result = "Dwarfy does not budge."

    discounted_price = (rolled * (100 - discount_percent)) // 100
    if haggling_roll == 20:
        final_price = discounted_price
    else:
        final_price = max(discounted_price, cost_basis)
    return BuyRoll(
        roll_detail=detail,
        rolled_price=rolled,
        haggling_roll=haggling_roll,
        haggling_result=haggling_result,
        discount_percent=discount_percent,
        discounted_price=discounted_price,
        final_price=final_price,
        realized_profit=final_price - cost_basis,
        insult_line=insult_line,
    )
