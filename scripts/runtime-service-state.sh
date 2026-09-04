#!/usr/bin/env bash

# Load the approved service runtime state without interpreting state text.
load_service_runtime_state() {
    if [[ $# -ne 1 || -z ${1:-} ]]; then
        return 1
    fi

    local project_root=$1
    local data_dir="$project_root/data"
    local state_file="$project_root/data/k3s-runtime-services.state"
    local crawler_worker=compose
    local youtube_memo=compose
    local book_memo=compose
    local crawler_worker_seen=0
    local youtube_memo_seen=0
    local book_memo_seen=0
    local row
    local -a rows

    if [[ -e "$data_dir" || -L "$data_dir" ]]; then
        [[ -d "$data_dir" && -r "$data_dir" && -x "$data_dir" ]] || return 1
    fi

    if [[ -e "$state_file" || -L "$state_file" ]]; then
        [[ -f "$state_file" && -r "$state_file" ]] || return 1
        if ! tr -d '\000' < "$state_file" | cmp -s - "$state_file"; then
            return 1
        fi
        while IFS= read -r row || [[ -n "$row" ]]; do
            rows+=("$row")
        done < "$state_file"
        for row in "${rows[@]}"; do
            case "$row" in
                crawler-worker=compose)
                    [[ $crawler_worker_seen -eq 0 ]] || return 1
                    crawler_worker=compose
                    crawler_worker_seen=1
                    ;;
                crawler-worker=k3s)
                    [[ $crawler_worker_seen -eq 0 ]] || return 1
                    crawler_worker=k3s
                    crawler_worker_seen=1
                    ;;
                youtube-memo=compose)
                    [[ $youtube_memo_seen -eq 0 ]] || return 1
                    youtube_memo=compose
                    youtube_memo_seen=1
                    ;;
                youtube-memo=k3s)
                    [[ $youtube_memo_seen -eq 0 ]] || return 1
                    youtube_memo=k3s
                    youtube_memo_seen=1
                    ;;
                book-memo=compose)
                    [[ $book_memo_seen -eq 0 ]] || return 1
                    book_memo=compose
                    book_memo_seen=1
                    ;;
                book-memo=k3s)
                    [[ $book_memo_seen -eq 0 ]] || return 1
                    book_memo=k3s
                    book_memo_seen=1
                    ;;
                *)
                    return 1
                    ;;
            esac
        done
    fi

    printf 'crawler-worker=%s\nyoutube-memo=%s\nbook-memo=%s\n' \
        "$crawler_worker" "$youtube_memo" "$book_memo"
}
