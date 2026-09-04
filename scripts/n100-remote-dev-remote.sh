#!/usr/bin/env bash
set -euo pipefail

readonly DEFAULT_SOURCE_REPO="/mnt/c/personal-server"
readonly SESSION_NAME="personal-server-codex-dev"
readonly STATE_DIR="${HOME}/.local/state/personal-server/n100-dev"
readonly WORKTREE_DIR="${HOME}/.local/share/personal-server/n100-dev-worktree"
readonly TASK_FILE="${STATE_DIR}/task.txt"
readonly STATUS_FILE="${STATE_DIR}/status"
readonly LOG_FILE="${STATE_DIR}/codex.log"
readonly LAST_MESSAGE_FILE="${STATE_DIR}/last-message.txt"
readonly RUNNER_FILE="${STATE_DIR}/run-codex.sh"

command_name="${1:-}"
source_repo="${2:-$DEFAULT_SOURCE_REPO}"

die() {
  printf 'n100_remote_dev=FAIL\n' >&2
  printf '오류: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "필수 명령을 찾을 수 없음: $1"
}

verify_source_repo() {
  [[ -d "$source_repo" ]] || die "원본 저장소를 찾을 수 없음"
  git -C "$source_repo" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "원본 경로가 Git 저장소가 아님"
  git -C "$source_repo" rev-parse --verify --quiet 'main^{commit}' >/dev/null || die "원본 저장소의 main 로컬 참조를 찾을 수 없음"
}

preflight_checks() {
  require_command git
  require_command tmux
  require_command codex
  require_command gh
  verify_source_repo
  codex login status >/dev/null 2>&1 || die "Codex 인증 상태를 확인할 수 없음"
  gh auth status >/dev/null 2>&1 || die "GitHub 인증 상태를 확인할 수 없음"
}

preflight() {
  preflight_checks
  printf 'n100_remote_dev=PASS\n'
}

prepare_worktree() {
  if [[ -e "$WORKTREE_DIR" ]]; then
    [[ -d "$WORKTREE_DIR" ]] || die "전용 작업 경로가 디렉터리가 아님"
    git -C "$WORKTREE_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "전용 작업 경로가 Git worktree가 아님"
    local source_common_dir worktree_common_dir
    source_common_dir="$(git -C "$source_repo" rev-parse --path-format=absolute --git-common-dir)"
    worktree_common_dir="$(git -C "$WORKTREE_DIR" rev-parse --path-format=absolute --git-common-dir)"
    [[ "$worktree_common_dir" = "$source_common_dir" ]] || die "전용 worktree가 원본 저장소에 연결되어 있지 않음"
    local branch
    branch="$(git -C "$WORKTREE_DIR" branch --show-current)"
    [[ "$branch" =~ ^codex/n100-dev- ]] || die "전용 worktree 브랜치가 허용 범위가 아님"
    local worktree_status
    worktree_status="$(git -C "$WORKTREE_DIR" status --porcelain)"
    [[ -z "$worktree_status" ]] || die "전용 worktree에 커밋되지 않은 변경이 있음"
    return
  fi

  mkdir -p "$(dirname "$WORKTREE_DIR")"
  local branch_name
  branch_name="codex/n100-dev-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  git -C "$source_repo" worktree add --quiet -b "$branch_name" "$WORKTREE_DIR" main || die "전용 Git worktree 생성에 실패함"
}

prepare_state_dir() {
  umask 077
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
}

store_task_from_stdin() {
  local temporary_task size
  temporary_task="$(mktemp "${STATE_DIR}/task.XXXXXX")"
  trap 'rm -f -- "$temporary_task"' RETURN
  cat > "$temporary_task"
  size="$(wc -c < "$temporary_task" | tr -d '[:space:]')"
  [[ "$size" -le 65536 ]] || die "작업 지시문은 64 KiB 이하이어야 함"
  chmod 600 "$temporary_task"
  mv -f "$temporary_task" "$TASK_FILE"
  trap - RETURN
}

write_status() {
  local status="$1" started_at="$2" completed_at="$3" exit_code="$4"
  local temporary_status
  temporary_status="$(mktemp "${STATE_DIR}/status.XXXXXX")"
  {
    printf 'status=%s\n' "$status"
    printf 'started_at=%s\n' "$started_at"
    printf 'completed_at=%s\n' "$completed_at"
    printf 'exit_code=%s\n' "$exit_code"
    printf 'worktree=%s\n' "$WORKTREE_DIR"
  } > "$temporary_status"
  chmod 600 "$temporary_status"
  mv -f "$temporary_status" "$STATUS_FILE"
}

write_runner() {
  local temporary_runner
  temporary_runner="$(mktemp "${STATE_DIR}/runner.XXXXXX")"
  {
    printf '%s\n' '#!/usr/bin/env bash' 'set -u' 'umask 077'
    printf 'TASK_FILE=%q\n' "$TASK_FILE"
    printf 'STATUS_FILE=%q\n' "$STATUS_FILE"
    printf 'LOG_FILE=%q\n' "$LOG_FILE"
    printf 'LAST_MESSAGE_FILE=%q\n' "$LAST_MESSAGE_FILE"
    printf 'WORKTREE_DIR=%q\n' "$WORKTREE_DIR"
    cat <<'RUNNER'
write_status() {
  local status="$1" started_at="$2" completed_at="$3" exit_code="$4"
  local temporary_status="${STATUS_FILE}.tmp.$$"
  {
    printf 'status=%s\n' "$status"
    printf 'started_at=%s\n' "$started_at"
    printf 'completed_at=%s\n' "$completed_at"
    printf 'exit_code=%s\n' "$exit_code"
    printf 'worktree=%s\n' "$WORKTREE_DIR"
  } > "$temporary_status"
  chmod 600 "$temporary_status"
  mv -f "$temporary_status" "$STATUS_FILE"
}

started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
write_status running "$started_at" "" ""
{
  cat <<'PROMPT'
You may make local code changes, run tests, and create local commits only.
Do not push, create a pull request, merge, deploy, access or store credentials, or modify server, scheduler, K3s, Compose, Caddy, or tunnel resources.
If the task requires any prohibited action, stop and report that user approval is required.

User task:
PROMPT
  cat "$TASK_FILE"
} | codex exec -C "$WORKTREE_DIR" --sandbox workspace-write --output-last-message "$LAST_MESSAGE_FILE" - > "$LOG_FILE" 2>&1
exit_code=$?
completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
write_status completed "$started_at" "$completed_at" "$exit_code"
exit "$exit_code"
RUNNER
  } > "$temporary_runner"
  chmod 700 "$temporary_runner"
  mv -f "$temporary_runner" "$RUNNER_FILE"
}

start() {
  preflight_checks
  if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    die "이미 실행 중인 Codex 세션이 있음"
  fi
  prepare_worktree
  prepare_state_dir
  store_task_from_stdin
  write_runner
  tmux new-session -d -s "$SESSION_NAME" bash "$RUNNER_FILE" || die "tmux Codex 세션 시작에 실패함"
  printf 'n100_remote_dev=PASS\n'
  printf 'session=%s\n' "$SESSION_NAME"
  printf 'log=%s\n' "$LOG_FILE"
}

status() {
  require_command tmux
  if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    printf 'session=active\n'
  else
    printf 'session=inactive\n'
  fi
  if [[ -f "$STATUS_FILE" ]]; then
    cat "$STATUS_FILE"
  fi
  printf 'n100_remote_dev=PASS\n'
}

logs() {
  if [[ -f "$LOG_FILE" ]]; then
    tail -n 200 "$LOG_FILE"
  else
    printf '로그가 없음\n'
  fi
  printf 'n100_remote_dev=PASS\n'
}

stop() {
  require_command tmux
  if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    tmux kill-session -t "$SESSION_NAME"
  fi
  printf 'n100_remote_dev=PASS\n'
}

case "$command_name" in
  preflight)
    [[ "$#" -le 2 ]] || die "preflight는 추가 인자를 받지 않음"
    preflight
    ;;
  start)
    [[ "$#" -le 2 ]] || die "start는 원본 저장소 경로 외 인자를 받지 않음"
    start
    ;;
  status)
    [[ "$#" -le 2 ]] || die "status는 추가 인자를 받지 않음"
    status
    ;;
  logs)
    [[ "$#" -le 2 ]] || die "logs는 추가 인자를 받지 않음"
    logs
    ;;
  stop)
    [[ "$#" -le 2 ]] || die "stop은 추가 인자를 받지 않음"
    stop
    ;;
  *)
    die "사용법: $0 {preflight|start|status|logs|stop} [원본_저장소]"
    ;;
esac
