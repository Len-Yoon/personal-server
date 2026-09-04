# N100 무인 K3s 전환 실행기 설계

## 1. 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | N100 무인 K3s 전환 실행기 설계 |
| 작성일 | 2026-09-04 |
| 목적 | crawler-worker, youtube-memo, book-memo의 Compose→K3s 전환을 N100에서 안전하게 순차 실행하기 위한 신뢰 경계를 정의함 |
| 대상 | N100 Windows, Ubuntu-24.04 WSL, K3s 및 세 대상 서비스 |
| 제외 | Portal, Caddy 자체, crawler scheduler 코드, HomeOps, system-agent, car-care, Cloudflare Tunnel 설정 변경 |

## 2. 핵심 요약

무인 실행기는 `sudo` 비밀번호 또는 rclone 암호를 `.env`, Git, 명령행에 저장하지 않음. systemd root service가 root 권한으로 실행되고, 최소 rclone 설정·rclone 설정 암호·age identity는 `LoadCredentialEncrypted=`로만 실행 중 private credential directory에 제공됨.

root 실행기는 `/mnt/c/personal-server` 또는 사용자 홈의 rclone 설정을 실행 시점에 읽거나 실행하지 않음. 이 경로들은 Windows/일반 사용자에 의해 변경될 수 있으므로, root 실행 파일·정책·상태·승인된 release artifact는 native ext4의 root 소유 경로에만 둠.

전환 순서는 crawler-worker → youtube-memo → book-memo로 고정함. 한 서비스에서 실패하면 이후 서비스를 시작하지 않음. 공개 라우팅이 K3s를 실제로 제공하기 전에는 해당 서비스만 Compose로 복구할 수 있으나, 공개 라우팅 후에는 데이터 유실 방지를 위해 자동 Compose 복구를 금지하고 격리·Telegram 알림으로 종료함.

## 3. 승인된 범위와 경계

| 구분 | 처리 |
|---|---|
| 대상 서비스 | `crawler-worker`, `youtube-memo`, `book-memo`만 허용함 |
| 권한 | systemd unit은 root로 실행하여 sudo 입력을 제거함 |
| 비밀값 | rclone config, rclone config passphrase, age identity는 encrypted credential로만 제공함 |
| trusted runtime | `/usr/local/libexec/personal-server-transition-runner`, `/etc/personal-server-transition`, `/var/lib/personal-server-transition` |
| 비신뢰 경로 | `/mnt/c/personal-server`, `$HOME/.config/rclone`, 사용자 제공 manifest·환경변수·CLI 경로 |
| 배포 보호 | 세 서비스의 전환 marker를 배포·부팅·health 경로가 인식하여 Compose 재기동을 제외함 |
| 제외 | Portal·Caddy 자체·scheduler·HomeOps·system-agent·car-care·Cloudflare Tunnel은 변경하지 않음 |

배포·부팅·health 스크립트의 변경은 세 서비스의 marker 처리로만 제한함. 이 예외는 Docker와 K3s의 SQLite 동시 writer를 방지하기 위한 필수 안전 장치이며, 서비스 시작 목록의 다른 항목은 변경하지 않음.

## 4. 구성 요소

| 구성 요소 | 위치 | 책임 |
|---|---|---|
| 전환 정책 | `/etc/personal-server-transition/runner-policy.conf` | 고정된 서비스, namespace, PVC, image digest, 단계 및 시간 제한 allowlist 제공 |
| root 실행기 | `/usr/local/libexec/personal-server-transition-runner` | 정책 검증, lock, backup, writer 전환, journal 기록 수행 |
| systemd unit | `/etc/systemd/system/personal-server-transition.service` | root 실행, credential 전개, sandbox 및 실행 범위 제한 |
| 상태·lock·journal | `/var/lib/personal-server-transition/` | 서비스별 lock, idempotent run id, 단계별 비밀값 없는 상태 기록 |
| 저장소 선언 | `infra/k8s/...` | 서명·승인된 release에 포함될 선언형 서비스 계약만 제공 |

저장소의 manifest는 명령어를 포함하지 않는 선언형 데이터만 허용함. runner는 서비스명·namespace·PVC·NodePort·상태 값이 정책 allowlist와 정확히 일치할 때만 처리함. 알 수 없는 필드, 중복 서비스, path traversal, mutable image tag, 이미 처리된 run id는 fail-closed로 거부함.

## 5. Credential 처리

| Credential | 용도 | 제공 방식 |
|---|---|---|
| `rclone-config` | Drive remote 정의 | root-owned encrypted credential |
| `rclone-config-passphrase` | rclone config 복호화 | root-owned encrypted credential |
| `age-identity` | backup artifact 복호화 검증 | root-owned encrypted credential |
| `telegram-runtime` | 격리·실패 알림 | 기존 relay Secret 경로를 통해서만 사용 |

credential 값은 Git, `.env`, process argument, journal, 일반 임시 파일, 표준 출력에 기록하지 않음. rclone은 실행 중 credential directory의 config만 참조함. 사용자 홈 rclone config는 runner에서 참조하지 않음.

## 6. 서비스별 전환 흐름

1. runner는 서비스별 lock과 실행 이력을 확인하고, 중복·동시 실행을 거부함.
2. Compose가 유일 writer인지, K3s 대상 Deployment가 0 replica인지, 전환 marker가 Compose인지 확인함.
3. 암호화 remote backup 생성, 별도 경로 restore, 파일 manifest, SQLite `PRAGMA quick_check`를 검증함. 하나라도 실패하면 Compose writer를 유지함.
4. 대상 Compose 서비스만 중지하고 종료를 확인함.
5. 대상 데이터만 고유 PVC로 복사하고 source/destination digest를 비교함.
6. 고정 image digest의 K3s Deployment를 1 replica로 기동하고 readiness·internal health를 확인함.
7. 서비스별 marker를 K3s로 원자 갱신하고, 배포·부팅·health 경로가 Compose 재기동을 제외하는지 확인함.
8. 별도 승인된 라우팅 계약이 있는 경우에만 해당 upstream을 K3s로 전환하고 public health를 확인함.
9. 성공 journal을 기록한 뒤에만 다음 서비스를 시작함.

## 7. 실패·복구 계약

| 실패 시점 | 처리 |
|---|---|
| backup/restore 검증 전 | Compose writer를 변경하지 않고 실패 기록 |
| Compose 중지 후 K3s readiness 전 | K3s writer를 0으로 축소, 데이터 digest 검증 후 해당 Compose만 복구 |
| 공개 라우팅 전 검증 실패 | 해당 서비스의 Compose upstream·writer만 복구하고 다음 서비스 중단 |
| 공개 라우팅 후 실패 | 자동 Compose 복구 금지, K3s/Compose writer 격리·Telegram 경고·수동 복구 필요 |
| lock·정책·credential 오류 | 어떠한 Compose/K3s writer 변경 없이 fail-closed |

이미 성공한 이전 서비스는 이후 서비스 실패를 이유로 자동 rollback하지 않음.

## 8. 검증 계약

| 검증 항목 | 성공 기준 |
|---|---|
| 신뢰 경계 | root runner가 `/mnt/c`·사용자 홈의 코드·rclone config를 실행·참조하지 않음 |
| allowlist | 허용되지 않은 서비스·namespace·경로·option이 무동작 거부됨 |
| 비밀값 | credential 값이 process 목록, 환경 출력, journal, Git diff, 오류 로그에 없음 |
| lock | 같은 서비스의 두 번째 전환이 거부됨 |
| backup | 원격 backup·별도 restore·manifest·SQLite 검증이 모두 성공해야만 Compose 중지 가능 |
| writer | 각 단계에서 Docker와 K3s writer가 동시에 실행되지 않음 |
| reboot/deploy | 완료된 서비스가 재부팅·GitHub 배포 뒤 Compose로 재기동되지 않음 |
| rollback | public route 전 실패는 Compose health 복구, public route 후 실패는 격리·알림으로 종료 |
| systemd | unit syntax, credential mode, root-owned native ext4 권한이 검증됨 |

## 9. 확인 필요 사항

- Caddy와 Cloudflare Tunnel 공개 경로는 현재 Docker DNS·localhost 계약에 의존함. 세 서비스의 공개 전환은 별도 고정 라우팅 계약 승인 후에만 수행 필요.
- root-owned release artifact를 `/usr/local/libexec`와 `/etc`에 최초 설치하려면 N100에서 1회 관리자 권한 작업 필요.
- 실제 전환은 세 서비스 각각의 maintenance window와 현재 backup evidence를 필요로 함.

## 10. 후속 조치

1. 본 설계 검토 후 구현 계획 작성.
2. 테스트 우선으로 policy validator, systemd unit template, release installer, service marker 계약을 구현.
3. 독립 보안·rollback 검토와 관련 테스트를 통과한 뒤 PR 생성.
4. 병합 후 N100에서 root-owned release 설치와 encrypted credential seed를 1회 수행.
