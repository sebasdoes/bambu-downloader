# bambu_downloader — MakerWorld Model Downloader

A self-hosted container with a web UI that:

1. **Authenticates to MakerWorld** (Bambu account — email + password with email-code or TOTP 2FA, or paste an existing access token)
2. **Downloads models by URL** — paste any MakerWorld model link
3. **Downloads all models from a collection** — paste a collection link
4. **Periodically syncs collections** — new models are downloaded automatically, existing ones are never duplicated (no user interaction needed after setup)
5. **PWA-ready** — installable/shareable from Bambu Handy links on Android (via a share target)

## Quick start (compose)

```bash
# podman (rootless) — 'podman-compose up -d' works too
podman compose up -d --build

# docker
docker compose up -d --build
```

The compose file ships with `userns_mode: keep-id`, which makes your host uid
appear inside the container so the bind-mounted `downloads/` and `data/` are
writable under **rootless podman**. **Docker users: remove that line** —
`keep-id` is podman-specific.

Then open http://localhost:8080

Alternatively, plain podman run:

```bash
podman build -t bambu-downloader .
podman run -d --name bambu-downloader --userns=keep-id \
  -p 8080:8080 -v ./downloads:/app/downloads:Z -v ./data:/app/data:Z \
  bambu-downloader
```

(Docker users: use `docker run` without `--userns=keep-id`, and ensure
`./data` and `./downloads` are writable by uid 1000.)

- Downloads land in `./downloads` (host) → `/app/downloads` (container). Each model folder also contains a small `cover.webp` thumbnail, so the folder is browsable in any file manager.
- State database (including your Bambu Cloud token) in `./data/bambu_downloader.db`
- To sign in: Settings → MakerWorld login (email + password). If your account uses email verification codes, you'll be asked for the 6-digit code. TOTP (authenticator app) is also supported. Alternatively paste an existing `access_token` from a browser session.

**Stops cleanly:** `podman stop` / `docker compose stop` sends SIGTERM; uvicorn (PID 1) drains connections and cancels in-flight syncs, so no SIGKILL is needed.

**Login persists across restarts:** the token lives in `./data/` (persistent volume). `podman stop`/`start`/`restart` keep you signed in — only logout (or Bambu expiring the token) signs you out.

### Troubleshooting: "attempt to write a readonly database"

The container runs as an unprivileged user (UID 1000 by default). With **rootless podman**, container UID 1000 does *not* map to your host UID — bind-mounted `./data` ends up owned by "nobody" from the container's perspective, and SQLite can't write. Two fixes:

```bash
# Option A (simplest, and what docker-compose.yml already does):
# keep-id — your host uid appears inside the container
podman run -d --name bambu-downloader --userns=keep-id \
  -p 8080:8080 -v ./downloads:/app/downloads:Z -v ./data:/app/data:Z bambu-downloader
# (compose: userns_mode: keep-id is already in docker-compose.yml)

# Option B: chown the host dirs into your subuid range (container uid 1000)
podman unshare chown -R 1000:1000 ./data ./downloads
```

Note: `podman unshare chown` makes the dirs owned by your subuid range — `ls -l` on the host will look odd afterwards; that's expected.

## Periodic collection sync

Collections tab → add a collection URL (e.g. `https://makerworld.com/en/collections/18095020-relief-sculpture-collections`).
Choose a sync interval (e.g. every 6h). The app:

- Lists every design in the collection
- Skips designs already downloaded (keyed by design/profile ID in SQLite)
- Downloads new ones into `downloads/<collection-title>/<model-slug>/`
- All automatic after setup — the scheduler wakes every 5 minutes and picks up due collections; no user interaction required
- Unfollowing a collection asks whether to keep or **delete its downloaded files** from disk (empty folders are cleaned up too)

## Security notes

- **Local/LAN use.** Only expose the port on your LAN. On a shared network, set `BND_API_KEY` (below).
- Your Bambu Cloud **access token is stored in plaintext in SQLite** (`./data/`). Protect the data volume's permissions; anyone with read access can act as your MakerWorld account. This is inherent to a self-hosted token store.
- The app never logs the token or password (logs go to stdout only).
- Downloads are written as UID 1000 (matching typical host user) — no root-owned files.
- UI escapes all remote content (titles/filenames) — no XSS from model metadata.

## Configuration

Environment variables (all optional — defaults shown):

| Variable | Default | Purpose |
|---|---|---|
| `BND_API_KEY` | *(unset)* | When set, every `/api/*` request must carry `X-API-Key: <value>` — protects the token from other LAN users |
| `BND_DOWNLOAD_DIR` | `/app/downloads` | Where models are saved |
| `BND_DATA_DIR` | `/app/data` | State dir (DB path derives from this unless overridden) |
| `BND_DB_PATH` | `/app/data/bambu_downloader.db` | SQLite state |
| `BND_PORT` | `8080` | Web UI port |
| `BND_SCHEDULER_INTERVAL_SECONDS` | `300` | How often the scheduler checks for due collections |
| `BND_SYNC_INTERVAL_MINUTES` | `360` | Default sync interval for newly added collections |

## How it works (reverse-engineered endpoints)

The app talks to the same backend MakerWorld's web UI uses:

- `api.bambulab.com` for login + download URLs (not behind Cloudflare challenge)
- `makerworld.com/api/v1/design-service/...` for public metadata and collection listings
- Authenticated calls use the Bambu Cloud `access_token` as a Bearer token

> Not affiliated with Bambu Lab / MakerWorld. For personal use with your own account, subject to their ToS.

## CI: prebuilt container image

A GitHub Actions workflow (`.github/workflows/container-image.yml`) builds the image on every push and publishes it to **GitHub Container Registry** — no secrets to configure, it uses the built-in `GITHUB_TOKEN`:

- Push to `main` → `ghcr.io/<owner>/<repo>:latest` (+ `:main`, `:sha`)
- Tag `v1.2.3` → `:1.2.3` (+ `:1.2`)
- Pull requests → build-only (validates the Dockerfile, no push)
- Multi-arch: `linux/amd64` and `linux/arm64` (works on a Pi/NAS)

Pull the prebuilt image instead of building locally:

```bash
podman pull ghcr.io/<owner>/bambu_downloader:latest
```

The package inherits the repo's visibility: **public repo → public image** (anyone can pull, no login). **Private repo → private image** — pull with `podman login ghcr.io` using a PAT with `read:packages`. If the first workflow run on a private repo fails to push the package, check that "Workflow permissions" in repo Settings → Actions is set to "Read and write permissions", or re-run the workflow after the package is created.

## Development

```bash
# local (non-container)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8080
```