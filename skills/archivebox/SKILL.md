---
name: archivebox
description: Use this when an agent needs to install, run, automate, inspect, or troubleshoot ArchiveBox collections. Covers setup, data-folder workflow, CLI usage, Docker usage, Admin UI, REST API, SQLite index.sqlite3 access, filesystem layout, and `archivebox shell -c '...'` Django ORM snippets.
---

# ArchiveBox

Use this skill when an agent needs to operate a full ArchiveBox collection: add URLs, run the server, inspect archived snapshots, automate via API, query `index.sqlite3`, or use the Django shell/ORM.

ArchiveBox is the full self-hosted archiving app. For one-off extraction without a collection/server, prefer the `abx-dl` skill.

## Core Model

- Always `cd` into the ArchiveBox data directory before running `archivebox ...`.
- A data directory contains `ArchiveBox.conf`, `index.sqlite3`, and `archive/`.
- Run `archivebox init` before other collection commands; it is safe to rerun and applies migrations.
- Do not rely on `DATA_DIR=/path/to/data archivebox ...` for agent workflows; commands must be run from inside the initialized `data/` directory.
- Avoid running local/dev ArchiveBox commands as root. The official Docker image handles its own runtime environment.

Current default layout:

```text
data/
  ArchiveBox.conf
  index.sqlite3
  archive/
    users/
      <username>/
        crawls/
          YYYYMMDD/
            <domain>/
              <crawl-uuid>/
                ...crawl-level state
        snapshots/
          YYYYMMDD/
            <domain>/
              <snapshot-uuid>/
                index.jsonl
                <plugin>/
                  ...plugin output files
```


## Setup

Preferred Docker Compose workflow:

```bash
mkdir -p ~/archivebox/data
cd ~/archivebox
curl -fsSL 'https://raw.githubusercontent.com/ArchiveBox/ArchiveBox/dev/docker-compose.yml' -o docker-compose.yml
docker compose run archivebox init
docker compose up
```

Local checkout workflow:

```bash
cd /path/to/ArchiveBox/archivebox
uv sync --dev --all-extras
mkdir -p ./data
cd ./data
uv run --project .. archivebox init --install
```

Published CLI workflow:

```bash
mkdir -p ~/archivebox/data
cd ~/archivebox/data
archivebox init --install
```

## Basic CLI Usage

Run commands from inside the data directory:

```bash
cd ~/archivebox/data
archivebox version
archivebox help
archivebox status
archivebox add 'https://example.com'
archivebox add --depth=1 'https://news.ycombinator.com'
echo 'https://example.com' | archivebox add
archivebox list --json --with-headers > index.json
archivebox list --html --with-headers > index.html
archivebox search 'example'
archivebox update --filter-type=domain example.com
archivebox remove --filter-type=exact 'https://example.com'
```

Docker equivalents:

```bash
docker compose run archivebox init --install
docker compose run archivebox add --depth=1 'https://example.com'
echo 'https://example.com' | docker compose run -T archivebox add
docker compose run -T archivebox list --json --with-headers > index.json
```

Useful subcommands:

- `archivebox init`, `install`, `config`, `status`, `version`, `help`
- `archivebox add`, `update`, `list`, `search`, `remove`, `schedule`
- `archivebox server`, `manage`, `shell`
- Model-oriented commands: `crawl`, `snapshot`, `archiveresult`, `tag`, `binary`, `process`, `machine`, `persona`

## Configuration

ArchiveBox config can come from environment variables, `ArchiveBox.conf`, or `archivebox config --set`.

```bash
archivebox config
archivebox config --get CHROME_BINARY
archivebox config --set TIMEOUT=240
archivebox config --set CHECK_SSL_VALIDITY=False
archivebox config --set PUBLIC_INDEX=False PUBLIC_SNAPSHOTS=False PUBLIC_ADD_VIEW=False
CHROME_BINARY=chromium archivebox add 'https://example.com'
```

Common knobs:

- `TIMEOUT`, `USER_AGENT`, `CHECK_SSL_VALIDITY`
- `PUBLIC_INDEX`, `PUBLIC_SNAPSHOTS`, `PUBLIC_ADD_VIEW`
- `CHROME_BINARY`, `SAVE_WGET`, `SAVE_DOM`, `SAVE_PDF`, `SAVE_SCREENSHOT`
- `LIB_DIR`, `TMP_DIR`

## Admin UI

Create an admin user and start the server:

```bash
archivebox manage createsuperuser
archivebox server 0.0.0.0:8000
```

Docker Compose:

```bash
docker compose run archivebox manage createsuperuser
docker compose up
```

Open:

- Public UI: `http://web.archivebox.localhost:8000`
- Admin UI: `http://admin.archivebox.localhost:8000`
- API docs: `http://web.archivebox.localhost:8000/api/v1/docs`

If custom hostnames are not resolving, try `/admin` and `/api/v1/docs` on the server base URL.

## REST API

The REST API is alpha and served by Django Ninja. Always check the live Swagger docs for the exact schema:

```text
GET /api/v1/docs
GET /api/v1/openapi.json
```

Get an API token:

```bash
curl -sS -X POST 'http://web.archivebox.localhost:8000/api/v1/auth/get_api_token' \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"password"}'
```

Authenticate with any of:

```bash
Authorization: Bearer <token>
X-ArchiveBox-API-Key: <token>
?api_key=<token>
```

Useful endpoints:

- `POST /api/v1/cli/add` mirrors `archivebox add` and queues background work.
- `GET /api/v1/core/snapshots`
- `GET /api/v1/core/snapshot/{snapshot_id}`
- `POST /api/v1/core/snapshots`
- `GET /api/v1/crawls/crawls`
- `POST /api/v1/crawls/crawls`
- `GET /api/v1/machine/binaries`

Example add via API:

```bash
TOKEN='...'
curl -sS -X POST 'http://web.archivebox.localhost:8000/api/v1/cli/add' \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"urls":["https://example.com"],"depth":0,"tag":"agent-run"}'
```

Keep `archivebox server` running to process queued UI/API jobs.

## SQLite Index

The main metadata database is `index.sqlite3` in the data directory. Prefer the ORM for writes. Direct SQLite is useful for read-only inspection, exports, and debugging.

```bash
sqlite3 ./index.sqlite3 '.tables'
sqlite3 ./index.sqlite3 'SELECT COUNT(*) FROM core_snapshot;'
sqlite3 ./index.sqlite3 "SELECT timestamp, url, title, status FROM core_snapshot ORDER BY bookmarked_at DESC LIMIT 20;"
sqlite3 ./index.sqlite3 "SELECT plugin, status, COUNT(*) FROM core_archiveresult GROUP BY plugin, status ORDER BY plugin, status;"
sqlite3 ./index.sqlite3 "SELECT id, status, substr(urls, 1, 120) FROM crawls_crawl ORDER BY created_at DESC LIMIT 20;"
```

Useful tables usually include:

- `core_snapshot`: URL, title, timestamp, status, crawl linkage, output size.
- `core_archiveresult`: extractor/plugin run status and output metadata.
- `core_tag`, `core_snapshottag`: tags and snapshot-tag links.
- `crawls_crawl`: crawl batches, queued URLs, config, status.
- Django tables such as `auth_user` and `django_migrations`.

For large or active archives, avoid long write transactions and keep `index.sqlite3` on local disk/SSD when possible.

## Django Shell

Use `archivebox shell` for interactive ORM access and `archivebox shell -c '...'` for agent-friendly one-liners. Run from the data directory.

Read-only examples:

```bash
archivebox shell -c 'from archivebox.core.models import Snapshot; print(Snapshot.objects.count())'
archivebox shell -c 'from archivebox.core.models import Snapshot; print(list(Snapshot.objects.values("timestamp", "url", "title", "status").order_by("-bookmarked_at")[:10]))'
archivebox shell -c 'from archivebox.core.models import ArchiveResult; print(list(ArchiveResult.objects.values("plugin", "status").order_by("plugin", "status").distinct()))'
archivebox shell -c 'from archivebox.crawls.models import Crawl; print(list(Crawl.objects.values("id", "status", "urls").order_by("-created_at")[:5]))'
```

Safe write examples:

```bash
archivebox shell -c 'from archivebox.core.models import Snapshot; s=Snapshot.objects.get(timestamp="1700000000"); s.notes="reviewed by agent"; s.save(update_fields=["notes", "modified_at"])'
archivebox shell -c 'from archivebox.core.models import Snapshot, Tag; s=Snapshot.objects.get(timestamp="1700000000"); t,_=Tag.objects.get_or_create(name="important"); s.tags.add(t)'
```

Prefer `archivebox add`, the REST API, or model methods for creating crawl/snapshot work. Use direct ORM writes only when you understand the model lifecycle.

## Recommended Agent Workflow

1. Identify the data directory and run `archivebox status`.
2. Use `archivebox config` to confirm privacy, timeout, and browser settings before ingesting sensitive URLs.
3. Add URLs with `archivebox add` or `POST /api/v1/cli/add`.
4. Inspect progress with the Admin UI, `archivebox list`, `archivebox shell -c`, or read-only SQLite queries.
5. Use filesystem outputs under `archive/` for artifacts, but use `index.sqlite3`/ORM/API for authoritative metadata.
