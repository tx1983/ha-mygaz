"""Data update coordinator."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
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
        # Values typed into the reading NumberEntities, waiting for the
        # submit button: {counter_uuid: {tariff_zone: value}}.
        self.pending: dict[str, dict[str, float]] = {}

    def set_pending(self, uuid: str, zone: str, value: float) -> None:
        """Store a typed-in reading and refresh the entity states."""
        self.pending.setdefault(uuid, {})[zone] = value
        self.async_update_listeners()

    def pending_for(self, uuid: str) -> dict[str, float]:
        """Pending readings of one meter ({} while nothing was typed)."""
        return self.pending.get(uuid) or {}

    def find_counter(self, uuid: str) -> tuple[str, dict[str, Any]]:
        """(account_id, counter) of a meter by its uuid across accounts."""
        for account_id, block in (self.data or {}).items():
            for counter in (block.get("info") or {}).get("counters") or []:
                if counter.get("uuid") == uuid:
                    return str(account_id), counter
        return "", {}

    async def async_submit_readings(
        self, readings: dict[str, dict[str, float]]
    ) -> None:
        """indicationSendV4 for every meter in ``readings``, then refresh."""
        for uuid, zones in readings.items():
            account_id, counter = self.find_counter(uuid)
            if not counter:
                raise HomeAssistantError(f"Unknown meter uuid {uuid}")
            try:
                result = await self.client.async_submit_reading(
                    els_id=int(account_id),
                    lspu_id=counter["lspuId"],
                    uuid=uuid,
                    values=zones,
                )
            except MyGazAuthError as err:
                raise ConfigEntryAuthFailed(
                    f"MyGaz credentials rejected: {err}"
                ) from err
            except MyGazError as err:
                raise HomeAssistantError(str(err)) from err
            if not result.get("sent"):
                raise HomeAssistantError(
                    result.get("message") or "Reading rejected by the cabinet"
                )
            # Drop the pending slots that were just accepted.
            stored = self.pending.get(uuid)
            if stored:
                for zone, value in zones.items():
                    if stored.get(zone) == value:
                        stored.pop(zone)
                if not stored:
                    self.pending.pop(uuid, None)
        self.async_update_listeners()  # grey out the button right away
        await self.async_request_refresh()

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
