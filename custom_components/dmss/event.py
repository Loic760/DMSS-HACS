"""Doorbell event entities for simple Home Assistant automations."""
from homeassistant.components.event import EventEntity, EventDeviceClass
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([entity
        for device in (coordinator.data or {}).get('devices', [])
        if str(device.get('deviceCatalog','')).upper() == 'VTH'
        for door in ('front', 'back', 'unknown')
        for entity in (DMSSDoorbellEvent(coordinator, device, door),
                       DMSSDoorbellRingEvent(coordinator, device, door))])


class DMSSDoorbellEvent(EventEntity):
    _attr_has_entity_name = False
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = ['ring', 'call', 'call_ended', 'missed_call']

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
                'source':event.get('source','local'),'call_state':event.get('call_state','unknown'),
                'event_code':event['event_code']})
        self.async_write_ha_state()


class DMSSDoorbellRingEvent(DMSSDoorbellEvent):
    """HomeKit-safe bell signal: only verified incoming-call starts.

    HomeKit rings on every event state change without filtering event_type.
    Keep these separate from the existing entities that retain call history.
    """

    _attr_event_types = ['ring']

    def __init__(self, coordinator, device, door):
        super().__init__(coordinator, device, door)
        self._attr_name += '_ring'
        self.entity_id = f'event.{self._attr_name}'
        self._attr_unique_id += '_ring'

    def _receive(self, serial, event):
        if (event and event.get('verified') and event.get('phase') == 'start'
                and event.get('kind') in ('ring', 'ring_identified')):
            # A later identity can identify an already-ringing unknown door.
            # It is internal to this signal and does not replay HA automations.
            event = {**event, 'kind': 'ring'}
        else:
            event = None
        super()._receive(serial, event)
