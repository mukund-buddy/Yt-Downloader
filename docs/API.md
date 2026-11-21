# TuneGrab API

The bridge serves the web app and a JSON API from the same origin. Default base URL: `http://127.0.0.1:8787`.

- All responses are JSON except the SSE stream.
- CORS is open (`Access-Control-Allow-Origin: *`) so a page served elsewhere can call the engine.
- If the server was started with `--token SECRET`, every `/api/*` call needs `X-Tunegrab-Token: SECRET` (or `?token=SECRET` for the SSE endpoint, which cannot send headers).

## GET /api/health

```json
{
  "ok": true,
  "name": "TuneGrab",
  "version": "1.0.0",
  "ytdlp": "2026.08.19",
  "ffmpeg": true,
  "out": "C:\\Users\\You\\Music\\TuneGrab"
}
```

`ytdlp` is `null` when the binary is missing. `ffmpeg` is `false` when it is not on PATH.

## GET /api/capabilities

```json
{
  "modes": ["audio", "video"],
  "audio_qualities": ["320", "256", "192", "128", "best"],
  "video_qualities": ["2160", "1440", "1080", "720", "480", "360", "best"],
  "options": { "prefix": "...", "suffix": "...", "clean": "...", "...": "..." }
}
```

## POST /api/jobs

Starts a job in the background and returns immediately.

### Request body

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `urls` | string[] | required | One or more `http(s)` URLs. Playlists are expanded. |
| `url` | string | — | Single-URL shorthand for `urls`. |
| `mode` | `"audio"` \| `"video"` | `"audio"` | Output kind. |
| `quality` | string | `"320"` / `"1080"` | Audio kbps or video height. `"best"` allowed. |
| `out` | string | server `--out` | Absolute save folder. Created if missing. |
| `prefix` | string | `""` | Literal words before the name (max 60 chars). |
| `suffix` | string | `""` | Literal words after the name (max 60 chars). |
| `template` | string | `%(artist,uploader,channel)s - %(title)s` | yt-dlp output template, no extension. |
| `clean` | boolean | `true` | Strip tags to `title` + `artist`, drop covers and chapters. |
| `sponsorblock` | boolean | `true` | Cut intro/outro/selfpromo/preview (audio only). |
| `fixed_mtime` | boolean | `true` | Set file dates to 2000-01-01. |
| `max_items` | integer | `200` | Playlist cap, clamped to 1–500. |
| `cookies` | string | `""` | Path to a Netscape cookies file. |

```json
{
  "urls": ["https://youtu.be/abc123"],
  "mode": "audio",
  "quality": "320",
  "out": "E:\\Music",
  "prefix": "Old Hindi",
  "suffix": "slowed + reverb",
  "clean": true,
  "sponsorblock": true,
  "fixed_mtime": true,
  "max_items": 200
}
```

### Response `202`

```json
{
  "ok": true,
  "job": {
    "id": "j_3f9c1a2b7d",
    "state": "queued",
    "mode": "audio",
    "quality": "320",
    "total": 0,
    "completed": 0,
    "failed": 0,
    "skipped": 0,
    "current": null,
    "summary": "Queued...",
    "items": [],
    "settings": { "...": "..." }
  }
}
```

Invalid URLs or an empty list return `400`.

## GET /api/jobs

```json
{ "ok": true, "jobs": [ { "id": "j_...", "state": "running", "...": "..." } ] }
```

Newest job first. Each job includes its full `items` array.

## GET /api/jobs/{id}

```json
{ "ok": true, "job": { "id": "j_...", "state": "running", "...": "..." } }
```

`404` when the id is unknown.

### Job object

| Field | Type | Meaning |
|-------|------|---------|
| `id` | string | `j_` + 10 hex chars |
| `state` | string | `queued`, `resolving`, `running`, `done`, `failed`, `cancelled` |
| `mode`, `quality` | string | As requested |
| `total` | integer | Items found after playlist expansion |
| `completed`, `failed`, `skipped` | integer | Counters |
| `current` | object \| null | `{ index, total, title, percent }` for the active item |
| `summary` | string | Human-readable one-liner, safe to show to the user |
| `error` | string \| null | Top-level failure reason |
| `settings` | object | Effective options for this job |
| `items` | array | Per-item rows (below) |
| `log` | string[] | Last 400 engine lines |
| `created`, `started`, `ended` | number | Unix seconds |

### Item object

```json
{
  "url": "https://www.youtube.com/watch?v=...",
  "title": "Artist - Song",
  "artist": "Artist",
  "status": "pending | running | done | failed | cancelled",
  "file": "E:\\Music\\Artist - Song.mp3",
  "error": null
}
```

## GET /api/jobs/{id}/events

Server-Sent Events. Every frame is a full job snapshot, so no diffing is needed.

```
event: progress
data: {"id":"j_...","state":"running","completed":12,"total":25,...}

event: progress
data: {...}
```

The stream closes after `state` becomes `done`, `failed` or `cancelled`. Reconnect or fall back to polling `GET /api/jobs/{id}`.

Browser example:

```js
const stream = new EventSource("http://127.0.0.1:8787/api/jobs/j_123/events");
stream.addEventListener("progress", (event) => {
  const job = JSON.parse(event.data);
  console.log(job.summary);
  if (["done", "failed", "cancelled"].includes(job.state)) stream.close();
});
```

## POST /api/jobs/{id}/cancel

Sets the cancel flag and terminates the active yt-dlp process. The job ends in `cancelled`.

```json
{ "ok": true, "job": { "id": "j_...", "state": "running", "...": "..." } }
```

## DELETE /api/jobs/{id}

Forgets a finished job. Returns `409` while the job is `queued`, `resolving` or `running`.

```json
{ "ok": true }
```

## GET /api/status

The one call an agent needs.

```json
{
  "ok": true,
  "active_jobs": 1,
  "jobs": [ { "id": "j_...", "state": "running", "...": "..." } ],
  "totals": { "completed": 12, "failed": 0, "total": 25 },
  "summary": "Running - 12/25 done, 0 failed, 13 left. Current: Song name (63%)",
  "hint": "GET /api/jobs/<id>/events for a live SSE stream"
}
```

When no job exists, `summary` is `"No jobs yet."`.

## Errors

Every error uses the same shape:

```json
{ "ok": false, "error": "unsupported URL: ftp://..." }
```

| Status | Meaning |
|--------|---------|
| `400` | invalid JSON, no URLs, unsupported URL |
| `401` | token missing or wrong |
| `403` | static path escaped the project root |
| `404` | unknown job or endpoint |
| `409` | delete attempted on an active job |

## Static files

`GET /` and any other non-API path serve files from the project root (`index.html`, `assets/`, `docs/`, `AGENTS.md`, `README.md`). Paths are confined to the project directory.
