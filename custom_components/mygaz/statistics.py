"""Backfill meter reading (and price) history into the sensors' statistics.

The counter «Показания» sensors are TOTAL_INCREASING — their statistics are
what the History panel graphs and what the energy dashboard consumes. This
module imports the API's reading history straight into the ``sensor.<...>``
statistic rows so both show the full staircase immediately instead of only
data collected after install. It also backfills the service «стоимость»
price sensor with hourly mean rows at the same timestamps, so the energy
dashboard can compute costs for the imported periods (entity_energy_price
reads the price entity's statistics).

Only rows older than the oldest recorded statistic are imported — hourly
stats already written by the recorder are never touched, and restarts don't
duplicate data. Readings that drop below the previous accepted value (meter
replacement/correction artefacts like a reset to 4 after 4026) are skipped
to keep the cumulative series monotonic.
"""
from __future__ import annotations

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
from .sensor import _float, _is_billed_service, _latest, _measure, _naive, _parse_dt

_LOGGER = logging.getLogger(__name__)

# unit -> recorder "unit_class" (as of UNIT_CLASS_SCHEMA_VERSION).
_UNIT_CLASS = {"m³": "volume", "ft³": "volume", "kWh": "energy", "Wh": "energy"}


def _history_stats(counter: dict[str, Any]) -> list[StatisticData]:
    """Monotonic {start, state, sum} rows from the counter's readings.

    ``start`` is truncated to the top of the hour — the statistics table
    stores hourly aggregates and the importer rejects other timestamps.
    """
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
        start = start.replace(minute=0, second=0, microsecond=0)
        # Same-hour rows: keep the latest reading.
        stats = [s for s in stats if s["start"] != start]
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
    except (ImportError, TypeError):
        await hass.async_add_executor_job(
            import_statistics, hass, StatisticMetaData(**metadata), stats
        )
    _LOGGER.debug("Импортировано %d записей истории в %s", len(stats), entity_id)


def _provider_measure(info: dict[str, Any], provider_id: Any) -> Any:
    for provider in info.get("providers") or []:
        if provider.get("id") == provider_id:
            return (provider.get("type") or {}).get("measure")
    return None


def _price_at(regimes: list[dict[str, Any]], start: datetime) -> float | None:
    """Price of the regime in effect at ``start`` (falls back to latest)."""
    naive_start = start.replace(tzinfo=None)
    best: dict[str, Any] | None = None
    for regime in regimes:
        begin = _naive(regime.get("startDate"))
        end = _naive(regime.get("endDate")) if regime.get("endDate") else datetime.max
        if begin <= naive_start < end:
            return _float(regime.get("price"))
        if begin <= naive_start and (best is None or begin > _naive(best.get("startDate"))):
            best = regime
    return _float((best or _latest(regimes, "startDate")).get("price"))


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


async def async_import_history(
    hass: HomeAssistant, coordinator: MyGazCoordinator
) -> None:
    """Import each counter's history into its sensor statistics, once."""
    registry = er.async_get(hass)
    for account_id in coordinator.data or {}:
        info = coordinator.data[account_id].get("info") or {}
        starts: set[datetime] = set()
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
            stats = _history_stats(counter)
            starts.update(s["start"] for s in stats)
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
        await _import_price_history(hass, registry, info, starts)
