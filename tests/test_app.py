"""End-to-end tests: a real headless mpv plus the Flask app.

Run from the repo folder:  python3 -m pytest -v
Needs: mpv, python3-flask, python3-pil, python3-pytest
"""
import base64
import importlib
import io
import os
import re
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

FLAT = ["a.jpg", "B.png", "slide2.jpg", "slide10.jpg"]  # this test screen is Flat
SCOPE = ["a.jpg", "B.png", "scope-only.jpg"]  # same slides, Scope versions
FLAT_RATIO, SCOPE_RATIO = 1998 / 1080, 2048 / 858
AUTH = {"Authorization": "Basic " + base64.b64encode(b"tester:secret").decode()}
POST = {**AUTH, "X-Requested-With": "presenter"}


def make_image(fmt="JPEG", size=(1998, 1080)):
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, fmt)
    return buf.getvalue()


def mpv_args(sock, fmt, folder):
    """The mpv options of the systemd service, minus the screen."""
    return ["mpv", "--vo=null", "--no-audio", "--idle=yes", "--no-terminal",
            "--loop-playlist=inf", "--image-display-duration=300", f"--input-ipc-server={sock}",
            f"--include={REPO}/mpv/formats.conf", f"--profile={fmt}", str(folder)]


class Player:
    """A headless mpv under test."""

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


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    if not shutil.which("mpv"):
        pytest.skip("mpv is not installed")
    root = tmp_path_factory.mktemp("presenter")
    slides, sock = root / "slides", str(root / "mpv.sock")
    for folder, names, size in [("flat", FLAT, (1998, 1080)), ("scope", SCOPE, (2048, 858))]:
        (slides / folder).mkdir(parents=True)
        for name in names:
            fmt = "PNG" if name.endswith(".png") else "JPEG"
            (slides / folder / name).write_bytes(make_image(fmt, size))
    (slides / "flat" / "notes.txt").write_text("not an image")
    os.environ.update(PRESENTER_SLIDES=str(slides), PRESENTER_FORMAT="flat", PRESENTER_SOCKET=sock,
                      PRESENTER_USER="tester", PRESENTER_PASSWORD="secret", HOME=str(root))
    import app as app_module
    app_module = importlib.reload(app_module)  # pick up the settings above
    player = Player(mpv_args(sock, "flat", slides / "flat"), sock)
    yield app_module, app_module.app.test_client(), slides, player
    player.stop()


def wait_for(client, **expected):
    """Poll /api/status until it matches, since mpv loads files asynchronously."""
    for _ in range(40):
        state = client.get("/api/status", headers=AUTH).get_json()
        if all(state.get(k) == v for k, v in expected.items()):
            return state
        time.sleep(0.1)
    raise AssertionError(f"status {state} never matched {expected}")


def frame(mpv_call, expected_ratio):
    """Wait until the frame mpv draws (after the format filter) has this shape."""
    params = None
    for _ in range(40):
        params = mpv_call(["get_property", "video-out-params"])[0]
        if params and abs(params["w"] / params["h"] - expected_ratio) < 0.01:
            return params
        time.sleep(0.1)
    raise AssertionError(f"frame {params} never reached ratio {expected_ratio:.3f}")


def upload(client, folder, data, name):
    return client.post(f"/api/upload/{folder}", headers=POST, content_type="multipart/form-data",
                       data={"files": (io.BytesIO(data), name)})


def test_login_required(env):
    _, client, _, _ = env
    assert client.get("/").status_code == 401
    bad = {"Authorization": "Basic " + base64.b64encode(b"tester:wrong").decode()}
    assert client.get("/", headers=bad).status_code == 401
    assert client.get("/", headers=AUTH).status_code == 200


def test_posts_need_page_header(env):
    _, client, _, _ = env
    assert client.post("/api/player/next", headers=AUTH).status_code == 400


def test_lists_both_folders_in_mpv_order(env):
    app, client, _, _ = env
    data = client.get("/api/images", headers=AUTH).get_json()
    flat = [i["name"] for i in data["flat"]]
    assert flat == ["a.jpg", "B.png", "slide2.jpg", "slide10.jpg"]
    assert [i["name"] for i in data["scope"]] == ["a.jpg", "B.png", "scope-only.jpg"]
    # this screen plays only the flat folder, in mpv's own order (mpv also lists non-images)
    assert flat == [n for n in app.playlist_names() if n != "notes.txt"]


def test_flat_profile_frames_exactly(env):
    app, client, _, _ = env
    client.post("/api/show/a.jpg", headers=POST)
    wait_for(client, current="a.jpg")
    params = frame(app.mpv, FLAT_RATIO)
    assert (params["w"], params["h"]) == (1998, 1080)  # a Flat slide fills the Flat frame


@pytest.mark.parametrize("fmt, ratio, size", [("flat", FLAT_RATIO, (1920, 1080)),
                                              ("scope", SCOPE_RATIO, (1920, 1080)),
                                              ("scope", SCOPE_RATIO, (6000, 4000))])
def test_profiles_frame_any_image(tmp_path, fmt, ratio, size):
    """Each mpv profile pads any image to its DCI shape and stays under the GPU limit."""
    if not shutil.which("mpv"):
        pytest.skip("mpv is not installed")
    Image.new("RGB", size).save(tmp_path / "img.jpg")
    sock = str(tmp_path / "mpv.sock")
    player = Player(mpv_args(sock, fmt, tmp_path / "img.jpg"), sock)
    try:
        import app
        old, app.MPV_SOCKET = app.MPV_SOCKET, sock
        try:
            params = frame(app.mpv, ratio)
        finally:
            app.MPV_SOCKET = old
        assert params["w"] <= 3840 and params["h"] <= 3840
    finally:
        player.stop()


def test_every_format_has_an_mpv_profile(env):
    app = env[0]
    with open(os.path.join(REPO, "mpv", "formats.conf")) as f:
        profiles = re.findall(r"^\[(\w+)\]", f.read(), re.M)
    assert profiles == list(app.FORMATS)


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


def test_pin_and_resume(env):
    app, client, _, _ = env
    client.post("/api/pin/B.png", headers=POST)
    wait_for(client, pinned=True, current="B.png")
    assert app.playlist_names() == ["B.png"]
    client.post("/api/resume", headers=POST)
    wait_for(client, pinned=False, paused=False, current="B.png")
    assert sorted(app.playlist_names()) == sorted(FLAT)


def test_show_leaves_pinned_mode(env):
    app, client, _, _ = env
    client.post("/api/pin/a.jpg", headers=POST)
    wait_for(client, pinned=True, current="a.jpg")
    client.post("/api/show/slide10.jpg", headers=POST)
    wait_for(client, pinned=False, current="slide10.jpg")
    assert len(app.playlist_names()) == len(FLAT)


def test_other_format_cannot_go_on_screen(env):
    _, client, _, _ = env
    assert client.post("/api/show/scope-only.jpg", headers=POST).status_code == 404
    assert client.post("/api/pin/scope-only.jpg", headers=POST).status_code == 404


def test_unknown_files_and_folders_are_404(env):
    _, client, _, _ = env
    assert client.post("/api/show/missing.jpg", headers=POST).status_code == 404
    assert client.post("/api/show/notes.txt", headers=POST).status_code == 404
    assert client.delete("/api/images/flat/..%2F..%2Fapp.py", headers=POST).status_code == 404
    assert client.delete("/api/images/imax/a.jpg", headers=POST).status_code == 404
    assert upload(client, "imax", make_image(), "x.jpg").status_code == 404
    assert client.get("/thumb/flat/missing.jpg", headers=AUTH).status_code == 404


def test_upload_to_screen_folder_joins_playlist(env):
    app, client, slides, _ = env
    res = upload(client, "flat", make_image("PNG"), "../../Šťastný film.PNG")
    assert res.status_code == 200, res.get_json()
    assert res.get_json()["saved"] == ["Stastny_film.png"]
    assert (slides / "flat" / "Stastny_film.png").exists()
    assert "Stastny_film.png" in app.playlist_names()
    # same name again gets a suffix instead of overwriting
    res = upload(client, "flat", make_image("PNG"), "Stastny film.png")
    assert res.get_json()["saved"] == ["Stastny_film-1.png"]


def test_upload_to_other_folder_is_only_stored(env):
    app, client, slides, _ = env
    res = upload(client, "scope", make_image(size=(2048, 858)), "Dune.jpg")
    assert res.get_json()["saved"] == ["Dune.jpg"]
    assert (slides / "scope" / "Dune.jpg").exists()
    assert not (slides / "flat" / "Dune.jpg").exists()
    assert "Dune.jpg" not in app.playlist_names()


def test_upload_rejects_other_types(env):
    _, client, slides, _ = env
    assert upload(client, "flat", b"#!/bin/sh", "evil.sh").status_code == 400
    assert not (slides / "flat" / "evil.sh").exists()


def test_upload_size_limit(env):
    app, client, _, _ = env
    big = b"0" * (app.MAX_UPLOAD_MB * 1024 * 1024 + 1)
    assert upload(client, "flat", big, "big.jpg").status_code == 413


def test_delete_in_other_folder_leaves_screen_alone(env):
    app, client, slides, _ = env
    # a.jpg exists in both folders; deleting the Scope copy must not touch the Flat slideshow
    assert client.delete("/api/images/scope/a.jpg", headers=POST).status_code == 200
    assert not (slides / "scope" / "a.jpg").exists()
    assert (slides / "flat" / "a.jpg").exists()
    assert "a.jpg" in app.playlist_names()


def test_delete_removes_from_folder_and_playlist(env):
    app, client, slides, _ = env
    assert client.delete("/api/images/flat/Stastny_film-1.png", headers=POST).status_code == 200
    assert not (slides / "flat" / "Stastny_film-1.png").exists()
    assert "Stastny_film-1.png" not in app.playlist_names()


def test_delete_pinned_image_resumes_slideshow(env):
    app, client, _, _ = env
    client.post("/api/pin/Stastny_film.png", headers=POST)
    wait_for(client, pinned=True, current="Stastny_film.png")
    client.delete("/api/images/flat/Stastny_film.png", headers=POST)
    wait_for(client, pinned=False)
    assert sorted(app.playlist_names()) == sorted(FLAT)


def test_thumbnails_per_folder(env):
    _, client, _, _ = env
    for folder in ("flat", "scope"):
        res = client.get(f"/thumb/{folder}/B.png", headers=AUTH)
        assert res.status_code == 200 and res.mimetype == "image/jpeg"
        assert Image.open(io.BytesIO(res.data)).size[0] <= 400


def test_player_restart_keeps_format(env):
    app, client, slides, player = env
    player.stop()
    player.start()  # like systemd restarting mpv, or a reboot
    wait_for(client, pinned=False)
    frame(app.mpv, FLAT_RATIO)
    assert all(n in FLAT for n in app.playlist_names() if n != "notes.txt")


def test_player_down_is_reported_but_files_still_work(env):
    _, client, _, player = env
    player.stop()
    res = client.get("/api/status", headers=AUTH)
    assert res.status_code == 503 and "not running" in res.get_json()["error"]
    assert client.get("/api/images", headers=AUTH).status_code == 200
    assert upload(client, "flat", make_image(), "while-down.jpg").status_code == 200


def test_invalid_format_is_refused(env, monkeypatch):
    import app
    monkeypatch.setenv("PRESENTER_FORMAT", "imax")
    with pytest.raises(SystemExit, match="flat, scope"):
        importlib.reload(app)
    monkeypatch.setenv("PRESENTER_FORMAT", "flat")
    importlib.reload(app)
