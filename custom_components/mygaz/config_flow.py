"""Config flow: login + password for the «Мой ГАЗ» personal account."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import MyGazAuthError, MyGazClient, MyGazError, async_get_session
from .const import (
    CONF_IDENTIFIER,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    MIN_SCAN_INTERVAL_MINUTES,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_IDENTIFIER): str,
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)

_SCAN_INTERVAL_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_SCAN_INTERVAL_MINUTES,
        max=1440,
        step=1,
        unit_of_measurement="min",
        mode=NumberSelectorMode.BOX,
    )
)


class MyGazConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config Flow для интеграции MyGaz."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> MyGazOptionsFlow:
        return MyGazOptionsFlow(config_entry)

    async def _validate(
        self, identifier: str, password: str
    ) -> tuple[MyGazClient | None, str | None]:
        """Sign in and fetch the account list; return (client, error_key)."""
        client = MyGazClient(async_get_session(self.hass), identifier, password)
        try:
            await client.async_sign_in()
            accounts = await client.async_get_accounts()
        except MyGazAuthError:
            return None, "invalid_auth"
        except MyGazError as err:
            _LOGGER.debug("Validation failed: %s", err)
            return None, "cannot_connect"
        if not accounts:
            return None, "no_accounts"
        return client, None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Шаг ввода логина и пароля личного кабинета."""
        errors: dict[str, str] = {}
        if user_input is not None:
            identifier = user_input[CONF_IDENTIFIER].strip()
            client, error = await self._validate(
                identifier, user_input[CONF_PASSWORD]
            )
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(identifier.lower())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"МойГАЗ {identifier}",
                    data={
                        CONF_IDENTIFIER: identifier,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-enter the password — the stored one no longer signs in."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            identifier = user_input[CONF_IDENTIFIER].strip()
            client, error = await self._validate(
                identifier, user_input[CONF_PASSWORD]
            )
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_IDENTIFIER: identifier,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA,
                {CONF_IDENTIFIER: entry.data.get(CONF_IDENTIFIER, "")},
            ),
            errors=errors,
        )


class MyGazOptionsFlow(OptionsFlow):
    """Scan interval option."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self._config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL, default=current
                    ): _SCAN_INTERVAL_SELECTOR
                }
            ),
        )
