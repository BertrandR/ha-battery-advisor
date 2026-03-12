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
    ACTION_CHARGE_GRID, ACTION_CHARGE_SOLAR,
    ACTION_DISCHARGE_NET, ACTION_DISCHARGE_USAGE,
    ACTION_IDLE,
    CHARGE_ACTIONS, DISCHARGE_ACTIONS,
    CONF_RTE, DEFAULT_RTE,
    CONF_DISCHARGE_USAGE_POWER, DEFAULT_DISCHARGE_USAGE_POWER,
    CONF_RETURN_PRICE_FORMULA, DEFAULT_RETURN_PRICE_FORMULA,
)

_LOGGER = logging.getLogger(__name__)

SOC_STEP_KWH = 0.2  # 0.2 kWh gives exact representation of common power values


# ---------------------------------------------------------------------------
# Return price formula evaluation
# ---------------------------------------------------------------------------

def _apply_return_formula(buy_price_mwh: float, formula: str) -> float:
    """
    Evaluate the return price formula.

    Variable: current_price  (EUR/MWh, same unit as buy_price_mwh)
    Example formulas:
      "current_price"                          → identity (Zonneplan default)
      "current_price / 1.21"                   → strip 21% VAT
      "current_price / 1.21 - 40.0"           → strip VAT and flat energy tax (EUR/MWh)
      "(current_price - 52.9) / 1.21"         → strip flat tax first, then VAT
    Returns buy_price_mwh unchanged on any evaluation error.
    """
    try:
        result = eval(formula, {"__builtins__": {}}, {"current_price": buy_price_mwh})  # noqa: S307
        return float(result)
    except Exception:
        _LOGGER.warning("Return price formula %r failed for price %.2f — using buy price", formula, buy_price_mwh)
        return buy_price_mwh


# ---------------------------------------------------------------------------
# Price extraction — supports multiple HA sensor formats
# ---------------------------------------------------------------------------

def _extract_prices(state_obj: Any, return_formula: str = DEFAULT_RETURN_PRICE_FORMULA) -> list[dict]:
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
       electricity_price         : incl. tax, Zonneplan micro-units (÷10,000,000 → EUR/kWh)
       electricity_price_excl_tax: excl. tax, same micro-unit encoding
       When return_formula is the default identity, uses electricity_price_excl_tax directly.
       When return_formula is overridden, applies formula to buy price instead.

    3. Tibber (via HACS):
       attributes: prices = [{startsAt, total, ...}, ...]

    4. Generic list sensor:
       attributes: today / prices / hourly_prices = [float, ...]  (24 floats from midnight)

    Returns list of dicts:  {ts, datetime, hour, price, return_price}
    where both prices are EUR/MWh.  return_price <= price always.
    Raises ValueError if no usable data found.
    """
    if state_obj is None or state_obj.state in ("unavailable", "unknown", None):
        raise ValueError("Price sensor is unavailable or has no state")

    attrs = state_obj.attributes or {}
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()

    use_formula = (return_formula != DEFAULT_RETURN_PRICE_FORMULA)

    # ------------------------------------------------------------------
    # Format 1: Nordpool
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
            try:
                start_str = slot.get("start") if isinstance(slot, dict) else getattr(slot, "start", None)
                value     = slot.get("value") if isinstance(slot, dict) else getattr(slot, "value", None)
            except Exception:
                continue
            if start_str is None or value is None:
                continue
            try:
                if isinstance(start_str, str):
                    dt = datetime.fromisoformat(start_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = start_str
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            ts = dt.timestamp()
            if ts < now_ts - 3600:
                continue
            buy_mwh = _to_eur_mwh(float(value), attrs)
            ret_mwh = _apply_return_formula(buy_mwh, return_formula)
            prices.append({
                "ts":           ts,
                "datetime":     dt.isoformat(),
                "hour":         dt.astimezone().strftime("%H:00"),
                "price":        round(buy_mwh, 2),
                "return_price": round(ret_mwh, 2),
            })
        if prices:
            return sorted(prices, key=lambda x: x["ts"])

    # ------------------------------------------------------------------
    # Format 2: Zonneplan ONE
    # ------------------------------------------------------------------
    zonneplan_forecast = attrs.get("forecast")
    if zonneplan_forecast and isinstance(zonneplan_forecast, (list, tuple)) and len(zonneplan_forecast) > 0:
        first = zonneplan_forecast[0]
        if isinstance(first, dict) and "electricity_price" in first and "datetime" in first:
            prices = []
            for slot in zonneplan_forecast:
                try:
                    dt_str    = slot.get("datetime")
                    raw_buy   = slot.get("electricity_price")
                    raw_ret   = slot.get("electricity_price_excl_tax")
                    if dt_str is None or raw_buy is None:
                        continue
                    dt_str_clean = dt_str.rstrip("Z").split(".")[0]
                    dt = datetime.fromisoformat(dt_str_clean).replace(tzinfo=timezone.utc)
                    ts = dt.timestamp()
                    if ts < now_ts - 3600:
                        continue
                    buy_mwh = float(raw_buy) / 10_000_000 * 1000
                    # If user overrode formula, apply it; otherwise use native excl_tax field
                    if use_formula or raw_ret is None:
                        ret_mwh = _apply_return_formula(buy_mwh, return_formula)
                    else:
                        ret_mwh = float(raw_ret) / 10_000_000 * 1000
                    prices.append({
                        "ts":           ts,
                        "datetime":     dt.isoformat(),
                        "hour":         dt.astimezone().strftime("%H:00"),
                        "price":        round(buy_mwh, 2),
                        "return_price": round(ret_mwh, 2),
                    })
                except Exception:
                    continue
            if prices:
                return sorted(prices, key=lambda x: x["ts"])

    # ------------------------------------------------------------------
    # Format 3: Tibber
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
                    value   = float(slot.get("total") or slot.get("energy") or 0)
                    buy_mwh = _to_eur_mwh(value, attrs)
                    ret_mwh = _apply_return_formula(buy_mwh, return_formula)
                    prices.append({
                        "ts":           ts,
                        "datetime":     dt.isoformat(),
                        "hour":         dt.astimezone().strftime("%H:00"),
                        "price":        round(buy_mwh, 2),
                        "return_price": round(ret_mwh, 2),
                    })
                except Exception:
                    continue
            if prices:
                return sorted(prices, key=lambda x: x["ts"])

    # ------------------------------------------------------------------
    # Format 4: Generic list
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
                    dt  = midnight + timedelta(hours=i)
                    ts  = dt.timestamp()
                    if ts < now_ts - 3600:
                        continue
                    buy_mwh = _to_eur_mwh(float(value), attrs)
                    ret_mwh = _apply_return_formula(buy_mwh, return_formula)
                    prices.append({
                        "ts":           ts,
                        "datetime":     dt.isoformat(),
                        "hour":         dt.strftime("%H:00"),
                        "price":        round(buy_mwh, 2),
                        "return_price": round(ret_mwh, 2),
                    })
                if prices:
                    return prices

    raise ValueError(
        "Could not extract hourly prices from sensor. "
        "Supported formats: Nordpool (official/HACS), Zonneplan ONE (forecast attribute), "
        "Tibber, or any sensor with a 'today', 'prices', or 'hourly_prices' attribute "
        "containing a list of floats."
    )


def _to_eur_mwh(value: float, attrs: dict) -> float:
    """Normalise a price value to EUR/MWh."""
    uom = (attrs.get("unit_of_measurement") or attrs.get("unit") or "").lower()
    if "kwh" in uom:
        return value * 1000.0
    if "mwh" in uom:
        return value
    if abs(value) < 10:
        return value * 1000.0
    return value


# ---------------------------------------------------------------------------
# Daylight window — uses HA sun entity
# ---------------------------------------------------------------------------

def _is_daylight(hass: HomeAssistant, slot_ts: float) -> bool:
    """
    Return True if the given Unix timestamp falls within daylight hours
    according to the sun.sun entity (next_rising / next_setting attributes).

    Falls back to a fixed 07:00–20:00 window if sun entity is unavailable.
    """
    sun = hass.states.get("sun.sun")
    if sun is not None:
        try:
            rising_str  = sun.attributes.get("next_rising")
            setting_str = sun.attributes.get("next_setting")
            if rising_str and setting_str:
                # HA provides these as ISO strings; they are always in the future.
                # We build a day's worth of windows by looking at today's date.
                slot_dt = datetime.fromtimestamp(slot_ts, tz=timezone.utc)
                rising  = datetime.fromisoformat(rising_str)
                setting = datetime.fromisoformat(setting_str)
                if rising.tzinfo is None:
                    rising  = rising.replace(tzinfo=timezone.utc)
                if setting.tzinfo is None:
                    setting = setting.replace(tzinfo=timezone.utc)

                # next_rising/setting are always future — shift back by 0 or 1 day
                # to cover today's window and tomorrow's window
                for day_offset in range(-2, 3):
                    r = rising  + timedelta(days=day_offset)
                    s = setting + timedelta(days=day_offset)
                    if r < s and r.timestamp() <= slot_ts < s.timestamp():
                        return True
                return False
        except Exception:
            pass

    # Fallback: fixed 07:00–20:00 local time
    local_hour = datetime.fromtimestamp(slot_ts).hour
    return 7 <= local_hour < 20


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
    rte: int = 100,
    discharge_usage_power: float = DEFAULT_DISCHARGE_USAGE_POWER,
    daylight_mask: list[bool] | None = None,
) -> list[dict]:
    """
    Globally optimal charge/discharge schedule via backward dynamic programming.

    Four actions per slot
    ---------------------
    charge_grid     : pay buy_price  × grid_kwh_in          (always available)
    charge_solar    : pay return_price × grid_kwh_in         (daylight hours only)
    discharge_net   : earn return_price × grid_kwh_out       (always available)
    discharge_usage : earn buy_price  × grid_kwh_out_usage   (always; reduced power)

    RTE model (symmetric half-loss)
    --------------------------------
    half_loss     = (1 − RTE/100) / 2
    charge_eff    = 1 / (1 + half_loss)   — fraction of grid draw stored
    discharge_eff = 1 − half_loss          — fraction of battery release delivered

    charge_solar is cheaper than charge_grid (return_price < buy_price), so the DP
    naturally prefers it during daylight.  discharge_usage earns buy_price > return_price
    but at reduced power — the DP weighs the opportunity cost of slower discharge
    against potentially better prices later.
    """
    if not prices:
        return []

    T = len(prices)
    soc_min_kwh = capacity * min_soc / 100.0
    soc_max_kwh = capacity * max_soc / 100.0
    usable_kwh  = soc_max_kwh - soc_min_kwh

    if usable_kwh < 0.01:
        _LOGGER.warning("Usable capacity near zero — all IDLE")
        return [{**p, "action": ACTION_IDLE, "kwh": 0.0, "partial": False} for p in prices]

    if daylight_mask is None:
        daylight_mask = [True] * T  # no sun entity available — allow solar in all slots

    # ── RTE ──────────────────────────────────────────────────────────────────
    half_loss     = (1.0 - max(0.01, min(1.0, rte / 100.0))) / 2.0
    charge_eff    = 1.0 / (1.0 + half_loss)
    discharge_eff = 1.0 - half_loss

    # ── SoC grid ─────────────────────────────────────────────────────────────
    step = SOC_STEP_KWH
    # Battery-side kWh per charge/discharge action
    charge_soc_steps         = max(1, round(power * charge_eff / step))
    discharge_soc_steps      = max(1, round(power / discharge_eff / step))
    discharge_usage_soc_steps = max(1, round(discharge_usage_power / discharge_eff / step))

    N    = max(2, round(usable_kwh / step)) + 1
    step = usable_kwh / (N - 1)

    buy_eur_kwh    = [p["price"]        / 1000.0 for p in prices]
    return_eur_kwh = [p["return_price"] / 1000.0 for p in prices]

    # ── Backward induction ───────────────────────────────────────────────────
    val_next = [0.0] * N
    policy   = [[ACTION_IDLE] * N for _ in range(T)]

    for t in range(T - 1, -1, -1):
        buy    = buy_eur_kwh[t]
        ret    = return_eur_kwh[t]
        is_day = daylight_mask[t]
        val_cur = [0.0] * N

        for s in range(N):
            best_val = val_next[s]
            best_act = ACTION_IDLE

            # ── CHARGE_GRID: pay buy_price per kWh drawn from grid ────────────
            steps_up = min(charge_soc_steps, N - 1 - s)
            if steps_up > 0:
                soc_delta   = steps_up * step
                grid_kwh_in = soc_delta / charge_eff
                v = -buy * grid_kwh_in + val_next[s + steps_up]
                if v > best_val:
                    best_val, best_act = v, ACTION_CHARGE_GRID

            # ── CHARGE_SOLAR: same SoC transition, but cheaper (return_price) ─
            if is_day and steps_up > 0:
                soc_delta   = steps_up * step
                grid_kwh_in = soc_delta / charge_eff   # physical draw unchanged
                v = -ret * grid_kwh_in + val_next[s + steps_up]
                if v > best_val:
                    best_val, best_act = v, ACTION_CHARGE_SOLAR

            # ── DISCHARGE_NET: earn return_price per kWh delivered ────────────
            steps_dn = min(discharge_soc_steps, s)
            if steps_dn > 0:
                soc_delta    = steps_dn * step
                grid_kwh_out = soc_delta * discharge_eff
                v = ret * grid_kwh_out + val_next[s - steps_dn]
                if v > best_val:
                    best_val, best_act = v, ACTION_DISCHARGE_NET

            # ── DISCHARGE_USAGE: earn buy_price, reduced power ────────────────
            steps_du = min(discharge_usage_soc_steps, s)
            if steps_du > 0:
                soc_delta    = steps_du * step
                grid_kwh_out = soc_delta * discharge_eff
                v = buy * grid_kwh_out + val_next[s - steps_du]
                if v > best_val:
                    best_val, best_act = v, ACTION_DISCHARGE_USAGE

            val_cur[s]   = best_val
            policy[t][s] = best_act

        val_next = val_cur

    # ── Forward pass ─────────────────────────────────────────────────────────
    if initial_soc_pct is not None:
        init_kwh = capacity * max(float(min_soc), min(float(max_soc), float(initial_soc_pct))) / 100.0
    else:
        init_kwh = soc_min_kwh

    soc_kwh     = init_kwh
    raw_actions: list[str]  = []
    raw_kwh:    list[float] = []

    for t in range(T):
        s   = min(N - 1, max(0, round((soc_kwh - soc_min_kwh) / step)))
        act = policy[t][s]
        raw_actions.append(act)

        if act in CHARGE_ACTIONS:
            steps_up    = min(charge_soc_steps, N - 1 - s)
            soc_delta   = steps_up * step
            grid_kwh_in = soc_delta / charge_eff
            soc_kwh     = min(soc_max_kwh, soc_kwh + soc_delta)
            raw_kwh.append(round(grid_kwh_in, 3))
        elif act == ACTION_DISCHARGE_NET:
            steps_dn     = min(discharge_soc_steps, s)
            soc_delta    = steps_dn * step
            grid_kwh_out = soc_delta * discharge_eff
            soc_kwh      = max(soc_min_kwh, soc_kwh - soc_delta)
            raw_kwh.append(round(grid_kwh_out, 3))
        elif act == ACTION_DISCHARGE_USAGE:
            steps_du     = min(discharge_usage_soc_steps, s)
            soc_delta    = steps_du * step
            grid_kwh_out = soc_delta * discharge_eff
            soc_kwh      = max(soc_min_kwh, soc_kwh - soc_delta)
            raw_kwh.append(round(grid_kwh_out, 3))
        else:
            raw_kwh.append(0.0)

    if min_profit_eur > 0.0:
        raw_actions, raw_kwh = _apply_min_profit_filter(
            raw_actions, raw_kwh, buy_eur_kwh, return_eur_kwh,
            power, min_profit_eur, usable_kwh, charge_eff, discharge_eff,
        )

    # nominal grid-side power for partial detection
    nominal = {
        ACTION_CHARGE_GRID:     power / charge_eff,
        ACTION_CHARGE_SOLAR:    power / charge_eff,
        ACTION_DISCHARGE_NET:   power * discharge_eff,
        ACTION_DISCHARGE_USAGE: discharge_usage_power * discharge_eff,
    }

    return [{
        **prices[t],
        "action":  raw_actions[t],
        "kwh":     raw_kwh[t],
        "partial": (raw_actions[t] != ACTION_IDLE
                    and raw_kwh[t] < nominal.get(raw_actions[t], power) - 0.01),
    } for t in range(T)]


def _apply_min_profit_filter(
    actions: list[str],
    kwh: list[float],
    buy_eur_kwh: list[float],
    return_eur_kwh: list[float],
    power_kwh: float,
    min_profit_eur_per_kwh: float,
    usable_kwh: float,
    charge_eff: float = 1.0,
    discharge_eff: float = 1.0,
) -> tuple[list[str], list[float]]:
    """
    Suppress charge/discharge cycles whose effective spread after RTE losses
    is below min_profit_eur_per_kwh.

    Effective spread = avg_sell_price × discharge_eff − avg_buy_price / charge_eff

    Charge actions (both grid and solar) use their respective tariff for cost:
      charge_grid  → buy_price / charge_eff
      charge_solar → return_price / charge_eff  (opportunity cost)
    Discharge actions use their respective tariff for revenue:
      discharge_net   → return_price × discharge_eff
      discharge_usage → buy_price × discharge_eff

    # TODO: consider separate min_profit thresholds per action type
    #       (e.g. lower threshold for solar charge since degradation cost is the
    #        main concern, not price arbitrage)
    """
    T = len(actions)

    def get_blocks_from(acts, targets):
        blocks, i = [], 0
        while i < T:
            if acts[i] in targets:
                j = i
                while j < T and acts[j] in targets:
                    j += 1
                blocks.append((i, j))
                i = j
            else:
                i += 1
        return blocks

    def avg_effective_charge_cost(start, end):
        """Average cost per kWh stored, accounting for which charge action and tariff."""
        costs = []
        for h in range(start, end):
            tariff = return_eur_kwh[h] if actions[h] == ACTION_CHARGE_SOLAR else buy_eur_kwh[h]
            costs.append(tariff / charge_eff)
        return sum(costs) / len(costs)

    def avg_effective_discharge_revenue(start, end):
        """Average revenue per kWh released, accounting for which discharge action."""
        revs = []
        for h in range(start, end):
            tariff = buy_eur_kwh[h] if actions[h] == ACTION_DISCHARGE_USAGE else return_eur_kwh[h]
            revs.append(tariff * discharge_eff)
        return sum(revs) / len(revs)

    suppress: set[int] = set()

    # ── Pass 1: charge → discharge pairs ─────────────────────────────────────
    charge_blocks    = get_blocks_from(actions, CHARGE_ACTIONS)
    discharge_blocks = get_blocks_from(actions, DISCHARGE_ACTIONS)
    used_discharge: set[int] = set()

    for cb_start, cb_end in charge_blocks:
        match = None
        for idx, (db_start, db_end) in enumerate(discharge_blocks):
            if db_start >= cb_end and idx not in used_discharge:
                match = (idx, db_start, db_end)
                break

        if match is None:
            suppress.update(range(cb_start, cb_end))
            continue

        didx, db_start, db_end = match
        spread = avg_effective_discharge_revenue(db_start, db_end) - avg_effective_charge_cost(cb_start, cb_end)

        _LOGGER.debug(
            "charge→discharge: cost=%.4f sell=%.4f effective_spread=%.4f vs min=%.4f EUR/kWh",
            avg_effective_charge_cost(cb_start, cb_end),
            avg_effective_discharge_revenue(db_start, db_end),
            spread, min_profit_eur_per_kwh,
        )

        if spread < min_profit_eur_per_kwh:
            suppress.update(range(cb_start, cb_end))
            suppress.update(range(db_start, db_end))
        else:
            used_discharge.add(didx)

    # ── Pass 2: discharge → charge micro-cycles ───────────────────────────────
    working = list(actions)
    for h in suppress:
        working[h] = ACTION_IDLE

    discharge_blocks2 = get_blocks_from(working, DISCHARGE_ACTIONS)
    charge_blocks2    = get_blocks_from(working, CHARGE_ACTIONS)
    used_charge: set[int] = set()

    for db_start, db_end in discharge_blocks2:
        match = None
        for idx, (cb_start, cb_end) in enumerate(charge_blocks2):
            if cb_start >= db_end and idx not in used_charge:
                match = (idx, cb_start, cb_end)
                break

        if match is None:
            continue

        cidx, cb_start, cb_end = match
        spread = avg_effective_discharge_revenue(db_start, db_end) - avg_effective_charge_cost(cb_start, cb_end)

        _LOGGER.debug(
            "discharge→charge: sell=%.4f cost=%.4f effective_spread=%.4f vs min=%.4f EUR/kWh",
            avg_effective_discharge_revenue(db_start, db_end),
            avg_effective_charge_cost(cb_start, cb_end),
            spread, min_profit_eur_per_kwh,
        )

        if spread < min_profit_eur_per_kwh:
            suppress.update(range(db_start, db_end))
            suppress.update(range(cb_start, cb_end))
        else:
            used_charge.add(cidx)

    result_actions = list(actions)
    result_kwh     = list(kwh)
    for h in suppress:
        result_actions[h] = ACTION_IDLE
        result_kwh[h]     = 0.0
    return result_actions, result_kwh


def _calc_savings(schedule: list[dict]) -> dict:
    cost = revenue = 0.0
    for h in schedule:
        buy_kwh    = h["price"]        / 1000.0
        return_kwh = h["return_price"] / 1000.0
        act        = h["action"]
        kwh        = h["kwh"]
        if act == ACTION_CHARGE_GRID:
            cost    += buy_kwh * kwh
        elif act == ACTION_CHARGE_SOLAR:
            cost    += return_kwh * kwh   # opportunity cost at return tariff
        elif act == ACTION_DISCHARGE_NET:
            revenue += return_kwh * kwh
        elif act == ACTION_DISCHARGE_USAGE:
            revenue += buy_kwh * kwh      # saving on import at buy tariff
    return {
        "cost":    round(cost, 3),
        "revenue": round(revenue, 3),
        "net":     round(revenue - cost, 3),
    }


# ---------------------------------------------------------------------------
# Zendure helpers — read only
# ---------------------------------------------------------------------------

def _read_number(hass: HomeAssistant, entity_id: str | None, fallback: float) -> float:
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
      • zen_inverse_max_power_entity  sensor  → overrides manual power (W → kW)

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
        rte: int = 100,
        discharge_usage_power: float = DEFAULT_DISCHARGE_USAGE_POWER,
        return_price_formula: str = DEFAULT_RETURN_PRICE_FORMULA,
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
            update_interval=None,
        )
        self.price_entity_id      = price_entity_id
        self._capacity            = capacity
        self._power               = power
        self._min_soc             = min_soc
        self._max_soc             = max_soc
        self.min_profit           = min_profit
        self._rte                 = rte
        self._discharge_usage_power = discharge_usage_power
        self._return_price_formula  = return_price_formula
        self.zen_soc_entity               = zen_soc_entity or ""
        self.zen_soc_min_entity           = zen_soc_min_entity or ""
        self.zen_soc_max_entity           = zen_soc_max_entity or ""
        self.zen_inverse_max_power_entity = zen_inverse_max_power_entity or ""
        self._unsub_state: list = []

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def capacity(self) -> float:
        return self._capacity

    @property
    def power(self) -> float:
        return _read_number(self.hass, self.zen_inverse_max_power_entity, self._power * 1000) / 1000

    @property
    def min_soc(self) -> int:
        return int(_read_number(self.hass, self.zen_soc_min_entity, self._min_soc))

    @property
    def max_soc(self) -> int:
        return int(_read_number(self.hass, self.zen_soc_max_entity, self._max_soc))

    @property
    def current_soc(self) -> float | None:
        if not self.zen_soc_entity:
            return None
        return _read_number(self.hass, self.zen_soc_entity, None)

    # ── Listeners ─────────────────────────────────────────────────────────────

    async def async_setup(self) -> None:
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

    @callback
    def async_teardown(self) -> None:
        for unsub in self._unsub_state:
            unsub()
        self._unsub_state.clear()

    # ── Main update ───────────────────────────────────────────────────────────

    async def _async_update_data(self) -> dict:
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
            prices = _extract_prices(state_obj, self._return_price_formula)
        except ValueError as err:
            raise UpdateFailed(str(err)) from err

        # ── 2. Build daylight mask ────────────────────────────────────────────
        daylight_mask = [_is_daylight(self.hass, p["ts"]) for p in prices]

        # ── 3. Read live Zendure values ───────────────────────────────────────
        live_min_soc  = self.min_soc
        live_max_soc  = self.max_soc
        live_power_kw = self.power
        live_soc      = self.current_soc

        _LOGGER.debug(
            "Zendure — SoC: %s%% | power: %.1fkW | SoC range: %d%%–%d%%",
            live_soc, live_power_kw, live_min_soc, live_max_soc,
        )

        # ── 4. Optimise ───────────────────────────────────────────────────────
        schedule = _optimize_schedule(
            prices,
            self._capacity,
            live_power_kw,
            live_min_soc,
            live_max_soc,
            self.min_profit,
            initial_soc_pct=live_soc,
            rte=self._rte,
            discharge_usage_power=self._discharge_usage_power,
            daylight_mask=daylight_mask,
        )
        savings = _calc_savings(schedule)

        # ── 5. Derive current-hour values ─────────────────────────────────────
        current = next(
            (h for h in schedule if h["ts"] <= now_ts < h["ts"] + 3600),
            schedule[0],
        )
        next_different = next(
            (h for h in schedule if h["ts"] > current["ts"] and h["action"] != current["action"]),
            None,
        )
        next_charge = next(
            (h for h in schedule if h["ts"] >= now_ts and h["action"] in CHARGE_ACTIONS),
            None,
        )
        next_discharge = next(
            (h for h in schedule if h["ts"] >= now_ts and h["action"] in DISCHARGE_ACTIONS),
            None,
        )

        return {
            "schedule":          schedule,
            "savings":           savings,
            "current_action":    current["action"],
            "current_price":     current["price"],
            "current_return_price": current["return_price"],
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
