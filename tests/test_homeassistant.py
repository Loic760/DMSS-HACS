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
            coordinator.async_set_update_error(UpdateFailed("test failure"))
            await self.hass.async_block_till_done()
            self.assertTrue(all(self.hass.states.get(e).state == "unavailable" for e in ids))
            self.assertTrue(await self.hass.config_entries.async_unload(self.entry.entry_id))


if __name__ == "__main__":
    unittest.main()
