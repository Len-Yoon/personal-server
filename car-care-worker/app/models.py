from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class VehicleSnapshot:
    observed_at: datetime
    odometer_km: int
    dte_km: int | None
    warnings: frozenset[str]


@dataclass(frozen=True)
class MaintenanceRecord:
    item: str
    odometer_km: int | None
    completed_at: date
