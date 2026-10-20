#!/bin/sh
# Install Presenter for the current desktop user (e.g. "pi").
# Run it from the cloned repo as that user, NOT with sudo:  ./install.sh
set -eu

APP_DIR=$(cd "$(dirname "$0")" && pwd)
UNIT_DIR="$HOME/.config/systemd/user"
ENV_FILE="$HOME/.config/presenter.env"

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as the desktop user, not root (it uses sudo where needed)." >&2
    exit 1
fi

echo "==> Installing packages"
sudo apt-get update
sudo apt-get install -y mpv python3-flask python3-pil python3-waitress wlr-randr

echo "==> Installing services"
mkdir -p "$HOME/slides/flat" "$HOME/slides/scope" "$UNIT_DIR" "$HOME/.config/autostart"
for unit in presenter-mpv.service presenter-hdmi.service presenter-web.service; do
    sed "s|%h/presenter|$APP_DIR|g" "$APP_DIR/systemd/$unit" > "$UNIT_DIR/$unit"
done
cp "$APP_DIR/autostart/presenter-mpv.desktop" "$HOME/.config/autostart/"

if [ ! -f "$ENV_FILE" ]; then
    printf "Choose a password for the web page (leave empty for a random one): "
    stty -echo 2>/dev/null || true
    read -r password || true
    stty echo 2>/dev/null || true
    echo
    [ -n "$password" ] || password=$(python3 -c "import secrets; print(secrets.token_urlsafe(9))")
    (umask 077 && printf 'PRESENTER_USER=cinema\nPRESENTER_PASSWORD=%s\n' "$password" > "$ENV_FILE")
    echo "Web login: cinema / $password  (change it in $ENV_FILE)"
fi

# The screen format of this Pi decides which slides folder it plays.
if ! grep -q '^PRESENTER_FORMAT=' "$ENV_FILE"; then
    format=""
    while [ "$format" != flat ] && [ "$format" != scope ]; do
        printf "Is this Pi's screen Flat or Scope? Type flat or scope: "
        read -r format || exit 1
        format=$(echo "$format" | tr 'A-Z' 'a-z')
    done
    echo "PRESENTER_FORMAT=$format" >> "$ENV_FILE"
fi
FORMAT=$(sed -n 's/^PRESENTER_FORMAT=//p' "$ENV_FILE" | tail -n 1)
echo "This screen plays the $FORMAT slides from $HOME/slides/$FORMAT"

# Images from older versions sat directly in ~/slides: move them to this screen's folder.
for f in "$HOME"/slides/*; do
    case "$f" in
        *.jpg|*.jpeg|*.png|*.webp|*.gif|*.JPG|*.JPEG|*.PNG|*.WEBP|*.GIF)
            [ -f "$f" ] && mv -n "$f" "$HOME/slides/$FORMAT/" && echo "Moved $(basename "$f") to slides/$FORMAT" ;;
    esac
done
rm -f "$HOME/.config/presenter/mpv.conf"  # format file of an earlier version, no longer used

# Start the user's services at boot, even before the desktop logs in.
sudo loginctl enable-linger "$(id -un)"
systemctl --user daemon-reload
systemctl --user enable presenter-web.service
systemctl --user restart presenter-web.service

if command -v raspi-config >/dev/null 2>&1; then
    echo "==> Turning off screen blanking"
    sudo raspi-config nonint do_blanking 1
fi

echo "==> Never going to sleep (a slideshow screen must stay on)"
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1 || true

if [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]; then
    systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY
    systemctl --user restart presenter-mpv.service
fi

IP=$(hostname -I 2>/dev/null | cut -d' ' -f1)
echo
echo "Done. Create folders and upload slides on the web page (or copy them into folders under $HOME/slides/$FORMAT)."
echo "Web remote: http://${IP:-<pi-ip>}:8888"
echo "Reboot to check that everything starts on its own: sudo reboot"
