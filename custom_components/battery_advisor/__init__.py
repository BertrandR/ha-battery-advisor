"""Battery Dispatch Advisor — Home Assistant Integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    CONF_PRICE_ENTITY,
    CONF_CAPACITY, CONF_POWER, CONF_MIN_SOC, CONF_MAX_SOC, CONF_MIN_PROFIT,
    CONF_ZEN_SOC, CONF_ZEN_SOC_MIN, CONF_ZEN_SOC_MAX, CONF_ZEN_INVERSE_MAX_POWER,
    DEFAULT_CAPACITY, DEFAULT_POWER, DEFAULT_MIN_SOC, DEFAULT_MAX_SOC, DEFAULT_MIN_PROFIT,
)
from .coordinator import BatteryOptimizerCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    cfg = {**entry.data, **entry.options}

    coordinator = BatteryOptimizerCoordinator(
        hass,
        price_entity_id              = cfg[CONF_PRICE_ENTITY],
        capacity                     = cfg.get(CONF_CAPACITY,   DEFAULT_CAPACITY),
        power                        = cfg.get(CONF_POWER,      DEFAULT_POWER),
        min_soc                      = cfg.get(CONF_MIN_SOC,    DEFAULT_MIN_SOC),
        max_soc                      = cfg.get(CONF_MAX_SOC,    DEFAULT_MAX_SOC),
        min_profit                   = cfg.get(CONF_MIN_PROFIT, DEFAULT_MIN_PROFIT),
        zen_soc_entity               = cfg.get(CONF_ZEN_SOC)                or None,
        zen_soc_min_entity           = cfg.get(CONF_ZEN_SOC_MIN)            or None,
        zen_soc_max_entity           = cfg.get(CONF_ZEN_SOC_MAX)            or None,
        zen_inverse_max_power_entity = cfg.get(CONF_ZEN_INVERSE_MAX_POWER)  or None,
    )

    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator: BatteryOptimizerCoordinator = hass.data[DOMAIN].get(entry.entry_id)
    if coordinator:
        coordinator.async_teardown()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
