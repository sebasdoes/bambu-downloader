# Architecture — C4 Models

Bambu Downloader is a self-hosted, single-user **archiver for
[MakerWorld](https://makerworld.com)** (Bambu Lab's model sharing platform). It
downloads `.3mf` model files by URL, mirrors whole MakerWorld collections on a
schedule without duplicates, and exposes a small web UI / PWA for managing the
library.

The design goal is **politeness and resilience**: the app talks to Bambu's
APIs with an honest identity, spaces out requests, aborts cleanly on
rate-limiting, and never re-downloads what it already has. State lives in one
SQLite file; models land in a plain folder tree so they are directly usable by
Bambu Studio / a printer.

---

## Level 1 — System Context

> **Purpose**: who uses the system and what external systems it depends on.

```mermaid
C4Context
    title Bambu Downloader — System Context

    Person(user, "Hobbyist user", "Single user on a LAN; owns a Bambu Lab printer and a MakerWorld account")

    System(bd, "Bambu Downloader", "Self-hosted MakerWorld archiver: downloads models by URL and mirrors collections on a schedule. Web UI + PWA.")

    System_Ext(mw, "MakerWorld", "makerworld.com — model/collection metadata API (JSON gateway, not Cloudflare-challenged) + CDN for covers")
    System_Ext(bambu, "Bambu Cloud API", "api.bambulab.com (or .cn) — authentication and per-plate download URLs")
    System_Ext(cdn, "Bambu CDNs", "makerworld.bblmw.com (covers, Aliyun OSS) and S3 buckets (model files, presigned URLs)")

    Rel(user, bd, "Browses library, pastes model/collection URLs, manages account", "HTTPS (LAN)")
    Rel(bd, mw, "Fetches design/collection metadata + covers", "HTTPS JSON API")
    Rel(bd, bambu, "Logs in, validates/refreshes tokens, resolves download URLs", "HTTPS JSON API, Bearer token")
    Rel(bd, cdn, "Downloads .3mf files and cover images", "HTTPS presigned URLs")
```

**Notes**

- The user is the *only* user: it is a personal, single-tenant system. The
  optional `BND_API_KEY` shared secret protects the API on shared networks.
- MakerWorld's HTML pages sit behind a Cloudflare challenge, but the
  `/api/*` JSON gateway is not — that distinction shapes the whole client
  design (see [`makerworld-integration.md`](makerworld-integration.md)).
- The app never talks to printers. Models are saved to a folder the user can
  sync to a printer via their existing tooling.

---

## Level 2 — Containers

> **Purpose**: the runtime units inside the system and how they communicate.

```mermaid
C4Container
    title Bambu Downloader — Containers (single container process)

    Person(user, "Hobbyist user", "Uses the web UI / PWA from a browser or phone")

    Container_Boundary(cb, "bambu-downloader container (podman/compose)", "Python 3.13 + uvicorn, one process") {
        Container(ui, "Web UI / PWA", "Vanilla JS, HTML, CSS, service worker", "Single-page app shell served by the backend; offline cache, share-target for shared URLs")
        Container(webapp, "FastAPI application (uvicorn)", "Python", "Serves the REST API, thumbnails and static shell; owns the lifespan that builds all singletons")
        ContainerDb(db, "SQLite database", "SQLite (WAL)", "bambu_downloader.db — models, collections, remote-collection cache, auth token, sync state")
        Container(scheduler, "Sync scheduler", "Python asyncio task", "Wakes every BND_SCHEDULER_INTERVAL_SECONDS, syncs due collections one at a time; refreshes the own-collections cache hourly")
        Container(mwclient, "MakerWorld client pool", "Python httpx (+ urllib for S3)", "Shared AsyncClient per (token, region); ad-hoc clients for login flows; verbatim urllib fetch for presigned S3 URLs")
    }

    SystemDb_Ext(sqlite, "SQLite file", "On the mounted data/ volume")

    System_Ext(mw, "MakerWorld API", "Metadata + collection listings")
    System_Ext(bambu, "Bambu Cloud API", "Login, token refresh, download URLs")
    System_Ext(cdn, "CDNs", "Model files (S3 presigned) and covers (OSS)")
    System_Ext(fs, "downloads/ volume", "Bind-mounted host folder with the .3mf archive")

    Rel(user, ui, "Uses", "HTTPS")
    Rel(ui, webapp, "Calls /api/* (JSON), /thumb (images)", "HTTPS, optional X-API-Key")
    Rel(webapp, scheduler, "Creates / cancels via lifespan; manual syncs via trigger_sync()", "in-process")
    Rel(webapp, mwclient, "get_client()/release_client()", "in-process")
    Rel(scheduler, mwclient, "Syncs due collections, refreshes own-collections cache", "HTTPS")
    Rel(mwclient, mw, "Metadata, collections", "HTTPS")
    Rel(mwclient, bambu, "Auth + download URLs", "HTTPS Bearer")
    Rel(mwclient, cdn, "File + cover downloads", "HTTPS")
    Rel(webapp, db, "Reads/writes state", "sqlite3")
    Rel(scheduler, db, "Reads due collections, records sync results", "sqlite3")
    Rel(db, sqlite, "Persists", "WAL journal, busy_timeout 30s")
    Rel(webapp, fs, "Writes model files, cover.webp, .part temp files", "filesystem")
```

**Key points**

- **One process, one container.** The FastAPI app, scheduler and client pool
  live in the same asyncio event loop. There is no queue, no workers, no
  message broker — concurrency is bounded by an `asyncio.Semaphore(2)`
  around downloads.
- **SQLite in WAL mode** with a 30 s busy timeout is shared by the API layer,
  scheduler, and the boot-time metadata backfill task without locks.
- The **UI is a PWA shell** served by the backend: no build step, no external
  CDNs; a strict CSP keeps everything self-origin. Covers are proxied through
  `/thumb` because the browser cannot hotlink the CDN (CORS).
- Deployment runs under **rootless podman** with `userns_mode: keep-id` so
  bind-mounted `data/` and `downloads/` stay writable (the Dockerfile bakes a
  matching `BND_UID`).

---

## Level 3 — Components (FastAPI application)

> **Purpose**: the modules inside the Python container and their
> responsibilities. Wiring happens once at startup in
> `app/main.py::lifespan`.

```mermaid
C4Component
    title FastAPI application — components (app/)

    Container_Boundary(app, "FastAPI application") {
        Component(main, "main.py", "Python", "Lifespan: writability probe, DB backup, singletons, scheduler start, backfill task. Also /thumb proxy, PWA manifest, service worker, CSP shell.")
        Component(config, "config.py", "Python", "Settings from BND_* env vars, captured once at import")
        Component(routes, "routes.py", "Python", "REST API under /api/* (auth, download, collections, library, events). Singletons injected via init(); optional X-API-Key gate")
        Component(downloader, "downloader.py — DownloadManager", "Python", "Queue, dedup, politeness delay, download-URL resolution chain, metadata backfill, token refresh, own-collections refresh")
        Component(makerworld, "makerworld.py", "Python", "MakerWorldClient, pooled get_client()/release_client(), URL parsers, typed error hierarchy, captcha detection, S3-verbatim downloader")
        Component(scheduler, "scheduler.py — SyncScheduler", "Python", "Polling loop for due collections; per-collection manual sync tasks; my-collections refresh deadline")
        Component(db, "db.py — Database", "Python", "SQLite wrapper: schema, migrations, models/collections/meta/remote_collections queries, upserts")
        Component(static, "static/ (index.html, app.js, sw.js, manifest)", "JS/HTML", "PWA UI: library grid, sync controls, login, activity log; versioned service-worker cache")
    }

    Rel(main, routes, "routes.init(db, manager, scheduler)", "startup wiring")
    Rel(main, db, "Creates", "startup")
    Rel(main, downloader, "Creates", "startup")
    Rel(main, scheduler, "Creates + starts", "startup")
    Rel(routes, downloader, "download/resolve/sync calls", "in-process")
    Rel(routes, db, "CRUD", "in-process")
    Rel(routes, makerworld, "Login flows (ad-hoc clients), collection validation", "in-process")
    Rel(downloader, makerworld, "Pooled clients; downloads", "in-process")
    Rel(downloader, db, "Dedup checks, model rows, token storage", "in-process")
    Rel(scheduler, downloader, "sync_collection(cid), refresh_my_collections()", "in-process")
    Rel(scheduler, db, "due_collections(), record_sync()", "in-process")
    Rel(db, static, "No dependency", "—")
```

Dependency rule: `main → routes/downloader/scheduler → makerworld, db`.
`routes` imports module-level singletons set by `init()` to avoid import
cycles. `static/` only consumes the HTTP API — it has no Python imports.

### Component responsibilities

| Component | Owns |
| --- | --- |
| `main.py` | Lifespan (probe writable volumes → backup DB → open DB → wire routes → start scheduler → background metadata backfill); `/thumb` proxy with ETag/304; PWA endpoints; CSP headers |
| `config.py` | All `BND_*` settings; 15-minute floors on anti-abuse-sensitive intervals |
| `routes.py` | 18 REST endpoints; API-key gate (`hmac.compare_digest`); auth flows (login/verify/token/logout); path-guarded file deletion |
| `downloader.py` | `DownloadManager`: semaphore-bounded queue, dedup by `(design_id, profile_id)`, download-URL fallback chain, cover saving, event ring buffer, silent token refresh with lock |
| `makerworld.py` | HTTP client(s) keyed on `(token, region)`; login (password → email code / TOTP); endpoint wrappers; error taxonomy (`AuthRequiredError`, `AuthExpiredError`, `ForbiddenError`, `NotFoundError`, `CaptchaError`); `download_file` (httpx CDN / urllib S3-verbatim) |
| `scheduler.py` | Due-collection polling; sequential syncs; my-collections hourly deadline (backdated on restart so a fresh container makes no request); graceful cancellation of manual syncs |
| `db.py` | Schema + idempotent migrations; WAL; NULL-safe unique index; label migration and backfill scan watermarks |

---

## Dynamic views

### Sequence — single model download (POST /api/download)

```mermaid
sequenceDiagram
    autonumber
    participant U as User (UI)
    participant R as routes.py
    participant M as DownloadManager
    participant D as Database
    participant C as MakerWorldClient (pool)
    participant MW as MakerWorld/Bambu APIs
    participant FS as downloads/ volume

    U->>R: POST /api/download {url}
    R->>M: download_model(url)
    M->>M: parse_model_url → (design_id, profile_id?)
    M->>D: model_exists(_any)? — dedup
    alt already stored (file exists)
        M-->>R: {"status": "exists"}
    else new
        M->>C: get_client(token, region)
        M->>MW: GET design metadata + instances
        M->>MW: GET /v1/iot-service/api/user/profile/{profileId}?model_id=…
        MW-->>M: signed download URL + filename hint
        Note over M: Fallbacks (old models only):<br/>instance /f3mf → design /model
        M->>FS: stream to .part → rename to final name
        M->>MW: fetch cover (CDN resize params)
        M->>FS: cover.webp next to model
        M->>D: insert_model (upsert)
        M->>M: add_event("download", …)
        M-->>R: {"status": "downloaded", path, size}
    end
    R-->>U: 200 summary
```

### Sequence — scheduled collection sync

```mermaid
sequenceDiagram
    autonumber
    participant S as SyncScheduler loop
    participant D as Database
    participant M as DownloadManager
    participant C as MakerWorldClient
    participant MW as MakerWorld APIs

    S->>D: due_collections(now)
    loop for each due collection (sequential)
        S->>S: active[cid] = "syncing" (UI state)
        S->>M: sync_collection(cid)
        M->>C: get_collection_info + list_collection_designs (paged)
        MW-->>M: hits[] with design ids
        loop per design (plates_mode: default | all)
            M->>D: model_exists? — skip downloaded
            M->>M: download_model(url#profileId-N, subfolder=title)
            Note over M: ≥ BND_DOWNLOAD_DELAY_SECONDS (3 s)<br/>between downloads — anti-abuse
        end
        alt AuthRequiredError / CaptchaError
            Note over M: abort whole sync (never hammer);<br/>auth → one silent try_token_refresh() then retry
            M->>D: record_sync(cid, "auth-required"|"captcha", new_so_far)
        else normal end
            M->>D: record_sync(cid, "ok"|"partial", new)
        end
        S->>S: active[cid] = None
    end
    Note over S: every my_collections_refresh_minutes (60):<br/>refresh_my_collections() → remote_collections cache
```

### Sequence — token expiry and silent refresh

```mermaid
sequenceDiagram
    autonumber
    participant M as DownloadManager
    participant C as Pooled client
    participant B as Bambu Cloud API
    participant D as Database

    M->>C: authed call
    C->>B: GET … Bearer <expired token>
    B-->>C: 401
    C-->>M: AuthExpiredError
    M->>M: try_token_refresh() (asyncio.Lock)
    M->>B: POST /v1/user-service/user/login (refresh token)
    B-->>M: new access + (rotated) refresh token
    M->>D: store bambu_token / bambu_token_refresh
    M->>C: invalidate_shared_clients() — pool rebuilt with new token
    M-->>M: caller retries the failed request
    Note over M: refresh failure keeps the old tokens —<br/>a transient 5xx must not force a sign-out
```

### State machine — collection sync outcome

```mermaid
stateDiagram-v2
    [*] --> syncing: scheduler (interval elapsed) or manual trigger
    syncing --> ok: all designs processed
    syncing --> partial: per-model errors recorded
    syncing --> auth_required: AuthRequiredError and refresh failed
    syncing --> captcha: HTTP 418 — stop, never retry
    auth_required --> [*]: record_sync(status, new)
    captcha --> [*]: clears by itself in hours
    partial --> [*]
    ok --> [*]: last_sync_at stamps the next due time
```

### Data flow — thumbnails

```mermaid
flowchart LR
    UI[app.js img tags] -->|GET /thumb?url=…| TH[/thumb proxy]
    TH -->|cover.webp exists on disk| L[Local file + ETag]
    TH -->|miss| CDN[CDN: x-oss-process resize]
    CDN -->|persist| FS[(cover.webp next to model)]
    CDN --> B[Response + ETag, 24 h cache]
```

---

## Key design decisions

| # | Decision | Rationale |
| --- | --- | --- |
| 1 | **SQLite, single file, WAL** | Single-user archive; zero ops; WAL + 30 s busy timeout covers API + scheduler + backfill concurrency. |
| 2 | **Dedup keyed on `(design_id, profile_id)`** with `COALESCE(profile_id, -1)` | Plate-less designs were duplicating on re-download (SQLite treats NULLs as distinct in UNIQUE). The coalesced index matches the upsert exactly. |
| 3 | **Pooled httpx clients per `(token, region)`, invalidated on any credential change** | Warm TLS connections for syncs; login flows use ad-hoc clients so CSRF cookies don't leak between identities. |
| 4 | **S3 presigned URLs fetched verbatim via urllib in a thread** | httpx re-encodes query strings → SigV4 `SignatureDoesNotMatch`. The signed query *is* the credential — no auth headers on CDN calls. |
| 5 | **Politeness everywhere** (3 s inter-download delay, capped pagination, one sync at a time, semaphore(2)) | Bambu's anti-abuse layer (HTTP 418 CAPTCHA) trips on bursts; every extra request during a block deepens it. |
| 6 | **Abort the whole sync on auth/CAPTCHA; record truthful partial progress** | Avoids 68 failed requests hammering the API; `last_sync_status` keeps `last_sync_new` honest. |
| 7 | **Fail-fast writability probe + on-boot DB backup** | Rootless-podman UID mapping quietly breaks bind mounts; better to say so at startup, with the exact fix commands. |
| 8 | **Graceful shutdown via uvicorn lifespan** | `podman stop` → SIGTERM → cancel scheduler/manual syncs → checkpoint WAL → close pooled sockets cleanly. |
| 9 | **Covers cached as `cover.webp` next to the model** | The library browses offline and `/thumb` avoids CDN round-trips; CDN resize params turn 4 MB PNGs into ~22 KB WebPs. |
| 10 | **Honest User-Agent, no browser impersonation** | Plays well with the API gateway; only the JSON endpoints are used (never Cloudflare-challenged). |

See also: [`data-model.md`](data-model.md) for schema specifics,
[`makerworld-integration.md`](makerworld-integration.md) for the endpoint map
and auth flows, [`operations.md`](operations.md) for deployment and tuning.