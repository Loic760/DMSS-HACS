"""Doorbell event entities for simple Home Assistant automations."""
from homeassistant.components.event import EventEntity, EventDeviceClass
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DMSSDoorbellEvent(coordinator, device, door)
        for device in (coordinator.data or {}).get('devices', [])
        if str(device.get('deviceCatalog','')).upper() == 'VTH'
        for door in ('front', 'back', 'unknown')])


class DMSSDoorbellEvent(EventEntity):
    _attr_has_entity_name = False
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = ['ring', 'call', 'missed_call']

    def __init__(self, coordinator, device, door):
        self.manager = coordinator.bells
        self.serial = device['deviceId']
        self.door = door
        self._attr_name = f'building_doorbell_{door}'
        self.entity_id = f'event.{self._attr_name}'
        self._attr_unique_id = f'{coordinator.entry.entry_id}_{self.serial}_doorbell_{door}'
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN,self.serial)},
            name=device.get('name') or 'DMSS interphone', manufacturer='Dahua')

    @property
    def available(self):
        return self.serial in self.manager.connected

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self.manager.add_listener(self._receive))

    def _receive(self, serial, event):
        if serial != self.serial: return
        if event and event.get('door') == self.door and event.get('kind') in self._attr_event_types:
            self._trigger_event(event['kind'], {'door':self.door,'phase':event.get('phase','unknown'),
                'source':'local','event_code':event['event_code']})
        self.async_write_ha_state()
