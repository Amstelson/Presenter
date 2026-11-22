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
import struct
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
            "--image-display-duration=300", f"--input-ipc-server={sock}", "--no-input-default-bindings",
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


def eventually(check, what):
    """Wait for check() to be true: mpv loads files in the background."""
    for _ in range(40):
        if check():
            return
        time.sleep(0.1)
    raise AssertionError(f"never happened: {what}")


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


def test_no_login_without_a_password(env, monkeypatch):
    app, client, _, _ = env
    monkeypatch.setattr(app, "PASSWORD", "")
    assert client.get("/").status_code == 200
    assert client.get("/api/library").status_code == 200
    assert client.post("/api/player/play").status_code == 400  # other websites are still blocked
    assert client.post("/api/player/play", headers={"X-Requested-With": "presenter"}).status_code == 200


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
    eventually(lambda: playing(app) == ALL_FLAT, "mpv plays the playlist file")


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
    eventually(lambda: status(client)["current"] != at("Kids", "slide10.jpg"), "another slide follows")


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


def test_present_a_folder(env):
    app, client, _, _ = env
    post(client, "/api/folders/flat/Kids/switch", {"on": False})  # can be off in the slideshow
    assert post(client, "/api/present/Kids").status_code == 200
    state = wait_for(client, presenting=True, paused=True, current=at("Kids", "slide2.jpg"))
    assert (state["position"], state["count"]) == (1, 2)
    assert playing(app) == ["Kids/slide2.jpg", "Kids/slide10.jpg"]  # only this folder, in order
    post(client, "/api/player/next")
    wait_for(client, current=at("Kids", "slide10.jpg"), position=2)
    post(client, "/api/player/next")  # at the last slide: stays, does not start over
    time.sleep(0.4)
    assert status(client)["current"] == at("Kids", "slide10.jpg")
    post(client, "/api/player/prev")
    post(client, "/api/player/prev")  # at the first slide: stays
    wait_for(client, current=at("Kids", "slide2.jpg"))
    time.sleep(0.4)
    assert status(client)["current"] == at("Kids", "slide2.jpg") and status(client)["paused"]


def test_presentation_is_left_alone_by_slideshow_changes(env):
    app, client, _, _ = env
    post(client, "/api/slides/flat/Coming soon/B.png/switch", {"on": False})
    assert playing(app) == ["Kids/slide2.jpg", "Kids/slide10.jpg"]  # the presentation goes on
    assert "Coming soon/B.png" not in playlist_file(app)  # the slideshow is updated for later
    post(client, "/api/slides/flat/Coming soon/B.png/switch", {"on": True})
    assert status(client)["presenting"]


def test_present_from_a_slide_and_jump_within(env):
    app, client, _, _ = env
    post(client, "/api/present/Kids", {"start": "slide10.jpg"})
    wait_for(client, presenting=True, current=at("Kids", "slide10.jpg"), position=2)
    assert playing(app) == ["Kids/slide2.jpg", "Kids/slide10.jpg"]
    post(client, "/api/show/Kids/slide2.jpg")  # Show now jumps within the presentation
    wait_for(client, presenting=True, paused=True, current=at("Kids", "slide2.jpg"))


def test_resume_ends_the_presentation(env):
    app, client, _, _ = env
    post(client, "/api/resume")
    wait_for(client, presenting=False, paused=False)
    assert app.mpv(["get_property", "loop-playlist"])[0] == "inf"
    eventually(lambda: sorted(playing(app)) == sorted(p for p in ALL_FLAT if not p.startswith("Kids/")),
               "the slideshow, still without the switched-off Kids folder")
    post(client, "/api/folders/flat/Kids/switch", {"on": True})
    assert sorted(playing(app)) == sorted(ALL_FLAT)


def test_presentation_details(env):
    app, client, _, _ = env
    assert post(client, "/api/present/Nope").status_code == 404
    post(client, "/api/folders/flat", {"name": "Empty"})
    assert post(client, "/api/present/Empty").status_code == 409
    client.delete("/api/folders/flat/Empty", headers=POST)
    post(client, "/api/slides/flat/Coming soon/a.jpg/switch", {"on": False})
    post(client, "/api/present/Coming soon")  # switched-off slides are left out
    wait_for(client, presenting=True, current=at("Coming soon", "B.png"))
    assert playing(app) == ["Coming soon/B.png"]
    post(client, "/api/slides/flat/Coming soon/a.jpg/switch", {"on": True})
    post(client, "/api/pin/Kids/slide2.jpg")  # Only this ends the presentation
    wait_for(client, presenting=False, pinned=True, current=at("Kids", "slide2.jpg"))
    post(client, "/api/resume")
    wait_for(client, presenting=False, pinned=False, paused=False)


def key_bitmap(codes):
    """A key capability bitmap as Linux shows it in sysfs: hex words, highest first."""
    bits, digits = sum(1 << c for c in codes), struct.calcsize("l") * 2
    words = []
    while bits:
        words.append(f"{bits & ((1 << digits * 4) - 1):x}")
        bits >>= digits * 4
    return " ".join(reversed(words)) or "0"


def key_events(*keys):
    """Input events as a device sends them: (code, value) pairs, value 1 = press."""
    return b"".join(struct.pack("llHHi", 0, 0, 1, code, value) for code, value in keys)


def fake_device(app, tmp_path, name, keys, events=b""):
    (tmp_path / "dev").mkdir(exist_ok=True)
    (tmp_path / "sys" / name / "device" / "capabilities").mkdir(parents=True)
    (tmp_path / "sys" / name / "device" / "capabilities" / "key").write_text(key_bitmap(keys) + "\n")
    (tmp_path / "dev" / name).write_bytes(events)
    app.INPUT_DIR, app.INPUT_SYS = str(tmp_path / "dev"), str(tmp_path / "sys")
    return str(tmp_path / "dev" / name)


def test_finds_clickers_and_keyboards_but_not_mice(env, tmp_path, monkeypatch):
    app = env[0]
    monkeypatch.setattr(app, "INPUT_DIR", app.INPUT_DIR)
    monkeypatch.setattr(app, "INPUT_SYS", app.INPUT_SYS)
    fake_device(app, tmp_path, "event0", {272, 273})  # a mouse: left and right button
    clicker = fake_device(app, tmp_path, "event1", {104, 109, 63, 1, 52})  # Page Up/Down, F5, Esc, .
    keyboard = fake_device(app, tmp_path, "event2", set(range(1, 128)))
    fake_device(app, tmp_path, "event3", {113, 114, 115})  # volume keys only
    assert app.clicker_devices() == [clicker, keyboard]


def test_one_press_is_one_step(env):
    app = env[0]
    events = key_events((109, 1), (109, 2), (109, 2), (109, 0),  # Page Down pressed, held, let go
                        (103, 1), (30, 1), (1, 1))  # Up, then "a" and Esc (not clicker keys)
    events += struct.pack("llHHi", 0, 0, 0, 0, 0)  # a sync event
    assert app.pressed(events) == ["next", "prev"]


def test_clicker_moves_the_slides(env, tmp_path, monkeypatch):
    app, client, _, _ = env
    monkeypatch.setattr(app, "INPUT_DIR", app.INPUT_DIR)
    monkeypatch.setattr(app, "INPUT_SYS", app.INPUT_SYS)
    post(client, "/api/show/Coming soon/a.jpg")
    wait_for(client, current=at("Coming soon", "a.jpg"))
    clicker = fake_device(app, tmp_path, "event4", {104, 109}, key_events((109, 1), (109, 0)))
    devices = {}
    app.poll_clickers(devices, timeout=1)  # reads the Page Down press
    wait_for(client, current=at("Coming soon", "B.png"))
    app.poll_clickers(devices, timeout=1)  # nothing more to read: like the clicker unplugged
    assert clicker not in devices
    post(client, "/api/pin/Coming soon/B.png")
    wait_for(client, pinned=True)
    with open(clicker, "wb") as f:
        f.write(key_events((109, 1)))
    app.poll_clickers({}, timeout=1)  # "Only this" ignores the clicker, like the page's buttons
    time.sleep(0.4)
    assert status(client)["current"] == at("Coming soon", "B.png")
    post(client, "/api/resume")
    wait_for(client, pinned=False, paused=False)


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


def test_delete_folder(env):
    _, client, slides, _ = env
    assert client.delete("/api/folders/flat/Vianoce 2026", headers=POST).status_code == 409  # not empty
    client.delete("/api/slides/flat/Vianoce 2026/Stastny_film.png", headers=POST)
    assert client.delete("/api/folders/flat/Vianoce 2026", headers=POST).status_code == 200
    assert not (slides / "flat" / "Vianoce 2026").exists()


def test_several_slides_at_once(env):
    app, client, slides, _ = env
    post(client, "/api/folders/flat", {"name": "Batch"})
    post(client, "/api/folders/flat", {"name": "Archive"})
    for name in ("b1.jpg", "b2.jpg", "b3.jpg", "b4.jpg"):
        upload(client, "flat", "Batch", make_image(), name)
    url = "/api/slides/flat/Batch"
    res = post(client, url, {"names": ["b1.jpg", "b2.jpg", "gone.jpg", "b1.jpg"], "action": "off"})
    assert res.get_json() == {"done": 2, "skipped": 2}  # a missing name and a repeat are skipped
    assert "Batch/b1.jpg" not in playing(app) and "Batch/b3.jpg" in playing(app)
    post(client, url, {"names": ["b1.jpg", "b2.jpg", "b3.jpg"], "action": "move", "to": "Archive"})
    assert sorted(os.listdir(slides / "flat" / "Archive")) == ["b1.jpg", "b2.jpg", "b3.jpg"]
    archive = next(f for f in library(client)["formats"]["flat"] if f["name"] == "Archive")
    assert [s["on"] for s in archive["slides"]] == [False, False, True]  # off stays off
    post(client, "/api/slides/flat/Archive", {"names": ["b1.jpg", "b2.jpg"], "action": "on"})
    assert "Archive/b1.jpg" in playing(app)
    post(client, "/api/slides/flat/Archive", {"names": ["b1.jpg", "b3.jpg"], "action": "delete"})
    assert os.listdir(slides / "flat" / "Archive") == ["b2.jpg"]
    assert "Archive/b1.jpg" not in playing(app)
    assert post(client, url, {"names": ["b4.jpg"], "action": "explode"}).status_code == 400
    assert post(client, url, {"names": "b4.jpg", "action": "delete"}).status_code == 400
    assert post(client, url, {"names": ["b4.jpg"], "action": "move", "to": "Nope"}).status_code == 404
    assert (slides / "flat" / "Batch" / "b4.jpg").exists()  # nothing touched by the bad requests


def test_delete_folder_with_its_slides(env):
    app, client, slides, _ = env
    post(client, "/api/slides/flat/Archive/b2.jpg/switch", {"on": False})
    client.get("/thumb/flat/Archive/b2.jpg", headers=AUTH)  # make a cached thumbnail
    res = client.delete("/api/folders/flat/Archive?with_slides=1", headers=POST)
    assert res.status_code == 200
    assert not (slides / "flat" / "Archive").exists()
    assert not os.path.exists(os.path.join(app.THUMB_DIR, "flat", "Archive"))
    assert all(not k.startswith("flat/Archive") for k in app.state["slides_off"])
    assert all(not p.startswith("Archive/") for p in playing(app))
    client.delete("/api/folders/flat/Batch?with_slides=1", headers=POST)


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
    eventually(lambda: playing(app) == playlist_file(app), "mpv plays the saved slideshow")
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


def test_no_password_is_the_default(env, monkeypatch):
    import app
    monkeypatch.delenv("PRESENTER_PASSWORD")
    try:
        importlib.reload(app)
        assert app.PASSWORD == ""
    finally:
        monkeypatch.setenv("PRESENTER_PASSWORD", "secret")
        importlib.reload(app)


def test_invalid_format_is_refused(env, monkeypatch):
    import app
    monkeypatch.setenv("PRESENTER_FORMAT", "imax")
    with pytest.raises(SystemExit, match="flat, scope"):
        importlib.reload(app)
    monkeypatch.setenv("PRESENTER_FORMAT", "flat")
    importlib.reload(app)
