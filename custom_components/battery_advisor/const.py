"""Constants for Battery Dispatch Advisor."""

DOMAIN = "battery_advisor"

# ── Price sensor ────────────────────────────────────────────────────────────
CONF_PRICE_ENTITY   = "price_entity_id"

# ── Battery (manual fallback values) ────────────────────────────────────────
CONF_CAPACITY       = "capacity"
CONF_POWER          = "power"
CONF_MIN_SOC        = "min_soc"
CONF_MAX_SOC        = "max_soc"
CONF_MIN_PROFIT     = "min_profit"

# ── Zendure device entities (all optional) ───────────────────────────────────
CONF_ZEN_SOC                  = "zendure_soc_entity"
CONF_ZEN_SOC_MIN              = "zendure_soc_min_entity"
CONF_ZEN_SOC_MAX              = "zendure_soc_max_entity"
CONF_ZEN_INVERSE_MAX_POWER    = "zendure_inverse_max_power_entity"

# ── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_CAPACITY    = 10.0
DEFAULT_POWER       = 3.0
DEFAULT_MIN_SOC     = 10
DEFAULT_MAX_SOC     = 90
DEFAULT_MIN_PROFIT  = 0.10

# ── Dispatch actions ─────────────────────────────────────────────────────────
ACTION_CHARGE    = "charge"
ACTION_DISCHARGE = "discharge"
ACTION_IDLE      = "idle"
