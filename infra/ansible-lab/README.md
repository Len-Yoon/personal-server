# Ansible localhost 실습

## 범위

WSL `localhost`에서만 고정 Compose project `personal-server-ansible-lab`을 관리함. 파일은 현재 사용자의 `~/.local/share/personal-server-ansible-lab`에 두고 HTTP는 `127.0.0.1:18088`에만 바인딩함. 운영 서비스와 기존 배포 도구는 대상에서 제외함.

## 실행 전 확인

다음 명령은 실습 대상 WSL에서 실행함. Ansible, `community.docker`, Docker Compose v2와 샘플 이미지가 이미 준비되어 있어야 함. playbook은 설치나 이미지 반입을 수행하지 않음.

```sh
ansible-galaxy collection list community.docker
docker compose version
ansible-playbook -i infra/ansible-lab/inventory/localhost.ini infra/ansible-lab/playbooks/site.yml --syntax-check
ansible-playbook -i infra/ansible-lab/inventory/localhost.ini infra/ansible-lab/playbooks/site.yml --check --diff
```

전체 `site.yml` 실행 시 collection·Compose·고정 대상 변수·전용 디렉터리 소유 표식·동일 이름 Docker project 자원과 Compose 소유 label·loopback 포트를 먼저 검사함. `-e`로 project 이름, 경로, 포트를 바꾸면 실패함. 기존 Compose 정의는 승인된 템플릿과 전체 구성이 일치해야 함. 사전 검사가 실패하면 앱을 시작하지 않음. `--tags deploy`만 지정하면 사전 검사 완료 기록이 없어 실패함.

## 배치·검증·롤백

아래 실행은 실제 Docker 상태를 변경하므로, 운영 대상에서는 별도 적용 승인을 받은 뒤 수행해야 함.

```sh
ansible-playbook -i infra/ansible-lab/inventory/localhost.ini infra/ansible-lab/playbooks/site.yml
ansible-playbook -i infra/ansible-lab/inventory/localhost.ini infra/ansible-lab/playbooks/site.yml
ansible-playbook -i infra/ansible-lab/inventory/localhost.ini infra/ansible-lab/playbooks/rollback.yml
```

첫 실행은 응답 파일과 Compose 정의를 배치한 뒤 loopback HTTP 200 및 고정 응답을 확인함. 두 번째 실행의 변경 수는 0건이어야 함. 롤백은 `.owner` 내용, 승인된 Compose 전체 구성, 동일 이름 Docker 자원, 디렉터리 내 일반 파일 3개만 있는지 확인한 뒤 전용 project를 중지하고 전용 디렉터리만 제거함. 소유 확인에 실패하거나 다른 파일·디렉터리·링크가 있으면 삭제하지 않음.

## 증적 기록

| 항목 | 기록값 | 비고 |
|---|---|---|
| 기준 커밋 | 확인 필요 | 실행한 저장소 커밋 |
| 실행 환경 | 확인 필요 | WSL 호스트와 도구 버전 |
| 1차 실행 | 확인 필요 | changed 수, loopback HTTP 결과 |
| 2차 실행 | 확인 필요 | changed=0 여부 |
| 롤백 | 확인 필요 | 전용 project·디렉터리 제거 여부 |
| 운영 서비스 영향 | 확인 필요 | 별도 운영 상태 확인 결과 |
