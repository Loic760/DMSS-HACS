"""Unlock pulse buttons; no assumption about physical locked state."""
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    added = set()

    def discover():
        entities = []
        for target in (coordinator.data or {}).get("targets", []):
            key = (target["deviceId"], target.get("channel", 0), target["door_index"])
            if key not in added:
                added.add(key)
                entities.append(DMSSUnlockButton(coordinator, target))
        async_add_entities(entities)

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))


class DMSSUnlockButton(CoordinatorEntity, ButtonEntity):
    _attr_has_entity_name = False
    _attr_icon = "mdi:door-open"

    def __init__(self, coordinator, target):
        super().__init__(coordinator)
        self.target = target
        self.key = (target["deviceId"], target.get("channel", 0), target["door_index"])
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{self.key[0]}_{self.key[1]}_{self.key[2]}_unlock"
        door = slugify(target.get("door_name") or "door")
        self._attr_name = f"unlock_building_{door}_{target['door_index'] + 1}"
        # Supply a valid, fully qualified suggestion so HA does not prepend
        # the monitor's device name. Existing registry names remain respected.
        self.entity_id = f"button.{self._attr_name}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, target["deviceId"])},
                                           name=target["deviceName"], manufacturer="Dahua")

    @property
    def available(self):
        return super().available and any(
            (row["deviceId"], row.get("channel", 0), row["door_index"]) == self.key
            for row in (self.coordinator.data or {}).get("targets", [])
        )

    async def async_press(self):
        current = next((row for row in self.coordinator.data.get("targets", [])
                        if (row["deviceId"], row.get("channel", 0), row["door_index"]) == self.key), None)
        if current is None:
            from homeassistant.exceptions import HomeAssistantError
            raise HomeAssistantError("Door is no longer in the DMSS inventory")
        await self.coordinator.async_unlock(current)
