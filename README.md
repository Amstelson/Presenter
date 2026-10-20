# Presenter

A promo slideshow for the cinema screen that staff control from a phone or PC.

- Each Pi drives one screen, set up once as **DCI Flat** (1.85:1) or **DCI Scope** (2.39:1).
- Slides are kept in folders, for example `~/slides/flat/Coming soon/`.
- **mpv** plays the slideshow fullscreen, in a loop, framed to the format: every slide that is switched on, from every folder that is switched on.
- A small **Flask** web app (`app.py`) controls mpv through its IPC socket and serves the control page at **`http://<pi-ip>:8888`**.

```
 phone / PC  --HTTP :8888-->  app.py (Flask)  --JSON over /tmp/mpv.sock-->  mpv (fullscreen)
                                   |                                          |
                         ~/slides/<format>/<folder>/          ~/.config/presenter/playlist.m3u8
```

The web page lets staff:

- pause and play, or go to the next or previous slide
- set the **seconds per slide** and switch **shuffle** (random order) on or off
- create **folders**, switch whole folders on or off, or **play only one folder**
- switch single slides **off** for a while (for example once their event is over) and back on
- tap a slide to **show it now**, **show only this** one until told otherwise (**Resume slideshow** goes back), **move** it to another folder, or delete it
- upload images (JPG, PNG, WebP, GIF, up to 50 MB each) into a folder

It works offline on the LAN (no CDN, no internet needed) and is protected by a username and password.

## What you need

- A Raspberry Pi 4 (2 GB or more is comfortable, 1 GB works) running **Raspberry Pi OS with desktop** (Bookworm or Trixie, Wayland or X11).
- Auto login to the desktop. This is the default; check it under `sudo raspi-config` > System Options > Boot / Auto Login > Desktop Autologin.

## Install

1. Get the code onto the Pi into `~/presenter`:

   ```sh
   git clone https://github.com/Amstelson/Presenter.git ~/presenter
   ```

   If the repository is private, either sign in with a GitHub token, or download the ZIP from GitHub and copy the folder over with a USB stick or `scp`.

2. Run the installer **as the normal desktop user** (not with sudo). It asks for the web password and whether this Pi's screen is **flat** or **scope**:

   ```sh
   cd ~/presenter
   ./install.sh
   ```

3. **Turn off the old slideshow viewer** so it does not fight mpv for the screen. Look for its start entry in `~/.config/autostart/`, `~/.config/labwc/autostart`, `~/.config/wayfire.ini` or `~/.config/lxsession/LXDE-pi/autostart`, and remove it.

4. Create folders and upload slides on the web page, or copy them into folders under `~/slides/flat` (Flat versions) and `~/slides/scope` (Scope versions). Images left directly in `~/slides` by an earlier version are moved into this screen's format, into the folder "General".

5. Reboot (`sudo reboot`) and check that the slideshow appears without touching anything.

### What install.sh does (manual install)

If you prefer doing it by hand, these are the same steps:

```sh
# 1. Packages. Raspberry Pi OS blocks system-wide pip installs, so use apt:
sudo apt update
sudo apt install mpv python3-flask python3-pil python3-waitress wlr-randr

# 2. Folders, services and the desktop autostart entry
#    (the unit files expect the code in ~/presenter)
mkdir -p ~/slides/flat ~/slides/scope ~/.config/systemd/user ~/.config/autostart
cp ~/presenter/systemd/*.service ~/.config/systemd/user/
cp ~/presenter/autostart/presenter-mpv.desktop ~/.config/autostart/

# 3. Login for the web page, and this screen's format (flat or scope)
printf 'PRESENTER_USER=cinema\nPRESENTER_PASSWORD=pick-a-password\nPRESENTER_FORMAT=flat\n' > ~/.config/presenter.env
chmod 600 ~/.config/presenter.env

# 4. Start the web app at boot (linger = user services start without a login)
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now presenter-web.service

# 5. Keep the screen on, and never go to sleep
sudo raspi-config nonint do_blanking 1
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

`python3-pil` (thumbnails) and `python3-waitress` (a sturdier web server) are optional. Without them the app still works, using full images as thumbnails and Flask's built-in server.

If you would rather use pip, use a virtual environment: `python3 -m venv ~/presenter/.venv && ~/presenter/.venv/bin/pip install -r ~/presenter/requirements.txt`. Then point `ExecStart` in `presenter-web.service` at `~/presenter/.venv/bin/python`.

## How it starts and recovers

| Part | Started by | If it dies |
|---|---|---|
| `presenter-web.service` (Flask, port 8888) | systemd user service, enabled at boot | restarted after 3 s |
| `presenter-mpv.service` (mpv, fullscreen) | the desktop autostart entry `presenter-mpv.desktop`, once the desktop is up | restarted after 3 s |
| `presenter-hdmi.service` (HDMI mode keeper) | together with mpv; exits at once unless `PRESENTER_HDMI_MODE` is set | restarted after 10 s |

mpv needs the screen, so it cannot start before the desktop does. The autostart entry passes the desktop's display settings (`WAYLAND_DISPLAY` or `DISPLAY`) to systemd and then starts the mpv service. That way it works on Wayland (labwc, wayfire) and X11 alike, and systemd still restarts mpv if it crashes or someone closes it.

After a power cut the Pi boots, logs into the desktop and both services come back on their own. The web app also copes with mpv restarting: it opens a fresh socket connection for every action.

## Folders, switching slides off, shuffle and timing

```
~/slides/flat/Coming soon/dune.jpg
~/slides/flat/Kids Club/paddington.jpg
~/slides/scope/Coming soon/dune.jpg
```

- **Folders.** The start page lists this screen's folders. Each has a switch: switched-on folders play in the slideshow, switched-off ones are kept but not shown. **+ New folder** creates one. Tap a folder to open it: **Upload** adds slides to it, **Play only this folder** switches all the others off, and an empty folder can be deleted.
- **Switching slides off.** Every slide has its own switch. A switched-off slide stays in its folder (dimmed, marked "Off") but is left out of the slideshow until you switch it back on. Handy for events that are over, or slides that are not due yet.
- **Tap a slide** for more: **Show now** jumps to it, **Show only this** keeps it on screen, **Move to another folder** and **Delete**. A switched-off slide can still be shown with **Show only this**.
- **Seconds per slide** and **Shuffle** are on the start page and apply to the whole slideshow straight away. Shuffle mixes the order again whenever the slideshow changes.
- The screen keeps showing the current slide while you change things around it, so switching and moving do not make it flicker. If you switch off the slide on screen, the next one follows.
- Everything is remembered across restarts and power cuts, in `~/.config/presenter/` on the Pi (`state.json` for the switches and settings, `playlist.m3u8` and `player.conf` for mpv).
- **Copying files in without the web page** (over the network or from a USB stick) works too: put them into a folder, and they join the slideshow within 30 seconds. Images put directly into `~/slides/flat` or `~/slides/scope`, outside any folder, are moved into the folder "General".

## Flat and Scope screens

Every slide exists twice, once per format: the Flat version (1998x1080, or 3996x2160 for 4K) under `~/slides/flat/` and the Scope version (2048x858, or 4096x1716 for 4K) under `~/slides/scope/`, each with its own folders.

Each Pi is set up for the screen it drives (`PRESENTER_FORMAT=flat` or `scope`, asked by the installer). It plays **only its own format** and frames the picture to it, with black around it:

| Format | Picture area | In a 2048x1080 (DCI 2K) output | In a 1920x1080 output |
|---|---|---|---|
| Flat | 1.85:1, centered | 1998x1080, 25 px black left and right | 1920x1038 |
| Scope | 2.39:1, centered | 2048x858, 111 px black top and bottom | 1920x804 |

- On the web page, the **Flat** and **Scope** tabs at the top switch between the two formats. The tab marked **on screen** is the one this Pi plays. The other format can be filled and organized too (useful to keep both sets on every Pi), but its slides are never shown on this screen.
- Thumbnails take the shape of the format, so they show how each slide will sit on the screen.
- A slide with the wrong shape is fitted without cropping or stretching. For example, a 16:9 image in a Scope folder gets black bars at its sides.
- To move a Pi to the other kind of screen, change `PRESENTER_FORMAT` in `~/.config/presenter.env` and run `systemctl --user restart presenter-web presenter-mpv`.
- The Pi only frames its own picture. The projector's lens and masking stay set for that theatre as usual.

The framing for each format lives in `mpv/formats.conf` (one mpv profile per format). To add a format, add a profile there and an entry in `FORMATS` at the top of `app.py`. The folder of the same name is created automatically.

## Forcing the HDMI signal

By default the Pi sends whatever the projector reports as its preferred mode, often 1920x1080. `hdmi_mode.py` forces a different signal and keeps it. There are two sensible choices:

| Signal | What the screen gets | Projector side |
|---|---|---|
| **2048x1080** (recommended) | The DCI 2K container, like a DCP: Flat slides land exactly on 1998x1080, Scope slides exactly on 2048x858 | Set the HDMI input's format (screen file or macro) to Flat or Scope, the same way as for DCPs |
| **2048x858** (Scope theatre only) | An exact Scope signal, no black bars sent at all | The input must accept this non-standard timing; many cinema inputs do not |

An exact 1998x1080 Flat signal is not possible: standard video timings come in steps of 8 pixels, so it becomes 2000x1080. For Flat, use 2048x1080. Either way the slides need no change, since the framing adapts to any signal.

**1. See what the projector offers** (over SSH or on the Pi's desktop):

```sh
cd ~/presenter
./hdmi_mode.py list
```

**2. Try a mode safely.** Watch the screen. If the picture is right, press Enter. If you don't (for example because the projector shows "no signal"), the Pi switches back after 20 seconds on its own:

```sh
./hdmi_mode.py try 2048x1080@24
./hdmi_mode.py try 2048x858@24
```

Modes the projector does not list are created as custom modes. The refresh rate after `@` is optional: without it you get the highest rate the projector offers for that size, or 60 for a custom mode. Try 24, 48 or 60, depending on what the input accepts.

If a mode is already kept with `PRESENTER_HDMI_MODE` (step 3), stop the keeper while trying others, or it switches back within 10 seconds: `systemctl --user stop presenter-hdmi`.

**3. Keep it.** Add the working mode to `~/.config/presenter.env` and restart the player:

```sh
echo "PRESENTER_HDMI_MODE=2048x1080@24" >> ~/.config/presenter.env
systemctl --user restart presenter-mpv
```

`presenter-hdmi.service` then sets that mode whenever the slideshow starts. It checks every 10 seconds and sets it again if something else changed it, for example when the projector was turned off and on. To go back to the default, remove the line, restart the player and reboot.

Check what it did with `journalctl --user -u presenter-hdmi -n 20`. If the screen ever stays black, connect over SSH, remove the line and reboot.

## Finding the Pi's address

On the Pi, run:

```sh
hostname -I
```

Then open `http://<that-ip>:8888`. On most PCs and iPhones `http://<hostname>.local:8888` also works (for example `http://raspberrypi.local:8888`). Android support for `.local` names varies.

**Make the address permanent** so staff bookmarks keep working:

- **Best: a DHCP reservation in the router.** Find the Pi's MAC address with `cat /sys/class/net/eth0/address` (or `wlan0` for Wi-Fi). Then reserve an IP for it in the router's DHCP settings.
- **Or a static IP on the Pi.** Raspberry Pi OS Bookworm and Trixie use NetworkManager:

  ```sh
  nmcli con show                       # find the connection name, e.g. "Wired connection 1"
  sudo nmcli con mod "Wired connection 1" ipv4.method manual \
      ipv4.addresses 192.168.1.50/24 ipv4.gateway 192.168.1.1 ipv4.dns 192.168.1.1
  sudo nmcli con up "Wired connection 1"
  ```

  Pick an address outside the router's DHCP range. `sudo nmtui` does the same with a menu.

## Settings

Edit `~/.config/presenter.env`, then restart both services with `systemctl --user restart presenter-web presenter-mpv`.

| Variable | Default | Meaning |
|---|---|---|
| `PRESENTER_USER` | `cinema` | web login name |
| `PRESENTER_PASSWORD` | `change-me` | web password (the installer sets one) |
| `PRESENTER_FORMAT` | `flat` | this Pi's screen: `flat` or `scope` (anything else stops the slideshow) |
| `PRESENTER_SLIDES` | `~/slides` (`/home/pi/slides` for user pi) | folder holding `flat/` and `scope/` |
| `PRESENTER_HDMI_MODE` | not set | force the HDMI signal, e.g. `2048x1080@24` (see "Forcing the HDMI signal") |
| `PRESENTER_HDMI_OUTPUT` | first HDMI output | which output `hdmi_mode.py` changes, e.g. `HDMI-A-2` |
| `PRESENTER_DURATION` | `10` | seconds per slide, until it is changed on the web page |
| `PRESENTER_PORT` | `8888` | web port |
| `PRESENTER_SOCKET` | `/tmp/mpv.sock` | mpv IPC socket |

The same defaults are at the top of `app.py`, along with the upload limit (`MAX_UPLOAD_MB`), the minimum free disk space (`MIN_FREE_MB`) and the thumbnail size.

**Security note:** basic auth over plain HTTP is enough to keep guests on a shared network out of the page. However, the password is not encrypted on the wire, so use one that is not used anywhere else. If the router can isolate guest Wi-Fi from staff devices, turn that on too.

## Testing

### 1. Automated tests (any Debian or Ubuntu machine, about 2 minutes)

The tests start a real mpv without a screen (`--vo=null`) and drive every button through the web API. They cover login, the cross-site request block, next/prev/pause, "only this", resume, upload (filename cleanup, wrong file types, size limit), delete, thumbnails, the exact Flat and Scope frames, keeping the two formats apart, folders (creating, switching, playing only one, deleting), switching slides off without disturbing the screen, moving slides, shuffle, seconds per slide, files copied in without the page, everything coming back after an mpv restart, and mpv being down. The HDMI tests switch modes on a headless labwc (Wayland) and an Xorg dummy display (X11): trying and switching back, keeping a mode, and setting it again after a change.

```sh
sudo apt install mpv python3-flask python3-pil python3-pytest
# optional, for the HDMI mode tests on headless displays:
sudo apt install labwc wlr-randr xserver-xorg-video-dummy x11-xserver-utils
cd ~/presenter
python3 -m pytest -v
```

### 2. Full rehearsal in a Proxmox VM

Raspberry Pi OS images for the Pi do not boot in an x86 VM. Debian 13 is what Raspberry Pi OS Trixie is built on, with the same mpv, Flask and systemd, so it makes a good stand-in.

1. Create a VM from the Debian 13 netinst ISO: 2 vCPU, 2 GB RAM (like a small Pi 4), 16 GB disk, network on your LAN bridge (for example `vmbr0`), Display "Default" or "VirtIO-GPU".
2. In the installer, leave the root password empty (your user then gets sudo). In the software selection, pick a desktop:
   - **Xfce** to test X11
   - **GNOME** to test Wayland (GNOME uses Wayland by default)

   You can do one, then reinstall with the other, or clone the VM.
3. Turn on auto login: on Xfce, set `autologin-user=<you>` under `[Seat:*]` in `/etc/lightdm/lightdm.conf`. On GNOME, use Settings > Users > Automatic Login.

   Then turn off the screen lock and screen blanking. Unlike Raspberry Pi OS, these desktops lock an idle screen, which looks like being logged out. In a terminal on the VM's desktop:

   ```sh
   # GNOME
   gsettings set org.gnome.desktop.screensaver lock-enabled false
   gsettings set org.gnome.desktop.session idle-delay 0
   gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type nothing

   # Xfce: remove the screen lockers, then in Settings > Power Manager set
   # every "Blank after", "Put to sleep after" and "Switch off after" to Never
   sudo apt remove light-locker xfce4-screensaver
   ```
4. Install as on the Pi: `sudo apt install git`, clone into `~/presenter`, run `./install.sh` (the Pi-only screen blanking step is skipped automatically).
5. Watch the "TV" in the Proxmox console and open `http://<vm-ip>:8888` on your phone.

Checklist (use the same one on the Pi):

- [ ] After a reboot the slideshow comes up fullscreen without any clicks.
- [ ] Left alone overnight, and paused for an hour, the screen shows the slide: no lock screen, no blank screen, no sleep.
- [ ] Previous / Pause / Play / Next, Show now, Show only this and Resume slideshow all change the screen within a second.
- [ ] Upload from a phone (several files at once) and delete work. A `.txt` file and a file over 50 MB are refused.
- [ ] The screen shows only the slides of its own format, in the right frame. Uploading to the other tab changes nothing on screen.
- [ ] Folders: create one, upload into it, switch it off and on, Play only this folder. Move a slide between folders.
- [ ] Switching a slide off removes it from the slideshow without a flicker. Seconds per slide and Shuffle take effect at once.
- [ ] After a reboot, switched-off folders and slides, shuffle and seconds per slide are as you left them.
- [ ] With `PRESENTER_HDMI_MODE` set: the mode is back after a reboot and, on the Pi, after the projector is turned off and on.
- [ ] Two phones at once stay in sync (the page refreshes every few seconds).
- [ ] `pkill mpv`: the slideshow is back in about 3 seconds.
- [ ] `pkill -f presenter/app.py`: the page works again in about 3 seconds.
- [ ] Proxmox **Stop** (a hard power off, like a power cut), then **Start**: everything returns on its own.
- [ ] A browser without the password only gets a login prompt.

What the VM cannot tell you: the Pi's GPU speed and limits, and whether Raspberry Pi OS runs the autostart entry. So after the VM run, do the checklist once more on the Pi itself.

## Troubleshooting

```sh
systemctl --user status presenter-web presenter-mpv   # are they running?
journalctl --user -u presenter-mpv -n 50              # mpv log
journalctl --user -u presenter-web -n 50              # web app log
```

- **The page says "Player not running":** run `systemctl --user restart presenter-mpv`. This also works over SSH once the desktop has started.
- **A lock or login screen appears after a while:** that is the desktop locking an idle screen, or the machine going to sleep. Raspberry Pi OS does neither by default. The installer stops sleep for good, and mpv keeps screensavers away while it runs (`--stop-screensaver=always`), even when paused. For desktops that lock anyway (GNOME, Xfce on the test VM), turn the lock off as in the VM steps under Testing. To find out what happened: `journalctl -b | grep -iE "suspend|sleep"` shows sleep, and if the slideshow is still running after you unlock, it was only the lock.
- **No slideshow after boot:** check that desktop auto login is on. If the desktop ignores `~/.config/autostart`, add this line to `~/.config/labwc/autostart` (labwc) or the `[autostart]` section of `~/.config/wayfire.ini` (wayfire):

  ```sh
  sh -c "systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY; systemctl --user restart presenter-mpv.service"
  ```

- **Black screen, mpv log says "Unknown profile":** `PRESENTER_FORMAT` in `~/.config/presenter.env` must be exactly `flat` or `scope`.
- **Black screen after setting `PRESENTER_HDMI_MODE`:** the projector does not accept that signal. Over SSH, remove the line from `~/.config/presenter.env` and reboot. Use `./hdmi_mode.py try` first next time.
- **Talk to mpv directly** (needs `sudo apt install socat`):

  ```sh
  echo '{"command":["get_property","path"]}' | socat - /tmp/mpv.sock
  ```

- **Keep it fast on a Pi 4:** export slides at their DCI 2K size (1998x1080 or 2048x858) as JPG. Larger images are shrunk automatically when shown, because the Pi 4 GPU cannot display images wider or taller than 4096 pixels. Shrinking them costs time on every change, though.

## Uninstall

```sh
systemctl --user disable --now presenter-web presenter-mpv presenter-hdmi
rm ~/.config/autostart/presenter-mpv.desktop ~/.config/systemd/user/presenter-*.service
systemctl --user daemon-reload
sudo systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target  # allow sleep again
```

## Files

| File | Purpose |
|---|---|
| `app.py` | Flask web app, talks to mpv over `/tmp/mpv.sock` |
| `templates/index.html` | the control page (HTML, CSS and JS, no external files) |
| `systemd/presenter-mpv.service` | runs mpv fullscreen with the IPC socket, restarts it |
| `mpv/formats.conf` | the Flat and Scope framing, one mpv profile each |
| `hdmi_mode.py` | list, try and keep an HDMI output mode |
| `systemd/presenter-hdmi.service` | keeps `PRESENTER_HDMI_MODE` set while the slideshow runs |
| `systemd/presenter-web.service` | runs the web app at boot, restarts it if it stops |
| `autostart/presenter-mpv.desktop` | starts the mpv service when the desktop is up |
| `install.sh` | one-step install on the Pi |
| `tests/test_app.py` | end-to-end tests with a real headless mpv |
| `tests/test_hdmi_mode.py` | HDMI mode tests on headless Wayland and X11 displays |
