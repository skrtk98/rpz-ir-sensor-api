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
    monkeypatch.setattr(api, "read_environment", lambda: {"temperature_c": 24})
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


def test_sampling_records_history(client, monkeypatch):
    monkeypatch.setattr(api, "read_environment", lambda: {"temperature_c": 24.123, "humidity_percent": 50,
                                                          "pressure_hpa": 1013, "address": "0x76"})
    monkeypatch.setattr(api, "read_light", lambda: {"illuminance_lux": 100, "sensor": "TSL2572"})
    api.sample_once()
    result = client.get("/sensors/history", params={"hours": 1}).json()
    assert result["latest"]["temperature_c"] == 24.12
    assert result["readings"][0]["illuminance_lux"] == 100
    assert client.get("/sensors/history", headers={"X-API-Key": "wrong"}).status_code == 401


def test_failed_sensor_is_stored_as_null(client, monkeypatch):
    def broken():
        raise OSError("device missing")

    monkeypatch.setattr(api, "read_environment", broken)
    monkeypatch.setattr(api, "read_light", lambda: {"illuminance_lux": 5})
    api.sample_once()
    latest = client.get("/sensors/history").json()["latest"]
    assert latest["temperature_c"] is None and latest["illuminance_lux"] == 5
    monkeypatch.setattr(api, "read_light", broken)
    monkeypatch.setattr(api.time, "time", lambda: 2_000_000_000)
    api.sample_once()
    assert client.get("/sensors/history").json()["latest"]["illuminance_lux"] == 5
    assert not api.sensor_lock.locked()


def test_history_buckets_and_retention(client, monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(api.time, "time", lambda: now)
    monkeypatch.setenv("RPZ_RETENTION_DAYS", "1")
    with api.connection() as conn:
        conn.executemany("INSERT INTO readings VALUES (?, ?, NULL, NULL, NULL)",
                         [(now - 3 * 86400, 0)] + [(now - 3600 + i * 60, i) for i in range(60)])
    result = client.get("/sensors/history", params={"hours": 1, "points": 10}).json()
    assert result["bucket_seconds"] == 360
    assert len(result["readings"]) <= 11
    assert sum(r["temperature_c"] for r in result["readings"]) > 0
    monkeypatch.setattr(api, "read_environment", lambda: {"temperature_c": 20})
    monkeypatch.setattr(api, "read_light", dict)
    api.sample_once()
    with api.connection() as conn:
        assert conn.execute("SELECT MIN(ts) FROM readings").fetchone()[0] > now - 86400


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
