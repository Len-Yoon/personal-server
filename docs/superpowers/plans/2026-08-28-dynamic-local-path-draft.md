# Dynamic local-path 초안 계획

## 목표

native ext4에서 통과한 K3s `local-path` 동적 저장소 smoke 결과를 검토용 GitOps 문서·비활성 템플릿에 반영함.

## 범위

- PVC 계약만 `.yaml.tmpl`로 작성하고 실제 PV·StorageClass·PVC·워크로드는 적용하지 않음.
- 1차 후보는 portal, crawler, YouTube memo, book memo로 제한함.
- portal 계약은 실제 컨테이너의 8000 포트, `/health`, `/data/files`와 일치시킴.

## 검증 및 게이트

- 기존 scratch 결과(local-path, 파일 I/O, SQLite 잠금, portal 빈 볼륨)를 근거로 기록함.
- 앱 데이터 복사·복원, 단일 writer cutover, Secret 주입, Caddy 전환은 미수행으로 명시함.
- 템플릿은 root Kustomize에 포함하지 않고, 대화형 셸의 `EXIT trap`에 의존하지 않는 명시적 정리를 요구함.

## 성공 기준

관련 단위 테스트, diff 공백 검사, 빈 Kustomize 렌더링이 통과하고 변경 범위가 문서·비활성 템플릿·테스트로 제한됨.
