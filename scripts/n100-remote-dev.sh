#!/usr/bin/env bash
set -euo pipefail

readonly DEFAULT_TARGET="n100-codex"
readonly DEFAULT_DISTRO="Ubuntu-24.04"
readonly DEFAULT_REPO="/mnt/c/personal-server"

command_name="${1:-}"
case "$command_name" in
  keygen|preflight|start|status|logs|stop) ;;
  *)
    printf '사용법: %s {keygen|preflight|start|status|logs|stop} [옵션]\n' "$0" >&2
    exit 2
    ;;
esac

target="${N100_SSH_TARGET:-$DEFAULT_TARGET}"
distro="${N100_WSL_DISTRO:-$DEFAULT_DISTRO}"
repo="${N100_REMOTE_REPO:-$DEFAULT_REPO}"
key_path="${N100_SSH_KEY:-${HOME}/.ssh/id_ed25519_n100}"

die() { printf '오류: %s\n' "$*" >&2; exit 1; }

validate_key_path() {
  [[ "$key_path" = /* ]] || die "SSH 키 경로는 절대 경로여야 함: $key_path"
  if [[ -e "$key_path" ]]; then
    [[ -f "$key_path" ]] || die "SSH 키가 일반 파일이 아님: $key_path"
    local mode
    mode="$(stat -f '%Lp' "$key_path" 2>/dev/null || stat -c '%a' "$key_path" 2>/dev/null || true)"
    [[ "$mode" = 600 ]] || die "SSH 개인키 권한은 0600이어야 함: $key_path (현재 $mode)"
  fi
}

create_key_if_missing() {
  validate_key_path
  mkdir -p "$(dirname "$key_path")"
  chmod 700 "$(dirname "$key_path")"
  if [[ ! -e "$key_path" ]]; then
    command -v ssh-keygen >/dev/null 2>&1 || die "ssh-keygen을 찾을 수 없음"
    umask 077
    ssh-keygen -q -t ed25519 -N '' -f "$key_path"
    chmod 600 "$key_path"
  fi
  local public_key="${key_path}.pub"
  if [[ ! -f "$public_key" ]]; then
    ssh-keygen -y -f "$key_path" > "$public_key" || die "개인키에서 공개키를 생성할 수 없음"
    chmod 644 "$public_key"
  fi
  printf '공개키를 Windows의 administrators_authorized_keys에 등록 필요:\n'
  cat "$public_key"
}

validate_remote_value() {
  local name="$1" value="$2"
  [[ "$value" =~ ^[A-Za-z0-9_./:@+-]+$ ]] || die "$name에 허용되지 않는 문자가 있음"
}

run_remote() {
  validate_key_path
  [[ -f "$key_path" ]] || die "SSH 개인키가 없음. keygen을 먼저 실행 필요: $key_path"
  command -v ssh >/dev/null 2>&1 || die "ssh를 찾을 수 없음"
  validate_remote_value "WSL 배포판" "$distro"
  validate_remote_value "원격 저장소" "$repo"

  local remote_script="${repo%/}/scripts/n100-remote-dev-remote.sh"
  local remote_command
  # Windows OpenSSH invokes the remote command through cmd.exe; avoid POSIX
  # single quotes and pass only validated, whitespace-free values.
  remote_command="wsl.exe -d $distro -- bash $remote_script $command_name $repo"
  local -a ssh_args
  ssh_args=(ssh -i "$key_path" -o BatchMode=yes -o PasswordAuthentication=no -o StrictHostKeyChecking=yes "$target" "$remote_command")
  if [[ "$command_name" = start ]]; then
    cat "$task_file" | "${ssh_args[@]}"
  else
    "${ssh_args[@]}"
  fi
}

if [[ "$command_name" = keygen ]]; then
  [[ "$#" -eq 1 ]] || die "keygen은 추가 인자를 받지 않음"
  create_key_if_missing
  exit 0
fi

task_file=""
if [[ "$command_name" = start ]]; then
  [[ "${2:-}" = --task-file && -n "${3:-}" && "$#" -eq 3 ]] || die "start 사용법: start --task-file FILE"
  task_file="$3"
  [[ "$task_file" = /* ]] || die "작업 파일은 절대 경로여야 함"
  [[ ! -L "$task_file" ]] || die "작업 파일은 심볼릭 링크가 아니어야 함"
  [[ -f "$task_file" ]] || die "작업 파일이 일반 파일이 아님: $task_file"
  local_size="$(wc -c < "$task_file" | tr -d '[:space:]')"
  [[ "$local_size" -le 65536 ]] || die "작업 파일은 64 KiB 이하이어야 함"
else
  [[ "$#" -eq 1 ]] || die "$command_name은 추가 인자를 받지 않음"
fi

run_remote
