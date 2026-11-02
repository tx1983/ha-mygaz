"""One-off import of counter reading history into long-term statistics.

Each counter gets an external statistic ``mygaz:<uuid>_consumption`` holding
the cumulative consumption (m³) rebuilt from the API's per-period ``rate``
deltas. Cumulative consumption (rather than raw meter readings) is imported
so meter replacements/resets never produce negative spikes — a new meter
starts from a low reading and its negative delta is simply skipped.

Import is skipped when the statistic already has rows, so re-runs on every
startup are cheap and idempotent.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.recorder.statistics import (
    StatisticData,
    StatisticMetaData,
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import MyGazCoordinator
from .sensor import _float, _measure, _naive, _parse_dt

_LOGGER = logging.getLogger(__name__)


def _consumption_stats(counter: dict[str, Any]) -> list[StatisticData]:
    """Build cumulative-consumption stats from the readings list."""
    values = [v for v in counter.get("values") or [] if isinstance(v, dict)]
    values.sort(key=lambda v: _naive(v.get("dateDt") or v.get("date")))
    by_start: dict[Any, float] = {}
    total = 0.0
    for value in values:
        start = _parse_dt(value.get("dateDt")) or _parse_dt(value.get("date"))
        if start is None:
            continue
        rate = _float(value.get("rate"))
        # Negative/missing deltas are resets or corrections, not usage.
        if rate and rate > 0:
            total += rate
        by_start[start] = total  # same-date rows: keep the accumulated last
    return [StatisticData(start=start, sum=by_start[start]) for start in by_start]


async def async_import_history(
    hass: HomeAssistant, coordinator: MyGazCoordinator
) -> None:
    """Import history for every counter that does not have stats yet."""
    for account_id in coordinator.data or {}:
        info = coordinator.data[account_id].get("info") or {}
        for counter in info.get("counters") or []:
            uuid = counter.get("uuid")
            _, unit = _measure(counter.get("measure"))
            if not uuid or not unit:
                continue
            statistic_id = f"{DOMAIN}:{uuid}_consumption"
            stats = _consumption_stats(counter)
            if not stats:
                continue
            try:
                last = await hass.async_add_executor_job(
                    get_last_statistics, hass, 1, statistic_id, True, {"sum"}
                )
                if last.get(statistic_id):
                    continue
                metadata = StatisticMetaData(
                    has_mean=False,
                    has_sum=True,
                    name=f"Счётчик {counter.get('serialNumber') or counter.get('name')} — расход (история)",
                    source=DOMAIN,
                    statistic_id=statistic_id,
                    unit_of_measurement=unit,
                )
                await async_add_external_statistics(hass, metadata, stats)
                _LOGGER.debug(
                    "Импортировано %d записей истории в %s",
                    len(stats),
                    statistic_id,
                )
            except Exception:
                _LOGGER.exception(
                    "Не удалось импортировать историю %s", statistic_id
                )
