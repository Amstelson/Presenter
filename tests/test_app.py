"""End-to-end tests: a real headless mpv plus the Flask app.

Run from the repo folder:  python3 -m pytest -v
Needs: mpv, python3-flask, python3-pil, python3-pytest
"""
import base64
import importlib
import io
import json
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

LIBRARY = {  # this test screen is Flat
    "flat": {"Coming soon": ["a.jpg", "B.png"], "Kids": ["slide10.jpg", "slide2.jpg"]},
    "scope": {"Coming soon": ["a.jpg"]},
}
ALL_FLAT = ["Coming soon/a.jpg", "Coming soon/B.png", "General/loose.jpg", "Kids/slide2.jpg", "Kids/slide10.jpg"]
FLAT_RATIO, SCOPE_RATIO = 1998 / 1080, 2048 / 858
AUTH = {"Authorization": "Basic " + base64.b64encode(b"tester:secret").decode()}
POST = {**AUTH, "X-Requested-With": "presenter"}


def make_image(fmt="JPEG", size=(1998, 1080)):
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, fmt)
    return buf.getvalue()


def mpv_args(sock, fmt, *files, extra=()):
    """The mpv options of the systemd service, minus the screen."""
    return ["mpv", "--vo=null", "--no-audio", "--idle=yes", "--no-terminal", "--loop-playlist=inf",
            "--image-display-duration=300", f"--input-ipc-server={sock}",
            f"--include={REPO}/mpv/formats.conf", f"--profile={fmt}", *extra, *map(str, files)]


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
    for fmt, folders in LIBRARY.items():
        for folder, names in folders.items():
            (slides / fmt / folder).mkdir(parents=True)
            for name in names:
                (slides / fmt / folder / name).write_bytes(make_image("PNG" if name.endswith(".png") else "JPEG"))
    (slides / "flat" / "loose.jpg").write_bytes(make_image())  # copied in without a folder
    (slides / "flat" / "Kids" / "notes.txt").write_text("not an image")
    os.environ.update(PRESENTER_SLIDES=str(slides), PRESENTER_FORMAT="flat", PRESENTER_SOCKET=sock,
                      PRESENTER_USER="tester", PRESENTER_PASSWORD="secret", HOME=str(root))
    import app as app_module
    app_module = importlib.reload(app_module)  # pick up the settings above, writes the playlist file
    player = Player(mpv_args(sock, "flat", app_module.PLAYLIST_FILE,
                             extra=[f"--include={app_module.PLAYER_CONF}"]), sock)
    yield app_module, app_module.app.test_client(), slides, player
    player.stop()


def playing(app):
    """mpv's playlist as "folder/name" entries."""
    return [os.path.relpath(p, app.PLAY_DIR) for p in app.mpv_playlist()]


def playlist_file(app):
    with open(app.PLAYLIST_FILE) as f:
        return [os.path.relpath(line.strip(), app.PLAY_DIR) for line in f if not line.startswith("#")]


def status(client):
    return client.get("/api/status", headers=AUTH).get_json()


def wait_for(client, **expected):
    """Poll /api/status until it matches, since mpv loads files asynchronously."""
    for _ in range(40):
        state = status(client)
        if all(state.get(k) == v for k, v in expected.items()):
            return state
        time.sleep(0.1)
    raise AssertionError(f"status {state} never matched {expected}")


def at(folder, name):
    return {"folder": folder, "name": name}


def post(client, url, data=None):
    return client.post(url, headers=POST, json=data)


def upload(client, fmt, folder, data, name):
    return client.post(f"/api/upload/{fmt}/{folder}", headers=POST, content_type="multipart/form-data",
                       data={"files": (io.BytesIO(data), name)})


def library(client):
    return client.get("/api/library", headers=AUTH).get_json()


def frame(mpv_call, expected_ratio):
    """Wait until the frame mpv draws (after the format filter) has this shape."""
    params = None
    for _ in range(40):
        params = mpv_call(["get_property", "video-out-params"])[0]
        if params and abs(params["w"] / params["h"] - expected_ratio) < 0.01:
            return params
        time.sleep(0.1)
    raise AssertionError(f"frame {params} never reached ratio {expected_ratio:.3f}")


def test_login_required(env):
    _, client, _, _ = env
    assert client.get("/").status_code == 401
    bad = {"Authorization": "Basic " + base64.b64encode(b"tester:wrong").decode()}
    assert client.get("/", headers=bad).status_code == 401
    assert client.get("/", headers=AUTH).status_code == 200


def test_posts_need_page_header(env):
    _, client, _, _ = env
    assert client.post("/api/player/next", headers=AUTH).status_code == 400


def test_library_lists_folders_and_tidies_loose_images(env):
    _, client, slides, _ = env
    lib = library(client)
    flat = {f["name"]: [s["name"] for s in f["slides"]] for f in lib["formats"]["flat"]}
    assert flat == {"Coming soon": ["a.jpg", "B.png"], "General": ["loose.jpg"],
                    "Kids": ["slide2.jpg", "slide10.jpg"]}  # natural order, notes.txt left out
    assert not (slides / "flat" / "loose.jpg").exists()  # moved into General
    assert [f["name"] for f in lib["formats"]["scope"]] == ["Coming soon"]
    assert lib["duration"] == 10 and lib["shuffle"] is False


def test_player_starts_with_the_playlist_file(env):
    app, _, _, _ = env
    assert playlist_file(app) == ALL_FLAT
    assert playing(app) == ALL_FLAT


def test_flat_profile_frames_exactly(env):
    app, client, _, _ = env
    post(client, "/api/show/Coming soon/a.jpg")
    wait_for(client, current=at("Coming soon", "a.jpg"))
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
    assert post(client, "/api/show/Kids/slide2.jpg").status_code == 200
    wait_for(client, current=at("Kids", "slide2.jpg"))
    post(client, "/api/player/next")
    wait_for(client, current=at("Kids", "slide10.jpg"))
    post(client, "/api/player/next")  # wraps around
    wait_for(client, current=at("Coming soon", "a.jpg"))
    post(client, "/api/player/prev")
    wait_for(client, current=at("Kids", "slide10.jpg"))


def test_pause_and_play(env):
    _, client, _, _ = env
    post(client, "/api/player/pause")
    wait_for(client, paused=True)
    post(client, "/api/player/play")
    wait_for(client, paused=False)


def test_pin_and_resume(env):
    app, client, _, _ = env
    post(client, "/api/pin/Coming soon/B.png")
    wait_for(client, pinned=True, current=at("Coming soon", "B.png"))
    assert playing(app) == ["Coming soon/B.png"]
    post(client, "/api/resume")
    wait_for(client, pinned=False, paused=False, current=at("Coming soon", "B.png"))
    assert sorted(playing(app)) == sorted(ALL_FLAT)


def test_switching_a_slide_off_keeps_the_screen_steady(env):
    app, client, _, _ = env
    post(client, "/api/show/Kids/slide2.jpg")
    wait_for(client, current=at("Kids", "slide2.jpg"))
    assert post(client, "/api/slides/flat/Kids/slide10.jpg/switch", {"on": False}).status_code == 200
    assert "Kids/slide10.jpg" not in playing(app)
    assert "Kids/slide10.jpg" not in playlist_file(app)  # also after a restart
    assert status(client)["current"] == at("Kids", "slide2.jpg")  # not reloaded
    assert app.mpv(["get_property", "playlist-pos"])[0] == 0  # the rest was swapped around it
    lib = library(client)
    kids = next(f for f in lib["formats"]["flat"] if f["name"] == "Kids")
    assert [s["on"] for s in kids["slides"]] == [True, False]


def test_switched_off_slide_cannot_be_shown_but_can_be_pinned(env):
    _, client, _, _ = env
    assert post(client, "/api/show/Kids/slide10.jpg").status_code == 409
    assert post(client, "/api/pin/Kids/slide10.jpg").status_code == 200
    wait_for(client, pinned=True, current=at("Kids", "slide10.jpg"))
    post(client, "/api/resume")  # not in the slideshow, so the next due slide follows
    wait_for(client, pinned=False)
    assert status(client)["current"] != at("Kids", "slide10.jpg")


def test_switching_off_the_slide_on_screen_moves_on(env):
    app, client, _, _ = env
    post(client, "/api/show/Coming soon/a.jpg")
    wait_for(client, current=at("Coming soon", "a.jpg"))
    post(client, "/api/slides/flat/Coming soon/a.jpg/switch", {"on": False})
    wait_for(client, current=at("Coming soon", "B.png"))  # the slide that was due next
    post(client, "/api/slides/flat/Coming soon/a.jpg/switch", {"on": True})
    post(client, "/api/slides/flat/Kids/slide10.jpg/switch", {"on": True})
    assert sorted(playing(app)) == sorted(ALL_FLAT)


def test_folder_switch_and_play_only_one_folder(env):
    app, client, _, _ = env
    post(client, "/api/folders/flat/Kids/switch", {"on": False})
    assert all(not p.startswith("Kids/") for p in playing(app))
    assert post(client, "/api/show/Kids/slide2.jpg").status_code == 409
    post(client, "/api/folders/flat/Coming soon/switch", {"only": True})
    assert sorted(playing(app)) == ["Coming soon/B.png", "Coming soon/a.jpg"]
    assert [f["on"] for f in library(client)["formats"]["flat"]] == [True, False, False]
    for folder in ("General", "Kids"):
        post(client, f"/api/folders/flat/{folder}/switch", {"on": True})
    assert sorted(playing(app)) == sorted(ALL_FLAT)


def test_shuffle(env):
    app, client, _, _ = env
    orders = set()
    for _ in range(8):  # each reshuffle keeps the same slides
        post(client, "/api/settings", {"shuffle": False})
        post(client, "/api/settings", {"shuffle": True})
        assert sorted(playlist_file(app)) == sorted(ALL_FLAT)
        assert sorted(playing(app)) == sorted(ALL_FLAT)
        orders.add(tuple(playlist_file(app)))
    assert len(orders) > 1  # 8 shuffles of 5 slides are not all the same
    assert library(client)["shuffle"] is True
    post(client, "/api/settings", {"shuffle": False})
    assert playlist_file(app) == ALL_FLAT


def test_duration(env):
    app, client, _, _ = env
    assert post(client, "/api/settings", {"duration": 15}).status_code == 200
    assert app.mpv(["get_property", "image-display-duration"])[0] == 15
    with open(app.PLAYER_CONF) as f:
        assert f.read() == "image-display-duration=15\n"
    for bad in (0, 5000, "abc", None, float("nan")):
        assert post(client, "/api/settings", {"duration": bad}).status_code == 400
    assert library(client)["duration"] == 15


def test_create_folder(env):
    _, client, slides, _ = env
    res = post(client, "/api/folders/flat", {"name": "  Vianoce   2026 "})
    assert res.status_code == 200 and res.get_json()["name"] == "Vianoce 2026"
    assert (slides / "flat" / "Vianoce 2026").is_dir()
    assert post(client, "/api/folders/flat", {"name": "vianoce 2026"}).status_code == 409
    for bad in ("", "   ", "../x", "a/b", ".hidden", "x" * 61, "tab\x01"):
        assert post(client, "/api/folders/flat", {"name": bad}).status_code == 400, bad
    assert post(client, "/api/folders/imax", {"name": "x"}).status_code == 404
    assert post(client, "/api/folders/scope", {"name": "Vianoce 2026"}).status_code == 200


def test_upload_into_a_folder_joins_the_slideshow(env):
    app, client, slides, _ = env
    res = upload(client, "flat", "Vianoce 2026", make_image("PNG"), "../../Šťastný film.PNG")
    assert res.status_code == 200, res.get_json()
    assert res.get_json()["saved"] == ["Stastny_film.png"]
    assert (slides / "flat" / "Vianoce 2026" / "Stastny_film.png").exists()
    assert "Vianoce 2026/Stastny_film.png" in playing(app)
    res = upload(client, "flat", "Vianoce 2026", make_image("PNG"), "Stastny film.png")
    assert res.get_json()["saved"] == ["Stastny_film-1.png"]  # no overwriting


def test_upload_to_other_format_is_only_stored(env):
    app, client, slides, _ = env
    res = upload(client, "scope", "Vianoce 2026", make_image(size=(2048, 858)), "Dune.jpg")
    assert res.get_json()["saved"] == ["Dune.jpg"]
    assert (slides / "scope" / "Vianoce 2026" / "Dune.jpg").exists()
    assert all("Dune.jpg" not in p for p in playing(app))


def test_upload_checks(env):
    app, client, slides, _ = env
    assert upload(client, "flat", "Kids", b"#!/bin/sh", "evil.sh").status_code == 400
    assert not (slides / "flat" / "Kids" / "evil.sh").exists()
    big = b"0" * (app.MAX_UPLOAD_MB * 1024 * 1024 + 1)
    assert upload(client, "flat", "Kids", big, "big.jpg").status_code == 413
    assert upload(client, "flat", "Nope", make_image(), "x.jpg").status_code == 404


def test_move_slide(env):
    app, client, slides, _ = env
    post(client, "/api/slides/flat/Vianoce 2026/Stastny_film-1.png/switch", {"on": False})
    res = post(client, "/api/slides/flat/Vianoce 2026/Stastny_film-1.png/move", {"to": "Kids"})
    assert res.status_code == 200 and res.get_json()["name"] == "Stastny_film-1.png"
    assert (slides / "flat" / "Kids" / "Stastny_film-1.png").exists()
    kids = next(f for f in library(client)["formats"]["flat"] if f["name"] == "Kids")
    moved = next(s for s in kids["slides"] if s["name"] == "Stastny_film-1.png")
    assert moved["on"] is False  # a switched-off slide stays off after a move
    # a name that exists in the target gets a suffix
    upload(client, "flat", "Kids", make_image(), "a.jpg")
    res = post(client, "/api/slides/flat/Kids/a.jpg/move", {"to": "Coming soon"})
    assert res.get_json()["name"] == "a-1.jpg"
    assert "Coming soon/a-1.jpg" in playing(app) and "Kids/a.jpg" not in playing(app)
    assert post(client, "/api/slides/flat/Kids/slide2.jpg/move", {"to": "Nope"}).status_code == 404
    assert post(client, "/api/slides/flat/Kids/slide2.jpg/move", {"to": "Kids"}).status_code == 200


def test_delete_slide(env):
    app, client, slides, _ = env
    assert client.delete("/api/slides/flat/Coming soon/a-1.jpg", headers=POST).status_code == 200
    assert not (slides / "flat" / "Coming soon" / "a-1.jpg").exists()
    assert "Coming soon/a-1.jpg" not in playing(app)
    client.delete("/api/slides/flat/Kids/Stastny_film-1.png", headers=POST)
    assert all("Stastny_film-1" not in key for key in app.state["slides_off"])  # forgotten


def test_delete_folder_only_when_empty(env):
    _, client, slides, _ = env
    assert client.delete("/api/folders/flat/Vianoce 2026", headers=POST).status_code == 409
    client.delete("/api/slides/flat/Vianoce 2026/Stastny_film.png", headers=POST)
    assert client.delete("/api/folders/flat/Vianoce 2026", headers=POST).status_code == 200
    assert not (slides / "flat" / "Vianoce 2026").exists()


def test_files_copied_in_or_removed_outside_the_page(env):
    app, client, slides, _ = env
    shutil.copy(slides / "flat" / "Kids" / "slide2.jpg", slides / "flat" / "Kids" / "copied.jpg")
    shutil.copy(slides / "flat" / "Kids" / "slide2.jpg", slides / "flat" / "dropped.jpg")  # no folder
    app.pick_up_outside_changes()
    assert "Kids/copied.jpg" in playing(app) and "General/dropped.jpg" in playing(app)
    (slides / "flat" / "Kids" / "copied.jpg").unlink()
    app.pick_up_outside_changes()
    assert "Kids/copied.jpg" not in playing(app)
    before = app.mpv(["get_property", "playlist"])[0]
    app.pick_up_outside_changes()  # nothing changed, nothing touched
    assert app.mpv(["get_property", "playlist"])[0] == before
    client.delete("/api/slides/flat/General/dropped.jpg", headers=POST)


def test_unknown_or_sneaky_paths_are_404(env):
    _, client, _, _ = env
    assert post(client, "/api/show/Kids/missing.jpg").status_code == 404
    assert post(client, "/api/show/Kids/notes.txt").status_code == 404
    assert post(client, "/api/show/scope/a.jpg").status_code == 404
    assert client.delete("/api/slides/flat/..%2F..%2F/app.py", headers=POST).status_code == 404
    assert client.delete("/api/slides/flat/../Kids/slide2.jpg", headers=POST).status_code == 404
    assert client.delete("/api/folders/flat/..", headers=POST).status_code == 404
    assert client.get("/thumb/flat/Kids/missing.jpg", headers=AUTH).status_code == 404


def test_thumbnails(env):
    _, client, _, _ = env
    for url in ("/thumb/flat/Kids/slide2.jpg", "/thumb/scope/Coming soon/a.jpg"):
        res = client.get(url, headers=AUTH)
        assert res.status_code == 200 and res.mimetype == "image/jpeg"
        assert Image.open(io.BytesIO(res.data)).size[0] <= 400


def test_restart_brings_back_slideshow_and_duration(env):
    app, client, _, player = env
    post(client, "/api/slides/flat/General/loose.jpg/switch", {"on": False})
    player.stop()
    player.start()  # like systemd restarting mpv, or a reboot
    wait_for(client, pinned=False)
    assert "General/loose.jpg" not in playing(app)
    assert app.mpv(["get_property", "image-display-duration"])[0] == 15
    frame(app.mpv, FLAT_RATIO)
    with open(app.STATE_FILE) as f:
        assert "flat/General/loose.jpg" in json.load(f)["slides_off"]


def test_player_down_is_reported_but_files_still_work(env):
    _, client, _, player = env
    player.stop()
    res = client.get("/api/status", headers=AUTH)
    assert res.status_code == 503 and "not running" in res.get_json()["error"]
    assert client.get("/api/library", headers=AUTH).status_code == 200
    assert upload(client, "flat", "Kids", make_image(), "while-down.jpg").status_code == 200
    assert post(client, "/api/slides/flat/Kids/while-down.jpg/switch", {"on": False}).status_code == 200


def test_invalid_format_is_refused(env, monkeypatch):
    import app
    monkeypatch.setenv("PRESENTER_FORMAT", "imax")
    with pytest.raises(SystemExit, match="flat, scope"):
        importlib.reload(app)
    monkeypatch.setenv("PRESENTER_FORMAT", "flat")
    importlib.reload(app)
