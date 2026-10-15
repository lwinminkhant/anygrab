# How to Run AnyGrab

AnyGrab consists of a FastAPI backend (which serves the Web UI from `public/`) and an optional Telegram Bot.

---

## 🚀 Quick Start (Local Development)

### Option 1: Using npm (Recommended)

Since `package.json` is configured, you can simply run:

```bash
npm run dev
```

- **Web UI:** [http://localhost:8000](http://localhost:8000)
- **API Health Check:** [http://localhost:8000/api/v1/health](http://localhost:8000/api/v1/health)

To start the Telegram bot:
```bash
npm run bot
```

---

### Option 2: Using Python Directly

#### 1. Setup Virtual Environment & Dependencies
If you need to install or refresh dependencies:
```bash
# Using uv (fastest)
uv venv
uv pip install -r requirements.txt

# Or using standard python venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

#### 2. Start the API & Web UI Server
```bash
source .venv/bin/activate
python main.py
```

#### 3. Start the Telegram Bot (Optional)
In a separate terminal:
```bash
source .venv/bin/activate
python telegram_bot.py
```

---

## 🐳 Running with Docker

To build and run AnyGrab with Docker Compose:

```bash
docker compose build
docker compose up -d
```

Check container status & logs:
```bash
docker compose ps
docker compose logs -f
```

---

## Render Free deployment

The root `render.yaml` defines one Docker web service on the Free plan in
Singapore. It deploys the `dark` branch, which contains the current frontend.
Commit and push the current app files and deployment configuration before deploying.

1. In Render, select **New > Blueprint** and connect `lwinminkhant/anygrab`.
2. Select the `dark` branch and use `render.yaml` as the Blueprint path.
3. Review that the service uses **Docker**, **Free**, and **Singapore**, then deploy.
4. Once live, open the assigned `https://...onrender.com` URL and check
   `/api/v1/health`. Both the frontend and API share this origin.

For manual **New > Web Service** setup instead, select the same repository and
branch, choose Docker and the Free plan, and leave Docker Command blank to use
the Dockerfile's `python main.py`. Copy the environment values from `render.yaml`
and set Health Check Path to `/api/v1/health`. Do not use `npm start` on Render:
the Docker image installs Python globally rather than creating `.venv`.

The app listens on Render's `PORT`; local runs default to port 8000. The free
configuration uses one worker and reduced download concurrency. Transcription is
disabled. The Telegram bot is not included in this web service.

Render Free sleeps after 15 minutes without inbound traffic. Its filesystem is
ephemeral: SQLite accounts, sessions, saved history, and server-side files are
lost on restart, redeploy, or sleep. This configuration is a downloader demo;
durable account history requires a storage/database change or a paid disk.

Google sign-in is optional and disabled unless its required variables are set.
To enable it later, add `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, a stable
`ACCOUNT_ENCRYPTION_KEY`, `ACCOUNT_ORIGIN=https://YOUR_SERVICE.onrender.com`, and
`GOOGLE_REDIRECT_URI=https://YOUR_SERVICE.onrender.com/api/v1/auth/google/callback`
in Render's Environment settings. Register that exact redirect URI in Google
Cloud. Do not upload `.env`, cookies, or Telegram credentials to GitHub.

Automatic deployments are disabled in the Blueprint; deploy approved changes
manually. When changing a CSS/JS asset, update its version query in
`public/index.html` so existing visitors receive the new file.

Cloud datacenter IPs can be blocked by media platforms. Verify extraction and a
small download after deployment before relying on this instance.

The anonymous downloader restricts source and CDN hosts, rejects private network
addresses and unsafe filenames, and checks redirects in its Python HTTP transport. Its guarded
yt-dlp transport uses urllib without Chrome impersonation; some sources may fail
where browser impersonation was previously needed. New platform/CDN hosts need
review before being added to `public_media_safety.py`.

References: [Render Docker](https://render.com/docs/docker),
[Blueprint reference](https://render.com/docs/blueprint-spec),
[Free service limits](https://render.com/docs/free).

---

## 🌐 Production Server & Nginx

If deploying behind Nginx on a Linux server:

1. **Reload Nginx:**
   ```bash
   sudo rm -f /etc/nginx/sites-enabled/default
   sudo nginx -t && sudo systemctl reload nginx
   ```

2. **Verify Deployment:**
   ```bash
   curl -sS --resolve anygrab.lwinminkhant.pro:443:49.12.100.233 https://anygrab.lwinminkhant.pro/api/v1/health
   ```

---

## ⚠️ Bypassing Bot Detection (YouTube & Instagram)

Datacenter IP addresses (Hetzner, AWS, DigitalOcean, etc.) frequently encounter YouTube's:
> `Sign in to confirm you're not a bot`

AnyGrab supports two methods to resolve this:

### Method 1: Export `cookies.txt` (Recommended & Free)

1. Install the browser extension **"Get cookies.txt LOCALLY"** (available on Chrome Web Store / Firefox Add-ons).
2. Go to [https://www.youtube.com](https://www.youtube.com) (and/or Instagram) in your browser while signed in.
3. Click the extension icon and click **Export** (Netscape format).
4. Save or copy the file to `cookies.txt` in the AnyGrab project root (or `/opt/anygrab/cookies.txt` on the VPS).
   - **Local run:** `main.py` and `telegram_bot.py` automatically detect `cookies.txt`.
   - **Docker run:** `docker-compose.yml` mounts `./cookies.txt:/app/cookies.txt:ro` automatically for both API and bot.

> [!NOTE]
> `cookies.txt` is already added to `.gitignore` so your personal cookies will never be pushed to version control.

---

### Method 2: Residential Proxy (For High Volume)

If you are running a high-traffic instance or don't want to update cookies periodically:
1. Obtain a residential or rotating proxy (HTTP or SOCKS5), for example from BrightData, Webshare, Smartproxy, or IPRoyal.
2. Set `PROXY_URL` in your `.env` file:
   ```env
   PROXY_URL=http://username:password@proxy-server.com:8080
   # or
   PROXY_URL=socks5://username:password@proxy-server.com:1080
   ```
3. Restart the server or Docker container:
   ```bash
   docker compose restart
   ```
   Both yt-dlp and the API backend will route extractions and downloads through the proxy.
