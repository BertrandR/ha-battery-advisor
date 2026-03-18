"""Config flow for Battery Advisor — multi-step."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
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

_SENSOR_SEL     = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
_OPT_SENSOR_SEL = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", multiple=False))

_pos_float = lambda v: vol.All(vol.Coerce(float), vol.Range(min=0.01))


def _validate_formula(formula: str) -> bool:
    if not formula:
        return True   # empty = use native excl-tax (Zonneplan) or buy price (others)
    try:
        result = eval(formula, {"__builtins__": {}}, {"current_price": 100.0})  # noqa: S307
        return isinstance(result, (int, float))
    except Exception:
        return False


def _formula_schema(current_formula: str) -> vol.Schema:
    """
    Return a schema for the price-sensor step.

    The formula field uses vol.Required (not vol.Optional) so that an empty
    submission is explicitly saved as "" rather than being omitted from
    user_input and leaving the old value in place.
    """
    return vol.Schema({
        vol.Required(CONF_PRICE_ENTITY): _SENSOR_SEL,
        vol.Required(
            CONF_RETURN_PRICE_FORMULA,
            default=current_formula,
        ): str,
    })


class BatteryAdvisorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self):
        self._data: dict = {}

    # ── Step 1: Price sensor ──────────────────────────────────────────────────

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            formula = user_input.get(CONF_RETURN_PRICE_FORMULA, DEFAULT_RETURN_PRICE_FORMULA)
            if not _validate_formula(formula):
                errors[CONF_RETURN_PRICE_FORMULA] = "formula_invalid"
            else:
                self._data.update(user_input)
                return await self.async_step_battery()
        d = self._data
        return self.async_show_form(
            step_id="user",
            data_schema=_formula_schema(d.get(CONF_RETURN_PRICE_FORMULA, DEFAULT_RETURN_PRICE_FORMULA)),
            errors=errors,
        )

    # ── Step 2: Battery ───────────────────────────────────────────────────────

    async def async_step_battery(self, user_input=None):
        errors = {}
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_schedule()
        d = self._data
        return self.async_show_form(
            step_id="battery",
            data_schema=vol.Schema({
                vol.Required(CONF_CHARGE_ENERGY,
                    default=d.get(CONF_CHARGE_ENERGY, DEFAULT_CHARGE_ENERGY)):    _pos_float(0),
                vol.Required(CONF_DISCHARGE_ENERGY,
                    default=d.get(CONF_DISCHARGE_ENERGY, DEFAULT_DISCHARGE_ENERGY)): _pos_float(0),
                vol.Required(CONF_CHARGE_POWER,
                    default=d.get(CONF_CHARGE_POWER, DEFAULT_CHARGE_POWER)):      _pos_float(0),
                vol.Required(CONF_DISCHARGE_POWER,
                    default=d.get(CONF_DISCHARGE_POWER, DEFAULT_DISCHARGE_POWER)): _pos_float(0),
            }),
            errors=errors,
        )

    # ── Step 3: Schedule ──────────────────────────────────────────────────────

    async def async_step_schedule(self, user_input=None):
        errors = {}
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_soc()
        d = self._data
        return self.async_show_form(
            step_id="schedule",
            data_schema=vol.Schema({
                vol.Required(CONF_MIN_PROFIT,
                    default=d.get(CONF_MIN_PROFIT, DEFAULT_MIN_PROFIT)):
                    vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
                vol.Required(CONF_DISCHARGE_USAGE_POWER,
                    default=d.get(CONF_DISCHARGE_USAGE_POWER, DEFAULT_DISCHARGE_USAGE_POWER)):
                    vol.All(vol.Coerce(float), vol.Range(min=0.05, max=10.0)),
            }),
            errors=errors,
        )

    # ── Step 4: Battery SoC (optional) ───────────────────────────────────────

    async def async_step_soc(self, user_input=None):
        if user_input is not None:
            self._data.update({k: v for k, v in user_input.items() if v})
            entity_id = self._data[CONF_PRICE_ENTITY]
            await self.async_set_unique_id(f"battery_advisor_{entity_id}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"Battery Advisor ({entity_id})",
                data=self._data,
            )
        d = self._data
        return self.async_show_form(
            step_id="soc",
            data_schema=vol.Schema({
                vol.Optional(CONF_ZEN_SOC,
                    default=d.get(CONF_ZEN_SOC, "")): _OPT_SENSOR_SEL,
            }),
            last_step=True,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return BatteryAdvisorOptionsFlow(config_entry)


# ── Options flow — mirrors setup steps ───────────────────────────────────────

class BatteryAdvisorOptionsFlow(config_entries.OptionsFlow):

    def __init__(self, config_entry):
        self._config_entry = config_entry
        self._data: dict = {}

    def _current(self) -> dict:
        return {**self._config_entry.data, **self._config_entry.options}

    async def async_step_init(self, user_input=None):
        return await self.async_step_user(user_input)

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            formula = user_input.get(CONF_RETURN_PRICE_FORMULA, DEFAULT_RETURN_PRICE_FORMULA)
            if not _validate_formula(formula):
                errors[CONF_RETURN_PRICE_FORMULA] = "formula_invalid"
            else:
                self._data.update(user_input)
                return await self.async_step_battery()
        d = {**self._current(), **self._data}
        return self.async_show_form(
            step_id="user",
            data_schema=_formula_schema(d.get(CONF_RETURN_PRICE_FORMULA, DEFAULT_RETURN_PRICE_FORMULA)),
            errors=errors,
        )

    async def async_step_battery(self, user_input=None):
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_schedule()
        d = {**self._current(), **self._data}
        return self.async_show_form(
            step_id="battery",
            data_schema=vol.Schema({
                vol.Required(CONF_CHARGE_ENERGY,
                    default=d.get(CONF_CHARGE_ENERGY, DEFAULT_CHARGE_ENERGY)):    _pos_float(0),
                vol.Required(CONF_DISCHARGE_ENERGY,
                    default=d.get(CONF_DISCHARGE_ENERGY, DEFAULT_DISCHARGE_ENERGY)): _pos_float(0),
                vol.Required(CONF_CHARGE_POWER,
                    default=d.get(CONF_CHARGE_POWER, DEFAULT_CHARGE_POWER)):      _pos_float(0),
                vol.Required(CONF_DISCHARGE_POWER,
                    default=d.get(CONF_DISCHARGE_POWER, DEFAULT_DISCHARGE_POWER)): _pos_float(0),
            }),
        )

    async def async_step_schedule(self, user_input=None):
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_soc()
        d = {**self._current(), **self._data}
        return self.async_show_form(
            step_id="schedule",
            data_schema=vol.Schema({
                vol.Required(CONF_MIN_PROFIT,
                    default=d.get(CONF_MIN_PROFIT, DEFAULT_MIN_PROFIT)):
                    vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
                vol.Required(CONF_DISCHARGE_USAGE_POWER,
                    default=d.get(CONF_DISCHARGE_USAGE_POWER, DEFAULT_DISCHARGE_USAGE_POWER)):
                    vol.All(vol.Coerce(float), vol.Range(min=0.05, max=10.0)),
            }),
        )

    async def async_step_soc(self, user_input=None):
        if user_input is not None:
            self._data.update({k: v for k, v in user_input.items() if v})
            return self.async_create_entry(title="", data=self._data)
        d = {**self._current(), **self._data}
        return self.async_show_form(
            step_id="soc",
            data_schema=vol.Schema({
                vol.Optional(CONF_ZEN_SOC,
                    default=d.get(CONF_ZEN_SOC, "")): _OPT_SENSOR_SEL,
            }),
            last_step=True,
        )
