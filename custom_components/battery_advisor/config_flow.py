"""Config flow for Battery Dispatch Advisor."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    CONF_PRICE_ENTITY,
    CONF_CAPACITY, CONF_POWER, CONF_MIN_SOC, CONF_MAX_SOC, CONF_MIN_PROFIT,
    CONF_ZEN_SOC, CONF_ZEN_SOC_MIN, CONF_ZEN_SOC_MAX, CONF_ZEN_INVERSE_MAX_POWER,
    DEFAULT_CAPACITY, DEFAULT_POWER, DEFAULT_MIN_SOC, DEFAULT_MAX_SOC, DEFAULT_MIN_PROFIT,
)

_SENSOR_SEL = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
_NUMBER_SEL = selector.EntitySelector(selector.EntitySelectorConfig(domain="number"))


def _schema(d: dict) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_PRICE_ENTITY,  default=d.get(CONF_PRICE_ENTITY,  "")): _SENSOR_SEL,
        vol.Required(CONF_CAPACITY,      default=d.get(CONF_CAPACITY,      DEFAULT_CAPACITY)):  vol.Coerce(float),
        vol.Required(CONF_POWER,         default=d.get(CONF_POWER,         DEFAULT_POWER)):     vol.Coerce(float),
        vol.Required(CONF_MIN_SOC,       default=d.get(CONF_MIN_SOC,       DEFAULT_MIN_SOC)):   vol.All(int, vol.Range(min=0,  max=49)),
        vol.Required(CONF_MAX_SOC,       default=d.get(CONF_MAX_SOC,       DEFAULT_MAX_SOC)):   vol.All(int, vol.Range(min=51, max=100)),
        vol.Required(CONF_MIN_PROFIT,    default=d.get(CONF_MIN_PROFIT,    DEFAULT_MIN_PROFIT)): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=50.0)),
        vol.Optional(CONF_ZEN_SOC,       default=d.get(CONF_ZEN_SOC,       "")): _SENSOR_SEL,
        vol.Optional(CONF_ZEN_SOC_MIN,   default=d.get(CONF_ZEN_SOC_MIN,   "")): _NUMBER_SEL,
        vol.Optional(CONF_ZEN_SOC_MAX,   default=d.get(CONF_ZEN_SOC_MAX,   "")): _NUMBER_SEL,
        vol.Optional(CONF_ZEN_INVERSE_MAX_POWER, default=d.get(CONF_ZEN_INVERSE_MAX_POWER, "")): _NUMBER_SEL,
    })


def _validate(u: dict) -> dict:
    if u[CONF_MIN_SOC] >= u[CONF_MAX_SOC]:
        return {"base": "soc_range_invalid"}
    return {}


class BatteryOptimizerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                entity_id = user_input[CONF_PRICE_ENTITY]
                await self.async_set_unique_id(f"battery_advisor_{entity_id}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Battery Advisor ({entity_id})",
                    data=user_input,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input or {}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return BatteryOptimizerOptionsFlow(config_entry)


class BatteryOptimizerOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry):
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        errors = {}
        current = {**self._config_entry.data, **self._config_entry.options}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=_schema(user_input or current),
            errors=errors,
        )
