"""Tests against installed Home Assistant, with no real door operations."""
import tempfile
import json
from pathlib import Path
from types import MappingProxyType
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.config_entries import ConfigEntry, ConfigEntries
from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.helpers import entity_registry, device_registry, area_registry

from custom_components.dmss.button import DMSSUnlockButton
from custom_components.dmss.client import APIError
from custom_components.dmss.config_flow import DMSSConfigFlow
from custom_components.dmss.coordinator import DMSSCoordinator


class HomeAssistantTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.entry = ConfigEntry(version=1, minor_version=1, domain="dmss", title="Test",
                                 data={"email": "test@example.invalid", "password": "test-only"},
                                 options={}, source="user", unique_id="test", discovery_keys=MappingProxyType({}),
                                 subentries_data=[])

    async def asyncTearDown(self):
        await self.hass.async_stop()
        self.directory.cleanup()

    async def test_three_named_buttons_press_once_and_go_unavailable_on_failure(self):
        coordinator = DMSSCoordinator(self.hass, self.entry, Mock())
        targets = [{"deviceId": "test-device", "deviceName": "Test interphone", "channel": channel,
                    "door_name": name, "door_index": relay} for name, channel, relay in
                   [("front", 1, 0), ("back", 2, 0), ("back", 2, 1)]]
        coordinator.async_set_updated_data({"devices": [], "targets": targets})
        coordinator.async_unlock = AsyncMock()
        entities = [DMSSUnlockButton(coordinator, target) for target in targets]
        self.assertEqual([e.name for e in entities], ["unlock_building_front_1", "unlock_building_back_1", "unlock_building_back_2"])
        self.assertTrue(all(e.available for e in entities))
        await entities[0].async_press()
        coordinator.async_unlock.assert_awaited_once_with(targets[0])
        coordinator.async_set_update_error(UpdateFailed("test failure"))
        self.assertTrue(all(not e.available for e in entities))

    async def test_wrong_password_becomes_config_flow_auth_error(self):
        flow = DMSSConfigFlow()
        flow.hass = self.hass
        flow._validate = AsyncMock(side_effect=APIError("10000", 200, "/login"))
        result = await flow.async_step_user({"email": "test@example.invalid", "password": "test-only"})
        self.assertEqual(result["errors"], {"base": "invalid_auth"})

    async def test_real_config_entry_setup_registers_three_homekit_button_entities(self):
        self.hass.config_entries = ConfigEntries(self.hass, {})
        loader.async_setup(self.hass)
        device_registry.async_setup(self.hass)
        await device_registry.async_load(self.hass)
        await entity_registry.async_load(self.hass)
        await area_registry.async_load(self.hass)
        folder = Path(__file__).resolve().parents[1] / "custom_components" / "dmss"
        self.hass.data[loader.DATA_CUSTOM_COMPONENTS] = {"dmss": loader.Integration(
            self.hass, "custom_components.dmss", folder, json.loads((folder / "manifest.json").read_text()),
            top_level_files={p.name for p in folder.iterdir()})}
        client = Mock()
        client.devices.return_value = [{"deviceId": "test-device", "name": "Test interphone",
                                       "deviceCatalog": "VTH", "devType": 21,
                                       "deviceChannels": [{"channelCatalog": "VTO"}]}]
        client.doors.return_value = {"locks": [{"channel": channel, "door_index": relay,
                                                "door_name": name} for name, channel, relay in
                                               [("front", 1, 0), ("back", 2, 0), ("back", 2, 1)]]}
        with patch("custom_components.dmss.DMSSClient", return_value=client):
            await self.hass.config_entries.async_add(self.entry)
            await self.hass.async_block_till_done()
            ids = sorted(s.entity_id for s in self.hass.states.async_all("button"))
            self.assertEqual(ids, ["button.unlock_building_back_1", "button.unlock_building_back_2",
                                   "button.unlock_building_front_1"])
            await self.hass.services.async_call("button", "press", {
                "entity_id": "button.unlock_building_front_1"}, blocking=True)
            client.unlock.assert_called_once()
            coordinator = self.hass.data["dmss"][self.entry.entry_id]
            event_ids = sorted(s.entity_id for s in self.hass.states.async_all("event"))
            self.assertEqual(event_ids, ["event.building_doorbell_back", "event.building_doorbell_front",
                                         "event.building_doorbell_unknown"])
            from custom_components.dmss.device_trigger import async_get_triggers, async_attach_trigger
            front_registry = entity_registry.async_get(self.hass).async_get("event.building_doorbell_front")
            device_id = front_registry.device_id
            triggers = await async_get_triggers(self.hass, device_id)
            self.assertEqual({trigger["type"] for trigger in triggers}, {"front_ring", "back_ring", "unknown_ring"})
            self.assertEqual(await async_get_triggers(self.hass, "unrelated-device"), [])
            from homeassistant.components.device_automation import async_get_device_automations, DeviceAutomationType
            picker = await async_get_device_automations(self.hass, DeviceAutomationType.TRIGGER, [device_id])
            self.assertEqual({trigger["type"] for trigger in picker[device_id] if trigger["domain"] == "dmss"},
                             {"front_ring", "back_ring", "unknown_ring"})
            fired = []
            async def trigger_action(variables, context=None):
                fired.append(variables)
            detach_trigger = await async_attach_trigger(self.hass,
                {"platform":"device", "domain":"dmss", "device_id":device_id, "type":"front_ring"},
                trigger_action, {"trigger_data":{"id":"fixture", "idx":"0", "alias":None}, "variables":{}})
            bus_events = []
            remove_listener = self.hass.bus.async_listen("dmss_doorbell", lambda event: bus_events.append(dict(event.data)))
            coordinator.bells._on_event("test-device", None, True)
            coordinator.bells._on_event("test-device", {"door":"front", "kind":"ring", "phase":"start",
                "source":"local event push", "received_at":"2026-10-14T08:00:00+00:00", "event_code":"VTPCallState",
                "verified":True}, None)
            await self.hass.async_block_till_done()
            front = self.hass.states.get("event.building_doorbell_front")
            back = self.hass.states.get("event.building_doorbell_back")
            self.assertEqual(front.attributes["event_type"], "ring")
            self.assertEqual(front.attributes["phase"], "start")
            self.assertEqual(bus_events[0]["door"], "front")
            self.assertEqual(bus_events[0]["phase"], "start")
            self.assertNotEqual(back.attributes.get("event_type"), "ring")
            self.assertEqual(set(front.attributes["event_types"]), {"ring","call","call_ended","missed_call"})
            for kind, phase in (("call","connected"),("call_ended","end"),("missed_call","end")):
                coordinator.bells._on_event("test-device", {"door":"front","kind":kind,"phase":phase,
                    "source":"local state poll","received_at":"2026-10-14T08:00:01+00:00",
                    "event_code":"VTPCallState","verified":True}, None)
                await self.hass.async_block_till_done()
                front=self.hass.states.get("event.building_doorbell_front")
                self.assertEqual(front.attributes["event_type"],kind)
                self.assertEqual(front.attributes["phase"],phase)
                self.assertEqual(bus_events[-1]["type"],kind)
                self.assertEqual(front.attributes["source"],"local state poll")
            coordinator.bells._on_event("test-device", None, False)
            await self.hass.async_block_till_done()
            self.assertEqual(self.hass.states.get("event.building_doorbell_front").state, "unavailable")
            self.assertEqual(len(fired), 1)
            for data in (
                {"device_id":device_id, "door":"front", "type":"missed_call", "phase":"end"},
                {"device_id":device_id, "door":"back", "type":"ring", "phase":"start"},
                {"device_id":"unrelated-device", "door":"front", "type":"ring", "phase":"start"},
            ):
                self.hass.bus.async_fire("dmss_doorbell", data)
            await self.hass.async_block_till_done()
            self.assertEqual(len(fired), 1)
            detach_trigger()
            remove_listener()
            coordinator.async_set_update_error(UpdateFailed("test failure"))
            await self.hass.async_block_till_done()
            self.assertTrue(all(self.hass.states.get(e).state == "unavailable" for e in ids))
            self.assertTrue(await self.hass.config_entries.async_unload(self.entry.entry_id))


if __name__ == "__main__":
    unittest.main()
