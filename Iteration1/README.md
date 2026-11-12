# BOYANT – 365-day Southern Ocean simulation
Site 63.67°S, 40.49°E · NASA POWER hourly weather 1 Jan–31 Dec 2025 (local solar time) · 10-minute simulation step

## Run it
```
pip install numpy pandas matplotlib
python boyant_sim.py --weather POWER_Point_Hourly_20250101_20260101_063d67S_040d49E_LST.csv --out logs --scenarios
python make_plots.py
```
Every parameter is in the `CFG` dict at the top of `boyant_sim.py` and tagged
`[SHEET]` (from your spreadsheet), `[SLIDE]` (from your slides/report) or `[ASSUMED]` (my placeholder – replace it).

## Files
| File | What it is |
|---|---|
| `hourly_log.csv` | 8760 rows – weather, sun, PV, SOC, voltage, temperatures, mode, heater, every subsystem's Wh, task counts, SD usage |
| `step_log_10min.csv.gz` | the raw 52,560-row engine log (10 min) |
| `daily_summary.csv` / `monthly_summary.csv` | aggregates, min/max SOC, energy balance, hours per mode, autonomy days |
| `pv_panels_hourly.csv` | power and plane-of-array irradiance for each of the 8 panels |
| `task_log.csv` | every sensor cycle, IMU burst, GNSS fix (hot/cold), satellite window – with joules per subsystem |
| `event_log.csv` | mode changes, heater on/off + reason, BMS cold-charge inhibit/allow, alerts, brown-outs |
| `subsystem_energy_annual.csv` | annual Wh by consumer |
| `scenario_comparison.csv`, `scenarios/` | 6 sensitivity runs (daily + hourly-core logs) |
| `config_used.json` | the exact parameters of the baseline run |
| `fig1…fig3 *.png` | quick-look plots |

Timestamps are hour-start, local solar time (as in the NASA file). Energies are Wh per step unless named `_w`, `_j`.
`soc_pct` = stored energy ÷ temperature-derated capacity (your capacity-vs-temperature table); `soc_nom_pct` = ÷ 192 Wh.

## Model summary
* **Sun/PV** – solar position from lat/lon; hourly GHI split into beam/diffuse (Erbs), transposed onto 8 panels tilted 45° (cone R = H = 300 mm) at 45° azimuth spacing, with incidence-angle loss, cell-temperature gain, albedo (open water → pack ice), 4 series pairs (adjacent panels, current-limited by the weaker one), soiling/motion/wiring derates, 95 % MPPT.
* **Battery** – 192 Wh, 95 %/95 % charge/discharge efficiency, 3 %/month self-discharge, capacity vs temperature, **BMS blocks charging below 0 °C**, brown-out at 3 % and restart at 10 %.
* **Thermal** – lumped node (battery + electronics), HDPE 8 mm + 10 mm air gap + XPS 50 mm + PC 5 mm stack, split between sea water and air (wind-dependent), internal heat from electronics/converters/MPPT, 6 W PTC heater.
* **Controller** – NORMAL / LOW-ENERGY-WINTER / DEEP-SLEEP / OFF, with satellite window and heater as overlays; SOC hysteresis plus a 72 h solar-outlook rule; heater policy "adaptive" (protect < −12 °C, or warm the battery to > 0 °C only when PV surplus can power it and the battery can accept charge).

## Things to confirm (`[ASSUMED]` values that matter)
1. **Satellite modem current** – not in your sheet. I used 100 mA × 105 s TX + 20 mA × 15 s RX per window.
2. **Missing sensor data** – DO (15 mA, 10 s), depth sensor (1 mA), photodiode, TDS warm-up (3 s), salinity treated as derived from conductivity (the separate salinity sensor is not simulated).
3. **Thermal geometry** – insulated compartment area 0.5 m², 60 % below waterline, 0.03 W/K bridges, 3.5 kJ/K heat capacity, PC k = 0.2 W/mK.
4. **Sea temperature** – no SST data supplied; seasonal model from +0.4 °C (Feb) to −1.8 °C (winter).
5. **Albedo, soiling, wave-heel losses, buck efficiency (90 %), BMS/MPPT idle draw (4 mW).**
6. The SD module is assumed **power-gated** (its 15–30 mA idle current is the single biggest risk – see scenarios).

## Issues spotted in the spreadsheet
* Solar block: `Vmp = 8.3 A` and `Imp = 0.85 A` are swapped (must be 8.3 V / 0.85 A – that matches area × efficiency = 7.06 W). `rated power = 5V` is also not a power (it is 7.06 W).
* Sensor-table "duty cycle" column and the SD card "sleep curr 15–30 mA" look like copy errors; I treated the SD idle current as 15–30 mA only when the module is not power-gated.
* The keel notes mention a 20 kg buoy; the simulation does not model buoyancy/stability (only energy, thermal, solar).
* Hourly NASA GHI is horizontal only – tilted-plane values are modelled, not measured, and at sun elevations < 5° (all of June–July) are the least certain numbers.
