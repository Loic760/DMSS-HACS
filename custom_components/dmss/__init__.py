"""DMSS cloud integration with credentials managed by Home Assistant."""
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .client import DMSSClient
from .const import DOMAIN, PLATFORMS
from .coordinator import DMSSCoordinator
from .errors import is_auth_error


async def async_setup_entry(hass, entry: ConfigEntry):
    password = entry.data[CONF_PASSWORD]
    client = DMSSClient(state={"auth_method": "web"}, account=entry.data[CONF_EMAIL],
                        password_provider=lambda _account: password)
    coordinator = DMSSCoordinator(hass, entry, client)
    try:
        from .media import DMSSMediaManager

        coordinator.media = DMSSMediaManager(hass, coordinator)
        await coordinator.async_call(client.login, password)
        await coordinator.async_config_entry_first_refresh()
    except Exception as error:
        await coordinator.async_close()
        if is_auth_error(error) or isinstance(error, ConfigEntryAuthFailed):
            raise ConfigEntryAuthFailed("DMSS authentication needs attention") from None
        raise ConfigEntryNotReady("Unable to connect to DMSS") from None
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(coordinator.start_renewal())
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        await coordinator.async_close()
        raise
    coordinator.bells.start()
    return True


async def _async_options_updated(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_close()
        return True
    return False
