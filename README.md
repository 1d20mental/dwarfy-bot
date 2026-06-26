# Dwarfy Bot

Dwarfy Bot is a public Python Discord bot for a D&D server. It uses slash commands only.

It does two separate jobs:

- `/sessionloot` rolls a complete session loot package from a Google Sheet.
- `/dwarfy` commands run Dwarfy's magic-item shop, where players sell permanent magic items to the shop and other players buy those items later.

Google Sheets is the master item reference. SQLite is the live shop inventory and ledger.

## What The Bot Does

- Loads the `Bot Items` and `Monster Components` tabs from one Google Sheet.
- Caches that sheet data in memory so commands are fast.
- Creates a SQLite database automatically at `data/dwarfy.sqlite`.
- Lets players sell permanent magic items directly with `/dwarfy sell`.
- Lets players broker permanent magic-item sales with `/dwarfy broker`.
- Lets players browse, inspect, and buy Dwarfy shop listings.
- Lets admins/mods run `/dwarfy stats`, `/dwarfy void`, and `/dwarfy reload`.
- Rolls session loot with `/sessionloot`.

The bot does not check character ownership, deduct DTP, deduct gold, or manage mundane equipment. Players should record all downtime and purchases manually on their adventure logs.

## Create A Discord Bot Token

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application**.
3. Give it a name, such as `Dwarfy Bot`.
4. Open **Bot** in the left menu.
5. Click **Reset Token** or **View Token**.
6. Copy the token into your local `.env` file as `DISCORD_TOKEN=...`.

Never commit the real token. This repo only includes `.env.example`.

## Invite The Bot

1. In the Developer Portal, open your application.
2. Go to **OAuth2** then **URL Generator**.
3. Select scopes:
   - `bot`
   - `applications.commands`
4. Select bot permissions:
   - Send Messages
   - Read Message History
   - Use Slash Commands
5. Copy the generated URL and open it in your browser.
6. Choose your server and invite the bot.

## Create Your `.env` File

Copy `.env.example` to `.env`, then fill in real values:

```env
DISCORD_TOKEN=your_discord_bot_token
GUILD_ID=your_test_server_id
ADMIN_ROLE_NAMES=Admin,Moderator,DM,Loot Manager

DWARFY_SELL_CHANNEL_ID=your_sell_channel_id
DWARFY_SHOP_CHANNEL_ID=your_shop_channel_id
SESSION_LOOT_CHANNEL_ID=your_session_loot_channel_id_or_blank

GOOGLE_SHEET_ID=your_google_sheet_id
GOOGLE_SERVICE_ACCOUNT_FILE=service-account.json
BOT_ITEMS_TAB=Bot Items
MONSTER_COMPONENTS_TAB=Monster Components

DATABASE_PATH=data/dwarfy.sqlite
```

`SESSION_LOOT_CHANNEL_ID` may be left blank. If it is blank, `/sessionloot` works in any channel.

## Get Discord IDs

1. In Discord, open **User Settings**.
2. Go to **Advanced**.
3. Turn on **Developer Mode**.
4. Right-click your server and click **Copy Server ID** for `GUILD_ID`.
5. Right-click a channel and click **Copy Channel ID** for the channel variables.

`GUILD_ID` makes slash command syncing fast while testing.

## Google Service Account

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create or choose a project.
3. Enable the **Google Sheets API**.
4. Go to **IAM & Admin** then **Service Accounts**.
5. Create a service account.
6. Open the service account, go to **Keys**, and create a JSON key.
7. Download the JSON key.
8. Save it locally as `service-account.json` in the project folder.

Never commit this JSON file. It is ignored by `.gitignore`.

## Share The Sheet

Open your Google Sheet and click **Share**.

Share it with the service account email. The email looks like:

```text
something@your-project.iam.gserviceaccount.com
```

Viewer access is enough because the bot only reads the sheet.

## Workbook Tabs

The workbook can contain these tabs:

- `Start Here`
- `Bot Items`
- `Monster Components`
- `Validation Summary`
- `Weight Audit`
- `Source Index`
- `Source Mapping Audit`
- `Deleted Rows`

At runtime the bot only reads:

- `Bot Items`
- `Monster Components`

The other tabs are human/admin reference tabs. They help you maintain the sheet, but the bot should still run without reading them.

`Source Index` is the human-readable index of source abbreviations and full book names. If you add a future source, usually you only need to add it to `Source Index` and then use that player-facing `Source` label in `Bot Items`. No code change should be needed.

## `Bot Items` Columns

The `Bot Items` tab should have these headers:

```text
Item Name
Rarity
Roll Rarity
Weight
Consumable
Allowed
Loot Type
Creature Type
Source
Source Code
Source Name
Alternate Sources
Category
Tags
Min APL
Max APL
Session Eligible
Dwarfy Sell Eligible
Variant Type
Variant Instructions
Variant Options
Page
Item Type
Attunement
Display Detail
Short Description
Rules Text
JSON Notes
Item Tags
JSON Source Key
JSON Match Status
Notes
```

Rules:

- Header matching is case-insensitive and ignores extra spaces.
- The bot tolerates extra columns and ignores columns it does not need.
- Do not rename required columns.
- `Item Name` is the name shown in Discord.
- `Rarity` is the actual item rarity and is used by Dwarfy pricing.
- `Roll Rarity` is the rarity bucket used by `/sessionloot`.
- Blank `Roll Rarity` excludes a row from session loot.
- If the whole `Roll Rarity` column is missing, the bot falls back to `Rarity` for older sheets.
- `Weight` is relative probability for session loot.
- Weight totals do not need to equal 100.
- Blank `Weight` defaults to `1`.
- Invalid `Weight` creates a reload warning and defaults to `1`.
- `Allowed` should be `TRUE` or `FALSE`.
- `Allowed=FALSE` excludes the row from `/sessionloot` and blocks `/dwarfy sell` and `/dwarfy broker`.
- `Session Eligible=FALSE` excludes the row from `/sessionloot`.
- `Consumable` should be `TRUE` or `FALSE`.
- `Consumable=TRUE` is for consumable loot slots.
- `Consumable=FALSE` is for permanent loot slots.
- Blank `Loot Type` means `Item`.
- Supported `Loot Type` values are `Item` and `Monster Component`.
- `Creature Type` can be used by Monster Component trigger rows.
- `Source` is what players see in Discord. Keep player-facing labels here, such as `DMG 2024`, `PHB 2024`, `XGE`, `TCE`, `BMT`, `FRHOF`, or `HGtMH`.
- `Source Code` is only for reference. It is not shown in normal public loot output.
- `Source Name` is only for reference. It is not shown in normal public loot output.
- `Alternate Sources` is reference/future-use information.
- `Tags` are comma-separated and case-insensitive.
- Blank `Min APL` means no minimum.
- Blank `Max APL` means no maximum.
- Rarity values are normalized, so `very rare`, `Very rare`, and `Very Rare` all become `Very Rare`.
- `Dwarfy Sell Eligible=FALSE` blocks `/dwarfy sell` and `/dwarfy broker` for that row.
- `Variant Type` describes generic/template rows, such as `Generic Weapon`, `Generic Armor`, `Generic Shield`, `Generic Ammunition`, `Generic Item`, or `Specific Item`.
- `Variant Instructions` tells the DM/player how to resolve a generic row.
- `Variant Options` is an optional comma-separated suggestion list for the `/dwarfy sell` and `/dwarfy broker` variant field.
- `Page` is shown with the player-facing source when available.
- `Item Type`, `Attunement`, `Display Detail`, `Short Description`, and `Rules Text` come from JSON-enriched item data and are used for clean display and audit receipts.
- `JSON Notes`, `Item Tags`, `JSON Source Key`, and `JSON Match Status` are optional reference/audit columns.
- `Notes` are for human reference.

Only permanent magic items can be sold to Dwarfy with `/dwarfy sell` or `/dwarfy broker`. Consumables are valid for `/sessionloot`, but not for shop sales.

Generic/template items stay as one row. For example, `+1 Weapon` should stay as `+1 Weapon`; the bot should not generate every possible longsword, rapier, or bow in code. Use `Variant Type` and `Variant Instructions` to tell the DM/player how to resolve it.

Example:

```text
Item Name: +1 Weapon
Variant Type: Generic Weapon
Variant Instructions: Choose any valid weapon when awarded.
```

For `/dwarfy sell` and `/dwarfy broker`, players can use the optional `variant` field for generic/template item identity:

```text
item: +1 Weapon
variant: Longsword
```

The shop listing will show:

```text
+1 Weapon (Longsword)
```

Use `details` only for custom notes, such as a minor property, inscription, or session note. Do not paste the item description into `item` or `details`; the bot already reads item data from the sheet.

Normal specific items should not use `variant`. For example, `Ring of Protection` should be sold as:

```text
item: Ring of Protection
```

## Selling To Dwarfy

Dwarfy has two player-facing ways to turn a permanent magic item into gold.

`/dwarfy sell` is a direct sale:

- No DTP cost.
- No gold cost.
- No downtime broker roll.
- Guaranteed payout: `40%` of the item's base price.
- The item enters Dwarfy's magic inventory.

`/dwarfy broker` is a downtime brokered sale:

- Costs `5 DTP` and `25gp` manually.
- Rolls a flat `1d20`.
- Can pay better or worse than direct sale.
- A natural 1 loses the item and it does not enter Dwarfy's inventory.
- A successful brokered item enters Dwarfy's magic inventory.

Direct sale is fast and safe, but pays less. Brokered sale spends downtime and gold for a chance at a better payout. Players who want better than direct sale without broker risk should try to sell to another player.

Brokered sale roll table:

| d20 | Result | Payout |
| --- | --- | --- |
| 20 | Excellent buyer | 100% of base price |
| 16-19 | Strong buyer | 60% of base price |
| 10-15 | Fair buyer | 50% of base price |
| 6-9 | Weak buyer | 30% of base price |
| 2-5 | Poor buyer | 20% of base price |
| 1 | Disaster, item lost | 0gp |

## Weighting Example

Weights are ticket ranges, not percentages.

Bag of Holding has Weight `2`.
Boots of Elvenkind has Weight `1`.
Cloak of Protection has Weight `3`.

The total weight is `6`.

- Tickets `1-2` select Bag of Holding.
- Ticket `3` selects Boots of Elvenkind.
- Tickets `4-6` select Cloak of Protection.

Changing a `Weight` cell and running `/dwarfy reload` updates future session-loot probabilities.

If `/sessionloot` rolls a rarity that has no eligible pool for that slot type and APL, the bot automatically fills the slot from the nearest valid rarity pool. It tries higher rarities first when two fallback rarities are equally close, keeps permanent slots permanent and consumable slots consumable, and keeps the original d100 roll visible in the public output.

Example:

```text
Permanent 1: 10 -> Common, fallback to Uncommon -> Gloves of Swimming and Climbing
```

## `Monster Components` Columns

The `Monster Components` tab should have these headers:

```text
Creature Type
Roll
Component
Examples
```

The bot rolls `1d100` for monster components.

`Roll` can be one number like `41` or a range like `41-60`. A normal hyphen and an en dash both work.

## Install Dependencies

Use Python 3.11 or newer.

```bash
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

On macOS or Linux, activate with:

```bash
source .venv/bin/activate
```

## Run The Bot Locally

```bash
python bot.py
```

On startup the bot will:

1. Connect to Discord.
2. Create the `data/` folder if needed.
3. Create the SQLite database and tables if needed.
4. Load `Bot Items`.
5. Load `Monster Components`.
6. Sync slash commands to `GUILD_ID` if it is set.

If Google Sheets cannot load, the bot still starts when possible. Sheet-dependent commands will reply with a clear private error until the sheet is fixed and `/dwarfy reload` succeeds.

## First Tests In Discord

1. Run `/dwarfy ping`.
   - Expected reply: `Dwarfy's Shop is open.`

2. Run `/dwarfy reload`.
   - You need one of the roles listed in `ADMIN_ROLE_NAMES`.
   - Expected reply includes Bot Items row count, Monster Component row count, and warnings.

3. Run `/sessionloot players:4 apl:3`.
   - Expected result is a public session loot report.

After that, test the shop flow:

1. In the sell channel, run `/dwarfy sell character:Name level:5 item:Bag of Holding`.
2. In the sell channel, run `/dwarfy broker character:Name level:5 item:Bag of Holding`.
3. For a generic/template item, run `/dwarfy sell character:Name level:5 item:+1 Weapon variant:Longsword`.
4. In the shop channel, run `/dwarfy browse`.
5. Run `/dwarfy inspect listing:DWF-00001`.
6. Run `/dwarfy buy listing:DWF-00001 character:Other Name level:5 gold:2000`.

## Commands

### Player Commands

- `/dwarfy ping`
- `/dwarfy sell`
- `/dwarfy broker`
- `/dwarfy browse`
- `/dwarfy inspect`
- `/dwarfy buy`
- `/sessionloot`

### Admin/Mod Commands

These require one of the role names in `ADMIN_ROLE_NAMES`:

- `/dwarfy stats`
- `/dwarfy void`
- `/dwarfy reload`

### Owner Commands

These require either Discord server ownership or an `Owner` role:

- `/dwarfy stock_add` adds a specific item from `Bot Items` to Dwarfy's inventory. The `item` field autocompletes clean item names. Generic/template items can use `variant`, such as `Longsword` for `+1 Weapon`. `cost_basis` is optional; if blank, the bot uses the same 40% direct-sale value Dwarfy would pay a player.
- `/dwarfy stock_random` adds a weighted random batch of shop stock from the Google Sheet. By default it creates 20 permanent items and 30 consumables using an owner-stock rarity table and the sheet's item weights. `clear_first=True` voids old owner stock before adding the new batch.
- `/dwarfy stock_clear` voids currently available owner-stocked listings. It does not delete records, and it does not touch player-sold inventory.
- `/dwarfy stock_gold` records gold added to Dwarfy's ledger for audit purposes.

Owner-stocked listings are marked separately in SQLite with `stock_source=owner_stock`. They show up in `/dwarfy browse`, `/dwarfy inspect`, and `/dwarfy buy` like normal shop inventory, but their origin displays as `Dwarfy stock` instead of a player seller.

## Channel Rules

- `/dwarfy sell` and `/dwarfy broker` only work in `DWARFY_SELL_CHANNEL_ID`.
- `/dwarfy browse`, `/dwarfy inspect`, and `/dwarfy buy` only work in `DWARFY_SHOP_CHANNEL_ID`.
- `/sessionloot` only checks `SESSION_LOOT_CHANNEL_ID` if that value is filled in.

Wrong-channel errors are private.

`/dwarfy browse` and `/dwarfy inspect` replies are private to the person who ran the command so shop lookups do not clutter the channel. Browse shows all matching listings up to a safety cap and splits long results across private follow-up messages. Completed sales, brokered sales, purchases, and session loot remain public audit messages.

## Buying From Dwarfy

`/dwarfy buy` asks for the character's available gold. Dwarfy already owns the listed item; there is no outside seller search.

The bot first rolls the normal Xanathar-style asking price for the item's rarity. Then it rolls a flat `1d20` Dwarfy haggling roll:

- `20`: 20% discount.
- `16-19`: 10% discount.
- `15`: 5% discount.
- `2-14`: no discount.
- `1`: no discount and Dwarfy insults the buyer, but there is no mechanical penalty.

The haggling roll can only reduce the item price. The final item price can never be below Dwarfy's cost basis. `/dwarfy buy` has no DTP cost and no flat shop/search expense; only the final item price matters.

Once `/dwarfy buy` is submitted and the listing is valid, the deal is final. If the final item price is higher than the declared gold, the bot still marks the item as sold to that character. The character owes the shortfall plus a `5,000gp` contract-default fine, is jailed/unplayable until that debt is paid, and cannot sell or trade the item until the debt is cleared.

## Common Errors

### `DISCORD_TOKEN is not set`

You have not created `.env`, or `DISCORD_TOKEN=` is blank.

### `Google service account file was not found`

`GOOGLE_SERVICE_ACCOUNT_FILE` points to a JSON file that is not in the project folder. Put the downloaded service account JSON there, or update the path in `.env`.

### `The caller does not have permission`

The Google Sheet has not been shared with the service account email.

### `Google Sheet data is not loaded yet`

The bot started, but the sheet cache failed to load. Check the terminal logs, fix the sheet or credentials, then run `/dwarfy reload`.

### Slash commands do not appear

Make sure `GUILD_ID` is your server ID, restart the bot, and watch the terminal for `Synced ... guild slash commands`.

### `/dwarfy sell` says multiple item matches

The fuzzy matcher found several possible sheet items. Run the command again with the exact item name shown in the private reply.

### `/dwarfy buy` says the listing is sold or voided

That item is no longer available. Run `/dwarfy browse` again.

## Security Notes

Do not commit:

- `.env`
- real Discord tokens
- real Google service account JSON files
- SQLite database files
- anything in `data/`

Those paths are already listed in `.gitignore`.
