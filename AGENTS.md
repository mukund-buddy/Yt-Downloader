# AGENTS.md

Instructions for an AI agent using TuneGrab to download music. Read this before running anything.

## What this project is

TuneGrab is a local downloader. `bridge/tunegrab_bridge.py` is the engine: it serves the web UI, wraps yt-dlp and ffmpeg, and exposes a JSON/SSE API. There is no cloud service.

## Preconditions

Check these once, then proceed:

```bash
python --version                 # 3.9 or newer
yt-dlp --version                 # if missing: python -m pip install --user -U yt-dlp
ffmpeg -version                  # needed for MP3, metadata cleaning, cuts, video merge
```

If ffmpeg is missing, downloads still work for some formats but clean metadata, SponsorBlock cuts and video merging do not. Tell the user instead of silently skipping those options.

## Choose a path

| Situation | Use |
|-----------|-----|
| One job, no server running | **One-shot CLI** (`add`) |
| Several jobs, or the user is watching the UI | **HTTP API** (`serve` + `POST /api/jobs`) |
| Only want to know what a link contains | `resolve` |
| User double-clicked `start.bat` | HTTP API at `http://127.0.0.1:8787` |

Never start a second server on the same port. If `GET /api/health` answers, reuse it.

## One-shot CLI

```bash
python bridge/tunegrab_bridge.py add <URL> [<URL> ...] [options]
```

| Flag | Values | Default |
|------|--------|---------|
| `--mode` | `audio`, `video` | `audio` |
| `--quality` | audio `320 256 192 128 best`; video `2160 1440 1080 720 480 360 best` | `320` audio, `1080` video |
| `--out` | absolute folder | `~/Music/TuneGrab` |
| `--prefix` | words before the filename | none |
| `--suffix` | words after the filename | none |
| `--template` | yt-dlp output template, no extension | `%(artist,uploader,channel)s - %(title)s` |
| `--no-clean` | keep original metadata | clean is on |
| `--no-sponsorblock` | do not cut intros/outros | cuts are on (audio) |
| `--no-fixed-mtime` | keep real file dates | fixed date is on |
| `--max-items` | playlist cap, 1–500 | `200` |
| `--cookies` | Netscape cookies file | none |
| `--json` | emit one JSON object per line | off |

Exit codes: `0` all items downloaded, `1` at least one failure, `2` invalid arguments.

### JSON event stream

Each line is one JSON object:

```json
{"event":"state","state":"resolving"}
{"event":"resolved","total":25}
{"event":"item","index":1,"total":25,"status":"running","title":"..."}
{"event":"item","index":1,"total":25,"status":"done","title":"...","file":"E:\\Music\\Artist - Song.mp3"}
{"event":"progress","summary":"Running - 1/25 done, 0 failed, 24 left.","completed":1,"failed":0,"total":25}
{"event":"done","summary":"Done - 25 downloaded, 0 failed.","completed":25,"failed":0,"total":25}
{"event":"result","job":{...}}
```

Read `item` events for per-song results and `done`/`result` for the final state. Do not parse the human-readable summary as data.

### Resolve first when the user is unsure

```bash
python bridge/tunegrab_bridge.py resolve "<URL>" --max-items 50
```

Returns `{ "ok": true, "total": N, "items": [ { "url", "title", "artist" } ] }`. Use it to confirm a playlist is the right one before downloading 200 files.

## HTTP API

Start the server (skip if already running):

```bash
python bridge/tunegrab_bridge.py serve --out "E:\Music"
```

Base URL: `http://127.0.0.1:8787`. If the server was started with `--token`, send `X-Tunegrab-Token: <token>` on every API call.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/health` | engine, yt-dlp and ffmpeg status |
| GET | `/api/capabilities` | valid modes, qualities and options |
| GET | `/api/status` | one-call progress summary for every job |
| GET | `/api/jobs` | all jobs |
| POST | `/api/jobs` | start a job |
| GET | `/api/jobs/<id>` | one job |
| GET | `/api/jobs/<id>/events` | live SSE stream |
| POST | `/api/jobs/<id>/cancel` | cancel a running job |
| DELETE | `/api/jobs/<id>` | forget a finished job |

Start a job:

```bash
curl -s -X POST http://127.0.0.1:8787/api/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "urls": ["https://www.youtube.com/playlist?list=..."],
    "mode": "audio",
    "quality": "320",
    "out": "E:\\Music",
    "prefix": "Old Hindi",
    "suffix": "slowed + reverb",
    "clean": true,
    "sponsorblock": true,
    "fixed_mtime": true,
    "max_items": 200
  }'
```

Response `202`: `{ "ok": true, "job": { "id": "j_...", "state": "queued", ... } }`.

Poll progress:

```bash
curl -s http://127.0.0.1:8787/api/status
```

```json
{
  "ok": true,
  "active_jobs": 1,
  "summary": "Running - 12/25 done, 0 failed, 13 left. Current: Song name (63%)",
  "totals": { "completed": 12, "failed": 0, "total": 25 },
  "jobs": [ { "id": "j_...", "state": "running", "items": [ ... ] } ]
}
```

Report `summary` to the user verbatim, or use `completed` / `failed` / `total` for structured updates.

## Progress reporting rules

1. After starting a job, immediately tell the user the item count (from `resolved` or `total`).
2. Poll `/api/status` every 5–15 seconds, or read the CLI JSON stream. Do not busy-loop.
3. Report when `state` becomes `done`, `failed` or `cancelled`.
4. On success, list a few finished file paths from `items[].file`, not every path.
5. On failure, quote `items[].error` or `job.error` and suggest the fix (usually updating yt-dlp or installing ffmpeg).

## Rules for filenames and metadata

- Default name is `Artist - Title`. Add the user's words with `--prefix` / `--suffix`, not by renaming files afterwards.
- Keep `clean` on unless the user asks otherwise. It leaves only `title` and `artist` tags.
- Keep `fixed_mtime` on unless the user asks otherwise. It sets 2000-01-01 so no download time is stored.
- Never add tags, comments, cover art, download dates or device names yourself.
- Do not move or rename files after download unless the user explicitly asks.

## Guardrails

- Only `http` and `https` URLs are accepted.
- Respect the playlist cap; ask before raising `max_items` above 200.
- Do not run two jobs into the same folder at the same time.
- Do not edit files inside `bridge/` while a job is running.
- Do not claim a file exists until `items[].status` is `done` and `file` is set.
- If the engine reports `ffmpeg: false`, warn that metadata cleaning and cuts will be skipped before starting.
- Downloads are the user's responsibility; do not fetch content the user has no right to keep.

## Typical session

```bash
# 1. is the engine up?
curl -s http://127.0.0.1:8787/api/health

# 2. inspect the playlist
python bridge/tunegrab_bridge.py resolve "https://youtube.com/playlist?list=..." --max-items 50

# 3. download it
python bridge/tunegrab_bridge.py add "https://youtube.com/playlist?list=..." \
  --mode audio --quality 320 --out "E:\Music" \
  --prefix "Old Hindi" --json

# 4. report the final "done" event and a few file paths
```

If the user prefers the browser, start `serve --open` and tell them the URL; the UI exposes the same options.
