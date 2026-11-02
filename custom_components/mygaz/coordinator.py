"""Data update coordinator."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import MyGazAuthError, MyGazClient, MyGazError
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES, DOMAIN

_LOGGER = logging.getLogger(__name__)


class MyGazCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Fetches account info for every personal account on the login."""

    def __init__(
        self, hass: HomeAssistant, entry: MyGazConfigEntry, client: MyGazClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                minutes=entry.options.get(
                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES
                )
            ),
        )
        self.client = client

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            accounts = await self.client.async_get_accounts()
            _LOGGER.debug("Получено %s лицевых счетов", len(accounts))

            detailed: dict[str, dict[str, Any]] = {}
            for account in accounts:
                key = str(account.get("id"))
                detailed[key] = {
                    "account": account,
                    "info": await self.client.async_get_account_info(account),
                    "messages": await self.client.async_get_messages(account),
                }
                _LOGGER.debug("Данные по ЛС %s обновлены", account.get("number"))
        except MyGazAuthError as err:
            raise ConfigEntryAuthFailed(
                f"MyGaz credentials rejected: {err}"
            ) from err
        except MyGazError as err:
            raise UpdateFailed(str(err)) from err

        return detailed


type MyGazConfigEntry = ConfigEntry[MyGazCoordinator]
