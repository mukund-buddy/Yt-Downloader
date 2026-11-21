# TuneGrab

A local-first YouTube music downloader with a clean web UI, a double-click launcher, and an API that an AI agent can drive.

Paste a playlist or a single song, choose audio or video and the exact quality, add your own words to the filename, and keep only the title and artist inside the file. Everything runs on your machine — no account, no cloud, no telemetry.

```
Double-click start.bat  ->  http://127.0.0.1:8787  ->  paste links  ->  Download
```

## Why it exists

Most downloaders give you a command line and a folder full of messy tags. TuneGrab is built for a tidy library:

- **Playlists and singles** — a playlist URL expands into every track; a song URL downloads alone. Mix both in one job.
- **Audio or video** — MP3 at 128 / 192 / 256 / 320 kbps, or MP4 at 360p up to 4K.
- **Names you control** — add words before or after the name, or write a full yt-dlp template.
- **Clean files** — tags are stripped to `title` and `artist` only. Cover art, comments, dates, encoder notes and chapters are removed, and file dates are fixed to 2000-01-01.
- **Intro and outro cuts** — SponsorBlock removes intros, outros, self-promos and previews from audio.
- **Agent control** — a JSON API, an SSE progress stream, and a one-shot CLI with JSON events.

## Requirements

| Tool | Needed for | Notes |
|------|------------|-------|
| Python 3.9+ | the engine | `python` must be on PATH |
| yt-dlp | every download | `start.bat` installs it if missing |
| ffmpeg | MP3 conversion, video merge, metadata cleaning, SponsorBlock | install with `winget install Gyan.FFmpeg` |

## Quick start

1. Download or clone this repository.
2. Double-click **`start.bat`**.
3. The app opens at `http://127.0.0.1:8787`.
4. Paste one or more links, pick your options, press **Download**.

The first run installs yt-dlp automatically if it is missing. Keep the console window open while downloading; press `Ctrl+C` there to stop the engine.

### Starting it manually

```bash
python bridge/tunegrab_bridge.py serve --open
```

Options:

```text
--host 127.0.0.1     bind address (use 0.0.0.0 to allow your LAN)
--port 8787          port
--out  PATH          default save folder (default: ~/Music/TuneGrab)
--token SECRET       require X-Tunegrab-Token on every API call
--open               open the browser after starting
```

`start.bat` forwards extra arguments, so this works too:

```bat
start.bat --out "E:\Welcome\Music\TuneGrab" --port 8788
```

## Options in the app

| Option | What it does |
|--------|--------------|
| Format | `Audio` produces MP3, `Video` produces MP4 |
| Quality | Audio: 320, 256, 192, 128 kbps. Video: 2160, 1440, 1080, 720, 480, 360 |
| Words before / after | Literal text added to every filename |
| Name template | yt-dlp output template without extension, default `%(artist,uploader,channel)s - %(title)s` |
| Save folder | Any absolute path; remembered in the browser |
| Max items | Playlist cap, 1–500 (default 200) |
| Cookies file | Netscape-format cookies for private or age-restricted videos |
| Clean metadata | Keep only title and artist, drop covers, chapters and other tags |
| Cut intro and outro | SponsorBlock segments, audio only |
| Fixed file date | Set every file date to 2000-01-01 |
| Engine URL | Point the page at another local port |
| API token | Sent as `X-Tunegrab-Token` when the engine was started with `--token` |

## Agent usage

An agent has two ways in. Both use the same options and the same downloader.

### 1. One-shot CLI

```bash
python bridge/tunegrab_bridge.py add "https://www.youtube.com/playlist?list=..." \
  --mode audio --quality 320 \
  --out "E:\Music" \
  --prefix "Old Hindi" --suffix "slowed + reverb" \
  --json
```

With `--json`, every line is a JSON event (`state`, `resolved`, `item`, `progress`, `done`, `result`), so an agent can read progress without parsing prose. The process exits `0` when every item downloaded and `1` when anything failed.

List what a URL would download, without downloading:

```bash
python bridge/tunegrab_bridge.py resolve "https://www.youtube.com/playlist?list=..." --max-items 50
```

### 2. HTTP API

Start the server, then:

```bash
curl -s -X POST http://127.0.0.1:8787/api/jobs \
  -H "Content-Type: application/json" \
  -d '{"urls":["https://youtu.be/..."],"mode":"audio","quality":"320","prefix":"Old Hindi"}'

curl -s http://127.0.0.1:8787/api/status
```

`/api/status` returns a one-line `summary` such as `Running - 12/25 done, 0 failed, 13 left. Current: ...`, plus machine-readable counts and every job. For live updates, subscribe to `GET /api/jobs/<id>/events` (Server-Sent Events).

Full endpoint list and payloads: [docs/API.md](docs/API.md). Agent playbook: [AGENTS.md](AGENTS.md).

## What lands on disk

```
<save folder>/
  Old Hindi Kishore Kumar - Mere Sapno Ki Rani slowed + reverb.mp3
  ...
```

- Default folder: `%USERPROFILE%\Music\TuneGrab` on Windows, `~/Music/TuneGrab` elsewhere.
- Filenames are sanitized for Windows (`--windows-filenames`).
- With clean metadata on, the only tags are `title` and `artist`. Cover art and chapters are removed.
- File dates are fixed, so nothing leaks the download time or device.
- A dedupe archive lives in `~/.tunegrab/`, never in your music folder.

## Project layout

```
Yt-Downloader/
├── index.html                 web app
├── assets/
│   ├── css/style.css          theme
│   └── js/app.js              UI logic
├── bridge/
│   └── tunegrab_bridge.py     engine, server and API (stdlib only)
├── docs/API.md                endpoint reference
├── AGENTS.md                  agent playbook
├── llms.txt                   short machine index
├── start.bat                  double-click launcher
├── requirements.txt           yt-dlp
└── LICENSE                    Apache-2.0
```

The bridge uses only the Python standard library. yt-dlp and ffmpeg are external programs.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| "Engine offline" in the app | Start `start.bat` and keep the console open |
| `yt-dlp: command not found` | `python -m pip install --user -U yt-dlp` |
| Metadata or cuts did not apply | Install ffmpeg and reopen the terminal so PATH refreshes |
| Port 8787 already in use | `start.bat --port 8788`, then set Engine URL in advanced options |
| Playlist stops early | Raise `Max items`; the default cap is 200 |
| Private or age-restricted video | Export cookies and set the cookies field |
| Download fails with HTTP 403 | Update yt-dlp: `python -m pip install --user -U yt-dlp` |

## Notes

- Download only what you have the rights to keep. Respect creators and local law.
- TuneGrab is a thin, friendly layer over [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [ffmpeg](https://ffmpeg.org/).
- No analytics, no accounts, no network calls except the ones yt-dlp makes to the source site.

## License

Apache License 2.0. See [LICENSE](LICENSE).
