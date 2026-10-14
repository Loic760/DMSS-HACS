"""Diagnostics expose counts and capabilities, never cloud/session payloads."""
from .const import DOMAIN


async def async_get_config_entry_diagnostics(hass, entry):
    coordinator = hass.data[DOMAIN].get(entry.entry_id)
    if coordinator is None:
        return {"loaded": False}
    data = coordinator.data or {}
    return {
        "loaded": True,
        "local_bell_subscriptions": len(coordinator.bells.connected),
        "incoming_call_push_subscriptions": sum(bool(getattr(feed,'call_state_supported',False)) for feed in coordinator.bells.feeds.values()),
        "incoming_call_poll_connections": sum(bool(getattr(feed,'call_state_poll_supported',False)) for feed in coordinator.bells.feeds.values()),
        "call_state_poll_interval_seconds": 1,
        "bell_transport": "local_native_push",
        "last_update_success": coordinator.last_update_success,
        "auth_method": "web",
        "session_storage": "memory_only",
        "include_unlisted_primary": coordinator.include_unlisted_primary,
        "device_count": len(data.get("devices", [])),
        "unlock_button_count": len(data.get("targets", [])),
        "unlisted_primary_count": sum(bool(row.get("requires_unlisted_primary_override"))
                                      for row in data.get("targets", [])),
    }
