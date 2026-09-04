#!/usr/bin/env bash

RUNTIME_SERVICE_STATE_FILE=/var/lib/personal-server/k3s-runtime-services.state

_runtime_trusted_directory() {
    local directory=$1 owner_uid mode
    [[ -d "$directory" && ! -L "$directory" && -r "$directory" && -x "$directory" ]] || return 1
    owner_uid=$(stat -f '%u' "$directory" 2>/dev/null) || owner_uid=$(stat -c '%u' "$directory" 2>/dev/null) || return 1
    [[ $owner_uid -eq 0 ]] || return 1
    mode=$(stat -f '%Lp' "$directory" 2>/dev/null) || mode=$(stat -c '%a' "$directory" 2>/dev/null) || return 1
    (( (0$mode & 022) == 0 )) || return 1
}

_parse_runtime_service_state_file() {
    local state_file=$1 crawler_worker=compose youtube_memo=compose book_memo=compose
    local crawler_worker_seen=0 youtube_memo_seen=0 book_memo_seen=0 row
    local -a rows
    [[ -L "$state_file" ]] && return 1
    if [[ -e "$state_file" ]]; then
        [[ -f "$state_file" && -r "$state_file" ]] || return 1
        if ! tr -d '\000' < "$state_file" | cmp -s - "$state_file"; then return 1; fi
        while IFS= read -r row || [[ -n "$row" ]]; do rows+=("$row"); done < "$state_file"
        for row in "${rows[@]}"; do
            case "$row" in
                crawler-worker=compose) [[ $crawler_worker_seen -eq 0 ]] || return 1; crawler_worker_seen=1 ;;
                crawler-worker=k3s) [[ $crawler_worker_seen -eq 0 ]] || return 1; crawler_worker=k3s; crawler_worker_seen=1 ;;
                youtube-memo=compose) [[ $youtube_memo_seen -eq 0 ]] || return 1; youtube_memo_seen=1 ;;
                youtube-memo=k3s) [[ $youtube_memo_seen -eq 0 ]] || return 1; youtube_memo=k3s; youtube_memo_seen=1 ;;
                book-memo=compose) [[ $book_memo_seen -eq 0 ]] || return 1; book_memo_seen=1 ;;
                book-memo=k3s) [[ $book_memo_seen -eq 0 ]] || return 1; book_memo=k3s; book_memo_seen=1 ;;
                *) return 1 ;;
            esac
        done
    fi
    printf 'crawler-worker=%s\nyoutube-memo=%s\nbook-memo=%s\n' "$crawler_worker" "$youtube_memo" "$book_memo"
}

load_service_runtime_state() {
    [[ $# -eq 1 && -n ${1:-} ]] || return 1
    python3 "${BASH_SOURCE[0]%/*}/runtime-service-state-reader.py" "$RUNTIME_SERVICE_STATE_FILE"
}

# Explicit test-only seam; production callers must use load_service_runtime_state.
load_service_runtime_state_test_fixture() {
    [[ $# -eq 1 && -n ${1:-} ]] || return 1
    _parse_runtime_service_state_file "$1/data/k3s-runtime-services.state"
}
