"""Sensors exposing account balance, mutual settlements, counters, messages."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import MyGazConfigEntry, MyGazCoordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# Counter measure (as returned by the API) → (device_class, unit).
_MEASURE_MAP: dict[str, tuple[SensorDeviceClass, str]] = {
    "м3": (SensorDeviceClass.GAS, UnitOfVolume.CUBIC_METERS),
    "m3": (SensorDeviceClass.GAS, UnitOfVolume.CUBIC_METERS),
    "квт*ч": (SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR),
    "квтч": (SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR),
}

RUB = "₽"

# serviceType codes that are not real billed services — e.g. the СОГАЗ
# insurance offer the cabinet advertises separately (its "balance" is the
# policy price, not an amount due).
_NON_SERVICE_TYPES = {"INDIVIDUAL_PROPERTY_INSURANCE"}


def _is_billed_service(service: dict[str, Any]) -> bool:
    code = (service.get("serviceType") or {}).get("code")
    return code not in _NON_SERVICE_TYPES


def _parse_dt(value: Any) -> datetime | None:
    """Best-effort parse of API date values."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if parsed := dt_util.parse_datetime(text):
        # Timestamp sensors require tz-aware values.
        return parsed if parsed.tzinfo else dt_util.as_local(parsed)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return dt_util.as_local(datetime.strptime(text, fmt))
        except ValueError:
            continue
    return None


def _naive(value: Any) -> datetime:
    """Parse to a naive datetime for safe comparisons."""
    dt = _parse_dt(value)
    return dt.replace(tzinfo=None) if dt else datetime.min


def _float(value: Any) -> float | None:
    """Convert API's stringy numbers ("177.48") to float."""
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _measure(measure: Any) -> tuple[SensorDeviceClass | None, str | None]:
    key = str(measure or "").lower().replace("³", "3").replace("·", "*").replace(" ", "")
    if key in _MEASURE_MAP:
        return _MEASURE_MAP[key]
    return None, str(measure) if measure else None


def _latest(items: list[dict[str, Any]], field: str = "date") -> dict[str, Any]:
    return max(items, key=lambda i: _naive(i.get(field)), default={})


def _latest_reading(counter: dict[str, Any]) -> dict[str, Any]:
    """Newest accepted reading; prefer user-submitted over billing on ties."""
    values = [v for v in counter.get("values") or [] if isinstance(v, dict)]
    if not values:
        return {}
    return max(
        values,
        key=lambda v: (_naive(v.get("date")), v.get("source") != "Биллинг"),
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyGazConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создание сенсоров MyGaz для каждого лицевого счёта."""
    coordinator = entry.runtime_data
    sensors = []

    for account_id in coordinator.data:
        info = coordinator.data[account_id].get("info") or {}
        sensors.append(MyGazBalanceSensor(coordinator, account_id))
        sensors.append(MyGazMessagesSensor(coordinator, account_id))
        sensors.append(MyGazLastMessageSensor(coordinator, account_id))

        for service in info.get("services") or []:
            if not _is_billed_service(service):
                continue
            sensors.append(MyGazServiceBalanceSensor(coordinator, account_id, service))
            if service.get("regimes"):
                sensors.extend(
                    MyGazServiceParamSensor(coordinator, account_id, service, kind)
                    for kind in MyGazServiceParamSensor.KINDS
                )

        # One set of settlement sensors per service balance (uuid) on each lspu.
        groups: dict[tuple, list[dict[str, Any]]] = {}
        for balance in info.get("balances") or []:
            groups.setdefault((balance.get("lspuId"), balance.get("uuid")), []).append(
                balance
            )
        for (lspu_id, uuid), rows in groups.items():
            sensors.extend(
                MyGazSettlementSensor(coordinator, account_id, lspu_id, uuid, kind)
                for kind in MyGazSettlementSensor.KINDS
            )

        for counter in info.get("counters") or []:
            sensors.append(MyGazCounterSensor(coordinator, account_id, counter))
            sensors.append(MyGazCounterRateSensor(coordinator, account_id, counter))
            sensors.append(MyGazCounterCheckDateSensor(coordinator, account_id, counter))

    async_add_entities(sensors)
    _LOGGER.debug("Создано %d сенсоров MyGaz", len(sensors))


class MyGazBaseSensor(CoordinatorEntity[MyGazCoordinator], SensorEntity):
    """Базовый класс — доступ к данным координатора."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MyGazCoordinator, account_id: str):
        self.account_id = account_id
        super().__init__(coordinator)

    def _block(self, name: str) -> Any:
        return (self.coordinator.data or {}).get(self.account_id, {}).get(name)

    def _info(self) -> dict[str, Any]:
        data = self._block("info")
        return data if isinstance(data, dict) else {}

    def _provider(self, provider_id: Any) -> dict[str, Any]:
        for provider in self._info().get("providers") or []:
            if provider.get("id") == provider_id:
                return provider
        return {}

    def _provider_name(self, provider_id: Any) -> str | None:
        return self._provider(provider_id).get("name")

    def _lspu(self, lspu_id: Any) -> dict[str, Any]:
        for lspu in self._info().get("lspus") or []:
            if lspu.get("id") == lspu_id:
                return lspu
        return {}

    def _lspu_device_info(self, lspu_id: Any) -> dict[str, Any]:
        """Device «Провайдер (номер ЛС)», nested under the account device."""
        lspu = self._lspu(lspu_id)
        if not lspu:
            return self._account_device_info()
        provider = self._provider_name(lspu.get("providerId")) or "Поставщик"
        return {
            "identifiers": {(DOMAIN, f"lspu_{lspu_id}")},
            "via_device": (DOMAIN, self.account_id),
            "name": f"{provider} ({lspu.get('number')})",
            "manufacturer": provider,
        }

    def _account_device_info(self) -> dict[str, Any]:
        account = self._block("account") or {}
        info = self._info()
        number = account.get("number") or info.get("number") or self.account_id
        providers = ", ".join(
            p["name"] for p in info.get("providers") or [] if p.get("name")
        )
        return {
            "identifiers": {(DOMAIN, self.account_id)},
            "name": account.get("alias") or info.get("alias") or f"ЛС {number}",
            "manufacturer": providers or "Мой ГАЗ",
            "model": account.get("address") or info.get("address"),
        }


class MyGazAccountSensor(MyGazBaseSensor):
    """Сенсор уровня лицевого счёта — живёт на устройстве ЛС."""

    def __init__(self, coordinator: MyGazCoordinator, account_id: str):
        super().__init__(coordinator, account_id)
        self._attr_device_info = self._account_device_info()


class MyGazBalanceSensor(MyGazAccountSensor):
    """Текущая сумма к оплате по лицевому счёту (баланс на конец периода)."""

    def __init__(self, coordinator: MyGazCoordinator, account_id: str):
        self._attr_unique_id = f"{DOMAIN}_{account_id}_balance"
        self._attr_name = "К оплате"
        self._attr_icon = "mdi:currency-rub"
        self._attr_native_unit_of_measurement = RUB
        super().__init__(coordinator, account_id)

    @property
    def native_value(self):
        return _float(self._info().get("balance"))

    @property
    def extra_state_attributes(self):
        info = self._info()
        return {
            "account": info.get("number"),
            "address": info.get("address"),
            "payment_source_available": info.get("paymentSourceAvailable"),
        }


class MyGazMessagesSensor(MyGazAccountSensor):
    """Непрочитанные сообщения от поставщиков."""

    def __init__(self, coordinator: MyGazCoordinator, account_id: str):
        self._attr_unique_id = f"{DOMAIN}_{account_id}_messages"
        self._attr_name = "Сообщения"
        self._attr_icon = "mdi:email"
        super().__init__(coordinator, account_id)

    def _messages(self) -> list[dict[str, Any]]:
        data = self._block("messages")
        return data if isinstance(data, list) else []

    @property
    def native_value(self):
        return sum(1 for m in self._messages() if not m.get("readDate"))

    @property
    def extra_state_attributes(self):
        messages = self._messages()
        return {
            "total": len(messages),
            "messages": [
                {
                    "title": m.get("title"),
                    "text": m.get("text"),
                    "sender": m.get("sender"),
                    "creator": m.get("creator"),
                    "type": m.get("type"),
                    "date": _parse_dt(m.get("sendDate")),
                    "read": bool(m.get("readDate")),
                }
                for m in sorted(
                    messages, key=lambda m: _naive(m.get("sendDate")), reverse=True
                )
            ],
        }


class MyGazLastMessageSensor(MyGazAccountSensor):
    """Последнее сообщение от поставщика — текст в состоянии."""

    def __init__(self, coordinator: MyGazCoordinator, account_id: str):
        self._attr_unique_id = f"{DOMAIN}_{account_id}_last_message"
        self._attr_name = "Последнее сообщение"
        self._attr_icon = "mdi:message-text"
        super().__init__(coordinator, account_id)

    def _last(self) -> dict[str, Any]:
        data = self._block("messages")
        messages = data if isinstance(data, list) else []
        return _latest(messages, "sendDate")

    @property
    def native_value(self):
        text = self._last().get("title") or self._last().get("text")
        return text[:255] if isinstance(text, str) else None

    @property
    def extra_state_attributes(self):
        last = self._last()
        return {
            "title": last.get("title"),
            "text": last.get("text"),
            "sender": last.get("sender"),
            "creator": last.get("creator"),
            "type": last.get("type"),
            "date": _parse_dt(last.get("sendDate")),
            "read": bool(last.get("readDate")),
            "account": last.get("account"),
        }


class MyGazLspuSensor(MyGazBaseSensor):
    """Сенсор на устройстве ЛСПУ (лицевой счёт у поставщика)."""

    def __init__(self, coordinator: MyGazCoordinator, account_id: str, lspu_id: Any):
        self.lspu_id = lspu_id
        super().__init__(coordinator, account_id)
        self._attr_device_info = self._lspu_device_info(lspu_id)


class MyGazServiceBalanceSensor(MyGazLspuSensor):
    """Сумма к оплате по конкретной услуге."""

    def __init__(
        self, coordinator: MyGazCoordinator, account_id: str, service: dict[str, Any]
    ):
        self._service_id = str(service.get("id"))
        self._attr_unique_id = (
            f"{DOMAIN}_{service.get('lspuId')}_{self._service_id}_balance"
        )
        self._attr_name = f"{service.get('name') or 'Услуга'} — к оплате"
        self._attr_icon = (
            "mdi:water-boiler"
            if "газ" in (service.get("name") or "").lower()
            else "mdi:file-document"
        )
        self._attr_native_unit_of_measurement = RUB
        super().__init__(coordinator, account_id, service.get("lspuId"))

    def _service(self) -> dict[str, Any]:
        for service in self._info().get("services") or []:
            if (
                str(service.get("id")) == self._service_id
                and service.get("lspuId") == self.lspu_id
            ):
                return service
        return {}

    @property
    def native_value(self):
        return _float(self._service().get("balance"))

    @property
    def extra_state_attributes(self):
        service = self._service()
        regime = (service.get("regimes") or [{}])[0] or {}
        service_type = service.get("serviceType") or {}
        return {
            "lspu": service.get("lspu"),
            "provider": self._provider_name(service.get("providerId")),
            "service_type": service_type.get("name"),
            "regime": regime.get("name"),
            "price": regime.get("price"),
            "norm": regime.get("norm"),
            "tariff": regime.get("tariff"),
        }


class MyGazServiceParamSensor(MyGazLspuSensor):
    """Параметр услуги из режима потребления (стоимость, норма)."""

    # (unique key, label, regime field, unit suffix, icon)
    KINDS: tuple[tuple[str, str, str, str, str], ...] = (
        ("price", "стоимость", "price", f"{RUB}/", "mdi:tag"),
        ("norm", "норма потребления", "norm", "", "mdi:gauge"),
    )

    def __init__(
        self,
        coordinator: MyGazCoordinator,
        account_id: str,
        service: dict[str, Any],
        kind: tuple[str, str, str, str, str],
    ):
        self._service_id = str(service.get("id"))
        self._kind, label, self._field, unit_prefix, icon = kind
        lspu_id = service.get("lspuId")
        self._attr_unique_id = f"{DOMAIN}_{lspu_id}_{self._service_id}_{self._kind}"
        self._attr_name = f"{service.get('name') or 'Услуга'} — {label}"
        self._attr_icon = icon
        super().__init__(coordinator, account_id, lspu_id)
        measure = (self._provider(service.get("providerId")).get("type") or {}).get(
            "measure"
        ) or ""
        _, unit = _measure(measure)
        self._attr_native_unit_of_measurement = (
            f"{unit_prefix}{unit or measure}" or None
        )

    def _regime(self) -> dict[str, Any]:
        for service in self._info().get("services") or []:
            if (
                str(service.get("id")) == self._service_id
                and service.get("lspuId") == self.lspu_id
            ):
                regimes = service.get("regimes") or []
                return _latest(regimes, "startDate")
        return {}

    @property
    def native_value(self):
        return _float(self._regime().get(self._field))

    @property
    def extra_state_attributes(self):
        regime = self._regime()
        return {
            "regime": regime.get("name"),
            "tariff": regime.get("tariff"),
            "start_date": _parse_dt(regime.get("startDate")),
            "end_date": _parse_dt(regime.get("endDate")),
        }


class MyGazSettlementSensor(MyGazLspuSensor):
    """Одна строка «Взаиморасчётов» по услуге за последний расчётный период."""

    # (unique key, cabinet label, balance field, unit, icon)
    KINDS: tuple[tuple[str, str, str, str | None, str], ...] = (
        ("start", "Баланс на начало месяца", "balanceStartSum", RUB, "mdi:calendar-arrow-left"),
        ("charged", "Сумма начислений", "chargedSum", RUB, "mdi:receipt-text"),
        ("volume", "Начисленный объём", "chargedVolume", None, "mdi:fire"),
        ("adjust", "Сумма корректировок", "paymentAdjustments", RUB, "mdi:tune"),
        ("paid", "Сумма оплаты", "paidSum", RUB, "mdi:cash-check"),
        ("end", "Баланс на конец месяца", "balanceEndSum", RUB, "mdi:calendar-arrow-right"),
        ("recalc", "Сумма перерасчётов", "circulationSum", RUB, "mdi:autorenew"),
    )

    def __init__(
        self,
        coordinator: MyGazCoordinator,
        account_id: str,
        lspu_id: Any,
        uuid: Any,
        kind: tuple[str, str, str, str | None, str],
    ):
        self._uuid = uuid
        self._kind, label, self._field, unit, icon = kind
        super().__init__(coordinator, account_id, lspu_id)
        self._attr_unique_id = f"{DOMAIN}_{lspu_id}_{uuid}_{self._kind}"
        self._attr_name = f"{self._row().get('name') or 'Услуга'} — {label}"
        self._attr_icon = icon
        if self._kind == "volume":
            provider = self._provider(self._lspu(self.lspu_id).get("providerId"))
            _, unit = _measure((provider.get("type") or {}).get("measure"))
        self._attr_native_unit_of_measurement = unit

    def _rows(self) -> list[dict[str, Any]]:
        return [
            b
            for b in self._info().get("balances") or []
            if b.get("lspuId") == self.lspu_id and b.get("uuid") == self._uuid
        ]

    def _row(self) -> dict[str, Any]:
        return _latest(self._rows())

    @property
    def native_value(self):
        return _float(self._row().get(self._field))

    @property
    def extra_state_attributes(self):
        row = self._row()
        return {
            "service": row.get("name"),
            "period": _parse_dt(row.get("date")),
            "debt": _float(row.get("debtSum")),
            "privilege_sum": _float(row.get("privilegeSum")),
            "privilege_volume": _float(row.get("privilegeVolume")),
            "forgiven_debt": _float(row.get("forgivenDebt")),
            "restored_debt": _float(row.get("restoredDebt")),
        }


class MyGazCounterBaseSensor(MyGazBaseSensor):
    """Базовый класс сенсоров счётчика — отдельное устройство под ЛСПУ."""

    def __init__(
        self, coordinator: MyGazCoordinator, account_id: str, counter: dict[str, Any]
    ):
        self._counter_uuid = counter.get("uuid")
        self.account_id = account_id

        serial = counter.get("serialNumber") or counter.get("name") or "?"
        lspu_id = counter.get("lspuId")
        device_info = {
            "identifiers": {(DOMAIN, str(self._counter_uuid))},
            "name": f"Счётчик {serial}",
            "manufacturer": "Мой ГАЗ",
            "model": counter.get("model"),
            "serial_number": counter.get("serialNumber"),
        }
        if self._lspu_static(coordinator, account_id, lspu_id):
            device_info["via_device"] = (DOMAIN, f"lspu_{lspu_id}")
        else:
            device_info["via_device"] = (DOMAIN, account_id)
        self._attr_device_info = device_info
        CoordinatorEntity.__init__(self, coordinator)

    @staticmethod
    def _lspu_static(
        coordinator: MyGazCoordinator, account_id: str, lspu_id: Any
    ) -> dict[str, Any]:
        info = (coordinator.data.get(account_id) or {}).get("info") or {}
        for lspu in info.get("lspus") or []:
            if lspu.get("id") == lspu_id:
                return lspu
        return {}

    def _counter(self) -> dict[str, Any]:
        for counter in self._info().get("counters") or []:
            if counter.get("uuid") == self._counter_uuid:
                return counter
        return {}


class MyGazCounterSensor(MyGazCounterBaseSensor):
    """Последние переданные показания счётчика."""

    def __init__(
        self, coordinator: MyGazCoordinator, account_id: str, counter: dict[str, Any]
    ):
        device_class, unit = _measure(counter.get("measure"))
        self._attr_unique_id = f"{DOMAIN}_{counter.get('uuid')}_reading"
        self._attr_name = "Показания"
        self._attr_device_class = device_class
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_native_unit_of_measurement = unit
        super().__init__(coordinator, account_id, counter)

    @property
    def native_value(self):
        return _float(_latest_reading(self._counter()).get("valueDay"))

    @property
    def extra_state_attributes(self):
        counter = self._counter()
        reading = _latest_reading(counter)
        values = [v for v in counter.get("values") or [] if isinstance(v, dict)]
        history = sorted(
            values, key=lambda v: _naive(v.get("date")), reverse=True
        )[:24]
        submitted = [v for v in values if v.get("source") != "Биллинг"]
        return {
            "serial_number": counter.get("serialNumber"),
            "state": counter.get("state"),
            "reading_date": _parse_dt(reading.get("date")),
            "reading_state": reading.get("state"),
            "source": reading.get("source"),
            "consumption": _float(reading.get("rate")),
            "average_rate": counter.get("averageRate"),
            "measure": counter.get("measure"),
            "tariff": counter.get("tariff"),
            "check_date": _parse_dt(counter.get("checkDate")),
            "need_verification": counter.get("needVerification"),
            "position": counter.get("position"),
            "readings_total": len(values),
            "last_submitted": _parse_dt(_latest(submitted, "date").get("date")),
            "readings": [
                {
                    "date": _parse_dt(v.get("date")),
                    "value_day": _float(v.get("valueDay")),
                    "value_middle": _float(v.get("valueMiddle")),
                    "value_night": _float(v.get("valueNight")),
                    "rate": _float(v.get("rate")),
                    "state": v.get("state"),
                    "source": v.get("source"),
                }
                for v in history
            ],
        }


class MyGazCounterRateSensor(MyGazCounterBaseSensor):
    """Расход по последней паре показаний."""

    def __init__(
        self, coordinator: MyGazCoordinator, account_id: str, counter: dict[str, Any]
    ):
        _, unit = _measure(counter.get("measure"))
        self._attr_unique_id = f"{DOMAIN}_{counter.get('uuid')}_consumption"
        self._attr_name = "Расход за период"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = "mdi:fire"
        super().__init__(coordinator, account_id, counter)

    @property
    def native_value(self):
        return _float(_latest_reading(self._counter()).get("rate"))

    @property
    def extra_state_attributes(self):
        return {"average_rate": self._counter().get("averageRate")}


class MyGazCounterCheckDateSensor(MyGazCounterBaseSensor):
    """Дата следующей поверки счётчика."""

    def __init__(
        self, coordinator: MyGazCoordinator, account_id: str, counter: dict[str, Any]
    ):
        self._attr_unique_id = f"{DOMAIN}_{counter.get('uuid')}_check_date"
        self._attr_name = "Дата поверки"
        self._attr_icon = "mdi:calendar-check"
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        super().__init__(coordinator, account_id, counter)

    @property
    def native_value(self):
        return _parse_dt(self._counter().get("checkDate"))

    @property
    def extra_state_attributes(self):
        return {
            "need_verification": self._counter().get("needVerification"),
            "commissioned_on": _parse_dt(self._counter().get("commissionedOn")),
        }
