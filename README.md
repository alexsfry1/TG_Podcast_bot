# TG Podcast Bot

Telegram bot that posts new podcast episodes from an RSS feed into a channel.

## Setup

1. Create a bot and add it as an admin to your channel (with post rights).
2. Copy `config.json.example` to `config.json` and fill in:
   - `bot_token`
   - `channel` (e.g. `@your_channel`)
   - `admin_ids` (Telegram user IDs allowed to run `/new_podcast`)
   - `rss_url`

## Install

```
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```
python main.py
```

Optional: set a custom config path:

```
CONFIG_PATH=/path/to/config.json python main.py
```

## Usage

Send `/new_podcast` to the bot (from an admin account). The bot will:
1. Fetch the RSS feed
2. Post new items (audio, text, image)
3. Update `last_published_id` in `config.json`
