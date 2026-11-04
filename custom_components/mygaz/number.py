"""Number entities for entering new meter readings.

One box per tariff zone of every meter (day / night / middle, per the
counter's ``numberOfRates``). Typing a value does not send it immediately —
it is kept as «pending» and is POSTed together with the other zones of the
same meter when its «Передать показания» button is pressed, mirroring the
cabinet's single submission form.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MyGazConfigEntry, MyGazCoordinator
from .sensor import _float, _latest_reading, _measure

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# Tariff zones of a meter, in cabinet order, by numberOfRates:
# 1 rate -> day only; 2 -> day/night; 3 -> day/night/middle.
_ZONES = ("day", "night", "middle")
_ZONE_LABELS = {"day": "День", "night": "Ночь", "middle": "Средняя"}
_ZONE_VALUE_FIELDS = {
    "day": "valueDay",
    "night": "valueNight",
    "middle": "valueMiddle",
}


def counter_zones(counter: dict[str, Any]) -> list[str]:
    """Zone keys the meter has, per its numberOfRates."""
    rates = counter.get("numberOfRates") or 1
    try:
        rates = int(rates)
    except (TypeError, ValueError):
        rates = 1
    return list(_ZONES[: max(1, min(rates, len(_ZONES)))])


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyGazConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создание полей ввода показаний по каждой зоне каждого счётчика."""
    coordinator = entry.runtime_data
    numbers = []
    for account_id, block in (coordinator.data or {}).items():
        for counter in (block.get("info") or {}).get("counters") or []:
            numbers.extend(
                MyGazReadingNumber(coordinator, str(account_id), counter, zone)
                for zone in counter_zones(counter)
            )
    async_add_entities(numbers)
    _LOGGER.debug("Создано %d полей ввода показаний MyGaz", len(numbers))


class MyGazReadingNumber(CoordinatorEntity[MyGazCoordinator], NumberEntity):
    """Input box for a new reading of one tariff zone of a meter."""

    _attr_has_entity_name = True
    _attr_native_min_value = 0
    _attr_native_max_value = 1_000_000
    _attr_native_step = 1
    _attr_mode = "box"
    _attr_icon = "mdi:meter-gas-outline"

    def __init__(
        self,
        coordinator: MyGazCoordinator,
        account_id: str,
        counter: dict[str, Any],
        zone: str,
    ):
        super().__init__(coordinator)
        self.account_id = account_id
        self._counter_uuid = counter.get("uuid")
        self._zone = zone
        multi = len(counter_zones(counter)) > 1
        self._attr_name = "Новое показание" + (
            f" — {_ZONE_LABELS[zone]}" if multi else ""
        )
        self._attr_unique_id = (
            f"{DOMAIN}_{self._counter_uuid}_new_reading_{zone}"
        )
        _, unit = _measure(counter.get("measure"))
        self._attr_native_unit_of_measurement = unit
        self._attr_device_info = {
            "identifiers": {(DOMAIN, str(self._counter_uuid))},
        }

    def _counter(self) -> dict[str, Any]:
        _, counter = self.coordinator.find_counter(str(self._counter_uuid))
        return counter

    @property
    def native_value(self) -> float | None:
        """Typed-in value if pending, else the last reading of the zone."""
        pending = self.coordinator.pending_for(str(self._counter_uuid))
        if self._zone in pending:
            return pending[self._zone]
        reading = _latest_reading(self._counter())
        return _float(reading.get(_ZONE_VALUE_FIELDS[self._zone]))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        counter = self._counter()
        reading = _latest_reading(counter)
        return {
            "zone": self._zone,
            "pending": self.coordinator.pending_for(
                str(self._counter_uuid)
            ).get(self._zone),
            "serial_number": counter.get("serialNumber"),
            "last_reading": _float(
                reading.get(_ZONE_VALUE_FIELDS[self._zone])
            ),
            "last_reading_source": reading.get("source"),
        }

    async def async_set_native_value(self, value: float) -> None:
        """Keep the value as pending — the button submits it."""
        self.coordinator.set_pending(str(self._counter_uuid), self._zone, value)
