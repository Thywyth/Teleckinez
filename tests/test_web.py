import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from teleckinez.app import App
from teleckinez.core.config import CONFIG_DIR, DispatcherSettings, Settings, load_devices
from teleckinez.core.events import CommandStatusChanged
from teleckinez.core.models import CommandStatus, Intent
from teleckinez.transport.messages import AckReport, AvailabilityReport, StateReport
from teleckinez.web.app import create_app
from teleckinez.web.serialize import event_to_dict, jsonable


class InstantHome:
    """Stands in for MQTT: every device acks and applies commands at once."""

    def __init__(self, core: App) -> None:
        self.core, self.seq = core, 0

    def is_connected(self) -> bool:
        return True

    async def send(self, device_id, cmd_id, desired, ttl_s) -> None:
        asyncio.get_running_loop().call_soon(self.apply, device_id, cmd_id, desired)

    def feed(self, message) -> None:
        self.core.twins.on_message(message)
        self.core.dispatcher.on_device_message(message)

    def apply(self, device_id, cmd_id, desired) -> None:
        self.feed(AckReport(device_id, cmd_id, "accepted"))
        self.seq += 1
        state = {**self.core.twins.get(device_id).reported, **desired}
        self.feed(StateReport(device_id, state, None, self.seq))


@pytest.fixture
def core(tmp_path):
    settings = Settings(
        dispatcher=DispatcherSettings(ack_timeout_s=0.2, state_timeout_s={"default": 0.5})
    )
    settings.storage.path = tmp_path / "web.db"
    app = App(settings, load_devices(CONFIG_DIR / "devices.yaml"))
    home = InstantHome(app)
    app.dispatcher.sender = home
    for dev in app.registry:
        home.feed(AvailabilityReport(dev.id, True))
        home.seq += 1
        home.feed(StateReport(dev.id, dev.type.initial_state(), None, home.seq))
    yield app
    app.storage.close()


@pytest.fixture
async def client(core):
    transport = httpx.ASGITransport(app=create_app(core))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_status(client) -> None:
    body = (await client.get("/api/status")).json()
    assert body["devices"] == 31 and body["online"] == 31
    assert body["voice_available"] is False and body["broker_connected"] is False


async def test_devices_with_rooms_and_twin(client) -> None:
    body = (await client.get("/api/devices")).json()
    assert len(body["rooms"]) == 8 and body["rooms"][0] == {"id": "living_room", "name": "Вітальня"}
    light = next(d for d in body["devices"] if d["id"] == "light.kitchen_main")
    assert light["title"] == "Світло на кухні" and light["availability"] == "online"
    assert light["reported"] == {"on": False, "brightness": 100}
    assert light["capabilities"]["brightness"]["max"] == 100
    assert "turn_on" in light["actions"]


async def test_manual_command_goes_through_dispatcher(client, core) -> None:
    r = await client.post(
        "/api/devices/light.kitchen_main/command",
        json={"action": "set", "params": {"brightness": 40}, "wait": True},
    )
    body = r.json()
    assert r.status_code == 200 and body["status"] == "completed"
    assert body["desired"] == {"on": True, "brightness": 40}
    assert body["response"] == "Світло на кухні: яскравість 40 %."
    assert core.twins.get("light.kitchen_main").reported["brightness"] == 40


async def test_manual_command_validation(client) -> None:
    assert (
        await client.post("/api/devices/nope.x/command", json={"action": "turn_on"})
    ).status_code == 404
    r = await client.post(
        "/api/devices/light.kitchen_main/command",
        json={"action": "set", "params": {"brightness": 400}, "wait": True},
    )
    assert r.json()["status"] == "rejected_local"
    assert (
        await client.post("/api/devices/light.kitchen_main/command", json={})
    ).status_code == 422


async def test_text_command_same_path_as_voice(client) -> None:
    r = await client.post("/api/text-command", json={"text": "відкрий штори у спальні"})
    assert r.json() == {"response": "Штори у спальні відкрито."}
    r = await client.post("/api/text-command", json={"text": "розкажи анекдот"})
    assert r.json() == {"response": "Вибачте, я не зрозумів команду."}


async def test_command_journal(client) -> None:
    await client.post("/api/text-command", json={"text": "увімкни світло на кухні"})
    await client.post("/api/text-command", json={"text": "вимкни світло у спальні"})
    rows = (await client.get("/api/commands?limit=10")).json()
    assert [r["device_id"] for r in rows] == ["light.bedroom_main", "light.kitchen_main"]
    assert rows[0]["status"] == "completed" and rows[0]["t_ack"] is not None
    rows = (await client.get("/api/commands?device_id=light.kitchen_main")).json()
    assert len(rows) == 1


async def test_ptt_without_voice_pipeline(client) -> None:
    r = await client.post("/api/voice/ptt", json={"pressed": True})
    assert r.status_code == 503


async def test_get_settings(client) -> None:
    body = (await client.get("/api/settings")).json()
    assert body["settings"]["dispatcher"]["ack_timeout_s"] == 0.2
    assert "password" not in body["settings"]["mqtt"]
    assert "dispatcher" in body["hot"] and "mqtt" in body["restart"]


async def test_put_settings_applies_hot_values(client, core) -> None:
    r = await client.put(
        "/api/settings",
        json={
            "dispatcher": {"ack_timeout_s": 1.5, "max_attempts": 2},
            "nlu": {"min_confidence": 0.7},
            "health": {"stale_factor": 4},
            "language": "en",
            "web": {"port": 9000},
        },
    )
    body = r.json()
    assert r.status_code == 200
    assert set(body["applied"]) == {
        "dispatcher.ack_timeout_s", "dispatcher.max_attempts", "nlu.min_confidence",
        "health.stale_factor", "language",
    }  # fmt: skip
    assert body["restart_required"] == ["web.port"]
    # the very objects the components use were updated
    assert core.dispatcher.settings.ack_timeout_s == 1.5
    assert core.assistant.settings.max_attempts == 2
    assert core.parser.min_confidence == 0.7 and core.health.stale_factor == 4
    assert core.settings.web.port == 8000  # not applied without restart
    r = await client.post("/api/text-command", json={"text": "розкажи анекдот"})
    assert r.json()["response"] == "Вибачте, я не зрозумів команду."  # uk text -> uk answer
    devices = (await client.get("/api/devices")).json()
    assert devices["rooms"][0]["name"] == "Living room"


async def test_put_settings_rejects_invalid_and_changes_nothing(client, core) -> None:
    r = await client.put(
        "/api/settings", json={"dispatcher": {"ack_timeout_s": 1.0, "max_attempts": 0}}
    )
    assert r.status_code == 422 and "max_attempts" in r.json()["detail"]
    assert core.settings.dispatcher.ack_timeout_s == 0.2
    r = await client.put("/api/settings", json={"nlu": {"mode": "hybrid"}})
    assert r.status_code == 422 and "етапі 4" in r.json()["detail"]


def test_websocket_streams_bus_events(core) -> None:
    with TestClient(create_app(core)) as tc, tc.websocket_connect("/ws") as ws:
        tc.post("/api/text-command", json={"text": "яка температура у спальні"})
        types = []
        while "SpeakRequested" not in types:
            message = ws.receive_json()
            types.append(message["type"])
            assert set(message) == {"type", "ts", "data"}
        assert types[0] == "TranscriptReady" and "IntentParsed" in types
        assert message["data"]["text"] == "Датчик у спальні: температура 21 °C."


def test_event_serialization() -> None:
    event = CommandStatusChanged(
        cmd_id="c", device_id="d", old=None, new=CommandStatus.ACCEPTED, ts=5.0
    )
    assert event_to_dict(event) == {
        "type": "CommandStatusChanged",
        "ts": 5.0,
        "data": {
            "session_id": None,
            "cmd_id": "c",
            "device_id": "d",
            "old": None,
            "new": "accepted",
            "attempts": 0,
            "error": None,
        },
    }
    assert jsonable(Intent(kind="query", params={"a": (1, 2)}))["params"] == {"a": [1, 2]}


async def test_static_frontend_is_served(client) -> None:
    index = await client.get("/")
    assert index.status_code == 200 and "Teleckinez" in index.text
    for asset in ("/static/app.js", "/static/style.css"):
        assert (await client.get(asset)).status_code == 200
    assert "cdn" not in index.text.lower()  # works offline (web.md)


def test_frontend_javascript_parses() -> None:
    """Catches syntax errors in app.js before a browser does (needs Node.js, else skipped)."""
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    script = Path(__file__).resolve().parents[1] / "src/teleckinez/web/static/app.js"
    result = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
