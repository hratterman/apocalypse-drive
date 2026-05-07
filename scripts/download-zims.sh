#!/bin/bash
# Kiwix ZIM downloader — resumable, correct URLs verified May 2026
# Skips files already fully downloaded. Resumes .part files.

ZIM_DIR="/Volumes/Media/apocalypse/kiwix/zim"
LOG="/Volumes/Media/apocalypse/kiwix/download.log"
BASE="https://download.kiwix.org/zim"

mkdir -p "$ZIM_DIR"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG"
}

download() {
  local name="$1"
  local url="$2"
  local dest="$ZIM_DIR/$name"
  local part="${dest}.part"

  if [ -f "$dest" ]; then
    log "SKIP (complete): $name"
    return 0
  fi

  log "START/RESUME: $name"
  curl -L -C - --retry 10 --retry-delay 30 --retry-all-errors \
    --connect-timeout 30 --speed-limit 10000 --speed-time 60 \
    -o "$part" "$url" 2>>"$LOG"
  local code=$?

  if [ $code -eq 0 ]; then
    mv "$part" "$dest"
    chmod 644 "$dest"
    log "DONE: $name"
  else
    log "FAILED (exit $code): $name — partial kept for resume"
  fi
}

log "=== Kiwix download session started ==="

# Wikipedia (~115 GB)
download "wikipedia_en_all_maxi_2026-02.zim" \
  "$BASE/wikipedia/wikipedia_en_all_maxi_2026-02.zim"

# Gutenberg (~206 GB)
download "gutenberg_en_all_2025-11.zim" \
  "$BASE/gutenberg/gutenberg_en_all_2025-11.zim"

# Stack Overflow (~75 GB)
download "stackoverflow.com_en_all_2023-11.zim" \
  "$BASE/stack_exchange/stackoverflow.com_en_all_2023-11.zim"

# Khan Academy (~168 GB) — lives in /other/
download "khanacademy_en_all_2023-03.zim" \
  "$BASE/other/khanacademy_en_all_2023-03.zim"

# Wikivoyage (~1 GB)
download "wikivoyage_en_all_maxi_2026-03.zim" \
  "$BASE/wikivoyage/wikivoyage_en_all_maxi_2026-03.zim"

# Wikibooks (~5.8 GB)
download "wikibooks_en_all_maxi_2026-04.zim" \
  "$BASE/wikibooks/wikibooks_en_all_maxi_2026-04.zim"

# Wikisource (~18 GB)
download "wikisource_en_all_maxi_2026-02.zim" \
  "$BASE/wikisource/wikisource_en_all_maxi_2026-02.zim"

# Wikispecies (~3.2 GB) — lives in /other/
download "wikispecies_en_all_maxi_2026-04.zim" \
  "$BASE/other/wikispecies_en_all_maxi_2026-04.zim"

# Wikipedia Medicine (~2.1 GB)
download "wikipedia_en_medicine_maxi_2026-04.zim" \
  "$BASE/wikipedia/wikipedia_en_medicine_maxi_2026-04.zim"

# Stack Exchange extras
download "math.stackexchange.com_en_all_2026-02.zim" \
  "$BASE/stack_exchange/math.stackexchange.com_en_all_2026-02.zim"

download "unix.stackexchange.com_en_all_2026-02.zim" \
  "$BASE/stack_exchange/unix.stackexchange.com_en_all_2026-02.zim"

download "askubuntu.com_en_all_2025-12.zim" \
  "$BASE/stack_exchange/askubuntu.com_en_all_2025-12.zim"

download "superuser.com_en_all_2026-02.zim" \
  "$BASE/stack_exchange/superuser.com_en_all_2026-02.zim"

download "serverfault.com_en_all_2026-02.zim" \
  "$BASE/stack_exchange/serverfault.com_en_all_2026-02.zim"

download "physics.stackexchange.com_en_all_2026-02.zim" \
  "$BASE/stack_exchange/physics.stackexchange.com_en_all_2026-02.zim"

log "=== All downloads complete ==="
touch /Volumes/Media/kiwix/zim/.downloads_complete
