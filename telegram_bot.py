"""High-performance Telegram audio/media bot.

Architecture:
  1. Direct yt-dlp with concurrent_fragment_downloads (multi-threaded)
  2. Format 140 (M4A/AAC) — zero transcoding, direct stream copy
  3. SQLite file_id cache — instant re-delivery for previously downloaded tracks
  4. Fully async via aiogram 3.x
  5. Pyrogram MTProto uploader for files > 50 MB (up to 2 GB)
  6. Non-YouTube platforms proxied through FastAPI backend
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

import aiohttp
import aiosqlite
import yt_dlp
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)
from aiogram.filters import Command
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")

# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN or BOT_TOKEN == "your_bot_token_here":
    raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env")

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000/api/v1")
DB_PATH = os.getenv("CACHE_DB", "file_cache.db")
MAX_CONCURRENT = int(os.getenv("BOT_WORKERS", "4"))
FRAGMENT_THREADS = int(os.getenv("FRAGMENT_THREADS", "8"))
CAPTION_DEFAULT = os.getenv("CAPTION_DEFAULT", "true").lower() == "true"

TG_API_ID = os.getenv("TG_API_ID", "")
TG_API_HASH = os.getenv("TG_API_HASH", "")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

_dl_sem = asyncio.Semaphore(MAX_CONCURRENT)
_pending: dict[str, dict] = {}

TG_BOT_API_LIMIT = 49 * 1024 * 1024   # 49 MB — aiogram / Bot API ceiling
TG_MTPROTO_LIMIT = 2000 * 1024 * 1024  # ~2 GB — Pyrogram / MTProto ceiling

# ── Pyrogram MTProto Client (optional, for large files) ──────────────────────

_pyro_client = None  # initialized at startup if credentials exist


async def _init_pyrogram():
    global _pyro_client
    if not TG_API_ID or not TG_API_HASH:
        log.info("Pyrogram disabled — set TG_API_ID + TG_API_HASH in .env to upload files > 50 MB")
        return

    try:
        from pyrogram import Client
        _pyro_client = Client(
            "anygrab_bot",
            api_id=int(TG_API_ID),
            api_hash=TG_API_HASH,
            bot_token=BOT_TOKEN,
            workdir=tempfile.gettempdir(),
            no_updates=True,
        )
        await _pyro_client.start()
        me = await _pyro_client.get_me()
        log.info("Pyrogram ready — @%s — large uploads enabled (up to 2 GB)", me.username)
    except Exception as e:
        log.warning("Pyrogram init failed: %s — large uploads disabled", e)
        _pyro_client = None


def _can_use_pyrogram() -> bool:
    return _pyro_client is not None


async def _pyro_send_audio(
    chat_id: int,
    filepath: Path,
    title: str,
    performer: Optional[str],
    duration: int,
    thumb_path: Optional[Path],
    caption: Optional[str],
    reply_to: int,
) -> Optional[str]:
    """Upload audio via Pyrogram MTProto. Returns file_id on success."""
    msg = await _pyro_client.send_audio(
        chat_id,
        audio=str(filepath),
        title=title,
        performer=performer,
        duration=duration,
        thumb=str(thumb_path) if thumb_path else None,
        caption=caption,
        reply_to_message_id=reply_to,
    )
    return msg.audio.file_id if msg and msg.audio else None


async def _pyro_send_video(
    chat_id: int,
    filepath: Path,
    title: str,
    duration: int,
    thumb_path: Optional[Path],
    caption: Optional[str],
    reply_to: int,
) -> Optional[str]:
    """Upload video via Pyrogram MTProto. Returns file_id on success."""
    msg = await _pyro_client.send_video(
        chat_id,
        video=str(filepath),
        caption=caption,
        duration=duration,
        thumb=str(thumb_path) if thumb_path else None,
        supports_streaming=True,
        reply_to_message_id=reply_to,
    )
    return msg.video.file_id if msg and msg.video else None


async def _pyro_send_document(
    chat_id: int,
    filepath: Path,
    caption: Optional[str],
    thumb_path: Optional[Path],
    reply_to: int,
) -> None:
    """Upload any file as document via Pyrogram MTProto."""
    await _pyro_client.send_document(
        chat_id,
        document=str(filepath),
        caption=caption,
        thumb=str(thumb_path) if thumb_path else None,
        reply_to_message_id=reply_to,
    )


async def _save_thumb_to_file(thumb_data: Optional[bytes], tmp_dir: Path) -> Optional[Path]:
    """Write thumbnail bytes to a temp file for Pyrogram (which needs a file path)."""
    if not thumb_data:
        return None
    p = tmp_dir / "thumb.jpg"
    p.write_bytes(thumb_data)
    return p


# ── SQLite File-ID Cache ──────────────────────────────────────────────────────

_db: aiosqlite.Connection = None  # type: ignore[assignment]


async def _init_db():
    global _db
    _db = await aiosqlite.connect(DB_PATH)
    await _db.execute("""
        CREATE TABLE IF NOT EXISTS file_cache (
            video_id   TEXT NOT NULL,
            format     TEXT NOT NULL,
            file_id    TEXT NOT NULL,
            title      TEXT,
            performer  TEXT,
            duration   INTEGER,
            cached_at  REAL NOT NULL,
            PRIMARY KEY (video_id, format)
        )
    """)
    await _db.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id    INTEGER PRIMARY KEY,
            captions   INTEGER NOT NULL DEFAULT 1
        )
    """)
    await _db.commit()
    log.info("Cache DB ready (%s)", DB_PATH)


async def _get_cached(video_id: str, fmt: str = "audio") -> Optional[dict]:
    async with _db.execute(
        "SELECT file_id, title, performer, duration FROM file_cache WHERE video_id=? AND format=?",
        (video_id, fmt),
    ) as cur:
        row = await cur.fetchone()
    if row:
        return {"file_id": row[0], "title": row[1], "performer": row[2], "duration": row[3]}
    return None


async def _set_cached(video_id: str, fmt: str, file_id: str, **kw):
    await _db.execute(
        "INSERT OR REPLACE INTO file_cache"
        "(video_id,format,file_id,title,performer,duration,cached_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (video_id, fmt, file_id, kw.get("title"), kw.get("performer"), kw.get("duration"), time.time()),
    )
    await _db.commit()


async def _user_wants_caption(user_id: int) -> bool:
    async with _db.execute(
        "SELECT captions FROM user_settings WHERE user_id=?", (user_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return CAPTION_DEFAULT
    return bool(row[0])


async def _toggle_caption(user_id: int) -> bool:
    current = await _user_wants_caption(user_id)
    new_val = 0 if current else 1
    await _db.execute(
        "INSERT OR REPLACE INTO user_settings(user_id, captions) VALUES(?,?)",
        (user_id, new_val),
    )
    await _db.commit()
    return bool(new_val)


# ── URL Helpers ───────────────────────────────────────────────────────────────

_URL_RE = re.compile(r"(https?://\S+)")
_YT_RE = re.compile(r"(youtube\.com|youtu\.be)")
_YT_ID_RE = re.compile(r"(?:v=|/shorts/|youtu\.be/)([a-zA-Z0-9_-]{11})")


def _extract_url(text: str) -> Optional[str]:
    m = _URL_RE.search(text)
    return m.group(1) if m else None


def _is_youtube(url: str) -> bool:
    return bool(_YT_RE.search(url))


def _yt_id(url: str) -> Optional[str]:
    m = _YT_ID_RE.search(url)
    return m.group(1) if m else None


def _file_size_label(nbytes: int) -> str:
    if nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.0f} KB"
    return f"{nbytes / 1_048_576:.1f} MB"


# ── yt-dlp Direct Integration ────────────────────────────────────────────────

def _base_opts() -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if os.path.exists("cookies.txt"):
        opts["cookiefile"] = "cookies.txt"
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        opts["impersonate"] = ImpersonateTarget(client="chrome")
    except Exception:
        pass
    return opts


def _audio_opts(out: str) -> dict:
    return {
        **_base_opts(),
        "format": "140/bestaudio[ext=m4a]/bestaudio",
        "outtmpl": out,
        "concurrent_fragment_downloads": FRAGMENT_THREADS,
    }


def _mp3_opts(out: str, quality: str = "320") -> dict:
    return {
        **_base_opts(),
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": out,
        "concurrent_fragment_downloads": FRAGMENT_THREADS,
        "writethumbnail": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": quality},
            {"key": "EmbedThumbnail"},
            {"key": "FFmpegMetadata"},
        ],
    }


def _video_opts(out: str) -> dict:
    return {
        **_base_opts(),
        "format": (
            "bestvideo[ext=mp4][vcodec^=avc1]+140"
            "/bestvideo[ext=mp4]+bestaudio[ext=m4a]"
            "/best[ext=mp4]/best"
        ),
        "merge_output_format": "mp4",
        "outtmpl": out,
        "concurrent_fragment_downloads": FRAGMENT_THREADS,
    }


def _pick_mp3_bitrate(duration_secs: int, size_limit: int) -> str:
    if duration_secs <= 0:
        return "320"
    max_kbps = int(size_limit * 8 / duration_secs / 1000)
    for br in (320, 256, 192, 128, 96, 64):
        if br <= max_kbps:
            return str(br)
    return "64"


async def _yt_download(url: str, mode: str = "audio", mp3_quality: str = "320") -> tuple[Optional[Path], dict]:
    tmp = Path(tempfile.mkdtemp(prefix="ytbot_"))
    out = str(tmp / "%(id)s.%(ext)s")

    if mode == "mp3":
        opts = _mp3_opts(out, quality=mp3_quality)
    elif mode == "audio":
        opts = _audio_opts(out)
    else:
        opts = _video_opts(out)

    def _run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)

    info = await asyncio.to_thread(_run)
    files = sorted(tmp.iterdir(), key=lambda f: f.stat().st_size, reverse=True)

    if mode == "mp3":
        mp3s = [f for f in files if f.suffix == ".mp3"]
        return (mp3s[0] if mp3s else (files[0] if files else None)), info
    elif mode == "audio":
        audio_files = [f for f in files if f.suffix in (".m4a", ".webm", ".opus", ".ogg")]
        return (audio_files[0] if audio_files else (files[0] if files else None)), info
    else:
        mp4s = [f for f in files if f.suffix == ".mp4"]
        return (mp4s[0] if mp4s else (files[0] if files else None)), info


async def _yt_extract_info(url: str) -> dict:
    opts = {**_base_opts(), "skip_download": True}

    def _run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    return await asyncio.to_thread(_run)


async def _fetch_thumb(url: str) -> Optional[bytes]:
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    data = await r.read()
                    return data if len(data) > 500 else None
    except Exception:
        return None


# ── Upload Helper (auto-selects aiogram vs Pyrogram) ─────────────────────────

async def _upload_audio(
    chat_id: int,
    filepath: Path,
    title: str,
    performer: Optional[str],
    duration: int,
    thumb_data: Optional[bytes],
    reply_to: int,
    tmp_dir: Path,
) -> Optional[str]:
    """Send audio file. Uses Pyrogram for large files. Returns file_id."""
    file_size = filepath.stat().st_size
    size_label = _file_size_label(file_size)

    if file_size <= TG_BOT_API_LIMIT:
        audio_file = FSInputFile(filepath, filename=f"{title}{filepath.suffix}")
        thumb_file = BufferedInputFile(thumb_data, "thumb.jpg") if thumb_data else None
        timeout = max(300, file_size // (100 * 1024))
        sent = await bot.send_audio(
            chat_id, audio=audio_file,
            title=title, performer=performer, duration=duration,
            thumbnail=thumb_file, reply_to_message_id=reply_to,
            request_timeout=timeout,
        )
        log.info("SENT via Bot API  %s  %s", title[:30], size_label)
        return sent.audio.file_id if sent.audio else None

    if not _can_use_pyrogram():
        raise ValueError(
            f"File is {size_label} — exceeds Telegram's 50 MB Bot API limit.\n"
            "Large uploads require Pyrogram. Set TG_API_ID + TG_API_HASH in .env\n"
            "(get them free at https://my.telegram.org)"
        )

    if file_size > TG_MTPROTO_LIMIT:
        raise ValueError(f"File is {size_label} — exceeds Telegram's 2 GB absolute limit.")

    thumb_path = await _save_thumb_to_file(thumb_data, tmp_dir)
    file_id = await _pyro_send_audio(
        chat_id, filepath, title, performer, duration, thumb_path, None, reply_to,
    )
    log.info("SENT via Pyrogram  %s  %s", title[:30], size_label)
    return file_id


async def _upload_video(
    chat_id: int,
    filepath: Path,
    title: str,
    duration: int,
    thumb_data: Optional[bytes],
    caption: Optional[str],
    reply_to: int,
    tmp_dir: Path,
) -> Optional[str]:
    """Send video file. Uses Pyrogram for large files. Returns file_id."""
    file_size = filepath.stat().st_size
    size_label = _file_size_label(file_size)

    if file_size <= TG_BOT_API_LIMIT:
        thumb_file = BufferedInputFile(thumb_data, "thumb.jpg") if thumb_data else None
        timeout = max(300, file_size // (100 * 1024))
        sent = await bot.send_video(
            chat_id,
            video=FSInputFile(filepath, filename=f"{title}.mp4"),
            caption=caption, duration=duration,
            thumbnail=thumb_file, supports_streaming=True,
            reply_to_message_id=reply_to,
            request_timeout=timeout,
        )
        log.info("SENT via Bot API  %s  %s", title[:30], size_label)
        return sent.video.file_id if sent.video else None

    if not _can_use_pyrogram():
        raise ValueError(
            f"Video is {size_label} — exceeds Telegram's 50 MB Bot API limit.\n"
            "Large uploads require Pyrogram. Set TG_API_ID + TG_API_HASH in .env\n"
            "(get them free at https://my.telegram.org)"
        )

    if file_size > TG_MTPROTO_LIMIT:
        raise ValueError(f"Video is {size_label} — exceeds Telegram's 2 GB absolute limit.")

    thumb_path = await _save_thumb_to_file(thumb_data, tmp_dir)
    file_id = await _pyro_send_video(
        chat_id, filepath, title, duration, thumb_path, caption, reply_to,
    )
    log.info("SENT via Pyrogram  %s  %s", title[:30], size_label)
    return file_id


# ── Handlers ──────────────────────────────────────────────────────────────────

@router.message(Command("start", "help"))
async def cmd_start(msg: Message):
    pyro_status = "✅ Large files up to 2 GB" if _can_use_pyrogram() else "⚠️ Limited to 50 MB (set TG_API_ID/HASH for 2 GB)"
    await msg.answer(
        "👋 <b>AnyGrab Downloader</b>\n\n"
        "Paste a link from YouTube, TikTok, Instagram, X and more.\n\n"
        "🎵 YouTube audio: <b>M4A</b> (fast) or <b>MP3 320kbps</b>\n"
        f"📦 Upload limit: {pyro_status}\n"
        "🚀 Just send a link!\n\n"
        "⚙️ /caption — toggle captions on/off"
    )


@router.message(Command("caption"))
async def cmd_caption(msg: Message):
    new_state = await _toggle_caption(msg.from_user.id)
    emoji = "✅" if new_state else "❌"
    await msg.answer(f"{emoji} Captions are now <b>{'ON' if new_state else 'OFF'}</b> for you.")


@router.message(F.text)
async def on_text(msg: Message):
    text = msg.text or ""

    if msg.chat.type in ("group", "supergroup"):
        me = await bot.get_me()
        if f"@{me.username}" not in text:
            return

    url = _extract_url(text)
    if not url:
        return

    if _is_youtube(url):
        key = os.urandom(4).hex()
        _pending[key] = {"url": url, "msg_id": msg.message_id, "user_id": msg.from_user.id}
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🎬 Video", callback_data=f"v|{key}"),
                InlineKeyboardButton(text="🎵 MP3", callback_data=f"m|{key}"),
            ],
            [
                InlineKeyboardButton(text="⚡ M4A (fast)", callback_data=f"a|{key}"),
            ],
        ])
        await msg.reply("🎶 YouTube detected — choose format:", reply_markup=kb)
    else:
        await _handle_other_platform(msg, url)


@router.callback_query(F.data.startswith(("v|", "a|", "m|")))
async def on_yt_choice(cb: CallbackQuery):
    choice, key = cb.data.split("|", 1)
    stored = _pending.pop(key, None)

    if not stored:
        await cb.answer("⏰ Expired — please send the link again.", show_alert=True)
        return

    labels = {"a": "⚡ M4A…", "m": "🎵 MP3…", "v": "🎬 Video…"}
    await cb.answer(labels.get(choice, "Downloading…"))
    status = await cb.message.edit_text("⏳ Processing…")

    url = stored["url"]
    reply_to = stored["msg_id"]
    user_id = stored.get("user_id") or cb.from_user.id

    if choice == "a":
        await _send_yt_audio(cb.message, url, reply_to, status, user_id)
    elif choice == "m":
        await _send_yt_mp3(cb.message, url, reply_to, status, user_id)
    else:
        await _send_yt_video(cb.message, url, reply_to, status, user_id)


@router.callback_query()
async def fallback_cb(cb: CallbackQuery):
    await cb.answer("❌ Unhandled action.")


# ── YouTube Audio (M4A fast path) ─────────────────────────────────────────────

async def _send_yt_audio(msg: Message, url: str, reply_to: int, status: Message, user_id: int = 0):
    vid = _yt_id(url)
    t0 = time.perf_counter()

    if vid:
        cached = await _get_cached(vid, "audio")
        if cached:
            log.info("CACHE HIT  %s  →  instant", vid)
            try:
                await bot.send_audio(msg.chat.id, audio=cached["file_id"], reply_to_message_id=reply_to)
                await status.delete()
                return
            except Exception:
                log.warning("Stale cache for %s, re-downloading", vid)

    async with _dl_sem:
        tmp_dir: Optional[Path] = None
        try:
            await status.edit_text("⬇️ Downloading audio…")
            filepath, info = await _yt_download(url, mode="audio")
            if not filepath or not filepath.exists() or filepath.stat().st_size < 1000:
                await status.edit_text("❌ Download failed or file too small.")
                return

            tmp_dir = filepath.parent
            title = (info.get("title") or "audio")[:64]
            performer = info.get("uploader") or info.get("artist")
            duration = int(info.get("duration") or 0)
            file_size = filepath.stat().st_size
            dl_sec = time.perf_counter() - t0
            log.info("DOWNLOADED  %s  %.1fs  %s  fmt=%s", vid, dl_sec, _file_size_label(file_size), info.get("format_id", "?"))

            thumb_task = asyncio.create_task(_fetch_thumb(info.get("thumbnail", "")))
            await status.edit_text(f"📤 Uploading ({_file_size_label(file_size)})…")
            thumb_data = await thumb_task

            file_id = await _upload_audio(
                msg.chat.id, filepath, title, performer, duration,
                thumb_data, reply_to, tmp_dir,
            )

            total = time.perf_counter() - t0
            log.info("SENT  %s  total=%.1fs", vid, total)

            if file_id and vid:
                await _set_cached(vid, "audio", file_id, title=title, performer=performer, duration=duration)
                log.info("CACHED  %s", vid)

            await status.delete()

        except Exception as e:
            log.error("Audio error: %s", e, exc_info=True)
            try:
                await status.edit_text(f"❌ {str(e)[:300]}")
            except Exception:
                pass
        finally:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)


# ── YouTube MP3 ───────────────────────────────────────────────────────────────

async def _send_yt_mp3(msg: Message, url: str, reply_to: int, status: Message, user_id: int = 0):
    vid = _yt_id(url)
    t0 = time.perf_counter()

    if vid:
        cached = await _get_cached(vid, "mp3")
        if cached:
            log.info("MP3 CACHE HIT  %s  →  instant", vid)
            try:
                await bot.send_audio(msg.chat.id, audio=cached["file_id"], reply_to_message_id=reply_to)
                await status.delete()
                return
            except Exception:
                log.warning("Stale mp3 cache for %s, re-downloading", vid)

    async with _dl_sem:
        tmp_dir: Optional[Path] = None
        try:
            size_limit = TG_MTPROTO_LIMIT if _can_use_pyrogram() else TG_BOT_API_LIMIT

            await status.edit_text("⬇️ Checking audio length…")
            info_pre = await _yt_extract_info(url)
            duration = int(info_pre.get("duration") or 0)
            quality = _pick_mp3_bitrate(duration, size_limit)
            log.info("MP3 plan  %s  dur=%ds  bitrate=%skbps  limit=%s", vid, duration, quality, _file_size_label(size_limit))

            await status.edit_text(f"⬇️ Downloading MP3 ({quality} kbps)…")
            filepath, info = await _yt_download(url, mode="mp3", mp3_quality=quality)
            if not filepath or not filepath.exists() or filepath.stat().st_size < 1000:
                await status.edit_text("❌ Download failed or file too small.")
                return

            tmp_dir = filepath.parent
            title = (info.get("title") or "audio")[:64]
            performer = info.get("uploader") or info.get("artist")
            duration = int(info.get("duration") or 0)
            file_size = filepath.stat().st_size
            dl_sec = time.perf_counter() - t0
            log.info("MP3 READY  %s  %.1fs  %s  %skbps", vid, dl_sec, _file_size_label(file_size), quality)

            thumb_task = asyncio.create_task(_fetch_thumb(info.get("thumbnail", "")))
            await status.edit_text(f"📤 Uploading ({_file_size_label(file_size)})…")
            thumb_data = await thumb_task

            file_id = await _upload_audio(
                msg.chat.id, filepath, title, performer, duration,
                thumb_data, reply_to, tmp_dir,
            )

            total = time.perf_counter() - t0
            log.info("MP3 SENT  %s  total=%.1fs", vid, total)

            if file_id and vid:
                await _set_cached(vid, "mp3", file_id, title=title, performer=performer, duration=duration)
                log.info("MP3 CACHED  %s", vid)

            await status.delete()

        except Exception as e:
            log.error("MP3 error: %s", e, exc_info=True)
            try:
                await status.edit_text(f"❌ {str(e)[:300]}")
            except Exception:
                pass
        finally:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)


# ── YouTube Video ─────────────────────────────────────────────────────────────

async def _send_yt_video(msg: Message, url: str, reply_to: int, status: Message, user_id: int = 0):
    vid = _yt_id(url)
    t0 = time.perf_counter()

    if vid:
        cached = await _get_cached(vid, "video")
        if cached:
            log.info("VIDEO CACHE HIT  %s", vid)
            try:
                await bot.send_video(
                    msg.chat.id, video=cached["file_id"],
                    supports_streaming=True, reply_to_message_id=reply_to,
                )
                await status.delete()
                return
            except Exception:
                log.warning("Stale video cache for %s", vid)

    async with _dl_sem:
        tmp_dir: Optional[Path] = None
        try:
            await status.edit_text("⬇️ Downloading video…")
            filepath, info = await _yt_download(url, mode="video")
            if not filepath or not filepath.exists():
                await status.edit_text("❌ Download failed.")
                return

            tmp_dir = filepath.parent
            file_size = filepath.stat().st_size
            title = (info.get("title") or "video")[:64]
            duration = int(info.get("duration") or 0)
            dl_sec = time.perf_counter() - t0
            log.info("VIDEO DL  %s  %.1fs  %s", vid, dl_sec, _file_size_label(file_size))

            thumb_task = asyncio.create_task(_fetch_thumb(info.get("thumbnail", "")))
            await status.edit_text(f"📤 Uploading ({_file_size_label(file_size)})…")
            thumb_data = await thumb_task

            show_cap = await _user_wants_caption(user_id) if user_id else CAPTION_DEFAULT
            file_id = await _upload_video(
                msg.chat.id, filepath, title, duration,
                thumb_data, title if show_cap else None, reply_to, tmp_dir,
            )

            if file_id and vid:
                await _set_cached(vid, "video", file_id, title=title, duration=duration)

            await status.delete()

        except Exception as e:
            log.error("Video error: %s", e, exc_info=True)
            try:
                await status.edit_text(f"❌ {str(e)[:300]}")
            except Exception:
                pass
        finally:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)


# ── Non-YouTube (proxy through FastAPI backend) ──────────────────────────────

async def _handle_other_platform(msg: Message, url: str):
    status = await msg.reply("⏳ Processing…")
    try:
        show_cap = await _user_wants_caption(msg.from_user.id) if msg.from_user else CAPTION_DEFAULT
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{API_BASE_URL}/extract", json={"url": url},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                if r.status != 200:
                    detail = (await r.json()).get("detail", "Unknown error")
                    await status.edit_text(f"❌ {detail}")
                    return
                data = await r.json()

            media_urls = data.get("media_urls", [])
            caption = (data.get("caption") or "") if show_cap else ""
            headers = data.get("http_headers", {})

            if not media_urls:
                await status.edit_text("❌ No media found.")
                return
            if len(caption) > 1000:
                caption = caption[:997] + "…"

            await status.edit_text("⬇️ Downloading…")

            if len(media_urls) == 1:
                await _send_single_media(session, msg, status, media_urls[0], url, headers, caption)
            else:
                await _send_media_group(session, msg, status, media_urls, url, headers, caption)

    except Exception as e:
        log.error("Platform error: %s", e, exc_info=True)
        try:
            await status.edit_text(f"❌ {str(e)[:200]}")
        except Exception:
            pass


async def _send_single_media(
    session: aiohttp.ClientSession, msg: Message, status: Message,
    m_url: str, original_url: str, headers: dict, caption: str,
):
    async with session.post(
        f"{API_BASE_URL}/download",
        json={"url": m_url, "original_url": original_url, "headers": headers},
        timeout=aiohttp.ClientTimeout(total=300),
    ) as dl:
        if dl.status != 200:
            await status.edit_text("❌ Download failed.")
            return
        content = await dl.read()

    if not content or len(content) < 1000:
        await status.edit_text("❌ File too small.")
        return

    file_size = len(content)
    await status.edit_text(f"📤 Uploading ({_file_size_label(file_size)})…")
    is_img = any(ext in m_url.lower() for ext in (".jpg", ".jpeg", ".png", ".webp"))

    if file_size <= TG_BOT_API_LIMIT or not _can_use_pyrogram():
        if is_img:
            await bot.send_photo(
                msg.chat.id, BufferedInputFile(content, "photo.jpg"),
                caption=caption, reply_to_message_id=msg.message_id,
            )
        else:
            await bot.send_video(
                msg.chat.id, BufferedInputFile(content, "video.mp4"),
                caption=caption, supports_streaming=True,
                reply_to_message_id=msg.message_id,
            )
    else:
        tmp = Path(tempfile.mkdtemp(prefix="ytbot_"))
        try:
            ext = "jpg" if is_img else "mp4"
            fpath = tmp / f"media.{ext}"
            fpath.write_bytes(content)
            if is_img:
                await _pyro_client.send_photo(
                    msg.chat.id, photo=str(fpath),
                    caption=caption, reply_to_message_id=msg.message_id,
                )
            else:
                await _pyro_send_video(
                    msg.chat.id, fpath, "video", 0, None, caption, msg.message_id,
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    await status.delete()


async def _send_media_group(
    session: aiohttp.ClientSession, msg: Message, status: Message,
    media_urls: list, original_url: str, headers: dict, caption: str,
):
    tasks = [_fetch_backend_media(session, m, original_url, headers) for m in media_urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    media_group: list = []
    for i, (m_url, result) in enumerate(zip(media_urls, results)):
        if isinstance(result, Exception) or not result:
            continue
        is_img = any(ext in m_url.lower() for ext in (".jpg", ".jpeg", ".png", ".webp"))
        cap = caption if i == 0 else None
        inp = BufferedInputFile(result, f"media_{i}.{'jpg' if is_img else 'mp4'}")

        if is_img:
            media_group.append(InputMediaPhoto(media=inp, caption=cap))
        else:
            media_group.append(InputMediaVideo(media=inp, caption=cap))

        if len(media_group) == 10:
            await bot.send_media_group(msg.chat.id, media_group, reply_to_message_id=msg.message_id)
            media_group = []

    if media_group:
        await bot.send_media_group(msg.chat.id, media_group, reply_to_message_id=msg.message_id)

    await status.delete()


async def _fetch_backend_media(
    session: aiohttp.ClientSession, m_url: str, original_url: str, headers: dict,
) -> Optional[bytes]:
    try:
        async with session.post(
            f"{API_BASE_URL}/download",
            json={"url": m_url, "original_url": original_url, "headers": headers},
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            if resp.status == 200:
                data = await resp.read()
                return data if len(data) > 1000 else None
    except Exception:
        pass
    return None


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def main():
    await _init_db()
    await _init_pyrogram()
    me = await bot.get_me()
    pyro_tag = "Pyrogram=ON" if _can_use_pyrogram() else "Pyrogram=OFF (50 MB limit)"
    log.info("Bot @%s started  |  workers=%d  fragments=%d  %s", me.username, MAX_CONCURRENT, FRAGMENT_THREADS, pyro_tag)
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        if _pyro_client:
            await _pyro_client.stop()
        if _db:
            await _db.close()


if __name__ == "__main__":
    asyncio.run(main())
