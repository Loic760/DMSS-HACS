"""Maintain native local subscriptions; deliver callbacks on HA's event loop."""
import asyncio
import ipaddress
import threading

from .bell_protocol import BellNormalizer, LocalDHIP
from .client import decrypt_play_info


class BellManager:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.hass = coordinator.hass
        self.stop = threading.Event()
        self.workers = []
        self.feeds = {}
        self.connected = set()
        self.listeners = []
        self.started = False

    def add_listener(self, listener):
        self.listeners.append(listener)
        def unsubscribe():
            if listener in self.listeners: self.listeners.remove(listener)
        return unsubscribe

    def _details(self, serial):
        device = self.coordinator.client.select_device(serial)
        host = device.get('devIp')
        address = ipaddress.ip_address(host)
        if not address.is_private or address.is_unspecified or address.is_multicast:
            raise ValueError('Interphone is not reachable on a private local address')
        info = decrypt_play_info(device['playInfo'], serial)
        return device, host, info['devLoginName'], info['devLoginPassword']

    def _dispatch(self, serial, event=None, connected=None):
        if self.stop.is_set(): return
        self.hass.loop.call_soon_threadsafe(self._on_event, serial, event, connected)

    def _on_event(self, serial, event, connected):
        if self.stop.is_set(): return
        if connected is True: self.connected.add(serial)
        elif connected is False: self.connected.discard(serial)
        if event and event.get('verified') and event.get('kind') in ('ring', 'call', 'call_ended', 'missed_call'):
            from homeassistant.helpers import device_registry as dr
            from .const import DOMAIN
            device = dr.async_get(self.hass).async_get_device_by_identifier((DOMAIN, serial), self.coordinator.entry.entry_id)
            self.hass.bus.async_fire('dmss_doorbell', {
                'device_id': device.id if device else None,
                'door': event['door'], 'type': event['kind'], 'phase': event.get('phase', 'unknown'),
                'source': event.get('source','local'), 'call_state':event.get('call_state','unknown'),
                'received_at': event['received_at'], 'event_code': event['event_code']})
        for listener in tuple(self.listeners): listener(serial, event)

    def start(self):
        if self.started: return
        self.started = True
        for device in (self.coordinator.data or {}).get('devices', []):
            if str(device.get('deviceCatalog','')).upper() != 'VTH' or not device.get('devIp'):
                continue
            worker = threading.Thread(target=self._run, args=(device['deviceId'],), daemon=True)
            self.workers.append(worker); worker.start()

    def _run(self, serial):
        delay = 1
        while not self.stop.is_set():
            feed = None
            future = None
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self.coordinator.async_call(self._details, serial), self.hass.loop)
                device, host, username, password = future.result(timeout=40)
                if self.stop.is_set(): break
                normalizer = BellNormalizer(device)
                feed = LocalDHIP(host, username, password)
                self.feeds[serial] = feed
                feed.connect(); delay = 1
                self._dispatch(serial, connected=True)
                def receive(raw):
                    event = normalizer.normalize(raw)
                    if event and event['kind'] != 'notification': self._dispatch(serial, event)
                feed.listen(receive, self.stop)
            except Exception:
                self._dispatch(serial, connected=False)
            finally:
                if future and not future.done(): future.cancel()
                if feed: feed.close()
                self.feeds.pop(serial, None)
            self.stop.wait(delay); delay = min(30, delay*2)

    async def async_stop(self):
        self.stop.set()
        for feed in tuple(self.feeds.values()): feed.close()
        def join():
            for worker in self.workers: worker.join(timeout=2)
        await self.hass.async_add_executor_job(join)
        self.connected.clear()
