#!/bin/bash
# LDPB Swing Screener — Finder 더블클릭 → 터미널 기동 → Chrome.

cd "$(dirname "$0")" || exit 1
ulimit -n 10240

export STREAMLIT_SERVER_FILE_WATCHER_TYPE=none
export STREAMLIT_SERVER_HEADLESS=true
export STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

STREAMLIT_BIN="./.venv/bin/streamlit"
URL="http://localhost:8502"

cleanup() {
    echo ""
    echo "[LDPB] 종료. 주문은 나가지 않는다. 창을 닫아도 된다."
}
trap cleanup EXIT INT TERM

# 좀비 프로세스(FD 고갈 상태)가 8502를 물고 있으면 제거
if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -nP -tiTCP:8502 -sTCP:LISTEN 2>/dev/null || true)"
    if [ -n "$pids" ]; then
        echo "[LDPB] 기존 8502 프로세스 종료: $pids"
        kill $pids 2>/dev/null || true
        sleep 0.4
        kill -9 $pids 2>/dev/null || true
    fi
fi

if [ ! -x "$STREAMLIT_BIN" ]; then
    echo "[LDPB] .venv 없음. 터미널에서 한 번만:"
    echo "  cd \"$(pwd)\""
    echo "  python3 -m venv .venv"
    echo "  source .venv/bin/activate"
    echo "  pip install -r requirements.txt"
    echo "  chmod +x run_bot.command"
    read -r -p "엔터를 누르면 창이 닫힙니다..." _
    exit 1
fi

open_chrome() {
    i=0
    while [ "$i" -lt 40 ]; do
        if /usr/bin/curl -sf "http://127.0.0.1:8502/_stcore/health" >/dev/null 2>&1; then
            if [ -d "/Applications/Google Chrome.app" ]; then
                /usr/bin/open -a "Google Chrome" "$URL"
            else
                /usr/bin/open "$URL"
            fi
            return 0
        fi
        sleep 0.25
        i=$((i + 1))
    done
}

open_chrome &

"$STREAMLIT_BIN" run app.py --server.port 8502 --server.headless true --server.fileWatcherType none
