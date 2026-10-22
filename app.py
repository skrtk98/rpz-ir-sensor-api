"""RPZ-IR-Sensor API. Run one worker: the hardware is shared."""
from __future__ import annotations

import hmac
import logging
import os
import select
import sqlite3
import struct
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

log = logging.getLogger(__name__)
api_key = APIKeyHeader(name="X-API-Key", auto_error=False)
sensor_lock = threading.Lock()
ir_lock = threading.Lock()
MAX_PULSES = 1023
READING_FIELDS = ("temperature_c", "humidity_percent", "pressure_hpa", "illuminance_lux")
STATIC = Path(__file__).parent / "static"


def authorize(key: str | None = Depends(api_key)) -> None:
    expected = os.environ.get("RPZ_API_KEY", "")
    if not expected:
        raise HTTPException(503, "RPZ_API_KEY is not configured")
    if key is None or not hmac.compare_digest(key, expected):
        raise HTTPException(401, "Invalid API key")


def sample_once():
    """Record one reading. A failed sensor is stored as NULL; nothing is stored if all fail."""
    if not sensor_lock.acquire(timeout=10):
        log.warning("Sampling skipped: sensors busy")
        return
    measured = int(time.time())
    values = {}
    try:
        for read in (read_environment, read_light):
            try:
                values.update(read())
            except (OSError, RuntimeError) as error:  # I2C errors and ID mismatches
                log.warning("Sampling %s failed: %s", read.__name__, error)
    finally:
        sensor_lock.release()
    if not any(values.get(f) is not None for f in READING_FIELDS):
        return
    retention = float(os.environ.get("RPZ_RETENTION_DAYS", "90"))
    with connection() as conn:
        conn.execute("INSERT OR REPLACE INTO readings VALUES (?, ?, ?, ?, ?)",
                     (measured, *(values.get(f) for f in READING_FIELDS)))
        conn.execute("DELETE FROM readings WHERE ts < ?", (measured - int(retention * 86400),))


def sampler(stop: threading.Event, interval: float):
    while not stop.is_set():
        try:
            sample_once()
        except Exception:
            log.exception("Sampling failed")
        stop.wait(interval)


@asynccontextmanager
async def lifespan(app):
    interval = float(os.environ.get("RPZ_SAMPLE_SECONDS", "60"))
    stop = threading.Event()
    thread = threading.Thread(target=sampler, args=(stop, interval), name="sampler", daemon=True)
    if interval > 0:
        thread.start()
    yield
    stop.set()
    if thread.is_alive():
        thread.join(timeout=15)


app = FastAPI(title="RPZ-IR-Sensor API", version="1.1.0", lifespan=lifespan,
              description="Temperature (°C), humidity (%), pressure (hPa), illuminance (lux), and 38 kHz infrared.")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class Code(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pulses_us: list[int] = Field(min_length=1, max_length=MAX_PULSES,
                                description="Alternating mark/space durations in microseconds, starting and ending with mark")

    @field_validator("pulses_us", mode="before")
    @classmethod
    def check_code(cls, values):
        if not isinstance(values, list) or not values or len(values) % 2 == 0:
            raise ValueError("An odd-length pulse list is required")
        if any(type(v) is not int or not 1 <= v <= 100_000 for v in values):
            raise ValueError("Each duration must be an integer between 1 and 100000 us")
        if sum(values) > 1_000_000:
            raise ValueError("A code must be at most one second long")
        return values


class Receive(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeout_seconds: float = Field(default=10, ge=0.1, le=30, allow_inf_nan=False)
    name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


def timestamp():
    return datetime.now(UTC).isoformat()


@contextmanager
def connection():
    path = Path(os.environ.get("RPZ_DB", "data/codes.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    try:
        with conn:
            conn.execute("CREATE TABLE IF NOT EXISTS codes (name TEXT PRIMARY KEY, pulses TEXT NOT NULL, updated_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS readings (ts INTEGER PRIMARY KEY, temperature_c REAL, "
                         "humidity_percent REAL, pressure_hpa REAL, illuminance_lux REAL)")
            yield conn
    finally:
        conn.close()


def save_code(name: str, code: Code):
    import json
    with connection() as conn:
        conn.execute("INSERT INTO codes VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET pulses=excluded.pulses, updated_at=excluded.updated_at",
                     (name, json.dumps(code.pulses_us), timestamp()))


def get_code(name: str):
    import json
    with connection() as conn:
        row = conn.execute("SELECT pulses, updated_at FROM codes WHERE name=?", (name,)).fetchone()
    if row is None:
        raise HTTPException(404, "Code not found")
    return {"name": name, "pulses_us": json.loads(row[0]), "updated_at": row[1]}


@contextmanager
def hardware(lock):
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "Hardware is busy; retry after the current operation")
    try:
        yield
    except HTTPException:
        raise
    except Exception:
        log.exception("Hardware operation failed")
        raise HTTPException(503, "Hardware unavailable; check journalctl and device configuration") from None
    finally:
        lock.release()


def read_environment(address: str | None = None):
    import cgsensor
    address = int(address or os.environ.get("RPZ_BME280_ADDRESS", "0x77"), 0)
    sensor = cgsensor.BME280(i2c_addr=address)
    try:
        if not sensor.check_id():
            raise RuntimeError("BME280 ID mismatch")
        sensor.forced()
        return {"temperature_c": sensor.temperature, "humidity_percent": sensor.humidity,
                "pressure_hpa": sensor.pressure, "address": hex(address)}
    finally:
        sensor.bus.close()


def read_light():
    import cgsensor
    sensor = cgsensor.TSL2572()
    try:
        if not sensor.check_id():
            raise RuntimeError("TSL2572 ID mismatch; this service requires RPZ-IR-Sensor Rev2")
        sensor.single_auto_measure()
        return {"illuminance_lux": sensor.illuminance, "sensor": "TSL2572"}
    finally:
        sensor.bus.close()


def infrared():
    import cgir
    return cgir.Infrared(dev_send=os.environ.get("RPZ_IR_TX", "/dev/lirc0"),
                         dev_rec=os.environ.get("RPZ_IR_RX", "/dev/lirc1"))


def record_ir(timeout):
    # cgir.record resets its timeout per packet. Use an absolute deadline and
    # unbuffered reads so noise cannot keep an HTTP request alive indefinitely.
    import cgir
    receiver = infrared()
    deadline = time.monotonic() + timeout
    pulses = []
    with open(receiver.dev_rec, "rb", buffering=0) as fd:
        features = receiver.lirc_get_features(fd)
        if not receiver.lirc_can_rec_mode2(features) or not receiver.lirc_can_set_timeout(features):
            raise RuntimeError("LIRC receiver does not support mode2/timeout")
        receiver.lirc_set_rec_mode(fd, cgir.infrared.LIRC_MODE_MODE2)
        receiver.lirc_set_rec_timeout(fd, receiver.rec_max_gap_us)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], max(0, remaining))[0]:
                raise HTTPException(408, "No complete IR code received within timeout")
            data = fd.read(4)
            if len(data) != 4:
                raise RuntimeError("Incomplete LIRC packet")
            packet = struct.unpack("=I", data)[0]
            kind, duration = packet >> 24, packet & 0xFFFFFF
            if kind == 3 and pulses:
                break
            if kind == 4:
                raise HTTPException(422, "IR receiver overflow; retry recording")
            if (kind == 1 and len(pulses) % 2 == 0) or (kind == 0 and len(pulses) % 2 == 1):
                pulses.append(duration)
            if len(pulses) > MAX_PULSES or sum(pulses) > 1_000_000:
                raise HTTPException(422, "IR code exceeds supported size")
    if len(pulses) % 2 == 0:
        pulses.pop()  # trailing gap is not transmitted
    try:
        return Code(pulses_us=pulses)
    except ValueError:
        raise HTTPException(422, "IR code contains unsupported durations") from None


@app.get("/health")
def health():
    return {"status": "ok", "time": timestamp(), "configured": bool(os.environ.get("RPZ_API_KEY")),
            "devices": {"i2c": Path("/dev/i2c-1").exists(),
                        "ir_tx": Path(os.environ.get("RPZ_IR_TX", "/dev/lirc0")).exists(),
                        "ir_rx": Path(os.environ.get("RPZ_IR_RX", "/dev/lirc1")).exists()}}


@app.get("/sensors", dependencies=[Depends(authorize)])
def sensors():
    with hardware(sensor_lock):
        return {"measured_at": timestamp(), **read_environment(), **read_light()}


@app.get("/sensors/environment", dependencies=[Depends(authorize)])
def environment(address: Literal["0x76", "0x77"] | None = None):
    with hardware(sensor_lock):
        return {"measured_at": timestamp(), **read_environment(address)}


@app.get("/sensors/light", dependencies=[Depends(authorize)])
def light():
    with hardware(sensor_lock):
        return {"measured_at": timestamp(), **read_light()}


def reading(row):
    return {"measured_at": datetime.fromtimestamp(row[0], UTC).isoformat(),
            **{f: None if v is None else round(v, 2) for f, v in zip(READING_FIELDS, row[1:])}}


@app.get("/sensors/history", dependencies=[Depends(authorize)])
def history(hours: float = Query(24, gt=0, le=24 * 366), points: int = Query(360, ge=10, le=2000)):
    """Recorded readings averaged into at most `points` buckets; `measured_at` is the bucket start."""
    span = int(hours * 3600)
    bucket = max(1, -(-span // points))
    with connection() as conn:
        rows = conn.execute("SELECT ts / ? * ? AS t, AVG(temperature_c), AVG(humidity_percent), AVG(pressure_hpa), "
                            "AVG(illuminance_lux) FROM readings WHERE ts >= ? GROUP BY t ORDER BY t",
                            (bucket, bucket, int(time.time()) - span)).fetchall()
        latest = conn.execute("SELECT * FROM readings ORDER BY ts DESC LIMIT 1").fetchone()
    return {"bucket_seconds": bucket, "latest": latest and reading(latest), "readings": [reading(r) for r in rows]}


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/dashboard")


@app.get("/dashboard", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC / "dashboard.html")


@app.post("/ir/receive", dependencies=[Depends(authorize)])
def receive(request: Receive):
    with hardware(ir_lock):
        code = record_ir(request.timeout_seconds)
        if request.name:
            save_code(request.name, code)
        return {"received_at": timestamp(), "name": request.name,
                "signal": "short" if len(code.pulses_us) <= 10 else "full",
                **code.model_dump()}


@app.post("/ir/send", dependencies=[Depends(authorize)])
def send(code: Code):
    with hardware(ir_lock):
        infrared().send(code.pulses_us)
    return {"status": "sent", "sent_at": timestamp(), "carrier_hz": 38000}


@app.get("/ir/codes", dependencies=[Depends(authorize)])
def codes():
    with connection() as conn:
        rows = conn.execute("SELECT name, updated_at FROM codes ORDER BY name").fetchall()
    return {"codes": [{"name": n, "updated_at": t} for n, t in rows]}


def valid_name(name: str):
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise HTTPException(422, "Code name must contain 1-64 letters, digits, underscores or hyphens")
    return name


@app.get("/ir/codes/{name}", dependencies=[Depends(authorize)])
def code_detail(name: str):
    return get_code(valid_name(name))


@app.put("/ir/codes/{name}", dependencies=[Depends(authorize)])
def put_code(name: str, code: Code):
    save_code(valid_name(name), code)
    return get_code(name)


@app.delete("/ir/codes/{name}", dependencies=[Depends(authorize)])
def delete_code(name: str):
    with connection() as conn:
        count = conn.execute("DELETE FROM codes WHERE name=?", (valid_name(name),)).rowcount
    if not count:
        raise HTTPException(404, "Code not found")
    return {"status": "deleted", "name": name}


@app.post("/ir/codes/{name}/send", dependencies=[Depends(authorize)])
def send_saved(name: str):
    code = Code(pulses_us=get_code(valid_name(name))["pulses_us"])
    return {"name": name, **send(code)}
