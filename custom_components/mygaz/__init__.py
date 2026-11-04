"""The MyGaz (Мой ГАЗ / Смородина) integration."""
from __future__ import annotations

import logging
import re
from datetime import timedelta

import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .api import MyGazClient, async_get_session
from .const import (
    CONF_IDENTIFIER,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
)
from .coordinator import MyGazConfigEntry, MyGazCoordinator
from .statistics import async_import_history

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.NUMBER, Platform.BUTTON]

SERVICE_SUBMIT_READING = "submit_reading"

# unique_id of a reading NumberEntity: mygaz_{uuid}_new_reading_{zone}
_READING_UID_RE = re.compile(r"mygaz_(.+)_new_reading_(.+)$")

_SUBMIT_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): vol.All(cv.ensure_list, [cv.entity_id]),
        vol.Required("value"): vol.Coerce(float),
    }
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the submit_reading service once per domain."""

    async def _handle_submit_reading(call: ServiceCall) -> None:
        await _async_submit_reading(call)

    hass.services.async_register(
        DOMAIN, SERVICE_SUBMIT_READING, _handle_submit_reading, _SUBMIT_SCHEMA
    )
    return True


async def _async_submit_reading(call: ServiceCall) -> None:
    """Send a reading straight to the cabinet (skips the pending box).

    Target is the meter's «Новое показание» number entity — the counter
    uuid and tariff zone are read from its unique_id, so the service can
    never be aimed at a foreign entity.
    """
    hass = call.hass
    ent_reg = er.async_get(hass)
    value = float(call.data["value"])
    for entity_id in call.data["entity_id"]:
        entity_entry = ent_reg.async_get(entity_id)
        m = (
            _READING_UID_RE.match(entity_entry.unique_id)
            if entity_entry
            else None
        )
        if m is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_a_reading_field",
            )
        uuid, zone = m.groups()
        entry = hass.config_entries.async_get_entry(
            entity_entry.config_entry_id
        )
        if entry is None or getattr(entry, "runtime_data", None) is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entry_not_loaded",
            )
        await entry.runtime_data.async_submit_readings(
            {uuid: {zone: value}}
        )


async def async_setup_entry(hass: HomeAssistant, entry: MyGazConfigEntry) -> bool:
    """Настройка интеграции MyGaz по записи конфигурации."""
    client = MyGazClient(
        async_get_session(hass),
        identifier=entry.data[CONF_IDENTIFIER],
        password=entry.data[CONF_PASSWORD],
    )

    coordinator = MyGazCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Backfill meter history into long-term statistics. Must finish inside
    # setup — if the recorder compiles a zero-point row (sum=0) before the
    # imported tail lands, the dashboard shows a huge negative delta seam.
    try:
        await async_import_history(hass, coordinator)
    except Exception:
        _LOGGER.exception("Не удалось импортировать историю счётчиков")
    return True


async def _async_update_listener(hass: HomeAssistant, entry: MyGazConfigEntry) -> None:
    """Reload only when the scan interval actually changed."""
    coordinator = getattr(entry, "runtime_data", None)
    minutes = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES)
    if coordinator is None or coordinator.update_interval != timedelta(
        minutes=minutes
    ):
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: MyGazConfigEntry) -> bool:
    """Разгрузка интеграции."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
