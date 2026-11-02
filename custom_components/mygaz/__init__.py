"""The MyGaz (Мой ГАЗ / Смородина) integration."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .api import MyGazClient, async_get_session
from .const import (
    CONF_IDENTIFIER,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_MINUTES,
)
from .coordinator import MyGazConfigEntry, MyGazCoordinator
from .statistics import async_import_history

PLATFORMS = [Platform.SENSOR]


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
    # Backfill meter history into long-term statistics in the background.
    entry.async_create_background_task(
        hass, async_import_history(hass, coordinator), "mygaz_import_history"
    )
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
