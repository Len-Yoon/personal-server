# Portal PVC 백업 자동화 설계

## 목적

K3s Portal PVC 백업·암호화 원격 업로드·복원 검증을 N100에서 하루 한 번 자동 실행하고, 결과를 Telegram으로 한글 보고함.

## 범위와 제외 범위

| 구분 | 내용 |
|---|---|
| 포함 | N100 `systemd --user` service/timer, rclone config·passphrase의 host-bound 암호화 credential, 백업 상태 ConfigMap, 설치·상태 확인·제거 도구 |
| 포함 | 기존 `portal-pvc-backup-verify.sh --go` 실행과 Portal writer 복구 확인 |
| 제외 | Portal 앱 코드, 일반 서비스 스케줄러, Docker/서버 기동 구성, Telegram·Alertmanager 기존 비밀값의 복제 또는 출력 |
| 선택하지 않음 | K3s CronJob. rclone 암호를 Kubernetes Secret에 복제해야 하므로 host 암호화 credential 방식보다 안전하지 않음 |

## 구성

```text
systemd user timer (N100)
  -> systemd credentials: encrypted rclone config + passphrase
  -> portal-pvc-backup-verify.sh --go
  -> fixed backup-status ConfigMap
  -> sre-telegram-relay reads ConfigMap
  -> Telegram
```

systemd timer만 사용함. CronJob은 허용 예외에는 포함되지만 host 암호화 credential을 쓸 수 없어 rclone 자격 증명을 Kubernetes Secret으로 복제해야 하므로 선택하지 않음.

## 실행 흐름

1. timer는 하루 한 번 service를 시작함. Persistent timer로 인해 N100이 꺼져 있던 시간의 한 번의 실행만 복구함.
2. service는 encrypted `rclone-config`와 `rclone-config-passphrase` credential을 load함. backup tool은 credential 파일 경로만 받고 rclone의 `--password-command`로 passphrase를 실행 중에만 전달함.
3. tool의 고정 결과를 `completed`, `unchanged`, `failed`, `restore_failed`로 분류함. 중단·lock·원격 인증·업로드·복원·Portal readiness 실패는 `failed` 또는 `restore_failed`임.
4. service는 이름이 고정된 `monitoring` ConfigMap에 허용 status, run ID, UTC 완료 시각, 안전한 stage만 기록함. relay는 해당 ConfigMap만 읽고 bounded best-effort dedup으로 run ID 중복을 줄여 보고함.
5. relay는 Telegram으로 아래 형식만 전송함.

```text
[백업 완료]
상태: 암호화 백업과 복원 검증을 완료했습니다.
대상: Portal 데이터
```

`unchanged`는 `[백업 확인] 변경 없음`, 실패는 `[백업 실패]`, writer/readiness 또는 restore 검증 실패는 `[복원 검증 실패]`로 구분함.

## 안전 계약

- sudo 비밀번호를 저장하지 않으며 `sudo -n k3s`만 사용함.
- rclone 설정 암호와 encrypted rclone config은 사용자 입력/원본에서 `systemd-creds encrypt --with-key=host`로 직접 전달하며 평문 파일·Git·로그·환경 변수·환경 설정 파일에 저장하지 않음.
- relay는 backup-status ConfigMap에 대한 읽기 권한만 추가하며, report API·별도 token·외부 listener를 만들지 않음.
- Telegram 보고 전달은 at-least-once로 보장함. Telegram이 수락한 뒤 성공 상태를 ConfigMap에 기록하며, 그 쓰기 또는 relay가 처리 중 crash가 발생하면 재시작 후 동일 run ID가 재전송될 수 있음. bounded best-effort dedup은 중복을 줄이지만 exactly-once를 보장하지 않음.
- report 실패는 백업 artifact/evidence를 지우지 않지만 service 실패로 기록하고 다음 실행에서 재시도됨.
- 기존 backup tool의 cleanup은 실패·취소에도 Portal replica 및 health 복구를 우선함.
- WSL이 완전히 종료된 동안에는 실행할 수 없음. `loginctl enable-linger window`와 timer의 `Persistent=true`로 다음 기동 시 한 번만 보완 실행함.
- `systemd-creds` host key는 디스크 보관 암호화 용도임. 동일 N100 사용자 또는 host root가 침해된 경우까지 방어하지 않음.

## 검증

| 대상 | 성공 기준 |
|---|---|
| unit tests | 허용/거부된 ConfigMap report, 한글 메시지, credential 경계, runner 결과 분류를 검증함 |
| static checks | shell syntax, systemd unit 검증, manifest 구조 검증을 통과함 |
| N100 preflight | `systemd-creds`, `sudo -n k3s`, rclone, relay health, systemd user manager를 확인함 |
| N100 live run | timer 대신 one-shot service 실행으로 upload 또는 unchanged, restore/Portal health, Telegram 보고를 확인함 |
| rollback | timer disable, credential 제거, backup-status ConfigMap 제거, relay rollout 확인으로 자동화를 제거함 |

## 시간 기준

내부 run ID와 evidence 시각은 UTC ISO 8601을 사용함. 사용자 Telegram 메시지에는 시각 원문을 표시하지 않음.
