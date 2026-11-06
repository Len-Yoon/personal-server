# 실제 7개 앱 PostgreSQL 격리 검증

Mac의 `orbstack` 로컬 socket, Docker, OpenSSL이 필요함. 저장소 루트에서 `python3 infra/k8s/ha-lab/run.py`를 명시 실행함. 기존 로컬 PG17 이미지와 각 앱 Dockerfile의 Python 기반 이미지를 사용하며, 별도 가상환경에 실제 앱 requirements 및 검증용 psycopg를 설치함.

일회용 TLS/SCRAM primary/standby, private 설정, 합성 원본 상태를 생성함. 7개 앱 이전·A/B 동작·단절 commit 취소/동일 요청 재보호·권한·lock 세션 상실·이전 노드 거절을 확인하고 소유 자원을 정리함. 최종 JSON의 `status=PASS`, `owned_fixture_removed=true`를 함께 확인해야 함. 실패 JSON은 원인 분류와 소스 위치만 기록하며 원본 SQL·파라미터·예외 메시지·인증값을 출력하지 않음.

운영 자료·키·환경 파일을 build context에 복사하지 않으며 포트를 공개하거나 N100/Windows에 접근하지 않음. 앱은 source allowlist로 임시 runner에 복사됨. 실제 외부 provider와 호스트 재시작은 대역으로 제한함. 이 도구는 운영 배포·온라인 migration·DB 승격·fencing·backup/restore 도구가 아님.

공유 런타임은 `../ha_runtime`에 있음. 운영 활성화는 차단되어 있으며 기존 운영 Dockerfile/배포 설정은 변경하지 않음. PG 모드에서 commit 결과가 모호하면 같은 요청을 재시도하며 SQLite/JSON으로 fallback하지 않음. 외부 결과의 exactly-once 및 진행 중 효과 중단은 별도 운영 fencing/대사 설계가 필요함.

개발·검증 범위와 잔여 작업은 [구현 기록](../../../docs/20261106_N100_전체이중화_구현기록.md)에 구분함.
