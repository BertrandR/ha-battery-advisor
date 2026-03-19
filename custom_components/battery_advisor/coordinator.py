"""Data coordinator for Battery Advisor."""
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
    ACTION_DISCHARGE_GRID, ACTION_DISCHARGE_USAGE,
    ACTION_IDLE,
    CHARGE_ACTIONS, DISCHARGE_ACTIONS,
    DEFAULT_DISCHARGE_USAGE_POWER,
    DEFAULT_RETURN_PRICE_FORMULA,
)

_LOGGER = logging.getLogger(__name__)

SOC_STEP_KWH = 0.05  # 0.05 kWh fine grid — derived from energy values, not fixed power steps


# ---------------------------------------------------------------------------
# Return price formula
# ---------------------------------------------------------------------------

def _apply_return_formula(buy_price_mwh: float, formula: str) -> float:
    """Apply return price formula. Empty string = identity (return equals buy)."""
    if not formula:
        return buy_price_mwh
    try:
        result = eval(formula, {"__builtins__": {}}, {"current_price": buy_price_mwh})  # noqa: S307
        return float(result)
    except Exception:
        _LOGGER.warning("Return price formula %r failed for %.2f — using buy price", formula, buy_price_mwh)
        return buy_price_mwh


# ---------------------------------------------------------------------------
# Price extraction
# ---------------------------------------------------------------------------

def _extract_prices(state_obj: Any, return_formula: str = DEFAULT_RETURN_PRICE_FORMULA) -> list[dict]:
    """
    Extract hourly price dicts from a HA state object.

    Returns list of {ts, datetime, hour, price, return_price} — both in EUR/MWh.

    Supported formats:
      1. Nordpool (raw_today / raw_tomorrow)
      2. Zonneplan ONE (forecast attribute with electricity_price / electricity_price_excl_tax)
      3. Tibber (prices attribute with startsAt / total)
      4. Generic float list (today / prices / hourly_prices attribute)
    """
    if state_obj is None or state_obj.state in ("unavailable", "unknown", None):
        raise ValueError("Price sensor unavailable")

    attrs    = state_obj.attributes or {}
    now      = datetime.now(timezone.utc)
    now_ts   = now.timestamp()
    use_formula = bool(return_formula)  # non-empty = explicit override, always apply

    def _slot(dt, buy_mwh, ret_mwh=None):
        if ret_mwh is None:
            ret_mwh = _apply_return_formula(buy_mwh, return_formula)
        return {
            "ts":           dt.timestamp(),
            "datetime":     dt.isoformat(),
            "hour":         dt.astimezone().strftime("%H:00"),
            "price":        round(buy_mwh, 2),
            "return_price": round(ret_mwh, 2),
        }

    # ── Format 1: Nordpool ────────────────────────────────────────────────────
    raw_today = attrs.get("raw_today")
    if raw_today and isinstance(raw_today, (list, tuple)):
        combined = list(raw_today)
        if attrs.get("tomorrow_valid") and attrs.get("raw_tomorrow"):
            combined += list(attrs["raw_tomorrow"])
        prices = []
        for slot in combined:
            try:
                start = slot.get("start") if isinstance(slot, dict) else getattr(slot, "start", None)
                value = slot.get("value") if isinstance(slot, dict) else getattr(slot, "value", None)
                if start is None or value is None:
                    continue
                dt = datetime.fromisoformat(str(start)) if isinstance(start, str) else start
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt.timestamp() < now_ts - 3600:
                    continue
                buy = _to_eur_mwh(float(value), attrs)
                prices.append(_slot(dt, buy))
            except Exception:
                continue
        if prices:
            return sorted(prices, key=lambda x: x["ts"])

    # ── Format 2: Zonneplan ONE ───────────────────────────────────────────────
    forecast = attrs.get("forecast")
    if forecast and isinstance(forecast, (list, tuple)) and len(forecast) > 0:
        first = forecast[0]
        if isinstance(first, dict) and "electricity_price" in first and "datetime" in first:
            prices = []
            for slot in forecast:
                try:
                    dt_str  = slot.get("datetime", "")
                    raw_buy = slot.get("electricity_price")
                    raw_ret = slot.get("electricity_price_excl_tax")
                    if not dt_str or raw_buy is None:
                        continue
                    dt = datetime.fromisoformat(dt_str.rstrip("Z").split(".")[0]).replace(tzinfo=timezone.utc)
                    if dt.timestamp() < now_ts - 3600:
                        continue
                    buy_mwh = float(raw_buy) / 10_000_000 * 1000
                    if use_formula:
                        # Explicit formula set — apply it (e.g. "current_price" = equal tariffs)
                        ret_mwh = _apply_return_formula(buy_mwh, return_formula)
                    elif raw_ret is not None:
                        # Auto-detect: use Zonneplan's native excl-tax field
                        ret_mwh = float(raw_ret) / 10_000_000 * 1000
                    else:
                        ret_mwh = buy_mwh
                    prices.append(_slot(dt, buy_mwh, ret_mwh))
                except Exception:
                    continue
            if prices:
                return sorted(prices, key=lambda x: x["ts"])

    # ── Format 3: Tibber ──────────────────────────────────────────────────────
    tibber = attrs.get("prices")
    if tibber and isinstance(tibber, (list, tuple)) and len(tibber) > 0:
        first = tibber[0]
        if isinstance(first, dict) and "startsAt" in first:
            prices = []
            for slot in tibber:
                try:
                    dt = datetime.fromisoformat(slot["startsAt"])
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt.timestamp() < now_ts - 3600:
                        continue
                    buy = _to_eur_mwh(float(slot.get("total") or slot.get("energy") or 0), attrs)
                    prices.append(_slot(dt, buy))
                except Exception:
                    continue
            if prices:
                return sorted(prices, key=lambda x: x["ts"])

    # ── Format 4: Generic float list ─────────────────────────────────────────
    for key in ("today", "prices", "hourly_prices"):
        generic = attrs.get(key)
        if generic and isinstance(generic, (list, tuple)):
            if all(isinstance(v, (int, float)) for v in generic if v is not None):
                midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
                prices = []
                for i, value in enumerate(generic):
                    if value is None:
                        continue
                    dt = midnight + timedelta(hours=i)
                    if dt.timestamp() < now_ts - 3600:
                        continue
                    buy = _to_eur_mwh(float(value), attrs)
                    prices.append(_slot(dt, buy))
                if prices:
                    return prices

    raise ValueError(
        "Could not extract prices. Supported: Nordpool, Zonneplan ONE, Tibber, "
        "or sensor with today/prices/hourly_prices float list attribute."
    )


def _to_eur_mwh(value: float, attrs: dict) -> float:
    uom = (attrs.get("unit_of_measurement") or attrs.get("unit") or "").lower()
    if "kwh" in uom:
        return value * 1000.0
    if "mwh" in uom:
        return value
    return value * 1000.0 if abs(value) < 10 else value


# ---------------------------------------------------------------------------
# Daylight window — HA sun entity
# ---------------------------------------------------------------------------

def _is_daylight(hass: HomeAssistant, slot_ts: float) -> bool:
    """Return True if slot_ts falls between sunrise and sunset on its calendar day."""
    sun = hass.states.get("sun.sun")
    if sun is not None:
        try:
            rising_str  = sun.attributes.get("next_rising")
            setting_str = sun.attributes.get("next_setting")
            if rising_str and setting_str:
                slot_dt = datetime.fromtimestamp(slot_ts, tz=timezone.utc)

                next_rising  = datetime.fromisoformat(rising_str)
                next_setting = datetime.fromisoformat(setting_str)
                if next_rising.tzinfo is None:
                    next_rising  = next_rising.replace(tzinfo=timezone.utc)
                if next_setting.tzinfo is None:
                    next_setting = next_setting.replace(tzinfo=timezone.utc)

                # next_rising and next_setting are the *next* upcoming events from
                # now, so they may be on different days. Derive the time-of-day only
                # and check whether slot_dt's local time falls between them.
                local_slot   = slot_dt.astimezone()
                local_rising = next_rising.astimezone()
                local_setting = next_setting.astimezone()

                # Build sunrise and sunset anchored to the slot's local calendar date
                slot_date = local_slot.date()
                sunrise = local_rising.replace(
                    year=slot_date.year, month=slot_date.month, day=slot_date.day
                )
                sunset = local_setting.replace(
                    year=slot_date.year, month=slot_date.month, day=slot_date.day
                )
                return sunrise <= local_slot < sunset
        except Exception:
            pass

    # Fallback when sun.sun is unavailable
    local_hour = datetime.fromtimestamp(slot_ts).hour
    return 7 <= local_hour < 20


# ---------------------------------------------------------------------------
# Battery model — derived from measured charge/discharge energy
# ---------------------------------------------------------------------------

class BatteryModel:
    """
    Encapsulates all battery parameters derived from two measured quantities:
      charge_energy   — grid-side kWh drawn to go from min→max SoC
      discharge_energy — grid-side kWh delivered going from max→min SoC

    Derived:
      eff             = sqrt(discharge_energy / charge_energy)
                        symmetric charge/discharge efficiency
      usable_kwh      = charge_energy × eff   (battery-side usable capacity)
      charge_time     = charge_energy / charge_power    (hours to full charge)
      discharge_time  = discharge_energy / discharge_power (hours to full discharge)

    SoC conversion:
      stored_kwh(soc%) = soc / 100 × usable_kwh
      — treats 0% as empty (min SoC) and 100% as full (max SoC)
    """

    def __init__(
        self,
        charge_energy: float,
        discharge_energy: float,
        charge_power: float,
        discharge_power: float,
    ):
        self.charge_energy    = charge_energy
        self.discharge_energy = discharge_energy
        self.charge_power     = charge_power
        self.discharge_power  = discharge_power

        self.eff              = math.sqrt(discharge_energy / charge_energy)
        self.usable_kwh       = charge_energy * self.eff
        self.charge_time      = charge_energy  / charge_power    # hours
        self.discharge_time   = discharge_energy / discharge_power  # hours

        # Grid-side kWh per hour at rated power
        self.charge_kwh_per_hour    = charge_power     # grid draw per charge hour
        self.discharge_kwh_per_hour = discharge_power  # grid delivery per discharge hour

        # Battery-side kWh stored/released per rated hour
        self.charge_soc_per_hour    = charge_power    * self.eff   # stored per charge hour
        self.discharge_soc_per_hour = discharge_power / self.eff   # released per discharge hour

    def soc_to_kwh(self, soc_pct: float) -> float:
        """Convert SoC% (0-100) to stored battery kWh."""
        return max(0.0, min(self.usable_kwh, soc_pct / 100.0 * self.usable_kwh))

    def kwh_to_soc(self, stored_kwh: float) -> float:
        """Convert stored battery kWh to SoC%."""
        return stored_kwh / self.usable_kwh * 100.0 if self.usable_kwh > 0 else 0.0

    def __repr__(self):
        return (
            f"BatteryModel(charge={self.charge_energy}kWh/{self.charge_power}kW "
            f"discharge={self.discharge_energy}kWh/{self.discharge_power}kW "
            f"eff={self.eff:.4f} usable={self.usable_kwh:.3f}kWh)"
        )


# ---------------------------------------------------------------------------
# Optimisation — Dynamic Programming
# ---------------------------------------------------------------------------

def _optimize_schedule(
    prices: list[dict],
    battery: BatteryModel,
    min_profit_eur: float = 0.0,
    initial_soc_pct: float | None = None,
    discharge_usage_power: float = DEFAULT_DISCHARGE_USAGE_POWER,
    daylight_mask: list[bool] | None = None,
) -> list[dict]:
    """
    Globally optimal charge/discharge schedule via backward dynamic programming.

    State space : (hour t, SoC grid index s)
    SoC grid    : N points spanning [0, usable_kwh] in SOC_STEP_KWH increments

    Four actions per slot:
      charge_grid     — pay buy_price  × charge_kwh_per_hour   (always)
      charge_solar    — pay return_price × charge_kwh_per_hour  (daylight only)
      discharge_grid  — earn return_price × discharge_kwh_per_hour (always)
      discharge_usage — earn buy_price × usage_kwh_per_hour    (always, reduced power)

    All per-hour grid-side and battery-side quantities are derived from
    BatteryModel, which is built from the two measured energy values.
    """
    if not prices:
        return []

    T = len(prices)
    if battery.usable_kwh < 0.01:
        _LOGGER.warning("Usable capacity near zero — all IDLE")
        return [{**p, "action": ACTION_IDLE, "kwh": 0.0, "partial": False} for p in prices]

    if daylight_mask is None:
        daylight_mask = [True] * T

    eff  = battery.eff
    step = SOC_STEP_KWH
    N    = max(2, round(battery.usable_kwh / step)) + 1
    step = battery.usable_kwh / (N - 1)

    # Battery-side SoC steps per action at rated power
    charge_steps        = max(1, round(battery.charge_soc_per_hour    / step))
    discharge_steps     = max(1, round(battery.discharge_soc_per_hour / step))
    # discharge_usage: battery drains at usage_power / eff per hour
    usage_soc_per_hour  = discharge_usage_power / eff
    usage_steps         = max(1, round(usage_soc_per_hour / step))

    # Grid-side kWh per step (for cost/revenue calculation)
    charge_grid_kwh_per_step    = step / eff          # kWh drawn per battery-step charged
    discharge_grid_kwh_per_step = step * eff           # kWh delivered per battery-step discharged

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

            steps_up = min(charge_steps, N - 1 - s)
            if steps_up > 0:
                grid_in = steps_up * charge_grid_kwh_per_step
                # charge_grid
                v = -buy * grid_in + val_next[s + steps_up]
                if v > best_val:
                    best_val, best_act = v, ACTION_CHARGE_GRID
                # charge_solar (daylight only — cheaper opportunity cost)
                if is_day:
                    v = -ret * grid_in + val_next[s + steps_up]
                    if v > best_val:
                        best_val, best_act = v, ACTION_CHARGE_SOLAR

            steps_dn = min(discharge_steps, s)
            if steps_dn > 0:
                grid_out = steps_dn * discharge_grid_kwh_per_step
                v = ret * grid_out + val_next[s - steps_dn]
                if v > best_val:
                    best_val, best_act = v, ACTION_DISCHARGE_GRID

            steps_du = min(usage_steps, s)
            if steps_du > 0:
                grid_out = steps_du * discharge_grid_kwh_per_step
                v = buy * grid_out + val_next[s - steps_du]
                if v > best_val:
                    best_val, best_act = v, ACTION_DISCHARGE_USAGE

            val_cur[s]   = best_val
            policy[t][s] = best_act

        val_next = val_cur

    # ── Forward pass ─────────────────────────────────────────────────────────
    init_kwh = battery.soc_to_kwh(initial_soc_pct) if initial_soc_pct is not None else battery.usable_kwh * 0.5
    if initial_soc_pct is None:
        _LOGGER.warning(
            "No live SoC available — assuming 50%% (%.2f kWh). "
            "Configure a Zendure SoC entity for accurate scheduling.",
            battery.usable_kwh * 0.5,
        )
    soc_kwh  = init_kwh

    raw_actions: list[str]  = []
    raw_kwh:    list[float] = []

    for t in range(T):
        s   = min(N - 1, max(0, round(soc_kwh / step)))
        act = policy[t][s]
        raw_actions.append(act)

        if act in CHARGE_ACTIONS:
            su       = min(charge_steps, N - 1 - s)
            grid_in  = su * charge_grid_kwh_per_step
            soc_kwh  = min(battery.usable_kwh, soc_kwh + su * step)
            raw_kwh.append(round(grid_in, 3))
        elif act == ACTION_DISCHARGE_GRID:
            sd       = min(discharge_steps, s)
            grid_out = sd * discharge_grid_kwh_per_step
            soc_kwh  = max(0.0, soc_kwh - sd * step)
            raw_kwh.append(round(grid_out, 3))
        elif act == ACTION_DISCHARGE_USAGE:
            su2      = min(usage_steps, s)
            grid_out = su2 * discharge_grid_kwh_per_step
            soc_kwh  = max(0.0, soc_kwh - su2 * step)
            raw_kwh.append(round(grid_out, 3))
        else:
            raw_kwh.append(0.0)

    if min_profit_eur > 0.0:
        raw_actions, raw_kwh = _apply_min_profit_filter(
            raw_actions, raw_kwh, buy_eur_kwh, return_eur_kwh,
            min_profit_eur, eff,
        )

    nom_charge    = battery.charge_kwh_per_hour
    nom_discharge = battery.discharge_kwh_per_hour
    nom_usage     = discharge_usage_power

    return [{
        **prices[t],
        "action":  raw_actions[t],
        "kwh":     raw_kwh[t],
        "partial": (
            raw_actions[t] != ACTION_IDLE
            and raw_kwh[t] < (
                nom_charge if raw_actions[t] in CHARGE_ACTIONS
                else nom_usage if raw_actions[t] == ACTION_DISCHARGE_USAGE
                else nom_discharge
            ) - 0.01
        ),
    } for t in range(T)]


# ---------------------------------------------------------------------------
# Profit filter
# ---------------------------------------------------------------------------

def _apply_min_profit_filter(
    actions: list[str],
    kwh: list[float],
    buy_eur_kwh: list[float],
    return_eur_kwh: list[float],
    min_profit_eur_per_kwh: float,
    eff: float,
) -> tuple[list[str], list[float]]:
    """
    Suppress cycles whose effective spread after efficiency losses is below threshold.

    charge_grid  cost  = buy_price   / eff  (per kWh stored)
    charge_solar cost  = return_price / eff  (opportunity cost per kWh stored)
    discharge_grid rev = return_price × eff  (per kWh released)
    discharge_usage rev = buy_price  × eff  (per kWh released)

    Pairs each charge block with the best-spread following discharge block
    (not just the nearest) to avoid micro-cycles masking the real arbitrage window.

    # TODO: separate min_profit thresholds per action type
    """
    T = len(actions)

    def get_blocks(acts, targets):
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

    def charge_cost(start, end):
        costs = []
        for h in range(start, end):
            tariff = return_eur_kwh[h] if actions[h] == ACTION_CHARGE_SOLAR else buy_eur_kwh[h]
            costs.append(tariff / eff)
        return sum(costs) / len(costs)

    def discharge_rev(start, end):
        revs = []
        for h in range(start, end):
            tariff = buy_eur_kwh[h] if actions[h] == ACTION_DISCHARGE_USAGE else return_eur_kwh[h]
            revs.append(tariff * eff)
        return sum(revs) / len(revs)

    suppress: set[int] = set()

    # ── Pass 1: charge → best discharge ──────────────────────────────────────
    charge_blocks    = get_blocks(actions, CHARGE_ACTIONS)
    discharge_blocks = get_blocks(actions, DISCHARGE_ACTIONS)
    used_discharge: set[int] = set()

    # ── Pass 1: pair each charge block with its nearest following discharge ──────
    # Using nearest-first (not best-spread) so that a high-value future discharge
    # block is not greedily consumed by an earlier charge block, leaving a later
    # charge block with no candidate and getting incorrectly suppressed.
    for cb_s, cb_e in charge_blocks:
        candidates = [
            (i, db_s, db_e)
            for i, (db_s, db_e) in enumerate(discharge_blocks)
            if db_s >= cb_e and i not in used_discharge
        ]
        if not candidates:
            suppress.update(range(cb_s, cb_e))
            continue

        nearest = min(candidates, key=lambda c: c[1])
        didx, db_s, db_e = nearest
        spread = discharge_rev(db_s, db_e) - charge_cost(cb_s, cb_e)

        _LOGGER.debug("charge→discharge spread=%.4f vs min=%.4f", spread, min_profit_eur_per_kwh)

        if spread < min_profit_eur_per_kwh:
            suppress.update(range(cb_s, cb_e))
            suppress.update(range(db_s, db_e))
        else:
            used_discharge.add(didx)

    # ── Pass 2: discharge → charge micro-cycles ───────────────────────────────
    working = list(actions)
    for h in suppress:
        working[h] = ACTION_IDLE

    discharge_blocks2 = get_blocks(working, DISCHARGE_ACTIONS)
    charge_blocks2    = get_blocks(working, CHARGE_ACTIONS)
    used_charge: set[int] = set()

    # Build set of discharge block index ranges already kept in Pass 1
    # so Pass 2 doesn't re-evaluate or suppress them
    kept_discharge_ranges: set[tuple] = set()
    for didx in used_discharge:
        kept_discharge_ranges.add(discharge_blocks[didx])

    for db_s, db_e in discharge_blocks2:
        # Skip if this block was already validated in Pass 1
        if (db_s, db_e) in kept_discharge_ranges:
            continue

        candidates = [
            (i, cb_s, cb_e)
            for i, (cb_s, cb_e) in enumerate(charge_blocks2)
            if cb_s >= db_e and i not in used_charge
        ]
        if not candidates:
            continue

        nearest = min(candidates, key=lambda c: c[1])
        cidx, cb_s, cb_e = nearest
        spread = discharge_rev(db_s, db_e) - charge_cost(cb_s, cb_e)

        _LOGGER.debug("discharge→charge spread=%.4f vs min=%.4f", spread, min_profit_eur_per_kwh)

        if spread < min_profit_eur_per_kwh:
            suppress.update(range(db_s, db_e))
            suppress.update(range(cb_s, cb_e))
        else:
            used_charge.add(cidx)

    result_actions = list(actions)
    result_kwh     = list(kwh)
    for h in suppress:
        result_actions[h] = ACTION_IDLE
        result_kwh[h]     = 0.0
    return result_actions, result_kwh


# ---------------------------------------------------------------------------
# Savings
# ---------------------------------------------------------------------------

def _calc_savings(schedule: list[dict]) -> dict:
    cost = revenue = 0.0
    for h in schedule:
        buy = h["price"]        / 1000.0
        ret = h["return_price"] / 1000.0
        act = h["action"]
        kwh = h["kwh"]
        if   act == ACTION_CHARGE_GRID:     cost    += buy * kwh
        elif act == ACTION_CHARGE_SOLAR:    cost    += ret * kwh
        elif act == ACTION_DISCHARGE_GRID:   revenue += ret * kwh
        elif act == ACTION_DISCHARGE_USAGE: revenue += buy * kwh
    return {"cost": round(cost, 3), "revenue": round(revenue, 3), "net": round(revenue - cost, 3)}


# ---------------------------------------------------------------------------
# Zendure helper
# ---------------------------------------------------------------------------

def _read_soc(hass: HomeAssistant, entity_id: str) -> float | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unavailable", "unknown", None):
        return None
    try:
        return float(state.state)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class BatteryOptimizerCoordinator(DataUpdateCoordinator):
    """
    Reads prices and computes optimal dispatch schedule.

    Battery parameters are derived from two measured energy values
    (charge_energy, discharge_energy) rather than capacity + SoC limits + RTE.
    The only optional HA entity read is zen_soc_entity for live SoC%.

    Updates driven by:
      1. Price sensor forecast attribute change
      2. SoC entity change (significant move, ≥ SOC_REPLAN_THRESHOLD %)
      3. Hourly tick at :00
    """

    # Re-plan when SoC moves by at least this many percentage points
    SOC_REPLAN_THRESHOLD = 5.0

    def __init__(
        self,
        hass: HomeAssistant,
        battery_name: str,
        price_entity_id: str,
        charge_energy: float,
        discharge_energy: float,
        charge_power: float,
        discharge_power: float,
        min_profit: float = 0.0,
        discharge_usage_power: float = DEFAULT_DISCHARGE_USAGE_POWER,
        return_price_formula: str = DEFAULT_RETURN_PRICE_FORMULA,
        zen_soc_entity: str | None = None,
    ) -> None:
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}_{battery_name}", update_interval=None)
        self.battery_name             = battery_name
        self.price_entity_id          = price_entity_id
        self.battery                  = BatteryModel(charge_energy, discharge_energy, charge_power, discharge_power)
        self.min_profit               = min_profit
        self._discharge_usage_power   = discharge_usage_power
        self._return_price_formula    = return_price_formula
        self.zen_soc_entity           = zen_soc_entity or ""
        self._unsub_state: list       = []
        self._last_planned_soc: float | None = None

        _LOGGER.debug("[%s] Coordinator initialised: %s", self.battery_name, self.battery)

    # ── Listeners ─────────────────────────────────────────────────────────────

    async def async_setup(self) -> None:
        @callback
        def _on_price_change(event: Event) -> None:
            ns = event.data.get("new_state")
            os = event.data.get("old_state")
            if ns is None:
                return
            # Only re-plan when the forecast attribute actually changes,
            # not on every current-price tick (state value change).
            ns_forecast = (ns.attributes or {}).get("forecast")
            os_forecast = (os.attributes or {}).get("forecast") if os else None
            if ns_forecast is not None and ns_forecast == os_forecast:
                _LOGGER.debug("Price sensor state changed but forecast unchanged — skipping re-plan")
                return
            self.hass.async_create_task(self.async_refresh())

        @callback
        def _on_soc_change(event: Event) -> None:
            ns = event.data.get("new_state")
            os = event.data.get("old_state")
            if ns is None:
                return

            new_unavailable = ns.state in ("unavailable", "unknown", None)
            old_unavailable = (os is None) or (os.state in ("unavailable", "unknown", None))

            # Re-plan when entity recovers from unavailable — the last plan may
            # have been computed with a stale/zero SoC.
            if old_unavailable and not new_unavailable:
                _LOGGER.debug("SoC entity recovered from unavailable — triggering re-plan")
                self.hass.async_create_task(self.async_refresh())
                return

            if new_unavailable:
                return

            try:
                new_soc = float(ns.state)
            except (ValueError, TypeError):
                return

            last = self._last_planned_soc

            # Always re-plan on first reading
            if last is None:
                _LOGGER.debug("First SoC reading (%.1f%%) — triggering re-plan", new_soc)
                self.hass.async_create_task(self.async_refresh())
                return

            # Re-plan if the last plan used a bad SoC (0 or None-fallback 50%)
            # and we now have a real reading that differs meaningfully
            if last <= 1.0 and new_soc >= 5.0:
                _LOGGER.debug(
                    "Last plan used near-zero SoC (%.1f%%), now reading %.1f%% — triggering re-plan",
                    last, new_soc,
                )
                self.hass.async_create_task(self.async_refresh())
                return

            # Re-plan if SoC has moved significantly from the last planned value
            if abs(new_soc - last) >= self.SOC_REPLAN_THRESHOLD:
                _LOGGER.debug(
                    "SoC moved from %.1f%% to %.1f%% (≥%.0f%%) — triggering re-plan",
                    last, new_soc, self.SOC_REPLAN_THRESHOLD,
                )
                self.hass.async_create_task(self.async_refresh())

        @callback
        def _on_new_hour(_now) -> None:
            self.hass.async_create_task(self.async_refresh())

        self._unsub_state.append(
            async_track_state_change_event(self.hass, [self.price_entity_id], _on_price_change)
        )
        self._unsub_state.append(
            async_track_time_change(self.hass, _on_new_hour, minute=0, second=0)
        )
        # Track SoC entity if configured
        if self.zen_soc_entity:
            self._unsub_state.append(
                async_track_state_change_event(self.hass, [self.zen_soc_entity], _on_soc_change)
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

        # 1. Prices
        state_obj = self.hass.states.get(self.price_entity_id)
        if state_obj is None:
            raise UpdateFailed(f"Price sensor '{self.price_entity_id}' not found")
        try:
            prices = _extract_prices(state_obj, self._return_price_formula)
        except ValueError as err:
            raise UpdateFailed(str(err)) from err

        # 2. Daylight mask
        daylight_mask = [_is_daylight(self.hass, p["ts"]) for p in prices]

        # 3. Live SoC
        live_soc = _read_soc(self.hass, self.zen_soc_entity)

        # Guard: if SoC reads suspiciously low (≤1%) but a previous plan used a
        # significantly higher value, treat the reading as transient/stale.
        # Zendure (and similar) occasionally report 0 briefly during comms gaps
        # or HA startup, which causes the DP to plan all-idle for the current day.
        if (
            live_soc is not None
            and live_soc <= 1.0
            and self._last_planned_soc is not None
            and self._last_planned_soc >= 10.0
        ):
            _LOGGER.warning(
                "SoC reads %.1f%% but last planned SoC was %.1f%% — "
                "treating as transient read, using last known SoC",
                live_soc,
                self._last_planned_soc,
            )
            live_soc = self._last_planned_soc

        _LOGGER.debug(
            "[%s] Coordinator update — zen_soc_entity=%r live_soc=%s last_planned_soc=%s slots=%d min_profit=%.3f",
            self.battery_name, self.zen_soc_entity, live_soc, self._last_planned_soc, len(prices), self.min_profit,
        )

        # 4. Optimise
        try:
            schedule = _optimize_schedule(
                prices,
                self.battery,
                self.min_profit,
                initial_soc_pct=live_soc,
                discharge_usage_power=self._discharge_usage_power,
                daylight_mask=daylight_mask,
            )
        except Exception as err:
            _LOGGER.error("Optimisation failed: %s", err, exc_info=True)
            raise UpdateFailed(f"Optimisation failed: {err}") from err

        # Record SoC used for this plan (for stale-read guard and re-plan trigger)
        self._last_planned_soc = live_soc
        savings = _calc_savings(schedule)
        _LOGGER.debug(
            "[%s] Schedule computed — active slots=%d  savings=€%.2f  initial_soc=%s%%",
            self.battery_name,
            sum(1 for h in schedule if h["action"] != ACTION_IDLE),
            savings["net"],
            live_soc,
        )

        # 5. Derive current-hour values
        current = next(
            (h for h in schedule if h["ts"] <= now_ts < h["ts"] + 3600),
            schedule[0],
        )
        next_diff = next(
            (h for h in schedule if h["ts"] > current["ts"] and h["action"] != current["action"]),
            None,
        )
        next_charge = next(
            (h for h in schedule if h["ts"] >= now_ts and h["action"] in CHARGE_ACTIONS), None
        )
        next_discharge = next(
            (h for h in schedule if h["ts"] >= now_ts and h["action"] in DISCHARGE_ACTIONS), None
        )

        return {
            "schedule":             schedule,
            "savings":              savings,
            "current_action":       current["action"],
            "current_price":        current["price"],
            "current_return_price": current["return_price"],
            "current_hour":         current["hour"],
            "next_action":          next_diff["action"] if next_diff else current["action"],
            "next_action_at":       next_diff["hour"]   if next_diff else None,
            "next_charge_at":       next_charge["hour"]    if next_charge    else None,
            "next_discharge_at":    next_discharge["hour"] if next_discharge else None,
            "price_entity":         self.price_entity_id,
            "zendure_soc":          live_soc,
            "planned_soc":          live_soc,   # SoC actually used for this plan (post-guard)
            "last_updated":         now.isoformat(),
            # Expose derived battery info for sensors
            "battery_charge_time":    round(self.battery.charge_time, 2),
            "battery_discharge_time": round(self.battery.discharge_time, 2),
            "battery_usable_kwh":     round(self.battery.usable_kwh, 3),
            "battery_eff":            round(self.battery.eff, 4),
        }
