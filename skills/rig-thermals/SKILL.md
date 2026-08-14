---
name: rig-thermals
description: Read temperatures, fan and pump speeds, and cooling efficiency trends for a workstation, and decide whether it needs physical cleaning or repasting. Use when the user asks about heat, temperature, fans, noise, throttling, thermal limits, "is my PC dusty", "does it need cleaning", "why is it so hot", "should I repaste", GPU or CPU temps, or whether cooling has degraded over time. Also use before attributing slowness to heat.
---

# rig-thermals

```
rig thermals
```

Prints current temperatures, the heat path split into its two halves, and the
cooling-efficiency comparison against a month earlier.

`rig` is on PATH whenever this plugin is installed, and needs only the
Prometheus endpoint. If it cannot reach it, the error names the command to
start the stack.

## A temperature cannot tell you the machine is dirty

A hot CPU means the CPU is working. What dust changes is **thermal resistance**
— the degrees you pay per watt of heat removed:

```
R = (T_component - T_ambient) / P
```

`R` is a property of the hardware, not the workload, so it is comparable across
months. Dust raises it. That is what this stack records, and it is the only
honest way to answer "does it need cleaning".

Never answer that question from a raw temperature reading.

## Read the split

`rig thermals` breaks the CPU heat path in two, and each half fails for a
different reason:

| Series | Rising means | Do |
| --- | --- | --- |
| `rig:thermal:coolant_rise_c` — coolant above case air | Radiator fins or intake blocked | Clean the radiator and intake |
| `rig:thermal:die_to_coolant_c` — die above coolant | Cold plate contact worse | Reseat and repaste |
| `rig:thermal:coolant_delta_c` — across the block | Flow dropping | Check the pump |
| `rig:thermal:gpu_resistance_c_per_w` | GPU cooler airflow blocked | Clean the card and filters |

Both CPU halves rising together is age. Only the delta rising is the pump.

## The degradation ratios

```
rig q 'rig:thermal:gpu_degradation_ratio'         # >1.15 is dust
rig q 'rig:thermal:radiator_degradation_ratio'    # >1.20 is dust
rig q 'rig:thermal:mount_degradation_ratio'       # >1.20 is dried paste
```

Each compares the last 7 days against the same 7 days 30 days earlier. `1.0` is
unchanged.

**Empty means not enough history, not "fine".** The comparison needs 37 days.
Until then `rig thermals` prints `no baseline yet` — report exactly that. Do
not substitute a current temperature and call it a degradation finding.

## When the ratios lie

- **A changed fan curve invalidates the baseline.** The comparison assumes the
  fan responds to temperature as it did a month ago. Check `rig:thermal:gpu_fan_ratio`
  and `rig:fan_rpm` alongside; if the fan is visibly doing less work, the ratio
  is measuring a BIOS edit.
- **Cleaning drives it below 1.0 for 30 days** while the dirty period is still
  inside the comparison window. That is the confirmation the clean worked.
- **Ambient is a proxy.** The motherboard sensor sits inside the case, so it
  partly tracks the machine's own heat. This makes the detector conservative,
  not wrong.
- **A dead sensor reads as perfect stability.** Run `rig health` if a thermal
  figure looks impossibly flat.
- **GPU thermal resistance is only sampled above 60 W.** An idle GPU leaves it
  empty; that is by design, not a fault.

## Throttling

```
rig q 'rig:thermal:gpu_headroom_c'    # degrees remaining before the card throttles
```

Small is bad. If headroom is low *and* the fan is already at maximum, the cooler
is the constraint, not the workload. If the fan has room, it is the workload.

## Heat is rarely the reason a machine feels slow

Before attributing slowness to temperature, check that anything is actually
throttling. A machine stalled on IO is far more common, and heat is a symptom of
load rather than its cause. Use **rig-diagnose** first, and only return here if
`rig:thermal:gpu_headroom_c` is genuinely small or `rig:cpu_celsius` is
sustained above 90.

## Where things are

`rig where` prints the stack root on this machine — never hardcode it. Method,
physics and full failure modes: `$(rig where -q)/docs/thermals.md`.
Dashboard: <http://localhost:13337/d/rig-thermals>.

The dashboard's degradation panel keeps its own 90-day window regardless of the
dashboard range, so it stays meaningful when the rest is zoomed to hours.
