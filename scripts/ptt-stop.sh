#!/bin/sh
# Push-to-talk stop signal for VoiceMode.
#
# Creates a signal file that tells the VoiceMode recording loop to stop
# immediately. The recording function checks for this file on each iteration
# (~every 30ms) and deletes it after reading.
#
# Usage:
#   ./scripts/ptt-stop.sh
#   # or just:
#   touch ~/.voicemode/push-to-talk-stop
#
# To bind to a Claude Code keybinding, add to ~/.claude/keybindings.json:
#   [
#     {
#       "key": "ctrl+space",
#       "command": "shell",
#       "args": "touch ~/.voicemode/push-to-talk-stop"
#     }
#   ]
#
# Note: Check Claude Code docs for the exact keybinding format -- it may
# differ from the example above.

touch "${HOME}/.voicemode/push-to-talk-stop"
