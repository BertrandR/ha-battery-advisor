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
    CONF_RTE, CONF_DISCHARGE_USAGE_POWER, CONF_RETURN_PRICE_FORMULA,
    CONF_ZEN_SOC, CONF_ZEN_SOC_MIN, CONF_ZEN_SOC_MAX, CONF_ZEN_INVERSE_MAX_POWER,
    DEFAULT_CAPACITY, DEFAULT_POWER, DEFAULT_MIN_SOC, DEFAULT_MAX_SOC, DEFAULT_MIN_PROFIT,
    DEFAULT_RTE, DEFAULT_DISCHARGE_USAGE_POWER, DEFAULT_RETURN_PRICE_FORMULA,
)

_SENSOR_SEL     = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
_OPT_SENSOR_SEL = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", multiple=False))
_OPT_NUMBER_SEL = selector.EntitySelector(selector.EntitySelectorConfig(domain="number", multiple=False))


def _schema(d: dict) -> vol.Schema:
    return vol.Schema({
        # ── Required ──────────────────────────────────────────────────────────
        vol.Required(CONF_PRICE_ENTITY, default=d.get(CONF_PRICE_ENTITY, "")): _SENSOR_SEL,
        vol.Required(CONF_CAPACITY,     default=d.get(CONF_CAPACITY,  DEFAULT_CAPACITY)):  vol.Coerce(float),
        vol.Required(CONF_POWER,        default=d.get(CONF_POWER,     DEFAULT_POWER)):     vol.Coerce(float),
        vol.Required(CONF_MIN_SOC,      default=d.get(CONF_MIN_SOC,   DEFAULT_MIN_SOC)):   vol.All(int, vol.Range(min=0,  max=49)),
        vol.Required(CONF_MAX_SOC,      default=d.get(CONF_MAX_SOC,   DEFAULT_MAX_SOC)):   vol.All(int, vol.Range(min=51, max=100)),
        vol.Required(CONF_MIN_PROFIT,   default=d.get(CONF_MIN_PROFIT, DEFAULT_MIN_PROFIT)): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
        vol.Required(CONF_RTE,          default=d.get(CONF_RTE,        DEFAULT_RTE)):        vol.All(int, vol.Range(min=50, max=100)),
        vol.Required(CONF_DISCHARGE_USAGE_POWER, default=d.get(CONF_DISCHARGE_USAGE_POWER, DEFAULT_DISCHARGE_USAGE_POWER)): vol.All(vol.Coerce(float), vol.Range(min=0.05, max=10.0)),
        # ── Return price formula (optional — leave blank for Zonneplan auto-detect) ─
        vol.Optional(CONF_RETURN_PRICE_FORMULA, default=d.get(CONF_RETURN_PRICE_FORMULA, DEFAULT_RETURN_PRICE_FORMULA)): str,
        # ── Zendure (all optional — leave blank to disable) ───────────────────
        vol.Optional(CONF_ZEN_SOC):               _OPT_SENSOR_SEL,
        vol.Optional(CONF_ZEN_SOC_MIN):           _OPT_NUMBER_SEL,
        vol.Optional(CONF_ZEN_SOC_MAX):           _OPT_NUMBER_SEL,
        vol.Optional(CONF_ZEN_INVERSE_MAX_POWER): _OPT_SENSOR_SEL,
    })


def _validate(u: dict) -> dict:
    if u[CONF_MIN_SOC] >= u[CONF_MAX_SOC]:
        return {"base": "soc_range_invalid"}
    # Validate formula is safe — only allow arithmetic on current_price
    formula = u.get(CONF_RETURN_PRICE_FORMULA, DEFAULT_RETURN_PRICE_FORMULA)
    if formula:
        try:
            result = eval(formula, {"__builtins__": {}}, {"current_price": 100.0})  # noqa: S307
            if not isinstance(result, (int, float)):
                return {"base": "formula_invalid"}
        except Exception:
            return {"base": "formula_invalid"}
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
