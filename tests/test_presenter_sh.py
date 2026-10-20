"""Tests for presenter.sh (the Start/Stop Presenter icons), with a stand-in systemctl."""
import os
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "presenter.sh")

# Pretends to be systemctl: services are "running" while a file named after them exists.
FAKE_SYSTEMCTL = """#!/bin/sh
echo "$*" >> "$FAKE_DIR/calls"
shift  # --user
case "$1" in
    is-active)
        [ "$2" = --quiet ] && shift
        if [ -e "$FAKE_DIR/running/$2" ]; then echo active; exit 0; else echo inactive; exit 3; fi ;;
    restart) touch "$FAKE_DIR/running/$2" ;;
    stop) shift; for unit in "$@"; do rm -f "$FAKE_DIR/running/$unit"; done ;;
    status) echo "(last messages of $2)" ;;
esac
exit 0
"""


@pytest.fixture
def fake(tmp_path):
    bin_dir, home = tmp_path / "bin", tmp_path / "home"
    (tmp_path / "running").mkdir()
    (home / ".config" / "autostart").mkdir(parents=True)
    bin_dir.mkdir()
    for name, text in [("systemctl", FAKE_SYSTEMCTL), ("hostname", "#!/bin/sh\necho 192.168.1.50\n"),
                       ("sleep", "#!/bin/sh\n")]:
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(home), "FAKE_DIR": str(tmp_path),
           "DISPLAY": ":0"}

    def run(*args, stdin=subprocess.DEVNULL):
        done = subprocess.run([SCRIPT, *args], env=env, stdin=stdin, capture_output=True, text=True)
        calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
        (tmp_path / "calls").unlink(missing_ok=True)
        return done, calls

    run.autostart = home / ".config" / "autostart" / "presenter-mpv.desktop"
    run.stopped = home / ".config" / "presenter" / "stopped"  # the services do not start while it exists
    return run


def test_start_restarts_everything_and_starts_at_login_again(fake):
    fake.stopped.parent.mkdir(parents=True)
    fake.stopped.touch()
    done, calls = fake("start")
    assert done.returncode == 0, done.stdout + done.stderr
    assert not fake.stopped.exists()
    assert fake.autostart.exists()  # put back in case it went missing
    assert "--user reset-failed presenter-web presenter-mpv presenter-hdmi" in calls
    assert "--user enable --quiet presenter-web" in calls
    assert "--user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY" in calls
    assert calls.index("--user restart presenter-web") < calls.index("--user restart presenter-mpv")
    assert "Presenter is running. Web page: http://192.168.1.50:8888" in done.stdout


def test_stop_keeps_it_off_after_restarts(fake):
    fake("start")
    done, calls = fake("stop", "--yes")
    assert done.returncode == 0
    assert "--user stop presenter-mpv presenter-hdmi presenter-web" in calls
    assert fake.stopped.exists()  # nothing starts at the next boot or login
    assert fake.autostart.exists()  # the login step stays, so "start" works over SSH too
    done, _ = fake("status")
    assert done.returncode == 1
    assert "stays off" in done.stdout and "presenter-mpv is not running" in done.stdout


def test_stop_asks_first(fake):
    fake("start")
    for answer, stopped in [(b"n\n", False), (b"\n", False), (b"y\n", True)]:
        keyboard, terminal = os.openpty()  # like the icon's terminal window
        os.write(keyboard, answer)
        done, calls = fake("stop", stdin=terminal)
        os.close(keyboard)
        os.close(terminal)
        assert ("--user stop presenter-mpv presenter-hdmi presenter-web" in calls) is stopped, answer
        assert fake.stopped.exists() is stopped
        assert ("Nothing was changed" in done.stdout) is not stopped


def test_unknown_command_shows_help(fake):
    done, _ = fake("explode")
    assert done.returncode == 2 and "./presenter.sh start" in done.stdout
