import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from html import unescape
from typing import Any, Awaitable, Callable, Dict, List, Optional

import feedparser
import requests
from telegram import Update
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

CONFIG_PATH_ENV = "CONFIG_PATH"
DEFAULT_CONFIG_PATH = "config.json"
PUBLISH_LOCK_KEY = "publish_lock"
CONFIG_LOCK_KEY = "config_lock"

TAG_RE = re.compile(r"<[^>]+>")
DEFAULT_MAX_UPLOAD_BYTES = 45 * 1024 * 1024
DEFAULT_MAX_SOURCE_BYTES = 200 * 1024 * 1024
DEFAULT_MAX_THUMB_BYTES = 15 * 1024 * 1024
DEFAULT_SEND_ORIGINAL_LINK = True
DEFAULT_TRANSCODE_HEADROOM = 0.9
DEFAULT_TRANSCODE_MAX_KBPS = 320
DEFAULT_TRANSCODE_MIN_KBPS = 8
THUMB_MAX_BYTES = 200 * 1024
THUMB_MAX_DIM = 320
THUMB_QUALITIES = (6, 10, 14, 18, 22, 26, 30)
THUMB_SIZES = (THUMB_MAX_DIM, 256, 192, 128)
DEFAULT_PROCESS_ALL_DELAY_SECONDS = 120
DEFAULT_AUDIO_SEND_MODE = "auto"
DEFAULT_DOWNLOAD_RETRIES = 3
DEFAULT_DOWNLOAD_RETRY_BASE_SECONDS = 2
DEFAULT_DOWNLOAD_TIMEOUT = (10, 60)
MAX_MESSAGE_LENGTH = 4096
DEFAULT_LANGUAGE = "ru"
SUPPORTED_LANGUAGES = ("ru", "en")
DEFAULT_AUTO_CHECK_ENABLED = True
DEFAULT_AUTO_CHECK_INTERVAL_SECONDS = 24 * 60 * 60

MESSAGES = {
    "ru": {
        "id_not_found": "Не удалось получить ваш ID.",
        "config_read_error": "Ошибка чтения конфига: {error}",
        "config_missing_channel": "В конфиге не указан channel.",
        "config_missing_rss_url": "В конфиге не указан rss_url.",
        "no_access": "Нет доступа.",
        "searching_new": "Ищу новые выпуски...",
        "processing_all": "Запускаю обработку всех выпусков...",
        "publishing_in_progress": "Публикация уже выполняется. Дождитесь её завершения.",
        "uploading_audio": "Загружаю аудиофайл по ссылке...",
        "transcoding_audio": "Перекодирую аудио в {bitrate_kbps} кбит/с...",
        "done_published": "Готово. Опубликовано: {count}",
        "error_generic": "Ошибка: {error}",
        "process_all_reset": "Сбросил process_all_last_id.",
        "usage_add_admin": "Использование: /add_admin <user_id>",
        "usage_remove_admin": "Использование: /remove_admin <user_id>",
        "usage_update_config": "Использование: /update_config ключ:значение",
        "usage_set_language": "Использование: /set_language en|ru",
        "invalid_user_id": "Некорректный user_id.",
        "admin_already": "Этот пользователь уже админ.",
        "admin_added": "Добавил админа: {user_id}",
        "admin_missing": "Такого админа нет.",
        "admin_remove_last": "Нельзя удалить последнего админа.",
        "admin_removed": "Удалил админа: {user_id}",
        "admin_ids_list_required": "admin_ids должен быть списком.",
        "config_key_missing": "Не указан ключ.",
        "bot_token_updated": "bot_token обновлен. Перезапусти бота.",
        "config_updated": "Обновил {key}.",
        "language_set": "Язык переключен на {language}.",
        "language_unsupported": "Недоступный язык: {language}",
        "help_title": "Команды:",
        "fallback_too_large": "Файл слишком большой для загрузки ботом.\n{url}",
        "original_label": (
            "Оригинал: {url}\n"
            "Аудио было сжато из-за ограничения Telegram на размер файлов для ботов."
        ),
        "help_myid": "/myid - показывает ваш Telegram ID",
        "help_new": "/new_podcast - публикует новые выпуски после last_published_id",
        "help_all": "/process_all_podcast - публикует все выпуски из RSS (от старых к новым)",
        "help_reset": "/reset_process_all - сбрасывает process_all_last_id для начала заново",
        "help_add_admin": "/add_admin <user_id> - добавить админа по ID",
        "help_remove_admin": "/remove_admin <user_id> - удалить админа по ID",
        "help_update": "/update_config ключ:значение - обновляет поле конфига",
        "help_set_language": "/set_language en|ru - переключает язык бота",
        "help_help": "/help - выводит список доступных команд",
        "myid_reply": "Ваш ID: {user_id}",
    },
    "en": {
        "id_not_found": "Could not get your ID.",
        "config_read_error": "Config read error: {error}",
        "config_missing_channel": "channel is not set in config.",
        "config_missing_rss_url": "rss_url is not set in config.",
        "no_access": "Access denied.",
        "searching_new": "Looking for new episodes...",
        "processing_all": "Processing all episodes...",
        "publishing_in_progress": "Publishing is already in progress. Wait for it to finish.",
        "uploading_audio": "Uploading audio file from URL...",
        "transcoding_audio": "Transcoding audio to {bitrate_kbps} kbps...",
        "done_published": "Done. Published: {count}",
        "error_generic": "Error: {error}",
        "process_all_reset": "process_all_last_id reset.",
        "usage_add_admin": "Usage: /add_admin <user_id>",
        "usage_remove_admin": "Usage: /remove_admin <user_id>",
        "usage_update_config": "Usage: /update_config key:value",
        "usage_set_language": "Usage: /set_language en|ru",
        "invalid_user_id": "Invalid user_id.",
        "admin_already": "User is already an admin.",
        "admin_added": "Added admin: {user_id}",
        "admin_missing": "Admin not found.",
        "admin_remove_last": "Cannot remove the last admin.",
        "admin_removed": "Removed admin: {user_id}",
        "admin_ids_list_required": "admin_ids must be a list.",
        "config_key_missing": "Key is required.",
        "bot_token_updated": "bot_token updated. Restart the bot.",
        "config_updated": "Updated {key}.",
        "language_set": "Language set to {language}.",
        "language_unsupported": "Unsupported language: {language}",
        "help_title": "Commands:",
        "fallback_too_large": "File is too large to upload via bot.\n{url}",
        "original_label": (
            "Original: {url}\n"
            "Audio was compressed due to Telegram bot file size limits."
        ),
        "help_myid": "/myid - shows your Telegram user ID",
        "help_new": "/new_podcast - publishes new episodes since last_published_id",
        "help_all": "/process_all_podcast - publishes all episodes (oldest to newest)",
        "help_reset": "/reset_process_all - resets process_all_last_id to start over",
        "help_add_admin": "/add_admin <user_id> - add an admin by ID",
        "help_remove_admin": "/remove_admin <user_id> - remove an admin by ID",
        "help_update": "/update_config key:value - updates a config field",
        "help_set_language": "/set_language en|ru - switches bot language",
        "help_help": "/help - shows available commands",
        "myid_reply": "Your ID: {user_id}",
    },
}
FALLBACK_AUDIO_ERRORS = (
    "failed to get http url content",
    "wrong type of the web page content",
    "wrong file identifier/http url specified",
    "webpage_media_empty",
    "request entity too large",
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
    directory = os.path.dirname(os.path.abspath(path))
    fd, temp_path = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=directory)
    try:
        os.chmod(temp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


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


def parse_non_negative_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < 0:
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


def get_max_thumb_source_bytes(config: Dict[str, Any]) -> int:
    raw_value = config.get("max_thumb_source_mb")
    if raw_value is None:
        return DEFAULT_MAX_THUMB_BYTES
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_THUMB_BYTES
    if value <= 0:
        return DEFAULT_MAX_THUMB_BYTES
    return int(value * 1024 * 1024)


def get_send_original_link(config: Dict[str, Any]) -> bool:
    return parse_bool(config.get("send_original_link"), DEFAULT_SEND_ORIGINAL_LINK)


def get_audio_send_mode(config: Dict[str, Any]) -> str:
    value = (config.get("audio_send_mode") or "").strip().lower()
    if value in ("auto", "upload", "url"):
        return value
    return DEFAULT_AUDIO_SEND_MODE


def get_process_all_delay_seconds(config: Dict[str, Any]) -> int:
    return parse_non_negative_int(
        config.get("process_all_delay_seconds"),
        DEFAULT_PROCESS_ALL_DELAY_SECONDS,
    )


def get_auto_check_enabled(config: Dict[str, Any]) -> bool:
    return parse_bool(config.get("auto_check_enabled"), DEFAULT_AUTO_CHECK_ENABLED)


def get_auto_check_interval_seconds(config: Dict[str, Any]) -> int:
    return parse_positive_int(
        config.get("auto_check_interval_seconds"),
        DEFAULT_AUTO_CHECK_INTERVAL_SECONDS,
    )


def strip_html(text: str) -> str:
    text = unescape(text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"(?i)<p\s*>", "", text)
    text = re.sub(r"(?i)</li\s*>", "\n", text)
    text = re.sub(r"(?i)<li\s*>", "- ", text)
    text = TAG_RE.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
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
    last_exc = None
    for attempt in range(1, DEFAULT_DOWNLOAD_RETRIES + 1):
        try:
            with requests.get(
                url,
                headers=headers,
                stream=True,
                timeout=DEFAULT_DOWNLOAD_TIMEOUT,
            ) as response:
                response.raise_for_status()
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        if int(content_length) > max_bytes:
                            raise AudioTooLargeError(
                                f"File is too large: {content_length} bytes"
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
                                    f"File exceeded limit: {size} bytes"
                                )
                            tmp.write(chunk)
                        return tmp.name
                    except (AudioTooLargeError, requests.RequestException, OSError):
                        tmp.close()
                        try:
                            os.remove(tmp.name)
                        except OSError:
                            pass
                        raise
        except AudioTooLargeError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= DEFAULT_DOWNLOAD_RETRIES:
                raise
            delay = DEFAULT_DOWNLOAD_RETRY_BASE_SECONDS ** attempt
            logging.warning(
                "Download failed: %s. Retrying in %ss (%d/%d).",
                exc,
                delay,
                attempt,
                DEFAULT_DOWNLOAD_RETRIES,
            )
            time.sleep(delay)
    if last_exc:
        raise last_exc


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
        try:
            os.remove(output.name)
        except OSError:
            pass
        raise RuntimeError("ffmpeg not found. Install ffmpeg to use transcoding.") from exc
    except subprocess.CalledProcessError as exc:
        try:
            os.remove(output.name)
        except OSError:
            pass
        raise RuntimeError("ffmpeg failed to transcode audio.") from exc
    return output.name


def create_thumbnail(input_path: str) -> Optional[str]:
    best_path = None
    best_size = None
    for size in THUMB_SIZES:
        for quality in THUMB_QUALITIES:
            output = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            output.close()
            command = [
                "ffmpeg",
                "-y",
                "-i",
                input_path,
                "-vf",
                (
                    f"scale='min({size},iw)':'min({size},ih)':"
                    "force_original_aspect_ratio=decrease"
                ),
                "-frames:v",
                "1",
                "-q:v",
                str(quality),
                "-loglevel",
                "error",
                output.name,
            ]
            try:
                subprocess.run(command, check=True)
            except FileNotFoundError as exc:
                try:
                    os.remove(output.name)
                except OSError:
                    pass
                raise RuntimeError(
                    "ffmpeg not found. Install ffmpeg to use thumbnails."
                ) from exc
            except subprocess.CalledProcessError:
                try:
                    os.remove(output.name)
                except OSError:
                    pass
                continue

            try:
                size_bytes = os.path.getsize(output.name)
            except OSError:
                size_bytes = 0
            if size_bytes <= 0:
                try:
                    os.remove(output.name)
                except OSError:
                    pass
                continue

            if best_size is None or (size_bytes and size_bytes < best_size):
                if best_path:
                    try:
                        os.remove(best_path)
                    except OSError:
                        pass
                best_path = output.name
                best_size = size_bytes
            else:
                try:
                    os.remove(output.name)
                except OSError:
                    pass

            if size_bytes and size_bytes <= THUMB_MAX_BYTES:
                return best_path
    if best_path and best_size and best_size <= THUMB_MAX_BYTES:
        return best_path
    if best_path:
        try:
            os.remove(best_path)
        except OSError:
            pass
    return None


def get_audio_duration_seconds(path: str) -> float:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffprobe not found. Install ffmpeg to use transcoding.") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("ffprobe failed to read duration.") from exc
    output = result.stdout.strip()
    if not output:
        raise RuntimeError("ffprobe returned empty duration.")
    try:
        return float(output)
    except ValueError as exc:
        raise RuntimeError("ffprobe returned invalid duration.") from exc


def choose_transcode_bitrate_kbps(duration_seconds: float, max_upload_bytes: int) -> int:
    target_bits = int(max_upload_bytes * 8 * DEFAULT_TRANSCODE_HEADROOM)
    bitrate_kbps = int(target_bits / max(duration_seconds, 1.0) / 1000)
    if bitrate_kbps > DEFAULT_TRANSCODE_MAX_KBPS:
        return DEFAULT_TRANSCODE_MAX_KBPS
    if bitrate_kbps < DEFAULT_TRANSCODE_MIN_KBPS:
        return DEFAULT_TRANSCODE_MIN_KBPS
    return bitrate_kbps


def should_fallback_to_download(error: Exception) -> bool:
    message = str(error).lower()
    return any(token in message for token in FALLBACK_AUDIO_ERRORS)


def format_text(entry: Dict[str, Any]) -> str:
    title = (entry.get("title") or "").strip()
    summary = strip_html(entry.get("summary") or entry.get("description") or "")

    parts = [part for part in (title, summary) if part]
    text = "\n\n".join(parts)
    if len(text) > 3900:
        text = text[:3900].rstrip() + "..."
    return text


def format_caption(entry: Dict[str, Any], max_length: int = 1024) -> str:
    title = (entry.get("title") or "").strip()
    summary = strip_html(entry.get("summary") or entry.get("description") or "")

    parts = [part for part in (title, summary) if part]
    text = "\n\n".join(parts)
    if len(text) > max_length:
        text = text[: max_length - 3].rstrip() + "..."
    return text


def build_fallback_message(text: str, audio_url: str, language: str) -> str:
    note = t(language, "fallback_too_large", url=audio_url)
    if len(note) > MAX_MESSAGE_LENGTH:
        if len(audio_url) <= MAX_MESSAGE_LENGTH:
            return audio_url
        return audio_url[:MAX_MESSAGE_LENGTH]
    if not text:
        return note
    available = MAX_MESSAGE_LENGTH - len(note) - 2
    if available <= 3:
        return note
    if len(text) > available:
        text = text[: available - 3].rstrip() + "..."
    return f"{text}\n\n{note}"


def append_original_link(caption: str, audio_url: str, language: str) -> str:
    note = t(language, "original_label", url=audio_url)
    if len(note) > 1024:
        if len(audio_url) <= 1024:
            return audio_url
        return audio_url[:1024]
    if not caption:
        return note
    available = 1024 - len(note) - 2
    if available <= 3:
        return note
    if len(caption) > available:
        caption = caption[: available - 3].rstrip() + "..."
    return f"{caption}\n\n{note}"


def get_source_size_bytes(source) -> Optional[int]:
    try:
        fileno = source.fileno()
    except Exception:
        return None
    try:
        return os.fstat(fileno).st_size
    except OSError:
        return None


def parse_config_value(raw_value: str) -> Any:
    raw_value = raw_value.strip()
    if not raw_value:
        return ""
    if raw_value.lower() in ("true", "false", "null"):
        try:
            return json.loads(raw_value.lower())
        except json.JSONDecodeError:
            return raw_value
    if raw_value[0] in "[{\"-0123456789":
        try:
            return json.loads(raw_value)
        except json.JSONDecodeError:
            return raw_value
    return raw_value


def get_language(config: Dict[str, Any]) -> str:
    language = (config.get("language") or "").strip().lower()
    if language in SUPPORTED_LANGUAGES:
        return language
    return DEFAULT_LANGUAGE


def t(language: str, key: str, **kwargs: Any) -> str:
    catalog = MESSAGES.get(language) or MESSAGES[DEFAULT_LANGUAGE]
    template = catalog.get(key) or MESSAGES[DEFAULT_LANGUAGE].get(key, key)
    return template.format(**kwargs)


def normalize_admin_ids(value: Any) -> List[int]:
    if not isinstance(value, list):
        return []
    cleaned: List[int] = []
    for admin_id in value:
        try:
            cleaned.append(int(admin_id))
        except (TypeError, ValueError):
            continue
    return cleaned


def get_context_lock(context: ContextTypes.DEFAULT_TYPE, key: str) -> asyncio.Lock:
    lock = context.bot_data.get(key)
    if lock is None:
        lock = asyncio.Lock()
        context.bot_data[key] = lock
    return lock


async def call_telegram_once(label: str, call, *args, **kwargs):
    try:
        return await call(*args, **kwargs)
    except BadRequest:
        raise
    except (TimedOut, NetworkError) as exc:
        logging.warning(
            "%s failed with an ambiguous network error; not retrying to avoid duplicates: %s",
            label,
            exc,
        )
        raise


def build_thumbnail(image_url: str, max_bytes: int) -> Optional[str]:
    try:
        source_path = download_to_temp(
            image_url,
            ".img",
            max_bytes,
        )
    except AudioTooLargeError as exc:
        logging.warning(
            "Thumbnail source too large: %s. Trying direct compression.",
            exc,
        )
        try:
            return create_thumbnail(image_url)
        except Exception as fallback_exc:
            logging.warning(
                "Failed to create thumbnail from URL: %s",
                fallback_exc,
            )
            return None
    except requests.RequestException as exc:
        logging.warning("Failed to download thumbnail: %s", exc)
        return None

    try:
        return create_thumbnail(source_path)
    finally:
        try:
            os.remove(source_path)
        except OSError:
            pass


async def publish_entry(
    bot,
    channel: str,
    entry: Dict[str, Any],
    feed: Dict[str, Any],
    max_upload_bytes: int,
    max_source_bytes: int,
    transcode_enabled: bool,
    send_original_link: bool,
    audio_send_mode: str,
    max_thumb_source_bytes: int,
    language: str,
    status_callback: Optional[Callable[[str], Awaitable[None]]] = None,
) -> None:
    title = (entry.get("title") or "").strip()
    performer = (feed.get("title") or "").strip() or None
    audio_url = get_audio_url(entry)
    if not audio_url:
        raise RuntimeError("Audio URL not found for entry")

    base_caption = format_caption(entry)
    text = format_text(entry)
    thumb_path = None
    image_url = get_image_url(entry, feed)
    if image_url:
        try:
            thumb_path = await asyncio.to_thread(
                build_thumbnail,
                image_url,
                max_thumb_source_bytes,
            )
        except Exception as exc:
            logging.warning("Failed to prepare thumbnail: %s", exc)

    async def report_status(key: str, **kwargs: Any) -> None:
        if not status_callback:
            return
        try:
            await status_callback(t(language, key, **kwargs))
        except Exception as exc:
            logging.warning("Failed to send status message: %s", exc)

    async def send_audio_with(source, caption_text: Optional[str]) -> None:
        thumb_file = None
        size_bytes = get_source_size_bytes(source)
        try:
            if thumb_path:
                thumb_file = open(thumb_path, "rb")
            async def send_audio() -> None:
                if hasattr(source, "seek"):
                    source.seek(0)
                if thumb_file and hasattr(thumb_file, "seek"):
                    thumb_file.seek(0)
                start = time.monotonic()
                try:
                    await bot.send_audio(
                        chat_id=channel,
                        audio=source,
                        title=title or None,
                        performer=performer,
                        caption=caption_text or None,
                        thumbnail=thumb_file,
                    )
                except Exception as exc:
                    elapsed = time.monotonic() - start
                    if size_bytes:
                        logging.warning(
                            "send_audio failed after %.1fs (upload %.2f MB): %s",
                            elapsed,
                            size_bytes / (1024 * 1024),
                            exc,
                        )
                    else:
                        logging.warning(
                            "send_audio failed after %.1fs (source=url): %s",
                            elapsed,
                            exc,
                        )
                    raise
                else:
                    elapsed = time.monotonic() - start
                    if size_bytes:
                        logging.info(
                            "send_audio ok in %.1fs (upload %.2f MB).",
                            elapsed,
                            size_bytes / (1024 * 1024),
                        )
                    else:
                        logging.info("send_audio ok in %.1fs (source=url).", elapsed)

            await call_telegram_once("send_audio", send_audio)
        finally:
            if thumb_file:
                thumb_file.close()

    async def send_cover_message(caption_text: str) -> Optional[Any]:
        if not image_url:
            return None

        try:
            return await call_telegram_once(
                "send_photo",
                bot.send_photo,
                chat_id=channel,
                photo=image_url,
                caption=caption_text or None,
            )
        except BadRequest as exc:
            if not thumb_path:
                logging.warning("Failed to send cover image: %s", exc)
                return None
            logging.warning(
                "Failed to send cover image by URL: %s. Trying local thumbnail.",
                exc,
            )

        try:
            with open(thumb_path, "rb") as cover_file:
                async def send_local_cover() -> Any:
                    cover_file.seek(0)
                    return await bot.send_photo(
                        chat_id=channel,
                        photo=cover_file,
                        caption=caption_text or None,
                    )

                return await call_telegram_once("send_photo", send_local_cover)
        except (BadRequest, OSError) as exc:
            logging.warning("Failed to send local cover image: %s", exc)
            return None

    async def maybe_transcode(path: str) -> Optional[str]:
        if not transcode_enabled:
            return None
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if size and size <= max_upload_bytes:
            return None
        duration_seconds = await asyncio.to_thread(get_audio_duration_seconds, path)
        bitrate_kbps = choose_transcode_bitrate_kbps(
            duration_seconds,
            max_upload_bytes,
        )
        logging.info("Transcoding audio to %dkbps.", bitrate_kbps)
        await report_status("transcoding_audio", bitrate_kbps=bitrate_kbps)
        output_path = await asyncio.to_thread(transcode_audio, path, bitrate_kbps)
        try:
            output_size = os.path.getsize(output_path)
        except OSError:
            output_size = 0
        if output_size and output_size > max_upload_bytes:
            try:
                os.remove(output_path)
            except OSError:
                logging.warning(
                    "Failed to remove oversized transcoded file: %s",
                    output_path,
                )
            raise AudioTooLargeError(
                f"Transcoded file too large: {output_size} bytes"
            )
        return output_path

    audio_caption: Optional[str] = base_caption
    fallback_text = text

    async def upload_from_url() -> None:
        logging.info("Uploading audio file from URL.")
        await report_status("uploading_audio")
        try:
            audio_path = await asyncio.to_thread(
                download_to_temp,
                audio_url,
                ".mp3",
                max_source_bytes,
            )
        except AudioTooLargeError as too_large:
            logging.warning("Audio too large to upload: %s", too_large)
            await call_telegram_once(
                "send_message",
                bot.send_message,
                chat_id=channel,
                text=build_fallback_message(fallback_text, audio_url, language),
            )
        else:
            transcoded_path = None
            try:
                try:
                    transcoded_path = await maybe_transcode(audio_path)
                except Exception as exc:
                    logging.warning("Transcode failed: %s", exc)
                    await call_telegram_once(
                        "send_message",
                        bot.send_message,
                        chat_id=channel,
                        text=build_fallback_message(
                            fallback_text,
                            audio_url,
                            language,
                        ),
                    )
                    return
                caption = audio_caption
                if transcoded_path and send_original_link:
                    caption = append_original_link(
                        audio_caption or "",
                        audio_url,
                        language,
                    )
                path_to_send = transcoded_path or audio_path
                with open(path_to_send, "rb") as audio_file:
                    await send_audio_with(audio_file, caption)
            except NetworkError as net_exc:
                if "Request Entity Too Large" not in str(net_exc):
                    raise
                logging.warning("Telegram upload limit exceeded.")
                await call_telegram_once(
                    "send_message",
                    bot.send_message,
                    chat_id=channel,
                    text=build_fallback_message(
                        fallback_text,
                        audio_url,
                        language,
                    ),
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

    cover_message = None
    try:
        try:
            cover_message = await send_cover_message(base_caption)
        except (NetworkError, TimedOut) as exc:
            logging.warning("Failed to send cover image: %s", exc)
            cover_message = None

        if cover_message is not None:
            audio_caption = None
            fallback_text = ""

        if audio_send_mode == "upload":
            await upload_from_url()
            return

        try:
            await send_audio_with(audio_url, audio_caption)
        except BadRequest as exc:
            error_message = str(exc).lower()
            too_large = "request entity too large" in error_message
            if audio_send_mode == "url" and not too_large:
                raise
            if not too_large and not should_fallback_to_download(exc):
                raise
            logging.info("Falling back to upload audio file from URL.")
            await upload_from_url()
    except Exception:
        message_id = getattr(cover_message, "message_id", None)
        if message_id is not None:
            try:
                await call_telegram_once(
                    "delete_orphaned_cover",
                    bot.delete_message,
                    chat_id=channel,
                    message_id=message_id,
                )
            except Exception as cleanup_exc:
                logging.warning(
                    "Failed to delete orphaned cover message %s: %s",
                    message_id,
                    cleanup_exc,
                )
        raise
    finally:
        if thumb_path:
            try:
                os.remove(thumb_path)
            except OSError:
                logging.warning("Failed to remove temp thumbnail: %s", thumb_path)


async def publish_new_entries(
    bot,
    config: Dict[str, Any],
    config_path: str,
    process_all: bool = False,
    status_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    publish_lock: Optional[asyncio.Lock] = None,
    config_lock: Optional[asyncio.Lock] = None,
) -> int:
    if publish_lock is not None:
        async with publish_lock:
            return await _publish_new_entries(
                bot,
                config,
                config_path,
                process_all,
                status_callback,
                config_lock,
            )
    return await _publish_new_entries(
        bot,
        config,
        config_path,
        process_all,
        status_callback,
        config_lock,
    )


async def _publish_new_entries(
    bot,
    config: Dict[str, Any],
    config_path: str,
    process_all: bool,
    status_callback: Optional[Callable[[str], Awaitable[None]]],
    config_lock: Optional[asyncio.Lock],
) -> int:
    if config_lock is not None:
        async with config_lock:
            config = load_config(config_path)

    rss_url = config["rss_url"]
    language = get_language(config)
    max_upload_bytes = get_max_upload_bytes(config)
    max_source_bytes = get_max_source_bytes(config)
    max_thumb_source_bytes = get_max_thumb_source_bytes(config)
    transcode_enabled = parse_bool(config.get("transcode_enabled"), False)
    send_original_link = get_send_original_link(config)
    audio_send_mode = get_audio_send_mode(config)
    delay_seconds = get_process_all_delay_seconds(config) if process_all else 0
    parsed = await asyncio.to_thread(feedparser.parse, rss_url)
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
    process_all_last_id = (config.get("process_all_last_id") or "").strip()
    found_last = False
    new_entries: List[Dict[str, Any]] = []

    if process_all:
        ordered = list(reversed(entries))
        if process_all_last_id:
            resumed = False
            for entry in ordered:
                entry_id = get_entry_id(entry)
                if not resumed:
                    if entry_id == process_all_last_id:
                        resumed = True
                    continue
                new_entries.append(entry)
            if not resumed:
                logging.warning(
                    "process_all_last_id not found; starting from oldest."
                )
                new_entries = ordered
        else:
            new_entries = ordered
    else:
        for entry in entries:
            entry_id = get_entry_id(entry)
            if last_id and entry_id == last_id:
                found_last = True
                break
            new_entries.append(entry)

        if last_id and not found_last:
            logging.warning(
                "Last published id not found; posting only the latest entry."
            )
            new_entries = entries[:1]
        elif not last_id:
            new_entries = entries[:1]

    if not new_entries:
        return 0

    if not process_all:
        new_entries = list(reversed(new_entries))
        max_items = parse_non_negative_int(config.get("max_items_per_run"), 0)
        if max_items > 0 and len(new_entries) > max_items:
            new_entries = new_entries[:max_items]

    async def save_progress(entry_id: str) -> None:
        nonlocal config
        if config_lock is not None:
            async with config_lock:
                latest_config = load_config(config_path)
                latest_config["last_published_id"] = entry_id
                if process_all:
                    latest_config["process_all_last_id"] = entry_id
                save_config(config_path, latest_config)
            config = latest_config
            return

        config["last_published_id"] = entry_id
        if process_all:
            config["process_all_last_id"] = entry_id
        save_config(config_path, config)

    for index, entry in enumerate(new_entries):
        await publish_entry(
            bot,
            config["channel"],
            entry,
            parsed.feed,
            max_upload_bytes,
            max_source_bytes,
            transcode_enabled,
            send_original_link,
            audio_send_mode,
            max_thumb_source_bytes,
            language,
            status_callback,
        )
        await save_progress(get_entry_id(entry))
        if process_all and delay_seconds > 0 and index < len(new_entries) - 1:
            logging.info("Sleeping %s seconds before next entry.", delay_seconds)
            await asyncio.sleep(delay_seconds)

    return len(new_entries)


async def safe_reply(update: Update, text: str) -> None:
    message = update.effective_message
    if message:
        await message.reply_text(text)


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config_path = context.bot_data["config_path"]
    try:
        config = load_config(config_path)
    except Exception:
        config = {"language": DEFAULT_LANGUAGE}
    language = get_language(config)
    user = update.effective_user
    if not user:
        await safe_reply(update, t(language, "id_not_found"))
        return
    await safe_reply(update, t(language, "myid_reply", user_id=user.id))


async def new_podcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config_path = context.bot_data["config_path"]
    try:
        config = load_config(config_path)
    except Exception as exc:
        logging.exception("Failed to load config")
        await safe_reply(update, t(DEFAULT_LANGUAGE, "config_read_error", error=exc))
        return

    language = get_language(config)
    if not config.get("channel"):
        await safe_reply(update, t(language, "config_missing_channel"))
        return
    if not config.get("rss_url"):
        await safe_reply(update, t(language, "config_missing_rss_url"))
        return

    user_id = update.effective_user.id if update.effective_user else None
    if user_id not in config.get("admin_ids", []):
        await safe_reply(update, t(language, "no_access"))
        return

    publish_lock = get_context_lock(context, PUBLISH_LOCK_KEY)
    config_lock = get_context_lock(context, CONFIG_LOCK_KEY)
    if publish_lock.locked():
        await safe_reply(update, t(language, "publishing_in_progress"))
        return

    await safe_reply(update, t(language, "searching_new"))
    try:
        count = await publish_new_entries(
            context.bot,
            config,
            config_path,
            process_all=False,
            status_callback=lambda text: safe_reply(update, text),
            publish_lock=publish_lock,
            config_lock=config_lock,
        )
    except Exception as exc:
        logging.exception("Failed to publish new entries")
        await safe_reply(update, t(language, "error_generic", error=exc))
        return

    await safe_reply(update, t(language, "done_published", count=count))


async def process_all_podcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config_path = context.bot_data["config_path"]
    try:
        config = load_config(config_path)
    except Exception as exc:
        logging.exception("Failed to load config")
        await safe_reply(update, t(DEFAULT_LANGUAGE, "config_read_error", error=exc))
        return

    language = get_language(config)
    if not config.get("channel"):
        await safe_reply(update, t(language, "config_missing_channel"))
        return
    if not config.get("rss_url"):
        await safe_reply(update, t(language, "config_missing_rss_url"))
        return

    user_id = update.effective_user.id if update.effective_user else None
    if user_id not in config.get("admin_ids", []):
        await safe_reply(update, t(language, "no_access"))
        return

    publish_lock = get_context_lock(context, PUBLISH_LOCK_KEY)
    config_lock = get_context_lock(context, CONFIG_LOCK_KEY)
    if publish_lock.locked():
        await safe_reply(update, t(language, "publishing_in_progress"))
        return

    await publish_lock.acquire()
    try:
        await safe_reply(update, t(language, "processing_all"))
    except Exception:
        publish_lock.release()
        raise

    async def run_in_background() -> None:
        try:
            count = await publish_new_entries(
                context.bot,
                config,
                config_path,
                process_all=True,
                status_callback=lambda text: safe_reply(update, text),
                config_lock=config_lock,
            )
        except Exception as exc:
            logging.exception("Failed to publish all entries")
            await safe_reply(update, t(language, "error_generic", error=exc))
        else:
            await safe_reply(update, t(language, "done_published", count=count))
        finally:
            publish_lock.release()

    background_coro = run_in_background()
    try:
        context.application.create_task(
            background_coro,
            update=update,
            name="process_all_podcast",
        )
    except Exception:
        background_coro.close()
        publish_lock.release()
        raise


async def reset_process_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config_path = context.bot_data["config_path"]
    config_lock = get_context_lock(context, CONFIG_LOCK_KEY)
    user_id = update.effective_user.id if update.effective_user else None
    try:
        async with config_lock:
            config = load_config(config_path)
            language = get_language(config)
            if user_id not in config.get("admin_ids", []):
                reply_text = t(language, "no_access")
            else:
                config["process_all_last_id"] = ""
                save_config(config_path, config)
                reply_text = t(language, "process_all_reset")
    except Exception as exc:
        logging.exception("Failed to load config")
        reply_text = t(DEFAULT_LANGUAGE, "config_read_error", error=exc)
    await safe_reply(update, reply_text)


async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config_path = context.bot_data["config_path"]
    config_lock = get_context_lock(context, CONFIG_LOCK_KEY)
    user_id = update.effective_user.id if update.effective_user else None
    try:
        async with config_lock:
            config = load_config(config_path)
            language = get_language(config)
            if user_id not in config.get("admin_ids", []):
                reply_text = t(language, "no_access")
            elif not context.args:
                reply_text = t(language, "usage_add_admin")
            else:
                try:
                    new_admin_id = int(context.args[0])
                except ValueError:
                    reply_text = t(language, "invalid_user_id")
                else:
                    admin_ids = config.get("admin_ids", [])
                    if new_admin_id in admin_ids:
                        reply_text = t(language, "admin_already")
                    else:
                        admin_ids.append(new_admin_id)
                        config["admin_ids"] = admin_ids
                        save_config(config_path, config)
                        reply_text = t(
                            language,
                            "admin_added",
                            user_id=new_admin_id,
                        )
    except Exception as exc:
        logging.exception("Failed to load config")
        reply_text = t(DEFAULT_LANGUAGE, "config_read_error", error=exc)
    await safe_reply(update, reply_text)


async def remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config_path = context.bot_data["config_path"]
    config_lock = get_context_lock(context, CONFIG_LOCK_KEY)
    user_id = update.effective_user.id if update.effective_user else None
    try:
        async with config_lock:
            config = load_config(config_path)
            language = get_language(config)
            if user_id not in config.get("admin_ids", []):
                reply_text = t(language, "no_access")
            elif not context.args:
                reply_text = t(language, "usage_remove_admin")
            else:
                try:
                    remove_id = int(context.args[0])
                except ValueError:
                    reply_text = t(language, "invalid_user_id")
                else:
                    admin_ids = config.get("admin_ids", [])
                    if remove_id not in admin_ids:
                        reply_text = t(language, "admin_missing")
                    elif len(admin_ids) <= 1:
                        reply_text = t(language, "admin_remove_last")
                    else:
                        config["admin_ids"] = [
                            admin_id
                            for admin_id in admin_ids
                            if admin_id != remove_id
                        ]
                        save_config(config_path, config)
                        reply_text = t(
                            language,
                            "admin_removed",
                            user_id=remove_id,
                        )
    except Exception as exc:
        logging.exception("Failed to load config")
        reply_text = t(DEFAULT_LANGUAGE, "config_read_error", error=exc)
    await safe_reply(update, reply_text)


async def update_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config_path = context.bot_data["config_path"]
    config_lock = get_context_lock(context, CONFIG_LOCK_KEY)
    user_id = update.effective_user.id if update.effective_user else None
    try:
        async with config_lock:
            config = load_config(config_path)
            language = get_language(config)
            if user_id not in config.get("admin_ids", []):
                reply_text = t(language, "no_access")
            else:
                raw = " ".join(context.args).strip()
                if ":" not in raw:
                    reply_text = t(language, "usage_update_config")
                else:
                    key, value_raw = raw.split(":", 1)
                    key = key.strip()
                    if not key:
                        reply_text = t(language, "config_key_missing")
                    else:
                        value = parse_config_value(value_raw)
                        if key == "admin_ids" and not isinstance(value, list):
                            reply_text = t(language, "admin_ids_list_required")
                        else:
                            if key == "admin_ids":
                                value = normalize_admin_ids(value)
                            if key == "admin_ids" and not value:
                                reply_text = t(language, "admin_remove_last")
                            else:
                                config[key] = value
                                save_config(config_path, config)
                                if key == "bot_token":
                                    reply_text = t(language, "bot_token_updated")
                                else:
                                    reply_text = t(
                                        language,
                                        "config_updated",
                                        key=key,
                                    )
    except Exception as exc:
        logging.exception("Failed to load config")
        reply_text = t(DEFAULT_LANGUAGE, "config_read_error", error=exc)
    await safe_reply(update, reply_text)


async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config_path = context.bot_data["config_path"]
    config_lock = get_context_lock(context, CONFIG_LOCK_KEY)
    user_id = update.effective_user.id if update.effective_user else None
    try:
        async with config_lock:
            config = load_config(config_path)
            language = get_language(config)
            if user_id not in config.get("admin_ids", []):
                reply_text = t(language, "no_access")
            elif not context.args:
                reply_text = t(language, "usage_set_language")
            else:
                new_language = context.args[0].strip().lower()
                if new_language not in SUPPORTED_LANGUAGES:
                    reply_text = t(
                        language,
                        "language_unsupported",
                        language=new_language,
                    )
                else:
                    config["language"] = new_language
                    save_config(config_path, config)
                    reply_text = t(
                        new_language,
                        "language_set",
                        language=new_language,
                    )
    except Exception as exc:
        logging.exception("Failed to load config")
        reply_text = t(DEFAULT_LANGUAGE, "config_read_error", error=exc)
    await safe_reply(update, reply_text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config_path = context.bot_data["config_path"]
    try:
        config = load_config(config_path)
    except Exception:
        config = {"admin_ids": []}

    user_id = update.effective_user.id if update.effective_user else None
    is_admin = user_id in config.get("admin_ids", [])

    language = get_language(config)
    lines = [t(language, "help_title"), t(language, "help_myid"), t(language, "help_help")]
    if is_admin:
        lines = [
            t(language, "help_title"),
            t(language, "help_myid"),
            t(language, "help_new"),
            t(language, "help_all"),
            t(language, "help_reset"),
            t(language, "help_add_admin"),
            t(language, "help_remove_admin"),
            t(language, "help_update"),
            t(language, "help_set_language"),
            t(language, "help_help"),
        ]

    await safe_reply(update, "\n".join(lines))


async def auto_check_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    config_path = context.job.data["config_path"]
    try:
        config = load_config(config_path)
    except Exception as exc:
        logging.exception("Failed to load config for auto-check")
        return

    if not get_auto_check_enabled(config):
        return

    interval = get_auto_check_interval_seconds(config)
    current_interval = context.job.data.get("interval")
    if interval != current_interval and interval > 0:
        logging.info("Rescheduling auto-check to %s seconds.", interval)
        context.application.job_queue.run_repeating(
            auto_check_job,
            interval=interval,
            first=interval,
            data={"config_path": config_path, "interval": interval},
            name="auto_check",
        )
        context.job.schedule_removal()
        return

    if not config.get("channel") or not config.get("rss_url"):
        logging.warning("Auto-check skipped: channel or rss_url not set.")
        return

    publish_lock = get_context_lock(context, PUBLISH_LOCK_KEY)
    config_lock = get_context_lock(context, CONFIG_LOCK_KEY)
    if publish_lock.locked():
        logging.info("Auto-check skipped: another publication is in progress.")
        return

    try:
        count = await publish_new_entries(
            context.bot,
            config,
            config_path,
            process_all=False,
            publish_lock=publish_lock,
            config_lock=config_lock,
        )
        if count:
            logging.info("Auto-check published %s new entries.", count)
    except Exception:
        logging.exception("Auto-check failed")


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

    request = HTTPXRequest(
        connect_timeout=30,
        read_timeout=300,
        write_timeout=300,
        pool_timeout=30,
    )
    application = ApplicationBuilder().token(token).request(request).build()
    application.bot_data["config_path"] = config_path

    application.add_handler(CommandHandler("myid", myid))
    application.add_handler(CommandHandler("new_podcast", new_podcast))
    application.add_handler(CommandHandler("process_all_podcast", process_all_podcast))
    application.add_handler(CommandHandler("reset_process_all", reset_process_all))
    application.add_handler(CommandHandler("add_admin", add_admin))
    application.add_handler(CommandHandler("remove_admin", remove_admin))
    application.add_handler(CommandHandler("update_config", update_config))
    application.add_handler(CommandHandler("set_language", set_language))
    application.add_handler(CommandHandler("help", help_command))

    if get_auto_check_enabled(config):
        interval = get_auto_check_interval_seconds(config)
        application.job_queue.run_repeating(
            auto_check_job,
            interval=interval,
            first=interval,
            data={"config_path": config_path, "interval": interval},
            name="auto_check",
        )
        logging.info("Auto-check enabled: every %s seconds.", interval)

    application.run_polling()


if __name__ == "__main__":
    main()
