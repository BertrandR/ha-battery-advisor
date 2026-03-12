"""Constants for Battery Dispatch Advisor."""

DOMAIN = "battery_advisor"

# ── Price sensor ────────────────────────────────────────────────────────────
CONF_PRICE_ENTITY   = "price_entity_id"

# ── Battery (manual fallback values) ────────────────────────────────────────
CONF_CAPACITY           = "capacity"
CONF_POWER              = "power"
CONF_MIN_SOC            = "min_soc"
CONF_MAX_SOC            = "max_soc"
CONF_MIN_PROFIT         = "min_profit"
CONF_RTE                = "round_trip_efficiency"
CONF_DISCHARGE_USAGE_POWER  = "discharge_usage_power"   # kW when discharging to cover usage
CONF_RETURN_PRICE_FORMULA   = "return_price_formula"    # formula to derive return tariff from buy price
                                                         # variable: current_price (EUR/MWh)

# ── Zendure device entities (all optional) ───────────────────────────────────
CONF_ZEN_SOC                  = "zendure_soc_entity"
CONF_ZEN_SOC_MIN              = "zendure_soc_min_entity"
CONF_ZEN_SOC_MAX              = "zendure_soc_max_entity"
CONF_ZEN_INVERSE_MAX_POWER    = "zendure_inverse_max_power_entity"

# ── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_CAPACITY                = 10.0
DEFAULT_POWER                   = 3.0
DEFAULT_MIN_SOC                 = 10
DEFAULT_MAX_SOC                 = 90
DEFAULT_MIN_PROFIT              = 0.02   # EUR/kWh — minimum price spread to justify a cycle
DEFAULT_RTE                     = 85     # % — round trip efficiency
DEFAULT_DISCHARGE_USAGE_POWER   = 0.3    # kW — reduced power when discharging to cover usage
DEFAULT_RETURN_PRICE_FORMULA    = "current_price"  # identity — override for tax stripping

# ── Dispatch actions ─────────────────────────────────────────────────────────
ACTION_CHARGE_GRID      = "charge_grid"      # charge from grid at buy tariff
ACTION_CHARGE_SOLAR     = "charge_solar"     # charge from solar at return tariff (opportunity cost)
ACTION_DISCHARGE_NET    = "discharge_net"    # export to net at return tariff
ACTION_DISCHARGE_USAGE  = "discharge_usage"  # cover own usage at buy tariff, reduced power
ACTION_IDLE             = "idle"

# Legacy aliases — kept so existing filter/savings logic can reference them cleanly
ACTION_CHARGE    = ACTION_CHARGE_GRID       # primary charge action
ACTION_DISCHARGE = ACTION_DISCHARGE_NET     # primary discharge action

# All charge / discharge action sets for convenience
CHARGE_ACTIONS    = {ACTION_CHARGE_GRID, ACTION_CHARGE_SOLAR}
DISCHARGE_ACTIONS = {ACTION_DISCHARGE_NET, ACTION_DISCHARGE_USAGE}
