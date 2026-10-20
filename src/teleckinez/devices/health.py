"""Health monitoring: online / stale / offline (dispatcher.md §5).

`offline` comes immediately from the availability topic (Last Will). `stale`
is detected here: the connection formally exists, but the device has not
sent anything (not even a heartbeat) for stale_factor x heartbeat_s - the
`freeze` failure. Any later message makes it `online` again (TwinStore).
"""

import asyncio
import logging

from teleckinez.core.models import Availability
from teleckinez.devices.twin import TwinStore

log = logging.getLogger(__name__)


class HealthMonitor:
    def __init__(
        self, twins: TwinStore, *, stale_factor: float = 3.0, check_interval_s: float = 1.0
    ) -> None:
        self.twins = twins
        self.stale_factor = stale_factor
        self.check_interval_s = check_interval_s

    def check(self) -> list[str]:
        """Mark silent devices as stale; return their ids."""
        now = self.twins.clock()
        stale = []
        for twin in self.twins:
            if twin.availability is not Availability.ONLINE or twin.last_seen is None:
                continue
            if now - twin.last_seen > self.stale_factor * twin.heartbeat_s:
                self.twins.set_availability(twin.id, Availability.STALE)
                stale.append(twin.id)
        return stale

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.check_interval_s)
            self.check()
