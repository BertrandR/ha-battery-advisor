"""Sensor platform for Battery Dispatch Advisor."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    ACTION_CHARGE_GRID, ACTION_CHARGE_SOLAR,
    ACTION_DISCHARGE_NET, ACTION_DISCHARGE_USAGE,
    ACTION_IDLE,
    CHARGE_ACTIONS, DISCHARGE_ACTIONS,
)
from .coordinator import BatteryOptimizerCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: BatteryOptimizerCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        BatteryActionSensor(coordinator, entry),
        BatteryPriceSensor(coordinator, entry),
        BatteryNextActionSensor(coordinator, entry),
        BatterySavingsSensor(coordinator, entry),
        BatteryScheduleSensor(coordinator, entry),
    ]
    if coordinator.zen_soc_entity:
        entities.append(BatterySocSensor(coordinator, entry))

    async_add_entities(entities, update_before_add=True)


def _device(coordinator: BatteryOptimizerCoordinator, entry: ConfigEntry) -> DeviceInfo:
    model = f"{coordinator.capacity} kWh / {coordinator.power:.1f} kW"
    if coordinator.zen_soc_entity:
        model += " · Zendure connected"
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Battery Advisor",
        manufacturer="Battery Dispatch Advisor",
        model=model,
        entry_type="service",
    )


_ACTION_ICONS = {
    ACTION_CHARGE_GRID:     "mdi:transmission-tower-import",
    ACTION_CHARGE_SOLAR:    "mdi:solar-power",
    ACTION_DISCHARGE_NET:   "mdi:transmission-tower-export",
    ACTION_DISCHARGE_USAGE: "mdi:home-battery",
    ACTION_IDLE:            "mdi:battery-outline",
}


class _Base(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, unique_suffix, name):
        super().__init__(coordinator)
        self._attr_unique_id   = f"{entry.entry_id}_{unique_suffix}"
        self._attr_name        = name
        self._attr_device_info = _device(coordinator, entry)


# ── 1. Current Action ────────────────────────────────────────────────────────

class BatteryActionSensor(_Base):
    """
    Primary automation sensor.
    State: charge_grid | charge_solar | discharge_net | discharge_usage | idle
    """

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "current_action", "Current Action")

    @property
    def state(self):
        return self.coordinator.data.get("current_action", ACTION_IDLE)

    @property
    def icon(self):
        return _ACTION_ICONS.get(self.state, "mdi:battery-outline")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        attrs = {
            "current_hour":         d.get("current_hour"),
            "current_return_price": d.get("current_return_price"),
            "next_action":          d.get("next_action"),
            "next_action_at":       d.get("next_action_at"),
            "next_charge_at":       d.get("next_charge_at"),
            "next_discharge_at":    d.get("next_discharge_at"),
            "price_entity":         d.get("price_entity"),
            "last_updated":         d.get("last_updated"),
        }
        if self.coordinator.zen_soc_entity:
            attrs.update({
                "zendure_soc":      d.get("zendure_soc"),
                "zendure_power_kw": d.get("zendure_power_kw"),
            })
        return attrs


# ── 2. Current Price ──────────────────────────────────────────────────────────

class BatteryPriceSensor(_Base):
    """Current hour's buy price in EUR/MWh."""
    _attr_native_unit_of_measurement = "EUR/MWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:currency-eur"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "current_price", "Current Price")

    @property
    def native_value(self):
        return self.coordinator.data.get("current_price")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        schedule = self.coordinator.data.get("schedule", [])
        if not schedule:
            return {}
        prices        = [h["price"]        for h in schedule]
        return_prices = [h["return_price"]  for h in schedule]
        return {
            "min_price":        round(min(prices), 2),
            "max_price":        round(max(prices), 2),
            "avg_price":        round(sum(prices) / len(prices), 2),
            "current_return_price": self.coordinator.data.get("current_return_price"),
            "min_return_price": round(min(return_prices), 2),
            "max_return_price": round(max(return_prices), 2),
            "avg_return_price": round(sum(return_prices) / len(return_prices), 2),
        }


# ── 3. Next Action ────────────────────────────────────────────────────────────

class BatteryNextActionSensor(_Base):
    """Next scheduled action change and when it starts."""
    _attr_icon = "mdi:clock-fast"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "next_action", "Next Action")

    @property
    def state(self):
        return self.coordinator.data.get("next_action", ACTION_IDLE)

    @property
    def icon(self):
        return _ACTION_ICONS.get(self.state, "mdi:clock-fast")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        return {
            "starts_at":         d.get("next_action_at"),
            "next_charge_at":    d.get("next_charge_at"),
            "next_discharge_at": d.get("next_discharge_at"),
        }


# ── 4. Daily Savings ──────────────────────────────────────────────────────────

class BatterySavingsSensor(_Base):
    """Estimated net arbitrage for the forecast window (EUR)."""
    _attr_native_unit_of_measurement = "EUR"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:cash-plus"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "daily_savings", "Daily Savings")

    @property
    def native_value(self):
        s = self.coordinator.data.get("savings")
        return s["net"] if s else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self.coordinator.data.get("savings", {})
        return {"charge_cost": s.get("cost"), "discharge_revenue": s.get("revenue")}


# ── 5. Schedule ───────────────────────────────────────────────────────────────

class BatteryScheduleSensor(_Base):
    """Full dispatch schedule as attributes."""
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "schedule", "Schedule")

    @property
    def native_value(self):
        return len(self.coordinator.data.get("schedule", []))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d        = self.coordinator.data
        schedule = d.get("schedule", [])
        return {
            "schedule":              schedule,
            "charge_grid_hours":     [h["hour"] for h in schedule if h["action"] == ACTION_CHARGE_GRID],
            "charge_solar_hours":    [h["hour"] for h in schedule if h["action"] == ACTION_CHARGE_SOLAR],
            "discharge_net_hours":   [h["hour"] for h in schedule if h["action"] == ACTION_DISCHARGE_NET],
            "discharge_usage_hours": [h["hour"] for h in schedule if h["action"] == ACTION_DISCHARGE_USAGE],
            # Legacy combined lists for backwards-compatible automations
            "charge_hours":    [h["hour"] for h in schedule if h["action"] in CHARGE_ACTIONS],
            "discharge_hours": [h["hour"] for h in schedule if h["action"] in DISCHARGE_ACTIONS],
            "price_entity":    d.get("price_entity"),
            "last_updated":    d.get("last_updated"),
        }


# ── 6. Zendure SoC ───────────────────────────────────────────────────────────

class BatterySocSensor(_Base):
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:battery"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "battery_soc", "Battery SoC")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.get("zendure_soc")

    @property
    def icon(self) -> str:
        soc = self.native_value
        if soc is None:   return "mdi:battery-unknown"
        if soc >= 95:     return "mdi:battery"
        if soc >= 75:     return "mdi:battery-80"
        if soc >= 55:     return "mdi:battery-60"
        if soc >= 35:     return "mdi:battery-40"
        if soc >= 15:     return "mdi:battery-20"
        return "mdi:battery-alert"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        return {
            "min_soc":    d.get("zendure_min_soc"),
            "max_soc":    d.get("zendure_max_soc"),
            "power_kw":   d.get("zendure_power_kw"),
            "soc_entity": self.coordinator.zen_soc_entity,
        }
