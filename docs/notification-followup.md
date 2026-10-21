# 정기 알림 문구의 N100 일회성 검증

## 1. 문서 정보

| 항목 | 내용 | 비고 |
|---|---|---|
| 작성일 | 2026-10-21 | 표시 Asia/Seoul, 비교 UTC |
| 목적 | 다음 Portal 정기 백업·공식 월간 감사의 새 문구와 실제 Telegram API 전송 확인 | 사람의 수신·읽음은 자동 확인하지 않음 |
| 대상 | N100의 기존 순차 백업·월간 감사·relay | Mac 실행에 의존하지 않음 |
| 범위 | 저장소 구현·검증·독립 검토·PR·병합, 운영 설치는 적용 직전 별도 승인 | 기존 작업의 운영 승인을 재사용하지 않음 |

## 2. 핵심 요약

기존 전달 원장은 보고서 ID의 전송 성공을 기록하지만 실제 문구 해시는 포함하지 않음. relay의 기존 Portal 백업·공식 감사 전송 callback이 성공한 직후, 원장 저장 전에 보고서·실제 전송 문자열의 SHA-256과 보고서 ID·UTC 시각을 전용 로그로 기록함. 본문·채팅 ID·토큰·원본 보고서는 로그에 넣지 않음. 로그 실패는 성공한 전송이나 원장 저장을 방해하지 않음.

N100 일회성 작업은 자연 실행 뒤 실제 receipt와 정규화된 보고서·문구 해시, 원장, 실행 출처를 대조함. 현재 formatter를 다시 실행한 결과나 원장만으로 통과시키지 않음. 로그가 없거나 회전·재시작으로 근거가 부족하면 확인 필요로 남김. 기존 원시 SLO 판정·백업 및 감사 자체의 결과는 수정하지 않음.

## 3. 일정과 검증 범위

| 항목 | 기준 | 비고 |
|---|---|---|
| Portal 백업 | 기존 순차 systemd timer 매일 02:30 | Portal CronJob 자체는 suspend 상태 보존 |
| 공식 감사 | 활성 monthly-sre-audit, 매월 1일 03:30 | 분기·검증 CronJob의 suspend 상태 보존 |
| 현재 다음 대상 | 2026-10-22 백업, 2026-11-01 감사 | 설치 계획 생성 시 실제 다음 날짜 산출 |
| 검증 실행 | 각 대상 시각 5분 뒤, 대상 시각부터 최대 1시간 | 창 안에서 60초 이하 간격, 평상시 반복 조회 없음 |
| 재부팅·로그아웃 | 기존 linger·Persistent timer·부팅 연결 service 사용 | 새 linger·권한 생성 없음, N100 전원 필요 |
| 전원 꺼짐·기한 경과 | 기존 결과가 증명되지 않으면 확인 필요 | 백업·감사를 강제로 재실행하지 않음 |

## 4. 실제 전송과 출처 대조

`notification_delivery_receipt` 로그는 schema_version, kind, run_id, report_sha256, message_sha256, sent_at만 포함함. 같은 보고서에 대한 중복 receipt·시간 불일치·해시 불일치는 통과시키지 않음. report_sha256는 relay 검증기가 반환한 정규화된 dict의 canonical JSON 기준이며 원본 ConfigMap data 전체의 해시와 다를 수 있음.

| 검증 경계 | 필수 근거 | 비고 |
|---|---|---|
| 실행 relay | Deployment UID·전체 명세, 소유 ReplicaSet→Pod, 단일 Ready·재시작 0·고정 imageID·app 소스 해시 | 전송 당시 로그를 읽는 같은 Pod로 제한 |
| 문구 | 배포된 validator/formatter의 해시와 실제 전송 receipt 일치, 필수 새 문구 존재 | API 수락 증거이며 사람의 수신 증거가 아님 |
| 원장 | 해당 보고서 ID의 기존 전달 원장 포함 | 원장에 가짜 결과를 넣지 않음 |
| 감사 자연 실행 | 월간 Cron UID·명세, 정확한 scheduled-timestamp, 소유 Job·Pod·완료/exit 0 | 일반 수동 Job은 통과시키지 않음 |
| 백업 자연 실행 | 날짜 고정 순차 Job·label·template hash·Cron UID·Pod, 순차 결과 Portal passed, 기존 timer LastTrigger·service 시작 시각 | 기존 일정·unit 내용 해시 보존 |
| 보고서와 실행 연결 | runner Pod 시작 대비 run_id UTC 시작 시각, 보고서 완료와 Job 완료 구간 | 시간·소유·템플릿의 교차 증거이며 직접 Job UID를 포함한 생산자 인과 기록으로 확대하지 않음 |

실제 감사 결과가 실패이거나 SLO가 과거 실패여도 문구 전송 검증과 서비스 결과를 구분함. 생산자 Job이 정상 종료하지 않았거나 근거가 충분하지 않으면 검증 통과로 표시하지 않음.

## 5. 설치·상태·정리

새 도구는 기존 [SLO 일회성 검증 도구](../infra/k8s/tools/slo-followup-verification.py)의 검토된 private 파일·원자적 결과 저장·정리·메모리 내 전송 helper를 소스 SHA-256으로 고정해 재사용함. 설치 시 새 전용 지문 디렉터리에 driver/helper를 함께 보존하며 종료된 4df 실행 코드·결과는 변경하지 않음. helper가 달라지면 무조건 실패하고 재검토를 요구함.

설치는 명시 `--go`와 검토된 계획 해시를 요구함. main CI를 통과한 정확한 소스와 새 relay 이미지 적용 뒤 읽기 전용 prepare로 계획을 생성하고, 설치 직전에 relay·Cron·기존 순차 unit 지문을 다시 확인함. 현재 사용자·기존 linger·정상 user manager·활성화 여유 120초가 필요함. 이미 있는 작업·unit·결과는 덮어쓰지 않음. 활성화 응답이 불명확하면 실행 비활성 확인 전 파일을 삭제하지 않음.

검증 결과는 private JSON에 즉시 보존함. 백업 확인 후 결과 메시지 1건, 감사 확인 및 정리 후 최종 메시지 1건을 기존 relay 자격 증명으로 전송함. 자격 증명은 relay 컨테이너 안 메모리에서만 사용하며 외부 파일·환경 변수에 복제하지 않음. 전송 시도 marker를 먼저 저장하고 API 응답 불명 시 중복 재전송하지 않으며 로컬 결과에 unconfirmed를 보존함.

두 대상이 통과하거나 확인 기한이 끝나면 해당 작업의 service/timer·enable 링크·timer 상태·계획 파일·잠금만 정리함. 지문별 코드와 최종 증거 JSON은 보존함. 저장 계획·프로그램·unit hash 또는 소유권·정리 경계가 다르면 삭제를 중단함. 정리 도중 중단되면 durable 결과로 재개하며 사용자 파일·기존 원장·CronJob·서비스·Secret·PVC·운영 데이터는 삭제하거나 수정하지 않음.

## 6. 검토와 운영 경계

| 역할 | 범위 | 검증 구분 |
|---|---|---|
| root | N100 verifier·테스트·문서·통합·후속 적용 | 구현 및 자기 검증 |
| notification_receipt_impl, gpt-6.1-sol medium | relay receipt·해당 테스트 2파일 | 독립적인 구현, 자기 검증 |
| notification_followup_review, gpt-6.1-sol high | 실제 diff·관련 원본·출처·비밀·재부팅·정리·검사·운영 경계 | 구현 미참여 전문 독립 검토 |

동일 파일 동시 수정 없음. 로그·formatter 재실행과 실제 전송 증거, API 수락과 사용자 수신을 구분함. 실제 N100 source fast-forward·relay 이미지 교체·일회성 service/timer 설치는 별도 운영 승인을 받은 범위에서만 진행함. 기존 백업·감사 일정, 권한, Secret, 종료된 일회성 결과는 보존함.

## 7. 확인 필요 사항과 후속 조치

로컬·PR·main 검사 수와 최종 검토 대상 커밋은 해당 PR에 기록함. 운영 설치 전에는 일회성 검증이 활성화된 것으로 보고하지 않음. 실제 두 정기 실행의 문구·전송·정리 결과는 미래 N100 결과 JSON과 결과 알림으로 확인해야 하며 이번 개발 테스트와 구분함.
