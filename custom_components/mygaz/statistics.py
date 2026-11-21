"""Backfill meter reading (and price) history into the sensors' statistics.

The counter «Показания» sensors are TOTAL_INCREASING — their statistics are
what the History panel graphs and what the energy dashboard consumes. This
module imports the API's reading history straight into the ``sensor.<...>``
statistic rows so both show the full staircase immediately instead of only
data collected after install. It also backfills the service «стоимость»
price sensor with hourly mean rows at the same timestamps — useful for the
history panel; note the energy dashboard's price option (entity_energy_price)
reads the entity's live state, not its statistics, and costs are only accrued
going forward. For costs the dashboard should use the «Начислено итого»
sensor (running total of the real monthly charges) as stat_cost — its
history is backfilled here too.

Only rows older than the oldest recorded statistic are imported — hourly
stats already written by the recorder are never touched, and restarts don't
duplicate data. Readings that drop below the previous accepted value (meter
replacement/correction artefacts like a reset to 4 after 4026) are skipped
to keep the cumulative series monotonic.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.recorder.statistics import (
    StatisticData,
    StatisticMetaData,
    import_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import MyGazCoordinator
from .sensor import (
    _charged_points,
    _float,
    _is_billed_service,
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


def _charged_total_stats(rows: list[dict[str, Any]]) -> list[StatisticData]:
    """Running total of the periods' ``chargedSum`` — «Начислено итого».

    Built from the same points the live sensor sums on its first run, so
    the imported tail and the sensor state agree.
    """
    stats: list[StatisticData] = []
    total = 0.0
    for date, value in _charged_points(rows):
        start = _parse_dt(date)
        if start is None:
            continue
        total += value
        start = start.replace(minute=0, second=0, microsecond=0)
        stats.append(
            StatisticData(start=start, state=round(total, 2), sum=round(total, 2))
        )
    return stats


def _stat_start(row: dict[str, Any]) -> datetime | None:
    """Normalize the 'start' value of a recorded statistics row."""
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


# Lower bound for "everything recorded so far" queries.
_EPOCH = datetime(2000, 1, 1, tzinfo=dt_util.UTC)


async def _db_job(hass: HomeAssistant, func: Any, *args: Any) -> Any:
    """Run a statistics query on the recorder's own database executor."""
    try:
        from homeassistant.components.recorder import get_instance

        instance = get_instance(hass)
    except Exception:  # recorder not loaded / internals differ
        instance = None
    if instance is not None:
        return await instance.async_add_executor_job(func, *args)
    return await hass.async_add_executor_job(func, *args)


async def _recorded_rows(
    hass: HomeAssistant,
    entity_id: str,
    start: datetime,
    end: datetime | None,
    stat_types: set[str],
) -> list[dict[str, Any]]:
    """Hourly statistic rows of ``entity_id`` with ``start <= row < end``."""
    result = await _db_job(
        hass,
        statistics_during_period,
        hass,
        start,
        end,
        {entity_id},
        "hour",
        None,
        stat_types,
    )
    return list((result or {}).get(entity_id) or [])


async def _import_stats(
    hass: HomeAssistant,
    entity_id: str,
    metadata: dict[str, Any],
    stats: list[StatisticData],
    stat_types: set[str],
) -> bool:
    """Import stats rows older than the entity's oldest recorded statistic.

    Two bounded queries instead of scanning the latest N rows:

    1. rows at or before the oldest candidate — any hit means the history is
       already in the database (the usual restart): done after a tiny query;
    2. only when something may be imported — rows from the oldest candidate
       on: the real oldest recorded row plus the rows after the imported
       tail for the negative-seam check.

    Returns True when rows were queued for import.
    """
    if not stats:
        return False
    stats = sorted(stats, key=lambda s: s["start"])
    first = stats[0]["start"]
    if await _recorded_rows(
        hass, entity_id, _EPOCH, first + timedelta(hours=1), stat_types
    ):
        return False
    rows = await _recorded_rows(hass, entity_id, first, None, stat_types)
    starts = [s for s in (_stat_start(r) for r in rows) if s]
    if (oldest := min(starts, default=None)) is not None:
        stats = [s for s in stats if s["start"] < oldest]
    if not stats:
        return False
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
    return True


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


def _price_jobs(
    hass: HomeAssistant,
    registry: er.EntityRegistry,
    info: dict[str, Any],
    starts: set[datetime],
) -> list[Awaitable[bool]]:
    """Imports backfilling service price entities with mean stats at reading hours."""
    jobs: list[Awaitable[bool]] = []
    if not starts:
        return jobs
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
        try:
            _, unit = _measure(
                _provider_measure(info, service.get("providerId"))
            )
            price_unit = f"₽/{unit}" if unit else "₽"
            stats = [
                StatisticData(start=s, state=p, mean=p, min=p, max=p)
                for s in sorted(starts)
                if (p := _price_at(regimes, s)) is not None
            ]
        except Exception:
            _LOGGER.exception("Не удалось подготовить цены %s", entity_id)
            continue
        jobs.append(
            _safe(
                _import_stats(
                    hass,
                    entity_id,
                    _metadata(hass, entity_id, price_unit, True, False),
                    stats,
                    {"mean"},
                ),
                entity_id,
            )
        )
    return jobs


async def _safe(coro: Awaitable[bool], entity_id: str) -> bool:
    """Run one entity's import; a failure must not stop the others."""
    try:
        return await coro
    except Exception:
        _LOGGER.exception("Не удалось импортировать историю %s", entity_id)
        return False


async def async_import_history(
    hass: HomeAssistant, coordinator: MyGazCoordinator
) -> None:
    """Import each counter's history into its sensor statistics.

    All entities are checked concurrently; on a usual restart every check is
    one tiny query and nothing is imported.
    """
    registry = er.async_get(hass)
    jobs: list[Awaitable[bool]] = []

    def add(entity_id: str | None, unit: str, build: Any) -> None:
        if not entity_id:
            return
        try:
            stats = build()
        except Exception:
            _LOGGER.exception("Не удалось подготовить историю %s", entity_id)
            return
        jobs.append(
            _safe(
                _import_stats(
                    hass,
                    entity_id,
                    _metadata(hass, entity_id, unit, False, True),
                    stats,
                    {"sum"},
                ),
                entity_id,
            )
        )

    for account_id in coordinator.data or {}:
        info = coordinator.data[account_id].get("info") or {}
        starts: set[datetime] = set()
        for counter in info.get("counters") or []:
            uuid = counter.get("uuid")
            _, unit = _measure(counter.get("measure"))
            if not uuid or not unit:
                continue
            try:
                stats = _history_stats(counter)
            except Exception:
                _LOGGER.exception("Не удалось подготовить историю счётчика %s", uuid)
                stats = []
            starts.update(s["start"] for s in stats)
            add(
                registry.async_get_entity_id(
                    "sensor", DOMAIN, f"{DOMAIN}_{uuid}_reading"
                ),
                unit,
                lambda stats=stats: stats,
            )
        jobs.extend(_price_jobs(hass, registry, info, starts))

        # --- running total of charges (₽) per settlement group -------------
        # «Начислено итого»: its state is the running total itself, so every
        # imported row has state == sum, like the live sensor.
        groups: dict[tuple, list[dict[str, Any]]] = {}
        for balance in info.get("balances") or []:
            groups.setdefault(
                (balance.get("lspuId"), balance.get("uuid")), []
            ).append(balance)
        for (lspu_id, uuid), rows in groups.items():
            add(
                registry.async_get_entity_id(
                    "sensor", DOMAIN, f"{DOMAIN}_{lspu_id}_{uuid}_charged_total"
                ),
                "₽",
                lambda rows=rows: _charged_total_stats(rows),
            )

    imported = await asyncio.gather(*jobs)
    _LOGGER.debug(
        "История: проверено %d сущностей, импорт в %d", len(jobs), sum(imported)
    )
