"""Web panel backend: REST API + WebSocket bridge to the EventBus (docs/specs/web.md, ADR-008).

The API is a thin layer over the same components the voice path uses: a
manual command from a card goes through the dispatcher, a typed command
through the assistant. The frontend loads /api/devices once and then lives
on /ws events.
"""

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from teleckinez import __version__
from teleckinez.assistant import new_session_id
from teleckinez.core.models import Availability, DeviceCommand
from teleckinez.transport.mqtt_client import MqttNotConnected
from teleckinez.web.serialize import device_to_dict, event_to_dict, record_to_dict, room_name

if TYPE_CHECKING:
    from teleckinez.app import App

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class CommandBody(BaseModel):
    action: str = Field(min_length=1)
    params: dict[str, Any] = {}
    wait: bool = False  # wait for the final status (otherwise follow /ws)


class TextBody(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class PttBody(BaseModel):
    pressed: bool


SIM_FAULTS = ("offline", "freeze", "drop_rate", "stuck", "slow", "reject", "drift", "clear")


class FaultBody(BaseModel):
    device_id: str = Field(min_length=1)  # or "*" for every device
    fault: str
    params: dict[str, Any] = {}
    enabled: bool = True


def create_app(core: "App") -> FastAPI:
    # All endpoints are `async def`: they run on the event loop thread, like the rest of
    # the assistant (sqlite3 connections and twins are not meant for worker threads).
    api = FastAPI(title="Teleckinez", version=__version__, docs_url="/api/docs")

    def lang() -> str:  # can change at runtime via PUT /api/settings
        return core.settings.language

    # ------------------------------------------------------------------ status

    @api.get("/api/status")
    async def status() -> dict[str, Any]:
        twins = list(core.twins)
        return {
            "version": __version__,
            "broker_connected": core.gateway.is_connected(),
            "devices": len(twins),
            "online": sum(t.availability is Availability.ONLINE for t in twins),
            "voice_available": core.voice is not None,
            "language": lang(),
        }

    # ----------------------------------------------------------------- devices

    @api.get("/api/devices")
    async def devices() -> dict[str, Any]:
        now = core.twins.clock()
        reg = core.registry
        return {
            "rooms": [{"id": r, "name": room_name(reg, r, lang())} for r in reg.rooms],
            "devices": [device_to_dict(d, core.twins.get(d.id), now, lang()) for d in reg],
        }

    @api.post("/api/devices/{device_id}/command")
    async def device_command(device_id: str, body: CommandBody) -> dict[str, Any]:
        if device_id not in core.registry:
            raise HTTPException(404, f"unknown device {device_id}")
        group = await core.dispatcher.dispatch(
            [DeviceCommand(device_id, body.action, body.params)], session_id=new_session_id()
        )
        if body.wait:
            await group.wait(120)
        record = group.records[0]
        out = record_to_dict(record)
        if record.is_terminal:
            out["response"] = core.composer.command_result(group.records, lang())
        return out

    # ------------------------------------------------------------- assistant

    @api.post("/api/text-command")
    async def text_command(body: TextBody) -> dict[str, str]:
        """Same path as speech: TranscriptReady -> NLU -> dispatcher -> response."""
        return {"response": await core.assistant.ask(body.text.strip())}

    @api.post("/api/voice/ptt")
    async def voice_ptt(body: PttBody) -> dict[str, Any]:
        if core.voice is None:
            raise HTTPException(503, "голосовий конвеєр не запущено")
        await core.voice.push_to_talk(body.pressed)
        return {"pressed": body.pressed}

    # ---------------------------------------------------------------- journal

    @api.get("/api/commands")
    async def commands(limit: int = 50, device_id: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        if core.storage is not None:
            return core.storage.commands.recent(limit, device_id)
        records = [r for r in core.tracker.recent(500) if not device_id or r.device_id == device_id]
        return [record_to_dict(r) for r in records[:limit]]

    @api.get("/api/sim/faults")
    async def get_faults() -> dict[str, Any]:
        return {"faults": core.sim_faults, "available": list(SIM_FAULTS)}

    @api.post("/api/sim/faults")
    async def post_fault(body: FaultBody) -> dict[str, Any]:
        """Inject a fault in the simulator (devices.md §4); the result arrives via /ws."""
        if body.fault not in SIM_FAULTS:
            raise HTTPException(422, f"unknown fault {body.fault!r}")
        if body.device_id != "*" and body.device_id not in core.registry:
            raise HTTPException(404, f"unknown device {body.device_id}")
        try:
            await core.send_fault(body.device_id, body.fault, body.params, body.enabled)
        except MqttNotConnected as exc:
            raise HTTPException(503, f"немає зв\x27язку з брокером: {exc}") from None
        return {"sent": True}

    @api.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return {
            "settings": core.settings.model_dump(mode="json"),
            "hot": ["language", *core.HOT_SECTIONS],
            "restart": list(core.RESTART_SECTIONS),
            "voice_available": core.voice is not None,
        }

    @api.put("/api/settings")
    async def put_settings(patch: dict[str, Any]) -> dict[str, Any]:
        """Partial update {section: {field: value}}; applied at once where possible."""
        try:
            result = core.apply_settings(patch)
        except ValidationError as exc:
            errors = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
            raise HTTPException(422, "; ".join(errors)) from None
        return {**result, "settings": core.settings.model_dump(mode="json")}

    # --------------------------------------------------------------- websocket

    @api.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        stream = core.bus.stream()

        async def pump() -> None:
            async for event in stream:
                await socket.send_json(event_to_dict(event))

        sender = asyncio.create_task(pump())
        try:
            while True:  # we do not expect messages; this detects the disconnect
                await socket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender
            await stream.aclose()

    # ------------------------------------------------------------------ static

    if STATIC.is_dir():
        api.mount("/static", StaticFiles(directory=STATIC), name="static")

        @api.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(STATIC / "index.html")

    return api
