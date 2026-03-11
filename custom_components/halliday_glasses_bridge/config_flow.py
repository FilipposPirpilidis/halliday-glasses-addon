from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries

from .const import CONF_ADDON_HOST, CONF_ADDON_PORT, DEFAULT_ADDON_HOST, DEFAULT_ADDON_PORT, DOMAIN


class HallidayGlassesBridgeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Halliday Glasses Bridge", data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDON_HOST, default=DEFAULT_ADDON_HOST): str,
                    vol.Required(CONF_ADDON_PORT, default=DEFAULT_ADDON_PORT): int,
                }
            ),
        )
