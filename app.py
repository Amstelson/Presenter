#!/usr/bin/env python3
"""Presenter - a small web remote for an mpv image slideshow.

mpv shows the slides fullscreen. This Flask app serves a control page and
talks to mpv through its JSON IPC socket. See README.md for setup.

Slides live in <slides>/<format>/<folder>/<image>, where <format> is flat or
scope. A Pi shows only its own format: every slide that is switched on, in a
folder that is switched on, in order or shuffled.
"""
import hmac
import json
import os
import random
import re
import shutil
import socket
import threading
import time
from contextlib import suppress
from functools import wraps

from flask import Flask, Response, abort, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

try:
    from PIL import Image, ImageOps
except ImportError:  # no Pillow: thumbnails fall back to the full image
    Image = None

# --- Settings (environment variables override these defaults) ---
SLIDES_DIR = os.path.abspath(os.path.expanduser(os.environ.get("PRESENTER_SLIDES", "~/slides")))
MPV_SOCKET = os.environ.get("PRESENTER_SOCKET", "/tmp/mpv.sock")
USERNAME = os.environ.get("PRESENTER_USER", "cinema")
PASSWORD = os.environ.get("PRESENTER_PASSWORD", "change-me")
PORT = int(os.environ.get("PRESENTER_PORT", "8888"))
DEFAULT_DURATION = float(os.environ.get("PRESENTER_DURATION", "10"))  # seconds per slide
MAX_UPLOAD_MB = 50
MIN_FREE_MB = 500  # refuse uploads when the SD card gets this full
EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
THUMB_DIR = os.path.expanduser("~/.cache/presenter/thumbs")
THUMB_SIZE = 400
# One slides subfolder per screen format: name -> (label, picture width / height).
# The mpv framing for each format is in mpv/formats.conf.
FORMATS = {"flat": ("Flat", 1998 / 1080), "scope": ("Scope", 2048 / 858)}
# The format of this Pi's screen. Only its slides are shown, the other format is just stored.
FORMAT = os.environ.get("PRESENTER_FORMAT", "flat")
if FORMAT not in FORMATS:
    raise SystemExit(f"PRESENTER_FORMAT must be one of: {', '.join(FORMATS)}")
PLAY_DIR = os.path.join(SLIDES_DIR, FORMAT)
DEFAULT_FOLDER = "General"  # images put directly into a format folder are moved here
CONFIG_DIR = os.path.expanduser("~/.config/presenter")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")  # what was set on the web page
PLAYLIST_FILE = os.path.join(CONFIG_DIR, "playlist.m3u8")  # what mpv plays when it starts
PLAYER_CONF = os.path.join(CONFIG_DIR, "player.conf")  # mpv options set on the web page

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
lock = threading.RLock()  # one change at a time, so files, state and player stay in step
thumb_slots = threading.Semaphore(2)  # limit parallel thumbnail work to save RAM
os.makedirs(CONFIG_DIR, exist_ok=True)
for fmt in FORMATS:
    os.makedirs(os.path.join(SLIDES_DIR, fmt), exist_ok=True)


class MpvError(Exception):
    pass


def mpv(*commands):
    """Send commands to mpv over one socket connection and return their results."""
    if not commands:
        return []
    try:  # a fresh connection per call, so an mpv restart never leaves us stuck
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(3)
            sock.connect(MPV_SOCKET)
            sock.sendall(b"".join(json.dumps({"command": c, "request_id": i}).encode() + b"\n"
                                  for i, c in enumerate(commands)))
            replies = {}
            with sock.makefile("rb") as lines:
                for line in lines:  # skip event messages, collect our replies
                    msg = json.loads(line)
                    if "request_id" in msg and "event" not in msg:
                        replies[msg["request_id"]] = msg
                    if len(replies) == len(commands):
                        break
    except (OSError, ValueError) as e:
        raise MpvError(f"Slideshow player is not running ({e})")
    results = []
    for i, cmd in enumerate(commands):
        reply = replies.get(i)
        if reply is None:
            raise MpvError("Slideshow player did not answer")
        if reply["error"] != "success" and cmd[0] != "get_property":
            raise MpvError(f"Slideshow player error: {reply['error']}")
        results.append(reply.get("data"))
    return results


def player_state():
    paused, loop, path = mpv(["get_property", "pause"], ["get_property", "loop-file"],
                             ["get_property", "path"])
    return {"paused": bool(paused), "pinned": loop == "inf", "path": path}


def mpv_playlist():
    return [e["filename"] for e in mpv(["get_property", "playlist"])[0] or []]


# --- Settings made on the web page ---

def write_file(path, text):
    """Swap in a complete new file, so a power cut never leaves half of one."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_state():
    # folders_off and slides_off hold paths like "flat/Kids" and "flat/Kids/dino.jpg"
    state = {"duration": DEFAULT_DURATION, "shuffle": False, "folders_off": [], "slides_off": []}
    with suppress(OSError, ValueError):
        with open(STATE_FILE) as f:
            state.update(json.load(f))
    return state


def save_state():
    write_file(STATE_FILE, json.dumps(state, indent=1, ensure_ascii=False))
    write_file(PLAYER_CONF, f"image-display-duration={state['duration']:g}\n")


def switch(key, kind, on):
    """Switch a folder or slide (kind "folders_off" or "slides_off") on or off."""
    off = state[kind]
    if on and key in off:
        off.remove(key)
    elif not on and key not in off:
        off.append(key)


# --- The library: format folders, slide folders, slides ---

def natural_key(name):
    """Sort like people do: case-insensitive, "slide2" before "slide10"."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name.lower())]


def is_image(name):
    return not name.startswith(".") and os.path.splitext(name)[1].lower() in EXTENSIONS


def unique_name(folder_dir, name):
    base, ext = os.path.splitext(name)
    n = 1
    while os.path.exists(os.path.join(folder_dir, name)):
        name, n = f"{base}-{n}{ext}", n + 1
    return name


def folders(fmt):
    """Slide folders of a format. Images put directly into the format folder
    (for example copied over the network) are moved to DEFAULT_FOLDER first."""
    root = os.path.join(SLIDES_DIR, fmt)
    with os.scandir(root) as entries:
        loose = [e.name for e in entries if e.is_file() and is_image(e.name)]
    if loose:
        target = os.path.join(root, DEFAULT_FOLDER)
        os.makedirs(target, exist_ok=True)
        for name in loose:
            with suppress(OSError):
                os.rename(os.path.join(root, name), os.path.join(target, unique_name(target, name)))
    with os.scandir(root) as entries:
        return sorted((e.name for e in entries if e.is_dir() and not e.name.startswith(".")),
                      key=natural_key)


def slides(fmt, folder):
    """Images in a folder as {name: mtime}, in order."""
    with suppress(FileNotFoundError), os.scandir(os.path.join(SLIDES_DIR, fmt, folder)) as entries:
        found = {e.name: int(e.stat().st_mtime) for e in entries if e.is_file() and is_image(e.name)}
        return {n: found[n] for n in sorted(found, key=natural_key)}
    return {}


def folder_path(fmt, folder):
    """Path of an existing folder. Anything else, "../" tricks included, is a 404."""
    if fmt not in FORMATS or folder not in folders(fmt):
        abort(404)
    return os.path.join(SLIDES_DIR, fmt, folder)


def slide_path(fmt, folder, name):
    path = folder_path(fmt, folder)
    if name not in slides(fmt, folder):
        abort(404)
    return os.path.join(path, name)


def drop_thumb(fmt, folder, name):
    with suppress(FileNotFoundError):
        os.remove(os.path.join(THUMB_DIR, fmt, folder, name + ".jpg"))


# --- What the screen shows ---

def playlist_paths():
    """This screen's slideshow: switched-on slides in switched-on folders."""
    paths = [os.path.join(PLAY_DIR, folder, name)
             for folder in folders(FORMAT) if f"{FORMAT}/{folder}" not in state["folders_off"]
             for name in slides(FORMAT, folder) if f"{FORMAT}/{folder}/{name}" not in state["slides_off"]]
    if state["shuffle"]:
        random.shuffle(paths)
    return paths


def load_playlist(paths, start=None):
    """Give mpv a new playlist. Without `start`, the image on screen stays if it is
    still in the slideshow (no flicker), otherwise the slide that was due next follows."""
    current = mpv(["get_property", "path"])[0]
    if not paths:
        mpv(["stop"])
        return
    if start is None and current in paths:  # swap everything around the image on screen
        i = paths.index(current)
        mpv(["playlist-clear"], *(["loadfile", p, "append"] for p in paths[i + 1:] + paths[:i]))
        return
    if start is None:
        old = mpv_playlist()
        i = old.index(current) if current in old else -1
        start = next((p for p in old[i + 1:] + old[:i + 1] if p in paths), paths[0])
    i = paths.index(start)
    paths = paths[i:] + paths[:i]
    mpv(["loadfile", paths[0], "replace"], *(["loadfile", p, "append"] for p in paths[1:]))


def write_playlist_file(paths):
    global synced
    write_file(PLAYLIST_FILE, "#EXTM3U\n" + "".join(p + "\n" for p in paths))
    synced = sorted(paths)


def sync_player():
    """Bring the player, and the playlist file it starts with, in line with the library."""
    paths = playlist_paths()
    write_playlist_file(paths)
    with suppress(MpvError):  # if mpv is down it reads the playlist file when it starts
        state_now = player_state()
        if state_now["pinned"] and state_now["path"] and os.path.exists(state_now["path"]):
            return  # "Only this" stays on screen, Resume brings the new slideshow
        mpv(["set_property", "loop-file", "no"])
        load_playlist(paths)


def pick_up_outside_changes():
    """Slides copied in or deleted without the web page (over the network, from a
    USB stick) join or leave the slideshow. Runs every 30 seconds."""
    with lock:
        if sorted(playlist_paths()) != synced:
            sync_player()


def watch_library():
    while True:
        time.sleep(30)
        with suppress(Exception):  # e.g. a folder deleted halfway through a check
            pick_up_outside_changes()


def where(path):
    """{"folder", "name"} of a path in this screen's format folder, else None."""
    folder, name = os.path.split(os.path.relpath(path, PLAY_DIR)) if path else ("", "")
    if not folder or os.sep in folder or folder.startswith(".."):
        return None
    return {"folder": folder, "name": name}


def save_upload(file, folder_dir):
    base, ext = os.path.splitext(file.filename)
    name = unique_name(folder_dir, (secure_filename(base) or "image") + ext.lower())
    tmp = os.path.join(folder_dir, f".{name}.part")  # hidden until complete
    try:
        file.save(tmp)
        os.replace(tmp, os.path.join(folder_dir, name))
    finally:  # no half-written leftovers if the disk fills up
        with suppress(FileNotFoundError):
            os.remove(tmp)
    return name


def body():
    return request.get_json(silent=True) or {}


def mutating(view):
    """Run changes one at a time (several staff members may click at once)."""
    @wraps(view)
    def locked(*args, **kwargs):
        with lock:
            return view(*args, **kwargs)
    return locked


@app.before_request
def require_login():
    auth = request.authorization
    given = f"{auth.username}:{auth.password}" if auth else ""
    if not hmac.compare_digest(given.encode(), f"{USERNAME}:{PASSWORD}".encode()):
        return Response("Login required\n", 401, {"WWW-Authenticate": 'Basic realm="Presenter"'})
    # Only our own page sends this header, which blocks cross-site form posts.
    if request.method not in ("GET", "HEAD") and request.headers.get("X-Requested-With") != "presenter":
        abort(400)


@app.errorhandler(MpvError)
def mpv_error(e):
    return jsonify(error=str(e)), 503


@app.errorhandler(404)
def not_found(e):
    return jsonify(error="Not found. It may have been moved or deleted."), 404


@app.errorhandler(413)
def too_large(e):
    return jsonify(error=f"File is too large (max {MAX_UPLOAD_MB} MB)"), 413


@app.get("/")
def index():
    formats = [(n, label, ratio) for n, (label, ratio) in FORMATS.items()]
    return render_template("index.html", max_upload_mb=MAX_UPLOAD_MB, formats=formats,
                           on_screen=FORMAT)


@app.get("/api/library")
def library():
    return jsonify(duration=state["duration"], shuffle=state["shuffle"], formats={
        fmt: [{"name": folder, "on": f"{fmt}/{folder}" not in state["folders_off"],
               "slides": [{"name": n, "mtime": t, "on": f"{fmt}/{folder}/{n}" not in state["slides_off"]}
                          for n, t in slides(fmt, folder).items()]}
              for folder in folders(fmt)]
        for fmt in FORMATS})


@app.get("/api/status")
def status():
    state_now = player_state()
    return jsonify(paused=state_now["paused"], pinned=state_now["pinned"],
                   current=where(state_now["path"]))


@app.post("/api/settings")
@mutating
def settings():
    data = body()
    if "duration" in data:
        try:
            duration = float(data["duration"])
        except (TypeError, ValueError):
            duration = 0
        if not 1 <= duration <= 3600:
            return jsonify(error="Seconds per slide must be between 1 and 3600"), 400
        state["duration"] = duration
        with suppress(MpvError):  # applies to the slide on screen too
            mpv(["set_property", "image-display-duration", duration])
    if "shuffle" in data:
        state["shuffle"] = bool(data["shuffle"])
    save_state()
    if "shuffle" in data:
        sync_player()
    return jsonify(ok=True)


PLAYER_ACTIONS = {
    "next": ["playlist-next"],
    "prev": ["playlist-prev"],
    "pause": ["set_property", "pause", True],
    "play": ["set_property", "pause", False],
}


@app.post("/api/player/<action>")
def player(action):
    if action not in PLAYER_ACTIONS:
        abort(404)
    mpv(PLAYER_ACTIONS[action])
    return jsonify(ok=True)


@app.post("/api/show/<folder>/<name>")
@mutating
def show(folder, name):
    path = slide_path(FORMAT, folder, name)
    playing = mpv_playlist()
    if not player_state()["pinned"] and path in playing:
        mpv(["playlist-play-index", playing.index(path)])
        return jsonify(ok=True)
    paths = playlist_paths()
    if path not in paths:
        return jsonify(error="This slide is not in the slideshow: it or its folder is switched off."), 409
    mpv(["set_property", "loop-file", "no"])
    load_playlist(paths, start=path)
    return jsonify(ok=True)


@app.post("/api/pin/<folder>/<name>")
@mutating
def pin(folder, name):
    mpv(["set_property", "loop-file", "inf"], ["loadfile", slide_path(FORMAT, folder, name), "replace"])
    return jsonify(ok=True)


@app.post("/api/resume")
@mutating
def resume():
    mpv(["set_property", "loop-file", "no"], ["set_property", "pause", False])
    sync_player()  # continues from the image on screen
    return jsonify(ok=True)


@app.post("/api/folders/<fmt>")
@mutating
def create_folder(fmt):
    if fmt not in FORMATS:
        abort(404)
    name = " ".join(str(body().get("name", "")).split())
    if not re.fullmatch(r"[^/\\\x00-\x1f.][^/\\\x00-\x1f]{0,59}", name):
        return jsonify(error="A folder name needs 1 to 60 characters, without / or \\, "
                             "and must not start with a dot."), 400
    if name.lower() in (f.lower() for f in folders(fmt)):
        return jsonify(error=f'A folder called "{name}" already exists.'), 409
    os.mkdir(os.path.join(SLIDES_DIR, fmt, name))
    return jsonify(ok=True, name=name)


@app.delete("/api/folders/<fmt>/<folder>")
@mutating
def delete_folder(fmt, folder):
    try:
        os.rmdir(folder_path(fmt, folder))
    except OSError:
        return jsonify(error="Only empty folders can be deleted. Move or delete its slides first."), 409
    switch(f"{fmt}/{folder}", "folders_off", True)
    save_state()
    return jsonify(ok=True)


@app.post("/api/folders/<fmt>/<folder>/switch")
@mutating
def switch_folder(fmt, folder):
    """{"on": true/false}; with "only": true every other folder is switched off."""
    folder_path(fmt, folder)
    data = body()
    if data.get("only"):
        for other in folders(fmt):
            switch(f"{fmt}/{other}", "folders_off", other == folder)
    else:
        switch(f"{fmt}/{folder}", "folders_off", bool(data.get("on")))
    save_state()
    if fmt == FORMAT:
        sync_player()
    return jsonify(ok=True)


@app.post("/api/upload/<fmt>/<folder>")
@mutating
def upload(fmt, folder):
    folder_dir = folder_path(fmt, folder)
    files = request.files.getlist("files")
    if not files:
        return jsonify(error="No file received"), 400
    for f in files:
        if os.path.splitext(f.filename or "")[1].lower() not in EXTENSIONS:  # the name is cleaned up later
            return jsonify(error=f"{f.filename}: only JPG, PNG, WebP and GIF images are allowed"), 400
    if shutil.disk_usage(SLIDES_DIR).free < MIN_FREE_MB * 1024 * 1024:
        return jsonify(error="The Pi is running out of disk space, delete some images first"), 507
    saved = [save_upload(f, folder_dir) for f in files]
    if fmt == FORMAT:
        sync_player()
    return jsonify(saved=saved)


@app.post("/api/slides/<fmt>/<folder>/<name>/switch")
@mutating
def switch_slide(fmt, folder, name):
    slide_path(fmt, folder, name)
    switch(f"{fmt}/{folder}/{name}", "slides_off", bool(body().get("on")))
    save_state()
    if fmt == FORMAT:
        sync_player()
    return jsonify(ok=True)


@app.post("/api/slides/<fmt>/<folder>/<name>/move")
@mutating
def move_slide(fmt, folder, name):
    src = slide_path(fmt, folder, name)
    to = str(body().get("to", ""))
    target = folder_path(fmt, to)
    if to == folder:
        return jsonify(ok=True, name=name)
    new_name = unique_name(target, name)
    os.rename(src, os.path.join(target, new_name))
    drop_thumb(fmt, folder, name)
    if f"{fmt}/{folder}/{name}" in state["slides_off"]:  # a switched-off slide stays off
        switch(f"{fmt}/{folder}/{name}", "slides_off", True)
        switch(f"{fmt}/{to}/{new_name}", "slides_off", False)
    save_state()
    if fmt == FORMAT:
        sync_player()
    return jsonify(ok=True, name=new_name)


@app.delete("/api/slides/<fmt>/<folder>/<name>")
@mutating
def delete_slide(fmt, folder, name):
    os.remove(slide_path(fmt, folder, name))
    drop_thumb(fmt, folder, name)
    switch(f"{fmt}/{folder}/{name}", "slides_off", True)
    save_state()
    if fmt == FORMAT:
        sync_player()
    return jsonify(ok=True)


@app.get("/thumb/<fmt>/<folder>/<name>")
def thumb(fmt, folder, name):
    src = slide_path(fmt, folder, name)
    if Image is None:
        return send_file(src, max_age=86400)
    dst = os.path.join(THUMB_DIR, fmt, folder, name + ".jpg")
    if not os.path.exists(dst) or os.path.getmtime(dst) < os.path.getmtime(src):
        with thumb_slots:
            try:
                with Image.open(src) as im:
                    im.draft("RGB", (THUMB_SIZE, THUMB_SIZE))  # fast JPEG downscale while decoding
                    small = ImageOps.exif_transpose(im).convert("RGB")
                small.thumbnail((THUMB_SIZE, THUMB_SIZE))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                tmp = f"{dst}.{threading.get_ident()}.tmp"
                small.save(tmp, "JPEG", quality=80)
                os.replace(tmp, dst)
            except Exception:  # Pillow cannot read it, let the browser try the original
                return send_file(src, max_age=60)
    return send_file(dst, max_age=86400)


state, synced = load_state(), []
with suppress(OSError):  # the files mpv starts with, in case they are missing or outdated
    save_state()
    write_playlist_file(playlist_paths())

if __name__ == "__main__":
    if PASSWORD == "change-me":
        print("WARNING: default password in use, set PRESENTER_PASSWORD (see README)")
    threading.Thread(target=watch_library, daemon=True).start()
    try:
        from waitress import serve
    except ImportError:
        app.run(host="0.0.0.0", port=PORT, threaded=True)
    else:
        print(f"Presenter running on http://0.0.0.0:{PORT}")
        serve(app, host="0.0.0.0", port=PORT, threads=8)
