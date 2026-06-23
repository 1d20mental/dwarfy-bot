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
- Lets players sell permanent magic items with `/dwarfy sell`.
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

## `Bot Items` Columns

The `Bot Items` tab should have these headers:

```text
Item Name
Rarity
Consumable
Allowed
Loot Type
Source
Category
Tags
Min APL
Max APL
Notes
```

Rules:

- Header matching is case-insensitive and ignores extra spaces.
- `Allowed` should be `TRUE` or `FALSE`.
- `Consumable` should be `TRUE` or `FALSE`.
- Blank `Loot Type` means `Item`.
- Supported `Loot Type` values are `Item` and `Monster Component`.
- Blank `Min APL` means no minimum.
- Blank `Max APL` means no maximum.
- Tags are comma-separated.
- Rarity values are normalized, so `very rare`, `Very rare`, and `Very Rare` all become `Very Rare`.

Only permanent magic items can be sold to Dwarfy with `/dwarfy sell`. Consumables are valid for `/sessionloot`, but not for shop sales.

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
2. In the shop channel, run `/dwarfy browse`.
3. Run `/dwarfy inspect listing:DWF-00001`.
4. Run `/dwarfy buy listing:DWF-00001 character:Other Name level:5`.

## Commands

### Player Commands

- `/dwarfy ping`
- `/dwarfy sell`
- `/dwarfy browse`
- `/dwarfy inspect`
- `/dwarfy buy`
- `/sessionloot`

### Admin/Mod Commands

These require one of the role names in `ADMIN_ROLE_NAMES`:

- `/dwarfy stats`
- `/dwarfy void`
- `/dwarfy reload`

## Channel Rules

- `/dwarfy sell` only works in `DWARFY_SELL_CHANNEL_ID`.
- `/dwarfy browse`, `/dwarfy inspect`, and `/dwarfy buy` only work in `DWARFY_SHOP_CHANNEL_ID`.
- `/sessionloot` only checks `SESSION_LOOT_CHANNEL_ID` if that value is filled in.

Wrong-channel errors are private.

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
