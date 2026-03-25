"""Battery Advisor — Home Assistant Integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    CONF_BATTERY_NAME,
    CONF_PRICE_ENTITY, CONF_RETURN_PRICE_FORMULA,
    CONF_CHARGE_ENERGY, CONF_DISCHARGE_ENERGY,
    CONF_CHARGE_POWER, CONF_DISCHARGE_POWER,
    CONF_DISCHARGE_USAGE_POWER, CONF_MIN_PROFIT,
    CONF_ZEN_SOC,
    DEFAULT_CHARGE_ENERGY, DEFAULT_DISCHARGE_ENERGY,
    DEFAULT_CHARGE_POWER, DEFAULT_DISCHARGE_POWER,
    DEFAULT_DISCHARGE_USAGE_POWER, DEFAULT_MIN_PROFIT,
    DEFAULT_RETURN_PRICE_FORMULA,
)
from .coordinator import BatteryOptimizerCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    cfg = {**entry.data, **entry.options}

    coordinator = BatteryOptimizerCoordinator(
        hass,
        battery_name          = cfg.get(CONF_BATTERY_NAME, "Battery Advisor"),
        price_entity_id       = cfg[CONF_PRICE_ENTITY],
        charge_energy         = cfg.get(CONF_CHARGE_ENERGY,         DEFAULT_CHARGE_ENERGY),
        discharge_energy      = cfg.get(CONF_DISCHARGE_ENERGY,      DEFAULT_DISCHARGE_ENERGY),
        charge_power          = cfg.get(CONF_CHARGE_POWER,          DEFAULT_CHARGE_POWER),
        discharge_power       = cfg.get(CONF_DISCHARGE_POWER,       DEFAULT_DISCHARGE_POWER),
        min_profit            = cfg.get(CONF_MIN_PROFIT,            DEFAULT_MIN_PROFIT),
        discharge_usage_power = cfg.get(CONF_DISCHARGE_USAGE_POWER, DEFAULT_DISCHARGE_USAGE_POWER),
        return_price_formula  = cfg.get(CONF_RETURN_PRICE_FORMULA,  DEFAULT_RETURN_PRICE_FORMULA),
        zen_soc_entity        = cfg.get(CONF_ZEN_SOC) or None,
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
