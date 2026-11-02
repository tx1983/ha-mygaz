"""Backfill meter reading history into the sensor's own statistics.

The counter «Показания» sensors are TOTAL_INCREASING — their statistics are
what the History panel graphs and what the energy dashboard consumes. This
module imports the API's reading history straight into the ``sensor.<...>``
statistic rows so both show the full staircase immediately instead of only
data collected after install.

Only rows older than the oldest recorded statistic are imported — hourly
stats already written by the recorder are never touched, and restarts don't
duplicate data. Readings that drop below the previous accepted value (meter
replacement/correction artefacts like a reset to 4 after 4026) are skipped
to keep the cumulative series monotonic.
"""
from __future__ import annotations

import inspect
import logging
from datetime import datetime
from typing import Any

from homeassistant.components.recorder.models import Statistics
from homeassistant.components.recorder.statistics import (
    StatisticData,
    StatisticMetaData,
    get_last_statistics,
    import_statistics,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import MyGazCoordinator
from .sensor import _float, _measure, _naive, _parse_dt

_LOGGER = logging.getLogger(__name__)

# Newer HA requires the target table (Statistics vs StatisticsShortTerm)
# as an extra positional argument to import_statistics.
_IMPORT_NEEDS_TABLE = "table" in inspect.signature(import_statistics).parameters


def _history_stats(counter: dict[str, Any]) -> list[StatisticData]:
    """Monotonic {start, state, sum} rows from the counter's readings."""
    values = [v for v in counter.get("values") or [] if isinstance(v, dict)]
    values.sort(key=lambda v: _naive(v.get("dateDt") or v.get("date")))
    stats: list[StatisticData] = []
    top: float | None = None
    for value in values:
        start = _parse_dt(value.get("dateDt")) or _parse_dt(value.get("date"))
        reading = _float(value.get("valueDay"))
        if start is None or reading is None:
            continue
        if top is not None and reading <= top:
            continue  # reset/correction artefact — keep the series monotonic
        top = reading
        stats.append(StatisticData(start=start, state=reading, sum=reading))
    return stats


def _stat_start(row: dict[str, Any]) -> datetime | None:
    """Normalize the 'start' value returned by get_last_statistics."""
    start = row.get("start")
    if isinstance(start, datetime):
        return start
    if isinstance(start, (int, float)):
        return dt_util.utc_from_timestamp(start)
    return _parse_dt(start)


async def _import_for_counter(
    hass: HomeAssistant, entity_id: str, unit: str, counter: dict[str, Any]
) -> None:
    """Import history rows older than the entity's oldest statistic."""
    stats = _history_stats(counter)
    if not stats:
        return
    existing = await hass.async_add_executor_job(
        get_last_statistics, hass, 5000, entity_id, True, {"sum"}
    )
    rows = existing.get(entity_id) or []
    starts = [s for s in (_stat_start(r) for r in rows) if s]
    oldest = min(starts, default=None)
    if oldest is not None:
        stats = [s for s in stats if s["start"] < oldest]
    if not stats:
        return
    metadata = StatisticMetaData(
        has_mean=False,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id=entity_id,
        unit_of_measurement=unit,
    )
    args: tuple = (hass, metadata, stats)
    if _IMPORT_NEEDS_TABLE:
        args += (Statistics,)
    await hass.async_add_executor_job(import_statistics, *args)
    _LOGGER.debug(
        "Импортировано %d записей истории в %s", len(stats), entity_id
    )


async def async_import_history(
    hass: HomeAssistant, coordinator: MyGazCoordinator
) -> None:
    """Import each counter's history into its sensor statistics, once."""
    registry = er.async_get(hass)
    for account_id in coordinator.data or {}:
        info = coordinator.data[account_id].get("info") or {}
        for counter in info.get("counters") or []:
            uuid = counter.get("uuid")
            _, unit = _measure(counter.get("measure"))
            if not uuid or not unit:
                continue
            entity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{DOMAIN}_{uuid}_reading"
            )
            if not entity_id:
                continue
            try:
                await _import_for_counter(hass, entity_id, unit, counter)
            except Exception:
                _LOGGER.exception(
                    "Не удалось импортировать историю %s", entity_id
                )
