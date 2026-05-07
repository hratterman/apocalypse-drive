#!/bin/bash
# Apocalypse launcher — Linux
# Starts kiwix-serve + llamafile, opens browser to the unified RAG UI.

set -e
DRIVE="$(cd "$(dirname "$0")" && pwd)"
cd "$DRIVE"

ARCH=$(uname -m)
case "$ARCH" in
  x86_64) PLATFORM="linux-x86_64" ;;
  aarch64|arm64) PLATFORM="linux-aarch64" ;;
  *) echo "ERROR: unsupported Linux arch $ARCH"; exit 1 ;;
esac

KIWIX_BIN="$DRIVE/bin/$PLATFORM/kiwix-serve"
ZIM_DIR="$DRIVE/kiwix/zim"

if [ ! -x "$KIWIX_BIN" ]; then
  echo "ERROR: kiwix-serve not found at $KIWIX_BIN"
  exit 1
fi

TOTAL_RAM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
TOTAL_RAM_GB=$((TOTAL_RAM_KB / 1024 / 1024))

if [ "$TOTAL_RAM_GB" -ge 12 ] && [ -f "$DRIVE/llm/Meta-Llama-3.1-8B-Instruct.Q4_K_M.llamafile" ]; then
  MODEL="$DRIVE/llm/Meta-Llama-3.1-8B-Instruct.Q4_K_M.llamafile"
  MODEL_LABEL="Llama 3.1 8B"
elif [ -f "$DRIVE/llm/Llama-3.2-3B-Instruct.Q6_K.llamafile" ]; then
  MODEL="$DRIVE/llm/Llama-3.2-3B-Instruct.Q6_K.llamafile"
  MODEL_LABEL="Llama 3.2 3B"
else
  echo "ERROR: No llamafile model in $DRIVE/llm/"
  exit 1
fi

echo "==================================="
echo "  Apocalypse Offline Knowledge"
echo "==================================="
echo "Drive: $DRIVE"
echo "RAM:   ${TOTAL_RAM_GB}GB"
echo "Model: $MODEL_LABEL"
echo ""

ZIMS=("$ZIM_DIR"/*.zim)
if [ ! -e "${ZIMS[0]}" ]; then
  echo "ERROR: no ZIM files in $ZIM_DIR"
  exit 1
fi
echo "ZIMs:  ${#ZIMS[@]} found"
echo ""

echo "Starting kiwix-serve at http://localhost:8888 ..."
"$KIWIX_BIN" --port=8888 "${ZIMS[@]}" > "$DRIVE/.kiwix.log" 2>&1 &
KIWIX_PID=$!
echo "  PID $KIWIX_PID"

echo "Starting LLM server at http://localhost:8081 ..."
chmod +x "$MODEL" 2>/dev/null || true
"$MODEL" --server --nobrowser --port 8081 --host 127.0.0.1 -ngl 999 > "$DRIVE/.llm.log" 2>&1 &
LLM_PID=$!
echo "  PID $LLM_PID"

echo -n "Waiting for services "
for i in $(seq 1 60); do
  K=$(curl -sS -o /dev/null -w "%{http_code}" http://localhost:8888/ 2>/dev/null || echo 0)
  L=$(curl -sS -o /dev/null -w "%{http_code}" http://localhost:8081/v1/models 2>/dev/null || echo 0)
  if { [ "$K" = "200" ] || [ "$K" = "302" ]; } && [ "$L" = "200" ]; then
    echo " ready"
    break
  fi
  echo -n "."
  sleep 2
done
echo ""

echo "Opening browser ..."
xdg-open "$DRIVE/Apocalypse.html" 2>/dev/null || \
  firefox "$DRIVE/Apocalypse.html" 2>/dev/null || \
  google-chrome "$DRIVE/Apocalypse.html" 2>/dev/null || \
  echo "Open $DRIVE/Apocalypse.html manually."

echo ""
echo "Press Ctrl+C in this window to stop everything."
echo ""

cleanup() {
  echo ""
  echo "Stopping services ..."
  [ -n "$LLM_PID" ] && kill "$LLM_PID" 2>/dev/null || true
  [ -n "$KIWIX_PID" ] && kill "$KIWIX_PID" 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM

wait
