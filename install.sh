#!/bin/bash
# Apocalypse Drive: installer
#
# Bootstraps an empty drive (or any directory) into a working Apocalypse setup:
#   1. Creates the directory layout
#   2. Downloads cross-platform kiwix-serve binaries from the official
#      kiwix-tools releases (5 platforms, ~230 MB)
#   3. Downloads two llamafiles: Llama 3.2 3B (smaller, ~2.7 GB, runs on 8 GB RAM)
#      and Llama 3.1 8B (~4.9 GB, recommended on 16+ GB RAM)
#   4. Writes the launchers and the README
#   5. Runs the ZIM downloader (interactive: pick which knowledge bases you want)
#
# Default install location: current directory. Pass a path to install elsewhere:
#   ./install.sh /Volumes/Media/apocalypse
#
# Cross-platform: detects macOS / Linux. Windows users should run the .bat
# installer (TODO) or use WSL.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="${1:-$(pwd)/apocalypse}"
KIWIX_TOOLS_VERSION="3.7.0-2"  # https://download.kiwix.org/release/kiwix-tools/
LLAMAFILE_VERSION="0.8.16"     # https://github.com/Mozilla-Ocho/llamafile/releases

# 3B model: 2.7 GB, fits in 8 GB RAM
LLAMA_3B_URL="https://huggingface.co/Mozilla/Llama-3.2-3B-Instruct-llamafile/resolve/main/Llama-3.2-3B-Instruct.Q6_K.llamafile"
LLAMA_3B_NAME="Llama-3.2-3B-Instruct.Q6_K.llamafile"
LLAMA_3B_SIZE_GB="2.7"

# 8B model: 4.9 GB, runs nicely on 16 GB RAM
LLAMA_8B_URL="https://huggingface.co/Mozilla/Meta-Llama-3.1-8B-Instruct-llamafile/resolve/main/Meta-Llama-3.1-8B-Instruct.Q4_K_M.llamafile"
LLAMA_8B_NAME="Meta-Llama-3.1-8B-Instruct.Q4_K_M.llamafile"
LLAMA_8B_SIZE_GB="4.9"

# ---------------------------------------------------------------------------
# Pretty output
# ---------------------------------------------------------------------------

if [ -t 1 ]; then
  BOLD=$'\e[1m'; DIM=$'\e[2m'; AMBER=$'\e[33m'; GREEN=$'\e[32m'
  RED=$'\e[31m'; RESET=$'\e[0m'
else
  BOLD=""; DIM=""; AMBER=""; GREEN=""; RED=""; RESET=""
fi

banner() {
  echo
  echo "${AMBER}${BOLD}=== $* ===${RESET}"
}

step() { echo "${AMBER}>>>${RESET} $*"; }
ok()   { echo "${GREEN}  ✓${RESET} $*"; }
warn() { echo "${AMBER}  !${RESET} $*"; }
die()  { echo "${RED}  ✗ $*${RESET}" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Detect platform
# ---------------------------------------------------------------------------

UNAME_S="$(uname -s)"
UNAME_M="$(uname -m)"

case "$UNAME_S" in
  Darwin) HOST_OS="macos" ;;
  Linux)  HOST_OS="linux" ;;
  *) die "Unsupported host OS: $UNAME_S (use the .bat installer on Windows)" ;;
esac

case "$UNAME_M" in
  arm64|aarch64) HOST_ARCH="arm64" ;;
  x86_64|amd64)  HOST_ARCH="x86_64" ;;
  *) die "Unsupported architecture: $UNAME_M" ;;
esac

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

clear
cat <<'EOF'
    █████╗ ██████╗  ██████╗  ██████╗ █████╗ ██╗  ██╗   ██╗██████╗ ███████╗███████╗
   ██╔══██╗██╔══██╗██╔═══██╗██╔════╝██╔══██╗██║  ╚██╗ ██╔╝██╔══██╗██╔════╝██╔════╝
   ███████║██████╔╝██║   ██║██║     ███████║██║   ╚████╔╝ ██████╔╝███████╗█████╗
   ██╔══██║██╔═══╝ ██║   ██║██║     ██╔══██║██║    ╚██╔╝  ██╔═══╝ ╚════██║██╔══╝
   ██║  ██║██║     ╚██████╔╝╚██████╗██║  ██║███████╗██║   ██║     ███████║███████╗
   ╚═╝  ╚═╝╚═╝      ╚═════╝  ╚═════╝╚═╝  ╚═╝╚══════╝╚═╝   ╚═╝     ╚══════╝╚══════╝
                       OFFLINE KNOWLEDGE TERMINAL: INSTALLER
EOF
echo
echo "  Host OS:    $HOST_OS / $HOST_ARCH"
echo "  Install to: $INSTALL_DIR"
echo
echo "  ${BOLD}This is the headless / CLI installer.${RESET}"
echo "  Most people want the GUI app instead:"
echo "    ${AMBER}https://github.com/hratterman/apocalypse-drive/releases/latest${RESET}"
echo
echo "  The GUI gives you a setup wizard with checkboxes, live disk meter,"
echo "  and a progress page. This script does everything from the terminal."
echo "  Use this if you're on a server, Linux box without GUI, or just prefer it."
echo
echo "  This installer will:"
echo "    1. Create the directory layout in the install path"
echo "    2. Download kiwix-serve binaries for 5 platforms (~230 MB)"
echo "    3. Download one or both Llama models (~2.7 GB or ~7.6 GB total)"
echo "    4. Set up cross-platform launchers"
echo "    5. (Optional) Download Wikipedia + other ZIM archives (up to ~540 GB)"
echo
read -rp "  Proceed? [y/N] " ans
case "$ans" in [Yy]*) ;; *) die "aborted" ;; esac

# ---------------------------------------------------------------------------
# 1. Create layout
# ---------------------------------------------------------------------------

banner "1/5  Creating directory layout"
mkdir -p "$INSTALL_DIR/bin" \
         "$INSTALL_DIR/llm" \
         "$INSTALL_DIR/kiwix/zim"
ok "Directory layout created at $INSTALL_DIR"

# ---------------------------------------------------------------------------
# 2. Download kiwix-tools binaries
# ---------------------------------------------------------------------------

banner "2/5  Downloading kiwix-serve binaries (~230 MB)"

# kiwix-tools releases use these archive names:
KIWIX_BASE="https://download.kiwix.org/release/kiwix-tools"

declare -a KIWIX_DLS=(
  "macos-arm64:kiwix-tools_macos-arm64-${KIWIX_TOOLS_VERSION}.tar.gz"
  "macos-x86_64:kiwix-tools_macos-${KIWIX_TOOLS_VERSION}.tar.gz"
  "linux-x86_64:kiwix-tools_linux-x86_64-${KIWIX_TOOLS_VERSION}.tar.gz"
  "linux-aarch64:kiwix-tools_linux-aarch64-${KIWIX_TOOLS_VERSION}.tar.gz"
  "win-x86_64:kiwix-tools_win-i686-${KIWIX_TOOLS_VERSION}.zip"
)

for entry in "${KIWIX_DLS[@]}"; do
  platform="${entry%%:*}"
  archive="${entry##*:}"
  target_dir="$INSTALL_DIR/bin/$platform"

  # Skip if binary already present
  if [ -x "$target_dir/kiwix-serve" ] || [ -x "$target_dir/kiwix-serve.exe" ]; then
    ok "$platform: already installed, skipping"
    continue
  fi

  step "Downloading $platform"
  mkdir -p "$target_dir"
  tmp="$(mktemp -d)"
  url="$KIWIX_BASE/$archive"

  if ! curl --fail --location --show-error --silent -o "$tmp/$archive" "$url"; then
    warn "Download failed for $platform: skipping (you can retry later)"
    rm -rf "$tmp"
    continue
  fi

  # Extract just the binaries we need (kiwix-serve, kiwix-search, dependencies)
  case "$archive" in
    *.tar.gz)
      tar -xzf "$tmp/$archive" -C "$tmp"
      # The archive contains a single top-level dir
      extracted_dir="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -1)"
      cp "$extracted_dir"/* "$target_dir/" 2>/dev/null || true
      ;;
    *.zip)
      unzip -q "$tmp/$archive" -d "$tmp"
      extracted_dir="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -1)"
      cp "$extracted_dir"/* "$target_dir/" 2>/dev/null || true
      ;;
  esac

  rm -rf "$tmp"
  ok "$platform installed"
done

# ---------------------------------------------------------------------------
# 3. Download llamafiles
# ---------------------------------------------------------------------------

banner "3/5  Downloading LLM models"

# Detect available RAM to recommend which model
if [ "$HOST_OS" = "macos" ]; then
  RAM_GB=$(($(sysctl -n hw.memsize) / 1024 / 1024 / 1024))
else
  RAM_GB=$(awk '/MemTotal/ {print int($2/1024/1024)}' /proc/meminfo)
fi

echo "  Host RAM: ${RAM_GB} GB"
echo
echo "  Available models:"
echo "    [1] Llama 3.2 3B Instruct Q6 (${LLAMA_3B_SIZE_GB} GB): fits 8 GB RAM"
echo "    [2] Llama 3.1 8B Instruct Q4 (${LLAMA_8B_SIZE_GB} GB): recommended for 16+ GB RAM"
echo "    [3] Both (${LLAMA_3B_SIZE_GB} + ${LLAMA_8B_SIZE_GB} GB, runtime auto-picks)"
echo "    [4] Skip (you'll need to provide your own llamafile)"
echo
default="3"
[ "$RAM_GB" -lt 12 ] && default="1"
read -rp "  Pick [1-4] (default: $default): " model_choice
model_choice="${model_choice:-$default}"

download_llamafile() {
  local url="$1" name="$2" size="$3"
  local target="$INSTALL_DIR/llm/$name"

  if [ -f "$target" ]; then
    actual_size=$(du -m "$target" | awk '{print $1}')
    expected_mb=$(awk "BEGIN { print int($size * 1024) }")
    # Check size is roughly right (within 100MB)
    if [ "$actual_size" -gt $((expected_mb - 100)) ]; then
      ok "$name already downloaded (${actual_size} MB)"
      chmod +x "$target"
      return 0
    else
      warn "$name partially downloaded (${actual_size} MB, expected ~${expected_mb}). Re-downloading."
      rm -f "$target"
    fi
  fi

  step "Downloading $name (${size} GB)"
  if curl --fail --location -# -o "$target" "$url"; then
    chmod +x "$target"
    ok "$name installed"
  else
    warn "Failed to download $name (you can retry by re-running install.sh)"
    rm -f "$target"
  fi
}

case "$model_choice" in
  1) download_llamafile "$LLAMA_3B_URL" "$LLAMA_3B_NAME" "$LLAMA_3B_SIZE_GB" ;;
  2) download_llamafile "$LLAMA_8B_URL" "$LLAMA_8B_NAME" "$LLAMA_8B_SIZE_GB" ;;
  3)
    download_llamafile "$LLAMA_3B_URL" "$LLAMA_3B_NAME" "$LLAMA_3B_SIZE_GB"
    download_llamafile "$LLAMA_8B_URL" "$LLAMA_8B_NAME" "$LLAMA_8B_SIZE_GB"
    ;;
  4) warn "Skipping LLM download: drop a .llamafile into $INSTALL_DIR/llm/ before launching" ;;
  *) die "Invalid choice: $model_choice" ;;
esac

# ---------------------------------------------------------------------------
# 4. Copy launchers + UI + shim
# ---------------------------------------------------------------------------

banner "4/5  Installing launchers, UI, and shim"

cp "$REPO_ROOT/Apocalypse.html"           "$INSTALL_DIR/Apocalypse.html"
cp "$REPO_ROOT/bin/kiwix_shim.py"         "$INSTALL_DIR/bin/kiwix_shim.py"
cp "$REPO_ROOT/launchers/Apocalypse.command" "$INSTALL_DIR/Apocalypse.command"
cp "$REPO_ROOT/launchers/Apocalypse.sh"      "$INSTALL_DIR/Apocalypse.sh"
cp "$REPO_ROOT/launchers/Apocalypse.bat"     "$INSTALL_DIR/Apocalypse.bat"
cp "$REPO_ROOT/scripts/download-zims.sh"  "$INSTALL_DIR/kiwix/download.sh"
cp "$REPO_ROOT/scripts/watchdog.sh"       "$INSTALL_DIR/kiwix/watchdog.sh"

# Strip any drive-specific paths from the macOS launcher (the original was
# hardcoded to /Volumes/Media/apocalypse). Replace with the relocatable form.
sed -i.bak 's|/Volumes/Media/apocalypse|"$DRIVE"|g' "$INSTALL_DIR/Apocalypse.command" || true
rm -f "$INSTALL_DIR/Apocalypse.command.bak" 2>/dev/null || true

# Make executables executable
chmod +x "$INSTALL_DIR/Apocalypse.command" \
         "$INSTALL_DIR/Apocalypse.sh" \
         "$INSTALL_DIR/bin/kiwix_shim.py" \
         "$INSTALL_DIR/kiwix/download.sh" \
         "$INSTALL_DIR/kiwix/watchdog.sh" 2>/dev/null || true

# Write a fresh README that points users at the launchers
cat > "$INSTALL_DIR/README.txt" <<'README'
APOCALYPSE OFFLINE KNOWLEDGE
============================

A self-contained, offline-first knowledge library: Wikipedia + Stack Exchange
+ Project Gutenberg + Khan Academy on a portable drive, with a local LLM
that answers questions using those sources as context.

LAUNCHING
---------
- macOS:    double-click Apocalypse.command
- Linux:    ./Apocalypse.sh
- Windows:  double-click Apocalypse.bat

The launcher boots two local services (the Kiwix shim on port 8888-8893,
and a llamafile LLM on port 8081-8083), then opens Apocalypse.html in
your browser.

REQUIREMENTS
------------
- macOS:    Python 3 (Xcode CLT: xcode-select --install)
- Linux:    glibc 2.31+ (Ubuntu 20.04 or newer)
- Windows:  No dependencies
- 8 GB RAM minimum (16 GB recommended for the 8B model)
- ~2.7-7.6 GB drive space for LLM(s); see below for ZIM sizes

ASKING QUESTIONS
----------------
Type any question. The system will:
  1. Ask the local LLM what Wikipedia article(s) might answer it
  2. Pull those articles from the offline ZIMs
  3. Score relevance, extract focused sections
  4. Generate a sourced answer with citations

Try:
  - "how do I build a bridge?"
  - "what causes earthquakes?"
  - "how do vaccines work?"
  - "tell me about the French Revolution"

TROUBLESHOOTING
---------------
- The browser tab doesn't connect: refresh after a few seconds. The LLM
  takes ~10 seconds to load; the shim takes ~30 seconds to index 13+ ZIMs.
- "Kiwix offline" status: check the terminal window for shim errors.
- "LLM offline" status: check .llm.log on the drive root.
- macOS says "developer cannot be verified": right-click → Open the first time.

LICENSE: MIT.  Source: https://github.com/hratterman/apocalypse-drive
README

ok "Installed launchers, UI, shim, and README"

# ---------------------------------------------------------------------------
# 5. Optionally download ZIMs
# ---------------------------------------------------------------------------

banner "5/5  Knowledge archives (ZIMs)"

cat <<EOF

Recommended ZIMs (latest sizes from kiwix.org):

  Wikipedia (English, no images, full text)     ~115 GB
  Wikipedia (English Medical only)              ~2.1 GB
  Project Gutenberg (literature)                ~107 GB
  Stack Overflow                                 ~80 GB
  Khan Academy                                  ~168 GB
  Wikivoyage                                     ~1 GB
  Wikibooks                                      ~6 GB
  Wikisource                                    ~18 GB
  Wikispecies                                   ~3.2 GB
  AskUbuntu / ServerFault / SuperUser           ~2.6+1.5+3.7 GB
  Math / Physics / Unix Stack Exchange          ~6.9+1.7+1.2 GB

The download script supports resume and checks integrity. Total for
"the works" is around 540 GB. You'll need a USB-C SSD or large external
drive. exFAT is the recommended filesystem for cross-OS portability.

EOF

read -rp "  Open the ZIM downloader now? [y/N] " ans
case "$ans" in
  [Yy]*)
    cd "$INSTALL_DIR/kiwix"
    bash ./download.sh
    ;;
  *)
    echo
    echo "  Skipped. To download later:"
    echo "    cd $INSTALL_DIR/kiwix && bash download.sh"
    ;;
esac

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo
echo "${GREEN}${BOLD}=== Installation complete ===${RESET}"
echo
echo "  To launch:"
case "$HOST_OS" in
  macos) echo "    open '$INSTALL_DIR/Apocalypse.command'" ;;
  linux) echo "    bash '$INSTALL_DIR/Apocalypse.sh'" ;;
esac
echo
echo "  Or just double-click the appropriate launcher in Finder/Files."
echo
echo "  ${DIM}Built by Henry Ratterman · henryratterman.com${RESET}"
echo "  ${DIM}github.com/hratterman/apocalypse-drive${RESET}"
echo
