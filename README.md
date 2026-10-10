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
