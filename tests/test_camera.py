"""Camera entities against Home Assistant; all media operations are mocked."""
import tempfile
from types import MappingProxyType
import unittest
from unittest.mock import AsyncMock, Mock

from homeassistant.components.camera import CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.dmss.button import DMSSUnlockButton
from custom_components.dmss.camera import DMSSCamera, async_setup_entry
from custom_components.dmss.coordinator import DMSSCoordinator


class CameraTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.entry = ConfigEntry(
            version=1, minor_version=1, domain="dmss", title="Test",
            data={"email": "test@example.invalid", "password": "test-only"}, options={},
            source="user", unique_id="test", discovery_keys=MappingProxyType({}),
            subentries_data=[],
        )
        self.client = Mock()
        self.coordinator = DMSSCoordinator(self.hass, self.entry, self.client)
        self.coordinator.media = Mock(
            async_stream_source=AsyncMock(return_value="http://127.0.0.1:12345/test-only/front.ts"),
            async_camera_image=AsyncMock(return_value=None),
            async_close=AsyncMock(),
        )
        self.device = {
            "deviceId": "test-parent", "name": "Test interphone", "deviceCatalog": "VTH",
            "deviceChannels": [
                {"channelId": "0", "channelCatalog": "VTH", "onlineStatus": 1},
                {"channelId": "1", "channelCatalog": "VTO", "onlineStatus": 1},
                {"channelId": "2", "channelCatalog": "VTO", "onlineStatus": 1},
                {"channelId": "3", "channelCatalog": "IPC", "onlineStatus": 1},
            ],
        }
        self.coordinator.async_set_updated_data({"devices": [self.device], "targets": []})

    async def asyncTearDown(self):
        await self.hass.async_stop()
        self.directory.cleanup()

    async def test_discovery_adds_only_two_named_cameras_without_opening_media(self):
        self.hass.data["dmss"] = {self.entry.entry_id: self.coordinator}
        added = Mock()
        await async_setup_entry(self.hass, self.entry, added)
        entities = added.call_args.args[0]
        self.assertEqual([entity.entity_id for entity in entities],
                         ["camera.building_front", "camera.building_back"])
        self.assertEqual([entity.channel for entity in entities], [1, 2])
        self.assertTrue(all(entity.supported_features == CameraEntityFeature.STREAM for entity in entities))
        self.assertTrue(all(entity.device_info["identifiers"] == {("dmss", "test-parent")}
                            for entity in entities))
        self.coordinator.media.async_stream_source.assert_not_awaited()
        self.coordinator.media.async_camera_image.assert_not_awaited()
        self.coordinator.async_set_updated_data({"devices": [self.device], "targets": []})
        self.assertEqual(added.call_count, 1)

    async def test_stream_source_and_snapshot_delegate_without_cloud_side_effects(self):
        camera = DMSSCamera(self.coordinator, self.device, "front", 1)
        self.assertTrue(camera.available)
        self.assertIsNone(await camera.async_camera_image(320, 180))
        self.coordinator.media.async_stream_source.assert_not_awaited()
        self.coordinator.media.async_camera_image.assert_awaited_once_with("test-parent", 1)
        url = await camera.stream_source()
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        self.coordinator.media.async_stream_source.assert_awaited_once_with("test-parent", 1)
        self.coordinator.media.async_camera_image.return_value = b"cached-full-resolution-jpeg"
        self.assertEqual(await camera.async_camera_image(16, 16), b"cached-full-resolution-jpeg")
        self.assertEqual(self.client.mock_calls, [])

    async def test_camera_offline_removed_or_coordinator_failed_is_unavailable(self):
        camera = DMSSCamera(self.coordinator, self.device, "back", 2)
        self.assertTrue(camera.available)
        self.device["deviceChannels"][2]["onlineStatus"] = 0
        self.assertFalse(camera.available)
        self.device["deviceChannels"][2]["onlineStatus"] = 1
        self.coordinator.async_set_update_error(UpdateFailed("test failure"))
        self.assertFalse(camera.available)
        self.coordinator.async_set_updated_data({"devices": [], "targets": []})
        self.assertFalse(camera.available)

    async def test_removal_stops_only_this_camera_ha_stream(self):
        camera = DMSSCamera(self.coordinator, self.device, "front", 1)
        stream = Mock(stop=AsyncMock())
        camera.stream = stream
        await camera.async_will_remove_from_hass()
        stream.stop.assert_awaited_once()
        self.assertIsNone(camera.stream)
        self.coordinator.media.async_close.assert_not_awaited()

    async def test_coordinator_closes_media_before_account_session(self):
        media = self.coordinator.media
        order = []
        media.async_close.side_effect = lambda: order.append("media")
        self.client.http.close.side_effect = lambda: order.append("account")
        await self.coordinator.async_close()
        self.assertEqual(order, ["media", "account"])
        self.assertIsNone(self.coordinator.media)

    async def test_existing_button_ids_and_unique_ids_stay_unchanged(self):
        entities = [DMSSUnlockButton(self.coordinator, {
            "deviceId": "test-parent", "deviceName": "Test interphone", "channel": channel,
            "door_name": door, "door_index": relay,
        }) for door, channel, relay in [("front", 1, 0), ("back", 2, 0), ("back", 2, 1)]]
        self.assertEqual([entity.entity_id for entity in entities], [
            "button.unlock_building_front_1", "button.unlock_building_back_1",
            "button.unlock_building_back_2",
        ])
        self.assertEqual([entity.unique_id for entity in entities], [
            f"{self.entry.entry_id}_test-parent_1_0_unlock",
            f"{self.entry.entry_id}_test-parent_2_0_unlock",
            f"{self.entry.entry_id}_test-parent_2_1_unlock",
        ])


if __name__ == "__main__":
    unittest.main()
