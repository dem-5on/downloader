# downloader

YouTube video / playlist downloader built on `yt-dlp`.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
python script.py "URL" --quality 720p
python script.py "URL" --audio-only
python script.py --help
```

Downloads go to `Downloads/`. Sidecar `.vtt` / `.webp` files are
cleaned up automatically unless `--keep-subs` / `--keep-thumbnail` is passed.

## Web UI

```bash
.venv/bin/python app.py
# open http://127.0.0.1:5000
```

Paste a YouTube link, preview the video or every entry in a playlist,
pick quality / subtitles / audio-only, then download straight to your device.
Each download shows a live progress bar (percent, speed, ETA).
Playlists can be downloaded one video at a time or all at once as a ZIP
(failed entries are listed, not fatal).

`app.py` alone is the dev server. For production use gunicorn:

```bash
.venv/bin/gunicorn -w 1 --threads 8 --timeout 300 -b 127.0.0.1:5000 app:app
```

Keep `-w 1`: download jobs live in process memory, so extra workers
would split them across processes and progress polling would break.
`--threads 8` handles concurrent browsers and downloads.

## Reliability notes

- **JS runtime:** yt-dlp needs node or deno for YouTube's JS challenges
  (otherwise some videos 403). The app and CLI enable both automatically
  if installed (`node` is enough).
- **Cookies:** for age-restricted or blocked videos, pick your browser in
  the Cookies dropdown (reads that browser's YouTube cookies on the server),
  or CLI: `python script.py "URL" --cookies-from-browser firefox`.

## API

- `GET /` — the UI.
- `GET /api/options` — qualities and defaults.
- `POST /api/info` `{"url"}` — video or playlist preview (no download).
- `POST /api/jobs` `{"type": "video", "url", ...}` or
  `{"type": "zip", "urls": [{"url", "title"}], "playlist_title", ...}` —
  starts a background job, returns `{"job_id"}`.
  Common options: `quality`, `audio_only`, `subtitles`, `cookies_from_browser`.
- `GET /api/jobs/<id>` — progress JSON (`status`, `percent`, `speed`,
  `eta`, `current`/`total` for zips). Poll this.
- `GET /api/jobs/<id>/file` — the finished video or `.zip` (deleted from
  the server after streaming).

Errors are generic by design; full tracebacks stay in the server log.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```
