#!/usr/bin/env python3
"""Force the Pi's HDMI output to one resolution.

For example 2048x1080 (the DCI 2K container: Flat and Scope slides are framed
inside it exactly, like a DCP) or 2048x858 (an exact Scope signal, if the
projector accepts it).

  ./hdmi_mode.py list              the current mode and the modes the projector offers
  ./hdmi_mode.py try 2048x858@24   switch now, and switch back after 20 s unless you
                                   press Enter (safe: a black screen undoes itself)
  ./hdmi_mode.py keep 2048x858@24  switch, and switch back to it whenever something
                                   else changes it, e.g. the projector being turned
                                   off and on. presenter-hdmi.service runs this with
                                   PRESENTER_HDMI_MODE from ~/.config/presenter.env.

Works over SSH or on the Pi's desktop, with Wayland (wlr-randr) or X11 (xrandr).
A mode the projector does not offer is created as a custom (CVT) mode.
"""
import os
import re
import select
import subprocess
import sys
import time

TRY_SECONDS = 20
KEEP_INTERVAL = 10  # seconds between checks in "keep" mode


class ModeError(Exception):
    pass


def fail(message, code=1):
    print(message, file=sys.stderr)
    sys.exit(code)


def parse_mode(text):
    """'2048x858@24' -> (2048, 858, 24.0). The refresh rate is optional."""
    match = re.fullmatch(r"(\d+)x(\d+)(?:@(\d+(?:\.\d+)?))?", text.strip())
    if not match:
        fail(f'"{text}" is not a mode like 2048x1080 or 2048x858@24', 2)
    w, h, hz = match.groups()
    return int(w), int(h), float(hz) if hz else None


def describe(mode):
    w, h, hz = mode[:3]
    return f"{w}x{h}" + (f" @ {hz:g} Hz" if hz else "")


def same(a, b):
    """Same size, and the same refresh rate where both are known (CVT makes 23.91 of 24)."""
    return a[:2] == b[:2] and (a[2] is None or b[2] is None or abs(a[2] - b[2]) < 0.5)


def session_env():
    """Display settings of the desktop session, so this also works over SSH."""
    env = dict(os.environ)
    if not (env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")):
        # The autostart entry copies them into the systemd user manager at login.
        shown = subprocess.run(["systemctl", "--user", "show-environment"],
                               capture_output=True, text=True).stdout
        for line in shown.splitlines():
            key, _, value = line.partition("=")
            if key in ("WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY"):
                env[key] = value
    if not (env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")):
        raise ModeError("No desktop session found. Is the Pi logged in to the desktop?")
    return env


def run(env, *cmd, check=True):
    done = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if check and done.returncode:
        raise ModeError(f"{cmd[0]}: {done.stderr.strip() or 'failed'}")
    return done.stdout


def outputs(env):
    """{output name: {"modes": [(w, h, hz, x11 mode name)], "current": mode or None}}"""
    found, name = {}, None
    if env.get("WAYLAND_DISPLAY"):
        for line in run(env, "wlr-randr").splitlines():
            if line and not line[0].isspace():
                name = line.split()[0]
                found[name] = {"modes": [], "current": None}
            elif name and (m := re.match(r"\s+(\d+)x(\d+) px(?:, ([\d.]+) Hz)?(.*)", line)):
                mode = (int(m[1]), int(m[2]), float(m[3]) if m[3] else None, None)
                found[name]["modes"].append(mode)
                if "current" in m[4]:
                    found[name]["current"] = mode
    else:
        for line in run(env, "xrandr", "--query").splitlines():
            if m := re.match(r"(\S+) connected", line):
                name = m[1]
                found[name] = {"modes": [], "current": None}
            elif not line[:1].isspace():
                name = None  # the Screen line or a disconnected output
            elif name and (m := re.match(r"\s+((\d+)x(\d+)\S*)\s+(.*)", line)):
                for rate, star in re.findall(r"([\d.]+)(\*?)", m[4]):
                    mode = (int(m[2]), int(m[3]), float(rate), m[1])
                    found[name]["modes"].append(mode)
                    if star:
                        found[name]["current"] = mode
    return found


def pick_output(found):
    """PRESENTER_HDMI_OUTPUT if set, otherwise the first HDMI output, otherwise any."""
    wanted = os.environ.get("PRESENTER_HDMI_OUTPUT")
    for name in [wanted] if wanted else sorted(found, key=lambda n: not n.startswith("HDMI")):
        if name in found:
            return name
    raise ModeError(f"Output {wanted} not found" if wanted else "No connected HDMI output")


def set_mode(env, output, want, offered):
    w, h, hz = want[:3]
    match = max((m for m in offered if same(m, want)), key=lambda m: m[2] or 0, default=None)
    if env.get("WAYLAND_DISPLAY"):
        if match:
            run(env, "wlr-randr", "--output", output,
                "--mode", f"{w}x{h}" + (f"@{match[2]}Hz" if match[2] else ""))
        else:
            run(env, "wlr-randr", "--output", output, "--custom-mode", f"{w}x{h}@{hz or 60}Hz")
        return
    if not match:  # X11: create the mode from CVT timings first
        line = run(env, "cvt", str(w), str(h), str(hz or 60)).split("Modeline", 1)[1].split()
        name = line[0].strip('"')
        run(env, "xrandr", "--newmode", name, *line[1:], check=False)  # fails if it exists
        run(env, "xrandr", "--addmode", output, name, check=False)
        match = (w, h, None, name)
    run(env, "xrandr", "--output", output, "--mode", match[3],
        *(["--rate", str(match[2])] if match[2] else []))


def show_list(env):
    found = outputs(env)
    output = pick_output(found)
    current, modes = found[output]["current"], found[output]["modes"]
    print(f"Output: {output}")
    print(f"Current mode: {describe(current) if current else 'unknown'}")
    print("Modes the projector offers:")
    for mode in modes:
        print(f"  {describe(mode)}" + ("  (current)" if mode == current else ""))
    dci = any(same(m, (2048, 1080, None)) for m in modes)
    print("2048x1080 (DCI 2K) is " + ("offered." if dci else "not offered, only a custom mode can be tried."))


def try_mode(env, text, seconds=TRY_SECONDS):
    found = outputs(env)
    output = pick_output(found)
    before, offered = found[output]["current"], found[output]["modes"]
    if not before:
        raise ModeError("Cannot tell the current mode, so it could not be switched back. Not switching.")
    set_mode(env, output, parse_mode(text), offered)
    now = outputs(env)[output]["current"]
    print(f"{output} now sends {describe(now) if now else text}.")
    print(f"Press Enter within {seconds} s to keep it, otherwise it goes back to {describe(before)}.",
          flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], seconds)
    if ready and sys.stdin.readline():
        print(f"Kept until the next reboot. To keep it for good, add PRESENTER_HDMI_MODE={text}\n"
              "to ~/.config/presenter.env and run: systemctl --user restart presenter-mpv")
        return True
    # Read the list again: a custom mode can drop out of it once another one is set.
    set_mode(env, output, before, outputs(env)[output]["modes"])
    print(f"Switched back to {describe(before)}.")
    return False


def keep_once(env, want, applied):
    """Set the mode again if something else changed it. Returns the mode now applied."""
    found = outputs(env)
    output = pick_output(found)
    current = found[output]["current"]
    if current is None or same(current, applied or want):
        return applied
    set_mode(env, output, want, found[output]["modes"])
    now = outputs(env)[output]["current"] or want  # may differ from want, e.g. 2000 for 1998
    print(f"{output}: switched from {describe(current)} to {describe(now)}", flush=True)
    return now


def keep(text):
    if not text:
        print("PRESENTER_HDMI_MODE is not set, leaving the HDMI mode alone.")
        return
    want, applied, last_problem = parse_mode(text), None, None
    print(f"Keeping the HDMI output at {describe(want)}", flush=True)
    while True:
        try:
            applied, last_problem = keep_once(session_env(), want, applied), None
        except (ModeError, OSError) as e:  # e.g. the projector is off; say it once, not every time
            if str(e) != last_problem:
                print(f"Waiting: {e}", flush=True)
                last_problem = str(e)
        time.sleep(KEEP_INTERVAL)


def main(argv):
    command, text = (argv[1:] + ["", ""])[:2]
    if command not in ("list", "try", "keep") or (command == "try" and not text):
        print(__doc__)
        return 2
    try:
        if command == "keep":
            keep(text)
        elif command == "list":
            show_list(session_env())
        else:
            try_mode(session_env(), text)
    except ModeError as e:
        fail(str(e))
    except FileNotFoundError as e:
        fail(f"{e.filename} is not installed (sudo apt install wlr-randr for Wayland)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
