"""Tests against installed Home Assistant, with no real door operations."""
import tempfile
import json
from pathlib import Path
from types import MappingProxyType
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.config_entries import ConfigEntry, ConfigEntries
from homeassistant import loader
from homeassistant.auth import auth_manager_from_config
from homeassistant.components.http.server import HomeAssistantHTTP
from homeassistant.components.homekit.doorbell import HomeDoorbellAccessory
from homeassistant.components.homekit.accessories import HomeDriver
from homeassistant.components.homekit.iidmanager import AccessoryIIDStorage
from homeassistant.components.homekit.type_cameras import Camera as HomeKitCamera
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.helpers import entity_registry, device_registry, area_registry

from custom_components.dmss.button import DMSSUnlockButton
from custom_components.dmss.client import APIError
from custom_components.dmss.config_flow import DMSSConfigFlow
from custom_components.dmss.coordinator import DMSSCoordinator
from custom_components.dmss.bell_protocol import BellNormalizer, LocalDHIP


class HomeKitBellProbe:
    """Exercise HA's actual HomeKit event handling without sending alerts."""

    async_update_doorbell_state_event = HomeDoorbellAccessory.async_update_doorbell_state_event
    async_update_doorbell_state = HomeDoorbellAccessory.async_update_doorbell_state
    doorbell_is_event = True

    def __init__(self, hass, entity_id):
        self._char_doorbell_detected = Mock()
        self._char_doorbell_detected_switch = Mock()
        self.entity_id = entity_id
        self.linked_doorbell_sensor = entity_id
        # HomeKit evaluates the initial state while creating the accessory.
        self.async_update_doorbell_state(None, hass.states.get(entity_id))
        self.remove = async_track_state_change_event(
            hass, entity_id, self.async_update_doorbell_state_event)

    @property
    def count(self):
        return self._char_doorbell_detected.set_value.call_count


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

    async def test_real_setup_preserves_entities_and_routes_incoming_rings_to_homekit(self):
        self.hass.config_entries = ConfigEntries(self.hass, {})
        loader.async_setup(self.hass)
        device_registry.async_setup(self.hass)
        await device_registry.async_load(self.hass)
        await entity_registry.async_load(self.hass)
        await area_registry.async_load(self.hass)
        # Camera/stream setup loads HTTP, whose real auth manager needs the
        # device registry. Its HTTP listener uses an ephemeral local port.
        self.hass.auth = await auth_manager_from_config(self.hass, [], [])
        folder = Path(__file__).resolve().parents[1] / "custom_components" / "dmss"
        self.hass.data[loader.DATA_CUSTOM_COMPONENTS] = {"dmss": loader.Integration(
            self.hass, "custom_components.dmss", folder, json.loads((folder / "manifest.json").read_text()),
            top_level_files={p.name for p in folder.iterdir()})}
        client = Mock()
        client.devices.return_value = [{"deviceId": "test-device", "name": "Test interphone",
                                       "deviceCatalog": "VTH", "devType": 21,
                                       "deviceChannels": [
                                           {"channelCatalog": "VTH", "channelId": "0", "onlineStatus": 1},
                                           {"channelCatalog": "VTO", "channelId": "1", "onlineStatus": 1},
                                           {"channelCatalog": "VTO", "channelId": "2", "onlineStatus": 1},
                                       ]}]
        client.doors.return_value = {"locks": [{"channel": channel, "door_index": relay,
                                                "door_name": name} for name, channel, relay in
                                               [("front", 1, 0), ("back", 2, 0), ("back", 2, 1)]]}
        original_create_server = HomeAssistantHTTP._async_create_server

        async def isolated_http_server(server):
            # Exercise real component setup without claiming a normal HA port
            # or binding an external interface on the developer's computer.
            server.server_host = ["127.0.0.1"]
            server.server_port = 0
            return await original_create_server(server)

        with patch("custom_components.dmss.DMSSClient", return_value=client), \
                patch.object(HomeAssistantHTTP, "_async_create_server", isolated_http_server):
            await self.hass.config_entries.async_add(self.entry)
            await self.hass.async_block_till_done()
            ids = sorted(s.entity_id for s in self.hass.states.async_all("button"))
            self.assertEqual(ids, ["button.unlock_building_back_1", "button.unlock_building_back_2",
                                   "button.unlock_building_front_1"])
            await self.hass.services.async_call("button", "press", {
                "entity_id": "button.unlock_building_front_1"}, blocking=True)
            client.unlock.assert_called_once()
            coordinator = self.hass.data["dmss"][self.entry.entry_id]
            camera_ids = sorted(state.entity_id for state in self.hass.states.async_all("camera"))
            self.assertEqual(camera_ids, ["camera.building_back", "camera.building_front"])
            self.assertEqual(coordinator.media._sessions, {})
            self.assertIsNone(coordinator.media._server)
            event_ids = sorted(s.entity_id for s in self.hass.states.async_all("event"))
            self.assertEqual(event_ids, ["event.building_doorbell_back", "event.building_doorbell_back_ring",
                "event.building_doorbell_front", "event.building_doorbell_front_ring",
                "event.building_doorbell_unknown", "event.building_doorbell_unknown_ring"])
            # Construct actual HA HomeKit cameras with the documented links.
            # The HAP driver is never started: no pairing, mDNS or live media.
            storage = AccessoryIIDStorage(self.hass, "test-homekit")
            await storage.async_initialize()
            driver = HomeDriver(self.hass, "test-homekit", "Test", "Test", storage,
                address="127.0.0.1", port=0, loop=self.hass.loop,
                persist_file=str(Path(self.directory.name) / "test-homekit.state"))
            for number, door in enumerate(("front", "back"), 2):
                ring_id = f"event.building_doorbell_{door}_ring"
                accessory = HomeKitCamera(self.hass, driver, f"Building {door}",
                    f"camera.building_{door}", number,
                    {"linked_doorbell_sensor":ring_id, "support_audio":True,
                     "video_codec":"copy", "max_width":1280, "max_height":720, "max_fps":25})
                self.assertIsNotNone(accessory.get_service("Doorbell"))
                self.assertTrue(accessory.doorbell_is_event)
                self.assertEqual(accessory.linked_doorbell_sensor, ring_id)
            self.assertEqual(coordinator.media._sessions, {})
            bells = {door: HomeKitBellProbe(self.hass, f"event.building_doorbell_{door}_ring")
                     for door in ("front", "back", "unknown")}
            self.assertTrue(all(bell.count == 0 for bell in bells.values()))
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
            ring_front = self.hass.states.get("event.building_doorbell_front_ring")
            self.assertEqual(ring_front.attributes["event_types"], ["ring"])
            self.assertEqual(ring_front.attributes["phase"], "start")
            self.assertEqual((bells["front"].count, bells["back"].count), (1, 0))
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
                self.assertEqual(self.hass.states.get("event.building_doorbell_front_ring").state, ring_front.state)
                self.assertEqual(bells["front"].count, 1)

            async def native(n, raw):
                event = n.normalize(raw)
                if event:
                    coordinator.bells._on_event("test-device", event, None)
                await self.hass.async_block_till_done()

            # The real callback envelope must ring at incoming, before Talking
            # or a completed-call log, with no duplicate when polling agrees.
            n = BellNormalizer(client.devices.return_value[0])
            local = LocalDHIP("192.0.2.20", "test-only", "test-only")
            self.assertTrue(local._queue_notification({
                "method":"client.VideoTalkPhone.notifyCallState",
                "params":{"Info":{"CallState":"Called","ChannelID":2}}}))
            await native(n, local.pending.pop())
            self.assertEqual(bells["back"].count, 1)
            for raw in (
                {"Code":"VTPCallState","Data":{"callState":"Ringing","ChannelID":2}},
                {"Code":"VTPCallState","Data":{"callState":"Talking"}},
                {"Code":"VTPCallState","Data":{"callState":"Idle"}},
                {"Code":"VideoTalkLog","Data":{"EndState":"Missed","ChannelID":2}},
            ):
                await native(n, raw)
                self.assertEqual(bells["back"].count, 1)
            await native(n, {"Code":"Invite","Data":{"ChannelID":2}})
            self.assertEqual(bells["back"].count, 2)

            # A faster initial message can omit the door. Identifying it later
            # alerts the correct HomeKit camera, without replaying HA triggers.
            n = BellNormalizer({})
            await native(n, {"Code":"VTPCallState","Data":{"callState":"Called"}})
            self.assertEqual(bells["unknown"].count, 1)
            await native(n, {"Code":"Invite","Data":{"ChannelID":1}})
            self.assertEqual(bells["front"].count, 2)
            await native(n, {"Code":"VTPCallState","Data":{"callState":"Ringing","ChannelID":1}})
            self.assertEqual(bells["front"].count, 2)
            self.assertEqual(bells["back"].count, 2)
            for kind, phase, verified in (("ring","end",True),("ring","start",False)):
                coordinator.bells._on_event("test-device", {"door":"front","kind":kind,
                    "phase":phase,"verified":verified,"event_code":"fixture",
                    "received_at":"2026-10-31T08:00:00+00:00"}, None)
            await self.hass.async_block_till_done()
            self.assertEqual(bells["front"].count, 2)
            coordinator.bells._on_event("test-device", None, False)
            await self.hass.async_block_till_done()
            self.assertEqual(self.hass.states.get("event.building_doorbell_front").state, "unavailable")
            coordinator.bells._on_event("test-device", None, True)
            await self.hass.async_block_till_done()
            self.assertEqual((bells["front"].count, bells["back"].count), (2, 2))
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
            for bell in bells.values():
                bell.remove()
            coordinator.async_set_update_error(UpdateFailed("test failure"))
            await self.hass.async_block_till_done()
            self.assertTrue(all(self.hass.states.get(e).state == "unavailable" for e in ids))
            self.assertTrue(await self.hass.config_entries.async_unload(self.entry.entry_id))


if __name__ == "__main__":
    unittest.main()
