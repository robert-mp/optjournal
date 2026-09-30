#!/bin/bash
# Double-click this file to start optjournal. Keep the window open while you
# use it; close it to stop optjournal.
#
# The first start installs uv (the tool that runs optjournal) and Python, which
# takes a minute. Everything else happens in the page, including updates.

cd "$(dirname "$0")" || exit 1

UV="$(command -v uv 2>/dev/null)"
if [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ]; then
  UV="$HOME/.local/bin/uv"
fi
if [ -z "$UV" ]; then
  echo "First start: installing uv, the tool that runs optjournal..."
  if ! curl -LsSf https://astral.sh/uv/install.sh | sh; then
    echo
    echo "Could not install uv. Check your internet connection and try again."
    read -r -p "Press Enter to close this window."
    exit 1
  fi
  UV="$HOME/.local/bin/uv"
fi
export UV

"$UV" run --no-project --python 3.12 launcher/app.py
status=$?
if [ "$status" -ne 0 ]; then
  echo
  read -r -p "optjournal stopped (code $status). Press Enter to close this window."
fi
exit "$status"
