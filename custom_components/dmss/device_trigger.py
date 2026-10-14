"""Doorbell start triggers in Home Assistant's device automation picker."""
import probatio
from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA, InvalidDeviceAutomationConfig
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.helpers import entity_registry as er
from .const import DOMAIN

TRIGGER_TYPES = {'front_ring': 'front', 'back_ring': 'back', 'unknown_ring': 'unknown'}
TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend({
    probatio.Required('domain'): DOMAIN,
    probatio.Required('type'): probatio.In(TRIGGER_TYPES),
})


async def async_get_triggers(hass, device_id):
    entries = er.async_entries_for_device(er.async_get(hass), device_id)
    if not any(entry.domain == 'event' and entry.platform == DOMAIN for entry in entries):
        return []
    return [{'platform':'device', 'domain':DOMAIN, 'device_id':device_id, 'type':kind}
            for kind in TRIGGER_TYPES]


async def async_validate_trigger_config(hass, config):
    validated = TRIGGER_SCHEMA(config)
    triggers = await async_get_triggers(hass, validated['device_id'])
    if not any(trigger['type'] == validated['type'] for trigger in triggers):
        raise InvalidDeviceAutomationConfig('This device has no DMSS doorbell events')
    return validated


async def async_attach_trigger(hass, config, action, trigger_info):
    config = await async_validate_trigger_config(hass, config)
    event_config = event_trigger.TRIGGER_SCHEMA({
        'platform':'event', 'event_type':'dmss_doorbell',
        'event_data':{'device_id':config['device_id'], 'door':TRIGGER_TYPES[config['type']],
                      'type':'ring', 'phase':'start'},
    })
    return await event_trigger.async_attach_trigger(hass, event_config, action, trigger_info, platform_type='device')
