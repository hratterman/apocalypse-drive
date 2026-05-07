#!/bin/bash
# Apocalypse launcher — macOS
# Uses a Python+libzim HTTP shim instead of kiwix-serve, because kiwix-serve
# throws MMapException on macOS+exFAT. The shim is feature-equivalent for
# what the RAG UI needs: /search returns hits, /content/<book>/<path> serves
# the entry. Linux/Windows launchers still use real kiwix-serve.

set -e
DRIVE="$(cd "$(dirname "$0")" && pwd)"
cd "$DRIVE"

# Pick model based on RAM AND on whether the drive is currently busy with
# downloads (loading an 8B llamafile during heavy disk I/O can take >5 minutes).
TOTAL_RAM_BYTES=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
TOTAL_RAM_GB=$((TOTAL_RAM_BYTES / 1024 / 1024 / 1024))

DOWNLOADS_ACTIVE=0
if pgrep -f "curl.*Volumes/Media/apocalypse/kiwix" >/dev/null 2>&1; then
  DOWNLOADS_ACTIVE=1
fi

if [ "$FORCE_3B" = "1" ] || [ "$DOWNLOADS_ACTIVE" = "1" ]; then
  if [ -f "$DRIVE/llm/Llama-3.2-3B-Instruct.Q6_K.llamafile" ]; then
    MODEL="$DRIVE/llm/Llama-3.2-3B-Instruct.Q6_K.llamafile"
    MODEL_LABEL="Llama 3.2 3B (3B forced — downloads in progress or FORCE_3B=1)"
  else
    echo "ERROR: 3B llamafile missing"; exit 1
  fi
elif [ "$TOTAL_RAM_GB" -ge 12 ] && [ -f "$DRIVE/llm/Meta-Llama-3.1-8B-Instruct.Q4_K_M.llamafile" ]; then
  MODEL="$DRIVE/llm/Meta-Llama-3.1-8B-Instruct.Q4_K_M.llamafile"
  MODEL_LABEL="Llama 3.1 8B"
elif [ -f "$DRIVE/llm/Llama-3.2-3B-Instruct.Q6_K.llamafile" ]; then
  MODEL="$DRIVE/llm/Llama-3.2-3B-Instruct.Q6_K.llamafile"
  MODEL_LABEL="Llama 3.2 3B"
else
  echo "ERROR: No llamafile model in $DRIVE/llm/"
  exit 1
fi

# Port-finder: returns first free port from list
pick_port() {
  for p in "$@"; do
    if ! lsof -iTCP:$p -sTCP:LISTEN >/dev/null 2>&1; then
      echo "$p"
      return 0
    fi
  done
  echo ""
  return 1
}

KIWIX_PORT=$(pick_port 8888 8890 8891 8892 8893 18888)
LLM_PORT=$(pick_port 8081 8082 8083 18081)

if [ -z "$KIWIX_PORT" ] || [ -z "$LLM_PORT" ]; then
  echo "ERROR: Could not find free ports for Kiwix and/or LLM."
  exit 1
fi

echo "==================================="
echo "  Apocalypse Offline Knowledge"
echo "==================================="
echo "Drive:      $DRIVE"
echo "RAM:        ${TOTAL_RAM_GB}GB"
echo "Model:      $MODEL_LABEL"
echo "Kiwix port: $KIWIX_PORT"
echo "LLM port:   $LLM_PORT"
echo ""

# Pick a Python interpreter. Preference order:
#   1) /usr/bin/python3 (Apple system Python — always present on macOS, and
#      is the one we provisioned libzim into during testing)
#   2) any python3 on PATH
# This avoids picking up a Homebrew Python that doesn't have libzim.
if [ -x /usr/bin/python3 ]; then
  PYTHON=/usr/bin/python3
else
  PYTHON=$(command -v python3)
fi
if [ -z "$PYTHON" ]; then
  echo "ERROR: python3 not found. Install Xcode Command Line Tools: xcode-select --install"
  exit 1
fi
echo "Python:     $PYTHON ($($PYTHON --version 2>&1))"

# 1. Make sure libzim is installed into the chosen Python's user site-packages
if ! "$PYTHON" -c "import libzim" 2>/dev/null; then
  echo "First-run: installing libzim Python binding ..."
  # macOS Python 3.11+ enforces PEP 668 — need --break-system-packages with --user
  # to install into the user's site-packages without touching system Python.
  if ! "$PYTHON" -m pip install --user --quiet libzim 2>/dev/null; then
    if ! "$PYTHON" -m pip install --user --break-system-packages --quiet libzim 2>/dev/null; then
      echo "ERROR: could not install libzim."
      echo "Try one of:"
      echo "  $PYTHON -m pip install --user --break-system-packages libzim"
      echo "  pipx install libzim"
      exit 1
    fi
  fi
  echo "  libzim installed."
fi

# 2. Boot the Kiwix shim
echo "Starting Kiwix shim at http://localhost:${KIWIX_PORT} ..."
"$PYTHON" -u "$DRIVE/bin/kiwix_shim.py" --port "$KIWIX_PORT" --zim-dir "$DRIVE/kiwix/zim" \
  > "$DRIVE/.kiwix-shim.log" 2>&1 &
SHIM_PID=$!
echo "  PID $SHIM_PID"

# Wait for shim
echo -n "Waiting for shim "
for i in $(seq 1 30); do
  K=$(curl -sS -o /dev/null -w "%{http_code}" http://localhost:${KIWIX_PORT}/ 2>/dev/null || echo 0)
  if [ "$K" = "200" ]; then echo " ready"; break; fi
  # Detect shim died
  if ! kill -0 "$SHIM_PID" 2>/dev/null; then
    echo ""
    echo "ERROR: Kiwix shim died. Last log lines:"
    tail -20 "$DRIVE/.kiwix-shim.log"
    exit 1
  fi
  echo -n "."
  sleep 1
done
echo ""

# 3. Boot llamafile
echo "Starting LLM server at http://localhost:${LLM_PORT} ..."
chmod +x "$MODEL" 2>/dev/null || true
"$MODEL" --server --nobrowser --port "$LLM_PORT" --host 127.0.0.1 -ngl 999 \
  > "$DRIVE/.llm.log" 2>&1 &
LLM_PID=$!
echo "  PID $LLM_PID"

echo -n "Waiting for LLM to load model "
for i in $(seq 1 60); do
  L=$(curl -sS -o /dev/null -w "%{http_code}" http://localhost:${LLM_PORT}/v1/models 2>/dev/null || echo 0)
  if [ "$L" = "200" ]; then echo " ready"; break; fi
  echo -n "."
  sleep 2
done
echo ""

# 4. Open the unified RAG UI with the chosen ports baked in via URL params
echo "Opening browser ..."
open "file://${DRIVE}/Apocalypse.html?kiwix=${KIWIX_PORT}&llm=${LLM_PORT}"

echo ""
echo "Press Ctrl+C in this window to stop both servers."
echo ""

cleanup() {
  echo ""
  echo "Stopping ..."
  [ -n "$LLM_PID" ] && kill "$LLM_PID" 2>/dev/null || true
  [ -n "$SHIM_PID" ] && kill "$SHIM_PID" 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM
wait
