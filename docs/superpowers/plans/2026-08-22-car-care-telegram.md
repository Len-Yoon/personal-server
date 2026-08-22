# Telegram 차량관리 서비스 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 더 뉴 그랜저의 엔진오일·미션오일 관리, 경고등, 운행 종료 요약을 Telegram으로 제공하는 독립 서비스를 구축함.

**Architecture:** `car-care-worker`를 새 Python 컨테이너로 추가하고 Telegram long polling, SQLite 상태 저장, 정비 규칙, Hyundai API 어댑터를 분리함. Hyundai API가 비활성 상태여도 Telegram 수동 주행거리와 정비 알림은 동작함.

**Tech Stack:** Python 3.11, 표준 라이브러리 `sqlite3`·`urllib`, Docker Compose, `unittest`, Telegram Bot API, Hyundai Developers API.

**Spec:** `docs/superpowers/specs/2026-08-22-car-care-telegram-design.md`

## OAuth 실연동 보완 계획 (2026-08-22)

**목표:** Hyundai Developers API의 실제 OAuth 사용자 인증·동의·차량 데이터 호출을 통해 BlueLink 차량 상태를 수집함.

**공개 주소:** `https://car.len.pe.kr/oauth/hyundai/callback` (Cloudflare DNS CNAME 생성 완료)

**변경 범위:** `car-care-worker` 내부 OAuth·callback 서버·테스트, Compose localhost 포트, N100 Cloudflare Tunnel ingress 운영 설정. 기존 뉴스 및 스케줄러는 변경하지 않음.

**성공 기준:**

1. OAuth state가 일회성·만료 검증되고 authorization code를 refresh token으로 교환함.
2. refresh token 갱신, 필수 사용자 동의, 차량 목록 선택, 주행거리·DTE·경고등 호출이 모두 모킹 테스트로 검증됨.
3. callback은 localhost `8015`에만 노출하고, N100 Tunnel이 `car.len.pe.kr`을 그 포트로 전달함.
4. Hyundai 콘솔 Redirect URL·Callback URL에 위 HTTPS 주소를 등록한 후 `/현대연결`로 실차 인증을 완료할 수 있음.

**확인 필요:** Cloudflare API token은 DNS 권한만 있어 기존 N100의 locally-managed Tunnel ingress는 API로 수정할 수 없음. N100에서 `~/.cloudflared/config.yml` 한 줄을 반영하고 터널을 재시작해야 함.

## Global Constraints

- `crawler-worker`와 `crawler-worker/app/services/news_scheduler.py`는 수정하지 않음.
- `CAR_CARE_TELEGRAM_BOT_TOKEN`, `CAR_CARE_TELEGRAM_CHAT_ID`만 사용하며 기존 뉴스 Bot 환경변수는 사용하지 않음.
- 신규 컨테이너와 Compose 변경은 사용자가 A안으로 명시 승인한 범위임.
- 차량 번호, 차대번호, 위치, 계정 정보, 비밀값을 DB 메시지·로그·Telegram에 기록하지 않음.
- 엔진오일은 10,000km 또는 12개월, 미션오일은 60,000km 기준임.
- 경고등은 엔진오일·브레이크오일·타이어 공기압·워셔액·주유만 처리함.
- 운행 종료는 유휴 시간 기반 추정이며 연료 사용량은 표시하지 않음.

## File Structure

| 파일 | 책임 |
|---|---|
| `car-care-worker/app/models.py` | 차량 스냅샷, 정비 이력, 알림 자료형 |
| `car-care-worker/app/services/store.py` | SQLite 상태·이력·중복 알림 저장 |
| `car-care-worker/app/services/maintenance.py` | 정기 정비 판정·메시지 |
| `car-care-worker/app/services/vehicle_monitor.py` | 경고등 전이·운행 종료 판정 |
| `car-care-worker/app/services/telegram.py` | long polling, 허용 채팅 검증, 명령·발송 |
| `car-care-worker/app/services/hyundai.py` | Hyundai API 미설정 no-op 및 실제 어댑터 |
| `car-care-worker/app/main.py` | 실행 루프 조립 |
| `tests/car_care_worker/` | 서비스 단위 테스트 |

### Task 1: 도메인 모델·SQLite 저장소

**Files:**
- Create: `car-care-worker/app/__init__.py`
- Create: `car-care-worker/app/models.py`
- Create: `car-care-worker/app/services/__init__.py`
- Create: `car-care-worker/app/services/store.py`
- Create: `tests/car_care_worker/__init__.py`
- Create: `tests/car_care_worker/test_store.py`

**Interfaces:**
- Produces: `VehicleSnapshot(observed_at: datetime, odometer_km: int, dte_km: int | None, warnings: frozenset[str])`.
- Produces: `MaintenanceRecord(item: str, odometer_km: int | None, completed_at: date)`.
- Produces: `CarCareStore` with `initialize`, `complete_maintenance`, `get_maintenance`, `save_snapshot`, `load_last_snapshot`, `get_alert_state`, `set_alert_state`.

- [ ] **Step 1: Write failing persistence tests**

```python
def test_completion_persists_engine_oil_distance_and_date(tmp_path):
    store = CarCareStore(tmp_path / "car-care.sqlite3")
    store.initialize()
    store.complete_maintenance("engine_oil", 52340, date(2026, 8, 22))
    assert store.get_maintenance("engine_oil") == MaintenanceRecord(
        "engine_oil", 52340, date(2026, 8, 22)
    )

def test_snapshot_and_alert_state_survive_new_store_instance(tmp_path):
    path = tmp_path / "car-care.sqlite3"
    first = CarCareStore(path)
    first.initialize()
    first.set_alert_state("warning:fuel", "active")
    assert CarCareStore(path).get_alert_state("warning:fuel") == "active"
```

- [ ] **Step 2: Verify the tests fail**

Run: `PYTHONPATH=car-care-worker python -m unittest tests.car_care_worker.test_store -v`

Expected: FAIL because the model and store modules do not exist.

- [ ] **Step 3: Implement minimal models and storage**

```python
@dataclass(frozen=True)
class MaintenanceRecord:
    item: str
    odometer_km: int | None
    completed_at: date

class CarCareStore:
    def initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS maintenance_records ("
                "item TEXT PRIMARY KEY, odometer_km INTEGER, completed_at TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS vehicle_snapshots ("
                "id INTEGER PRIMARY KEY CHECK (id = 1), observed_at TEXT NOT NULL, "
                "odometer_km INTEGER NOT NULL, dte_km INTEGER, warnings_json TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS alert_states ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            )
```

Store timestamps as UTC ISO-8601 values and warnings as a JSON list. Reject negative odometer values and items outside `engine_oil`, `transmission_oil` with `ValueError`.

- [ ] **Step 4: Verify the storage tests pass**

Run: `PYTHONPATH=car-care-worker python -m unittest tests.car_care_worker.test_store -v`

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```bash
git add car-care-worker/app tests/car_care_worker
git commit -m "feat: 차량관리 상태 저장소 추가"
```

### Task 2: 정비·경고등·운행 종료 규칙

**Files:**
- Create: `car-care-worker/app/services/maintenance.py`
- Create: `car-care-worker/app/services/vehicle_monitor.py`
- Create: `tests/car_care_worker/test_maintenance.py`
- Create: `tests/car_care_worker/test_vehicle_monitor.py`

**Interfaces:**
- Consumes: Task 1의 `CarCareStore`, `MaintenanceRecord`, `VehicleSnapshot`.
- Produces: `Alert(kind: str, key: str, text: str)`.
- Produces: `evaluate_maintenance(current_odometer_km, today, records) -> list[Alert]` 및 `VehicleMonitor.observe(snapshot) -> list[Alert]`.

- [ ] **Step 1: Write failing rule tests**

```python
def test_engine_oil_alert_starts_at_9000km_after_service():
    records = {"engine_oil": MaintenanceRecord("engine_oil", 50000, date(2026, 1, 1)),
               "transmission_oil": None}
    alerts = evaluate_maintenance(59000, date(2026, 8, 22), records)
    assert alerts[0].key == "maintenance:engine_oil"
    assert "1,000km" in alerts[0].text

def test_warning_is_emitted_only_when_it_becomes_active(tmp_path):
    monitor = prepared_monitor(tmp_path)
    monitor.observe(snapshot(52340, frozenset()))
    assert [a.key for a in monitor.observe(snapshot(52340, frozenset({"tire_pressure"})))] == ["warning:tire_pressure"]
    assert monitor.observe(snapshot(52340, frozenset({"tire_pressure"}))) == []
```

- [ ] **Step 2: Verify the tests fail**

Run: `PYTHONPATH=car-care-worker python -m unittest tests.car_care_worker.test_maintenance tests.car_care_worker.test_vehicle_monitor -v`

Expected: FAIL because the rule modules do not exist.

- [ ] **Step 3: Implement exact rules**

```python
MAINTENANCE_RULES = {
    "engine_oil": MaintenanceRule(10_000, 12, 1_000, 30),
    "transmission_oil": MaintenanceRule(60_000, None, 5_000, None),
}
SUPPORTED_WARNINGS = {"engine_oil", "brake_oil", "tire_pressure", "washer_fluid", "fuel"}
```

Emit maintenance alerts once per key and clear the key only after `/정비완료`. Emit a warning only on inactive-to-active transition; store inactive after clearing. After an odometer increase, emit one trip summary only if the odometer remains unchanged for 15 minutes. The summary contains trip distance, odometer, DTE when available, and engine-oil remaining distance when an engine-oil record exists.

- [ ] **Step 4: Add boundary tests and verify pass**

```python
def test_transmission_oil_is_due_at_60000km_after_service():
    record = MaintenanceRecord("transmission_oil", 10000, date(2025, 1, 1))
    alerts = evaluate_maintenance(70000, date(2026, 8, 22), {"engine_oil": None, "transmission_oil": record})
    assert alerts[0].key == "maintenance:transmission_oil"

def test_idle_after_distance_increase_emits_one_trip_summary(tmp_path):
    # Observe 52,320km, 52,340km, then unchanged after 15 minutes.
    # Assert the only summary includes "이번 운행: 20km".
```

Run: `PYTHONPATH=car-care-worker python -m unittest tests.car_care_worker.test_maintenance tests.car_care_worker.test_vehicle_monitor -v`

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add car-care-worker/app/services tests/car_care_worker
git commit -m "feat: 차량 정비 및 경고 알림 판정 추가"
```

### Task 3: Telegram·Hyundai 어댑터

**Files:**
- Create: `car-care-worker/app/services/telegram.py`
- Create: `car-care-worker/app/services/hyundai.py`
- Create: `tests/car_care_worker/test_telegram.py`
- Create: `tests/car_care_worker/test_hyundai.py`

**Interfaces:**
- Consumes: Task 1-2 인터페이스.
- Produces: `TelegramClient.poll(offset) -> list[TelegramUpdate]`, `TelegramClient.send(text) -> bool`, `CommandHandler.handle_update(update) -> str | None`.
- Produces: `HyundaiClient.fetch_snapshot() -> VehicleSnapshot | None`.

- [ ] **Step 1: Write failing adapter tests**

```python
def test_rejects_update_from_unconfigured_chat_id():
    assert handler(allowed_chat_id="123").handle_update(TelegramUpdate("999", "/차량")) is None

def test_complete_transmission_oil_records_command_odometer(tmp_path):
    response = handler_for(tmp_path).handle_update(TelegramUpdate("123", "/정비완료 미션오일 52340"))
    assert "미션오일 정비 완료" in response
    assert handler_for(tmp_path).store.get_maintenance("transmission_oil").odometer_km == 52340

def test_hyundai_client_returns_none_without_credentials(monkeypatch):
    monkeypatch.delenv("HYUNDAI_CLIENT_ID", raising=False)
    assert HyundaiClient.from_environment().fetch_snapshot() is None
```

- [ ] **Step 2: Verify the tests fail**

Run: `PYTHONPATH=car-care-worker python -m unittest tests.car_care_worker.test_telegram tests.car_care_worker.test_hyundai -v`

Expected: FAIL because adapter modules do not exist.

- [ ] **Step 3: Implement safe external boundaries**

Use Telegram `getUpdates` long polling (25 seconds) and `sendMessage` (8 seconds). Accept only the configured chat ID. Implement `/차량`, `/주행거리 <km>`, `/정비완료 엔진오일 [km]`, `/정비완료 미션오일 [km]`, `/정비목록`, `/알림테스트`. When any required `HYUNDAI_*` variable is missing, use no-op mode without a network request. Map only odometer, DTE, and the five supported warning identifiers; ignore all other API fields and never log tokens or headers.

- [ ] **Step 4: Verify adapters pass**

Run: `PYTHONPATH=car-care-worker python -m unittest tests.car_care_worker.test_telegram tests.car_care_worker.test_hyundai -v`

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add car-care-worker/app/services tests/car_care_worker
git commit -m "feat: 차량관리 텔레그램 연동 추가"
```

### Task 4: 독립 컨테이너·실행 루프·운영 등록

**Files:**
- Create: `car-care-worker/app/main.py`
- Create: `car-care-worker/Dockerfile`
- Create: `car-care-worker/requirements.txt`
- Create: `tests/car_care_worker/test_main.py`
- Modify: `docker-compose.yml`
- Modify: `docker-compose.n100.yml`
- Modify: `.env.example`
- Modify: `tests/test_compose_config.py`
- Modify: `tests/run_service_tests.py`
- Modify: `README.md`
- Modify: `docs/operations-reference.md`

**Interfaces:**
- Consumes: Task 1-3 인터페이스.
- Produces: `python -m app.main` 장기 실행 프로세스.

- [ ] **Step 1: Write failing lifecycle and Compose tests**

```python
def test_compose_defines_isolated_car_care_worker():
    service = load_compose()["services"]["car-care-worker"]
    assert service["build"] == "./car-care-worker"
    assert "800" not in str(service.get("ports", []))
    assert "./data/car-care:/data/car-care" in service["volumes"]

def test_run_once_sends_command_response_and_monitor_alerts():
    telegram, hyundai, monitor = fakes()
    run_once(handler(), telegram, hyundai, monitor)
    assert telegram.sent == ["[차량 상태]", "[운행 결과]"]
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m unittest tests.test_compose_config tests.car_care_worker.test_main -v`

Expected: FAIL because the worker and service configuration do not exist.

- [ ] **Step 3: Implement lifecycle and deployment-safe config**

```python
def run_once(handler, telegram, hyundai, monitor):
    for update in telegram.poll():
        if response := handler.handle_update(update):
            telegram.send(response)
    if snapshot := hyundai.fetch_snapshot():
        for alert in monitor.observe(snapshot):
            telegram.send(alert.text)
```

`main()` initializes SQLite, polls Telegram every 5 seconds, retrieves Hyundai data every 5 minutes, and exits on SIGTERM/SIGINT. The Compose service has `restart: unless-stopped`, no public port, `.env`, and only `./data/car-care:/data/car-care` as writable state. Add empty `CAR_CARE_*` and optional `HYUNDAI_*` values to `.env.example`. Register a `car-care-worker` suite containing all six new test modules. Document required variables, supported commands, manual initialisation via `/정비완료`, Hyundai approval dependency, and no inbound Caddy route.

- [ ] **Step 4: Verify all relevant checks pass**

Run: `PYTHONPATH=car-care-worker python -m unittest discover -s tests/car_care_worker -v`

Expected: PASS.

Run: `python tests/run_service_tests.py --suite car-care-worker --suite maintenance`

Expected: PASS.

Run: `docker compose config --quiet`

Expected: exit code 0.

- [ ] **Step 5: Verify scope and commit Task 4**

Run: `git diff --check`

Expected: no output.

Run: `git diff --name-only`

Expected: only `car-care-worker/`, `tests/car_care_worker/`, Compose files, `.env.example`, the test runner/config tests, README, operations reference, and approved documentation.

```bash
git add car-care-worker tests/car_care_worker docker-compose.yml docker-compose.n100.yml .env.example tests README.md docs
git commit -m "feat: 텔레그램 차량관리 워커 추가"
```

## Execution Roles and Review Gates

| 역할 | 담당 범위 | 동시 수정 금지 | 성공 기준 |
|---|---|---|---|
| 주 에이전트 | Task 1·4 통합 및 전체 검증 | 구현 에이전트와 동일 파일 수정 금지 | 독립 서비스 구성과 회귀 검증 통과 |
| 구현 에이전트 | Task 2·3 규칙·어댑터 | `main.py`·Compose 수정 금지 | 단위 테스트 통과, 외부 호출 모킹 |
| 검토 에이전트 | 각 Task 완료 후 독립 검토 | 코드 수정 금지 | 뉴스 스케줄러 미변경, 비밀값 미노출, 잔여 위험 보고 |

## Plan Self-Review

| 점검 항목 | 결과 |
|---|---|
| 설계 범위 | 정기 정비, 경고등, 운행 종료 요약, 수동 모드, Hyundai 옵트인 반영됨 |
| 보안·오류 | 허용 채팅 검증, no-op, 민감정보 비표시, 외부 호출 모킹 반영됨 |
| 금지 영역 | 뉴스 서비스·뉴스 스케줄러·배포 스크립트·Caddy 미변경. Compose는 사용자 승인 예외 범위임 |
| 테스트 | 저장소, 규칙, 경고 전이, 운행 요약, Telegram, Hyundai, lifecycle, Compose 검증 포함됨 |
| 확인 필요 | Hyundai 상용 API 권한과 실제 더 뉴 그랜저 응답 필드 확인 필요 |
