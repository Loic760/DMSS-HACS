"""Email/password setup, reauthentication, and relay discovery options."""
import asyncio
from functools import partial

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback

from .client import DMSSClient, DMSSError
from .const import CONF_INCLUDE_UNLISTED_PRIMARY, DOMAIN
from .errors import is_auth_error


class DMSSConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return DMSSOptionsFlow()

    async def _validate(self, data):
        client = DMSSClient(state={"auth_method": "web"}, account=data[CONF_EMAIL],
                            password_provider=lambda _account: data[CONF_PASSWORD])
        lock = asyncio.Lock()
        try:
            async with lock:
                await self.hass.async_add_executor_job(partial(client.login, data[CONF_PASSWORD]))
                await self.hass.async_add_executor_job(client.devices)
        finally:
            async with lock:
                await self.hass.async_add_executor_job(client.http.close)

    async def async_step_user(self, user_input=None):
        return await self._credentials("user", user_input)

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        return await self._credentials("reauth_confirm", user_input, self._get_reauth_entry())

    async def async_step_reconfigure(self, user_input=None):
        return await self._credentials("reconfigure", user_input, self._get_reconfigure_entry())

    async def _credentials(self, step_id, user_input, entry=None):
        errors = {}
        if user_input is not None:
            data = {CONF_EMAIL: user_input[CONF_EMAIL].strip(), CONF_PASSWORD: user_input[CONF_PASSWORD]}
            try:
                await self._validate(data)
            except Exception as error:
                errors["base"] = "invalid_auth" if is_auth_error(error) else "cannot_connect" if isinstance(error, DMSSError) else "unknown"
            else:
                await self.async_set_unique_id(data[CONF_EMAIL].casefold())
                if entry:
                    self._abort_if_unique_id_mismatch()
                    return self.async_update_reload_and_abort(entry, data_updates=data)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=data[CONF_EMAIL], data=data)
        email = (user_input or (entry.data if entry else {})).get(CONF_EMAIL, "")
        return self.async_show_form(step_id=step_id, data_schema=vol.Schema({
            vol.Required(CONF_EMAIL, default=email): str,
            vol.Required(CONF_PASSWORD): str,
        }), errors=errors)


class DMSSOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(step_id="init", data_schema=vol.Schema({
            vol.Required(CONF_INCLUDE_UNLISTED_PRIMARY,
                         default=self.config_entry.options.get(CONF_INCLUDE_UNLISTED_PRIMARY, True)): bool,
        }))
