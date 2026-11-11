#!/usr/bin/env bash
# Starts the Discord bot with the settings in bot.env. Used by the service (systemd on the
# Pi, launchd on the Mac), but can also be run by hand.
cd "$(dirname "$0")" || exit 1
source ./bot.env
exec ./.venv/bin/python discbot.py
