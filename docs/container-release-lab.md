# 실제 두 버전 컨테이너 교체 실습

## 1. 문서 정보

| 항목 | 내용 | 비고 |
|---|---|---|
| 작성일 | 2026-10-18 | Asia/Seoul |
| 목적 | 실제 Book 코드 버전 교체·데이터 호환·롤백 검증 | 격리 로컬 실습 |
| 기준 | [실행기](../book-memo/tools/container_release_lab.py), [Docker 경계](../book-memo/tools/container_runtime.py), [probe](../book-memo/tools/container_probe.py) | 제품 앱 코드는 변경하지 않음 |
| 상태 | 구현·회귀 검토 진행 | 실제 측정 및 저장소 반영 확인 필요 |

## 2. 핵심 요약

기존 [프로세스 재배포](deployment-standard-lab.md)를 실제 서로 다른 두 앱 버전의 Docker 컨테이너로 확장함. v1 앱은 `d0a60639ccef6d8acdf821528baf88a1bb6af578`, v2 앱은 실행 시 Git HEAD임. 두 이미지 모두 HEAD의 Dockerfile·requirements를 사용하므로 과거 전체 환경 재현과 구분함.

## 3. 상세 내용

| 시나리오 | 확인 내용 | 비고 |
|---|---|---|
| upgrade | v1 초기 데이터→v2 읽기·수정·동일 요청 재전송 | version2·revision1·request1 |
| roundtrip | v2 수정 후 v1 복귀·읽기·다시 수정 | version3·revision2·request2 |
| readiness_failure | 실제 정상 앱에서 잘못된 probe404→v1 한 번 복구 | v2 쓰기 없음·배포 실패와 복구 성공 분리 |

실행 전 기존 로컬 Docker 엔진이 필요함. 새 VM·클라우드·운영 서버는 구성하지 않음. 실행은 두 이미지를 빌드하며 기본 이미지·의존성 다운로드가 발생할 수 있음. 앱 실행은 network none·호스트 포트 미게시·UID10001·읽기 전용 루트·소유 named volume으로 제한함. HTTP는 같은 컨테이너 내부 loopback에서 확인하며 외부 서비스 상태를 증명하지 않음. 쓰기는 실제 제품 서비스 함수로 수행하며 HTTP 쓰기·인증 검증으로 확대하지 않음.

```sh
.venv/book-memo/bin/python book-memo/tools/container_release_lab.py \
  --run --repeats 3 --deadline 900 \
  --output /tmp/container-own-practice-20261018-01.json
```

| 제한 | 값·동작 | 비고 |
|---|---|---|
| 반복 | 기본 각1회·최대 각3회 | 세 시나리오 |
| 시간 | 전체 기본900초·최대1800초, 빌드당300초·일반 호출15초·정리90초 | 중단 시 부분 JSON |
| 전환 | 이전 앱 컨테이너 정상 종료 확인 후 다음 버전 시작 | 두 버전 컨테이너 동시 실행 없음 |
| 데이터 | 관련8테이블 snapshot·schema·내용·이력·요청 원장·integrity | 파괴적 migration 미검증 |
| 이미지 | Git provenance·전체 context hash·실행 파일 hash·실제 route 차이·로컬 image config ID | registry manifest digest와 구분 |
| 정리 | 기록된 이름/ID와 owner label 대조 후 컨테이너→volume→이미지 | force·prune 없음 |

## 4. 검토 결과

Docker 경계와 시나리오 회귀는 Docker를 격리한 단위 검사로 수행함. 실제 Docker 실행·측정 원본·최종 독립 검토는 확인 필요함. 전용 CLI 프로세스 그룹을 회수하며 다른 이미지 tag/digest 참조는 보존함. 빌드 완료가 불명확하면 잔여 상태를 실패로 기록함.

## 5. 확인 필요 사항

사용자의 직접 진단 경험, 운영 배포·외부 ingress·무중단·다중 노드·breaking migration·모든 입력의 역방향 호환·registry 서명은 미검증임. 기본 이미지·빌드 캐시(이번 다운로드·생성분 포함)는 유지되므로 Docker 전체 상태 복원을 주장하지 않음.

## 6. 후속 조치

실측 원본과 검증 결과를 확인한 뒤 [면접 자료](20261018_DevOps_면접답변.md)의 근거로 사용함. 직접 설명 연습은 [DB 실습지](20261018_DB잠금_실습지.md)의 빈칸부터 작성함.
