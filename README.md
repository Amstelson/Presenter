# Presenter

A promo slideshow for the cinema screen that staff control from a phone or PC.

- Each Pi drives one screen, set up once as **DCI Flat** (1.85:1) or **DCI Scope** (2.39:1).
- **mpv** plays every image in that format's folder (`~/slides/flat` or `~/slides/scope`) fullscreen, in a loop, framed to the format.
- A small **Flask** web app (`app.py`) controls mpv through its IPC socket and serves the control page at **`http://<pi-ip>:8888`**.

```
 phone / PC  --HTTP :8888-->  app.py (Flask)  --JSON over /tmp/mpv.sock-->  mpv (fullscreen)
                                   |                                          |
                                   +--------- ~/slides/flat or scope ---------+
```

The web page lets staff:

- see thumbnails of all images and tap one to show it now
- pause and play, or go to the next or previous image
- use **Only this** to show one image until told otherwise, and **Resume slideshow** to go back to the full loop
- upload images (JPG, PNG, WebP, GIF, up to 50 MB each) into the Flat or Scope folder, and delete them

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

4. Copy your images to `~/slides/flat` (Flat versions) and `~/slides/scope` (Scope versions), or upload them from the web page later. Images left directly in `~/slides` by an earlier version are moved into this screen's folder by the installer.

5. Reboot (`sudo reboot`) and check that the slideshow appears without touching anything.

### What install.sh does (manual install)

If you prefer doing it by hand, these are the same steps:

```sh
# 1. Packages. Raspberry Pi OS blocks system-wide pip installs, so use apt:
sudo apt update
sudo apt install mpv python3-flask python3-pil python3-waitress

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

# 5. Keep the screen on
sudo raspi-config nonint do_blanking 1
```

`python3-pil` (thumbnails) and `python3-waitress` (a sturdier web server) are optional. Without them the app still works, using full images as thumbnails and Flask's built-in server.

If you would rather use pip, use a virtual environment: `python3 -m venv ~/presenter/.venv && ~/presenter/.venv/bin/pip install -r ~/presenter/requirements.txt`. Then point `ExecStart` in `presenter-web.service` at `~/presenter/.venv/bin/python`.

## How it starts and recovers

| Part | Started by | If it dies |
|---|---|---|
| `presenter-web.service` (Flask, port 8888) | systemd user service, enabled at boot | restarted after 3 s |
| `presenter-mpv.service` (mpv, fullscreen) | the desktop autostart entry `presenter-mpv.desktop`, once the desktop is up | restarted after 3 s |

mpv needs the screen, so it cannot start before the desktop does. The autostart entry passes the desktop's display settings (`WAYLAND_DISPLAY` or `DISPLAY`) to systemd and then starts the mpv service. That way it works on Wayland (labwc, wayfire) and X11 alike, and systemd still restarts mpv if it crashes or someone closes it.

After a power cut the Pi boots, logs into the desktop and both services come back on their own. The web app also copes with mpv restarting: it opens a fresh socket connection for every action.

## Flat and Scope screens

Every slide exists twice, once per format, in two folders:

```
~/slides/flat/    Flat versions, 1998x1080 (or 3996x2160 for 4K)
~/slides/scope/   Scope versions, 2048x858 (or 4096x1716 for 4K)
```

Each Pi is set up for the screen it drives (`PRESENTER_FORMAT=flat` or `scope`, asked by the installer). It plays **only its own folder** and frames the picture to that format, with black around it:

| Format | Picture area | In a 2048x1080 (DCI 2K) output | In a 1920x1080 output |
|---|---|---|---|
| Flat | 1.85:1, centered | 1998x1080, 25 px black left and right | 1920x1038 |
| Scope | 2.39:1, centered | 2048x858, 111 px black top and bottom | 1920x804 |

- On the web page, the **Flat** and **Scope** tabs show the two folders. The tab marked **on screen** is the one this Pi plays. **Upload** puts images into the open tab. The other folder can be filled and cleaned up too (useful to keep both sets on every Pi), but its slides are never shown on this screen.
- Thumbnails take the shape of the folder's format, so they show how each slide will sit on the screen.
- A slide with the wrong shape is fitted without cropping or stretching. For example, a 16:9 image in a Scope folder gets black bars at its sides.
- To move a Pi to the other kind of screen, change `PRESENTER_FORMAT` in `~/.config/presenter.env` and run `systemctl --user restart presenter-web presenter-mpv`.
- The Pi only frames its own picture. The projector's lens and masking stay set for that theatre as usual.

**Getting exact DCI frames.** The areas are pixel exact when the Pi sends a **2048x1080** signal (the DCI 2K container). This is what a digital cinema projector uses for DCPs. Whether 2048x1080 is offered depends on what the projector or cinema server tells the Pi it accepts:

- Check the modes on offer in a terminal on the Pi's desktop with `wlr-randr` (Wayland) or `xrandr` (X11).
- If 2048x1080 is listed, select it in the desktop's screen settings (Screen Configuration). The desktop remembers it across reboots.
- With a 1920x1080 signal the shapes are still correct, just slightly smaller (see the table). In that case the projector's own scaling for that input decides the final size on screen.

The framing for each format lives in `mpv/formats.conf` (one mpv profile per format). To add a format, add a profile there and an entry in `FORMATS` at the top of `app.py`. The folder of the same name is created automatically.

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
| `PRESENTER_SLIDES` | `~/slides` (`/home/pi/slides` for user pi) | folder holding `flat/` and `scope/`, as a full path |
| `PRESENTER_DURATION` | `10` | seconds per image |
| `PRESENTER_PORT` | `8888` | web port |
| `PRESENTER_SOCKET` | `/tmp/mpv.sock` | mpv IPC socket |

The same defaults are at the top of `app.py`, along with the upload limit (`MAX_UPLOAD_MB`), the minimum free disk space (`MIN_FREE_MB`) and the thumbnail size.

**Security note:** basic auth over plain HTTP is enough to keep guests on a shared network out of the page. However, the password is not encrypted on the wire, so use one that is not used anywhere else. If the router can isolate guest Wi-Fi from staff devices, turn that on too.

## Testing

### 1. Automated tests (any Debian or Ubuntu machine, about 2 minutes)

The tests start a real mpv without a screen (`--vo=null`) and drive every button through the web API. They cover login, the cross-site request block, next/prev/pause, "only this", resume, upload (filename cleanup, wrong file types, size limit), delete, thumbnails, the exact Flat and Scope frames, keeping the two folders apart (uploads and deletes in the other folder never touch the screen, even with the same file names), and mpv being down.

```sh
sudo apt install mpv python3-flask python3-pil python3-pytest
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
3. Turn on auto login: on Xfce, set `autologin-user=<you>` under `[Seat:*]` in `/etc/lightdm/lightdm.conf`. On GNOME, use Settings > Users > Automatic Login. Turn off screen blanking in the desktop's power settings.
4. Install as on the Pi: `sudo apt install git`, clone into `~/presenter`, run `./install.sh` (the Pi-only screen blanking step is skipped automatically).
5. Watch the "TV" in the Proxmox console and open `http://<vm-ip>:8888` on your phone.

Checklist (use the same one on the Pi):

- [ ] After a reboot the slideshow comes up fullscreen without any clicks.
- [ ] Previous / Pause / Play / Next, tapping a thumbnail, Only this and Resume slideshow all change the screen within a second.
- [ ] Upload from a phone (several files at once) and delete work. A `.txt` file and a file over 50 MB are refused.
- [ ] The screen shows only the slides of its own folder, in the right frame. Uploading to the other tab changes nothing on screen.
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
- **No slideshow after boot:** check that desktop auto login is on. If the desktop ignores `~/.config/autostart`, add this line to `~/.config/labwc/autostart` (labwc) or the `[autostart]` section of `~/.config/wayfire.ini` (wayfire):

  ```sh
  sh -c "systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY; systemctl --user restart presenter-mpv.service"
  ```

- **Black screen, mpv log says "Unknown profile":** `PRESENTER_FORMAT` in `~/.config/presenter.env` must be exactly `flat` or `scope`.
- **Talk to mpv directly** (needs `sudo apt install socat`):

  ```sh
  echo '{"command":["get_property","path"]}' | socat - /tmp/mpv.sock
  ```

- **Keep it fast on a Pi 4:** export slides at their DCI 2K size (1998x1080 or 2048x858) as JPG. Larger images are shrunk automatically when shown, because the Pi 4 GPU cannot display images wider or taller than 4096 pixels. Shrinking them costs time on every change, though.

## Uninstall

```sh
systemctl --user disable --now presenter-web presenter-mpv
rm ~/.config/autostart/presenter-mpv.desktop ~/.config/systemd/user/presenter-*.service
systemctl --user daemon-reload
```

## Files

| File | Purpose |
|---|---|
| `app.py` | Flask web app, talks to mpv over `/tmp/mpv.sock` |
| `templates/index.html` | the control page (HTML, CSS and JS, no external files) |
| `systemd/presenter-mpv.service` | runs mpv fullscreen with the IPC socket, restarts it |
| `mpv/formats.conf` | the Flat and Scope framing, one mpv profile each |
| `systemd/presenter-web.service` | runs the web app at boot, restarts it if it stops |
| `autostart/presenter-mpv.desktop` | starts the mpv service when the desktop is up |
| `install.sh` | one-step install on the Pi |
| `tests/test_app.py` | end-to-end tests with a real headless mpv |
