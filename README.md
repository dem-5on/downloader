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
