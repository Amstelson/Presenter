#!/usr/bin/env python3
"""Presenter - a small web remote for an mpv image slideshow.

mpv shows the slides fullscreen. This Flask app serves a control page and
talks to mpv through its JSON IPC socket. See README.md for setup.
"""
import hmac
import json
import os
import re
import shutil
import socket
import threading
from contextlib import suppress

from flask import Flask, Response, abort, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

try:
    from PIL import Image, ImageOps
except ImportError:  # no Pillow: thumbnails fall back to the full image
    Image = None

# --- Settings (environment variables override these defaults) ---
SLIDES_DIR = os.environ.get("PRESENTER_SLIDES", os.path.expanduser("~/slides"))
MPV_SOCKET = os.environ.get("PRESENTER_SOCKET", "/tmp/mpv.sock")
USERNAME = os.environ.get("PRESENTER_USER", "cinema")
PASSWORD = os.environ.get("PRESENTER_PASSWORD", "change-me")
PORT = int(os.environ.get("PRESENTER_PORT", "8888"))
MAX_UPLOAD_MB = 50
MIN_FREE_MB = 500  # refuse uploads when the SD card gets this full
EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
THUMB_DIR = os.path.expanduser("~/.cache/presenter/thumbs")
THUMB_SIZE = 400
# One slides subfolder per screen format: name -> (label, picture width / height).
# The mpv framing for each format is in mpv/formats.conf.
FORMATS = {"flat": ("Flat", 1998 / 1080), "scope": ("Scope", 2048 / 858)}
# The format of this Pi's screen. Only that folder is played, the other is just stored.
FORMAT = os.environ.get("PRESENTER_FORMAT", "flat")
if FORMAT not in FORMATS:
    raise SystemExit(f"PRESENTER_FORMAT must be one of: {', '.join(FORMATS)}")
PLAY_DIR = os.path.join(SLIDES_DIR, FORMAT)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
thumb_slots = threading.Semaphore(2)  # limit parallel thumbnail work to save RAM
for fmt in FORMATS:
    os.makedirs(os.path.join(SLIDES_DIR, fmt), exist_ok=True)
    os.makedirs(os.path.join(THUMB_DIR, fmt), exist_ok=True)


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
    return {"paused": bool(paused), "pinned": loop == "inf", "current": os.path.basename(path or "")}


def playlist_names():
    return [os.path.basename(e["filename"]) for e in mpv(["get_property", "playlist"])[0] or []]


def natural_key(name):
    """Sort like mpv does: case-insensitive, "slide2" before "slide10"."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name.lower())]


def list_images(folder):
    """Images in a format folder as {name: mtime}, in slideshow order."""
    with os.scandir(os.path.join(SLIDES_DIR, folder)) as entries:
        found = {e.name: int(e.stat().st_mtime) for e in entries if e.is_file()
                 and not e.name.startswith(".") and os.path.splitext(e.name)[1].lower() in EXTENSIONS}
    return {n: found[n] for n in sorted(found, key=natural_key)}


def image_path(folder, name):
    if folder not in FORMATS or name not in list_images(folder):  # also blocks "../" tricks
        abort(404)
    return os.path.join(SLIDES_DIR, folder, name)


def reload_playlist(start=None):
    """Load every image in this screen's folder as the playlist, starting at `start`."""
    names = list(list_images(FORMAT))
    if not names:
        return mpv(["stop"])
    i = names.index(start) if start in names else 0
    paths = [os.path.join(PLAY_DIR, n) for n in names[i:] + names[:i]]
    mpv(["loadfile", paths[0], "replace"], *(["loadfile", p, "append"] for p in paths[1:]))


def save_upload(file, folder):
    folder_dir = os.path.join(SLIDES_DIR, folder)
    base, ext = os.path.splitext(file.filename)
    base, ext = secure_filename(base) or "image", ext.lower()
    name, n = base + ext, 1
    while os.path.exists(os.path.join(folder_dir, name)):
        name, n = f"{base}-{n}{ext}", n + 1
    tmp = os.path.join(folder_dir, f".{name}.part")  # hidden until complete
    try:
        file.save(tmp)
        os.replace(tmp, os.path.join(folder_dir, name))
    finally:  # no half-written leftovers if the disk fills up
        with suppress(FileNotFoundError):
            os.remove(tmp)
    return name


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


@app.errorhandler(413)
def too_large(e):
    return jsonify(error=f"File is too large (max {MAX_UPLOAD_MB} MB)"), 413


@app.get("/")
def index():
    formats = [(n, label, ratio) for n, (label, ratio) in FORMATS.items()]
    return render_template("index.html", max_upload_mb=MAX_UPLOAD_MB, formats=formats,
                           on_screen=FORMAT)


@app.get("/api/images")
def images():
    return jsonify({folder: [{"name": n, "mtime": t} for n, t in list_images(folder).items()]
                    for folder in FORMATS})


@app.get("/api/status")
def status():
    return jsonify(player_state())


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


@app.post("/api/show/<name>")
def show(name):
    image_path(FORMAT, name)
    names = playlist_names()
    if not player_state()["pinned"] and name in names:
        mpv(["playlist-play-index", names.index(name)])
    else:  # leaving "only this" mode, or a file the player has not loaded yet
        mpv(["set_property", "loop-file", "no"])
        reload_playlist(start=name)
    return jsonify(ok=True)


@app.post("/api/pin/<name>")
def pin(name):
    mpv(["set_property", "loop-file", "inf"], ["loadfile", image_path(FORMAT, name), "replace"])
    return jsonify(ok=True)


@app.post("/api/resume")
def resume():
    current = player_state()["current"]
    mpv(["set_property", "loop-file", "no"], ["set_property", "pause", False])
    reload_playlist(start=current)  # continue from the image on screen
    return jsonify(ok=True)


@app.post("/api/upload/<folder>")
def upload(folder):
    if folder not in FORMATS:
        abort(404)
    files = request.files.getlist("files")
    if not files:
        return jsonify(error="No file received"), 400
    for f in files:
        if os.path.splitext(f.filename or "")[1].lower() not in EXTENSIONS:
            return jsonify(error=f"{f.filename}: only JPG, PNG, WebP and GIF images are allowed"), 400
    if shutil.disk_usage(SLIDES_DIR).free < MIN_FREE_MB * 1024 * 1024:
        return jsonify(error="The Pi is running out of disk space, delete some images first"), 507
    saved = [save_upload(f, folder) for f in files]
    if folder != FORMAT:  # stored for a screen of the other format
        return jsonify(saved=saved)
    with suppress(MpvError):  # if mpv is down it loads the whole folder when it starts
        if not player_state()["pinned"]:  # in "only this" mode new files join on resume
            mpv(*(["loadfile", os.path.join(PLAY_DIR, n), "append-play"] for n in saved))
    return jsonify(saved=saved)


@app.delete("/api/images/<folder>/<name>")
def delete(folder, name):
    os.remove(image_path(folder, name))
    with suppress(FileNotFoundError):
        os.remove(os.path.join(THUMB_DIR, folder, name + ".jpg"))
    if folder != FORMAT:  # not on this screen, nothing to tell the player
        return jsonify(ok=True)
    with suppress(MpvError):
        state = player_state()
        if state["pinned"] and state["current"] == name:
            mpv(["set_property", "loop-file", "no"])
            reload_playlist()
        else:
            gone = [i for i, n in enumerate(playlist_names()) if n == name]
            mpv(*(["playlist-remove", i] for i in reversed(gone)))
    return jsonify(ok=True)


@app.get("/thumb/<folder>/<name>")
def thumb(folder, name):
    src = image_path(folder, name)
    if Image is None:
        return send_file(src, max_age=86400)
    dst = os.path.join(THUMB_DIR, folder, name + ".jpg")
    if not os.path.exists(dst) or os.path.getmtime(dst) < os.path.getmtime(src):
        with thumb_slots:
            try:
                with Image.open(src) as im:
                    im.draft("RGB", (THUMB_SIZE, THUMB_SIZE))  # fast JPEG downscale while decoding
                    small = ImageOps.exif_transpose(im).convert("RGB")
                small.thumbnail((THUMB_SIZE, THUMB_SIZE))
                tmp = f"{dst}.{threading.get_ident()}.tmp"
                small.save(tmp, "JPEG", quality=80)
                os.replace(tmp, dst)
            except Exception:  # Pillow cannot read it, let the browser try the original
                return send_file(src, max_age=60)
    return send_file(dst, max_age=86400)


if __name__ == "__main__":
    if PASSWORD == "change-me":
        print("WARNING: default password in use, set PRESENTER_PASSWORD (see README)")
    try:
        from waitress import serve
    except ImportError:
        app.run(host="0.0.0.0", port=PORT, threaded=True)
    else:
        print(f"Presenter running on http://0.0.0.0:{PORT}")
        serve(app, host="0.0.0.0", port=PORT, threads=8)
