# TG Podcast Bot

Telegram bot that publishes podcast episodes from an RSS feed into a channel. The bot posts a single message per episode: audio + caption + cover image (thumbnail).

## What the bot does

- Fetches RSS by URL.
- Extracts title, description, cover image, and MP3 enclosure.
- Sends a single Telegram message with audio + caption + cover.
- Tracks `last_published_id` in `config.json` to avoid duplicates.
- Supports a full backfill mode (`/process_all_podcast`) with a configurable delay between posts.
- If MP3 is too big for Telegram, it can transcode to fit the limit (optional).
- If transcoding was used, it can append the original MP3 link in the same message.

## Requirements

- Python 3.10+
- ffmpeg (only if you enable transcoding or thumbnails)

## Setup

1. Create a bot with @BotFather and get the token.
2. Add the bot to your channel as an administrator with post rights.
3. Copy `config.json.example` to `config.json`.
4. Fill in the config values.

## Install (macOS/Linux)

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### fish shell

```
python3 -m venv .venv
source .venv/bin/activate.fish
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

## Commands

- `/myid` — returns your Telegram user ID.
- `/new_podcast` — publishes new episodes since `last_published_id`.
- `/process_all_podcast` — publishes all episodes from the RSS (oldest → newest) with delay between posts.
- `/reset_process_all` — clears `process_all_last_id` so `/process_all_podcast` starts from the beginning.
- `/add_admin <user_id>` — adds a user ID to admin list (admin-only).
- `/remove_admin <user_id>` — removes a user ID from admin list (admin-only).
- `/update_config ключ:значение` — updates any config field (admin-only). Values can be JSON (numbers, booleans, arrays).
- `/set_language en|ru` — switches bot language (admin-only).
- `/help` — shows available commands for your role.

## Config reference

Example in `config.json.example`.

Required:
- `bot_token` — token from @BotFather.
- `channel` — channel username (e.g. `@my_channel`) or numeric ID.
- `admin_ids` — list of Telegram user IDs allowed to run commands.
- `rss_url` — RSS feed URL.

State:
- `last_published_id` — last posted entry ID (bot updates this automatically).
- `process_all_last_id` — last entry processed by `/process_all_podcast` (used for resume).

Optional:
- `language` — `ru` or `en`.
- `auto_check_enabled` — `true/false` to enable automatic checks.
- `auto_check_interval_seconds` — interval for automatic checks (default: 86400).
- `max_items_per_run` — limit for `/new_podcast` only.
- `max_upload_mb` — Telegram upload size limit (default 45 MB).
- `max_source_mb` — maximum size to download for transcoding.
- `transcode_enabled` — `true/false` to enable MP3 transcoding.
- `send_original_link` — append original MP3 link when transcoding is used.
- `process_all_delay_seconds` — delay between posts in `/process_all_podcast` mode.

## How posting works

1. Bot reads RSS and extracts audio URL and description.
2. It tries to send audio by URL directly.
3. If Telegram cannot fetch the URL, it downloads and uploads the file.
4. If the file is too large and `transcode_enabled=true`, it transcodes to fit.
5. If still too large, the bot posts a text fallback with a link.

## Troubleshooting

- Bot doesn’t respond: message the bot in a private chat and make sure your ID is in `admin_ids`.
- `JSONDecodeError`: check commas in `config.json`.
- `Failed to get http url content`: fallback download is used automatically.
- `Request Entity Too Large (413)`: enable transcoding or reduce bitrate.
- `Timed out`: large upload, retry or increase delays.

## Notes

- Bot uses polling (no webhooks).
- The bot reads `config.json` on each command, so you can edit settings without restarting (except `bot_token`).
