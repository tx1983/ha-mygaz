# Мой ГАЗ for Home Assistant

**Repository:** https://github.com/tx1983/ha-mygaz

Unofficial [Home Assistant](https://www.home-assistant.io/) integration for the «Мой ГАЗ» personal account ([мойгаз.смородина.онлайн](https://мойгаз.смородина.онлайн), Смородина platform). Polls the same GraphQL endpoint (`/abr-lka-backend`) the web cabinet uses and exposes per-account data as sensors.

## Features

- **Amount due** (RUB) per personal account (ЕЛС)
- **Mutual settlements** — one device per provider account (ЛСПУ) with the same rows the cabinet shows: month start/end balance, charged sum and volume, adjustments, payments, recalculations
- **Per-service balance** — amount due for every service (gas supply, insurance, …) with tariff/norm/price attributes
- **Meter readings** — one device per meter: last reading (`TOTAL_INCREASING`, works with the energy dashboard), consumption per period, next verification date; serial/model/check date in attributes
- **Reading history** — full history in sensor attributes (last 24 records with consumption, state and source) plus a one-off backfill into the sensor's own statistics: the History graph and the energy dashboard show past periods right away
- **Messages** — unread count with the full inbox in attributes, plus a sensor holding the last message text
- **Reading submission** — a number input per tariff zone on each meter device plus a «Передать показания» button that posts the entered values via the `indicationSendV4` mutation (values wait for the button, like on the site). For automations there is also a `mygaz.submit_reading` service (target: the meter's «Новое показание» input; field: `value`)
- **Multiple accounts** — every account on the login becomes its own device
- **Login + password auth** — `signInN3` GraphQL mutation; the token is refreshed automatically and re-authentication is only requested if the password itself stops working
- **Configurable polling interval** (5–1440 min, default 30)

## How auth works

The cabinet uses a simple token scheme: `signInN3(identifier, password)` returns a session token sent as the `token` HTTP header on all subsequent GraphQL requests. When the token expires server-side the integration signs in again transparently; a re-authentication prompt appears only if the login/password itself is rejected.

## Installation

1. Copy `custom_components/mygaz` into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

## Configuration

1. `Settings` → `Devices & services` → **Add integration** → search `MyGaz` / `Мой ГАЗ`.
2. Enter the **login (e-mail)** and **password** of your personal account.
3. Done — a device appears per personal account and per meter.

## TODO

- Payments and other write operations beyond meter readings.

## Disclaimer

This integration is unofficial and not affiliated with Смородина or the gas provider. It uses only the endpoints of the public web cabinet. Use at your own risk.

## Support the project

If this integration saves you time, you can
[buy me a coffee](https://pay.cloudtips.ru/p/a039d481).

## License

MIT — see LICENSE.
