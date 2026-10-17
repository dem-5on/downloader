"""Committed test suite for the downloader web UI.

Run:  .venv/bin/python -m pytest tests/ -q
All download tests use a fake yt-dlp backend: no network needed.
"""
import glob
import io
import re
import tempfile
import time
import zipfile

import pytest

import app as appmod
from script import build_ydl_opts, parse_args, DownloadTracker


@pytest.fixture()
def client():
    return appmod.app.test_client()


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL: no network, writes a dummy file."""

    last_opts = None

    def __init__(self, opts):
        FakeYDL.last_opts = opts
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def download(self, urls):
        import os
        url = urls[0]
        if "fail" in url:
            raise RuntimeError("boom")
        for hook in self.opts.get("progress_hooks", []):
            hook({"status": "downloading", "downloaded_bytes": 50,
                  "total_bytes": 100, "_speed_str": "1MiB/s",
                  "_eta_str": "00:01", "filename": "/tmp/x/f.mp4"})
            hook({"status": "finished", "filename": "/tmp/x/f.mp4"})
        base = self.opts["outtmpl"].split("%")[0]
        os.makedirs(base, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9]", "", url)[-8:] or "vid"
        with open(os.path.join(base, f"Vid-{slug} [{slug}].mp4"), "wb") as f:
            f.write(b"M" * 2048)
        with open(os.path.join(base, f"Vid-{slug} [{slug}].en.vtt"), "w") as f:
            f.write("sidecar")


@pytest.fixture()
def fake_ydl(monkeypatch):
    monkeypatch.setattr(appmod.yt_dlp, "YoutubeDL", FakeYDL)
    return FakeYDL


@pytest.fixture()
def no_temp_leak():
    before = set(glob.glob(tempfile.gettempdir() + "/ytui-*"))
    yield
    after = set(glob.glob(tempfile.gettempdir() + "/ytui-*"))
    assert after == before, f"temp leak: {after - before}"


def wait_done(client, job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get(f"/api/jobs/{job_id}").get_json()
        if s["status"] in ("done", "error"):
            return s
        time.sleep(0.05)
    raise AssertionError("job timed out: " + job_id)


def test_index_has_ui_elements(client):
    html = client.get("/").get_data(as_text=True)
    for needle in ['id="url"', 'id="quality"', 'id="subs"', 'id="audio"',
                   'id="cookies"', 'id="status"', 'id="results"',
                   'id="toasts"', "<progress", "Download all as ZIP",
                   "pollJob", "tickBar", "has-results"]:
        assert needle in html, f"missing {needle}"


def test_options(client):
    opts = client.get("/api/options").get_json()
    assert opts["qualities"] == ["best", "1080p", "720p", "480p", "360p"]


def test_info_validation(client):
    assert client.post("/api/info", json={}).status_code == 400
    r = client.post("/api/info", json={"url": "https://example.com/x"})
    assert r.status_code == 400


def test_info_success_video_and_playlist(client, monkeypatch):
    video = {"id": "abc", "title": "T", "duration": 61, "uploader": "U",
             "thumbnail": "http://t/x.jpg"}
    playlist = {"_type": "playlist", "title": "P", "entries": [
        {"id": "v1", "title": "First", "duration": 65,
         "url": "https://www.youtube.com/watch?v=v1"},
        {"id": "v2", "title": None, "duration": None},
        None,
    ]}
    monkeypatch.setattr(appmod, "fetch_info", lambda url: video)
    got = client.post("/api/info", json={"url": "https://www.youtube.com/watch?v=abc"}).get_json()
    assert got["type"] == "video" and got["uploader"] == "U"
    monkeypatch.setattr(appmod, "fetch_info", lambda url: playlist)
    got = client.post("/api/info", json={"url": "https://www.youtube.com/playlist?list=x"}).get_json()
    assert got["type"] == "playlist" and got["count"] == 2
    assert got["entries"][1]["url"] == "https://www.youtube.com/watch?v=v2"


def test_info_backend_error_sanitized(client, monkeypatch):
    def angry(url):
        raise RuntimeError("/tmp/ytui-SECRET traceback internals")
    monkeypatch.setattr(appmod, "fetch_info", angry)
    r = client.post("/api/info", json={"url": "https://www.youtube.com/watch?v=x"})
    assert r.status_code == 502
    assert "SECRET" not in r.get_data(as_text=True)
    assert r.get_json()["error"] == "Could not fetch that link. Check it and try again."


def test_jobs_validation(client):
    base = {"url": "https://www.youtube.com/watch?v=x"}
    assert client.post("/api/jobs", json={"type": "movie", **base}).status_code == 400
    assert client.post("/api/jobs", json={"type": "video", "url": base["url"], "quality": "4k"}).status_code == 400
    assert client.post("/api/jobs", json={"type": "video"}).status_code == 400
    assert client.post("/api/jobs", json={"type": "video", "url": "https://evil.com/x"}).status_code == 400
    assert client.post("/api/jobs", json={"type": "video", "url": base["url"],
                                           "cookies_from_browser": "netscape"}).status_code == 400
    assert client.post("/api/jobs", json={"type": "zip", "urls": []}).status_code == 400
    big = {"type": "zip", "urls": [{"url": base["url"]}] * 201}
    assert client.post("/api/jobs", json=big).status_code == 400
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.get("/api/jobs/nope/file").status_code == 404


def test_hook_math():
    job = appmod.Job("video")
    job.hook({"status": "downloading", "downloaded_bytes": 1_000_000,
              "total_bytes": 4_000_000, "_speed_str": "1MiB/s",
              "_eta_str": "00:03", "filename": "/tmp/x/Vid [a].mp4"})
    assert job.percent == 25.0 and job.speed == "1MiB/s" and job.eta == "00:03"
    assert job.current_title == "Vid [a].mp4" and job.status == "downloading"
    job.hook({"status": "finished", "filename": "/tmp/x/f.mp4"})
    assert job.percent == 100.0


def test_video_job(client, fake_ydl, no_temp_leak):
    r = client.post("/api/jobs", json={"type": "video",
                    "url": "https://www.youtube.com/watch?v=vid00001",
                    "quality": "720p", "subtitles": False})
    assert r.status_code == 200
    jid = r.get_json()["job_id"]
    s = wait_done(client, jid)
    assert s["status"] == "done" and s["percent"] == 100.0
    assert s["filename"].endswith(".mp4")
    assert fake_ydl.last_opts["writesubtitles"] is False
    assert "height" in fake_ydl.last_opts["format"]
    r = client.get(f"/api/jobs/{jid}/file")
    assert r.status_code == 200 and r.data == b"M" * 2048
    assert r.headers["Content-Type"] == "video/mp4"
    r.close()
    assert client.get(f"/api/jobs/{jid}").status_code == 404


def test_video_job_cookies_passthrough(client, fake_ydl, no_temp_leak):
    r = client.post("/api/jobs", json={"type": "video",
                    "url": "https://www.youtube.com/watch?v=vid00002",
                    "cookies_from_browser": "firefox"})
    s = wait_done(client, r.get_json()["job_id"])
    assert s["status"] == "done"
    assert fake_ydl.last_opts["cookiesfrombrowser"] == ("firefox",)
    r = client.get(f"/api/jobs/{s['job_id']}/file")
    assert r.status_code == 200
    r.close()


def test_video_job_no_cookies_by_default(client, fake_ydl, no_temp_leak):
    r = client.post("/api/jobs", json={"type": "video",
                    "url": "https://www.youtube.com/watch?v=vid00003"})
    s = wait_done(client, r.get_json()["job_id"])
    assert s["status"] == "done"
    assert "cookiesfrombrowser" not in fake_ydl.last_opts
    r = client.get(f"/api/jobs/{s['job_id']}/file")
    assert r.status_code == 200
    r.close()


def test_zip_job(client, fake_ydl, no_temp_leak):
    r = client.post("/api/jobs", json={
        "type": "zip", "playlist_title": "My: List?",
        "urls": [{"url": "https://www.youtube.com/watch?v=aaa111", "title": "A"},
                 {"url": "https://youtu.be/bbb222", "title": "B"},
                 {"url": "https://www.youtube.com/watch?v=fail999", "title": "C"}]})
    assert r.get_json()["total"] == 3
    s = wait_done(client, r.get_json()["job_id"], timeout=30)
    assert s["status"] == "done" and s["total"] == 3 and s["current"] == 3
    assert s["failed"] == ["C"]  # title only: no backend tracebacks leak
    assert s["filename"] == "My_ List_.zip"
    r = client.get(f"/api/jobs/{s['job_id']}/file")
    assert r.headers["Content-Type"] == "application/zip"
    assert len(zipfile.ZipFile(io.BytesIO(r.data)).namelist()) == 2
    r.close()


def test_zip_all_fail(client, fake_ydl, no_temp_leak):
    r = client.post("/api/jobs", json={"type": "zip",
                    "urls": [{"url": "https://www.youtube.com/watch?v=fail1"}]})
    s = wait_done(client, r.get_json()["job_id"])
    assert s["status"] == "error" and "nothing to pack" in s["error"]
    assert client.get(f"/api/jobs/{s['job_id']}/file").status_code == 502


def test_job_error_sanitized(client, monkeypatch, no_temp_leak):
    class LeakyYDL(FakeYDL):
        def download(self, urls):
            raise RuntimeError("ffmpeg exploded at /tmp/ytui-SECRET/part-000")

    monkeypatch.setattr(appmod.yt_dlp, "YoutubeDL", LeakyYDL)
    r = client.post("/api/jobs", json={"type": "video",
                    "url": "https://www.youtube.com/watch?v=zzzzzzzz"})
    s = wait_done(client, r.get_json()["job_id"])
    assert s["status"] == "error"
    assert "SECRET" not in s["error"] and "/tmp" not in s["error"]
    assert s["error"] == "Download failed. The video may be private, removed, or unavailable."


def test_cli_uses_js_runtime():
    opts = build_ydl_opts(parse_args(["http://x"]), DownloadTracker(), "D/%(title)s.%(ext)s")
    assert opts["js_runtimes"] == {"node": {}, "deno": {}}


def test_formats_parse():
    import yt_dlp
    from script import QUALITY_FORMATS
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
    for name, fmt in QUALITY_FORMATS.items():
        assert ydl.build_format_selector(fmt), name


def test_formats_prefer_h264_over_vp9_av1():
    """Regression: 'best' must not pick VP9/AV1-in-mp4 (silent playback)."""
    import yt_dlp
    from script import QUALITY_FORMATS

    def fmt(fid, height, vcodec, acodec, ext):
        return {"format_id": fid, "url": f"http://x/{fid}", "ext": ext,
                "height": height, "vcodec": vcodec, "acodec": acodec,
                "filesize": 10_000_000, "tbr": 1000.0}

    # Real-world shape: 4K VP9 and AV1 wrapped in mp4 (the silent-playback
    # culprits), h264 only up to 1080p, plus an m4a audio track.
    formats = [
        fmt("628", 2160, "vp09.00.51.08", "none", "mp4"),
        fmt("401", 2160, "av01.0.13M.08", "none", "mp4"),
        fmt("137", 1080, "avc1.640028", "none", "mp4"),
        fmt("136", 720, "avc1.4d401f", "none", "mp4"),
        fmt("18", 360, "avc1.42001E", "mp4a.40.2", "mp4"),
        fmt("140", None, "none", "mp4a.40.2", "m4a"),
    ]
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})

    def resolve(name):
        """Return the chosen (video, audio) formats, audio may be None."""
        video = audio = None
        for item in ydl.build_format_selector(QUALITY_FORMATS[name])(
                {"formats": formats, "has_merged_format": True}):
            if isinstance(item, list):
                vf, af = item[0], (item[1] if len(item) > 1 else None)
            else:
                # Pre-merged single file: carries both streams itself.
                vf, af = item, None
            if vf.get("vcodec") != "none":
                video = vf
            if af is not None and af.get("vcodec") == "none":
                audio = af
            elif vf.get("vcodec") != "none" and vf.get("acodec") != "none":
                video, audio = vf, vf  # single file with both streams
        return video, audio

    # best: H.264 video (1080p here, since 4K is VP9/AV1 only) + AAC audio.
    v, a = resolve("best")
    assert v["vcodec"].startswith("avc1"), f"best video was {v['vcodec']}"
    assert v["height"] <= 1080, f"best video was {v['height']}p"
    assert a is not None and a["acodec"].startswith("mp4a"), "best lacks AAC audio"

    # Each capped quality must respect its ceiling, stay on H.264, keep audio.
    for name, cap in [("1080p", 1080), ("720p", 720), ("480p", 480), ("360p", 360)]:
        v, a = resolve(name)
        assert v["vcodec"].startswith("avc1"), f"{name} video was {v['vcodec']}"
        assert v["height"] <= cap, f"{name} video was {v['height']}p"
        assert a is not None and a["acodec"] != "none", f"{name} has no audio"
