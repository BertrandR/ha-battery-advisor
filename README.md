# Battery Dispatch Advisor — Home Assistant Integration

Reads hourly electricity prices from an **existing HA sensor** and computes an
optimal charge/discharge schedule for your home battery using dynamic programming.

No external API calls. Supports Nordpool, Zonneplan ONE, Tibber, and any sensor
exposing hourly prices as attributes. Automatically uses buy and return tariffs
separately when available (e.g. Zonneplan `electricity_price_excl_tax`).

---

## Supported price sensors

| Integration | Sensor | Return tariff |
|---|---|---|
| **Nord Pool** (official / HACS) | `sensor.nordpool_...` | Derived via formula (e.g. `current_price / 1.21`) |
| **Zonneplan ONE** (HACS) | `sensor.zonneplan_current_electricity_tariff` | Native `electricity_price_excl_tax` (auto) |
| **Tibber** (HACS) | `sensor.tibber_...` | Derived via formula |
| **Generic** | any sensor with `today` / `prices` / `hourly_prices` float list | Derived via formula |

Prices are auto-normalised to EUR/MWh regardless of source unit.

---

## Installation

### HACS
Add this repository as a custom repository in HACS, then install **Battery Dispatch Advisor**.

### Manual
1. Copy `custom_components/battery_advisor/` to `/config/custom_components/battery_advisor/`
2. Restart Home Assistant
3. Go to **Settings → Devices & Services → + Add Integration**, search for **Battery Dispatch Advisor**

---

## Configuration

Setup is a four-step flow:

### Step 1 — Price sensor
- **Price sensor entity** — your day-ahead price sensor
- **Return price formula** — leave empty to use the sensor's native return tariff (Zonneplan auto-detects `electricity_price_excl_tax`). Set to `current_price` to treat buy and return tariffs as equal. Other examples:
  - `current_price / 1.21` — strip 21% Dutch VAT
  - `(current_price - 52.9) / 1.21` — strip flat energy tax then VAT

### Step 2 — Battery
- **Charge energy** (kWh) — grid-side kWh drawn to go from min to max SoC (measure this)
- **Discharge energy** (kWh) — grid-side kWh delivered going from max to min SoC (measure this)
- **Charge power** (kW) — grid-side charge rate
- **Discharge power** (kW) — grid-side discharge rate

These two energy values replace the old capacity / min SoC / max SoC / RTE settings.
Efficiency and usable capacity are derived automatically:
`eff = √(discharge_energy / charge_energy)`, `usable_kwh = charge_energy × eff`

### Step 3 — Schedule
- **Minimum profit** (EUR/kWh) — suppress cycles below this effective spread after losses (default 0.02)
- **Discharge-to-usage power** (kW) — assumed drain rate when covering home usage (default 0.3)

### Step 4 — Zendure (optional)
- **Zendure SoC sensor** — if configured, the live SoC is used as the starting point for the DP optimiser. Leave blank to assume 50%.

---

## Sensors created

| Entity | State | Notes |
|---|---|---|
| `sensor.battery_advisor_current_action` | see below | Primary automation trigger |
| `sensor.battery_advisor_current_price` | EUR/MWh | Buy price; return price in attributes |
| `sensor.battery_advisor_next_action` | see below | Next scheduled action change |
| `sensor.battery_advisor_daily_savings` | EUR | Estimated net profit over forecast window |
| `sensor.battery_advisor_schedule` | int (slot count) | Full schedule + battery info in attributes |
| `sensor.battery_advisor_battery_soc` | % | Live SoC (only if Zendure entity configured) |

### Action values

| Value | Meaning |
|---|---|
| `charge_grid` | Charge from grid at buy tariff |
| `charge_solar` | Charge from solar (daylight only) at return tariff opportunity cost |
| `discharge_net` | Export to grid at return tariff, full discharge power |
| `discharge_usage` | Cover home usage at buy tariff, reduced power |
| `idle` | No action |

`charge_solar` is selected during daylight hours (using `sun.sun` entity) when the
return tariff opportunity cost is lower than the grid buy price.
`discharge_usage` is selected when the buy tariff significantly exceeds the return
tariff, making covering home usage more profitable than exporting.

---

## Algorithm

The optimiser uses **backward dynamic programming** over `(hour, SoC)` state space
to find the globally optimal schedule across the full forecast window (~35 hours for
Zonneplan, 24–48 hours for Nordpool).

RTE losses are modelled symmetrically from the measured energy values:
`eff = √(discharge_energy / charge_energy)`. Charge costs and discharge revenues
are adjusted accordingly.

A **minimum profit filter** suppresses cycles whose effective spread after losses
falls below the configured threshold. Each charge block is paired with its
best-spread discharge block (not just the nearest) to avoid micro-cycles masking
the main arbitrage window.

---

## Automation examples

### Four-action handler

```yaml
automation:
  - alias: "Battery: Follow optimal schedule"
    trigger:
      - platform: state
        entity_id: sensor.battery_advisor_current_action
    action:
      - choose:
          - conditions:
              - condition: state
                entity_id: sensor.battery_advisor_current_action
                state: "charge_grid"
            sequence:
              - service: select.select_option
                target: { entity_id: select.battery_charge_mode }
                data: { option: "grid" }
          - conditions:
              - condition: state
                entity_id: sensor.battery_advisor_current_action
                state: "charge_solar"
            sequence:
              - service: select.select_option
                target: { entity_id: select.battery_charge_mode }
                data: { option: "solar" }
          - conditions:
              - condition: state
                entity_id: sensor.battery_advisor_current_action
                state: "discharge_net"
            sequence:
              - service: select.select_option
                target: { entity_id: select.battery_discharge_mode }
                data: { option: "grid" }
          - conditions:
              - condition: state
                entity_id: sensor.battery_advisor_current_action
                state: "discharge_usage"
            sequence:
              - service: select.select_option
                target: { entity_id: select.battery_discharge_mode }
                data: { option: "usage" }
        default:
          - service: select.select_option
            target: { entity_id: select.battery_mode }
            data: { option: "idle" }
```

### Check any hour's scheduled action

```yaml
{% set schedule = state_attr('sensor.battery_advisor_schedule', 'schedule') %}
{% set h = now().strftime('%H:00') %}
{{ (schedule | selectattr('hour', 'eq', h) | list | first).action }}
```

### Guard: only act if price is above threshold

```yaml
condition:
  - condition: numeric_state
    entity_id: sensor.battery_advisor_current_price
    above: 200   # EUR/MWh
```

---

## Reconfiguring

**Settings → Devices & Services → Battery Advisor → Configure** — update any
parameter at any time without reinstalling. Changes take effect immediately on save.

---

## Measuring your battery's energy values

The two key config values are best measured empirically:

1. Fully discharge your battery to its minimum SoC
2. Charge to maximum SoC and note grid draw from your smart meter → **charge energy**
3. Discharge back to minimum SoC and note grid delivery → **discharge energy**

For a Zendure AIO 2400 with 8.6 kWh nominal capacity, typical values are
around 7.9 kWh charge energy and 6.44 kWh discharge energy (83% RTE).
