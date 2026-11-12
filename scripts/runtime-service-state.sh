#!/usr/bin/env bash

RUNTIME_SERVICE_STATE_FILE=/var/lib/personal-server/k3s-runtime-services.state

_lock_n100_legacy_deploy() {
    local state_dir=$1 authority=$2 lock
    lock="$state_dir/ha-legacy-bootstrap.lock"
    [[ -d "$state_dir" && ! -L "$state_dir" && -x "$state_dir" && ! -L "$lock" &&
       ( ! -e "$lock" || -f "$lock" ) ]] || return 1
    # The deployment shell retains this open description through its rollback.
    exec 9>>"$lock" || return 1
    /usr/bin/python3 -I -B -c '
import fcntl, os, pathlib, stat, sys

fd, path = int(sys.argv[1]), pathlib.Path(sys.argv[2])
authority = pathlib.Path(sys.argv[3])
opened = os.fstat(fd)
current = path.lstat()
if not stat.S_ISREG(current.st_mode) or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
    sys.exit(1)
fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
def owned(directory):
    with os.scandir(directory) as entries:
        return any(item.name.casefold() == "ha-controller-owner.json" for item in entries)

if owned(path.parent):
    sys.exit(1)
with os.scandir(authority.parent) as entries:
    matches = [item for item in entries if item.name.casefold() == authority.name.casefold()]
if not matches:
    sys.exit(0)
if len(matches) != 1 or not matches[0].is_dir(follow_symlinks=False):
    sys.exit(1)
if owned(matches[0].path):
    sys.exit(1)
' 9 "$lock" "$authority" 2>/dev/null || {
        exec 9>&-
        return 1
    }
}

require_n100_legacy_deploy_admission() {
    local project_root=$1 kernel kernel_release
    kernel=$(/usr/bin/uname -s) || return 1
    [[ "$kernel" == Linux ]] || return 0
    IFS= read -r kernel_release </proc/sys/kernel/osrelease || return 1
    [[ "${kernel_release,,}" == *microsoft* ]] || return 0
    if [[ "$project_root" != /mnt/c/personal-server ]] ||
        ! _lock_n100_legacy_deploy "$project_root/data" /mnt/c/ProgramData/N100HA-Cutover; then
        printf '%s\n' 'n100_legacy_deploy_admission_closed' >&2
        return 1
    fi
}

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
    [[ $# -ge 1 && $# -le 2 && -n ${1:-} ]] || return 1
    if [[ $# -eq 2 && ${2:-} != --require-explicit ]]; then
        return 1
    fi
    if [[ $# -eq 2 ]]; then
        python3 "${BASH_SOURCE[0]%/*}/runtime-service-state-reader.py" "$RUNTIME_SERVICE_STATE_FILE" "$2"
    else
        python3 "${BASH_SOURCE[0]%/*}/runtime-service-state-reader.py" "$RUNTIME_SERVICE_STATE_FILE"
    fi
}

# Explicit test-only seam; production callers must use load_service_runtime_state.
load_service_runtime_state_test_fixture() {
    [[ $# -eq 1 && -n ${1:-} ]] || return 1
    _parse_runtime_service_state_file "$1/data/k3s-runtime-services.state"
}
