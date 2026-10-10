"""Web UI for the YouTube downloader.

Run:
    .venv/bin/python app.py
Open:
    http://127.0.0.1:5000

Flow: paste a YouTube URL -> preview video or playlist entries ->
pick quality / subtitles -> download straight to the browser.
"""
import shutil
import tempfile
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
}

app = Flask(__name__)
# JSON bodies only; media streams back out, so keep the inbound cap small.
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024


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


def download_single(url, quality, audio_only, subtitles):
    """Download one video to a temp dir. Returns (tempdir, media_path)."""
    dest = Path(tempfile.mkdtemp(prefix="ytui-"))
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
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    cleanup_sidecars(str(dest))
    media = sorted(
        (p for p in dest.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_EXTS),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    if not media:
        detail = "; ".join(tracker.errors[:3]) or "no file produced"
        raise RuntimeError(f"Download failed ({detail})")
    return dest, media[0]


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
        return jsonify({"error": f"Could not fetch that URL: {e}"}), 502


@app.post("/api/download")
def api_download():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    quality = data.get("quality") or "best"
    audio_only = bool(data.get("audio_only"))
    subtitles = bool(data.get("subtitles", True))
    if not url:
        return jsonify({"error": "Missing video URL."}), 400
    if not is_youtube_url(url):
        return jsonify({"error": "Only youtube.com / youtu.be URLs are supported."}), 400
    if quality not in QUALITY_FORMATS:
        return jsonify({"error": f"Unknown quality '{quality}'."}), 400
    try:
        dest, media = download_single(url, quality, audio_only, subtitles)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    # Stream via generator (not send_file): the finally block deletes the
    # temp dir both when fully streamed and on client disconnect, and it
    # also runs under the Flask test client. send_file + call_on_close
    # does not reliably fire there (ClosingIterator swallows it).
    size = media.stat().st_size

    def generate():
        try:
            with open(media, "rb") as f:
                while chunk := f.read(1024 * 64):
                    yield chunk
        finally:
            shutil.rmtree(dest, ignore_errors=True)

    return Response(
        generate(),
        mimetype=MEDIA_MIME.get(media.suffix.lower(), "application/octet-stream"),
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(media.name)}",
            "Content-Length": str(size),
        },
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
