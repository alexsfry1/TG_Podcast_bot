import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
from html import unescape
from typing import Any, Dict, List, Optional

import feedparser
import requests
from telegram import Update
from telegram.error import BadRequest, NetworkError
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

CONFIG_PATH_ENV = "CONFIG_PATH"
DEFAULT_CONFIG_PATH = "config.json"

TAG_RE = re.compile(r"<[^>]+>")
DEFAULT_MAX_UPLOAD_BYTES = 45 * 1024 * 1024
DEFAULT_MAX_SOURCE_BYTES = 200 * 1024 * 1024
DEFAULT_MAX_THUMB_BYTES = 5 * 1024 * 1024
DEFAULT_TRANSCODE_BITRATE_KBPS = 96
MAX_MESSAGE_LENGTH = 4096
FALLBACK_AUDIO_ERRORS = (
    "failed to get http url content",
    "wrong type of the web page content",
    "wrong file identifier/http url specified",
    "webpage_media_empty",
)


class AudioTooLargeError(RuntimeError):
    pass


def load_config(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    admins = data.get("admin_ids", [])
    if isinstance(admins, list):
        cleaned_admins = []
        for admin_id in admins:
            try:
                cleaned_admins.append(int(admin_id))
            except (TypeError, ValueError):
                continue
        data["admin_ids"] = cleaned_admins
    return data


def save_config(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        value = value.strip().lower()
        if value in ("1", "true", "yes", "y", "on"):
            return True
        if value in ("0", "false", "no", "n", "off"):
            return False
    return default


def parse_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return parsed


def get_max_upload_bytes(config: Dict[str, Any]) -> int:
    raw_value = config.get("max_upload_mb")
    if raw_value is None:
        return DEFAULT_MAX_UPLOAD_BYTES
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_UPLOAD_BYTES
    if value <= 0:
        return DEFAULT_MAX_UPLOAD_BYTES
    return int(value * 1024 * 1024)


def get_max_source_bytes(config: Dict[str, Any]) -> int:
    raw_value = config.get("max_source_mb")
    if raw_value is None:
        return DEFAULT_MAX_SOURCE_BYTES
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_SOURCE_BYTES
    if value <= 0:
        return DEFAULT_MAX_SOURCE_BYTES
    return int(value * 1024 * 1024)


def get_transcode_bitrate_kbps(config: Dict[str, Any]) -> int:
    return parse_positive_int(
        config.get("transcode_bitrate_kbps"),
        DEFAULT_TRANSCODE_BITRATE_KBPS,
    )


def strip_html(text: str) -> str:
    text = unescape(text or "")
    text = TAG_RE.sub("", text)
    text = re.sub(r"\s+\n", "\n", text)
    return text.strip()


def get_entry_id(entry: Dict[str, Any]) -> str:
    for key in ("id", "guid", "link"):
        value = entry.get(key)
        if value:
            return str(value)
    return f"{entry.get('title', '')}|{entry.get('published', '')}"


def get_audio_url(entry: Dict[str, Any]) -> Optional[str]:
    for enc in entry.get("enclosures", []) or []:
        href = enc.get("href") or enc.get("url")
        enc_type = enc.get("type", "")
        if href and (enc_type.startswith("audio/") or href.endswith(".mp3")):
            return href
    for link in entry.get("links", []) or []:
        if link.get("rel") != "enclosure":
            continue
        href = link.get("href")
        link_type = link.get("type", "")
        if href and (link_type.startswith("audio/") or href.endswith(".mp3")):
            return href
    return None


def get_image_url(entry: Dict[str, Any], feed: Dict[str, Any]) -> Optional[str]:
    def extract_url(value: Any) -> Optional[str]:
        if isinstance(value, dict):
            return value.get("href") or value.get("url")
        if isinstance(value, str):
            return value
        return None

    for source in (entry, feed):
        for key in ("image", "itunes_image"):
            url = extract_url(source.get(key))
            if url:
                return url

    media = entry.get("media_thumbnail") or entry.get("media_content")
    if isinstance(media, list) and media:
        url = media[0].get("url")
        if url:
            return url

    return None


def download_to_temp(url: str, suffix: str, max_bytes: int) -> str:
    headers = {"User-Agent": "tg-podcast-bot/1.0"}
    with requests.get(url, headers=headers, stream=True, timeout=30) as response:
        response.raise_for_status()
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    raise AudioTooLargeError(
                        f"Audio is too large: {content_length} bytes"
                    )
            except ValueError:
                pass
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            try:
                size = 0
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise AudioTooLargeError(
                            f"Audio exceeded limit: {size} bytes"
                        )
                    tmp.write(chunk)
                return tmp.name
            except AudioTooLargeError:
                tmp.close()
                try:
                    os.remove(tmp.name)
                except OSError:
                    pass
                raise


def transcode_audio(input_path: str, bitrate_kbps: int) -> str:
    output = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    output.close()
    command = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        "-vn",
        "-b:a",
        f"{bitrate_kbps}k",
        "-loglevel",
        "error",
        output.name,
    ]
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg not found. Install ffmpeg to use transcoding.") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("ffmpeg failed to transcode audio.") from exc
    return output.name


def should_fallback_to_download(error: Exception) -> bool:
    message = str(error).lower()
    return any(token in message for token in FALLBACK_AUDIO_ERRORS)


def format_text(entry: Dict[str, Any]) -> str:
    title = (entry.get("title") or "").strip()
    summary = strip_html(entry.get("summary") or entry.get("description") or "")
    link = (entry.get("link") or "").strip()

    parts = [part for part in (title, summary, link) if part]
    text = "\n\n".join(parts)
    if len(text) > 3900:
        text = text[:3900].rstrip() + "..."
    return text


def format_caption(entry: Dict[str, Any], max_length: int = 1024) -> str:
    title = (entry.get("title") or "").strip()
    summary = strip_html(entry.get("summary") or entry.get("description") or "")
    link = (entry.get("link") or "").strip()

    parts = [part for part in (title, summary, link) if part]
    text = "\n\n".join(parts)
    if len(text) > max_length:
        text = text[: max_length - 3].rstrip() + "..."
    return text


def build_fallback_message(text: str, audio_url: str) -> str:
    note = (
        "Файл слишком большой для загрузки ботом.\n"
        f"{audio_url}"
    )
    if not text:
        return note
    combined = f"{text}\n\n{note}"
    if len(combined) > MAX_MESSAGE_LENGTH:
        combined = combined[: MAX_MESSAGE_LENGTH - 3].rstrip() + "..."
    return combined


async def publish_entry(
    bot,
    channel: str,
    entry: Dict[str, Any],
    feed: Dict[str, Any],
    max_upload_bytes: int,
    max_source_bytes: int,
    transcode_enabled: bool,
    transcode_bitrate_kbps: int,
) -> None:
    title = (entry.get("title") or "").strip()
    performer = (feed.get("title") or "").strip() or None
    audio_url = get_audio_url(entry)
    if not audio_url:
        raise RuntimeError("Audio URL not found for entry")

    caption = format_caption(entry)
    text = format_text(entry)
    thumb_path = None
    image_url = get_image_url(entry, feed)
    if image_url:
        try:
            thumb_path = await asyncio.to_thread(
                download_to_temp,
                image_url,
                ".jpg",
                DEFAULT_MAX_THUMB_BYTES,
            )
        except AudioTooLargeError as exc:
            logging.warning("Thumbnail too large: %s", exc)
        except requests.RequestException as exc:
            logging.warning("Failed to download thumbnail: %s", exc)

    async def send_audio_with(source) -> None:
        thumb_file = None
        try:
            if thumb_path:
                thumb_file = open(thumb_path, "rb")
            await bot.send_audio(
                chat_id=channel,
                audio=source,
                title=title or None,
                performer=performer,
                caption=caption or None,
                thumbnail=thumb_file,
            )
        finally:
            if thumb_file:
                thumb_file.close()

    def maybe_transcode(path: str) -> Optional[str]:
        if not transcode_enabled:
            return None
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if size and size <= max_upload_bytes:
            return None
        logging.info("Transcoding audio to %dkbps.", transcode_bitrate_kbps)
        return transcode_audio(path, transcode_bitrate_kbps)

    try:
        await send_audio_with(audio_url)
    except BadRequest as exc:
        if not should_fallback_to_download(exc):
            raise
        logging.info("Falling back to upload audio file from URL.")
        try:
            audio_path = await asyncio.to_thread(
                download_to_temp,
                audio_url,
                ".mp3",
                max_source_bytes,
            )
        except AudioTooLargeError as too_large:
            logging.warning("Audio too large to upload: %s", too_large)
            await bot.send_message(
                chat_id=channel,
                text=build_fallback_message(text, audio_url),
            )
        else:
            transcoded_path = None
            try:
                try:
                    transcoded_path = await asyncio.to_thread(
                        maybe_transcode,
                        audio_path,
                    )
                except Exception as exc:
                    logging.warning("Transcode failed: %s", exc)
                    await bot.send_message(
                        chat_id=channel,
                        text=build_fallback_message(text, audio_url),
                    )
                    return
                path_to_send = transcoded_path or audio_path
                with open(path_to_send, "rb") as audio_file:
                    await send_audio_with(audio_file)
            except NetworkError as net_exc:
                if "Request Entity Too Large" not in str(net_exc):
                    raise
                if transcode_enabled and not transcoded_path:
                    logging.info("Retrying after transcode due to size limit.")
                    try:
                        transcoded_path = await asyncio.to_thread(
                            transcode_audio,
                            audio_path,
                            transcode_bitrate_kbps,
                        )
                        with open(transcoded_path, "rb") as audio_file:
                            await send_audio_with(audio_file)
                    except Exception as exc:
                        logging.warning("Transcode retry failed: %s", exc)
                        await bot.send_message(
                            chat_id=channel,
                            text=build_fallback_message(text, audio_url),
                        )
                else:
                    logging.warning("Telegram upload limit exceeded.")
                    await bot.send_message(
                        chat_id=channel,
                        text=build_fallback_message(text, audio_url),
                    )
            finally:
                if transcoded_path:
                    try:
                        os.remove(transcoded_path)
                    except OSError:
                        logging.warning(
                            "Failed to remove temp audio file: %s",
                            transcoded_path,
                        )
                try:
                    os.remove(audio_path)
                except OSError:
                    logging.warning("Failed to remove temp audio file: %s", audio_path)
    finally:
        if thumb_path:
            try:
                os.remove(thumb_path)
            except OSError:
                logging.warning("Failed to remove temp thumbnail: %s", thumb_path)


async def publish_new_entries(bot, config: Dict[str, Any], config_path: str) -> int:
    rss_url = config["rss_url"]
    max_upload_bytes = get_max_upload_bytes(config)
    max_source_bytes = get_max_source_bytes(config)
    transcode_enabled = parse_bool(config.get("transcode_enabled"), False)
    transcode_bitrate_kbps = get_transcode_bitrate_kbps(config)
    parsed = feedparser.parse(rss_url)
    if parsed.bozo:
        if parsed.entries:
            logging.warning(
                "RSS parse warning: %s",
                parsed.bozo_exception,
            )
        else:
            raise RuntimeError(f"RSS parse error: {parsed.bozo_exception}")

    entries = parsed.entries or []
    if not entries:
        return 0

    last_id = (config.get("last_published_id") or "").strip()
    found_last = False
    new_entries: List[Dict[str, Any]] = []

    for entry in entries:
        entry_id = get_entry_id(entry)
        if last_id and entry_id == last_id:
            found_last = True
            break
        new_entries.append(entry)

    if last_id and not found_last:
        logging.warning("Last published id not found; posting only the latest entry.")
        new_entries = entries[:1]
    elif not last_id:
        new_entries = entries[:1]

    if not new_entries:
        return 0

    max_items = int(config.get("max_items_per_run", 0) or 0)
    if max_items > 0 and len(new_entries) > max_items:
        new_entries = new_entries[:max_items]

    new_entries = list(reversed(new_entries))

    last_posted_id = None
    for entry in new_entries:
        await publish_entry(
            bot,
            config["channel"],
            entry,
            parsed.feed,
            max_upload_bytes,
            max_source_bytes,
            transcode_enabled,
            transcode_bitrate_kbps,
        )
        last_posted_id = get_entry_id(entry)

    if last_posted_id:
        config["last_published_id"] = last_posted_id
        save_config(config_path, config)

    return len(new_entries)


async def safe_reply(update: Update, text: str) -> None:
    message = update.effective_message
    if message:
        await message.reply_text(text)


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        await safe_reply(update, "Не удалось получить ваш ID.")
        return
    await safe_reply(update, f"Ваш ID: {user.id}")


async def new_podcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config_path = context.bot_data["config_path"]
    try:
        config = load_config(config_path)
    except Exception as exc:
        logging.exception("Failed to load config")
        await safe_reply(update, f"Ошибка чтения конфига: {exc}")
        return

    if not config.get("channel"):
        await safe_reply(update, "В конфиге не указан channel.")
        return
    if not config.get("rss_url"):
        await safe_reply(update, "В конфиге не указан rss_url.")
        return

    user_id = update.effective_user.id if update.effective_user else None
    if user_id not in config.get("admin_ids", []):
        await safe_reply(update, "Нет доступа.")
        return

    await safe_reply(update, "Ищу новые выпуски...")
    try:
        count = await publish_new_entries(
            context.bot,
            config,
            config_path,
        )
    except Exception as exc:
        logging.exception("Failed to publish new entries")
        await safe_reply(update, f"Ошибка: {exc}")
        return

    await safe_reply(update, f"Готово. Опубликовано: {count}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    config_path = os.environ.get(CONFIG_PATH_ENV, DEFAULT_CONFIG_PATH)
    config = load_config(config_path)

    token = config.get("bot_token")
    if not token:
        raise RuntimeError("bot_token is required in config")
    if not config.get("channel"):
        raise RuntimeError("channel is required in config")
    if not config.get("rss_url"):
        raise RuntimeError("rss_url is required in config")

    application = ApplicationBuilder().token(token).build()
    application.bot_data["config_path"] = config_path

    application.add_handler(CommandHandler("myid", myid))
    application.add_handler(CommandHandler("new_podcast", new_podcast))

    application.run_polling()


if __name__ == "__main__":
    main()
