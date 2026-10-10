"""Web UI for the YouTube downloader.

Run:
    .venv/bin/python app.py
Open:
    http://127.0.0.1:5000

Flow: paste a YouTube URL -> preview video or playlist entries ->
pick quality / subtitles -> download with a live progress bar.
Playlists can be grabbed per-video or all at once as a ZIP.

Downloads run in background threads as jobs:
    POST /api/jobs {type: video|zip, ...} -> {job_id}
    GET  /api/jobs/<id>                  -> progress JSON (poll this)
    GET  /api/jobs/<id>/file             -> finished file (video or .zip)
"""
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlparse

from flask import Flask, Response, jsonify, render_template, request

import yt_dlp
from script import (
    QUALITY_FORMATS,
    DownloadTracker,
    build_outtmpl,
    build_ydl_opts,
    cleanup_sidecars,
)

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}

MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".opus"}

MEDIA_MIME = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".opus": "audio/ogg",
    ".zip": "application/zip",
}

MAX_ZIP_ENTRIES = 200
JOB_TTL_SECONDS = 3600

app = Flask(__name__)
# JSON bodies only; media streams back out, so keep the inbound cap small.
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024


class Job:
    """A background download tracked for progress polling."""

    def __init__(self, kind):
        self.id = secrets.token_urlsafe(8)
        self.kind = kind  # "video" or "zip"
        self.status = "queued"  # queued | downloading | zipping | done | error
        self.percent = 0.0
        self.speed = None
        self.eta = None
        self.current = 0  # zip: files finished
        self.total = 1  # zip: files total
        self.current_title = None
        self.failed = []  # zip: titles that failed
        self.filename = None
        self.error = None
        self.created = time.time()
        self.dest = None
        self.filepath = None
        self.lock = threading.Lock()

    def hook(self, d):
        # Called by yt-dlp from the worker thread; plain attribute
        # assignment is enough for polling readers.
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            self.percent = round(done / total * 100, 1) if total else 0.0
            self.speed = d.get("_speed_str")
            self.eta = d.get("_eta_str")
            fn = d.get("filename")
            if fn:
                self.current_title = Path(fn).name
            self.status = "downloading"
        elif status == "finished":
            self.percent = 100.0

    def snapshot(self):
        with self.lock:
            return {
                "job_id": self.id,
                "kind": self.kind,
                "status": self.status,
                "percent": self.percent,
                "speed": self.speed,
                "eta": self.eta,
                "current": self.current,
                "total": self.total,
                "current_title": self.current_title,
                "failed": list(self.failed),
                "filename": self.filename,
                "error": self.error,
            }


jobs = {}
jobs_lock = threading.Lock()


def prune_jobs():
    now = time.time()
    with jobs_lock:
        stale = [jid for jid, j in jobs.items() if now - j.created > JOB_TTL_SECONDS]
        for jid in stale:
            job = jobs.pop(jid)
            if job.dest:
                shutil.rmtree(job.dest, ignore_errors=True)


def log_error(context, exc):
    # Full detail stays in the server log; clients only get generic messages.
    print(f"[downloader] {context}: {exc!r}", file=sys.stderr)


def is_youtube_url(url):
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in YOUTUBE_HOSTS


def entry_thumbnail(entry):
    if entry.get("thumbnail"):
        return entry["thumbnail"]
    vid = entry.get("id")
    if vid:
        return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    return None


def summarize_info(info):
    """Convert raw yt-dlp info into JSON-safe preview data."""
    if not info:
        raise ValueError("Could not read that URL (unsupported, private, or removed).")
    if info.get("_type") == "playlist":
        entries = []
        for e in info.get("entries") or []:
            if not e:
                continue
            vid = e.get("id")
            entries.append({
                "id": vid,
                "title": e.get("title") or vid or "Untitled",
                "duration": e.get("duration"),
                "thumbnail": entry_thumbnail(e),
                "url": e.get("url")
                or (f"https://www.youtube.com/watch?v={vid}" if vid else None),
            })
        return {
            "type": "playlist",
            "title": info.get("title") or "Playlist",
            "count": len(entries),
            "entries": entries,
        }
    return {
        "type": "video",
        "id": info.get("id"),
        "title": info.get("title") or "Untitled",
        "duration": info.get("duration"),
        "thumbnail": entry_thumbnail(info),
        "uploader": info.get("uploader") or info.get("channel"),
    }


def fetch_info(url):
    with yt_dlp.YoutubeDL({
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,  # fast listing; entries carry id/title/url
        "noplaylist": False,
        "ignoreerrors": True,
    }) as ydl:
        return ydl.extract_info(url, download=False)


def ydl_opts_for(dest, job, quality, audio_only, subtitles):
    args = SimpleNamespace(
        quality=quality,
        audio_only=audio_only,
        no_playlist=True,
        out=str(dest),
        keep_subs=False,
        keep_thumbnail=False,
        cookies_from_browser=None,
        no_subs=not subtitles,
    )
    tracker = DownloadTracker()
    opts = build_ydl_opts(args, tracker, build_outtmpl(str(dest), False))
    opts.update({"quiet": True, "no_warnings": True, "consoletitle": False})
    opts["progress_hooks"] = [job.hook]
    return opts


def biggest_media(dest):
    media = sorted(
        (p for p in dest.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_EXTS),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    return media[0] if media else None


def sanitize_name(name, default="download"):
    clean = re.sub(r"[^\w\-. ]", "_", name or "").strip().strip(".")[:100]
    return clean or default


def run_video_job(job, url, quality, audio_only, subtitles):
    dest = Path(tempfile.mkdtemp(prefix="ytui-"))
    job.dest = dest
    try:
        with yt_dlp.YoutubeDL(ydl_opts_for(dest, job, quality, audio_only, subtitles)) as ydl:
            ydl.download([url])
        cleanup_sidecars(str(dest))
        media = biggest_media(dest)
        if not media:
            raise RuntimeError("no file produced")
        with job.lock:
            job.filepath = media
            job.filename = media.name
            job.percent = 100.0
            job.status = "done"
    except Exception as e:
        log_error(f"video job {job.id}", e)
        shutil.rmtree(dest, ignore_errors=True)
        with job.lock:
            job.status = "error"
            job.error = "Download failed. The video may be private, removed, or unavailable."


def run_zip_job(job, entries, playlist_title, quality, audio_only, subtitles):
    dest = Path(tempfile.mkdtemp(prefix="ytui-"))
    job.dest = dest
    try:
        with job.lock:
            job.total = len(entries)
        for i, entry in enumerate(entries):
            with job.lock:
                job.current = i
                job.current_title = entry.get("title") or entry["url"]
                job.percent = 0.0
            subdir = dest / f"part-{i:03d}"
            subdir.mkdir()
            try:
                with yt_dlp.YoutubeDL(
                    ydl_opts_for(subdir, job, quality, audio_only, subtitles)
                ) as ydl:
                    ydl.download([entry["url"]])
                cleanup_sidecars(str(subdir))
                media = biggest_media(subdir)
                if media:
                    # Flatten into the zip root; names already contain [id].
                    media.rename(dest / media.name)
                else:
                    raise RuntimeError("no file produced")
            except Exception as e:
                log_error(f"zip job {job.id} entry {entry['url']}", e)
                with job.lock:
                    # Title only: never expose backend tracebacks/paths.
                    job.failed.append((entry.get("title") or entry["url"])[:200])
            finally:
                shutil.rmtree(subdir, ignore_errors=True)
            with job.lock:
                job.current = i + 1
        with job.lock:
            job.status = "zipping"
            job.current_title = "Packing ZIP…"
        zip_name = sanitize_name(playlist_title, "playlist") + ".zip"
        zip_path = dest / zip_name
        added = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for p in sorted(dest.iterdir()):
                if p.is_file() and p.suffix.lower() in MEDIA_EXTS:
                    zf.write(p, p.name)
                    p.unlink()
                    added += 1
        with job.lock:
            if added == 0:
                # An empty ZIP is still ~22 bytes, so count entries instead.
                zip_path.unlink(missing_ok=True)
                job.status = "error"
                job.error = "All videos failed, so there was nothing to pack."
            else:
                job.filepath = zip_path
                job.filename = zip_name
                job.percent = 100.0
                job.status = "done"
    except Exception as e:
        log_error(f"zip job {job.id}", e)
        with job.lock:
            job.status = "error"
            job.error = "Something went wrong while building your download."
    if job.status == "error":
        shutil.rmtree(dest, ignore_errors=True)


def start_job(kind, target):
    prune_jobs()
    job = Job(kind)
    with jobs_lock:
        jobs[job.id] = job
    t = threading.Thread(target=target, args=(job,), daemon=True)
    t.start()
    return job


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/options")
def options():
    return jsonify({
        "qualities": list(QUALITY_FORMATS),
        "default_quality": "best",
        "default_subtitles": True,
    })


@app.post("/api/info")
def api_info():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Paste a YouTube URL first."}), 400
    if not is_youtube_url(url):
        return jsonify({"error": "Only youtube.com / youtu.be URLs are supported."}), 400
    try:
        return jsonify(summarize_info(fetch_info(url)))
    except ValueError as e:
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        log_error("fetch info", e)
        return jsonify({"error": "Could not fetch that link. Check it and try again."}), 502


@app.post("/api/jobs")
def api_create_job():
    data = request.get_json(force=True, silent=True) or {}
    kind = data.get("type")
    quality = data.get("quality") or "best"
    audio_only = bool(data.get("audio_only"))
    subtitles = bool(data.get("subtitles", True))
    if kind not in ("video", "zip"):
        return jsonify({"error": "type must be 'video' or 'zip'."}), 400
    if quality not in QUALITY_FORMATS:
        return jsonify({"error": f"Unknown quality '{quality}'."}), 400

    if kind == "video":
        url = (data.get("url") or "").strip()
        if not url:
            return jsonify({"error": "Missing video URL."}), 400
        if not is_youtube_url(url):
            return jsonify({"error": "Only youtube.com / youtu.be URLs are supported."}), 400
        job = start_job("video", lambda j: run_video_job(j, url, quality, audio_only, subtitles))
        return jsonify({"job_id": job.id})

    raw = data.get("urls") or []
    entries = [
        {"url": (e.get("url") or "").strip() if isinstance(e, dict) else str(e).strip(),
         "title": e.get("title") if isinstance(e, dict) else None}
        for e in raw
    ]
    entries = [e for e in entries if e["url"] and is_youtube_url(e["url"])]
    if not entries:
        return jsonify({"error": "No valid YouTube URLs supplied."}), 400
    if len(entries) > MAX_ZIP_ENTRIES:
        return jsonify({"error": f"Too many videos (max {MAX_ZIP_ENTRIES})."}), 400
    title = (data.get("playlist_title") or "playlist").strip()
    job = start_job("zip", lambda j: run_zip_job(j, entries, title, quality, audio_only, subtitles))
    return jsonify({"job_id": job.id, "total": len(entries)})


@app.get("/api/jobs/<job_id>")
def api_job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown or expired job."}), 404
    return jsonify(job.snapshot())


@app.get("/api/jobs/<job_id>/file")
def api_job_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown or expired job."}), 404
    snap = job.snapshot()
    if snap["status"] == "error":
        return jsonify({"error": snap["error"] or "Download failed. Please try again."}), 502
    if snap["status"] != "done" or not job.filepath or not job.filepath.is_file():
        return jsonify({"error": "Not ready yet."}), 409
    path, dest, filename = job.filepath, job.dest, job.filename
    size = path.stat().st_size

    # Generator finally deletes the temp dir on full stream, disconnect,
    # and under the Flask test client alike.
    def generate():
        try:
            with open(path, "rb") as f:
                while chunk := f.read(1024 * 64):
                    yield chunk
        finally:
            shutil.rmtree(dest, ignore_errors=True)
            with jobs_lock:
                jobs.pop(job_id, None)

    return Response(
        generate(),
        mimetype=MEDIA_MIME.get(path.suffix.lower(), "application/octet-stream"),
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "Content-Length": str(size),
        },
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
