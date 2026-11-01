from __future__ import annotations

from dataclasses import asdict, dataclass, field

import orjson


@dataclass(slots=True)
class Transaction:
    tx_id: str
    ts: int  # event time, epoch milliseconds
    src: str
    dst: str
    amount: float
    src_city: int
    dst_city: int
    device: str
    src_age_days: float  # account metadata, as a real payment feed would carry it
    # Ground-truth scenario id (e.g. "ring-001"), None for legitimate traffic.
    # Used only to evaluate the system; the scorer never reads it as a feature.
    truth: str | None = None
    src_district: int = -1  # id in vn_districts.geojson, or -1 where no district data exists
    dst_district: int = -1

    def to_bytes(self) -> bytes:
        return orjson.dumps(asdict(self))

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Transaction":
        return cls(**orjson.loads(raw))


@dataclass(slots=True)
class Decision:
    tx_id: str
    ts: int
    src: str
    dst: str
    amount: float
    src_city: int
    dst_city: int
    score: float
    action: str  # "allow" | "review" | "block"
    scored_ts: int
    latency_ms: float  # event time -> decision time, including broker hops
    reasons: list[str] = field(default_factory=list)
    truth: str | None = None
    src_district: int = -1
    dst_district: int = -1

    def to_bytes(self) -> bytes:
        return orjson.dumps(asdict(self))

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Decision":
        return cls(**orjson.loads(raw))
