#!/bin/zsh
# Starts the Discord bot with the settings in bot.env. Used by the launchd service
# (~/Library/LaunchAgents/local.drillbot.plist), but can also be run by hand.
cd "${0:A:h}" || exit 1
source ./bot.env
exec ./.venv/bin/python discbot.py
