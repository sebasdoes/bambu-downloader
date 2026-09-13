# Bambu Downloader — Documentation

Architectural documentation for the Bambu Downloader: a self-hosted, single-user
MakerWorld archiver (FastAPI + SQLite + vanilla-JS PWA, run in a container).

## Reading order

| Doc | Contents |
| --- | --- |
| [`architecture.md`](architecture.md) | **C4 models** — System Context (L1), Containers (L2), Components (L3), Dynamic views, key design decisions |
| [`data-model.md`](data-model.md) | SQLite schema, NULL-safe dedup rules, migration strategy, on-disk file layout |
| [`makerworld-integration.md`](makerworld-integration.md) | MakerWorld / Bambu Cloud API map, auth flows, download-URL resolution, anti-abuse handling |
| [`operations.md`](operations.md) | Deployment (podman/compose), configuration reference, lifecycle & graceful shutdown, security notes |

## About the C4 diagrams

The architecture doc follows the [C4 model](https://c4model.com/): zoom levels
from a single **Context** diagram down to **Component** and **Dynamic** views.
Diagrams are written in [Mermaid](https://mermaid.js.org/) C4 syntax and render
in GitHub, VS Code (with a Mermaid preview extension), and
[mermaid.live](https://mermaid.live). Sequence and ER diagrams use standard
Mermaid syntax.

All diagrams are hand-maintained text next to the code — update them when the
architecture changes. Verified API behaviour lives in
`makerworld-integration.md` (verified against live traffic 2026-09-11).