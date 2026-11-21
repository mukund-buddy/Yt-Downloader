#!/usr/bin/env python3
"""TuneGrab bridge - local download engine, web host and agent API.

Runs a small HTTP server that serves the TuneGrab UI and exposes a JSON/SSE
API for agents. Downloads are performed by yt-dlp + ffmpeg.

Commands:
    serve     Start the local web app (default)
    add       Run one download job and stream JSON progress
    resolve   List the items a URL would download (no download)

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

APP_NAME = "TuneGrab"
VERSION = "1.0.0"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(os.path.expanduser("~"), "Music", "TuneGrab")
SERVER_OUT = DEFAULT_OUT
FIXED_MTIME = 946684800  # 2000-01-01 UTC, deterministic file dates
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
SERVER_TOKEN = ""
ITEM_TIMEOUT = 1800

PROGRESS_RE = re.compile(r"\[download\]\s+([\d.]+)%")
BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def log(msg: str) -> None:
    sys.stdout.write(str(msg) + "\n")
    sys.stdout.flush()


def ytdlp_cmd() -> list[str]:
    exe = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if exe:
        return [exe]
    return [sys.executable, "-m", "yt_dlp"]


def ffmpeg_exe() -> str | None:
    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")


def clean_words(text: str | None, limit: int = 60) -> str:
    text = BAD_CHARS.sub("", (text or "").replace("%", "")).strip(" .")
    return text[:limit].strip()


def run_capture(cmd: list[str], timeout: int = 240) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as exc:
        return 127, "", str(exc)


def ytdlp_version() -> str | None:
    code, out, _ = run_capture(ytdlp_cmd() + ["--version"], timeout=30)
    return out.strip().splitlines()[0] if code == 0 and out.strip() else None


def safe_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except ValueError:
        return False


def resolve_items(urls: list[str], max_items: int) -> list[dict]:
    """Expand playlists into flat item rows without downloading media."""
    items: list[dict] = []
    seen: set[str] = set()
    for url in urls:
        cmd = ytdlp_cmd() + [
            "--flat-playlist", "--dump-single-json", "--no-warnings",
            "--playlist-end", str(max_items), url,
        ]
        code, out, err = run_capture(cmd, timeout=240)
        data = None
        if code == 0 and out.strip():
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                data = None
        if not isinstance(data, dict):
            candidate = url
            if candidate not in seen:
                seen.add(candidate)
                items.append({
                    "url": candidate, "title": url, "artist": "",
                    "status": "pending", "file": None, "error": None,
                })
            continue
        entries = data.get("entries")
        if entries:
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                link = entry.get("webpage_url") or entry.get("url") or ""
                if not str(link).startswith("http"):
                    vid = entry.get("id")
                    if not vid:
                        continue
                    link = "https://www.youtube.com/watch?v=" + str(vid)
                if link in seen:
                    continue
                seen.add(link)
                items.append({
                    "url": link,
                    "title": entry.get("title") or link,
                    "artist": entry.get("artist") or entry.get("uploader")
                    or entry.get("channel") or "",
                    "status": "pending", "file": None, "error": None,
                })
                if len(items) >= max_items:
                    break
        else:
            link = data.get("webpage_url") or url
            if link not in seen:
                seen.add(link)
                items.append({
                    "url": link,
                    "title": data.get("title") or link,
                    "artist": data.get("artist") or data.get("uploader")
                    or data.get("channel") or "",
                    "status": "pending", "file": None, "error": None,
                })
        if len(items) >= max_items:
            break
    return items[:max_items]


def build_template(prefix: str, suffix: str, template: str) -> str:
    base = template.strip() or "%(artist,uploader,channel)s - %(title)s"
    if prefix:
        base = prefix + " " + base
    if suffix:
        base = base + " " + suffix
    return base


def job_log(job: dict, line: str) -> None:
    line = line.strip()
    if line:
        job["log"].append(line[-500:])


def job_summary(job: dict) -> str:
    state = job["state"]
    done, failed = job["completed"], job["failed"]
    skipped = job.get("skipped") or 0
    total = job["total"]
    extra = f", {skipped} skipped" if skipped else ""
    if state == "done":
        return f"Done - {done} downloaded{extra}, {failed} failed."
    if state == "cancelled":
        return f"Cancelled - {done} downloaded{extra}, {failed} failed."
    if state == "failed":
        return f"Failed - {job.get('error') or 'unknown error'}"
    if state == "resolving":
        return "Resolving links..."
    if state == "queued":
        return "Queued..."
    current = job.get("current") or {}
    pct = current.get("percent")
    now = f" Current: {current.get('title')}" if current.get("title") else ""
    if pct is not None:
        now += f" ({pct:.0f}%)"
    left = max(total - done - failed - skipped, 0)
    return f"Running - {done}/{total} done{extra}, {failed} failed, {left} left.{now}"


def snapshot(job: dict) -> dict:
    return {
        "id": job["id"],
        "state": job["state"],
        "mode": job["mode"],
        "quality": job["quality"],
        "total": job["total"],
        "completed": job["completed"],
        "failed": job["failed"],
        "skipped": job["skipped"],
        "current": job.get("current"),
        "summary": job_summary(job),
        "error": job.get("error"),
        "settings": job.get("settings"),
        "items": list(job.get("items") or []),
        "log": list(job.get("log") or []),
        "created": job.get("created"),
        "started": job.get("started"),
        "ended": job.get("ended"),
    }


def scrub_metadata(path: str, title: str, artist: str) -> None:
    """Remove every tag except title and artist, and drop cover art."""
    ffmpeg = ffmpeg_exe()
    if not ffmpeg or not os.path.exists(path):
        return
    ext = os.path.splitext(path)[1].lower()
    audio_only = ext in (".mp3", ".m4a", ".opus", ".ogg", ".flac", ".wav", ".aac")
    tmp = path + ".tg-tmp" + ext
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", path]
    if audio_only:
        cmd += ["-map", "0:a:0"]
    else:
        cmd += ["-map", "0:V:0?", "-map", "0:a?"]
    cmd += ["-c", "copy", "-map_metadata", "-1", "-map_chapters", "-1"]
    if title:
        cmd += ["-metadata", "title=" + title]
    if artist:
        cmd += ["-metadata", "artist=" + artist]
    if ext == ".mp3":
        cmd += ["-id3v2_version", "3", "-write_xing", "0"]
    cmd += ["-fflags", "+bitexact", tmp]
    code, _, _ = run_capture(cmd, timeout=180)
    if code == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        try:
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
    elif os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass


def read_first_line(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    return line
    except OSError:
        pass
    return ""


def find_new_file(outdir: str, since: float, exts: tuple[str, ...]) -> str | None:
    newest, newest_mtime = None, since - 5
    try:
        for entry in os.scandir(outdir):
            if not entry.is_file():
                continue
            name = entry.name
            if name.startswith(".") or name.endswith((".part", ".ytdl", ".tmp")):
                continue
            if not name.lower().endswith(exts):
                continue
            mtime = entry.stat().st_mtime
            if mtime >= newest_mtime:
                newest, newest_mtime = entry.path, mtime
    except OSError:
        return None
    return newest


def download_item(job: dict, item: dict, opts: dict) -> dict:
    outdir = opts["out"]
    os.makedirs(outdir, exist_ok=True)
    template = build_template(opts["prefix"], opts["suffix"], opts["template"])
    token = uuid.uuid4().hex[:8]
    path_file = os.path.join(outdir, f".tg-path-{token}.txt")
    meta_file = os.path.join(outdir, f".tg-meta-{token}.txt")
    archive = os.path.join(
        os.path.expanduser("~"), ".tunegrab",
        "archive-" + str(abs(hash(os.path.normcase(outdir))))[:12] + ".txt",
    )
    os.makedirs(os.path.dirname(archive), exist_ok=True)

    cmd = ytdlp_cmd() + [
        "--no-playlist", "--windows-filenames", "--no-mtime",
        "--no-write-info-json", "--no-write-thumbnail", "--no-embed-thumbnail",
        "--newline", "--no-warnings", "--ignore-config",
        "--download-archive", archive,
        "--print-to-file", "after_move:filepath", path_file,
        "--print-to-file", "after_move:%(title)s\t%(artist,uploader,channel)s", meta_file,
        "-o", os.path.join(outdir, template + ".%(ext)s"),
    ]
    if opts["mode"] == "audio":
        quality = opts["quality"]
        cmd += ["-x", "--audio-format", "mp3"]
        cmd += ["--audio-quality", "320K" if quality == "best" else f"{quality}K"]
        if opts["sponsorblock"]:
            cmd += ["--sponsorblock-remove", "intro,outro,selfpromo,preview"]
    else:
        height = 9999 if opts["quality"] == "best" else int(opts["quality"])
        cmd += [
            "-f", f"bv*[height<={height}]+ba/b[height<={height}]/b",
            "--merge-output-format", "mp4",
        ]
    if opts.get("cookies"):
        cmd += ["--cookies", opts["cookies"]]
    cmd += [item["url"]]

    job_log(job, "$ " + " ".join(cmd))
    started = time.time()
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except OSError as exc:
        item["status"] = "failed"
        item["error"] = str(exc)
        return item

    job["proc"] = proc
    last_error = ""
    skipped_path = ""
    watchdog = threading.Timer(ITEM_TIMEOUT, lambda: proc.poll() is None and proc.kill())
    watchdog.daemon = True
    watchdog.start()
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip()
            if not line:
                continue
            if line.startswith("ERROR:"):
                last_error = line[6:].strip()
                job_log(job, line)
            elif "has already been downloaded" in line:
                skipped_path = re.sub(
                    r"^\[download\]\s+(.*?)\s+has already been downloaded.*$",
                    r"\1", line,
                )
                job_log(job, line)
            elif line.startswith("[download]"):
                match = PROGRESS_RE.search(line)
                if match:
                    current = job.get("current") or {}
                    current["percent"] = float(match.group(1))
                    job["current"] = current
            elif line.startswith("[ExtractAudio]") or line.startswith("[SponsorBlock]") \
                    or line.startswith("[Merger]") or line.startswith("[Metadata]"):
                job_log(job, line)
            if job.get("cancel"):
                proc.kill()
                break
    finally:
        watchdog.cancel()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        job["proc"] = None

    if job.get("cancel"):
        item["status"] = "cancelled"
        return item

    if skipped_path and os.path.exists(skipped_path):
        for leftover in (path_file, meta_file):
            try:
                os.remove(leftover)
            except OSError:
                pass
        item["status"] = "skipped"
        item["file"] = skipped_path
        return item

    path = read_first_line(path_file)
    if not path or not os.path.exists(path):
        ext = (".mp3",) if opts["mode"] == "audio" else (".mp4", ".mkv", ".webm")
        path = find_new_file(outdir, started, ext)
    for leftover in (path_file, meta_file):
        try:
            os.remove(leftover)
        except OSError:
            pass

    meta_line = read_first_line(meta_file)
    title, artist = item.get("title") or "", item.get("artist") or ""
    if meta_line:
        parts = meta_line.split("\t")
        if parts and parts[0].strip():
            title = parts[0].strip()
        if len(parts) > 1 and parts[1].strip():
            artist = parts[1].strip()

    if not path or not os.path.exists(path):
        item["status"] = "failed"
        item["error"] = last_error or "download produced no file"
        return item

    if opts["clean"]:
        scrub_metadata(path, title, artist)
    if opts["fixed_mtime"]:
        try:
            os.utime(path, (FIXED_MTIME, FIXED_MTIME))
        except OSError:
            pass

    item["status"] = "done"
    item["file"] = path
    item["title"] = title or os.path.basename(path)
    item["artist"] = artist
    return item


def run_job(job: dict, opts: dict, emit=None) -> None:
    def send(event: str, **fields) -> None:
        if emit:
            emit({"event": event, **fields})

    job["state"] = "resolving"
    job["started"] = time.time()
    send("state", state="resolving")
    try:
        items = resolve_items(opts["urls"], opts["max_items"])
    except Exception as exc:  # noqa: BLE001
        job["state"] = "failed"
        job["error"] = f"resolve failed: {exc}"
        job["ended"] = time.time()
        send("done", summary=job_summary(job), error=job["error"])
        return

    if not items:
        job["state"] = "failed"
        job["error"] = "no downloadable items found"
        job["ended"] = time.time()
        send("done", summary=job_summary(job), error=job["error"])
        return

    job["items"] = items
    job["total"] = len(items)
    job["state"] = "running"
    send("resolved", total=job["total"])

    for index, item in enumerate(items, start=1):
        if job.get("cancel"):
            break
        job["current"] = {
            "index": index, "total": job["total"],
            "title": item.get("title"), "percent": 0.0,
        }
        item["status"] = "running"
        send("item", index=index, total=job["total"], status="running",
             title=item.get("title"))
        try:
            download_item(job, item, opts)
        except Exception as exc:  # noqa: BLE001
            item["status"] = "failed"
            item["error"] = str(exc)
        if item["status"] == "done":
            job["completed"] += 1
        elif item["status"] == "failed":
            job["failed"] += 1
        elif item["status"] == "skipped":
            job["skipped"] += 1
        send("item", index=index, total=job["total"], status=item["status"],
             title=item.get("title"), file=item.get("file"), error=item.get("error"))
        send("progress", summary=job_summary(job), completed=job["completed"],
             failed=job["failed"], total=job["total"])

    job["current"] = None
    job["state"] = "cancelled" if job.get("cancel") else "done"
    job["ended"] = time.time()
    send("done", summary=job_summary(job), completed=job["completed"],
         failed=job["failed"], total=job["total"])


def normalize_opts(data: dict, base_out: str) -> dict:
    urls = data.get("urls")
    if not isinstance(urls, list):
        urls = [data.get("url")] if data.get("url") else []
    urls = [str(u).strip() for u in urls if isinstance(u, str) and str(u).strip()]
    bad = [u for u in urls if not safe_url(u)]
    if bad:
        raise ValueError("unsupported URL: " + bad[0])
    if not urls:
        raise ValueError("no URLs given")
    mode = "video" if data.get("mode") == "video" else "audio"
    quality = str(data.get("quality") or ("1080" if mode == "video" else "320"))
    if mode == "audio" and quality not in ("best", "320", "256", "192", "128"):
        quality = "320"
    if mode == "video" and quality not in ("best", "2160", "1440", "1080", "720", "480", "360"):
        quality = "1080"
    out = os.path.abspath(os.path.expanduser(str(data.get("out") or base_out)))
    try:
        max_items = int(data.get("max_items") or 200)
    except (TypeError, ValueError):
        max_items = 200
    max_items = max(1, min(500, max_items))
    return {
        "urls": urls,
        "mode": mode,
        "quality": quality,
        "out": out,
        "prefix": clean_words(data.get("prefix")),
        "suffix": clean_words(data.get("suffix")),
        "template": str(data.get("template") or "").strip(),
        "clean": bool(data.get("clean", True)),
        "sponsorblock": bool(data.get("sponsorblock", True)) and mode == "audio",
        "fixed_mtime": bool(data.get("fixed_mtime", True)),
        "max_items": max_items,
        "cookies": str(data.get("cookies") or "").strip(),
    }


def new_job(opts: dict) -> dict:
    job = {
        "id": "j_" + uuid.uuid4().hex[:10],
        "created": time.time(),
        "state": "queued",
        "mode": opts["mode"],
        "quality": opts["quality"],
        "total": 0, "completed": 0, "failed": 0, "skipped": 0,
        "current": None, "items": [], "log": deque(maxlen=400),
        "error": None, "started": None, "ended": None,
        "cancel": False, "proc": None,
        "settings": {k: opts[k] for k in (
            "mode", "quality", "out", "prefix", "suffix", "template",
            "clean", "sponsorblock", "fixed_mtime", "max_items")},
    }
    with JOBS_LOCK:
        JOBS[job["id"]] = job
    return job


def start_job_async(opts: dict) -> dict:
    job = new_job(opts)
    thread = threading.Thread(target=run_job, args=(job, opts), daemon=True)
    thread.start()
    return job


class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{VERSION}"

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Tunegrab-Token")
        self.send_header("Access-Control-Max-Age", "86400")

    def _json(self, payload, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, query: dict) -> bool:
        if not SERVER_TOKEN:
            return True
        token = self.headers.get("X-Tunegrab-Token") or (query.get("token") or [""])[0]
        return token == SERVER_TOKEN

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path.startswith("/api/") and not self._authorized(query):
            self._json({"ok": False, "error": "unauthorized"}, 401)
            return
        if path == "/api/health":
            self._json({
                "ok": True, "name": APP_NAME, "version": VERSION,
                "ytdlp": ytdlp_version(), "ffmpeg": bool(ffmpeg_exe()),
                "out": SERVER_OUT,
            })
            return
        if path == "/api/capabilities":
            self._json({
                "modes": ["audio", "video"],
                "audio_qualities": ["320", "256", "192", "128", "best"],
                "video_qualities": ["2160", "1440", "1080", "720", "480", "360", "best"],
                "options": {
                    "prefix": "words added before the name",
                    "suffix": "words added after the name",
                    "template": "yt-dlp output template without extension",
                    "clean": "keep only title and artist metadata",
                    "sponsorblock": "cut intro/outro/selfpromo/preview (audio)",
                    "fixed_mtime": "set a fixed 2000-01-01 file date",
                    "max_items": "playlist item cap (1-500)",
                    "out": "absolute output folder",
                },
            })
            return
        if path == "/api/status":
            with JOBS_LOCK:
                jobs = [snapshot(job) for job in JOBS.values()]
            jobs.sort(key=lambda row: row.get("created") or 0, reverse=True)
            active = [row for row in jobs if row["state"] in ("queued", "resolving", "running")]
            totals = {
                "completed": sum(row["completed"] for row in jobs),
                "failed": sum(row["failed"] for row in jobs),
                "total": sum(row["total"] for row in jobs),
            }
            summary = active[0]["summary"] if active else (
                jobs[0]["summary"] if jobs else "No jobs yet.")
            self._json({
                "ok": True, "active_jobs": len(active), "jobs": jobs,
                "totals": totals, "summary": summary,
                "hint": "GET /api/jobs/<id>/events for a live SSE stream",
            })
            return
        if path == "/api/jobs":
            with JOBS_LOCK:
                jobs = [snapshot(job) for job in JOBS.values()]
            jobs.sort(key=lambda row: row.get("created") or 0, reverse=True)
            self._json({"ok": True, "jobs": jobs})
            return
        match = re.match(r"^/api/jobs/([A-Za-z0-9_]+)(/events)?$", path)
        if match:
            job = JOBS.get(match.group(1))
            if not job:
                self._json({"ok": False, "error": "job not found"}, 404)
                return
            if match.group(2):
                self._stream(job)
            else:
                self._json({"ok": True, "job": snapshot(job)})
            return
        self._static(path)

    def _stream(self, job: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._cors()
        self.end_headers()
        try:
            while True:
                payload = json.dumps(snapshot(job), ensure_ascii=False)
                self.wfile.write(f"event: progress\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                if job["state"] in ("done", "failed", "cancelled"):
                    break
                time.sleep(0.7)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            self.close_connection = True

    def _static(self, path: str) -> None:
        rel = unquote(path).lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(ROOT, rel))
        if not full.startswith(ROOT):
            self._json({"ok": False, "error": "forbidden"}, 403)
            return
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            self._json({"ok": False, "error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in (
                "application/javascript", "application/json", "image/svg+xml"):
            ctype += "; charset=utf-8"
        try:
            with open(full, "rb") as handle:
                body = handle.read()
        except OSError:
            self._json({"ok": False, "error": "read failed"}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorized(query):
            self._json({"ok": False, "error": "unauthorized"}, 401)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json({"ok": False, "error": "invalid JSON"}, 400)
            return
        if parsed.path == "/api/jobs":
            try:
                opts = normalize_opts(data, SERVER_OUT)
            except ValueError as exc:
                self._json({"ok": False, "error": str(exc)}, 400)
                return
            job = start_job_async(opts)
            self._json({"ok": True, "job": snapshot(job)}, 202)
            return
        match = re.match(r"^/api/jobs/([A-Za-z0-9_]+)/cancel$", parsed.path)
        if match:
            job = JOBS.get(match.group(1))
            if not job:
                self._json({"ok": False, "error": "job not found"}, 404)
                return
            job["cancel"] = True
            proc = job.get("proc")
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
            self._json({"ok": True, "job": snapshot(job)})
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorized(query):
            self._json({"ok": False, "error": "unauthorized"}, 401)
            return
        match = re.match(r"^/api/jobs/([A-Za-z0-9_]+)$", parsed.path)
        if not match:
            self._json({"ok": False, "error": "not found"}, 404)
            return
        job = JOBS.get(match.group(1))
        if not job:
            self._json({"ok": False, "error": "job not found"}, 404)
            return
        if job["state"] in ("queued", "resolving", "running"):
            self._json({"ok": False, "error": "job is active; cancel first"}, 409)
            return
        with JOBS_LOCK:
            JOBS.pop(job["id"], None)
        self._json({"ok": True})


def cmd_serve(args: argparse.Namespace) -> int:
    global SERVER_TOKEN, SERVER_OUT
    SERVER_TOKEN = args.token or ""
    SERVER_OUT = os.path.abspath(os.path.expanduser(args.out or DEFAULT_OUT))
    host, port = args.host, args.port
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/"
    log(f"{APP_NAME} {VERSION}")
    log(f"  app    {url}")
    log(f"  out    {SERVER_OUT}")
    log(f"  ytdlp  {ytdlp_version() or 'NOT FOUND - run: pip install -U yt-dlp'}")
    log(f"  ffmpeg {ffmpeg_exe() or 'NOT FOUND'}")
    if SERVER_TOKEN:
        log("  token  required (X-Tunegrab-Token)")
    log("  stop   Ctrl+C")
    if args.open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopped")
    finally:
        server.server_close()
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    data = {
        "urls": args.urls,
        "mode": args.mode,
        "quality": args.quality,
        "out": args.out,
        "prefix": args.prefix,
        "suffix": args.suffix,
        "template": args.template,
        "clean": not args.no_clean,
        "sponsorblock": not args.no_sponsorblock,
        "fixed_mtime": not args.no_fixed_mtime,
        "max_items": args.max_items,
        "cookies": args.cookies,
    }
    try:
        opts = normalize_opts(data, DEFAULT_OUT)
    except ValueError as exc:
        log(json.dumps({"event": "error", "error": str(exc)}))
        return 2

    job = new_job(opts)

    def emit(event: dict) -> None:
        if args.json:
            log(json.dumps(event, ensure_ascii=False))
        else:
            kind = event.get("event")
            if kind == "resolved":
                log(f"resolved: {event.get('total')} item(s)")
            elif kind == "item":
                log(f"[{event.get('index')}/{event.get('total')}] "
                    f"{event.get('status')} {event.get('title') or ''}")
            elif kind == "done":
                log(event.get("summary") or "done")

    run_job(job, opts, emit)
    if args.json:
        log(json.dumps({"event": "result", "job": snapshot(job)}, ensure_ascii=False))
    return 0 if job["state"] == "done" and job["failed"] == 0 else 1


def cmd_resolve(args: argparse.Namespace) -> int:
    items = resolve_items([args.url], args.max_items)
    log(json.dumps({"ok": True, "total": len(items), "items": items},
                   ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tunegrab_bridge",
        description=f"{APP_NAME} local bridge - download music with yt-dlp.",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="start the local web app (default)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--out", default=DEFAULT_OUT, help="default output folder")
    serve.add_argument("--open", action="store_true", help="open the browser")
    serve.add_argument("--token", default="", help="require this API token")
    serve.set_defaults(func=cmd_serve)

    add = sub.add_parser("add", help="run one download job (agent friendly)")
    add.add_argument("urls", nargs="+")
    add.add_argument("--mode", choices=["audio", "video"], default="audio")
    add.add_argument("--quality", default="320")
    add.add_argument("--out", default=DEFAULT_OUT)
    add.add_argument("--prefix", default="")
    add.add_argument("--suffix", default="")
    add.add_argument("--template", default="")
    add.add_argument("--no-clean", action="store_true")
    add.add_argument("--no-sponsorblock", action="store_true")
    add.add_argument("--no-fixed-mtime", action="store_true")
    add.add_argument("--max-items", type=int, default=200)
    add.add_argument("--cookies", default="")
    add.add_argument("--json", action="store_true")
    add.set_defaults(func=cmd_add)

    resolve = sub.add_parser("resolve", help="list items for a URL")
    resolve.add_argument("url")
    resolve.add_argument("--max-items", type=int, default=200)
    resolve.set_defaults(func=cmd_resolve)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv = ["serve"] + argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
