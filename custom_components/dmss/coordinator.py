"""Serialize the synchronous DMSS session and maintain inventory/renewal."""
import asyncio
from datetime import timedelta
from functools import partial
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .bell_manager import BellManager
from .client import DMSSClient
from .const import CONF_INCLUDE_UNLISTED_PRIMARY, DOMAIN
from .errors import is_auth_error

_LOGGER = logging.getLogger(__name__)


class DMSSCoordinator(DataUpdateCoordinator):
    """One session and one lock per config entry; all requests run off-loop."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: DMSSClient):
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry,
                         update_interval=timedelta(minutes=10))
        self.entry = entry
        self.client = client
        self.api_lock = asyncio.Lock()
        # Set by config-entry setup. Merely loading inventory does not open media.
        self.media = None
        self.bells = BellManager(self)
        self.include_unlisted_primary = entry.options.get(CONF_INCLUDE_UNLISTED_PRIMARY, True)

    async def async_call(self, method, *args, **kwargs):
        """Media and buttons must use this shared executor/lock boundary."""
        async with self.api_lock:
            return await self.hass.async_add_executor_job(partial(method, *args, **kwargs))

    async def _async_update_data(self):
        try:
            return await self.async_call(self._inventory)
        except Exception as error:
            if is_auth_error(error):
                raise ConfigEntryAuthFailed("DMSS account needs authentication") from None
            raise UpdateFailed("Unable to refresh DMSS inventory") from None

    def _inventory(self):
        self.client.ensure_session()
        devices = self.client.devices()
        targets = []
        for device in devices:
            catalog = str(device.get("deviceCatalog", "")).upper()
            if catalog not in {"VTH", "VTO", "ASC"} and device.get("devType") not in {12, 17, 19, 21}:
                continue
            serial = device["deviceId"]
            channels = device.get("deviceChannels") or []
            if any(row.get("channelCatalog") == "VTO" for row in channels):
                locks = self.client.doors(serial, all_channels=True,
                    include_unlisted_primary=self.include_unlisted_primary).get("locks", [])
            else:
                result = self.client.doors(serial)
                locks = [{**row, "channel": result.get("channel", 0),
                          "requires_unlisted_primary_override": False} for row in result.get("doors", [])]
            for lock in locks:
                targets.append({**lock, "deviceId": serial,
                                "deviceName": device.get("name") or "DMSS device"})
        return {"devices": devices, "targets": targets}

    def start_renewal(self):
        """Check renewal independently of entity polling and button activity."""
        return async_track_time_interval(self.hass, self._async_renew, timedelta(minutes=5))

    async def _async_renew(self, _now):
        try:
            await self.async_call(self.client.ensure_session)
        except Exception as error:
            if is_auth_error(error):
                self.entry.async_start_reauth(self.hass)
            self.async_set_update_error(UpdateFailed("DMSS session renewal failed"))

    async def async_unlock(self, target):
        override = bool(target.get("requires_unlisted_primary_override"))
        if override and not self.include_unlisted_primary:
            raise HomeAssistantError("Unlisted primary relay is disabled")
        try:
            await self.async_call(self.client.unlock, target["deviceId"], target["door_index"],
                                 channel=target.get("channel", 0), allow_unlisted_primary=override)
        except Exception as error:
            if is_auth_error(error):
                self.entry.async_start_reauth(self.hass)
                raise HomeAssistantError("DMSS account needs authentication") from None
            raise HomeAssistantError("DMSS unlock request failed; physical door state is unknown") from None

    async def async_close(self):
        media, self.media = self.media, None
        try:
            if media is not None:
                await media.async_close()
        finally:
            await self.bells.async_stop()
            await self.async_call(self.client.http.close)
