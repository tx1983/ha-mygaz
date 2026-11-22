"""Data update coordinator."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import MyGazAuthError, MyGazClient, MyGazError
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES, DOMAIN

_LOGGER = logging.getLogger(__name__)

# A submitted value is shown in its reading box until the cabinet lists it
# (or a newer reading) — but no longer than this.
SUBMITTED_TIMEOUT = timedelta(hours=24)
# Tolerance for comparing readings as floats.
_EPS = 1e-6


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
        # Set once this run's statistics backfill has finished —
        # «Начислено итого» stays unavailable until then.
        self.history_ready = False
        # Values typed into the reading NumberEntities, waiting for the
        # submit button: {counter_uuid: {tariff_zone: value}}.
        self.pending: dict[str, dict[str, float]] = {}
        # Values the cabinet accepted but does not list yet — the reading
        # boxes keep showing them instead of falling back to the previous
        # reading: {counter_uuid: {tariff_zone: value}}.
        self.submitted: dict[str, dict[str, float]] = {}
        # {counter_uuid: (submission time, cabinet's last reading per zone
        # at that moment)} — for the timeout and the «newer reading» check.
        self._submitted_meta: dict[
            str, tuple[datetime, dict[str, float | None]]
        ] = {}

    def set_pending(self, uuid: str, zone: str, value: float) -> None:
        """Store a typed-in reading and refresh the entity states."""
        self.pending.setdefault(uuid, {})[zone] = value
        self.async_update_listeners()

    def pending_for(self, uuid: str) -> dict[str, float]:
        """Pending readings of one meter ({} while nothing was typed)."""
        return self.pending.get(uuid) or {}

    def submitted_for(self, uuid: str) -> dict[str, float]:
        """Accepted readings of one meter the cabinet does not list yet."""
        return self.submitted.get(uuid) or {}

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
            # Move the accepted pending slots to «submitted» — the boxes
            # keep the value until the cabinet lists it.
            stored = self.pending.get(uuid)
            if stored:
                accepted = {
                    zone: value
                    for zone, value in zones.items()
                    if stored.get(zone) == value
                }
                for zone in accepted:
                    stored.pop(zone)
                if not stored:
                    self.pending.pop(uuid, None)
                if accepted:
                    self._remember_submitted(uuid, counter, accepted)
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

        self._settle_submitted(detailed)
        return detailed

    def _remember_submitted(
        self, uuid: str, counter: dict[str, Any], values: dict[str, float]
    ) -> None:
        from .number import _ZONE_VALUE_FIELDS  # noqa: PLC0415 (import cycle)
        from .sensor import _float, _latest_reading  # noqa: PLC0415

        reading = _latest_reading(counter)
        baseline = (
            dict(self._submitted_meta[uuid][1])
            if uuid in self._submitted_meta
            else {}
        )
        for zone in values:
            baseline[zone] = _float(reading.get(_ZONE_VALUE_FIELDS[zone]))
        self.submitted.setdefault(uuid, {}).update(values)
        self._submitted_meta[uuid] = (dt_util.utcnow(), baseline)

    def _settle_submitted(self, data: dict[str, dict[str, Any]]) -> None:
        """Forget submitted values once the cabinet's data catches up.

        A zone is dropped when its last reading in the cabinet equals the
        submitted value, when the cabinet shows a newer, larger reading
        than the one at submission time, or after SUBMITTED_TIMEOUT.
        """
        from .number import _ZONE_VALUE_FIELDS  # noqa: PLC0415 (import cycle)
        from .sensor import _float, _latest_reading  # noqa: PLC0415

        now = dt_util.utcnow()
        for uuid in list(self.submitted):
            sent_at, baseline = self._submitted_meta.get(uuid, (now, {}))
            zones = self.submitted[uuid]
            if now - sent_at > SUBMITTED_TIMEOUT:
                zones.clear()
            counter = next(
                (
                    c
                    for block in data.values()
                    for c in (block.get("info") or {}).get("counters") or []
                    if c.get("uuid") == uuid
                ),
                {},
            )
            reading = _latest_reading(counter)
            for zone, value in list(zones.items()):
                last = _float(reading.get(_ZONE_VALUE_FIELDS[zone]))
                if last is None:
                    continue
                before = baseline.get(zone)
                newer = before is None or abs(last - before) > _EPS
                if abs(last - value) <= _EPS or (newer and last > value):
                    zones.pop(zone)
            if not zones:
                self.submitted.pop(uuid, None)
                self._submitted_meta.pop(uuid, None)


type MyGazConfigEntry = ConfigEntry[MyGazCoordinator]
