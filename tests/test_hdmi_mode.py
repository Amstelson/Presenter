"""Tests for hdmi_mode.py: parsing, and switching on headless Wayland and X11 displays.

The display tests need labwc + wlr-randr (Wayland) or Xorg with the dummy
driver + xrandr + cvt (X11), and are skipped where those are missing.
"""
import os
import shutil
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import hdmi_mode  # noqa: E402

WLR_RANDR = """\
HDMI-A-1 "NEC Corporation NC900C (HDMI-A-1)"
  Make: NEC Corporation
  Enabled: yes
  Modes:
    1920x1080 px, 60.000000 Hz (preferred, current)
    2048x1080 px, 24.000000 Hz
    1920x1080 px, 23.976000 Hz
  Position: 0,0
HDMI-A-2 "Unknown"
  Enabled: no
  Modes:
    1280x720 px, 60.000000 Hz
"""
XRANDR = """\
Screen 0: minimum 320 x 200, current 1920 x 1080, maximum 8192 x 8192
HDMI-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 597mm x 336mm
   1920x1080     60.00*+  50.00    59.94
   2048x1080     24.00 +
   2048x858_24.00  23.91
HDMI-2 disconnected (normal left inverted right x axis y axis)
   1280x720      60.00
"""
CVT = """\
# 2048x858 23.91 Hz (CVT) hsync: 20.90 kHz; pclk: 53.50 MHz
Modeline "2048x858_24.00"   53.50  2048 2104 2304 2560  858 861 871 874 -hsync +vsync
"""


def fake_run(calls, outputs_text):
    def run(env, *cmd, check=True):
        calls.append(cmd)
        return {"wlr-randr": outputs_text, "xrandr": outputs_text, "cvt": CVT}[cmd[0]] \
            if len(cmd) == 1 or cmd[1] == "--query" or cmd[0] == "cvt" else ""
    return run


def test_parse_mode():
    assert hdmi_mode.parse_mode("2048x858@24") == (2048, 858, 24.0)
    assert hdmi_mode.parse_mode(" 2048x1080 ") == (2048, 1080, None)
    with pytest.raises(SystemExit) as exit_info:
        hdmi_mode.parse_mode("2048by858")
    assert exit_info.value.code == 2  # the keeper service does not retry on this


def test_same_allows_cvt_rounding_of_refresh():
    assert hdmi_mode.same((2048, 858, 23.91), (2048, 858, 24.0))
    assert hdmi_mode.same((2048, 858, 23.91), (2048, 858, None))
    assert not hdmi_mode.same((2048, 858, 24.0), (2048, 1080, 24.0))
    assert not hdmi_mode.same((1920, 1080, 60.0), (1920, 1080, 24.0))


def test_reads_wlr_randr(monkeypatch):
    monkeypatch.setattr(hdmi_mode, "run", fake_run([], WLR_RANDR))
    found = hdmi_mode.outputs({"WAYLAND_DISPLAY": "wayland-0"})
    assert found["HDMI-A-1"]["current"] == (1920, 1080, 60.0, None)
    assert (2048, 1080, 24.0, None) in found["HDMI-A-1"]["modes"]
    assert found["HDMI-A-2"]["current"] is None  # disabled output
    assert hdmi_mode.pick_output(found) == "HDMI-A-1"


def test_reads_xrandr(monkeypatch):
    monkeypatch.setattr(hdmi_mode, "run", fake_run([], XRANDR))
    found = hdmi_mode.outputs({"DISPLAY": ":0"})
    assert list(found) == ["HDMI-1"]  # disconnected outputs are left out
    assert found["HDMI-1"]["current"] == (1920, 1080, 60.0, "1920x1080")
    assert (2048, 1080, 24.0, "2048x1080") in found["HDMI-1"]["modes"]  # "24.00 +" = preferred
    assert (2048, 858, 23.91, "2048x858_24.00") in found["HDMI-1"]["modes"]


def test_wayland_uses_offered_mode_or_custom(monkeypatch):
    calls = []
    monkeypatch.setattr(hdmi_mode, "run", fake_run(calls, WLR_RANDR))
    env = {"WAYLAND_DISPLAY": "wayland-0"}
    offered = hdmi_mode.outputs(env)["HDMI-A-1"]["modes"]
    hdmi_mode.set_mode(env, "HDMI-A-1", (2048, 1080, None), offered)
    hdmi_mode.set_mode(env, "HDMI-A-1", (2048, 858, 24.0), offered)
    assert calls[-2] == ("wlr-randr", "--output", "HDMI-A-1", "--mode", "2048x1080@24.0Hz")
    assert calls[-1] == ("wlr-randr", "--output", "HDMI-A-1", "--custom-mode", "2048x858@24.0Hz")


def test_x11_creates_custom_mode_from_cvt(monkeypatch):
    calls = []
    monkeypatch.setattr(hdmi_mode, "run", fake_run(calls, XRANDR.replace("2048x858_24.00  23.91\n", "")))
    env = {"DISPLAY": ":0"}
    offered = hdmi_mode.outputs(env)["HDMI-1"]["modes"]
    hdmi_mode.set_mode(env, "HDMI-1", (2048, 858, 24.0), offered)
    assert calls[1:] == [
        ("cvt", "2048", "858", "24.0"),
        ("xrandr", "--newmode", "2048x858_24.00", "53.50", "2048", "2104", "2304", "2560",
         "858", "861", "871", "874", "-hsync", "+vsync"),
        ("xrandr", "--addmode", "HDMI-1", "2048x858_24.00"),
        ("xrandr", "--output", "HDMI-1", "--mode", "2048x858_24.00"),
    ]


# --- On real (headless) displays ---

def current(env):
    found = hdmi_mode.outputs(env)
    return found[hdmi_mode.pick_output(found)]["current"][:2]


def stdin_with(monkeypatch, data):
    """Stand-in for the keyboard: Enter pressed (data) or nothing pressed at all."""
    read_end, write_end = os.pipe()
    if data:
        os.write(write_end, data)
    monkeypatch.setattr(sys, "stdin", os.fdopen(read_end))
    return write_end  # kept open, so "nothing pressed" waits instead of seeing end-of-input


@pytest.fixture(scope="module")
def wayland(tmp_path_factory):
    if not (shutil.which("labwc") and shutil.which("wlr-randr")):
        pytest.skip("labwc and wlr-randr are not installed")
    run_dir = tmp_path_factory.mktemp("xdg")
    run_dir.chmod(0o700)
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    env.update(XDG_RUNTIME_DIR=str(run_dir), WLR_BACKENDS="headless", WLR_RENDERER="pixman",
               WLR_LIBINPUT_NO_DEVICES="1")
    proc = subprocess.Popen(["labwc"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        sockets = [p.name for p in run_dir.iterdir() if p.name.startswith("wayland-") and "." not in p.name]
        if sockets:
            break
        time.sleep(0.1)
    else:
        proc.terminate()
        pytest.skip("headless labwc did not start")
    env["WAYLAND_DISPLAY"] = sockets[0]
    yield env
    proc.terminate()
    proc.wait()


@pytest.fixture(scope="module")
def x11(tmp_path_factory):
    if not (shutil.which("Xorg") and shutil.which("xrandr") and shutil.which("cvt")):
        pytest.skip("Xorg, xrandr and cvt are not installed")
    conf = tmp_path_factory.mktemp("xorg") / "xorg.conf"
    conf.write_text('Section "Device"\n Identifier "d"\n Driver "dummy"\n VideoRam 256000\nEndSection\n'
                    'Section "Monitor"\n Identifier "m"\n HorizSync 5-1000\n VertRefresh 5-200\nEndSection\n'
                    'Section "Screen"\n Identifier "s"\n Device "d"\n Monitor "m"\n DefaultDepth 24\n'
                    ' SubSection "Display"\n  Depth 24\n  Virtual 4096 2160\n EndSubSection\nEndSection\n')
    display = ":93"
    proc = subprocess.Popen(["Xorg", display, "-config", str(conf), "-noreset", "-logfile",
                             str(conf.with_suffix(".log"))],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    env = {k: v for k, v in os.environ.items() if k != "WAYLAND_DISPLAY"}
    env["DISPLAY"] = display
    for _ in range(50):
        if subprocess.run(["xrandr"], env=env, capture_output=True).returncode == 0:
            break
        time.sleep(0.1)
    else:
        proc.terminate()
        pytest.skip("Xorg with the dummy driver did not start")
    yield env
    proc.terminate()
    proc.wait()


@pytest.fixture(params=["wayland", "x11"])
def display(request):
    return request.getfixturevalue(request.param)


def test_try_switches_back_unless_enter_is_pressed(display, monkeypatch):
    hdmi_mode.set_mode(display, hdmi_mode.pick_output(hdmi_mode.outputs(display)), (1920, 1080, 60.0), [])
    before = current(display)
    stdin_with(monkeypatch, b"")
    assert hdmi_mode.try_mode(display, "2048x858@24", seconds=1) is False
    assert current(display) == before


def test_try_keeps_the_mode_when_enter_is_pressed(display, monkeypatch):
    stdin_with(monkeypatch, b"\n")
    assert hdmi_mode.try_mode(display, "2048x858@24", seconds=5) is True
    assert current(display) == (2048, 858)


def test_keep_sets_the_mode_again_after_a_change(display):
    want = (2048, 858, 24.0)
    applied = hdmi_mode.keep_once(display, want, None)
    assert current(display) == (2048, 858)
    output = hdmi_mode.pick_output(hdmi_mode.outputs(display))
    hdmi_mode.set_mode(display, output, (1920, 1080, 60.0), [])  # like the projector being power cycled
    assert current(display) == (1920, 1080)
    applied = hdmi_mode.keep_once(display, want, applied)
    assert current(display) == (2048, 858)
    assert hdmi_mode.keep_once(display, want, applied) == applied  # nothing to do now
