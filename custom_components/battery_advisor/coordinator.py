"""Data coordinator for Battery Dispatch Advisor."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant, callback, Event
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    ACTION_IDLE,
)

_LOGGER = logging.getLogger(__name__)

SOC_STEP_KWH = 0.5


# ---------------------------------------------------------------------------
# Price extraction — supports multiple HA sensor formats
# ---------------------------------------------------------------------------

def _extract_prices(state_obj: Any) -> list[dict]:
    """
    Extract an ordered list of hourly price dicts from a HA state object.

    Supported sensor formats
    ------------------------
    1. Official Nordpool / custom Nordpool HACS:
       attributes: raw_today = [{start, end, value}, ...]
                   raw_tomorrow = [{start, end, value}, ...]   (optional)
       Price unit auto-detected: if max value < 10 → assumed EUR/kWh → convert to EUR/MWh

    2. Zonneplan ONE (HACS — sensor.zonneplan_current_electricity_tariff):
       attributes: forecast = [{datetime, electricity_price, electricity_price_excl_tax, ...}, ...]
       datetime format : "2026-03-05T14:00:00.000Z"  (UTC, millisecond precision)
       electricity_price: incl. tax, in Zonneplan micro-units (divide by 10,000,000 for EUR/kWh)

    3. Tibber (via HACS):
       attributes: prices = [{startsAt, total, ...}, ...]

    4. Generic list sensor:
       attributes: today / prices / hourly_prices = [float, ...]  (24 floats from midnight)

    Returns list of dicts:  {ts, datetime, hour, price}  where price is EUR/MWh.
    Raises ValueError if no usable data found.
    """
    if state_obj is None or state_obj.state in ("unavailable", "unknown", None):
        raise ValueError("Price sensor is unavailable or has no state")

    attrs = state_obj.attributes or {}
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()

    # ------------------------------------------------------------------
    # Format 1: Nordpool (official built-in or HACS custom component)
    # Attributes: raw_today / raw_tomorrow  →  [{start, end, value}, ...]
    # ------------------------------------------------------------------
    raw_today    = attrs.get("raw_today")
    raw_tomorrow = attrs.get("raw_tomorrow")
    tomorrow_valid = attrs.get("tomorrow_valid", False)

    if raw_today and isinstance(raw_today, (list, tuple)) and len(raw_today) > 0:
        combined = list(raw_today)
        if tomorrow_valid and raw_tomorrow:
            combined += list(raw_tomorrow)

        prices = []
        for slot in combined:
            # slot may be a dict or a namedtuple-like object
            try:
                start_str = slot.get("start") if isinstance(slot, dict) else getattr(slot, "start", None)
                value     = slot.get("value") if isinstance(slot, dict) else getattr(slot, "value", None)
            except Exception:
                continue

            if start_str is None or value is None:
                continue

            try:
                if isinstance(start_str, str):
                    # Parse ISO string; handle both offset-aware and naive
                    dt = datetime.fromisoformat(start_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = start_str  # already a datetime
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue

            ts = dt.timestamp()
            if ts < now_ts - 3600:
                continue

            price_mwh = _to_eur_mwh(float(value), attrs)
            prices.append({
                "ts":       ts,
                "datetime": dt.isoformat(),
                "hour":     dt.astimezone().strftime("%H:00"),
                "price":    round(price_mwh, 2),
            })

        if prices:
            return sorted(prices, key=lambda x: x["ts"])[:24]

    # ------------------------------------------------------------------
    # Format 2: Zonneplan ONE (HACS) — attributes.forecast = [{datetime, electricity_price, ...}, ...]
    # datetime : ISO UTC string with ms  e.g. "2026-03-05T14:00:00.000Z"
    # electricity_price : price incl. tax in Zonneplan micro-units (1/10,000,000 EUR/kWh)
    # electricity_price_excl_tax : same unit, excl. tax
    # ------------------------------------------------------------------
    zonneplan_forecast = attrs.get("forecast")
    if zonneplan_forecast and isinstance(zonneplan_forecast, (list, tuple)) and len(zonneplan_forecast) > 0:
        first = zonneplan_forecast[0]
        if isinstance(first, dict) and "electricity_price" in first and "datetime" in first:
            prices = []
            for slot in zonneplan_forecast:
                try:
                    dt_str = slot.get("datetime")
                    raw_price = slot.get("electricity_price")
                    if dt_str is None or raw_price is None:
                        continue
                    # Parse UTC datetime — Zonneplan uses "2026-03-05T14:00:00.000Z" format
                    dt_str_clean = dt_str.rstrip("Z").split(".")[0]  # strip ms and Z
                    dt = datetime.fromisoformat(dt_str_clean).replace(tzinfo=timezone.utc)
                    ts = dt.timestamp()
                    if ts < now_ts - 3600:
                        continue
                    # Convert from Zonneplan micro-unit to EUR/MWh:
                    #   raw / 10_000_000  → EUR/kWh  ×  1000  → EUR/MWh
                    price_mwh = float(raw_price) / 10_000_000 * 1000
                    prices.append({
                        "ts":       ts,
                        "datetime": dt.isoformat(),
                        "hour":     dt.astimezone().strftime("%H:00"),
                        "price":    round(price_mwh, 2),
                    })
                except Exception:
                    continue
            if prices:
                return sorted(prices, key=lambda x: x["ts"])[:24]

    # ------------------------------------------------------------------
    # Format 3: Tibber (HACS) — attributes.prices = [{startsAt, total}, ...]
    # ------------------------------------------------------------------
    tibber_prices = attrs.get("prices")
    if tibber_prices and isinstance(tibber_prices, (list, tuple)) and len(tibber_prices) > 0:
        first = tibber_prices[0]
        if isinstance(first, dict) and "startsAt" in first:
            prices = []
            for slot in tibber_prices:
                try:
                    dt = datetime.fromisoformat(slot["startsAt"])
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    ts = dt.timestamp()
                    if ts < now_ts - 3600:
                        continue
                    value = float(slot.get("total") or slot.get("energy") or 0)
                    price_mwh = _to_eur_mwh(value, attrs)
                    prices.append({
                        "ts":       ts,
                        "datetime": dt.isoformat(),
                        "hour":     dt.astimezone().strftime("%H:00"),
                        "price":    round(price_mwh, 2),
                    })
                except Exception:
                    continue
            if prices:
                return sorted(prices, key=lambda x: x["ts"])[:24]

    # ------------------------------------------------------------------
    # Format 3: Generic — attributes.today / attributes.prices = [float, ...]
    # 24 floats starting from midnight local time
    # ------------------------------------------------------------------
    for attr_key in ("today", "prices", "hourly_prices"):
        generic = attrs.get(attr_key)
        if generic and isinstance(generic, (list, tuple)) and len(generic) >= 1:
            if all(isinstance(v, (int, float)) for v in generic if v is not None):
                midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
                prices = []
                for i, value in enumerate(generic):
                    if value is None:
                        continue
                    dt = midnight + timedelta(hours=i)
                    ts = dt.timestamp()
                    if ts < now_ts - 3600:
                        continue
                    price_mwh = _to_eur_mwh(float(value), attrs)
                    prices.append({
                        "ts":       ts,
                        "datetime": dt.isoformat(),
                        "hour":     dt.strftime("%H:00"),
                        "price":    round(price_mwh, 2),
                    })
                if prices:
                    return prices[:24]

    raise ValueError(
        "Could not extract hourly prices from sensor. "
        "Supported formats: Nordpool (official/HACS), Zonneplan ONE (forecast attribute), "
        "Tibber, or any sensor with a 'today', 'prices', or 'hourly_prices' attribute "
        "containing a list of floats."
    )


def _to_eur_mwh(value: float, attrs: dict) -> float:
    """
    Normalise a price value to EUR/MWh.

    Heuristic: if the raw value looks like EUR/kWh (< 10 for typical EU prices),
    multiply by 1000.  This correctly handles both Nordpool's EUR/kWh output
    and raw ENTSO-E EUR/MWh values.
    The sensor's unit_of_measurement attribute is used if available for a
    reliable determination.
    """
    uom = (attrs.get("unit_of_measurement") or attrs.get("unit") or "").lower()
    if "kwh" in uom or "kWh" in uom:
        return value * 1000.0
    if "mwh" in uom or "MWh" in uom:
        return value

    # Fallback heuristic: Nordpool typically outputs 0.05–0.40 EUR/kWh
    if abs(value) < 10:
        return value * 1000.0
    return value


# ---------------------------------------------------------------------------
# Optimisation algorithm — Dynamic Programming
# ---------------------------------------------------------------------------

def _optimize_schedule(
    prices: list[dict],
    capacity: float,
    power: float,
    min_soc: int,
    max_soc: int,
    min_profit_eur: float = 0.0,
    initial_soc_pct: float | None = None,
) -> list[dict]:
    """
    Globally optimal charge/discharge schedule via backward dynamic programming.

    State space : (hour t, discrete SoC index s)
    Transition  : CHARGE adds min(power, headroom) kWh
                  DISCHARGE removes min(power, available) kWh
                  IDLE keeps SoC constant
    Objective   : maximise total revenue(discharge) − cost(charge)

    initial_soc_pct : if provided (e.g. from Zendure), the forward pass starts
                      from this SoC rather than the minimum, giving a more
                      accurate schedule for the remaining hours of today.

    After finding the optimal path, _apply_min_profit_filter suppresses any
    charge→discharge cycle whose net profit < min_profit_eur back to IDLE.
    """
    if not prices:
        return []

    T = len(prices)
    soc_min_kwh = capacity * min_soc / 100.0
    soc_max_kwh = capacity * max_soc / 100.0

    n_steps  = max(1, round((soc_max_kwh - soc_min_kwh) / SOC_STEP_KWH))
    soc_grid = [soc_min_kwh + i * (soc_max_kwh - soc_min_kwh) / n_steps for i in range(n_steps + 1)]
    N = len(soc_grid)

    price_eur_kwh = [p["price"] / 1000.0 for p in prices]
    NEG_INF = float("-inf")

    value  = [[NEG_INF] * N for _ in range(T + 1)]
    action = [[ACTION_IDLE] * N for _ in range(T)]

    for s in range(N):
        value[T][s] = 0.0

    for t in range(T - 1, -1, -1):
        p = price_eur_kwh[t]
        for s in range(N):
            soc = soc_grid[s]
            best_val, best_act = NEG_INF, ACTION_IDLE

            # IDLE
            fut = value[t + 1][s]
            if fut > best_val:
                best_val, best_act = fut, ACTION_IDLE

            # CHARGE
            kwh_in = min(power, soc_max_kwh - soc)
            if kwh_in > 1e-6:
                new_soc = soc + kwh_in
                ns = min(N - 1, round((new_soc - soc_min_kwh) / (soc_max_kwh - soc_min_kwh) * n_steps))
                fc = value[t + 1][ns]
                if fc != NEG_INF:
                    vc = -p * kwh_in + fc
                    if vc > best_val:
                        best_val, best_act = vc, ACTION_CHARGE

            # DISCHARGE
            kwh_out = min(power, soc - soc_min_kwh)
            if kwh_out > 1e-6:
                new_soc = soc - kwh_out
                ns = max(0, round((new_soc - soc_min_kwh) / (soc_max_kwh - soc_min_kwh) * n_steps))
                fd = value[t + 1][ns]
                if fd != NEG_INF:
                    vd = p * kwh_out + fd
                    if vd > best_val:
                        best_val, best_act = vd, ACTION_DISCHARGE

            value[t][s] = best_val if best_val != NEG_INF else 0.0
            action[t][s] = best_act

    # Forward pass — start from initial_soc_pct if provided, else min_soc
    if initial_soc_pct is not None:
        init_soc_kwh = capacity * max(min_soc, min(max_soc, float(initial_soc_pct))) / 100.0
        s = min(N - 1, round((init_soc_kwh - soc_min_kwh) / (soc_max_kwh - soc_min_kwh) * n_steps))
    else:
        s = 0
    soc = soc_grid[s]
    raw_actions: list[str] = []

    for t in range(T):
        act = action[t][s]
        raw_actions.append(act)
        if act == ACTION_CHARGE:
            soc += min(power, soc_max_kwh - soc)
        elif act == ACTION_DISCHARGE:
            soc -= min(power, soc - soc_min_kwh)
        s = min(N - 1, round((soc - soc_min_kwh) / (soc_max_kwh - soc_min_kwh) * n_steps))

    if min_profit_eur > 0.0:
        raw_actions = _apply_min_profit_filter(
            raw_actions, price_eur_kwh, power, min_profit_eur, soc_min_kwh, soc_max_kwh
        )

    return [{**prices[t], "action": raw_actions[t]} for t in range(T)]


def _apply_min_profit_filter(
    actions: list[str],
    price_eur_kwh: list[float],
    power: float,
    min_profit_eur: float,
    soc_min_kwh: float,
    soc_max_kwh: float,
) -> list[str]:
    """Suppress charge→discharge cycles whose net profit is below threshold."""
    T = len(actions)

    def get_blocks(target: str) -> list[tuple[int, int]]:
        blocks, i = [], 0
        while i < T:
            if actions[i] == target:
                j = i
                while j < T and actions[j] == target:
                    j += 1
                blocks.append((i, j))
                i = j
            else:
                i += 1
        return blocks

    charge_blocks    = get_blocks(ACTION_CHARGE)
    discharge_blocks = get_blocks(ACTION_DISCHARGE)

    used_discharge: set[int] = set()
    suppress: set[int] = set()

    for cb_start, cb_end in charge_blocks:
        match = None
        for idx, (db_start, db_end) in enumerate(discharge_blocks):
            if db_start >= cb_end and idx not in used_discharge:
                match = (idx, db_start, db_end)
                break

        if match is None:
            for h in range(cb_start, cb_end):
                suppress.add(h)
            continue

        didx, db_start, db_end = match
        usable        = soc_max_kwh - soc_min_kwh
        charge_kwh    = min(power, usable) * (cb_end - cb_start)
        discharge_kwh = min(power, usable) * (db_end - db_start)
        avg_cp = sum(price_eur_kwh[h] for h in range(cb_start, cb_end)) / (cb_end - cb_start)
        avg_dp = sum(price_eur_kwh[h] for h in range(db_start, db_end)) / (db_end - db_start)
        profit = avg_dp * discharge_kwh - avg_cp * charge_kwh

        if profit < min_profit_eur:
            for h in range(cb_start, cb_end):
                suppress.add(h)
            for h in range(db_start, db_end):
                suppress.add(h)
        else:
            used_discharge.add(didx)

    result = list(actions)
    for h in suppress:
        result[h] = ACTION_IDLE
    return result


def _calc_savings(schedule: list[dict], power: float) -> dict:
    cost = revenue = 0.0
    for h in schedule:
        eur_per_kwh = h["price"] / 1000.0
        if h["action"] == ACTION_CHARGE:
            cost    += eur_per_kwh * power
        elif h["action"] == ACTION_DISCHARGE:
            revenue += eur_per_kwh * power
    return {
        "cost":    round(cost, 3),
        "revenue": round(revenue, 3),
        "net":     round(revenue - cost, 3),
    }


# ---------------------------------------------------------------------------
# Zendure helpers — read only
# ---------------------------------------------------------------------------

def _read_number(hass: HomeAssistant, entity_id: str | None, fallback: float) -> float:
    """Read a number entity's state, returning fallback if unavailable."""
    if not entity_id:
        return fallback
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unavailable", "unknown", None):
        return fallback
    try:
        return float(state.state)
    except (ValueError, TypeError):
        return fallback


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class BatteryOptimizerCoordinator(DataUpdateCoordinator):
    """
    Read prices from a HA sensor and compute optimal dispatch schedule.

    Zendure integration (all entities optional — guidance only, no writes)
    ----------------------------------------------------------------------
    READ on every refresh:
      • zen_soc_entity                sensor  → starting SoC for the DP optimiser
      • zen_soc_min_entity            number  → overrides manual min_soc
      • zen_soc_max_entity            number  → overrides manual max_soc
      • zen_inverse_max_power_entity  number  → overrides manual power for both
                                                charge and discharge (W → kW)

    The coordinator never writes to any Zendure entity — it provides guidance
    only.  Use the sensor states in your own automations to act on the schedule.

    Updates are driven by two listeners (no polling):
      1. async_track_state_change_event on the price entity
      2. async_track_time_change at :00 each hour
    """

    def __init__(
        self,
        hass: HomeAssistant,
        price_entity_id: str,
        capacity: float,
        power: float,
        min_soc: int,
        max_soc: int,
        min_profit: float = 0.0,
        # Zendure entities (all optional — pass None to disable)
        zen_soc_entity: str | None = None,
        zen_soc_min_entity: str | None = None,
        zen_soc_max_entity: str | None = None,
        zen_inverse_max_power_entity: str | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,   # no polling — driven by listeners
        )
        self.price_entity_id = price_entity_id
        # Manual fallback values (used when Zendure entities are absent)
        self._capacity  = capacity
        self._power     = power
        self._min_soc   = min_soc
        self._max_soc   = max_soc
        self.min_profit = min_profit
        # Zendure entity IDs
        self.zen_soc_entity               = zen_soc_entity or ""
        self.zen_soc_min_entity           = zen_soc_min_entity or ""
        self.zen_soc_max_entity           = zen_soc_max_entity or ""
        self.zen_inverse_max_power_entity = zen_inverse_max_power_entity or ""
        self._unsub_state: list = []

    # ── Properties — resolve live Zendure values or fall back to manual ───────

    @property
    def capacity(self) -> float:
        return self._capacity

    @property
    def power(self) -> float:
        """Effective max power (kW) for both charge and discharge.

        Reads inverse_max_power from Zendure (stored in W) if configured,
        converts to kW.  Falls back to the manually configured value otherwise.
        """
        return _read_number(self.hass, self.zen_inverse_max_power_entity, self._power * 1000) / 1000

    @property
    def min_soc(self) -> int:
        return int(_read_number(self.hass, self.zen_soc_min_entity, self._min_soc))

    @property
    def max_soc(self) -> int:
        return int(_read_number(self.hass, self.zen_soc_max_entity, self._max_soc))

    @property
    def current_soc(self) -> float | None:
        """Live SoC from Zendure sensor, or None if not configured."""
        if not self.zen_soc_entity:
            return None
        return _read_number(self.hass, self.zen_soc_entity, None)

    # ── Listener setup / teardown ─────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Register state-change and hourly-tick listeners."""

        @callback
        def _on_price_sensor_update(event: Event) -> None:
            new_state = event.data.get("new_state")
            old_state = event.data.get("old_state")
            if new_state is None:
                return
            if (
                old_state is not None
                and new_state.attributes == old_state.attributes
                and new_state.state == old_state.state
            ):
                return
            _LOGGER.debug("Price sensor %s changed — refreshing", self.price_entity_id)
            self.hass.async_create_task(self.async_refresh())

        @callback
        def _on_new_hour(_now) -> None:
            _LOGGER.debug("New hour tick — advancing schedule pointer")
            self.hass.async_create_task(self.async_refresh())

        self._unsub_state.append(
            async_track_state_change_event(
                self.hass, [self.price_entity_id], _on_price_sensor_update,
            )
        )
        self._unsub_state.append(
            async_track_time_change(self.hass, _on_new_hour, minute=0, second=0)
        )
        _LOGGER.debug(
            "BatteryOptimizerCoordinator: listening to %s + hourly tick (Zendure SOC: %s)",
            self.price_entity_id,
            self.zen_soc_entity or "not configured",
        )

    @callback
    def async_teardown(self) -> None:
        for unsub in self._unsub_state:
            unsub()
        self._unsub_state.clear()

    # ── Main update ───────────────────────────────────────────────────────────

    async def _async_update_data(self) -> dict:
        """Read prices + Zendure state and recompute schedule."""
        now    = datetime.now(timezone.utc)
        now_ts = now.timestamp()

        # ── 1. Fetch prices ───────────────────────────────────────────────────
        state_obj = self.hass.states.get(self.price_entity_id)
        if state_obj is None:
            raise UpdateFailed(
                f"Price sensor '{self.price_entity_id}' not found. "
                "Check the entity ID in the integration settings."
            )
        try:
            prices = _extract_prices(state_obj)
        except ValueError as err:
            raise UpdateFailed(str(err)) from err

        # ── 2. Read live Zendure values ───────────────────────────────────────
        live_min_soc  = self.min_soc    # from Zendure number entity or manual fallback
        live_max_soc  = self.max_soc
        live_power_kw = self.power      # inverse_max_power (W→kW) or manual
        live_soc      = self.current_soc

        _LOGGER.debug(
            "Zendure — SoC: %s%% | power: %.1fkW | SoC range: %d%%–%d%%",
            live_soc, live_power_kw, live_min_soc, live_max_soc,
        )

        # ── 3. Optimise ───────────────────────────────────────────────────────
        schedule = _optimize_schedule(
            prices,
            self._capacity,
            live_power_kw,
            live_min_soc,
            live_max_soc,
            self.min_profit,
            initial_soc_pct=live_soc,
        )
        savings = _calc_savings(schedule, live_power_kw)

        # ── 4. Derive current-hour values ─────────────────────────────────────
        current = next(
            (h for h in schedule if h["ts"] <= now_ts < h["ts"] + 3600),
            schedule[0],
        )
        next_different = next(
            (h for h in schedule if h["ts"] > current["ts"] and h["action"] != current["action"]),
            None,
        )
        next_charge = next(
            (h for h in schedule if h["ts"] >= now_ts and h["action"] == ACTION_CHARGE),
            None,
        )
        next_discharge = next(
            (h for h in schedule if h["ts"] >= now_ts and h["action"] == ACTION_DISCHARGE),
            None,
        )

        return {
            "schedule":          schedule,
            "savings":           savings,
            "current_action":    current["action"],
            "current_price":     current["price"],
            "current_hour":      current["hour"],
            "next_action":       next_different["action"] if next_different else current["action"],
            "next_action_at":    next_different["hour"]   if next_different else None,
            "next_charge_at":    next_charge["hour"]      if next_charge    else None,
            "next_discharge_at": next_discharge["hour"]   if next_discharge else None,
            "price_entity":      self.price_entity_id,
            "price_unit":        "EUR/MWh (normalised)",
            "zendure_soc":       live_soc,
            "zendure_min_soc":   live_min_soc,
            "zendure_max_soc":   live_max_soc,
            "zendure_power_kw":  round(live_power_kw, 2),
            "last_updated":      now.isoformat(),
        }
