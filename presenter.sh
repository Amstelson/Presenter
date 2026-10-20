#!/bin/bash
# Start or stop Presenter by hand. The desktop icons "Start Presenter" and
# "Stop Presenter" run this, and it works in a terminal or over SSH too.
#
#   ./presenter.sh start    start (or restart) the slideshow and the web page,
#                           and from now on start them after every reboot
#   ./presenter.sh stop     stop both, and keep them off after reboots until
#                           "start" is used (asks first; --yes skips the question)
#   ./presenter.sh status   show what is running
#
# --wait keeps the window open for a moment at the end (used by the icons).
set -u

APP_DIR=$(cd "$(dirname "$0")" && pwd)
AUTOSTART="$HOME/.config/autostart/presenter-mpv.desktop"
ENV_FILE="$HOME/.config/presenter.env"
STOPPED="$HOME/.config/presenter/stopped"  # while this exists, the services do not start

web_address() {
    local port
    port=$(sed -n 's/^PRESENTER_PORT=//p' "$ENV_FILE" 2>/dev/null | tail -n 1)
    echo "http://$(hostname -I 2>/dev/null | cut -d' ' -f1):${port:-8888}"
}

status() {
    local web mpv
    web=$(systemctl --user is-active presenter-web)
    mpv=$(systemctl --user is-active presenter-mpv)
    echo "Web page:   $web"
    echo "Slideshow:  $mpv"
    if [ -e "$STOPPED" ]; then
        echo "At startup: stays off (it was stopped with Stop Presenter)"
    else
        echo "At startup: starts by itself"
    fi
    if [ "$web" = active ] && [ "$mpv" = active ]; then
        echo
        echo "Presenter is running. Web page: $(web_address)"
        return 0
    fi
    for unit in presenter-web presenter-mpv; do
        if ! systemctl --user is-active --quiet "$unit"; then
            echo
            echo "--- $unit is not running. Its last messages:"
            systemctl --user status "$unit" --no-pager --lines 8 2>&1 | tail -n 8
        fi
    done
    if [ -z "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]; then
        echo
        echo "Tip: the slideshow needs the screen. If it does not start from here,"
        echo "use the Start Presenter icon on the Pi's desktop."
    fi
    return 1
}

start() {
    echo "Starting Presenter..."
    rm -f "$STOPPED"
    mkdir -p "$(dirname "$AUTOSTART")"
    cp "$APP_DIR/autostart/presenter-mpv.desktop" "$AUTOSTART"  # in case it went missing
    systemctl --user reset-failed presenter-web presenter-mpv presenter-hdmi 2>/dev/null
    systemctl --user enable --quiet presenter-web
    systemctl --user restart presenter-web
    if [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]; then  # hand the screen to systemd
        systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY 2>/dev/null
    fi
    systemctl --user restart presenter-mpv
    sleep 3  # give both a moment to come up (or fail)
    echo
    status
}

stop() {
    if [ "$1" != yes ] && [ -t 0 ]; then
        read -r -p "Are you sure? Stop the slideshow and keep it off, also after restarts? [y/N] " answer
        case "$answer" in
            [yYaA]*) ;;
            *) echo "Nothing was changed."; return 1 ;;
        esac
    fi
    mkdir -p "$(dirname "$STOPPED")"
    touch "$STOPPED"
    systemctl --user stop presenter-mpv presenter-hdmi presenter-web
    echo
    echo "Presenter is stopped and stays off after restarts."
    echo "To start it again, use the Start Presenter icon or run: $APP_DIR/presenter.sh start"
}

command=${1:-}
yes=no
wait=no
for arg in "${@:2}"; do
    case "$arg" in
        --yes) yes=yes ;;
        --wait) wait=yes ;;
    esac
done

case "$command" in
    start) start ;;
    stop) stop "$yes" ;;
    status) status ;;
    *) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 2 ;;
esac
result=$?

if [ "$wait" = yes ]; then
    echo
    read -r -t 20 -p "This window closes in 20 seconds (or press Enter). " _
fi
exit $result
