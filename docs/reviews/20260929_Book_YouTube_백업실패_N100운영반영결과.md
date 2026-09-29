# Book·YouTube 정기 백업 실패 조치 및 N100 운영 반영 결과

## 1. 문서 정보

| 항목 | 내용 | 비고 |
|---|---|---|
| 작성일 | 2026-09-29 | Asia/Seoul 기준 |
| 대상 | Book Memo·YouTube Memo PVC 암호화 백업 | Crawler·Portal 백업 설정은 변경하지 않음 |
| 기준 자료 | 2026-09-29 정기 Job·상태 ConfigMap·Pod, [PR #311](https://github.com/Len-Yoon/personal-server/pull/311), N100 적용 전후 조회 | Secret 값 제외 |
| 목적 | 정기 백업 실패 원인 범위, 코드 수정, 실제 복원 검증과 잔여 확인 사항 기록 | |

## 2. 핵심 요약

2026-09-29 04:00 Book과 08:30 YouTube 정기 백업 Job이 각각 실행 약 31초 뒤 `preflight`에서 실패함. 두 작업 모두 실행 이미지·일정은 목표 상태와 일치했으며, writer는 1/1, PVC는 Bound, 백업 잠금은 해제됐음. 당시 실행 코드는 원격 저장소 조회를 30초 제한으로 한 번만 수행했으므로 시간초과가 가장 유력하나, 명령의 세부 오류는 보안상 기록되지 않아 직접 원인은 **확인 필요**임.

맥 저장소에서 원격 사전 조회 시간초과에만 60초 제한·최대 2회 재시도를 추가하고, 시간초과와 다른 원격 오류를 고정된 실패 단계로 구분함. [PR #311](https://github.com/Len-Yoon/personal-server/pull/311)을 `c7101ba9462f9f706341363616d2bf2eb458011f`로 병합한 뒤 사용자 운영 승인에 따라 N100에 반영함. 두 서비스의 수동 백업 Job은 새 이미지로 암호화 업로드·재다운로드·해시 확인·격리 복원 검증을 완료했고, writer와 잠금이 정상 상태로 복귀함.

## 3. 상세 결과

| 항목 | 결과 | 비고 |
|---|---|---|
| 정기 실패 조사 | Book 04:00, YouTube 08:30 Job 모두 약 31초 뒤 `failed/preflight` | 당시 오류 하위 원인은 확인 필요. OOM·이미지 pull·스케줄 실패 징후는 없었음 |
| 맥 구현·검증 | 시간초과에만 최대 2회 제한 재시도, 고정 실패 단계 `preflight-remote-timeout`·`preflight-remote-error` | 재시도는 writer 중지 전에 수행. 다른 오류는 즉시 중단 |
| 저장소 병합 | PR #311, `c7101ba9462f9f706341363616d2bf2eb458011f` | PR CI·Trivy·Agent Review 통과. `main` CI는 무관한 Relay 모의 테스트의 첫 실행 실패 후 실패 검사 재실행 통과 |
| 자동배포 | `blocked_path`로 제외 | `Deploy N100` 실행 `36503349348`의 재실행에서 분류 성공·실제 deploy 건너뜀 |
| N100 동기화 | `3355dfd3f2513c18e4b6b828eb9b9643e25e3d65` → `c7101ba9462f9f706341363616d2bf2eb458011f` fast-forward | 추적 변경 0건 확인, 기존 미추적 파일 2개 보존 |
| 이미지 반입 | Book `dc46dfdc6714…ca0bc54`, YouTube `fb570a295d8a…72a4b4` | 맥 OCI `linux/amd64`·소스 일치·전송 SHA-256 확인 후 N100 containerd digest alias 반입. 기존 이미지는 rollback용으로 보존 |
| CronJob 적용 | Book·YouTube 각각 init·backup 컨테이너의 이미지 필드 2개만 변경 | UID·resourceVersion·기존 이미지 검사, 서버 dry-run, 적용 후 spec 재조회. 일정·활성 상태·기타 spec 보존. 전체 설치 manifest 재적용 없음 |
| Book 수동 검증 | `book-pvc-backup-verify-20260929-a` Job 10:07:57 KST 완료 | 백업 증적 완료 시각 10:07:54 KST. 백업 컨테이너 exit 0·`book_pvc_backup=PASS`, 현재 run ID의 암호화 백업·격리 복원 증적, writer 1/1·잠금 해제 확인 |
| YouTube 수동 검증 | `youtube-pvc-backup-verify-20260929-a` Job 10:12:53 KST 완료 | 백업 증적 완료 시각 10:12:50 KST. 백업 컨테이너 exit 0·`youtube_pvc_backup=PASS`, 현재 run ID의 암호화 백업·격리 복원 증적, writer 1/1·잠금 해제 확인 |
| 목표 상태 | `production_state=PASS` | 세 CronJob의 이미지·일정·writer·PVC·증적 존재 검사 통과. 성공·복원 내용은 위 Job과 ConfigMap을 별도 확인. Crawler 이미지·일정은 변경하지 않음 |
| 자동 실행 | Book·YouTube `suspend=false` 유지 | 다음 정기 실행의 성공은 별도 확인 필요 |
| 외부 health | Portal·Book·YouTube·News 각 10초 간격 3회, 모두 HTTP 200 | 12/12 통과 |

수동 YouTube Job이 실행 중일 때 간단한 완료 판독 명령이 빈 상태 필드를 잘못 해석함. Job 재실행이나 잠금 변경 없이 Kubernetes JSON 상태를 다시 확인했고, 같은 단일 Job의 최종 성공·증적을 검증함.

## 4. 검토 결과

주 담당은 맥 코드·테스트·이미지 빌드와 N100 제한 적용을 수행함. 독립 코드 검토 담당은 변경 diff, 보호 대상, 관련 테스트, OCI 아카이브의 manifest·플랫폼·이미지 내부 코드 일치를 확인함. 독립 운영 검토 담당은 변경 전 단일 writer·PVC·잠금·Job·외부 health를 읽기 전용으로 확인하고, 적용 후 Job·복원 증적과 운영 상태를 재확인함. Secret 값·PVC 데이터·Portal·Crawler CronJob·Caddy·Tunnel·bootstrap·scheduler 설정은 변경하지 않음.

## 5. 확인 필요 사항

| 항목 | 상태 | 후속 조치 |
|---|---|---|
| 실패 당시 `preflight`의 직접 원인 | 확인 필요 | 과거 stderr가 저장되지 않아 원격 조회 시간초과·인증·API 지연·네트워크 중 직접 원인 확정 불가. 수동 성공 중 신규 재시도가 실제 발동했는지도 확인되지 않음. 다음 실패 시 고정 단계로 분류 |
| 다음 정기 Book·YouTube 백업 | 확인 필요 | 다음 예정 Job의 성공·새 복원 증적·잠금 해제 확인 필요 |
| Crawler 다음 정기 백업 | 확인 필요 | 2026-09-29 13:00 KST 이후 별도 점검 필요 |
| Telegram 단말 수신 | 확인 필요 | 이번 적용에서 단말 도착을 확인하지 않음 |
| 보존 삭제 | 미수행 | 원격 객체 ID 고정·preview 검증 전에는 `--prune-go` 실행하지 않음 |

## 6. 후속 조치

다음 정기 Job 결과를 읽기 전용으로 확인하고, 실패하면 새 고정 단계·Job/Pod 상태·writer·잠금을 먼저 대조함. 운영 변경·수동 재실행은 별도 범위로 판단함. 이번 N100 적용 승인은 위 두 이미지 반입·CronJob 이미지 교체·각 수동 검증 1회에 사용 완료됐으며, 다른 보호 대상의 변경 승인으로 재사용하지 않음.
