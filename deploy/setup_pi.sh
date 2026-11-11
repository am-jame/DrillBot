#!/usr/bin/env bash
# One-time setup on a Raspberry Pi (or any Linux with systemd). Run from anywhere:
#   bash deploy/setup_pi.sh
# It creates the Python environment, installs the packages, and installs + starts the bot
# as a systemd user service that starts at boot and restarts if it stops. Safe to re-run.
set -euo pipefail

BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BOT_DIR"
echo "Setting up DrillBot in $BOT_DIR"

# 1. Python 3.10+ with venv support.
if ! command -v python3 >/dev/null; then
    echo "Python 3 isn't installed. Run: sudo apt install python3 python3-venv" >&2; exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    echo "The bot needs Python 3.10 or newer (found $(python3 -V))." >&2; exit 1
fi
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv || { echo "Couldn't create a virtual environment. Run: sudo apt install python3-venv" >&2; exit 1; }
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
echo "Python packages installed."

# 2. Settings.
if [ ! -f bot.env ]; then
    cp bot.env.example bot.env
    chmod 600 bot.env
    echo "Created bot.env from bot.env.example. Fill in the token (and the other values), then re-run this script." >&2
    exit 1
fi
chmod 600 bot.env
if ! ( source ./bot.env && [ -n "${DISCORD_TOKEN:-}" ] ); then
    echo "bot.env has no DISCORD_TOKEN yet. Fill it in, then re-run this script." >&2; exit 1
fi
[ -f words.json ] || { echo "words.json is missing. It isn't in git: copy it over from the old machine (or start with [] for no lists)." >&2; exit 1; }
[ -f stats.db ] || echo "Note: no stats.db yet. Copy it over from the old machine to keep stats and running quizzes."

# 3. The service.
mkdir -p "$HOME/.config/systemd/user"
sed "s|@BOT_DIR@|$BOT_DIR|g" deploy/drillbot.service > "$HOME/.config/systemd/user/drillbot.service"
# "Linger" keeps user services running without anyone logged in, and starts them at boot.
if [ "$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null)" != "yes" ]; then
    loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER"
fi
logins_before=$(grep -c "Logged on as" bot.log 2>/dev/null || true)
systemctl --user daemon-reload
systemctl --user enable drillbot >/dev/null
systemctl --user restart drillbot
echo "Service installed and (re)started. Waiting for the bot to log in..."

for _ in $(seq 1 60); do
    [ "$(grep -c "Logged on as" bot.log 2>/dev/null || true)" -gt "${logins_before:-0}" ] && break
    sleep 1
done
systemctl --user --no-pager status drillbot | head -5
tail -3 bot.log 2>/dev/null || true
echo
echo "Done. Useful commands:"
echo "  systemctl --user restart drillbot     # after changing code or bot.env"
echo "  systemctl --user stop drillbot"
echo "  tail -f $BOT_DIR/bot.log"
