# 2026-09-30 Book·YouTube 홈 UI N100 운영 반영 결과

## 1. 문서 정보

| 항목 | 내용 | 비고 |
|---|---|---|
| 대상 | Book Memo·YouTube Memo 홈 화면 | N100 K3s 실행 이미지 |
| 기준 | [PR #325](https://github.com/Len-Yoon/personal-server/pull/325), `9c2895d65aba6b2912bbc61da682ab0f98bb0279` | Mac 빌드·검증 커밋 |
| 목적 | 사용자 승인에 따른 운영 반영 및 서비스·백업 경계 확인 | 2026-09-30 KST |

## 2. 핵심 요약

Mac에서 검증·병합한 홈 화면 변경을 N100에 적용함. Book 검색 입력창을 키우고 두 서비스의 입력·내보내기·저장 목록 순서를 정리함. 기존 백업 일정과 데이터는 변경하지 않았으며 새 화면 버전과 비로그인 내보내기 차단을 공개 주소에서 확인함.

## 3. 적용·검증 결과

| 단계 | 결과 | 검증·비고 |
|---|---|---|
| 저장소 병합 | PR #325를 `9c2895d`로 병합 | Book 49건·YouTube 44건·maintenance 526건, PR 9개 검사 묶음·Trivy 및 병합 후 CI·Trivy 성공. 안전 자동배포는 K3s 서비스를 제외함 |
| N100 동기화 | `fbf21e7`에서 `9c2895d`로 fast-forward | 추적 변경 0건, 기존 미추적 파일 2개 보존. 강제 reset 없음 |
| 이미지 반입 | 두 OCI archive의 SHA-256 및 Linux AMD64 manifest 검증 후 containerd 반입 | Book 전송 중 연결 종료 시 원격 부분 파일과 로컬 접두 해시를 확인하고 남은 바이트만 이어 전송함. 전체 해시 일치 후 반입함 |
| Book 적용 | 기존 이미지 조건 검사 후 새 이미지로 전환 | 배포 도구 `--check`·`--go` 성공. 새 실행 이미지 digest 아래 표 참조 |
| YouTube 적용 | 배포 조건·백업 잠금을 확인한 뒤 이미지 필드만 전환 | 별도 검증 도구 `--check`·`--go` 성공. 새 실행 이미지 digest 아래 표 참조 |
| 수동 검증 | 두 공개 홈의 새 CSS 버전 확인 | Book `home-clarity-3`, YouTube `home-clarity-1`. 두 비인증 `/api/export` 요청 모두 HTTP 401 |
| 자동 실행 활성화 | 해당 없음 | 백업 일정·자동배포 정책 변경 없음 |
| 외부 health | Portal·Book·YouTube 각각 10초 간격 3회 HTTP 200 | 최종 9/9 성공 |

| 서비스 | archive SHA-256 | OCI manifest digest |
|---|---|---|
| Book Memo | `8bd70e6a8f4513c393bfe5f9f308b78af7c0de184402947906c34e34fc826a80` | `sha256:1a1bb0e9475c15769627d38b7c4314a15531bf5cb665955d6dfff0736626c763` |
| YouTube Memo | `945e6a9b37f4901a1acf21a164f274f19abeb15e026d0f736a273d1690da4719` | `sha256:0ff9b7f82b8d9b5e707d7f93583631582f25317287ed5724b8d62a26f51ad551` |

## 4. 검토 결과

적용 전 독립 운영 검토에서 단일 K3s writer, PVC Bound/RWO, 순차 백업 4단계 성공·잠금 해제, Portal·Book·YouTube 공개 health 9/9 HTTP 200을 확인함. 적용 후 독립 운영 검토에서도 Mac·N100 커밋 일치, Book·YouTube Deployment/Pod imageID와 목표 digest 일치, 각 Recreate·1/1/1 Ready, PVC Bound/RWO 및 기존 mount, 신뢰 가능한 K3s runtime marker와 Compose writer 0을 확인함. 순차 백업 timer 활성·네 기존 CronJob 중지·백업 잠금 해제·비종료 백업 Job 0건을 확인했고 외부 health는 다시 9/9 HTTP 200임. 이번 변경은 두 앱의 실행 이미지만 대상으로 하며 Secret·PVC 데이터·백업 일정·Caddy·Tunnel·bootstrap·scheduler는 변경하지 않음. rollback은 수행하지 않음.

## 5. 확인 필요 사항

인증된 사용자의 실제 내보내기·UI 실사용 조작과 기존 운영 기록을 수정하는 시험은 수행하지 않음. 다음 정기 백업(2026-10-01 02:30 KST)의 성공 여부는 해당 실행 후 확인 필요함.

## 6. 후속 조치

다음 예약 백업 실행 후 성공·잠금 해제·단일 writer 복귀를 확인함.
