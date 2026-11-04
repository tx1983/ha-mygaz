"""Minimal async GraphQL client for the «Мой ГАЗ» personal account.

The web cabinet (мойгаз.смородина.онлайн, Смородина platform) talks to a
single GraphQL endpoint. Auth is a plain login+password mutation
(``signInN3``) returning an opaque token which is then sent in the ``token``
HTTP header on every subsequent request. The token eventually expires
server-side (every query just starts failing), so the client keeps the
credentials and signs in again transparently, once, before giving up.

Only the fields the integration actually uses are requested — GraphQL is
fine with any subset of the cabinet's mega-query.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

SITE_URL = "https://xn--80afnfom.xn--80ahmohdapg.xn--80asehdb"
API_URL = f"{SITE_URL}/abr-lka-backend"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

SIGN_IN_QUERY = """
mutation signInN3($deviceInfo: DeviceInfoInputV2!, $input: ClientSignInInputV2!) {
  signInN3(deviceInfo: $deviceInfo, input: $input) {
    ok
    error
    hasAgreement
    token
  }
}
"""

ACCOUNTS_QUERY = """
query AccountList {
  accountsList {
    ok
    error
    data {
      id
      type
      number: account
      address
      alias
      isFull
      source: provider {
        id
        name
      }
    }
  }
}
"""

ACCOUNT_INFO_QUERY = """
query AccountInfo($account: AccountInput!) {
  accountInfo(account: $account) {
    ok
    error
    data {
      id
      number: account
      type
      isFull
      alias
      address
      balance
      paymentSourceAvailable
      providers {
        id
        name
        type {
          code
          name
          measure: measureByDefault
        }
      }
      lspus {
        id
        providerId
        number: account
        balance
        commission: comissionTotal
      }
      services {
        lspuId
        lspu
        id
        name
        balance
        providerId
        sort
        serviceType {
          code
          name
        }
        regimes: children {
          norm
          price
          tariff
          name
          startDate
          endDate
        }
      }
      counters {
        lspuId
        name
        uuid
        serialNumber
        numberOfRates
        state
        notification
        averageRate
        serviceName
        needVerification
        position
        model
        checkDate
        commissionedOn
        tariff
        measure
        values {
          valueDay
          valueMiddle
          valueNight
          rate
          state
          source
          date
        }
        price {
          day
          middle
          night
        }
      }
      equipments {
        lspuId
        type
        name
        state
      }
      balances {
        lspuId
        uuid
        date
        name
        balanceStartSum
        balanceEndSum
        chargedSum
        chargedVolume
        circulationSum
        forgivenDebt
        paymentAdjustments
        plannedSum
        privilegeSum
        privilegeVolume
        restoredDebt
        debtSum
        paidSum
      }
      notifications {
        id
        title
        content
        createDate
      }
    }
  }
}
"""

INDICATION_SEND_MUTATION = """
mutation indicationSendV4($deviceInfo: DeviceInfoInputV2!, $input: IndicationSendV4Input!) {
  indicationSendV4(deviceInfo: $deviceInfo, input: $input) {
    ok
    error
    data {
      ...IndicationSendResult
      __typename
    }
    __typename
  }
}

fragment IndicationSendCounter on CounterIndicationResult {
  uuid
  sent
  message
  __typename
}

fragment IndicationSendResult on LspuIndicationResult {
  lspuId
  online
  calculated
  counters {
    ...IndicationSendCounter
    __typename
  }
  __typename
}
"""

MESSAGES_QUERY = """
query Messages($lsId: Int!, $isEls: Boolean!, $senderId: Int) {
  clientMessages(
    pagination: {pageNum: 0, onPage: 100}
    senderId: $senderId
    isEls: $isEls
    lsId: $lsId
  ) {
    ok
    error
    data {
      id
      creator
      sender
      account
      title
      text: message
      type: messageType
      sendDate
      readDate
    }
  }
}
"""


# The same DeviceInfoInputV2 block the web cabinet sends on every mutation.
_DEVICE_INFO = {
    "appName": "desktop",
    "appVersion": "7.9.7",
    "browser": "Chrome 156.0.0.0",
    "device": "",
    "system": "Windows 11",
    "screenResolution": "1920x1080",
}

# Tariff zone key -> IndicationSendV4 counter field.
_ZONE_VALUE_FIELDS = {
    "day": "valueDay",
    "night": "valueNight",
    "middle": "valueMiddle",
}
_ZONE_OVERLAP_FIELDS = {
    "day": "overlapDay",
    "night": "overlapNight",
    "middle": "overlapMiddle",
}


class MyGazError(Exception):
    """Generic API error."""


class MyGazAuthError(MyGazError):
    """Credentials rejected — reauth required."""


def async_get_session(hass: HomeAssistant) -> aiohttp.ClientSession:
    """HA shared session. No cookies — header token auth only."""
    return async_get_clientsession(hass)


class MyGazClient:
    """Thin async wrapper around the cabinet's GraphQL endpoint."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        identifier: str,
        password: str,
    ) -> None:
        self._session = session
        self.identifier = identifier
        self._password = password
        self.token: str | None = None

    async def async_sign_in(self) -> None:
        """Run the signInN3 mutation and store the session token."""
        payload = {
            "operationName": "signInN3",
            "variables": {
                "deviceInfo": dict(_DEVICE_INFO),
                "input": {
                    "identifier": self.identifier,
                    "password": self._password,
                    "agreement": False,
                    "rememberMe": True,
                },
            },
            "query": SIGN_IN_QUERY,
        }
        body = await self._post(payload, auth=False)
        result = (body.get("data") or {}).get("signInN3") or {}
        token = result.get("token")
        if not result.get("ok") or not token:
            raise MyGazAuthError(
                f"Sign-in rejected: {result.get('error') or body.get('errors')}"
            )
        self.token = token

    async def _post(self, payload: dict[str, Any], auth: bool = True) -> dict[str, Any]:
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Origin": SITE_URL,
            "Referer": f"{SITE_URL}/",
            "User-Agent": USER_AGENT,
        }
        if auth and self.token:
            headers["token"] = self.token
        try:
            async with self._session.post(
                API_URL,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status in (401, 403):
                    raise MyGazAuthError(f"HTTP {resp.status}")
                resp.raise_for_status()
                body = await resp.json(content_type=None)
        except MyGazAuthError:
            raise
        except aiohttp.ClientResponseError as err:
            raise MyGazError(f"HTTP {err.status}") from err
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise MyGazError(f"Request failed: {err}") from err
        if not isinstance(body, dict):
            raise MyGazError("Unexpected GraphQL response")
        return body

    async def _query(
        self,
        operation: str,
        query: str,
        variables: dict[str, Any],
        field: str,
        _retried: bool = False,
    ) -> Any:
        """Run a GraphQL query; on failure re-login once and retry."""
        if not self.token:
            await self.async_sign_in()
        body = await self._post(
            {"operationName": operation, "variables": variables, "query": query}
        )
        result = (body.get("data") or {}).get(field) or {}
        errors = body.get("errors")
        if errors or not result.get("ok", not errors):
            if not _retried:
                # Most likely the token died — get a fresh one and retry once.
                _LOGGER.debug("%s failed (%s), re-signing in", operation, errors or result.get("error"))
                await self.async_sign_in()
                return await self._query(operation, query, variables, field, _retried=True)
            raise MyGazError(
                f"{operation} failed: {errors or result.get('error') or 'unknown'}"
            )
        return result.get("data")

    async def async_get_accounts(self) -> list[dict[str, Any]]:
        """All personal accounts (ЕЛС and provider-specific) bound to the login."""
        data = await self._query("AccountList", ACCOUNTS_QUERY, {}, "accountsList")
        return data if isinstance(data, list) else []

    async def async_get_account_info(self, account: dict[str, Any]) -> dict[str, Any]:
        """Full summary for one account: balances, services, counters, etc."""
        data = await self._query(
            "AccountInfo",
            ACCOUNT_INFO_QUERY,
            {"account": {"id": account["id"], "type": account["type"]}},
            "accountInfo",
        )
        return data if isinstance(data, dict) else {}

    async def async_get_messages(self, account: dict[str, Any]) -> list[dict[str, Any]]:
        """Inbox messages for the account. Non-fatal: returns [] on failure."""
        try:
            data = await self._query(
                "Messages",
                MESSAGES_QUERY,
                {
                    "lsId": account["id"],
                    "isEls": account.get("type") == "ELS",
                    "senderId": None,
                },
                "clientMessages",
            )
        except MyGazError as err:
            _LOGGER.debug("clientMessages failed: %s", err)
            return []
        return data if isinstance(data, list) else []

    async def async_submit_reading(
        self,
        *,
        els_id: int,
        lspu_id: int,
        uuid: str,
        values: dict[str, float],
        service_id: int = 0,
    ) -> dict[str, Any]:
        """POST new readings of ONE meter via the indicationSendV4 mutation.

        ``values`` maps tariff zone keys (``"day"``/``"night"``/``"middle"``)
        to readings — zones absent from the map are sent as null, like the
        cabinet does for zones the meter does not have. The ``overlap*``
        flags are always false, mirroring the real web payload.

        Returns the per-counter result row ``{uuid, sent, message}``.
        """
        counter_input: dict[str, Any] = {
            "uuid": uuid,
            "serviceId": service_id,
        }
        for zone, value_field in _ZONE_VALUE_FIELDS.items():
            counter_input[value_field] = values.get(zone)
            counter_input[_ZONE_OVERLAP_FIELDS[zone]] = False

        data = await self._query(
            "indicationSendV4",
            INDICATION_SEND_MUTATION,
            {
                "deviceInfo": dict(_DEVICE_INFO),
                "input": {
                    "elsId": els_id,
                    "lspuGroups": [
                        {
                            "lspuId": lspu_id,
                            "counters": [counter_input],
                        }
                    ],
                },
            },
            "indicationSendV4",
        )
        # data is one LspuIndicationResult per submitted lspuGroup — the
        # API may wrap it in a list or return it bare.
        results = data if isinstance(data, list) else [data]
        for group in results:
            if not isinstance(group, dict):
                continue
            for result in group.get("counters") or []:
                if result.get("uuid") == uuid:
                    return result
        raise MyGazError("indicationSendV4 returned no result for the counter")
