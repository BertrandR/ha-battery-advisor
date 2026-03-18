"""Constants for Battery Advisor."""

DOMAIN = "battery_advisor"

# ── Battery name ──────────────────────────────────────────────────────────────
CONF_BATTERY_NAME   = "battery_name"

# ── Price sensor ─────────────────────────────────────────────────────────────
CONF_PRICE_ENTITY           = "price_entity_id"
CONF_RETURN_PRICE_FORMULA   = "return_price_formula"

# ── Battery ───────────────────────────────────────────────────────────────────
CONF_CHARGE_ENERGY      = "charge_energy"       # grid-side kWh to go min→max SoC
CONF_DISCHARGE_ENERGY   = "discharge_energy"    # grid-side kWh delivered max→min SoC
CONF_CHARGE_POWER       = "charge_power"        # kW grid-side charge rate
CONF_DISCHARGE_POWER    = "discharge_power"     # kW grid-side discharge rate

# ── Installation ──────────────────────────────────────────────────────────────
CONF_DISCHARGE_USAGE_POWER  = "discharge_usage_power"   # kW assumed drain in usage mode

# ── Schedule ──────────────────────────────────────────────────────────────────
CONF_MIN_PROFIT     = "min_profit"

# ── Battery SoC (optional) ────────────────────────────────────────────────────
CONF_ZEN_SOC        = "zendure_soc_entity"

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_CHARGE_ENERGY       = 7.9    # kWh  (measured)
DEFAULT_DISCHARGE_ENERGY    = 6.44   # kWh  (measured)
DEFAULT_CHARGE_POWER        = 2.4    # kW
DEFAULT_DISCHARGE_POWER     = 2.4    # kW
DEFAULT_DISCHARGE_USAGE_POWER = 0.3  # kW
DEFAULT_MIN_PROFIT          = 0.02   # EUR/kWh
DEFAULT_RETURN_PRICE_FORMULA = ""  # empty = auto-detect (use native excl-tax if available)

# ── Actions ───────────────────────────────────────────────────────────────────
ACTION_CHARGE_GRID      = "charge_grid"
ACTION_CHARGE_SOLAR     = "charge_solar"
ACTION_DISCHARGE_GRID   = "discharge_grid"
ACTION_DISCHARGE_USAGE  = "discharge_usage"
ACTION_IDLE             = "idle"

CHARGE_ACTIONS    = {ACTION_CHARGE_GRID, ACTION_CHARGE_SOLAR}
DISCHARGE_ACTIONS = {ACTION_DISCHARGE_GRID, ACTION_DISCHARGE_USAGE}
