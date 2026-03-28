"""High-performance Telegram audio/media bot.

Architecture inspired by @YtbAudioBot:
  1. Direct yt-dlp with concurrent_fragment_downloads (multi-threaded)
  2. Format 140 (M4A/AAC) — zero transcoding, direct stream copy
  3. SQLite file_id cache — instant re-delivery for previously downloaded tracks
  4. Fully async via aiogram 3.x
  5. Non-YouTube platforms proxied through FastAPI backend
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

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

_dl_sem = asyncio.Semaphore(MAX_CONCURRENT)
_pending: dict[str, dict] = {}

# ── SQLite File-ID Cache ──────────────────────────────────────────────────────
# Maps (video_id, format) → Telegram file_id so repeat requests are instant.

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
    """Toggle caption preference. Returns the new state."""
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


# ── yt-dlp Direct Integration ────────────────────────────────────────────────
# Bypasses FastAPI backend entirely for YouTube. Downloads happen inside
# the bot process, eliminating the extra HTTP hop and memory copies.

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
    """Format 140 = M4A/AAC 128 kbps — YouTube's native audio stream.
    No FFmpeg transcoding needed; just a direct stream copy."""
    return {
        **_base_opts(),
        "format": "140/bestaudio[ext=m4a]/bestaudio",
        "outtmpl": out,
        "concurrent_fragment_downloads": FRAGMENT_THREADS,
    }


def _mp3_opts(out: str) -> dict:
    """Download best audio then transcode to MP3 320 kbps via FFmpeg."""
    return {
        **_base_opts(),
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": out,
        "concurrent_fragment_downloads": FRAGMENT_THREADS,
        "writethumbnail": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
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


async def _yt_download(url: str, mode: str = "audio") -> tuple[Optional[Path], dict]:
    """mode: 'audio' (M4A direct), 'mp3' (transcode), 'video' (MP4)."""
    tmp = Path(tempfile.mkdtemp(prefix="ytbot_"))
    out = str(tmp / "%(id)s.%(ext)s")

    if mode == "mp3":
        opts = _mp3_opts(out)
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


# ── Handlers ──────────────────────────────────────────────────────────────────

@router.message(Command("start", "help"))
async def cmd_start(msg: Message):
    await msg.answer(
        "👋 <b>AnyGrab Downloader</b>\n\n"
        "Paste a link from YouTube, TikTok, Instagram, X and more.\n\n"
        "🎵 YouTube audio: <b>lossless M4A</b>, multi-threaded download, instant cache.\n"
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


# ── YouTube Audio (high-performance path) ─────────────────────────────────────
# Pipeline: cache check → yt-dlp format 140 → thumbnail fetch ∥ upload → cache

async def _send_yt_audio(msg: Message, url: str, reply_to: int, status: Message, user_id: int = 0):
    vid = _yt_id(url)
    t0 = time.perf_counter()

    # ① Cache hit → forward the file already on Telegram's servers (~0.1 s)
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

    # ② Download format 140 (M4A AAC) — no FFmpeg transcoding
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
            size_mb = filepath.stat().st_size / 1_048_576
            dl_sec = time.perf_counter() - t0
            log.info("DOWNLOADED  %s  %.1fs  %.1fMB  fmt=%s", vid, dl_sec, size_mb, info.get("format_id", "?"))

            # ③ Fetch thumbnail concurrently while we prepare upload
            thumb_task = asyncio.create_task(_fetch_thumb(info.get("thumbnail", "")))

            await status.edit_text("📤 Uploading…")

            thumb_data = await thumb_task
            audio_file = FSInputFile(filepath, filename=f"{title}.m4a")
            thumb_file = BufferedInputFile(thumb_data, "thumb.jpg") if thumb_data else None

            sent = await bot.send_audio(
                msg.chat.id,
                audio=audio_file,
                title=title,
                performer=performer,
                duration=duration,
                thumbnail=thumb_file,
                reply_to_message_id=reply_to,
            )

            total = time.perf_counter() - t0
            log.info("SENT  %s  upload=%.1fs  total=%.1fs", vid, total - dl_sec, total)

            # ④ Cache file_id so next request for this track is instant
            if sent.audio and vid:
                await _set_cached(vid, "audio", sent.audio.file_id, title=title, performer=performer, duration=duration)
                log.info("CACHED  %s", vid)

            await status.delete()

        except Exception as e:
            log.error("Audio error: %s", e, exc_info=True)
            try:
                await status.edit_text(f"❌ {str(e)[:200]}")
            except Exception:
                pass
        finally:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)


# ── YouTube MP3 (transcoded via FFmpeg) ───────────────────────────────────────

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
            await status.edit_text("⬇️ Downloading & converting to MP3…")
            filepath, info = await _yt_download(url, mode="mp3")
            if not filepath or not filepath.exists() or filepath.stat().st_size < 1000:
                await status.edit_text("❌ Download failed or file too small.")
                return

            tmp_dir = filepath.parent
            title = (info.get("title") or "audio")[:64]
            performer = info.get("uploader") or info.get("artist")
            duration = int(info.get("duration") or 0)
            size_mb = filepath.stat().st_size / 1_048_576
            dl_sec = time.perf_counter() - t0
            log.info("MP3 READY  %s  %.1fs  %.1fMB", vid, dl_sec, size_mb)

            await status.edit_text("📤 Uploading…")

            audio_file = FSInputFile(filepath, filename=f"{title}.mp3")

            sent = await bot.send_audio(
                msg.chat.id,
                audio=audio_file,
                title=title,
                performer=performer,
                duration=duration,
                reply_to_message_id=reply_to,
            )

            total = time.perf_counter() - t0
            log.info("MP3 SENT  %s  total=%.1fs", vid, total)

            if sent.audio and vid:
                await _set_cached(vid, "mp3", sent.audio.file_id, title=title, performer=performer, duration=duration)
                log.info("MP3 CACHED  %s", vid)

            await status.delete()

        except Exception as e:
            log.error("MP3 error: %s", e, exc_info=True)
            try:
                await status.edit_text(f"❌ {str(e)[:200]}")
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

            if file_size > 50 * 1024 * 1024:
                await status.edit_text(
                    "❌ Video exceeds Telegram's 50 MB limit.\n"
                    "Tip: try 🎵 Audio instead, or use a shorter clip."
                )
                return

            dl_sec = time.perf_counter() - t0
            log.info("VIDEO DL  %s  %.1fs  %.1fMB", vid, dl_sec, file_size / 1e6)

            await status.edit_text("📤 Uploading…")

            thumb_url = info.get("thumbnail", "")
            thumb_data = await _fetch_thumb(thumb_url)
            thumb_file = BufferedInputFile(thumb_data, "thumb.jpg") if thumb_data else None

            show_cap = await _user_wants_caption(user_id) if user_id else CAPTION_DEFAULT
            sent = await bot.send_video(
                msg.chat.id,
                video=FSInputFile(filepath, filename=f"{title}.mp4"),
                caption=title if show_cap else None,
                duration=duration,
                thumbnail=thumb_file,
                supports_streaming=True,
                reply_to_message_id=reply_to,
            )

            if sent.video and vid:
                await _set_cached(vid, "video", sent.video.file_id, title=title, duration=duration)

            await status.delete()

        except Exception as e:
            log.error("Video error: %s", e, exc_info=True)
            try:
                await status.edit_text(f"❌ {str(e)[:200]}")
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

    await status.edit_text("📤 Uploading…")
    is_img = any(ext in m_url.lower() for ext in (".jpg", ".jpeg", ".png", ".webp"))

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
    me = await bot.get_me()
    log.info("Bot @%s started  |  workers=%d  fragments=%d", me.username, MAX_CONCURRENT, FRAGMENT_THREADS)
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        if _db:
            await _db.close()


if __name__ == "__main__":
    asyncio.run(main())
