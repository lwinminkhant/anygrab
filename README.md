# AnyGrab

Universal social media downloader with a web UI and Telegram bot. Grab videos, photos, and audio from YouTube, TikTok, Instagram, X/Twitter, and Facebook.

## Features

**Web UI** — Paste a link, preview metadata, download to your local `~/Downloads/AnyGrab` folder.

**Telegram Bot** — Send a link in chat, get the file delivered instantly. Supports groups.

**YouTube Audio** — Three format options:
- **M4A** — Zero-conversion direct stream copy, fastest possible
- **MP3 320 kbps** — Transcoded via FFmpeg with embedded thumbnail and metadata
- **Video** — Best quality MP4 with H.264

**Performance at scale:**
- Concurrent extractions and downloads with semaphore gates
- In-memory LRU + TTL cache for repeated URLs
- Per-IP rate limiting with sliding window
- Request timeouts and graceful error handling
- Multi-worker uvicorn support
- SQLite file_id cache in the Telegram bot — repeat requests are instant (~0.1s)

**Large file support:**
- Files under 50 MB upload through the standard Bot API
- Files up to 2 GB upload through Pyrogram's MTProto protocol (optional, needs API credentials)
- MP3 bitrate auto-adjusts to fit within the upload limit

## Architecture

```
┌─────────────────┐     ┌─────────────────────────────────┐
│   Web Browser    │────>│  FastAPI Server (main.py)       │
│   public/*       │     │  /api/v1/extract                │
└─────────────────┘     │  /api/v1/download               │
                        │  /api/v1/save                   │
┌─────────────────┐     │  /api/v1/health                 │
│  Telegram Bot   │────>│  /api/v1/queue                  │
│  telegram_bot.py│     └──────────┬──────────────────────┘
│                 │                │
│  YouTube:       │     ┌──────────▼──────────┐
│  Direct yt-dlp  │     │  yt-dlp             │
│  (bypasses API) │     │  tikwm.com fallback │
│                 │     │  Instagram API      │
│  Other:         │     └─────────────────────┘
│  Proxied via API│
└─────────────────┘
```

## Quick Start

### Prerequisites

- Python 3.11+
- FFmpeg (for audio conversion and thumbnail embedding)

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/anygrab.git
cd anygrab
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and add your Telegram bot token (get one from [@BotFather](https://t.me/BotFather)).

### 3. Start the API server

```bash
python main.py
```

The web UI is now at [http://localhost:8000](http://localhost:8000).

### 4. Start the Telegram bot

In a second terminal:

```bash
source .venv/bin/activate
python telegram_bot.py
```

## Configuration

All configuration is via environment variables in `.env`. See `.env.example` for the full list.

### Required

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |

### Optional — Large file uploads

| Variable | Description |
|---|---|
| `TG_API_ID` | Telegram API ID from [my.telegram.org](https://my.telegram.org) |
| `TG_API_HASH` | Telegram API hash from [my.telegram.org](https://my.telegram.org) |

With these set, the bot uses Pyrogram's MTProto protocol to upload files up to 2 GB. Without them, the 50 MB Bot API limit applies.

### Optional — Performance tuning

| Variable | Default | Description |
|---|---|---|
| `MAX_EXTRACTIONS` | `6` | Max concurrent yt-dlp extractions (API server) |
| `MAX_DOWNLOADS` | `4` | Max concurrent file downloads (API server) |
| `CACHE_TTL` | `300` | Extraction cache lifetime in seconds |
| `CACHE_MAX` | `256` | Max cached extraction results |
| `RATE_LIMIT_REQ` | `30` | Max requests per IP per window |
| `RATE_LIMIT_WIN` | `60` | Rate limit window in seconds |
| `REQUEST_TIMEOUT` | `120` | Server-side request timeout in seconds |
| `WORKERS` | `1` | Uvicorn worker processes |
| `BOT_WORKERS` | `4` | Max concurrent bot download tasks |
| `FRAGMENT_THREADS` | `8` | yt-dlp concurrent fragment downloads |
| `CAPTION_DEFAULT` | `true` | Show media captions by default |

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/extract` | Extract metadata and media URLs from a social media link |
| `POST` | `/api/v1/download` | Proxy-download a media file (streaming) |
| `GET`  | `/api/v1/download` | Same as above, GET version for convenience |
| `POST` | `/api/v1/save` | Download and save media to `~/Downloads/AnyGrab` |
| `GET`  | `/api/v1/settings` | Get current settings (download directory) |
| `GET`  | `/api/v1/health` | Server health, active tasks, cache stats |
| `GET`  | `/api/v1/queue` | Current queue status and available slots |

### Example: Extract metadata

```bash
curl -X POST http://localhost:8000/api/v1/extract \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
```

## Telegram Bot Commands

| Command | Description |
|---|---|
| `/start` | Welcome message and status |
| `/help` | Same as /start |
| `/caption` | Toggle captions on/off for your account |

For YouTube links, the bot shows inline buttons:
- **Video** — Best quality MP4
- **MP3** — 320 kbps with embedded thumbnail
- **M4A (fast)** — Direct stream copy, no conversion

For all other platforms, the bot downloads and sends media automatically.

## Supported Platforms

| Platform | Video | Photos | Audio | Carousel |
|---|---|---|---|---|
| YouTube | ✅ | — | ✅ M4A / MP3 | — |
| TikTok | ✅ | ✅ | — | ✅ |
| Instagram | ✅ | ✅ | — | ✅ |
| X / Twitter | ✅ | ✅ | — | — |
| Facebook | ✅ | — | — | — |

## Project Structure

```
anygrab/
├── main.py              # FastAPI server — extraction, download, proxy
├── telegram_bot.py      # Telegram bot — aiogram + pyrogram
├── public/
│   ├── index.html       # Web UI
│   ├── script.js        # Frontend logic with retry/abort
│   └── style.css        # Glassmorphism UI
├── deploy/
│   ├── setup.sh         # One-command VPS setup script
│   ├── anygrab-api.service  # systemd unit for API server
│   └── anygrab-bot.service  # systemd unit for Telegram bot
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── requirements.txt
├── pyproject.toml
├── .env.example
├── .gitignore
└── README.md
```

## Deployment (Hetzner / any VPS)

Two options: **Docker** (recommended) or **bare-metal systemd**.

### Option A: Docker Compose

SSH into your VPS and run:

```bash
git clone https://github.com/YOUR_USER/anygrab.git /opt/anygrab
cd /opt/anygrab

# Configure
cp .env.example .env
nano .env   # add your TELEGRAM_BOT_TOKEN

# Launch
docker compose up -d

# Check status
docker compose ps
docker compose logs -f bot
```

To update:

```bash
cd /opt/anygrab && git pull && docker compose up -d --build
```

### Option B: Systemd (bare metal)

One-command setup on Ubuntu 22.04 / Debian 12:

```bash
git clone https://github.com/YOUR_USER/anygrab.git /opt/anygrab
sudo /opt/anygrab/deploy/setup.sh
sudo nano /opt/anygrab/.env   # add your tokens
sudo systemctl restart anygrab-api anygrab-bot
```

This installs Python, FFmpeg, creates a system user, sets up a venv, and enables both services.

Management:

```bash
# Status
sudo systemctl status anygrab-api anygrab-bot

# Logs (live)
sudo journalctl -u anygrab-bot -f

# Restart after code changes
cd /opt/anygrab && git pull
sudo systemctl restart anygrab-api anygrab-bot
```

### Reverse proxy (optional)

To serve the web UI on port 443 with SSL, put Caddy or nginx in front:

**Caddy** (auto SSL):

```
anygrab.yourdomain.com {
    reverse_proxy localhost:8000
}
```

**Nginx**:

```nginx
server {
    listen 80;
    server_name anygrab.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 300s;
        client_max_body_size 0;
    }
}
```

### Firewall

```bash
# Allow SSH + HTTP + HTTPS
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

The Telegram bot doesn't need any inbound ports — it uses long polling.

## Authentication for Protected Content

Some platforms require authentication. Two options:

1. **cookies.txt** — Export cookies in Netscape format and place as `cookies.txt` in the project root. Used automatically by yt-dlp.

2. **Browser cookies** — By default, the app reads cookies from Brave browser. Change the browser in the source code if needed.

## License

MIT
