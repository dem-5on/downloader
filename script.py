"""YouTube video / playlist downloader (high quality + audio-only for transcription)."""
import argparse
import sys
from pathlib import Path

import yt_dlp

# Height-capped format selectors. Best video+audio merged to mp4,
# falling back to single-file best.
QUALITY_FORMATS = {
    "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
    "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "720p": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "480p": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    "360p": "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best[height<=360]/best",
}

AUDIO_FORMAT = "bestaudio/best"

# Sidecar extensions left behind by writesubtitles / writethumbnail.
SUBTITLE_EXTS = {".vtt", ".srt", ".ass", ".lrc"}
THUMBNAIL_EXTS = {".webp", ".jpg", ".jpeg", ".png"}


class DownloadTracker:
    """Collects per-file results across a playlist run."""

    def __init__(self):
        self.finished = []
        self.errors = []

    def progress_hook(self, d):
        status = d.get("status")
        if status == "downloading":
            percent = d.get("_percent_str", "N/A")
            speed = d.get("_speed_str", "N/A")
            eta = d.get("_eta_str", "N/A")
            filename = d.get("filename", "")
            sys.stdout.write(f"\rDownloading {percent} | {speed} | ETA {eta} | {filename[:60]}   ")
            sys.stdout.flush()
        elif status == "finished":
            filename = d.get("filename", "unknown")
            self.finished.append(filename)
            print(f"\nFinished: {filename}")
        elif status == "error":
            self.errors.append(str(d.get("error", "unknown error")))


class SummaryLogger:
    """yt-dlp logger that records errors for the final summary."""

    def __init__(self, tracker):
        self.tracker = tracker

    def debug(self, msg):
        pass

    def warning(self, msg):
        print(f"Warning: {msg}", file=sys.stderr)

    def error(self, msg):
        self.tracker.errors.append(msg)
        print(f"Error: {msg}", file=sys.stderr)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Download YouTube videos/playlists.")
    p.add_argument("url", nargs="?", help="YouTube video or playlist URL")
    p.add_argument("--quality", default="best",
                   choices=["best", "1080p", "720p", "480p", "360p"],
                   help="Max video height (default: best)")
    p.add_argument("--audio-only", action="store_true",
                   help="Download audio only as mp3 (10-20x smaller, ideal for transcription)")
    p.add_argument("--no-playlist", action="store_true",
                   help="Download only the single video even if URL is a playlist")
    p.add_argument("--out", default="Downloads", help="Output directory (default: Downloads)")
    p.add_argument("--keep-subs", action="store_true",
                   help="Keep downloaded subtitle sidecars (.vtt) instead of deleting after embed")
    p.add_argument("--keep-thumbnail", action="store_true",
                   help="Keep thumbnail sidecars (.webp/.jpg) instead of deleting after embed")
    p.add_argument("--no-subs", action="store_true",
                   help="Skip subtitles entirely (no download, no embed)")
    p.add_argument("--cookies-from-browser", default=None,
                   help="e.g. chrome, firefox — use browser cookies for age-restricted videos")
    return p.parse_args(argv)


def build_format(args):
    if args.audio_only:
        return AUDIO_FORMAT
    return QUALITY_FORMATS[args.quality]


def detect_playlist(url, no_playlist):
    """Return True if URL resolves to a playlist with multiple entries."""
    if no_playlist:
        return False
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True, "noplaylist": False}) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return False
        if info.get("_type") == "playlist":
            return len(info.get("entries", [])) > 1
        return False
    except Exception:
        return False


def build_outtmpl(outdir, is_playlist):
    # NOTE: "[%(id)s]" suffix prevents "..ext" when a title ends with "."
    # e.g. "Literally." -> "Literally. [abc123].mp4" instead of "Literally..mp4".
    if is_playlist:
        return f"{outdir}/%(playlist_title)s/%(title)s [%(id)s].%(ext)s"
    return f"{outdir}/%(title)s [%(id)s].%(ext)s"


def build_ydl_opts(args, tracker, outtmpl):
    want_subs = not getattr(args, "no_subs", False)
    if args.audio_only:
        postprocessors = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ]
    else:
        postprocessors = [
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ]
        if want_subs:
            # Embed must come before metadata so tracks are tagged correctly.
            postprocessors.insert(0, {"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})

    opts = {
        "outtmpl": outtmpl,
        "format": build_format(args),
        "merge_output_format": "mp4" if not args.audio_only else None,
        "postprocessors": postprocessors,
        "writesubtitles": want_subs,
        "writeautomaticsub": want_subs,
        "subtitleslangs": ["en"],
        "writethumbnail": True,
        # Sanitize filenames; trim very long titles.
        "windowsfilenames": True,
        "trim_file_name": 200,
        # Reliability: resume partial downloads (.part files), retry fragments.
        "continuedl": True,
        "ignoreerrors": True,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        # Progress + error collection.
        "progress_hooks": [tracker.progress_hook],
        "logger": SummaryLogger(tracker),
        "noplaylist": args.no_playlist,
        "consoletitle": True,
    }
    if args.cookies_from_browser:
        opts["cookiesfrombrowser"] = (args.cookies_from_browser,)
    # Drop None values (e.g. merge_output_format in audio mode).
    return {k: v for k, v in opts.items() if v is not None}


def cleanup_sidecars(outdir, keep_subs=False, keep_thumbnail=False):
    """Delete .vtt/.webp sidecars left after successful embed.

    .part files are intentionally kept — they are resume data for
    interrupted downloads, deleted automatically by yt-dlp on success.
    Returns number of files removed.
    """
    removed = 0
    base = Path(outdir)
    if not base.is_dir():
        return 0
    for f in base.rglob("*"):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        if ext in SUBTITLE_EXTS and not keep_subs:
            f.unlink()
            removed += 1
        elif ext in THUMBNAIL_EXTS and not keep_thumbnail:
            f.unlink()
            removed += 1
    return removed


def download_video_or_playlist(url, args):
    """Download URL. Returns (num_finished, num_errors)."""
    tracker = DownloadTracker()
    is_playlist = detect_playlist(url, args.no_playlist)
    outtmpl = build_outtmpl(args.out, is_playlist)
    print(f"Playlist detected: {is_playlist} | quality={args.quality} "
          f"| audio_only={args.audio_only}")
    print(f"Output: {outtmpl}")

    ydl_opts = build_ydl_opts(args, tracker, outtmpl)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    removed = cleanup_sidecars(args.out, args.keep_subs, args.keep_thumbnail)
    if removed:
        print(f"Cleaned up {removed} sidecar file(s) (.vtt/.webp).")

    # Deduplicate errors (logger + hook can both record the same failure).
    errors = list(dict.fromkeys(tracker.errors))
    print(f"\nSummary: {len(tracker.finished)} succeeded, {len(errors)} failed.")
    for e in errors:
        print(f"  FAILED: {e[:200]}")
    return len(tracker.finished), len(errors)


def main(argv=None):
    args = parse_args(argv)
    url = args.url
    if not url:
        print("Video Downloader (High Quality)")
        print("=" * 40)
        url = input("Enter YouTube video or playlist link: ").strip()
        if not url:
            print("No link provided.")
            return 1
    try:
        ok, failed = download_video_or_playlist(url, args)
        print("\nAll downloads completed!" if failed == 0 else "\nDone with errors.")
        return 0 if failed == 0 else 2
    except Exception as e:
        print(f"\nDownload failed: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
