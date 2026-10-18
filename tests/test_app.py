"""End-to-end tests: a real headless mpv plus the Flask app.

Run from the repo folder:  python3 -m pytest -v
Needs: mpv, python3-flask, python3-pil, python3-pytest
"""
import base64
import importlib
import io
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import suppress

import pytest
from PIL import Image

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

NAMES = ["a.jpg", "B.png", "slide2.jpg", "slide10.jpg"]
AUTH = {"Authorization": "Basic " + base64.b64encode(b"tester:secret").decode()}
POST = {**AUTH, "X-Requested-With": "presenter"}


def make_image(fmt="JPEG", size=(640, 360)):
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, fmt)
    return buf.getvalue()


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    if not shutil.which("mpv"):
        pytest.skip("mpv is not installed")
    root = tmp_path_factory.mktemp("presenter")
    slides, sock = root / "slides", str(root / "mpv.sock")
    slides.mkdir()
    for name in NAMES:
        (slides / name).write_bytes(make_image("PNG" if name.endswith(".png") else "JPEG"))
    (slides / "notes.txt").write_text("not an image")
    os.environ.update(PRESENTER_SLIDES=str(slides), PRESENTER_SOCKET=sock,
                      PRESENTER_USER="tester", PRESENTER_PASSWORD="secret", HOME=str(root))
    import app as app_module
    app_module = importlib.reload(app_module)  # pick up the settings above
    # Same options as the systemd service, minus the screen.
    player = Player(["mpv", "--vo=null", "--no-audio", "--idle=yes", "--no-terminal",
                     "--loop-playlist=inf", "--image-display-duration=300",
                     f"--input-ipc-server={sock}", f"--vf={app_module.format_filter('full')}",
                     f"--include={app_module.MPV_CONF}", str(slides)], sock)
    yield app_module, app_module.app.test_client(), slides, player
    player.stop()


class Player:
    """The headless mpv under test."""

    def __init__(self, args, sock):
        self.args, self.sock = args, sock
        self.start()

    def start(self):
        self.proc = subprocess.Popen(self.args)
        for _ in range(50):  # wait until mpv accepts connections
            with suppress(OSError), socket.socket(socket.AF_UNIX) as s:
                s.connect(self.sock)
                return
            time.sleep(0.1)

    def stop(self):
        self.proc.terminate()
        self.proc.wait()


def wait_for(client, **expected):
    """Poll /api/status until it matches, since mpv loads files asynchronously."""
    for _ in range(40):
        state = client.get("/api/status", headers=AUTH).get_json()
        if all(state.get(k) == v for k, v in expected.items()):
            return state
        time.sleep(0.1)
    raise AssertionError(f"status {state} never matched {expected}")


def test_login_required(env):
    _, client, _, _ = env
    assert client.get("/").status_code == 401
    bad = {"Authorization": "Basic " + base64.b64encode(b"tester:wrong").decode()}
    assert client.get("/", headers=bad).status_code == 401
    assert client.get("/", headers=AUTH).status_code == 200


def test_posts_need_page_header(env):
    _, client, _, _ = env
    assert client.post("/api/player/next", headers=AUTH).status_code == 400


def test_lists_images_in_mpv_order(env):
    app, client, _, _ = env
    names = [i["name"] for i in client.get("/api/images", headers=AUTH).get_json()]
    assert names == ["a.jpg", "B.png", "slide2.jpg", "slide10.jpg"]
    # same order as mpv's own folder loading (mpv also lists non-images, then skips them)
    assert names == [n for n in app.playlist_names() if n != "notes.txt"]


def test_show_next_prev(env):
    _, client, _, _ = env
    assert client.post("/api/show/slide2.jpg", headers=POST).status_code == 200
    wait_for(client, current="slide2.jpg")
    client.post("/api/player/next", headers=POST)
    wait_for(client, current="slide10.jpg")
    client.post("/api/player/next", headers=POST)  # wraps around
    wait_for(client, current="a.jpg")
    client.post("/api/player/prev", headers=POST)
    wait_for(client, current="slide10.jpg")


def test_pause_and_play(env):
    _, client, _, _ = env
    client.post("/api/player/pause", headers=POST)
    wait_for(client, paused=True)
    client.post("/api/player/play", headers=POST)
    wait_for(client, paused=False)


def wait_ratio(app, expected):
    """Wait until the frame mpv draws (after the format filter) has this shape."""
    params = None
    for _ in range(40):
        params = app.mpv(["get_property", "video-out-params"])[0]
        if params and abs(params["w"] / params["h"] - expected) < 0.01:
            return params
        time.sleep(0.1)
    raise AssertionError(f"frame {params} never reached ratio {expected:.3f}")


def test_screen_formats(env):
    app, client, _, _ = env
    client.post("/api/show/a.jpg", headers=POST)
    wait_for(client, current="a.jpg")
    for name, ratio in [("scope", 2048 / 858), ("flat", 1998 / 1080), ("full", 640 / 360)]:
        assert client.post(f"/api/format/{name}", headers=POST).status_code == 200
        assert client.get("/api/status", headers=AUTH).get_json()["format"] == name
        wait_ratio(app, ratio)
    assert client.post("/api/format/imax", headers=POST).status_code == 404


def test_scope_keeps_huge_images_under_gpu_limit(env, tmp_path):
    app, client, _, _ = env
    big = tmp_path / "huge.jpg"
    Image.new("RGB", (6000, 4000), (0, 0, 0)).save(big)
    client.post("/api/format/scope", headers=POST)
    app.mpv(["loadfile", str(big), "replace"])
    assert wait_ratio(app, 2048 / 858)["w"] <= 3840
    client.post("/api/format/full", headers=POST)
    client.post("/api/resume", headers=POST)


def test_format_survives_player_restart(env):
    app, client, _, player = env
    client.post("/api/format/scope", headers=POST)
    player.stop()
    player.start()  # like systemd restarting mpv, or a reboot
    wait_ratio(app, 2048 / 858)
    assert client.get("/api/status", headers=AUTH).get_json()["format"] == "scope"
    client.post("/api/format/full", headers=POST)


def test_service_default_matches_full_format(env):
    app = env[0]
    with open(os.path.join(REPO, "systemd", "presenter-mpv.service")) as f:
        assert f"--vf={app.format_filter('full')} \\" in f.read()


def test_pin_and_resume(env):
    app, client, _, _ = env
    client.post("/api/pin/B.png", headers=POST)
    wait_for(client, pinned=True, current="B.png")
    assert app.playlist_names() == ["B.png"]
    client.post("/api/resume", headers=POST)
    wait_for(client, pinned=False, paused=False, current="B.png")
    assert sorted(app.playlist_names()) == sorted(NAMES)


def test_show_leaves_pinned_mode(env):
    app, client, _, _ = env
    client.post("/api/pin/a.jpg", headers=POST)
    wait_for(client, pinned=True, current="a.jpg")
    client.post("/api/show/slide10.jpg", headers=POST)
    wait_for(client, pinned=False, current="slide10.jpg")
    assert len(app.playlist_names()) == len(NAMES)


def test_unknown_files_are_404(env):
    _, client, _, _ = env
    assert client.post("/api/show/missing.jpg", headers=POST).status_code == 404
    assert client.post("/api/show/notes.txt", headers=POST).status_code == 404
    assert client.delete("/api/images/..%2Fapp.py", headers=POST).status_code == 404
    assert client.get("/thumb/missing.jpg", headers=AUTH).status_code == 404


def test_upload_sanitizes_and_joins_playlist(env):
    app, client, slides, _ = env
    data = {"files": (io.BytesIO(make_image("PNG")), "../../Šťastný film.PNG")}
    res = client.post("/api/upload", headers=POST, data=data, content_type="multipart/form-data")
    assert res.status_code == 200, res.get_json()
    assert res.get_json()["saved"] == ["Stastny_film.png"]
    assert (slides / "Stastny_film.png").exists()
    assert "Stastny_film.png" in app.playlist_names()
    # same name again gets a suffix instead of overwriting
    data = {"files": (io.BytesIO(make_image("PNG")), "Stastny film.png")}
    res = client.post("/api/upload", headers=POST, data=data, content_type="multipart/form-data")
    assert res.get_json()["saved"] == ["Stastny_film-1.png"]


def test_upload_rejects_other_types(env):
    _, client, slides, _ = env
    data = {"files": (io.BytesIO(b"#!/bin/sh"), "evil.sh")}
    res = client.post("/api/upload", headers=POST, data=data, content_type="multipart/form-data")
    assert res.status_code == 400
    assert not (slides / "evil.sh").exists()


def test_upload_size_limit(env):
    app, client, _, _ = env
    big = io.BytesIO(b"0" * (app.MAX_UPLOAD_MB * 1024 * 1024 + 1))
    res = client.post("/api/upload", headers=POST, data={"files": (big, "big.jpg")},
                      content_type="multipart/form-data")
    assert res.status_code == 413


def test_delete_removes_from_folder_and_playlist(env):
    app, client, slides, _ = env
    assert client.delete("/api/images/Stastny_film-1.png", headers=POST).status_code == 200
    assert not (slides / "Stastny_film-1.png").exists()
    assert "Stastny_film-1.png" not in app.playlist_names()


def test_delete_pinned_image_resumes_slideshow(env):
    app, client, slides, _ = env
    client.post("/api/pin/Stastny_film.png", headers=POST)
    wait_for(client, pinned=True, current="Stastny_film.png")
    client.delete("/api/images/Stastny_film.png", headers=POST)
    wait_for(client, pinned=False)
    assert sorted(app.playlist_names()) == sorted(NAMES)


def test_thumbnail(env):
    _, client, _, _ = env
    res = client.get("/thumb/slide2.jpg", headers=AUTH)
    assert res.status_code == 200 and res.mimetype == "image/jpeg"
    assert Image.open(io.BytesIO(res.data)).size[0] <= 400


def test_player_down_is_reported_but_files_still_work(env):
    app, client, _, player = env
    player.stop()
    res = client.get("/api/status", headers=AUTH)
    assert res.status_code == 503 and "not running" in res.get_json()["error"]
    assert client.get("/api/images", headers=AUTH).status_code == 200
    data = {"files": (io.BytesIO(make_image()), "while-down.jpg")}
    res = client.post("/api/upload", headers=POST, data=data, content_type="multipart/form-data")
    assert res.status_code == 200
