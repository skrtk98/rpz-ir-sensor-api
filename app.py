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
ENVIRONMENT_FIELDS = ("temperature_c", "humidity_percent", "pressure_hpa")
BME280_ADDRESSES = ("0x76", "0x77")
LIGHT_ADDRESS = "0x39"  # TSL2572 on the board
DEFAULT_LABELS = {"0x76": "外付け", "0x77": "基板上", LIGHT_ADDRESS: "基板上"}
STATIC = Path(__file__).parent / "static"


def check_key(key: str | None, *names: str) -> None:
    if not os.environ.get("RPZ_API_KEY"):
        raise HTTPException(503, "RPZ_API_KEY is not configured")
    expected = [os.environ[n].encode() for n in names if os.environ.get(n)]
    if key is None or not any(hmac.compare_digest(key.encode(), e) for e in expected):
        raise HTTPException(401, "Invalid API key")


def authorize(key: str | None = Depends(api_key)) -> None:
    """Full access, including IR transmission and saved-code changes."""
    check_key(key, "RPZ_API_KEY")


def authorize_read(key: str | None = Depends(api_key)) -> None:
    """Sensor readings only; RPZ_READ_API_KEY is meant for the dashboard."""
    check_key(key, "RPZ_API_KEY", "RPZ_READ_API_KEY")


def configured_labels() -> dict[str, str]:
    """Parse RPZ_SENSOR_LABELS, e.g. "0x76=屋外,0x77=室内,0x39=室内", keeping the configured order."""
    labels = {}
    for item in filter(None, (part.strip() for part in os.environ.get("RPZ_SENSOR_LABELS", "").split(","))):
        address, _, label = item.partition("=")
        try:
            address = hex(int(address.strip(), 0))
        except ValueError:
            address = ""
        if address not in DEFAULT_LABELS or not label.strip():
            raise ValueError(f"Invalid RPZ_SENSOR_LABELS entry {item!r}; use 0x76, 0x77 or 0x39 with a label")
        labels[address] = label.strip()
    return labels


def label_of(address: str) -> str:
    return configured_labels().get(address, DEFAULT_LABELS[address])


def sensor_catalog() -> list[dict]:
    """Recorded sensors: every labelled BME280 (or RPZ_BME280_ADDRESS if none) plus the light sensor."""
    labels = configured_labels()
    addresses = [a for a in labels if a in BME280_ADDRESSES]
    addresses = addresses or [hex(int(os.environ.get("RPZ_BME280_ADDRESS", "0x77"), 0))]
    names = [labels.get(a, DEFAULT_LABELS[a]) for a in addresses]
    if len(set(names)) != len(names):
        raise ValueError("Each BME280 needs its own label in RPZ_SENSOR_LABELS")
    return [*({"address": a, "type": "BME280", "label": n, "metrics": list(ENVIRONMENT_FIELDS)}
              for a, n in zip(addresses, names)),
            {"address": LIGHT_ADDRESS, "type": "TSL2572", "label": labels.get(LIGHT_ADDRESS, DEFAULT_LABELS[LIGHT_ADDRESS]),
             "metrics": ["illuminance_lux"]}]


def migrate_readings(conn):
    """Move rows from the single-sensor table used before labels; they came from RPZ_BME280_ADDRESS."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='readings'").fetchone() is None:
        return
    address = hex(int(os.environ.get("RPZ_BME280_ADDRESS", "0x77"), 0))
    conn.execute("INSERT OR IGNORE INTO environment_readings SELECT ts, ?, temperature_c, humidity_percent, pressure_hpa "
                 "FROM readings WHERE COALESCE(temperature_c, humidity_percent, pressure_hpa) IS NOT NULL", (address,))
    conn.execute("INSERT OR IGNORE INTO light_readings SELECT ts, illuminance_lux FROM readings "
                 "WHERE illuminance_lux IS NOT NULL")
    conn.execute("DROP TABLE readings")


def sample_once():
    """Record every sensor in the catalog. A failed sensor is skipped for this round."""
    catalog = sensor_catalog()
    if not sensor_lock.acquire(timeout=10):
        log.warning("Sampling skipped: sensors busy")
        return
    measured = int(time.time())
    environments, light = [], None
    try:
        for sensor in catalog:
            try:
                if sensor["type"] == "BME280":
                    values = read_environment(sensor["address"])
                    environments.append((measured, sensor["address"], *(values[f] for f in ENVIRONMENT_FIELDS)))
                else:
                    light = read_light()["illuminance_lux"]
            except (OSError, RuntimeError) as error:  # I2C errors and ID mismatches
                log.warning("Sampling %s at %s failed: %s", sensor["type"], sensor["address"], error)
    finally:
        sensor_lock.release()
    retention = float(os.environ.get("RPZ_RETENTION_DAYS", "90"))
    with connection() as conn:
        conn.executemany("INSERT OR REPLACE INTO environment_readings VALUES (?, ?, ?, ?, ?)", environments)
        if light is not None:
            conn.execute("INSERT OR REPLACE INTO light_readings VALUES (?, ?)", (measured, light))
        for table in ("environment_readings", "light_readings"):
            conn.execute(f"DELETE FROM {table} WHERE ts < ?", (measured - int(retention * 86400),))


def sampler(stop: threading.Event, interval: float):
    while not stop.is_set():
        try:
            sample_once()
        except Exception:
            log.exception("Sampling failed")
        stop.wait(interval)


@asynccontextmanager
async def lifespan(app):
    sensor_catalog()  # fail at startup on a bad RPZ_SENSOR_LABELS
    with connection() as conn:
        migrate_readings(conn)
    interval = float(os.environ.get("RPZ_SAMPLE_SECONDS", "60"))
    stop = threading.Event()
    thread = threading.Thread(target=sampler, args=(stop, interval), name="sampler", daemon=True)
    if interval > 0:
        thread.start()
    yield
    stop.set()
    if thread.is_alive():
        thread.join(timeout=15)


app = FastAPI(title="RPZ-IR-Sensor API", version="1.2.0", lifespan=lifespan,
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
            conn.execute("CREATE TABLE IF NOT EXISTS environment_readings (ts INTEGER NOT NULL, address TEXT NOT NULL, "
                         "temperature_c REAL, humidity_percent REAL, pressure_hpa REAL, PRIMARY KEY (ts, address))")
            conn.execute("CREATE TABLE IF NOT EXISTS light_readings (ts INTEGER PRIMARY KEY, illuminance_lux REAL)")
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


@app.get("/sensors", dependencies=[Depends(authorize_read)])
def sensors():
    with hardware(sensor_lock):
        result = {"measured_at": timestamp(), **read_environment(), **read_light()}
    return {**result, "labels": {"environment": label_of(result["address"]), "light": label_of(LIGHT_ADDRESS)}}


@app.get("/sensors/labels", dependencies=[Depends(authorize_read)])
def labels():
    """Recorded sensors with their I2C address, label and metrics (set by RPZ_SENSOR_LABELS)."""
    return {"sensors": sensor_catalog()}


@app.get("/sensors/environment", dependencies=[Depends(authorize_read)])
def environment(address: Literal["0x76", "0x77"] | None = None):
    with hardware(sensor_lock):
        result = {"measured_at": timestamp(), **read_environment(address)}
    return {**result, "label": label_of(result["address"])}


@app.get("/sensors/light", dependencies=[Depends(authorize_read)])
def light():
    with hardware(sensor_lock):
        result = {"measured_at": timestamp(), **read_light()}
    return {**result, "address": LIGHT_ADDRESS, "label": label_of(LIGHT_ADDRESS)}


def reading(row, metrics):
    return {"measured_at": datetime.fromtimestamp(row[0], UTC).isoformat(),
            **{f: None if v is None else round(v, 2) for f, v in zip(metrics, row[1:])}}


@app.get("/sensors/history", dependencies=[Depends(authorize_read)])
def history(hours: float = Query(24, gt=0, le=24 * 366), points: int = Query(360, ge=10, le=2000)):
    """Recorded readings per sensor, averaged into at most `points` buckets; `measured_at` is the bucket start."""
    span = int(hours * 3600)
    bucket = max(1, -(-span // points))
    since = int(time.time()) - span
    sensors = []
    with connection() as conn:
        for sensor in sensor_catalog():
            metrics = sensor["metrics"]
            if sensor["type"] == "BME280":
                table, where, args = "environment_readings", "address = ? AND", (sensor["address"],)
            else:
                table, where, args = "light_readings", "", ()
            averages = ", ".join(f"AVG({m})" for m in metrics)
            rows = conn.execute(f"SELECT ts / ? * ? AS t, {averages} FROM {table} WHERE {where} ts >= ? "
                                "GROUP BY t ORDER BY t", (bucket, bucket, *args, since)).fetchall()
            latest = conn.execute(f"SELECT ts, {', '.join(metrics)} FROM {table} {'WHERE address = ?' if args else ''} "
                                  "ORDER BY ts DESC LIMIT 1", args).fetchone()
            sensors.append({**sensor, "latest": latest and reading(latest, metrics),
                            "readings": [reading(r, metrics) for r in rows]})
    return {"bucket_seconds": bucket, "sensors": sensors}


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
