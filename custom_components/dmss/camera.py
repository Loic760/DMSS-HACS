"""On-demand main-stream cameras; snapshots come only from an existing cache."""
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CAMERA_CHANNELS, DOMAIN


def _camera_targets(data):
    for device in (data or {}).get("devices", []):
        if str(device.get("deviceCatalog", "")).upper() != "VTH":
            continue
        channels = device.get("deviceChannels") or []
        for door, channel in CAMERA_CHANNELS.items():
            if any(str(row.get("channelId")) == str(channel)
                   and str(row.get("channelCatalog", "")).upper() == "VTO"
                   for row in channels):
                yield device, door, channel


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    added = set()

    def discover():
        entities = []
        for device, door, channel in _camera_targets(coordinator.data):
            key = (device["deviceId"], channel)
            if key not in added:
                added.add(key)
                entities.append(DMSSCamera(coordinator, device, door, channel))
        if entities:
            async_add_entities(entities)

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))


class DMSSCamera(CoordinatorEntity, Camera):
    """A camera source stays idle until a consumer requests the local feed."""

    _attr_has_entity_name = False
    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_brand = "Dahua"

    def __init__(self, coordinator, device, door, channel):
        Camera.__init__(self)
        CoordinatorEntity.__init__(self, coordinator)
        self.serial = device["deviceId"]
        self.channel = channel
        self.door = door
        self._attr_name = f"Building {door}"
        self.entity_id = f"camera.building_{door}"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{self.serial}_{channel}_camera"
        self._attr_model = device.get("deviceModel")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.serial)},
            name=device.get("name") or "DMSS interphone",
            manufacturer="Dahua",
        )

    def _current_channel(self):
        for device in (self.coordinator.data or {}).get("devices", []):
            if device.get("deviceId") == self.serial:
                return next((row for row in device.get("deviceChannels", [])
                             if str(row.get("channelId")) == str(self.channel)
                             and str(row.get("channelCatalog", "")).upper() == "VTO"), None)
        return None

    @property
    def available(self):
        channel = self._current_channel()
        return (self.coordinator.last_update_success
                and channel is not None
                and str(channel.get("onlineStatus", 1)) == "1"
                and Camera.available.fget(self))

    async def stream_source(self):
        """Return a stable local URL; the manager opens cloud media on GET only."""
        if self.coordinator.media is None:
            return None
        return await self.coordinator.media.async_stream_source(self.serial, self.channel)

    async def async_camera_image(self, width=None, height=None):
        """Never start an upstream session for dashboard/HomeKit thumbnails."""
        if self.coordinator.media is None:
            return None
        # Preserve the source frame's dimensions. HA may resize for a client.
        return await self.coordinator.media.async_camera_image(self.serial, self.channel)

    async def async_will_remove_from_hass(self):
        if self.stream is not None:
            await self.stream.stop()
            self.stream = None
        await super().async_will_remove_from_hass()
