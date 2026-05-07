#!/bin/bash
# Kiwix download watchdog. Runs every 10 min via cron.
# If no curl is running for kiwix AND there are incomplete ZIMs, restart the downloader.

LOG="/Volumes/Media/apocalypse/kiwix/watchdog.log"
SCRIPT="/Volumes/Media/apocalypse/kiwix/download.sh"
ZIM_DIR="/Volumes/Media/apocalypse/kiwix/zim"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

# Drive must be mounted
if [ ! -d "$ZIM_DIR" ]; then
  echo "[$(ts)] drive not mounted, skip" >> "$LOG"
  exit 0
fi

# Anything still incomplete?
incomplete=$(ls "$ZIM_DIR"/*.part 2>/dev/null | wc -l | tr -d ' ')
if [ "$incomplete" = "0" ]; then
  # No partials, but check if everything in download.sh is actually present
  expected=$(grep -E '^download "' "$SCRIPT" | sed -E 's/.*"([^"]+\.zim)".*/\1/')
  missing=0
  for z in $expected; do
    [ ! -f "$ZIM_DIR/$z" ] && missing=$((missing+1))
  done
  if [ "$missing" = "0" ]; then
    # Nothing to do, stay quiet
    exit 0
  fi
fi

# Is download.sh or a kiwix curl already running?
if pgrep -f "/Volumes/Media/apocalypse/kiwix/download.sh" >/dev/null; then
  echo "[$(ts)] downloader already running, ok" >> "$LOG"
  exit 0
fi
if pgrep -f "curl.*Volumes/Media/apocalypse/kiwix" >/dev/null; then
  echo "[$(ts)] curl in flight, ok" >> "$LOG"
  exit 0
fi

# Stalled, restart
echo "[$(ts)] no downloader running with incomplete files, restarting" >> "$LOG"
nohup bash "$SCRIPT" >> /Volumes/Media/apocalypse/kiwix/download.out 2>&1 &
disown
