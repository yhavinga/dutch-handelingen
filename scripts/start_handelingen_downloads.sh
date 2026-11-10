#!/bin/bash
# Start/resume Handelingen downloads in screen sessions
# Usage: ./scripts/start_handelingen_downloads.sh [historical|modern|both]
#
# The downloads are idempotent - they skip already downloaded files.
# You can start/stop/resume anytime.
#
# Monitor progress:
#   screen -r handelingen-historical
#   screen -r handelingen-modern
#   (Detach with Ctrl-A, D)
#
# Check file counts:
#   find data/handelingen/historical -name '*.txt' | wc -l
#   find data/handelingen/modern -name '*.xml' | wc -l

set -e
cd "$(dirname "$0")/.."

VENV="${VENV:-./.venv/bin/python}"
SCRIPT="scripts/download_handelingen.py"
OUTPUT_DIR="./data/handelingen"

# Conservative rate limiting to avoid 429s
CONCURRENCY=2
DELAY=1

start_historical() {
    SESSION="handelingen-historical"
    if screen -list | grep -q "$SESSION"; then
        echo "Session '$SESSION' already running. Attach with: screen -r $SESSION"
    else
        echo "Starting historical OCR download in screen session '$SESSION'..."
        screen -dmS "$SESSION" "$VENV" "$SCRIPT" \
            -o "$OUTPUT_DIR" \
            -s historical \
            --formats ocr \
            -c "$CONCURRENCY" \
            --delay "$DELAY"
        echo "Started. Attach with: screen -r $SESSION"
    fi
}

start_modern() {
    SESSION="handelingen-modern"
    if screen -list | grep -q "$SESSION"; then
        echo "Session '$SESSION' already running. Attach with: screen -r $SESSION"
    else
        echo "Starting modern XML download in screen session '$SESSION'..."
        screen -dmS "$SESSION" "$VENV" "$SCRIPT" \
            -o "$OUTPUT_DIR" \
            -s modern \
            --formats xml \
            -c "$CONCURRENCY" \
            --delay "$DELAY"
        echo "Started. Attach with: screen -r $SESSION"
    fi
}

show_status() {
    echo ""
    echo "=== Download Status ==="
    echo "Historical .txt files: $(find data/handelingen/historical -name '*.txt' 2>/dev/null | wc -l | tr -d ' ') / 377040"
    echo "Modern .xml files:     $(find data/handelingen/modern -name '*.xml' 2>/dev/null | wc -l | tr -d ' ') / $(grep -c '"doc_id"' data/handelingen/index_modern_sru.json 2>/dev/null || echo '? (no SRU index yet)')"
    echo ""
    echo "Screen sessions:"
    screen -list 2>/dev/null | grep handelingen || echo "  (none running)"
}

case "${1:-both}" in
    historical)
        start_historical
        ;;
    modern)
        start_modern
        ;;
    both)
        start_historical
        start_modern
        ;;
    status)
        show_status
        ;;
    stop)
        echo "Stopping screen sessions..."
        screen -S handelingen-historical -X quit 2>/dev/null || true
        screen -S handelingen-modern -X quit 2>/dev/null || true
        echo "Stopped."
        ;;
    *)
        echo "Usage: $0 [historical|modern|both|status|stop]"
        exit 1
        ;;
esac

show_status
