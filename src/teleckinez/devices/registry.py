"""Device registry: the catalogue of rooms, devices and their synonyms."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from teleckinez.core.config import DeviceConfig, DevicesConfig, RoomConfig
from teleckinez.core.text import normalize_name
from teleckinez.devices.types import DEVICE_TYPES, DeviceType


@dataclass(frozen=True)
class Device:
    config: DeviceConfig
    type: DeviceType
    room_config: RoomConfig

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def room(self) -> str:
        return self.config.room

    @property
    def critical(self) -> bool:
        return self.config.critical

    @property
    def plural(self) -> bool:
        return self.config.plural

    @property
    def groups(self) -> list[str]:
        return self.config.groups

    def title(self, lang: str = "uk") -> str:
        """Display name for responses: "Світло на кухні" / "Kitchen light"."""
        if title := self.config.title.get(lang):
            return title
        names = self.config.names.get(lang) or self.config.names.all()
        loc = self.room_config.loc.get(lang) or ""
        return f"{names[0].capitalize()} {loc}".strip()


class UnknownDeviceType(ValueError):
    pass


class DeviceRegistry:
    def __init__(
        self, config: DevicesConfig, types: Mapping[str, DeviceType] = DEVICE_TYPES
    ) -> None:
        unknown = sorted({d.type for d in config.devices} - set(types))
        if unknown:
            raise UnknownDeviceType(f"unknown device type(s): {', '.join(unknown)}")
        self.config = config
        self._devices = {
            d.id: Device(d, types[d.type], config.rooms[d.room]) for d in config.devices
        }

    # ------------------------------------------------------------- collection

    def __iter__(self) -> Iterator[Device]:
        return iter(self._devices.values())

    def __len__(self) -> int:
        return len(self._devices)

    def __contains__(self, device_id: object) -> bool:
        return device_id in self._devices

    def get(self, device_id: str) -> Device:
        return self._devices[device_id]

    # ----------------------------------------------------------------- rooms

    @property
    def rooms(self) -> Mapping[str, RoomConfig]:
        return self.config.rooms

    def room_loc(self, room: str, lang: str = "uk") -> str:
        """Locative form for responses: "на кухні" / "in the kitchen"."""
        cfg = self.rooms[room]
        return cfg.loc.get(lang) or (cfg.get(lang) or cfg.all())[0]

    # ---------------------------------------------------------------- search

    def find(
        self,
        *,
        room: str | None = None,
        type: str | None = None,
        name: str | None = None,
        group: str | None = None,
        capability: str | None = None,
        action: str | None = None,
    ) -> list[Device]:
        """Devices matching all given criteria (in devices.yaml order)."""
        wanted = normalize_name(name) if name else None
        out = []
        for dev in self._devices.values():
            if room and dev.room != room:
                continue
            if type and dev.type.name != type:
                continue
            if wanted and wanted not in dev.config.names.all():
                continue
            if group and group not in dev.groups:
                continue
            if capability and not dev.type.has(capability):
                continue
            if action and not dev.type.supports(action):
                continue
            out.append(dev)
        return out

    def names(self) -> dict[str, list[Device]]:
        """Every synonym -> devices that use it (a name may repeat across rooms)."""
        index: dict[str, list[Device]] = {}
        for dev in self._devices.values():
            for name in dev.config.names.all():
                index.setdefault(name, []).append(dev)
        return index
