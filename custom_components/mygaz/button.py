"""Button entity submitting pending meter readings."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MyGazConfigEntry, MyGazCoordinator
from .number import _ZONE_VALUE_FIELDS
from .sensor import _float, _latest_reading

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyGazConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создание кнопки «Передать показания» на каждый счётчик."""
    coordinator = entry.runtime_data
    buttons = []
    for account_id, block in (coordinator.data or {}).items():
        for counter in (block.get("info") or {}).get("counters") or []:
            buttons.append(
                MyGazSubmitButton(coordinator, str(account_id), counter)
            )
    async_add_entities(buttons)


class MyGazSubmitButton(CoordinatorEntity[MyGazCoordinator], ButtonEntity):
    """«Передать показания» — POSTs all pending values of the meter.

    Unavailable (greyed out) while no new values were typed, or when every
    pending value still equals its zone's last reading — nothing to send.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:send-check"

    def __init__(
        self,
        coordinator: MyGazCoordinator,
        account_id: str,
        counter: dict[str, Any],
    ):
        super().__init__(coordinator)
        self.account_id = account_id
        self._counter_uuid = str(counter.get("uuid"))
        self._attr_name = "Передать показания"
        self._attr_unique_id = f"{DOMAIN}_{self._counter_uuid}_submit"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._counter_uuid)},
        }

    def _unchanged(self, pending: dict[str, float]) -> bool:
        """Every pending value equals its zone's last reading.

        A value lower than the last one still enables the button (meter
        replacement / correction are legitimate edge cases). Unknown last
        readings never block submission.
        """
        reading = _latest_reading(self._counter())
        for zone, value in pending.items():
            last = _float(reading.get(_ZONE_VALUE_FIELDS.get(zone)))
            if last is None or value != last:
                return False
        return bool(pending)

    def _counter(self) -> dict[str, Any]:
        _, counter = self.coordinator.find_counter(self._counter_uuid)
        return counter

    @property
    def available(self) -> bool:
        pending = self.coordinator.pending_for(self._counter_uuid)
        return (
            super().available
            and bool(pending)
            and not self._unchanged(pending)
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"pending": self.coordinator.pending_for(self._counter_uuid)}

    async def async_press(self) -> None:
        """POST every pending zone of this meter in one mutation call."""
        pending = self.coordinator.pending_for(self._counter_uuid)
        if not pending:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_pending_readings",
            )
        await self.coordinator.async_submit_readings(
            {self._counter_uuid: dict(pending)}
        )
