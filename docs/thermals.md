# Thermal health, and detecting a dirty machine

A temperature reading cannot tell you the machine needs cleaning. A hot CPU
means the CPU is working. What a dirty cooler changes is not the temperature —
it is how much temperature you have to pay per watt of heat removed.

That quantity is **thermal resistance**:

```
R = (T_component - T_ambient) / P        degrees per watt
```

`R` is a property of the hardware, not of the workload. A clean cooler at 50 W
and the same cooler at 300 W have the same `R`. Dust on fins, a clogged filter
or a stopped fan raise it. That makes `R` comparable across months in a way a
raw temperature never is.

## What is recorded

`rig:ambient_celsius` is the motherboard sensor, standing in for case air. Every
figure below is measured against it, so a hot room does not read as a dirty
cooler.

### GPU — the clean measurement

```
rig:thermal:gpu_resistance_c_per_w = (gpu_temp - ambient) / gpu_watts
```

The GPU reports its own power draw, so this is real thermal resistance in real
units. Sampled only above 60 W; below that the numerator is sensor noise.

### CPU loop — split into two paths

The AIO exposes coolant temperatures, so the heat path splits and each half
fails for a different reason:

```
rig:thermal:die_to_coolant_c = cpu_temp     - coolant_in     (mount, paste)
rig:thermal:coolant_rise_c   = coolant_in   - ambient        (radiator, fans)
rig:thermal:coolant_delta_c  = |out - in|                    (flow, pump)
```

This split is the useful part. A rising `coolant_rise` with a flat
`die_to_coolant` is a dusty radiator — clean it. The reverse is dried paste or
a loosened cold plate — reseat it. Both rising is age. Only `coolant_delta`
rising is the pump.

`rig:thermal:cpu_resistance_index` normalises die-to-ambient by CPU busy ratio,
because k10temp on this chip exposes no package power. Its units are arbitrary;
only its trend against itself means anything.

## The degradation ratios

Instantaneous `R` is noisy. The comparison is done on week-long averages,
against the same week a month earlier:

```
rig:thermal:gpu_degradation_ratio =
    avg over last 7 days  /  avg over the 7 days ending 30 days ago
```

| Series | Rising means |
| --- | --- |
| `rig:thermal:gpu_degradation_ratio` | GPU cooler airflow blocked — dust |
| `rig:thermal:radiator_degradation_ratio` | Radiator fins or intake blocked — dust |
| `rig:thermal:mount_degradation_ratio` | Cold plate contact worse — dried paste |
| `rig:thermal:cpu_degradation_ratio` | The CPU path overall, either cause |

`1.0` is unchanged. Alerts fire above `1.15` (GPU) and `1.20` (the rest), held
for 6 hours.

Read them with:

```
tools/rig thermals
```

## When this lies

**Before 37 days of history it says nothing.** The ratio needs a 7-day window
and a 7-day window offset by 30 days. Until both exist the series is absent and
`rig thermals` prints `no baseline yet`. That is honest, not broken.

**A changed fan curve invalidates the baseline.** These ratios assume the fan
responds to temperature the same way it did a month ago. Change a curve in the
BIOS or in software and the comparison is against a different machine. Check
`rig:thermal:gpu_fan_ratio` and `rig:fan_rpm` alongside the ratio; if the fan
is doing visibly less work than before, the ratio is measuring your BIOS edit.

**Cleaning resets it.** After a clean, the ratio reads *below* 1.0 for 30 days
while the dirty period is still inside the comparison window. That is correct
behaviour and it is the confirmation the clean worked.

**Ambient is a proxy.** The motherboard sensor sits inside the case, so it
partly tracks the machine's own heat rather than room air. This compresses the
measured effect — it makes the detector conservative, not wrong.

**A dead sensor reads as perfect health.** `rig health` first if a thermal
figure looks impossibly stable.

## Acting on it

| Verdict | Do |
| --- | --- |
| GPU ratio > 1.15 | Blow out the card's fins and heatsink; check the intake filter |
| Radiator ratio > 1.20 | Clean the radiator fins and the intake path |
| Mount ratio > 1.20 | Reseat the cold plate and repaste |
| `RigPumpStalled` | Physical. Check the pump header and the fan curve |
| `RigGPUThrottleImminent` | If the fan is already at maximum, it is the cooler, not the workload |

After cleaning, note the date. The ratio confirms the work within a week and
fully re-baselines after 30 days.
