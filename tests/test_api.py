import os

import pytest
from fastapi.testclient import TestClient

import app as api


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("RPZ_API_KEY", "test-key")
    monkeypatch.setenv("RPZ_READ_API_KEY", "read-key")
    monkeypatch.setenv("RPZ_DB", str(tmp_path / "codes.sqlite3"))
    monkeypatch.setenv("RPZ_SAMPLE_SECONDS", "0")
    with TestClient(api.app, headers={"X-API-Key": "test-key"}) as client:
        yield client


def test_authentication_fails_closed(client, monkeypatch):
    assert client.get("/sensors", headers={"X-API-Key": "wrong"}).status_code == 401
    monkeypatch.delenv("RPZ_API_KEY")
    assert client.get("/sensors").status_code == 503
    assert client.get("/health").status_code == 200


def test_persistence_and_named_send(client, monkeypatch):
    code = {"pulses_us": [560] * 11}
    sent = []

    class Transmitter:
        def send(self, pulses):
            sent.append(pulses)

    monkeypatch.setattr(api, "infrared", Transmitter)
    assert client.put("/ir/codes/tv_power", json=code).status_code == 200
    assert client.get("/ir/codes/tv_power").json()["pulses_us"] == code["pulses_us"]
    assert client.post("/ir/codes/tv_power/send").status_code == 200
    assert sent == [code["pulses_us"]]
    assert client.delete("/ir/codes/tv_power").status_code == 200
    assert client.post("/ir/codes/tv_power/send").status_code == 404


@pytest.mark.parametrize("pulses", [[560] * 12, [True] * 11, [1.5] * 11, [0] * 11, [100001] * 11, [100000] * 11])
def test_unsafe_pulses_rejected(client, pulses):
    assert client.post("/ir/send", json={"pulses_us": pulses}).status_code == 422


def test_ir_busy_does_not_block_sensor_reads(client, monkeypatch):
    monkeypatch.setattr(api, "read_environment", lambda: {"temperature_c": 24, "address": "0x76"})
    monkeypatch.setattr(api, "read_light", lambda: {"illuminance_lux": 100})
    with api.ir_lock:
        assert client.post("/ir/receive", json={"timeout_seconds": 1}).status_code == 409
        assert client.get("/sensors").json()["temperature_c"] == 24


def test_receive_persists_and_timeout_is_reported(client, monkeypatch):
    monkeypatch.setattr(api, "record_ir", lambda timeout: api.Code(pulses_us=[560] * 11))
    result = client.post("/ir/receive", json={"name": "remote", "timeout_seconds": 1})
    assert result.status_code == 200
    assert client.get("/ir/codes/remote").status_code == 200
    assert client.post("/ir/receive", json={"timeout_seconds": 31}).status_code == 422


def test_short_repeat_signal_can_be_received(client, monkeypatch):
    monkeypatch.setattr(api, "record_ir", lambda timeout: api.Code(pulses_us=[9000, 2250, 560]))
    result = client.post("/ir/receive", json={"timeout_seconds": 1})
    assert result.status_code == 200
    assert result.json()["signal"] == "short"
    assert result.json()["pulses_us"] == [9000, 2250, 560]


def test_hardware_failure_releases_lock(client, monkeypatch):
    def broken():
        raise OSError("device missing")

    monkeypatch.setattr(api, "read_light", broken)
    assert client.get("/sensors/light").status_code == 503
    assert not api.sensor_lock.locked()


def test_absolute_receive_deadline(monkeypatch):
    class Receiver:
        dev_rec = "fake"
        rec_max_gap_us = 60000
        lirc_get_features = lambda *args: 0
        lirc_can_rec_mode2 = lambda *args: True
        lirc_can_set_timeout = lambda *args: True
        lirc_set_rec_mode = lambda *args: None
        lirc_set_rec_timeout = lambda *args: None

    class Device:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            return b"\x01\x00\x00\x02"  # noise, not a pulse

    ticks = iter([0, 0.1, 0.5, 1.1])
    monkeypatch.setattr(api, "infrared", Receiver)
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: Device())
    monkeypatch.setattr(api.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(api.select, "select", lambda *args: ([1], [], []))
    with pytest.raises(api.HTTPException) as error:
        api.record_ir(1)
    assert error.value.status_code == 408


def fake_sensors(monkeypatch, environments, light=100):
    """environments maps address -> temperature, or to an exception to raise."""
    def read_environment(address=None):
        address = address or os.environ.get("RPZ_BME280_ADDRESS", "0x77")
        value = environments[address]
        if isinstance(value, Exception):
            raise value
        return {"temperature_c": value, "humidity_percent": 50, "pressure_hpa": 1013, "address": address}

    def read_light():
        if isinstance(light, Exception):
            raise light
        return {"illuminance_lux": light, "sensor": "TSL2572"}

    monkeypatch.setattr(api, "read_environment", read_environment)
    monkeypatch.setattr(api, "read_light", read_light)


def test_labels_select_and_name_recorded_sensors(client, monkeypatch):
    monkeypatch.setenv("RPZ_SENSOR_LABELS", "0x77=室内, 0x76=屋外,0x39=室内")
    monkeypatch.setenv("RPZ_BME280_ADDRESS", "0x76")
    fake_sensors(monkeypatch, {"0x76": 12.345, "0x77": 28})
    catalog = client.get("/sensors/labels", headers={"X-API-Key": "read-key"}).json()["sensors"]
    assert [(s["address"], s["type"], s["label"]) for s in catalog] == [
        ("0x77", "BME280", "室内"), ("0x76", "BME280", "屋外"), ("0x39", "TSL2572", "室内")]
    api.sample_once()
    sensors = {s["address"]: s for s in client.get("/sensors/history", params={"hours": 1}).json()["sensors"]}
    assert sensors["0x76"]["label"] == "屋外"
    assert sensors["0x76"]["latest"]["temperature_c"] == 12.35
    assert sensors["0x77"]["readings"][0]["temperature_c"] == 28
    assert sensors["0x39"]["latest"]["illuminance_lux"] == 100
    assert "illuminance_lux" not in sensors["0x76"]["latest"]
    assert client.get("/sensors/environment", params={"address": "0x76"}).json()["label"] == "屋外"
    assert client.get("/sensors/light").json()["label"] == "室内"
    assert client.get("/sensors").json()["labels"] == {"environment": "屋外", "light": "室内"}


def test_default_catalog_records_configured_address(client, monkeypatch):
    monkeypatch.setenv("RPZ_BME280_ADDRESS", "0x76")
    labels = client.get("/sensors/labels").json()["sensors"]
    assert [(s["address"], s["label"]) for s in labels] == [("0x76", "外付け"), ("0x39", "基板上")]


@pytest.mark.parametrize("value", ["0x40=屋外", "0x76=", "屋外", "0x76=a,0x77=a"])
def test_invalid_labels_are_rejected(monkeypatch, value):
    monkeypatch.setenv("RPZ_SENSOR_LABELS", value)
    with pytest.raises(ValueError):
        api.sensor_catalog()


def test_failed_sensor_is_skipped(client, monkeypatch):
    monkeypatch.setenv("RPZ_SENSOR_LABELS", "0x76=屋外,0x77=室内")
    fake_sensors(monkeypatch, {"0x76": OSError("unplugged"), "0x77": 25}, light=OSError("busy"))
    api.sample_once()
    sensors = {s["address"]: s for s in client.get("/sensors/history").json()["sensors"]}
    assert sensors["0x76"]["latest"] is None and sensors["0x76"]["readings"] == []
    assert sensors["0x77"]["latest"]["temperature_c"] == 25
    assert sensors["0x39"]["latest"] is None
    assert not api.sensor_lock.locked()


def test_history_buckets_and_retention(client, monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(api.time, "time", lambda: now)
    monkeypatch.setenv("RPZ_RETENTION_DAYS", "1")
    monkeypatch.setenv("RPZ_SENSOR_LABELS", "0x76=屋外")
    with api.connection() as conn:
        conn.executemany("INSERT INTO environment_readings VALUES (?, '0x76', ?, NULL, NULL)",
                         [(now - 3 * 86400, 0)] + [(now - 3600 + i * 60, i) for i in range(60)])
    result = client.get("/sensors/history", params={"hours": 1, "points": 10}).json()
    assert result["bucket_seconds"] == 360
    readings = result["sensors"][0]["readings"]
    assert len(readings) <= 11 and sum(r["temperature_c"] for r in readings) > 0
    fake_sensors(monkeypatch, {"0x76": 20})
    api.sample_once()
    with api.connection() as conn:
        assert conn.execute("SELECT MIN(ts) FROM environment_readings").fetchone()[0] > now - 86400


def test_old_readings_are_migrated(monkeypatch, tmp_path):
    monkeypatch.setenv("RPZ_DB", str(tmp_path / "old.sqlite3"))
    monkeypatch.setenv("RPZ_BME280_ADDRESS", "0x76")
    with api.connection() as conn:
        conn.execute("CREATE TABLE readings (ts INTEGER PRIMARY KEY, temperature_c REAL, "
                     "humidity_percent REAL, pressure_hpa REAL, illuminance_lux REAL)")
        conn.executemany("INSERT INTO readings VALUES (?, ?, ?, ?, ?)",
                         [(1, 20, 50, 1013, 5), (2, None, None, None, 6), (3, 21, 51, 1012, None)])
        api.migrate_readings(conn)
        assert conn.execute("SELECT ts, address, temperature_c FROM environment_readings ORDER BY ts").fetchall() == [
            (1, "0x76", 20), (3, "0x76", 21)]
        assert conn.execute("SELECT * FROM light_readings ORDER BY ts").fetchall() == [(1, 5), (2, 6)]
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='readings'").fetchone() is None
        api.migrate_readings(conn)  # idempotent once the old table is gone


def test_sampling_waits_out_busy_sensors(monkeypatch):
    class BusyLock:
        def acquire(self, timeout):
            return False

    monkeypatch.setattr(api, "sensor_lock", BusyLock())
    called = []
    monkeypatch.setattr(api, "read_light", lambda: called.append(1))
    api.sample_once()
    assert not called


def test_dashboard_is_public_and_static(client):
    page = client.get("/dashboard", headers={})
    assert page.status_code == 200 and "uPlot" in page.text
    assert client.get("/static/vendor/uPlot.iife.min.js").status_code == 200
    assert client.get("/", follow_redirects=False).headers["location"] == "/dashboard"


def test_read_key_cannot_control_ir(client, monkeypatch):
    monkeypatch.setattr(api, "read_light", lambda: {"illuminance_lux": 1})
    read = {"X-API-Key": "read-key"}
    assert client.get("/sensors/light", headers=read).status_code == 200
    assert client.get("/sensors/history", headers=read).status_code == 200
    assert client.post("/ir/send", json={"pulses_us": [560]}, headers=read).status_code == 401
    assert client.get("/ir/codes", headers=read).status_code == 401
    assert client.get("/sensors/history", headers={"X-API-Key": b"caf\xe9"}).status_code == 401
    monkeypatch.delenv("RPZ_API_KEY")
    assert client.get("/sensors/history", headers=read).status_code == 503
