"""Backfill meter reading (and price) history into the sensors' statistics.

The counter «Показания» sensors are TOTAL_INCREASING — their statistics are
what the History panel graphs and what the energy dashboard consumes. This
module imports the API's reading history straight into the ``sensor.<...>``
statistic rows so both show the full staircase immediately instead of only
data collected after install. It also backfills the service «стоимость»
price sensor with hourly mean rows at the same timestamps — useful for the
history panel; note the energy dashboard's price option (entity_energy_price)
reads the entity's live state, not its statistics, and costs are only accrued
going forward. For historic costs the dashboard must use the «Стоимость итого»
sensor as stat_cost — its history is backfilled here too.

Only rows older than the oldest recorded statistic are imported — hourly
stats already written by the recorder are never touched, and restarts don't
duplicate data. Readings that drop below the previous accepted value (meter
replacement/correction artefacts like a reset to 4 after 4026) are skipped
to keep the cumulative series monotonic.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

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
from .sensor import (
    _float,
    _is_billed_service,
    _iter_cost_periods,
    _iter_meter_periods,
    _measure,
    _naive,
    _parse_dt,
    _price_at,
)

_LOGGER = logging.getLogger(__name__)

# unit -> recorder "unit_class" (as of UNIT_CLASS_SCHEMA_VERSION).
_UNIT_CLASS = {"m³": "volume", "ft³": "volume", "kWh": "energy", "Wh": "energy"}


def _history_stats(counter: dict[str, Any]) -> list[StatisticData]:
    """Monotonic {start, state, sum} rows from the counter's readings.

    ``sum`` mirrors the meter's absolute value — the dashboard shows sum
    deltas, so the first imported period absorbs the meter's whole
    pre-history as one bar (same as a live sensor added mid-life).
    ``start`` is truncated to the top of the hour — the statistics table
    stores hourly aggregates and the importer rejects other timestamps.
    """
    stats: list[StatisticData] = []
    for start, reading, _volume in _iter_meter_periods(counter):
        start = start.replace(minute=0, second=0, microsecond=0)
        # Same-hour rows: keep the latest reading.
        stats = [s for s in stats if s["start"] != start]
        stats.append(StatisticData(start=start, state=reading, sum=reading))
    return stats


def _cost_stats(
    counter: dict[str, Any], regimes: list[dict[str, Any]]
) -> list[StatisticData]:
    """Cumulative cost rows — same math as the live «Стоимость итого» sensor.

    ``sum`` accumulates volume × regime price per period, so each period is
    locked at its own price and a later tariff never revalues history.
    """
    stats: list[StatisticData] = []
    total = 0.0
    for start, delta in _iter_cost_periods(counter, regimes):
        total += delta
        start = start.replace(minute=0, second=0, microsecond=0)
        stats = [s for s in stats if s["start"] != start]
        stats.append(
            StatisticData(start=start, state=round(total, 2), sum=round(total, 2))
        )
    return stats


def _stat_start(row: dict[str, Any]) -> datetime | None:
    """Normalize the 'start' value returned by get_last_statistics."""
    start = row.get("start")
    if isinstance(start, datetime):
        return start
    if isinstance(start, (int, float)):
        return dt_util.utc_from_timestamp(start)
    return _parse_dt(start)


def _metadata(hass: HomeAssistant, entity_id: str, unit: str, has_mean: bool, has_sum: bool) -> dict[str, Any]:
    """Base metadata shared by both import paths."""
    state = hass.states.get(entity_id)
    return {
        "has_mean": has_mean,
        "has_sum": has_sum,
        # Keep the entity's friendly name so the metadata row isn't nulled.
        "name": state.name if state else None,
        "source": "recorder",
        "statistic_id": entity_id,
        "unit_of_measurement": unit,
    }


async def _import_stats(
    hass: HomeAssistant,
    entity_id: str,
    metadata: dict[str, Any],
    stats: list[StatisticData],
    stat_types: set[str],
) -> None:
    """Import stats rows older than the entity's oldest recorded statistic."""
    if not stats:
        return
    existing = await hass.async_add_executor_job(
        get_last_statistics, hass, 5000, entity_id, True, stat_types
    )
    rows = existing.get(entity_id) or []
    starts = [s for s in (_stat_start(r) for r in rows) if s]
    if (oldest := min(starts, default=None)) is not None:
        stats = [s for s in stats if s["start"] < oldest]
    if not stats:
        return
    # A stale row after the imported tail with a lower sum (e.g. a zero-point
    # left behind by a purge) becomes a huge negative delta in the dashboard.
    # The import can't fix existing rows — warn so the entity can be purged.
    last_sum = stats[-1].get("sum")
    if last_sum:
        bad = [
            r
            for r in rows
            if (s := _stat_start(r))
            and s > stats[-1]["start"]
            and (r.get("sum") or 0.0) < last_sum
        ]
        if bad:
            _LOGGER.warning(
                "%s: после импортированной истории (sum=%s) есть строки с "
                "меньшим sum — на дашборде будет отрицательный скачок. "
                "Очистите статистику сущности и перезапустите HA.",
                entity_id,
                last_sum,
            )
    # Current HA API: statistics.async_import_statistics(hass, metadata,
    # stats) — a @callback that validates input and queues the job on the
    # recorder thread itself (executor calls hit "unsafe call not in
    # recorder thread"). Older versions had the sync
    # import_statistics(hass, metadata, stats) — kept as fallback.
    try:
        from homeassistant.components.recorder.models import StatisticMeanType
        from homeassistant.components.recorder.statistics import (
            async_import_statistics,
        )

        new_meta = dict(metadata)
        new_meta.setdefault(
            "mean_type",
            StatisticMeanType.ARITHMETIC
            if new_meta.get("has_mean")
            else StatisticMeanType.NONE,
        )
        new_meta.setdefault(
            "unit_class", _UNIT_CLASS.get(new_meta.get("unit_of_measurement"))
        )
        # @callback, not a coroutine — runs in the event loop and queues
        # the actual DB work onto the recorder thread.
        async_import_statistics(hass, new_meta, stats)
        if new_meta.get("has_sum") and stats:
            _seed_short_term(hass, new_meta, stats[-1])
    except (ImportError, TypeError):
        await hass.async_add_executor_job(
            import_statistics, hass, StatisticMetaData(**metadata), stats
        )
    _LOGGER.debug("Импортировано %d записей истории в %s", len(stats), entity_id)


def _seed_short_term(
    hass: HomeAssistant, metadata: dict[str, Any], tail: StatisticData
) -> None:
    """Mirror the imported tail into ``statistics_short_term``.

    The 5-minute statistics compiler continues ``sum`` from the latest
    *short-term* row — not from ``statistics``. Without this seed it finds an
    empty table, writes a zero-point row (sum=0), and the hourly rollup turns
    it into a giant negative delta right after the imported history.
    """
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.db_schema import (
            StatisticsShortTerm,
        )
        from homeassistant.components.recorder.tasks import ImportStatisticsTask

        if (instance := get_instance(hass)) is None:
            return
        seed = StatisticData(
            start=dt_util.utcnow(),
            state=tail.get("state"),
            sum=tail.get("sum"),
        )
        instance.queue_task(
            ImportStatisticsTask(
                StatisticMetaData(**metadata), [seed], StatisticsShortTerm
            )
        )
    except Exception:  # best effort — seeding must never break setup
        _LOGGER.debug("Не удалось сидировать short-term статистику", exc_info=True)


def _provider_measure(info: dict[str, Any], provider_id: Any) -> Any:
    for provider in info.get("providers") or []:
        if provider.get("id") == provider_id:
            return (provider.get("type") or {}).get("measure")
    return None


async def _import_price_history(
    hass: HomeAssistant,
    registry: er.EntityRegistry,
    info: dict[str, Any],
    starts: set[datetime],
) -> None:
    """Backfill service price entities with mean stats at reading hours."""
    if not starts:
        return
    for service in info.get("services") or []:
        if not _is_billed_service(service):
            continue
        regimes = [r for r in service.get("regimes") or [] if isinstance(r, dict)]
        if not regimes:
            continue
        entity_id = registry.async_get_entity_id(
            "sensor",
            DOMAIN,
            f"{DOMAIN}_{service.get('lspuId')}_{service.get('id')}_price",
        )
        if not entity_id:
            continue
        _, unit = _measure(
            _provider_measure(info, service.get("providerId"))
        )
        price_unit = f"₽/{unit}" if unit else "₽"
        stats = [
            StatisticData(start=s, state=p, mean=p, min=p, max=p)
            for s in sorted(starts)
            if (p := _price_at(regimes, s)) is not None
        ]
        try:
            await _import_stats(
                hass,
                entity_id,
                _metadata(hass, entity_id, price_unit, True, False),
                stats,
                {"mean"},
            )
        except Exception:
            _LOGGER.exception("Не удалось импортировать цены %s", entity_id)


async def _drain_recorder(hass: HomeAssistant) -> None:
    """Wait briefly for the recorder to commit queued statistic inserts.

    ``async_import_statistics`` only enqueues the write — without the drain a
    statistics compile can run first, see "no stats" and write a zero-point
    row, which later renders as a giant negative delta in the dashboard.
    The wait is bounded: a stuck recorder queue must never hang entry setup.
    """
    try:
        from homeassistant.components.recorder import get_instance

        if (instance := get_instance(hass)) is None:
            return
        await asyncio.wait_for(instance.async_block_till_done(), timeout=60)
    except (asyncio.TimeoutError, TimeoutError):
        _LOGGER.warning("Очередь recorder не освободилась за 60с — продолжаем")
    except Exception:  # best effort — recorder internals vary by version
        _LOGGER.debug("Не удалось дождаться очереди recorder", exc_info=True)


async def async_import_history(
    hass: HomeAssistant, coordinator: MyGazCoordinator
) -> None:
    """Import each counter's history into its sensor statistics, once."""
    registry = er.async_get(hass)
    for account_id in coordinator.data or {}:
        info = coordinator.data[account_id].get("info") or {}
        regimes_by_lspu = {
            s.get("lspuId"): s.get("regimes") or []
            for s in info.get("services") or []
            if _is_billed_service(s)
        }
        starts: set[datetime] = set()
        for counter in info.get("counters") or []:
            uuid = counter.get("uuid")
            _, unit = _measure(counter.get("measure"))
            if not uuid or not unit:
                continue
            entity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{DOMAIN}_{uuid}_reading"
            )
            stats = _history_stats(counter)
            starts.update(s["start"] for s in stats)
            if entity_id:
                try:
                    await _import_stats(
                        hass,
                        entity_id,
                        _metadata(hass, entity_id, unit, False, True),
                        stats,
                        {"sum"},
                    )
                except Exception:
                    _LOGGER.exception(
                        "Не удалось импортировать историю %s", entity_id
                    )
            cost_entity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{DOMAIN}_{uuid}_cost"
            )
            if cost_entity_id:
                try:
                    await _import_stats(
                        hass,
                        cost_entity_id,
                        _metadata(hass, cost_entity_id, "₽", False, True),
                        _cost_stats(
                            counter, regimes_by_lspu.get(counter.get("lspuId")) or []
                        ),
                        {"sum"},
                    )
                except Exception:
                    _LOGGER.exception(
                        "Не удалось импортировать стоимость %s", cost_entity_id
                    )
        await _import_price_history(hass, registry, info, starts)
    await _drain_recorder(hass)
