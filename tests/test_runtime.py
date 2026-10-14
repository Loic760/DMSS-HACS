"""Offline tests for coordinator safety boundaries; no HA install/network needed.

Run: python3 -m unittest discover -s tests
These stubs test our runtime contract, not full Home Assistant compatibility.
"""
import asyncio
import importlib.util
from pathlib import Path
import sys
import threading
import time
import types
import unittest

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "dmss"
PKG = "dmss_test_integration"
package = types.ModuleType(PKG)
package.__path__ = [str(ROOT)]
sys.modules[PKG] = package


def module(name, **attrs):
    value = types.ModuleType(name)
    value.__dict__.update(attrs)
    sys.modules[name] = value
    return value


class AuthFailed(Exception):
    pass


class HAError(Exception):
    pass


class UpdateFailed(Exception):
    pass


class Coordinator:
    def __init__(self, hass, *args, **kwargs):
        self.hass = hass
        self.data = None
        self.error = None

    def async_set_update_error(self, error):
        self.error = error


module("homeassistant")
module("homeassistant.config_entries", ConfigEntry=object)
module("homeassistant.core", HomeAssistant=object)
module("homeassistant.exceptions", ConfigEntryAuthFailed=AuthFailed, HomeAssistantError=HAError)
module("homeassistant.helpers")
module("homeassistant.helpers.event", async_track_time_interval=lambda *args: lambda: None)
module("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=Coordinator, UpdateFailed=UpdateFailed)


def load(name):
    spec = importlib.util.spec_from_file_location(f"{PKG}.{name}", ROOT / f"{name}.py")
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


const = load("const")
client_module = load("client")
errors = load("errors")
runtime = load("coordinator")


class Hass:
    async def async_add_executor_job(self, job):
        return await asyncio.to_thread(job)


class Entry:
    options = {}
    reauth = False

    def async_start_reauth(self, hass):
        self.reauth = True


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_executor_calls_are_serial_and_off_event_loop(self):
        coordinator = runtime.DMSSCoordinator(Hass(), Entry(), object())
        active = 0
        high_water = 0
        event_thread = threading.get_ident()

        def operation():
            nonlocal active, high_water
            self.assertNotEqual(threading.get_ident(), event_thread)
            active += 1
            high_water = max(high_water, active)
            time.sleep(0.02)
            active -= 1

        await asyncio.gather(*(coordinator.async_call(operation) for _ in range(4)))
        self.assertEqual(high_water, 1)

    async def test_unlisted_primary_disabled_prevents_any_request(self):
        class Client:
            def unlock(self, *args, **kwargs):
                raise AssertionError("Must not issue a request")

        entry = Entry()
        entry.options = {"include_unlisted_primary": False}
        coordinator = runtime.DMSSCoordinator(Hass(), entry, Client())
        with self.assertRaises(HAError):
            await coordinator.async_unlock({"requires_unlisted_primary_override": True})

    async def test_renewal_auth_error_starts_reauth(self):
        class Client:
            def ensure_session(self):
                raise client_module.VerificationRequired(403, "/login")

        entry = Entry()
        coordinator = runtime.DMSSCoordinator(Hass(), entry, Client())
        await coordinator._async_renew(None)
        self.assertTrue(entry.reauth)
        self.assertIsInstance(coordinator.error, UpdateFailed)

    async def test_inventory_is_fresh_and_preserves_channel_relay_pairs(self):
        class Client:
            def ensure_session(self):
                pass

            def devices(self):
                return [{"deviceId": "test-only", "deviceCatalog": "VTH", "devType": 21,
                         "name": "Intercom", "deviceChannels": [{"channelCatalog": "VTO"}]}]

            def doors(self, serial, **kwargs):
                self.assertions = kwargs
                return {"locks": [{"channel": 0, "door_index": 0, "label": "Lock 1"},
                                  {"channel": 1, "door_index": 1, "label": "Lock 2"}]}

        client = Client()
        coordinator = runtime.DMSSCoordinator(Hass(), Entry(), client)
        data = await coordinator._async_update_data()
        self.assertEqual([(r["channel"], r["door_index"]) for r in data["targets"]], [(0, 0), (1, 1)])
        self.assertTrue(client.assertions["include_unlisted_primary"])


if __name__ == "__main__":
    unittest.main()
