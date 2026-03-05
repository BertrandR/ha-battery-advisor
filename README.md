# Battery Dispatch Advisor — Home Assistant Integration

Reads hourly electricity prices from an **existing HA sensor** and computes an
optimal charge/discharge schedule for your home battery using dynamic programming.

No external API calls. Works with Nordpool (official & HACS), Tibber, and any
sensor exposing hourly prices as attributes.

---

## Supported price sensors

| Integration | Sensor | Format detected |
|-------------|--------|----------------|
| **Nord Pool** (official, built-in) | `sensor.nordpool_...` | `raw_today` / `raw_tomorrow` attributes |
| **Nord Pool** (HACS custom component) | `sensor.nordpool_...` | `raw_today` / `raw_tomorrow` attributes |
| **Zonneplan ONE** (HACS) | `sensor.zonneplan_current_electricity_tariff` | `forecast` attribute with `{datetime, electricity_price}` list |
| **Tibber** (HACS) | `sensor.tibber_...` | `prices` attribute with `{startsAt, total}` list |
| **Generic** | any | `today`, `prices`, or `hourly_prices` attribute with a list of floats |

Prices are auto-normalised to EUR/MWh regardless of source unit.

---

## Installation

1. Copy the `custom_components/battery_advisor/` folder to:
   ```
   /config/custom_components/battery_advisor/
   ```

2. Restart Home Assistant.

3. Go to **Settings → Devices & Services → + Add Integration**,
   search for **Battery Dispatch Advisor**.

4. Select your **electricity price sensor** from the entity picker,
   then fill in your battery parameters:
   - **Battery capacity** (kWh)
   - **Max charge/discharge power** (kW)
   - **Min / Max SoC** (%)
   - **Minimum cycle profit** (EUR) — cycles below this are skipped

---

## Sensors created

| Entity | State | Use for |
|--------|-------|---------|
| `sensor.battery_advisor_current_action` | `charge` / `discharge` / `idle` | Automation triggers |
| `sensor.battery_advisor_current_price`  | float (EUR/MWh) | Numeric conditions |
| `sensor.battery_advisor_next_action`    | `charge` / `discharge` / `idle` | Lookahead |
| `sensor.battery_advisor_daily_savings`  | float (EUR net) | Dashboard |
| `sensor.battery_advisor_schedule`       | int (hours)     | Full schedule in attributes |

Prices are re-read from the source sensor every hour.
Tomorrow's prices are automatically included when `tomorrow_valid` is true (Nordpool).

---

## Algorithm

The optimizer uses **backward dynamic programming** over the state space
`(hour, SoC)` to find the globally optimal schedule — correctly handling
multiple charge/discharge cycles per day, partial charges, and negative prices.

A **minimum profit filter** then suppresses any cycle whose net
`revenue − cost` is below your configured threshold, preventing the battery
from cycling on negligibly small price spreads.

---

## Automation examples

### Start charging

```yaml
automation:
  - alias: "Battery: Start charging"
    trigger:
      - platform: state
        entity_id: sensor.battery_advisor_current_action
        to: "charge"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.battery_charger
```

### Stop when no longer optimal

```yaml
automation:
  - alias: "Battery: Stop charging"
    trigger:
      - platform: state
        entity_id: sensor.battery_advisor_current_action
        from: "charge"
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.battery_charger
```

### Combined charge/discharge handler

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
                state: "charge"
            sequence:
              - service: switch.turn_on
                target: { entity_id: switch.battery_charge_mode }
              - service: switch.turn_off
                target: { entity_id: switch.battery_discharge_mode }
          - conditions:
              - condition: state
                entity_id: sensor.battery_advisor_current_action
                state: "discharge"
            sequence:
              - service: switch.turn_off
                target: { entity_id: switch.battery_charge_mode }
              - service: switch.turn_on
                target: { entity_id: switch.battery_discharge_mode }
        default:
          - service: switch.turn_off
            target: { entity_id: switch.battery_charge_mode }
          - service: switch.turn_off
            target: { entity_id: switch.battery_discharge_mode }
```

### Guard: only charge if price is below threshold

```yaml
condition:
  - condition: numeric_state
    entity_id: sensor.battery_advisor_current_price
    below: 80   # EUR/MWh
```

### Template: check any hour's action

```yaml
{% set schedule = state_attr('sensor.battery_advisor_schedule', 'schedule') %}
{% set h = now().strftime('%H:00') %}
{{ (schedule | selectattr('hour', 'eq', h) | list | first).action }}
```

---

## Updating parameters

**Settings → Devices & Services → Battery Dispatch Advisor → Configure**
— change sensor, capacity, power, SoC limits or profit threshold at any time
without reinstalling.
