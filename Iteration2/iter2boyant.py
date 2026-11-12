#!/usr/bin/env python3
"""
BOYANT - 365-day Southern Ocean deployment simulation  (v2.0)
==============================================================
Team Treadmill | SIH 2026 | PS 26065

Physics-based, energy-aware simulation of the buoy at 63.67S 40.49E, driven by
a NASA POWER hourly weather file.

v2.0 - fixes from the code review
---------------------------------
Correctness
  * Two thermal nodes: enclosure (T_enc) and battery (T_batt). Battery capacity,
    charge inhibit, internal resistance and the protect/assist heater use T_batt.
  * Battery I^2*R heating is computed. It is included in both the battery
    thermal node and the energy balance.
  * MPPT conversion loss is based on the energy actually harvested (direct use
    plus accepted charge). Curtailed potential no longer counts.
  * Temperature-dependent capacity is now consistent. E is the nominal energy
    content (<= nominal capacity). Cold makes the bottom (1 - f(T)) of the pack
    unusable. A temperature change can no longer push E above capacity.
  * Config values that were previously unused are now used or validated:
    - pv.area_m2 / pv.eff: cross-checked, or selectable as the power source
    - batt.min_soc_policy: the SOC floor for DEEP_SLEEP
    - sd.capacity_gb: enforced by a storage manager
    - step-log writing: controlled by --no-step-log
  * Weather input is validated: header, required columns, numeric types,
    timestamps, continuity, physical ranges, gap length, duration and LST/UTC
    basis. Interpolated hours are flagged and a QC summary is written.
  * Solar geometry and SST use the real day-of-year of the file's start date.
Reliability
  * SD card capacity with a 'stop' or 'overwrite_oldest' policy.
  * Satellite link model: success probability, outage windows, retries, and a
    packet backlog with alert priority and overflow handling.
  * Battery model: capacity fade, temperature-dependent R_int,
    charge/discharge current limits, and optional BMS temperature lockout.
  * Fault injection: individual panel failures and an icing accumulation model.
  * Unmet load forces a brown-out. Loads that could not be powered do not
    produce heat.
Validation / reproducibility
  * Config validation and per-scenario exception handling.
  * Run provenance: file hash, library versions, CLI arguments.
  * New modes: --selftest and --monte-carlo N.

Values marked [SHEET] or [SLIDE] come from the team's data. Values marked
[ASSUMED] are placeholders and must be calibrated before the results are used
for design decisions.

Requirements: Python >= 3.9, numpy >= 1.22, pandas >= 1.5

Usage:
    python boyant_sim.py --selftest
    python boyant_sim.py --weather POWER_....csv --out logs
    python boyant_sim.py --weather POWER_....csv --out logs --scenarios
    python boyant_sim.py --weather POWER_....csv --out logs --monte-carlo 200 --workers 4
    optional: --sst sst.csv --albedo albedo.csv   (CSV with columns: time,value)
"""
import argparse
import copy
import hashlib
import json
import logging
import math
import os
import platform
import sys
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

import numpy as np
import pandas as pd

SIM_VERSION = "2.0.0"
log = logging.getLogger("boyant")


class ConfigError(ValueError):
    pass


class WeatherError(ValueError):
    pass


# ----------------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------------
CFG = dict(
    name="baseline",
    load_margin=1.0,                                   # multiplier on all measured loads
    site=dict(lat=-63.6686, lon=40.4935),              # [SLIDE/CSV header]
    dt_min=10,                                         # simulation step (must divide 60)
    n_days=365,

    weather=dict(
        max_gap_h=6.0,              # longest tolerated run of missing hours per variable
        allow_gaps=False,           # True = interpolate longer gaps anyway (flagged in logs)
        allow_short=False,          # True = run shorter than n_days if the file is short
        time_basis="auto",          # "auto" | "LST" | "UTC"; the solar model needs local solar time
    ),

    # ---------------- Solar array ----------------
    pv=dict(
        n_panels=8,                      # [SHEET]
        v_mp=8.3, i_mp=0.85,             # [SHEET]
        area_m2=0.03656, eff=0.193,      # [SHEET]
        p_stc_source="vmp_imp",          # "vmp_imp" or "area_eff"
        p_stc_tolerance=0.05,            # warn if the two STC ratings disagree by more than 5 %
        tilt_deg=45.0,                   # [SHEET geometry]
        heading_deg=0.0,
        temp_coeff=-0.0035,              # [ASSUMED]
        noct=45.0,                       # [ASSUMED]
        wiring="series_pairs",           # [ASSUMED] "series_pairs" | "parallel"
        albedo_monthly=[0.08, 0.08, 0.10, 0.25, 0.55, 0.60, 0.60, 0.60, 0.55, 0.50, 0.30, 0.10],  # [ASSUMED]
        iam_b0=0.05,
        soiling=0.95,                    # [ASSUMED]
        motion=0.95,                     # [ASSUMED] wave heel + yaw losses (constant)
        wiring_eff=0.97,                 # [ASSUMED]
        mppt_eff=0.95,                   # [SHEET]
        mppt_min_w=0.05,                 # [ASSUMED]
        scale=1.0,                       # global PV multiplier (sensitivity / Monte Carlo)
        panel_failures=[],               # e.g. [dict(panel=0, day=90, factor=0.0)]  factor 0 = open circuit
        icing=dict(                      # [ASSUMED] accumulation model; disabled in baseline
            enabled=False, t_below_c=-2.0, rh_min_pct=90.0, spray_ws_ms=10.0,
            grow_per_h=0.02, shed_per_h=0.05, melt_above_c=0.5, sun_shed_poa_wm2=300.0, max_loss=0.7,
        ),
    ),

    # ---------------- Battery ----------------
    batt=dict(
        nominal_wh=192.0,                # [SHEET] 12.8 V x 15 Ah
        n_cells_series=4,                # [SHEET] 12.8 V LiFePO4
        charge_eff=0.95, discharge_eff=0.95,   # [SHEET] non-ohmic efficiency
        ohmic_separate=True,             # True: I^2R is added on top of the efficiencies above
        self_discharge_per_month=0.03,   # [SHEET]
        soc0=0.95,                       # [ASSUMED] nominal SOC at deployment
        min_soc_policy=0.20,             # [SHEET] floor below which the controller forces DEEP_SLEEP
        cutoff_soc=0.03,                 # [ASSUMED] BMS under-voltage trip (available SOC)
        restart_soc=0.10,                # [ASSUMED]
        charge_min_temp_c=0.0,           # [ASSUMED] BMS blocks LiFePO4 charging below 0 degC
        allow_cold_charge=False,
        op_temp_range=(-20.0, 60.0),     # [SHEET]
        enforce_op_temp=True,            # BMS blocks discharge outside op_temp_range
        cap_vs_temp=([-20, -10, 0, 10, 25, 40], [70, 80, 90, 95, 100, 100]),   # [SHEET] % capacity
        r_int_ohm=0.06,                  # [ASSUMED] pack R_int at 25 degC
        r_int_vs_temp=([-20, -10, 0, 10, 25], [3.0, 2.2, 1.6, 1.25, 1.0]),    # [ASSUMED] multiplier on R_int
        max_charge_a=30.0,               # [SHEET]
        max_discharge_a=15.0,            # [ASSUMED] BMS continuous discharge limit
        fade_per_year=0.02,              # [ASSUMED] calendar + cycle capacity fade
        ocv_soc=[0, .05, .10, .20, .30, .40, .50, .60, .70, .80, .90, .95, 1.0],
        ocv_cell=[2.90, 3.10, 3.20, 3.24, 3.26, 3.28, 3.29, 3.31, 3.33, 3.35, 3.40, 3.45, 3.55],  # [ASSUMED]
    ),

    # ---------------- Power electronics ----------------
    pe=dict(
        eta_5v=0.9895, eta_3v3=0.9299,   # [ASSUMED]
        quiescent_w=0.004,               # [ASSUMED]
    ),

    # ---------------- Thermal (2 nodes: enclosure + battery) ----------------
    thermal=dict(
        hdpe_t_m=0.008, k_hdpe=0.45,     # [SHEET]
        gap_t_m=0.010, k_air=0.026,      # [SHEET]
        xps_t_m=0.050, k_xps=0.032,      # [SHEET]
        pc_t_m=0.005, k_pc=0.20,         # [SHEET thickness] k_PC [ASSUMED]
        area_m2=0.50,                    # [ASSUMED]
        frac_water=0.60,                 # [ASSUMED]
        bridge_ua=0.03,                  # [ASSUMED] W/K
        h_water=500.0,
        wind_factor=0.6,
        heat_capacity_enc_j_k=1500.0,    # [ASSUMED] electronics + box + air
        heat_capacity_batt_j_k=2000.0,   # [ASSUMED] ~2 kg LiFePO4 x ~1000 J/kgK
        ua_batt_enc_w_k=0.8,             # [ASSUMED] battery <-> enclosure coupling
        heater_to_batt_frac=0.7,         # [ASSUMED] share of PTC heat delivered directly to the pack
        inside_groups=["mcu", "gnss", "comm", "sd", "imu", "internal"],
        t_enc0=2.0, t_batt0=2.0,
        sst_mean=-0.7, sst_amp=1.1, sst_peak_doy=50, sst_floor=-1.8,   # [ASSUMED] used only without --sst
    ),

    # ---------------- Heater (PTC, 5 V) ----------------
    heater=dict(
        policy="adaptive",               # "adaptive" (uses T_batt) | "thermostat" (uses T_enc, spec > 8 degC)
        power_w=6.0,                     # [SHEET]
        protect_on_c=-12.0, protect_off_c=-8.0, protect_min_soc=0.15,
        assist_on_c=0.5, assist_off_c=3.0, assist_max_soc=0.97,
        assist_pv_on_frac=0.6, assist_pv_hold_frac=0.3,
        thermo_on_c=8.0, thermo_off_c=10.0, thermo_min_soc=0.05,
    ),

    # ---------------- Mode controller ----------------
    modes=dict(
        deep_enter_soc=0.20, deep_exit_soc=0.30,
        low_enter_soc=0.50, low_exit_soc=0.65,
        low_enter_harvest_wh_day=5.0, low_exit_harvest_wh_day=10.0,
        low_harvest_soc_gate=1.10,
        intervals_h={
            "sensors": {3: 0.25, 2: 2.0, 1: 12.0},
            "imu":     {3: 0.50, 2: 0.0, 1: 0.0},
            "gnss":    {3: 1.0,  2: 6.0, 1: 24.0},
            "comm":    {3: 6.0,  2: 24.0, 1: 48.0},
        },
        alert_min_gap_h=12.0,
    ),

    # ---------------- Loads ----------------
    sensors=[
        ("DS18B20_water",   "ctd",      3.3,  1.5,  0.0,  0.75, 1, 1, (3, 2)),
        ("TDS_conductivity","ctd",      5.0,  6.0,  3.0,  1.0,  1, 2, (3, 2)),
        ("depth_pressure",  "ctd",      3.3,  1.0,  0.02, 0.2,  1, 1, (3, 2)),
        ("pH",              "ph",       5.0, 10.0,  0.0, 60.0,  1, 1, (3,)),
        ("turbidity",       "turb",     5.0, 30.0,  1.0,  0.5,  1, 1, (3,)),
        ("DO_optical",      "do",       3.3, 15.0,  2.0,  8.0,  1, 1, (3,)),
        ("SHT20_air",       "atmos",    3.3,  0.33, 0.0,  0.1,  1, 2, (3, 2, 1)),
        ("BMP280",          "atmos",    3.3,  1.1,  0.002,0.01, 1, 2, (3, 2, 1)),
        ("photodiode_irr",  "atmos",    3.3,  0.1,  0.0,  0.05, 1, 1, (3, 2)),
        ("DHT22_internal",  "internal", 3.3,  1.5,  0.0,  2.0,  1, 2, (3, 2)),
        ("DS18B20_batt_elec","internal",3.3,  1.5,  0.0,  0.75, 2, 1, (3, 2, 1)),
        ("chlorophyll_a",   "chl",      5.0, 30.0,  5.0,  5.0,  1, 1, ()),
    ],
    mcu=dict(ua_per_mhz=84.0, f_mhz=24.0, v=3.3, sleep_ua=2.05,
             awake_base_s=2.0, awake_per_sensor_s=0.1, imu_duty=0.2),
    imu=dict(v=3.3, i_ma=3.4, burst_s=60.0, odr_hz=10.0, bytes_per_sample=100),
    gnss=dict(
        model="neo6m",
        neo6m=dict(v=3.3, i_acq_ma=47.0, i_trk_ma=37.0, cold_s=27.0, hot_s=1.0, backup_ua=22.0),
        l89ha=dict(v=3.3, i_acq_ma=32.0, i_trk_ma=32.0, cold_s=13.0, hot_s=1.0, backup_ua=51.0),
        track_s=5.0, ephemeris_valid_h=4.0,
    ),
    comm=dict(
        v=3.3, tx_ma=100.0, tx_s=105.0, rx_ma=20.0, rx_s=15.0, payload_bytes=240,   # [ASSUMED] per packet
        sleep_ua=0.0,
        max_packets_per_window=4,       # [ASSUMED] backlog drain per successful window
        link_success_prob=1.0,          # [ASSUMED] per attempt
        retry_after_h=1.0, max_retries=2,
        outages=[],                     # list of (start_day, end_day) since simulation start
        backlog_max_packets=2000,
        rng_seed=12345,
    ),
    sd=dict(v=3.3, write_ma=75.0, sleep_ma=22.5, power_gated=True,
            on_s_base=0.5, throughput_Bps=1.0e6, capacity_gb=32.0, bytes_per_meas=100,
            full_policy="overwrite_oldest"),   # "overwrite_oldest" | "stop"
    sleep_ua_always_on_3v3=dict(rtc_in_mcu=0.0, imu=6.0, ds18b20=3.0, sht20=0.4, bmp280=0.1, dht22=40.0),
    sensor_power_gating=True,
    output=dict(write_step_log=True),
)

MODE_NAMES = {0: "OFF_BROWNOUT", 1: "DEEP_SLEEP", 2: "LOW_ENERGY_WINTER", 3: "NORMAL_OBSERVATION"}
GROUPS = ["mcu", "imu", "gnss", "comm", "sd", "ctd", "ph", "turb", "do", "atmos", "internal", "chl"]
MAX_TASKS_PER_STEP = 20

L_COLS = (["pv_avail_wh", "pv_direct_wh", "accepted_wh", "charged_wh", "curtailed_wh", "load_bat_wh",
           "batt_discharge_wh", "batt_supplied_wh", "selfdis_wh", "fade_clip_wh", "ohmic_wh", "batt_loss_wh",
           "mppt_loss_wh", "conv_loss_wh", "quiescent_wh", "heater_wh_rail", "unmet_wh"]
          + ["e_" + g for g in GROUPS]
          + ["e_base_sleep", "pkt_sent", "pkt_failed", "pkt_dropped", "sd_dropped_bytes",
             "heat_enc_w", "heat_batt_w", "ua_w", "ua_a"])
SUM_EXCLUDE = {"heat_enc_w", "heat_batt_w", "ua_w", "ua_a"}
S_COLS = ["E", "usable_wh", "cap_wh", "soc_pct", "soc_nom_pct", "t_enc", "t_batt", "mode", "heater_on",
          "charge_ok", "discharge_ok", "volt", "curr", "r_int_ohm", "sst", "storage_mb", "backlog_pkts",
          "n_sensor", "n_imu", "n_gnss", "n_comm"]


# ----------------------------------------------------------------------------
# CONFIG VALIDATION
# ----------------------------------------------------------------------------
def validate_config(cfg):
    """Raise ConfigError on inconsistent configuration; return a list of warnings."""
    warns = []

    def need(cond, msg):
        if not cond:
            raise ConfigError(f"[{cfg.get('name', '?')}] {msg}")

    pv, B, MD, HT, TH = cfg["pv"], cfg["batt"], cfg["modes"], cfg["heater"], cfg["thermal"]
    sd, cm = cfg["sd"], cfg["comm"]
    need(cfg["dt_min"] > 0 and 60 % cfg["dt_min"] == 0, "dt_min must be a positive divisor of 60")
    need(cfg["n_days"] >= 1, "n_days must be >= 1")
    need(cfg.get("load_margin", 1.0) > 0, "load_margin must be > 0")
    need(pv["n_panels"] >= 1, "pv.n_panels must be >= 1")
    need(pv["wiring"] in ("series_pairs", "parallel"), "pv.wiring must be 'series_pairs' or 'parallel'")
    if pv["wiring"] == "series_pairs":
        need(pv["n_panels"] % 2 == 0, "series_pairs wiring needs an even panel count")
    for k in ("soiling", "motion", "wiring_eff", "mppt_eff"):
        need(0 < pv[k] <= 1, f"pv.{k} must be in (0, 1]")
    need(pv["scale"] > 0, "pv.scale must be > 0")
    need(pv["p_stc_source"] in ("vmp_imp", "area_eff"), "pv.p_stc_source must be 'vmp_imp' or 'area_eff'")
    p1, p2 = pv["v_mp"] * pv["i_mp"], 1000.0 * pv["area_m2"] * pv["eff"]
    need(p1 > 0 and p2 > 0, "PV STC ratings must be positive")
    if abs(p1 - p2) / max(p1, p2) > pv["p_stc_tolerance"]:
        warns.append(f"PV rating mismatch: Vmp*Imp = {p1:.2f} W vs 1000*area*eff = {p2:.2f} W; "
                     f"using {pv['p_stc_source']}")
    for f in pv["panel_failures"]:
        need(0 <= f["panel"] < pv["n_panels"], f"panel_failures: panel index {f['panel']} out of range")
        need(0 <= f.get("factor", 0.0) <= 1, "panel_failures: factor must be in [0, 1]")
    ic = pv["icing"]
    need(0 <= ic["max_loss"] <= 1, "pv.icing.max_loss must be in [0, 1]")

    need(B["nominal_wh"] > 0, "batt.nominal_wh must be > 0")
    for k in ("charge_eff", "discharge_eff"):
        need(0 < B[k] <= 1, f"batt.{k} must be in (0, 1]")
    need(0 <= B["self_discharge_per_month"] < 1, "batt.self_discharge_per_month must be in [0, 1)")
    need(0 <= B["soc0"] <= 1, "batt.soc0 must be in [0, 1]")
    need(0 <= B["fade_per_year"] < 1, "batt.fade_per_year must be in [0, 1)")
    need(B["max_charge_a"] > 0 and B["max_discharge_a"] > 0, "battery current limits must be > 0")
    need(B["r_int_ohm"] >= 0, "batt.r_int_ohm must be >= 0")
    need(np.all(np.diff(B["cap_vs_temp"][0]) > 0), "batt.cap_vs_temp temperatures must increase")
    need(len(B["cap_vs_temp"][0]) == len(B["cap_vs_temp"][1]), "batt.cap_vs_temp arrays differ in length")
    need(min(B["cap_vs_temp"][1]) > 0, "batt.cap_vs_temp capacities must be > 0")
    need(np.all(np.diff(B["r_int_vs_temp"][0]) > 0), "batt.r_int_vs_temp temperatures must increase")
    need(len(B["ocv_soc"]) == len(B["ocv_cell"]) and np.all(np.diff(B["ocv_soc"]) > 0),
         "batt.ocv_soc must increase and match ocv_cell in length")
    need(B["op_temp_range"][0] < B["op_temp_range"][1], "batt.op_temp_range must be (min, max)")

    deep_enter = max(MD["deep_enter_soc"], B["min_soc_policy"])
    if B["min_soc_policy"] > MD["deep_enter_soc"]:
        warns.append(f"min_soc_policy {B['min_soc_policy']} > deep_enter_soc {MD['deep_enter_soc']}; "
                     f"DEEP_SLEEP will start at {deep_enter}")
    need(B["cutoff_soc"] < B["restart_soc"] < deep_enter < MD["deep_exit_soc"]
         <= MD["low_enter_soc"] < MD["low_exit_soc"],
         "SOC thresholds must satisfy cutoff < restart < deep_enter < deep_exit <= low_enter < low_exit")
    for k, iv in MD["intervals_h"].items():
        for mode in (1, 2, 3):
            need(iv.get(mode, 0.0) >= 0, f"modes.intervals_h.{k}[{mode}] must be >= 0")

    need(HT["policy"] in ("adaptive", "thermostat"), "heater.policy must be 'adaptive' or 'thermostat'")
    need(HT["protect_on_c"] < HT["protect_off_c"], "heater protect_on_c must be < protect_off_c")
    need(HT["assist_on_c"] < HT["assist_off_c"], "heater assist_on_c must be < assist_off_c")
    need(HT["thermo_on_c"] < HT["thermo_off_c"], "heater thermo_on_c must be < thermo_off_c")
    need(HT["power_w"] >= 0, "heater.power_w must be >= 0")

    need(0 <= TH["frac_water"] <= 1, "thermal.frac_water must be in [0, 1]")
    need(0 <= TH["heater_to_batt_frac"] <= 1, "thermal.heater_to_batt_frac must be in [0, 1]")
    need(TH["heat_capacity_enc_j_k"] > 0 and TH["heat_capacity_batt_j_k"] > 0, "heat capacities must be > 0")
    need(TH["ua_batt_enc_w_k"] > 0, "thermal.ua_batt_enc_w_k must be > 0")

    need(sd["full_policy"] in ("stop", "overwrite_oldest"), "sd.full_policy must be 'stop' or 'overwrite_oldest'")
    need(sd["capacity_gb"] > 0, "sd.capacity_gb must be > 0")
    need(0 <= cm["link_success_prob"] <= 1, "comm.link_success_prob must be in [0, 1]")
    need(cm["max_packets_per_window"] >= 1 and cm["backlog_max_packets"] >= 1, "comm packet limits must be >= 1")
    need(cm["max_retries"] >= 0 and cm["retry_after_h"] > 0, "comm retry settings invalid")
    for o in cm["outages"]:
        need(len(o) == 2 and o[0] < o[1], f"comm.outages entry {o} must be (start_day, end_day)")
    need(cfg["gnss"]["model"] in cfg["gnss"], f"gnss.model '{cfg['gnss']['model']}' has no parameter block")
    return warns


# ----------------------------------------------------------------------------
# WEATHER + EXTERNAL SERIES
# ----------------------------------------------------------------------------
WX_TIME_COLS = ["YEAR", "MO", "DY", "HR"]
WX_DATA_COLS = ["ALLSKY_SFC_SW_DWN", "T2M", "RH2M", "WS10M"]
WX_LIMITS = {"ALLSKY_SFC_SW_DWN": (-5.0, 1500.0), "T2M": (-90.0, 60.0), "RH2M": (0.0, 105.0), "WS10M": (0.0, 80.0)}


def _max_run(mask):
    best = cur = 0
    for v in mask:
        cur = cur + 1 if v else 0
        if cur > best:
            best = cur
    return best


def load_weather(path, n_days, lon, max_gap_h=6.0, allow_gaps=False, allow_short=False, time_basis="auto"):
    """Load and validate a NASA POWER hourly CSV. Returns (DataFrame, qc dict)."""
    if not os.path.isfile(path):
        raise WeatherError(f"weather file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as exc:
        raise WeatherError(f"cannot read weather file: {exc}") from exc
    hdr = next((i for i, l in enumerate(lines) if l.strip().upper().startswith("YEAR")), None)
    if hdr is None:
        raise WeatherError("no data header line starting with 'YEAR' found - expected a NASA POWER hourly CSV "
                           "with columns YEAR,MO,DY,HR,...")
    meta = "\n".join(lines[:hdr]).upper()
    basis = str(time_basis).upper()
    if basis == "AUTO":
        if "UTC" in meta and "LST" not in meta:
            basis = "UTC"
        elif "LST" in meta:
            basis = "LST"
        else:
            basis = "LST"
            log.warning("weather time basis not stated in header; assuming local solar time (LST)")
    if basis not in ("LST", "UTC"):
        raise WeatherError(f"unknown time basis '{time_basis}'")

    try:
        df = pd.read_csv(path, skiprows=hdr, skipinitialspace=True)
    except Exception as exc:
        raise WeatherError(f"cannot parse weather table: {exc}") from exc
    df.columns = [str(c).strip().upper() for c in df.columns]
    missing = [c for c in WX_TIME_COLS + WX_DATA_COLS if c not in df.columns]
    if missing:
        raise WeatherError(f"weather file is missing required columns: {missing}")
    df = df[WX_TIME_COLS + WX_DATA_COLS].apply(pd.to_numeric, errors="coerce")
    bad_time = df[WX_TIME_COLS].isna().any(axis=1)
    if bad_time.any():
        raise WeatherError(f"{int(bad_time.sum())} rows have non-numeric YEAR/MO/DY/HR "
                           f"(first at data row {int(np.argmax(bad_time.values))})")
    if len(df) < 24:
        raise WeatherError("weather file has fewer than 24 hourly rows")
    try:
        dt = pd.to_datetime(pd.DataFrame(dict(year=df.YEAR.astype(int), month=df.MO.astype(int),
                                              day=df.DY.astype(int), hour=df.HR.astype(int))))
    except Exception as exc:
        raise WeatherError(f"invalid date/time values: {exc}") from exc

    n_rows = len(df)
    df.index = pd.DatetimeIndex(dt)
    dup = df.index.duplicated()
    if dup.any():
        log.warning("weather: %d duplicate timestamps dropped", int(dup.sum()))
        df = df[~dup]
    df = df.sort_index()
    full = pd.date_range(df.index[0], df.index[-1], freq="h", name="dt")
    n_missing_ts = len(full) - len(df)
    if n_missing_ts:
        log.warning("weather: %d missing hourly timestamps inserted as gaps", n_missing_ts)
    df = df.reindex(full)

    out_of_range = {}
    for c in WX_DATA_COLS:
        v = df[c]
        sentinel = v <= -990
        lo, hi = WX_LIMITS[c]
        oor = (~sentinel) & v.notna() & ((v < lo) | (v > hi))
        out_of_range[c] = int(oor.sum())
        df.loc[sentinel | oor, c] = np.nan
    miss = df[WX_DATA_COLS].isna()
    max_gap = {c: int(_max_run(miss[c].values)) for c in WX_DATA_COLS}
    worst = max(max_gap.values())
    if worst > max_gap_h:
        msg = f"longest weather data gap is {worst} h (limit {max_gap_h} h): {max_gap}"
        if not allow_gaps:
            raise WeatherError(msg + " - use --allow-gaps to interpolate anyway")
        log.warning(msg)
    flag = miss.any(axis=1).values
    df[WX_DATA_COLS] = df[WX_DATA_COLS].interpolate(method="linear", limit_area="inside").bfill().ffill()
    if df[WX_DATA_COLS].isna().any().any():
        raise WeatherError("a weather variable has no valid values at all")
    df["ALLSKY_SFC_SW_DWN"] = df["ALLSKY_SFC_SW_DWN"].clip(lower=0.0)
    df["RH2M"] = df["RH2M"].clip(0.0, 100.0)
    df["WS10M"] = df["WS10M"].clip(lower=0.0)

    if basis == "UTC":
        # Local solar time = UTC + lon/15 h. Re-sample each series onto the LST hour grid.
        # The first ~lon/15 hours are held at the edge value.
        shift = lon / 15.0
        x = np.arange(len(df), dtype=float)
        for c in WX_DATA_COLS:
            df[c] = np.interp(x - shift, x, df[c].values.astype(float))
        flag = np.interp(x - shift, x, flag.astype(float)) > 0.0
        log.info("weather converted from UTC to local solar time (shift %.2f h)", shift)
    df["wx_interpolated"] = flag

    need = int(n_days * 24)
    if len(df) < need:
        msg = f"weather covers {len(df) / 24:.1f} days but n_days = {n_days}"
        if not allow_short:
            raise WeatherError(msg + " - supply a full-length file or use --allow-short")
        log.warning(msg + "; the simulation will be shorter")
    df = df.iloc[:need]
    out = df.reset_index()
    out["YEAR"], out["MO"] = out["dt"].dt.year, out["dt"].dt.month
    out["DY"], out["HR"] = out["dt"].dt.day, out["dt"].dt.hour
    qc = dict(file=os.path.abspath(path), time_basis=basis, rows_in_file=int(n_rows),
              duplicate_rows=int(dup.sum()), missing_timestamps=int(n_missing_ts),
              out_of_range_values=out_of_range, max_gap_h=max_gap,
              hours_interpolated=int(out["wx_interpolated"].sum()),
              start=str(out["dt"].iloc[0]), end=str(out["dt"].iloc[-1]), hours_used=int(len(out)))
    return out, qc


def load_series_csv(path, name, lo, hi):
    """Generic time series (columns: time,value) for SST or albedo."""
    try:
        df = pd.read_csv(path)
    except Exception as exc:
        raise WeatherError(f"{name}: cannot read {path}: {exc}") from exc
    cols = {str(c).strip().lower(): c for c in df.columns}
    if "time" not in cols or "value" not in cols:
        raise WeatherError(f"{name}: {path} needs columns 'time' and 'value'")
    t = pd.to_datetime(df[cols["time"]], errors="coerce")
    v = pd.to_numeric(df[cols["value"]], errors="coerce")
    ok = t.notna() & v.notna()
    if ok.sum() < 2:
        raise WeatherError(f"{name}: fewer than 2 valid rows in {path}")
    t, v = t[ok], v[ok]
    bad = (v < lo) | (v > hi)
    if bad.any():
        raise WeatherError(f"{name}: {int(bad.sum())} values outside [{lo}, {hi}] in {path}")
    s = pd.Series(v.values.astype(float), index=pd.DatetimeIndex(t.values)).sort_index()
    return s[~s.index.duplicated()]


def series_on_grid(s, times):
    x = s.index.values.astype("datetime64[s]").astype(np.int64).astype(float)
    xt = pd.DatetimeIndex(times).values.astype("datetime64[s]").astype(np.int64).astype(float)
    if xt[0] < x[0] or xt[-1] > x[-1]:
        log.warning("external series does not cover the full simulation period; edge values are held")
    return np.interp(xt, x, s.values)


# ----------------------------------------------------------------------------
# SOLAR
# ----------------------------------------------------------------------------
def solar_position(lat_deg, t_h):
    """t_h = hours since Jan 1 00:00 local solar time of the data year."""
    lat = np.radians(lat_deg)
    G = 2 * np.pi * (t_h / 24.0) / 365.0
    dec = (0.006918 - 0.399912 * np.cos(G) + 0.070257 * np.sin(G) - 0.006758 * np.cos(2 * G)
           + 0.000907 * np.sin(2 * G) - 0.002697 * np.cos(3 * G) + 0.00148 * np.sin(3 * G))
    E0 = 1.000110 + 0.034221 * np.cos(G) + 0.001280 * np.sin(G) + 0.000719 * np.cos(2 * G) + 0.000077 * np.sin(2 * G)
    h = np.radians(15.0 * ((t_h % 24.0) - 12.0))
    cz = np.sin(lat) * np.sin(dec) + np.cos(lat) * np.cos(dec) * np.cos(h)
    A_s = np.arctan2(np.sin(h), np.cos(h) * np.sin(lat) - np.tan(dec) * np.cos(lat))
    az = (A_s + np.pi) % (2 * np.pi)
    return cz, az, dec, E0


def erbs_kd(kt):
    kt = np.clip(kt, 0, 1)
    return np.where(kt <= 0.22, 1 - 0.09 * kt,
           np.where(kt <= 0.80, 0.9511 - 0.1604 * kt + 4.388 * kt**2 - 16.638 * kt**3 + 12.336 * kt**4, 0.165))


def build_pv(cfg, wx, albedo_series=None):
    """Pre-compute solar geometry and PV potential (independent of the controller)."""
    pv = cfg["pv"]
    dt_h = cfg["dt_min"] / 60.0
    spp = int(round(1.0 / dt_h))
    n_hours = len(wx)
    n = n_hours * spp
    t_mid = (np.arange(n) + 0.5) * dt_h
    hour_idx = np.minimum((t_mid // 1).astype(int), n_hours - 1)
    t0 = pd.Timestamp(wx["dt"].iloc[0])
    t_off = (t0 - pd.Timestamp(year=t0.year, month=1, day=1)).total_seconds() / 3600.0
    times_mid = t0 + pd.to_timedelta(t_mid, unit="h")

    cz, az_s, dec, E0 = solar_position(cfg["site"]["lat"], t_mid + t_off)
    elev = np.degrees(np.arcsin(np.clip(cz, -1, 1)))
    etr_h = 1361.0 * E0 * np.clip(cz, 0, None)

    # Distribute hourly GHI over sub-steps in proportion to ETR (energy preserving).
    # Sub-hourly variability (passing cloud) is NOT represented.
    ghi_h = wx["ALLSKY_SFC_SW_DWN"].values.astype(float)
    mean_etr_rep = np.repeat(etr_h.reshape(n_hours, spp).mean(axis=1), spp)
    ghi_hr_rep = np.repeat(ghi_h, spp)
    with np.errstate(divide="ignore", invalid="ignore"):
        ghi = np.where(mean_etr_rep > 1e-3, ghi_hr_rep * etr_h / np.maximum(mean_etr_rep, 1e-3), ghi_hr_rep)
    kt = np.where(etr_h > 1.0, ghi / np.maximum(etr_h, 1.0), 0.0)
    kd = erbs_kd(kt)
    w = np.clip(elev / 8.0, 0, 1)
    kd = 1 - w * (1 - kd)
    dhi = kd * ghi
    beam_h = ghi - dhi
    cz_floor = np.maximum(cz, np.sin(np.radians(2.0)))

    if albedo_series is not None:
        albedo = np.clip(series_on_grid(albedo_series, times_mid), 0.0, 1.0)
    else:
        month = pd.DatetimeIndex(wx["dt"]).month.values[hour_idx]
        albedo = np.array(pv["albedo_monthly"])[month - 1]
    xh = np.arange(n_hours) + 0.5
    t_air = np.interp(t_mid, xh, wx["T2M"].values)
    rh = np.interp(t_mid, xh, wx["RH2M"].values)
    ws = np.interp(t_mid, xh, wx["WS10M"].values)

    beta = np.radians(pv["tilt_deg"])
    npan = pv["n_panels"]
    az_p = np.radians(pv["heading_deg"] + np.arange(npan) * 360.0 / npan)
    cos_th = (cz[:, None] * np.cos(beta)
              + np.sqrt(np.clip(1 - cz**2, 0, 1))[:, None] * np.sin(beta) * np.cos(az_s[:, None] - az_p[None, :]))
    cos_th = np.clip(cos_th, 0, 1)
    iam = np.clip(1 - pv["iam_b0"] * (1 / np.maximum(cos_th, 0.05) - 1), 0, 1)
    beam_poa = np.where(elev[:, None] > 0, beam_h[:, None] * cos_th / cz_floor[:, None], 0.0) * iam
    diff_poa = dhi[:, None] * (1 + np.cos(beta)) / 2
    refl_poa = (ghi * albedo)[:, None] * (1 - np.cos(beta)) / 2
    poa = np.clip(beam_poa + diff_poa + refl_poa, 0, 1400)

    p_stc = pv["v_mp"] * pv["i_mp"] if pv["p_stc_source"] == "vmp_imp" else 1000.0 * pv["area_m2"] * pv["eff"]
    tcell = t_air[:, None] + (pv["noct"] - 20.0) / 800.0 * poa
    tfac = 1 + pv["temp_coeff"] * (tcell - 25.0)
    lowlight = 1 - 0.04 * np.clip(1 - poa / 200.0, 0, 1)
    p_pan = p_stc * poa / 1000.0 * tfac * lowlight

    day_rel = t_mid / 24.0
    for f in pv["panel_failures"]:
        p_pan[day_rel >= f["day"], int(f["panel"])] *= f.get("factor", 0.0)

    if pv["wiring"] == "series_pairs":
        p_dc = (2 * p_pan.reshape(n, npan // 2, 2).min(axis=2)).sum(axis=1)
    else:
        p_dc = p_pan.sum(axis=1)

    # Icing: accumulates under freezing + humid/spray conditions; sheds with melt or strong sun.
    ice = np.zeros(n)
    ic = pv["icing"]
    if ic["enabled"]:
        poa_mean = poa.mean(axis=1)
        frac = 0.0
        for i in range(n):
            if t_air[i] < ic["t_below_c"] and (rh[i] >= ic["rh_min_pct"] or ws[i] >= ic["spray_ws_ms"]):
                frac += ic["grow_per_h"] * dt_h
            elif t_air[i] > ic["melt_above_c"]:
                frac -= 4.0 * ic["shed_per_h"] * dt_h
            elif poa_mean[i] > ic["sun_shed_poa_wm2"]:
                frac -= ic["shed_per_h"] * dt_h
            frac = min(max(frac, 0.0), 1.0)
            ice[i] = frac
        p_dc = p_dc * (1.0 - ic["max_loss"] * ice)

    p_net = p_dc * pv["soiling"] * pv["motion"] * pv["wiring_eff"] * pv["scale"]
    p_avail = np.where(p_net >= pv["mppt_min_w"], p_net * pv["mppt_eff"], 0.0)   # W at MPPT output

    wx_interp = (np.repeat(wx["wx_interpolated"].values.astype(float), spp)
                 if "wx_interpolated" in wx.columns else np.zeros(n))
    return dict(n=n, spp=spp, dt_h=dt_h, t_mid=t_mid, t_off=t_off, t0=t0, times=times_mid, hour_idx=hour_idx,
                elev=elev, az=np.degrees(az_s), ghi=ghi, dhi=dhi, poa=poa, p_pan=p_pan, p_dc_raw=p_dc,
                p_avail=p_avail, t_air=t_air, rh=rh, ws=ws, albedo=albedo, etr=etr_h, ice=ice,
                wx_interp=wx_interp, p_stc=p_stc)


# ----------------------------------------------------------------------------
# SIMULATION
# ----------------------------------------------------------------------------
def thermal_ua(cfg, wind_ms):
    th = cfg["thermal"]
    r_stack = (th["hdpe_t_m"] / th["k_hdpe"] + th["gap_t_m"] / th["k_air"]
               + th["xps_t_m"] / th["k_xps"] + th["pc_t_m"] / th["k_pc"])
    h_air = 5.7 + 3.8 * th["wind_factor"] * np.asarray(wind_ms, dtype=float)
    ua_w = th["area_m2"] * th["frac_water"] / (r_stack + 1 / th["h_water"]) + th["bridge_ua"] * th["frac_water"]
    ua_a = th["area_m2"] * (1 - th["frac_water"]) / (r_stack + 1 / h_air) + th["bridge_ua"] * (1 - th["frac_water"])
    return ua_w, ua_a, r_stack


def sensor_cycle(cfg, mode):
    groups, n_active, n_meas, e5 = {}, 0, 0, 0.0
    for (name, grp, V, I, warm, meas, cnt, ch, modes) in cfg["sensors"]:
        if mode not in modes:
            continue
        e = V * I / 1000.0 * (warm + meas) * cnt
        if V >= 4.5:
            e5 += e
        groups[grp] = groups.get(grp, 0.0) + e
        n_active += cnt
        n_meas += cnt * ch
    m = cfg["mcu"]
    awake = m["awake_base_s"] + m["awake_per_sensor_s"] * n_active
    e_mcu = m["v"] * (m["ua_per_mhz"] * m["f_mhz"] * 1e-6) * awake
    return groups, e_mcu, n_meas, e5


def run(cfg, wx, pvd, sst_series=None):
    B, PE, TH, HT, MD = cfg["batt"], cfg["pe"], cfg["thermal"], cfg["heater"], cfg["modes"]
    cm, sd, imu, m = cfg["comm"], cfg["sd"], cfg["imu"], cfg["mcu"]
    gcfg = cfg["gnss"]
    gm = gcfg[gcfg["model"]]
    dt_h, n, spp = pvd["dt_h"], pvd["n"], pvd["spp"]
    dt_s = dt_h * 3600.0
    t0 = pvd["t0"]
    Cn0 = B["nominal_wh"]
    cap_T, cap_pct = B["cap_vs_temp"]
    r_T, r_mult = B["r_int_vs_temp"]

    def f_cap(T):
        return float(np.interp(T, cap_T, cap_pct)) / 100.0

    def r_int(T):
        return B["r_int_ohm"] * float(np.interp(T, r_T, r_mult))

    def v_ocv(frac):
        return B["n_cells_series"] * float(np.interp(min(max(frac, 0.0), 1.0), B["ocv_soc"], B["ocv_cell"]))

    def cap_at(day):
        return Cn0 * max(0.0, 1.0 - B["fade_per_year"] * day / 365.0)

    lam = -math.log(1 - B["self_discharge_per_month"]) / (30 * 24)
    deep_enter = max(MD["deep_enter_soc"], B["min_soc_policy"])
    op_min, op_max = B["op_temp_range"]
    rng = np.random.default_rng(cm["rng_seed"])
    cap_bytes = sd["capacity_gb"] * 1e9
    frac_hb = TH["heater_to_batt_frac"]
    Ce, Cb, K = TH["heat_capacity_enc_j_k"], TH["heat_capacity_batt_j_k"], TH["ua_batt_enc_w_k"]
    mppt_eff = cfg["pv"]["mppt_eff"]
    mg = cfg.get("load_margin", 1.0)

    if sst_series is not None:
        sst = series_on_grid(sst_series, pvd["times"])
    else:
        doy = (pvd["t_mid"] + pvd["t_off"]) / 24.0 + 1
        sst = np.clip(TH["sst_mean"] + TH["sst_amp"] * np.cos(2 * np.pi * (doy - TH["sst_peak_doy"]) / 365.0),
                      TH["sst_floor"], 5.0)
    ua_w_c, ua_a_arr, _ = thermal_ua(cfg, pvd["ws"])
    ua_w_c = float(ua_w_c)
    ua_a_arr = np.broadcast_to(ua_a_arr, (n,))

    mcu_run_w = m["v"] * m["ua_per_mhz"] * m["f_mhz"] * 1e-6
    base_ua = m["sleep_ua"] + gm["backup_ua"] + cm["sleep_ua"]
    if not cfg["sensor_power_gating"]:
        base_ua += sum(cfg["sleep_ua_always_on_3v3"].values())
    base_w_rail = base_ua * 1e-6 * 3.3
    if not sd["power_gated"]:
        base_w_rail += sd["sleep_ma"] * 1e-3 * sd["v"]
    sc_cache = {md: sensor_cycle(cfg, md) for md in (1, 2, 3)}

    def sd_energy(byt):
        return sd["v"] * sd["write_ma"] * 1e-3 * (sd["on_s_base"] + byt / sd["throughput_Bps"])

    # ---- state ----
    E = B["soc0"] * Cn0
    Te, Tb = TH["t_enc0"], TH["t_batt0"]
    feq0 = f_cap(Tb)
    soc_init = max(0.0, E - Cn0 * (1 - feq0)) / (Cn0 * feq0)
    mode = 3 if soc_init > MD["low_enter_soc"] else 2
    heater_on, heater_reason = False, ""
    next_due = dict(sensors=0.0, imu=0.05, gnss=0.0, comm=1.0)
    last_fix, last_alert = -1e9, -1e9
    prev_charge_ok = True
    retry_at, retries_left = math.inf, 0
    force_brownout, brownout_cause = False, ""
    flags = dict(temp=False, lock=False, overcurrent=False)
    st = dict(storage=0.0, sd_dropped=0.0, sd_overwritten=0.0, sd_full=False,
              backlog=0, alert_q=0, pending_alert=False, sent=0, failed=0, dropped=0, overflow=False, skipped=0)
    harvest_hist = deque(maxlen=72)
    hour_acc = 0.0
    events, tasks = [], []

    def ts(t_h):
        return (t0 + pd.Timedelta(hours=float(t_h))).strftime("%Y-%m-%d %H:%M")

    def ev(t_h, kind, detail, **kw):
        events.append(dict(time=ts(t_h), t_hours=round(float(t_h), 3), event=kind, detail=detail, **kw))

    def sd_write(t_h, byt):
        """Store bytes; returns True if a physical write happened (SD energy spent)."""
        if st["storage"] + byt <= cap_bytes:
            st["storage"] += byt
            return True
        if not st["sd_full"]:
            st["sd_full"] = True
            ev(t_h, "SD_FULL", f"{sd['capacity_gb']} GB reached, policy={sd['full_policy']}")
        if sd["full_policy"] == "overwrite_oldest":
            st["sd_overwritten"] += byt
            st["storage"] = cap_bytes
            return True
        st["sd_dropped"] += byt
        return False

    def link_up(td):
        d_ = td / 24.0
        for (a, b) in cm["outages"]:
            if a <= d_ < b:
                return False
        return rng.random() < cm["link_success_prob"]

    def queue_packet(td, alert):
        st["backlog"] += 1
        if alert:
            st["alert_q"] += 1
        over = st["backlog"] - cm["backlog_max_packets"]
        if over > 0:
            st["backlog"] -= over
            st["dropped"] += over
            st["alert_q"] = min(st["alert_q"], st["backlog"])
            if not st["overflow"]:
                st["overflow"] = True
                ev(td, "COMM_BACKLOG_OVERFLOW", f"queue limit {cm['backlog_max_packets']} packets; oldest dropped")

    def comm_attempt(td):
        up = link_up(td)
        npk = max(1, min(st["backlog"], cm["max_packets_per_window"])) if up else 1
        e_c = cm["v"] * (cm["tx_ma"] * 1e-3 * cm["tx_s"] * npk + cm["rx_ma"] * 1e-3 * cm["rx_s"])
        e_mc = mcu_run_w * (cm["tx_s"] * npk + cm["rx_s"]) * 0.3
        if up:
            sent = min(st["backlog"], npk)
            st["alert_q"] -= min(st["alert_q"], sent)       # alerts drain first
            st["backlog"] -= sent
            st["sent"] += sent
        else:
            sent = 0
            st["failed"] += 1
        return up, sent, e_c, e_mc

    L = {c: np.zeros(n) for c in L_COLS}
    S = {c: np.zeros(n) for c in S_COLS}
    sum_charged = sum_draw = sum_selfd = sum_clip = 0.0
    E_start = E

    for i in range(n):
        t = i * dt_h
        t_end = t + dt_h
        Ta, Ts = float(pvd["t_air"][i]), float(sst[i])
        pv_w = float(pvd["p_avail"][i])
        pv_wh = pv_w * dt_h
        Cn = cap_at(t / 24.0)
        E_eff = Cn * f_cap(Tb)
        usable = max(0.0, E - (Cn - E_eff))
        soc = min(1.0, usable / E_eff) if E_eff > 0 else 0.0
        too_hot = Tb > op_max
        locked_dis = B["enforce_op_temp"] and (Tb < op_min or too_hot)
        if locked_dis != flags["lock"]:
            flags["lock"] = locked_dis
            ev(t, "BMS_TEMP_LOCKOUT" if locked_dis else "BMS_TEMP_LOCKOUT_CLEARED",
               f"T_batt={Tb:.1f} C, limits {B['op_temp_range']}")
        sent0, failed0, dropped0, sddrop0 = st["sent"], st["failed"], st["dropped"], st["sd_dropped"]

        # ---- 72 h rolling harvest potential ----
        hour_acc += pv_wh
        if (i + 1) % spp == 0:
            harvest_hist.append(hour_acc)
            hour_acc = 0.0
        harv_day = sum(harvest_hist) / 72.0 * 24.0 if len(harvest_hist) == 72 else 1e9

        # ---- mode controller ----
        old_mode = mode
        if mode == 0:
            if soc >= B["restart_soc"] and pv_w > 0 and not locked_dis:
                mode = 1
        elif force_brownout or soc <= B["cutoff_soc"]:
            mode = 0
            brownout_cause = brownout_cause or "SOC at cutoff"
        elif mode == 1:
            if soc >= MD["deep_exit_soc"]:
                mode = 2
        elif soc < deep_enter:
            mode = 1
        elif mode == 3:
            if soc < MD["low_enter_soc"] or (harv_day < MD["low_enter_harvest_wh_day"]
                                             and soc < MD["low_harvest_soc_gate"]):
                mode = 2
        elif mode == 2:
            if soc >= MD["low_exit_soc"] and harv_day >= MD["low_exit_harvest_wh_day"]:
                mode = 3
        force_brownout = False
        if mode != old_mode:
            ev(t, "MODE_CHANGE", f"{MODE_NAMES[old_mode]} -> {MODE_NAMES[mode]}", soc_pct=round(soc * 100, 1),
               t_enc=round(Te, 1), t_batt=round(Tb, 1),
               harvest_wh_day=round(harv_day, 1) if harv_day < 1e8 else None,
               cause=brownout_cause if mode == 0 else "")
            if mode == 0:
                last_fix = -1e9
                retry_at, retries_left = math.inf, 0
            else:
                brownout_cause = ""
                for k in next_due:
                    ivk = MD["intervals_h"][k].get(mode, 0.0)
                    next_due[k] = (t if old_mode == 0 else min(next_due[k], t + ivk)) if ivk > 0 else math.inf
            if mode == 1 and old_mode >= 2 and t - last_alert >= MD["alert_min_gap_h"]:
                next_due["comm"] = t
                last_alert = t
                st["pending_alert"] = True
                ev(t, "ALERT_QUEUED", "critical-SOC alert transmission scheduled")
            if old_mode == 0 and mode == 1:
                next_due["comm"] = t
                ev(t, "RECOVERY", "restarted after brown-out, status packet queued")

        # ---- scheduled tasks ----
        e5_task = 0.0
        e = {g: 0.0 for g in GROUPS}
        n_s = n_i = n_g = n_c = 0
        if mode > 0:
            iv = MD["intervals_h"]
            for k in next_due:
                ivk = iv[k].get(mode, 0.0)
                if ivk > 0 and next_due[k] > t + ivk:
                    next_due[k] = t + ivk
                if ivk == 0:
                    next_due[k] = math.inf

            guard = 0
            while next_due["sensors"] < t_end and guard < MAX_TASKS_PER_STEP:
                td = next_due["sensors"]
                guard += 1
                grp, e_mcu, n_meas, e5c = sc_cache[mode]
                byt = (n_meas + 1) * sd["bytes_per_meas"]
                e_sd = sd_energy(byt) if sd_write(td, byt) else 0.0
                e5_task += e5c
                for g_, v_ in grp.items():
                    e[g_] += v_
                e["mcu"] += e_mcu
                e["sd"] += e_sd
                n_s += 1
                tasks.append(dict(time=ts(td), t_hours=round(td, 3), task="SENSOR_CYCLE", mode=MODE_NAMES[mode],
                                  energy_J=round(sum(grp.values()) + e_mcu + e_sd, 3), bytes=byt,
                                  **{f"J_{k_}": round(v_, 3) for k_, v_ in grp.items()},
                                  J_mcu=round(e_mcu, 3), J_sd=round(e_sd, 3)))
                next_due["sensors"] = td + iv["sensors"][mode]

            guard = 0
            while next_due["imu"] < t_end and guard < MAX_TASKS_PER_STEP:
                td = next_due["imu"]
                guard += 1
                e_imu = imu["v"] * imu["i_ma"] * 1e-3 * imu["burst_s"]
                e_mcu = mcu_run_w * imu["burst_s"] * m["imu_duty"]
                byt = imu["burst_s"] * imu["odr_hz"] * imu["bytes_per_sample"]
                e_sd = sd_energy(byt) if sd_write(td, byt) else 0.0
                e["imu"] += e_imu
                e["mcu"] += e_mcu
                e["sd"] += e_sd
                n_i += 1
                tasks.append(dict(time=ts(td), t_hours=round(td, 3), task="IMU_BURST", mode=MODE_NAMES[mode],
                                  energy_J=round(e_imu + e_mcu + e_sd, 3), bytes=byt, J_imu=round(e_imu, 3),
                                  J_mcu=round(e_mcu, 3), J_sd=round(e_sd, 3)))
                next_due["imu"] = td + iv["imu"][mode]

            guard = 0
            while next_due["gnss"] < t_end and guard < MAX_TASKS_PER_STEP:
                td = next_due["gnss"]
                guard += 1
                cold = (td - last_fix) > gcfg["ephemeris_valid_h"]
                acq = gm["cold_s"] if cold else gm["hot_s"]
                e_g = gm["v"] * (gm["i_acq_ma"] * 1e-3 * acq + gm["i_trk_ma"] * 1e-3 * gcfg["track_s"])
                e_mcu = mcu_run_w * 2.0
                byt = sd["bytes_per_meas"]
                e_sd = sd_energy(byt) if sd_write(td, byt) else 0.0
                e["gnss"] += e_g
                e["mcu"] += e_mcu
                e["sd"] += e_sd
                n_g += 1
                last_fix = td
                tasks.append(dict(time=ts(td), t_hours=round(td, 3), task="GNSS_FIX", mode=MODE_NAMES[mode],
                                  energy_J=round(e_g + e_mcu + e_sd, 3), bytes=byt,
                                  start_type="cold" if cold else "hot", time_to_fix_s=acq,
                                  J_gnss=round(e_g, 3), J_mcu=round(e_mcu, 3), J_sd=round(e_sd, 3)))
                next_due["gnss"] = td + iv["gnss"][mode]

            guard = 0
            while next_due["comm"] < t_end and guard < MAX_TASKS_PER_STEP:
                td = next_due["comm"]
                guard += 1
                alert = st["pending_alert"]
                st["pending_alert"] = False
                queue_packet(td, alert)
                up, sent, e_c, e_mc = comm_attempt(td)
                e["comm"] += e_c
                e["mcu"] += e_mc
                n_c += 1
                if up:
                    retry_at, retries_left = math.inf, 0
                elif cm["max_retries"] > 0:
                    retry_at, retries_left = td + cm["retry_after_h"], cm["max_retries"]
                tasks.append(dict(time=ts(td), t_hours=round(td, 3), task="SAT_COMM_WINDOW", mode=MODE_NAMES[mode],
                                  energy_J=round(e_c + e_mc, 3), bytes=sent * cm["payload_bytes"], link_ok=up,
                                  packets_sent=sent, backlog_after=st["backlog"], alert=alert,
                                  J_comm=round(e_c, 3), J_mcu=round(e_mc, 3)))
                next_due["comm"] = td + iv["comm"][mode]

            guard = 0
            while retry_at < t_end and retries_left > 0 and guard < MAX_TASKS_PER_STEP:
                td = retry_at
                guard += 1
                retries_left -= 1
                up, sent, e_c, e_mc = comm_attempt(td)
                e["comm"] += e_c
                e["mcu"] += e_mc
                n_c += 1
                retry_at = math.inf if (up or retries_left == 0) else td + cm["retry_after_h"]
                tasks.append(dict(time=ts(td), t_hours=round(td, 3), task="SAT_COMM_RETRY", mode=MODE_NAMES[mode],
                                  energy_J=round(e_c + e_mc, 3), bytes=sent * cm["payload_bytes"], link_ok=up,
                                  packets_sent=sent, backlog_after=st["backlog"],
                                  J_comm=round(e_c, 3), J_mcu=round(e_mc, 3)))

            for k in next_due:                         # overrun protection: never silently drop tasks
                ivk = iv[k].get(mode, 0.0)
                if ivk > 0 and next_due[k] < t_end:
                    nskip = int(math.ceil((t_end - next_due[k]) / ivk))
                    next_due[k] += nskip * ivk
                    st["skipped"] += nskip
                    ev(t, "TASK_OVERRUN_SKIPPED", f"{k}: {nskip} execution(s) skipped")

        # ---- baseline sleep loads + margin ----
        e_base = base_w_rail * dt_s if mode > 0 else 0.0
        if mg != 1.0:
            for k_ in e:
                e[k_] *= mg
            e5_task *= mg
            e_base *= mg
        e5 = e5_task
        e33 = sum(e.values()) - e5 + e_base
        e_rail_total_J = e5 + e33

        # ---- heater decision (adaptive -> battery temperature) ----
        P_h = HT["power_w"]
        batt_h_w = P_h / PE["eta_5v"]
        load_bat_w_est = (e33 / PE["eta_3v3"] + e5 / PE["eta_5v"]) / dt_s + PE["quiescent_w"]
        surplus_w = pv_w - load_bat_w_est
        charge_ok = (B["allow_cold_charge"] or Tb >= B["charge_min_temp_c"]) and not too_hot
        was_on = heater_on
        if mode == 0:
            heater_on, reason = False, ""
        elif HT["policy"] == "thermostat":
            if heater_on:
                heater_on = Te < HT["thermo_off_c"] and soc > HT["thermo_min_soc"]
            else:
                heater_on = Te < HT["thermo_on_c"] and soc > HT["thermo_min_soc"]
            reason = "thermostat"
        else:
            if heater_reason == "protect" and heater_on:
                protect = Tb < HT["protect_off_c"] and soc > HT["protect_min_soc"]
            else:
                protect = Tb < HT["protect_on_c"] and soc > HT["protect_min_soc"]
            assist = False
            if mode >= 2:
                if heater_reason == "assist" and heater_on:
                    assist = (Tb < HT["assist_off_c"] and surplus_w >= HT["assist_pv_hold_frac"] * batt_h_w
                              and soc < HT["assist_max_soc"] + 0.02)
                else:
                    assist = (Tb < HT["assist_on_c"] and surplus_w >= HT["assist_pv_on_frac"] * batt_h_w
                              and soc < HT["assist_max_soc"])
            heater_on = protect or assist
            reason = "protect" if protect else ("assist" if assist else "")
        if heater_on != was_on:
            ev(t, "HEATER_ON" if heater_on else "HEATER_OFF", reason if heater_on else heater_reason,
               t_enc=round(Te, 2), t_batt=round(Tb, 2), soc_pct=round(soc * 100, 1), pv_w=round(pv_w, 2))
        heater_reason = reason
        e_heater_J = P_h * dt_s if heater_on else 0.0

        # ---- energy balance ----
        q_wh = PE["quiescent_w"] * dt_h if mode > 0 else 0.0
        load_bat_wh = (e33 / PE["eta_3v3"] + e5 / PE["eta_5v"] + e_heater_J / PE["eta_5v"]) / 3600.0 + q_wh
        rail_wh = (e_rail_total_J + e_heater_J) / 3600.0
        conv_loss_wh = load_bat_wh - rail_wh - q_wh
        direct = min(pv_wh, load_bat_wh)
        surplus = pv_wh - direct
        deficit = load_bat_wh - direct
        Voc = max(v_ocv(E / Cn if Cn > 0 else 0.0), 1.0)
        R = r_int(Tb)
        ohm_on = 1.0 if B["ohmic_separate"] else 0.0

        accepted = charged = ohm_c = 0.0
        if charge_ok and surplus > 0:
            room = max(0.0, Cn - E)
            accepted = min(surplus, room / B["charge_eff"])
            i_c = accepted / dt_h / Voc
            if i_c > B["max_charge_a"]:
                accepted = B["max_charge_a"] * Voc * dt_h
                i_c = B["max_charge_a"]
            ohm_c = ohm_on * i_c * i_c * R * dt_h
            charged = max(0.0, accepted * B["charge_eff"] - ohm_c)
        curtailed = surplus - accepted

        draw = supplied = ohm_d = unmet = 0.0
        cause = ""
        if deficit > 0:
            want = deficit
            i_d = want / dt_h / Voc
            if i_d > B["max_discharge_a"]:
                want = B["max_discharge_a"] * Voc * dt_h
                i_d = B["max_discharge_a"]
                cause = "discharge current limit"
                if not flags["overcurrent"]:
                    flags["overcurrent"] = True
                    ev(t, "BATTERY_OVERCURRENT", f"demand above {B['max_discharge_a']} A limit")
            else:
                flags["overcurrent"] = False
            ohm_d = ohm_on * i_d * i_d * R * dt_h
            need = want / B["discharge_eff"] + ohm_d
            avail = 0.0 if locked_dis else usable
            if need <= avail:
                draw, supplied = need, want
            else:
                draw = avail
                supplied = want * avail / need if need > 0 else 0.0
                ohm_d = ohm_d * avail / need if need > 0 else 0.0
                cause = cause or ("BMS temperature lockout" if locked_dis else "battery empty")
            unmet = deficit - supplied

        E_new = E + charged - draw
        selfd = max(E_new, 0.0) * (1 - math.exp(-lam * dt_h))
        E_new -= selfd
        Cn_next = cap_at(t_end / 24.0)
        fade_clip = 0.0
        if E_new > Cn_next:
            fade_clip = E_new - Cn_next
            E_new = Cn_next
        sum_charged += charged
        sum_draw += draw
        sum_selfd += selfd
        sum_clip += fade_clip

        if charge_ok != prev_charge_ok:
            ev(t, "CHARGE_ALLOWED" if charge_ok else "CHARGE_INHIBITED_TEMP", f"T_batt={Tb:.1f} C",
               pv_w=round(pv_w, 2))
            prev_charge_ok = charge_ok
        if unmet > 1e-9:
            ev(t, "UNMET_LOAD", f"{unmet:.3f} Wh could not be supplied ({cause})")
            force_brownout = True
            brownout_cause = cause

        # ---- thermal update: 2-node implicit (backward Euler) ----
        served = (direct + supplied) / load_bat_wh if load_bat_wh > 1e-12 else 1.0
        inside_W = (sum(e[g] for g in TH["inside_groups"]) + e_base) / dt_s * served
        conv_W = (load_bat_wh - rail_wh) / dt_h * served
        heater_W = e_heater_J / dt_s * served
        mppt_wh = (direct + accepted) * (1.0 / mppt_eff - 1.0)
        batt_loss_wh = (accepted - charged) + (draw - supplied)
        Q_enc = inside_W + conv_W + heater_W * (1 - frac_hb) + mppt_wh / dt_h
        Q_bat = heater_W * frac_hb + batt_loss_wh / dt_h
        ua_a = float(ua_a_arr[i])
        a11 = Ce / dt_s + ua_w_c + ua_a + K
        a22 = Cb / dt_s + K
        b1 = Ce / dt_s * Te + Q_enc + ua_w_c * Ts + ua_a * Ta
        b2 = Cb / dt_s * Tb + Q_bat
        det = a11 * a22 - K * K
        Te = (b1 * a22 + K * b2) / det
        Tb = (a11 * b2 + K * b1) / det

        out_spec = Tb < op_min or Tb > op_max
        if out_spec != flags["temp"]:
            flags["temp"] = out_spec
            ev(t, "BATTERY_OUT_OF_SPEC_TEMP" if out_spec else "BATTERY_TEMP_BACK_IN_SPEC",
               f"T_batt={Tb:.1f} C, range {B['op_temp_range']}")

        E = E_new
        I_term = (accepted - supplied) / dt_h / Voc
        V_term = Voc + I_term * R

        # ---- logging ----
        E_eff_n = Cn_next * f_cap(Tb)
        usable_n = max(0.0, E - (Cn_next - E_eff_n))
        L["pv_avail_wh"][i] = pv_wh
        L["pv_direct_wh"][i] = direct
        L["accepted_wh"][i] = accepted
        L["charged_wh"][i] = charged
        L["curtailed_wh"][i] = curtailed
        L["load_bat_wh"][i] = load_bat_wh
        L["batt_discharge_wh"][i] = draw
        L["batt_supplied_wh"][i] = supplied
        L["selfdis_wh"][i] = selfd
        L["fade_clip_wh"][i] = fade_clip
        L["ohmic_wh"][i] = ohm_c + ohm_d
        L["batt_loss_wh"][i] = batt_loss_wh
        L["mppt_loss_wh"][i] = mppt_wh
        L["conv_loss_wh"][i] = conv_loss_wh
        L["quiescent_wh"][i] = q_wh
        L["heater_wh_rail"][i] = e_heater_J / 3600.0
        L["unmet_wh"][i] = unmet
        for g in GROUPS:
            L["e_" + g][i] = e[g] / 3600.0
        L["e_base_sleep"][i] = e_base / 3600.0
        L["pkt_sent"][i] = st["sent"] - sent0
        L["pkt_failed"][i] = st["failed"] - failed0
        L["pkt_dropped"][i] = st["dropped"] - dropped0
        L["sd_dropped_bytes"][i] = st["sd_dropped"] - sddrop0
        L["heat_enc_w"][i] = Q_enc
        L["heat_batt_w"][i] = Q_bat
        L["ua_w"][i] = ua_w_c
        L["ua_a"][i] = ua_a
        S["E"][i] = E
        S["usable_wh"][i] = usable_n
        S["cap_wh"][i] = Cn_next
        S["soc_pct"][i] = 100 * min(1.0, usable_n / E_eff_n) if E_eff_n > 0 else 0.0
        S["soc_nom_pct"][i] = 100 * E / Cn0
        S["t_enc"][i] = Te
        S["t_batt"][i] = Tb
        S["mode"][i] = mode
        S["heater_on"][i] = 1.0 if heater_on else 0.0
        S["charge_ok"][i] = 1.0 if charge_ok else 0.0
        S["discharge_ok"][i] = 0.0 if locked_dis else 1.0
        S["volt"][i] = V_term
        S["curr"][i] = I_term
        S["r_int_ohm"][i] = R
        S["sst"][i] = Ts
        S["storage_mb"][i] = st["storage"] / 1e6
        S["backlog_pkts"][i] = st["backlog"]
        S["n_sensor"][i] = n_s
        S["n_imu"][i] = n_i
        S["n_gnss"][i] = n_g
        S["n_comm"][i] = n_c

    extra = dict(
        pv_stc_w_per_panel=round(float(pvd["p_stc"]), 3),
        packets_sent=int(st["sent"]), comm_failed_attempts=int(st["failed"]), packets_dropped=int(st["dropped"]),
        backlog_end=int(st["backlog"]), backlog_max=int(S["backlog_pkts"].max()) if n else 0,
        sd_dropped_mb=round(st["sd_dropped"] / 1e6, 3), sd_overwritten_mb=round(st["sd_overwritten"] / 1e6, 3),
        tasks_skipped=int(st["skipped"]),
        energy_balance_residual_wh=float((E - E_start) - (sum_charged - sum_draw - sum_selfd - sum_clip)),
    )
    return L, S, events, tasks, sst, extra


# ----------------------------------------------------------------------------
# POST-PROCESSING
# ----------------------------------------------------------------------------
RENAME = {"pv_avail_wh": "pv_potential_wh", "charged_wh": "batt_charged_wh", "e_mcu": "load_mcu_wh",
          "e_imu": "load_imu_wh", "e_gnss": "load_gnss_wh", "e_comm": "load_comm_wh", "e_sd": "load_sd_wh",
          "e_ctd": "load_ctd_wh", "e_ph": "load_ph_wh", "e_turb": "load_turbidity_wh", "e_do": "load_do_wh",
          "e_atmos": "load_atmos_wh", "e_internal": "load_internal_wh", "e_chl": "load_chl_wh",
          "e_base_sleep": "load_sleep_wh", "heater_wh_rail": "load_heater_wh", "heat_enc_w": "heat_enc_avg_w",
          "heat_batt_w": "heat_batt_avg_w", "n_sensor": "n_sensor_cycles", "n_imu": "n_imu_bursts",
          "n_gnss": "n_gnss_fixes", "n_comm": "n_comm_windows", "volt": "batt_voltage_v", "curr": "batt_current_a",
          "ua_w": "UA_water_WK", "ua_a": "UA_air_WK"}


def build_logs(cfg, wx, pvd, L, S, sst):
    n, spp, dt_h = pvd["n"], pvd["spp"], pvd["dt_h"]
    times = pvd["t0"] + pd.to_timedelta(np.arange(n) * dt_h, unit="h")
    step = pd.DataFrame(dict(
        time=times, t_hours=np.arange(n) * dt_h, solar_elev_deg=pvd["elev"], solar_az_deg=pvd["az"],
        ghi_wm2=pvd["ghi"], dhi_wm2=pvd["dhi"], poa_mean_wm2=pvd["poa"].mean(axis=1),
        poa_max_wm2=pvd["poa"].max(axis=1), t_air_c=pvd["t_air"], rh_pct=pvd["rh"], wind_10m_ms=pvd["ws"],
        t_sea_c=sst, albedo=pvd["albedo"], ice_frac=pvd["ice"], wx_interpolated=pvd["wx_interp"] > 0,
        **L, **S))
    step["mode_name"] = step["mode"].map(MODE_NAMES)
    step["pv_potential_w"] = step["pv_avail_wh"] / dt_h
    step["hour"] = np.arange(n) // spp
    step["date"] = step["time"].dt.date

    g = step.groupby("hour")
    means = ["solar_elev_deg", "solar_az_deg", "ghi_wm2", "dhi_wm2", "poa_mean_wm2", "poa_max_wm2", "t_air_c",
             "rh_pct", "wind_10m_ms", "t_sea_c", "heat_enc_w", "heat_batt_w", "volt", "curr", "r_int_ohm",
             "pv_potential_w", "ua_w", "ua_a", "ice_frac"]
    sums = [c for c in L if c not in SUM_EXCLUDE] + ["n_sensor", "n_imu", "n_gnss", "n_comm"]
    h = g[means].mean()
    for c in sums:
        h[c] = g[c].sum()
    h["time"] = g["time"].first()
    for dst, src in [("soc_pct", "soc_pct"), ("soc_nom_pct", "soc_nom_pct"), ("E_stored_wh", "E"),
                     ("usable_wh", "usable_wh"), ("capacity_wh", "cap_wh"), ("storage_mb", "storage_mb"),
                     ("backlog_pkts", "backlog_pkts"), ("mode", "mode")]:
        h[dst] = g[src].last()
    for node in ("enc", "batt"):
        h[f"t_{node}_c_end"] = g[f"t_{node}"].last()
        h[f"t_{node}_c_min"] = g[f"t_{node}"].min()
        h[f"t_{node}_c_max"] = g[f"t_{node}"].max()
    h["heater_on_min"] = g["heater_on"].sum() * cfg["dt_min"]
    h["charge_blocked_min"] = (1 - g["charge_ok"].mean()) * 60
    h["discharge_blocked_min"] = (1 - g["discharge_ok"].mean()) * 60
    for k, nm in MODE_NAMES.items():
        h[f"min_{nm.lower()}"] = (step["mode"] == k).groupby(step["hour"]).sum() * cfg["dt_min"]
    h["data_gap_min"] = h["min_off_brownout"]
    h["wx_interpolated"] = g["wx_interpolated"].max()
    h["mode_name"] = h["mode"].map(MODE_NAMES)
    h = h.rename(columns=RENAME).reset_index(drop=True)
    first = ["time", "ghi_wm2", "dhi_wm2", "poa_mean_wm2", "poa_max_wm2", "solar_elev_deg", "solar_az_deg",
             "t_air_c", "rh_pct", "wind_10m_ms", "t_sea_c", "pv_potential_wh", "pv_direct_wh", "batt_charged_wh",
             "curtailed_wh", "soc_pct", "soc_nom_pct", "E_stored_wh", "usable_wh", "capacity_wh",
             "batt_voltage_v", "batt_current_a", "t_enc_c_end", "t_enc_c_min", "t_enc_c_max",
             "t_batt_c_end", "t_batt_c_min", "t_batt_c_max", "mode", "mode_name", "heater_on_min",
             "charge_blocked_min", "discharge_blocked_min", "data_gap_min", "wx_interpolated"]
    first = [c for c in first if c in h.columns]
    h = h[first + [c for c in h.columns if c not in first]]
    h["load_total_rail_wh"] = h[[c for c in h.columns if c.startswith("load_") and c.endswith("_wh")
                                 and c != "load_bat_wh"]].sum(axis=1)
    num = h.select_dtypes("number").columns
    h[num] = h[num].round(5)

    # per-panel log
    pan = pd.DataFrame({f"P{k + 1}_w": pvd["p_pan"][:, k] for k in range(pvd["p_pan"].shape[1])})
    pan.insert(0, "hour", step["hour"].values)
    pan_h = pan.groupby("hour").mean()
    pan_h.insert(0, "time", h["time"].values)
    for k in range(pvd["poa"].shape[1]):
        pan_h[f"POA{k + 1}_wm2"] = pd.Series(pvd["poa"][:, k]).groupby(step["hour"].values).mean().values
    num = pan_h.select_dtypes("number").columns
    pan_h[num] = pan_h[num].round(4)

    # daily
    h["date"] = pd.to_datetime(h["time"]).dt.date
    dg, sg = h.groupby("date"), step.groupby("date")
    sensor_cols = ["load_ctd_wh", "load_ph_wh", "load_turbidity_wh", "load_do_wh", "load_atmos_wh",
                   "load_internal_wh", "load_chl_wh"]
    d = pd.DataFrame(dict(
        ghi_kwh_m2=dg["ghi_wm2"].sum() / 1000, t_air_mean_c=dg["t_air_c"].mean(), t_air_min_c=dg["t_air_c"].min(),
        t_sea_c=dg["t_sea_c"].mean(),
        pv_potential_wh=dg["pv_potential_wh"].sum(), pv_direct_wh=dg["pv_direct_wh"].sum(),
        pv_to_batt_terminal_wh=dg["accepted_wh"].sum(), batt_charged_wh=dg["batt_charged_wh"].sum(),
        curtailed_wh=dg["curtailed_wh"].sum(), load_bat_wh=dg["load_bat_wh"].sum(),
        batt_discharge_wh=dg["batt_discharge_wh"].sum(), batt_supplied_wh=dg["batt_supplied_wh"].sum(),
        heater_wh_rail=dg["load_heater_wh"].sum(), heater_hours=dg["heater_on_min"].sum() / 60,
        selfdischarge_wh=dg["selfdis_wh"].sum(), fade_clip_wh=dg["fade_clip_wh"].sum(),
        ohmic_wh=dg["ohmic_wh"].sum(), batt_loss_wh=dg["batt_loss_wh"].sum(),
        mppt_loss_wh=dg["mppt_loss_wh"].sum(), conv_loss_wh=dg["conv_loss_wh"].sum(),
        sensors_wh=dg[sensor_cols].sum().sum(axis=1),
        mcu_wh=dg["load_mcu_wh"].sum(), imu_wh=dg["load_imu_wh"].sum(), gnss_wh=dg["load_gnss_wh"].sum(),
        comm_wh=dg["load_comm_wh"].sum(), sd_wh=dg["load_sd_wh"].sum(), sleep_wh=dg["load_sleep_wh"].sum(),
        unmet_wh=dg["unmet_wh"].sum(),
        soc_end_pct=dg["soc_pct"].last(), soc_min_pct=sg["soc_pct"].min(), soc_max_pct=sg["soc_pct"].max(),
        usable_wh_end=dg["usable_wh"].last(), capacity_wh=dg["capacity_wh"].last(),
        t_enc_min_c=sg["t_enc"].min(), t_enc_max_c=sg["t_enc"].max(), t_enc_mean_c=sg["t_enc"].mean(),
        t_batt_min_c=sg["t_batt"].min(), t_batt_max_c=sg["t_batt"].max(), t_batt_mean_c=sg["t_batt"].mean(),
        charge_blocked_hours=dg["charge_blocked_min"].sum() / 60,
        discharge_blocked_hours=dg["discharge_blocked_min"].sum() / 60,
        data_gap_hours=dg["data_gap_min"].sum() / 60,
        hours_normal=dg["min_normal_observation"].sum() / 60, hours_low_energy=dg["min_low_energy_winter"].sum() / 60,
        hours_deep_sleep=dg["min_deep_sleep"].sum() / 60, hours_off=dg["min_off_brownout"].sum() / 60,
        n_sensor_cycles=dg["n_sensor_cycles"].sum(), n_gnss_fixes=dg["n_gnss_fixes"].sum(),
        n_comm_windows=dg["n_comm_windows"].sum(), n_imu_bursts=dg["n_imu_bursts"].sum(),
        pkt_sent=dg["pkt_sent"].sum(), pkt_failed=dg["pkt_failed"].sum(), pkt_dropped=dg["pkt_dropped"].sum(),
        backlog_pkts_end=dg["backlog_pkts"].last(), backlog_pkts_max=sg["backlog_pkts"].max(),
        storage_mb=dg["storage_mb"].last(), sd_dropped_mb=dg["sd_dropped_bytes"].sum() / 1e6,
        ice_frac_max=sg["ice_frac"].max(), wx_interpolated_hours=dg["wx_interpolated"].sum(),
    ))
    d["net_wh"] = d["batt_charged_wh"] - d["batt_discharge_wh"] - d["selfdischarge_wh"] - d["fade_clip_wh"]
    d["load_no_heater_wh"] = d["load_bat_wh"] - d["heater_wh_rail"] / cfg["pe"]["eta_5v"]
    d["autonomy_days_at_7d_load"] = d["usable_wh_end"] / (d["load_no_heater_wh"].rolling(7, min_periods=1).mean() + 1e-9)
    d["day_of_sim"] = np.arange(1, len(d) + 1)
    num = d.select_dtypes("number").columns
    d[num] = d[num].round(4)
    d.index.name = "date"
    d = d.reset_index()

    # monthly (grouped by calendar year-month)
    d["month"] = pd.to_datetime(d["date"]).dt.to_period("M").astype(str)
    mg = d.groupby("month")
    mo = pd.DataFrame(dict(
        ghi_kwh_m2=mg["ghi_kwh_m2"].sum(), t_air_mean_c=mg["t_air_mean_c"].mean(), t_air_min_c=mg["t_air_min_c"].min(),
        pv_potential_wh=mg["pv_potential_wh"].sum(), batt_charged_wh=mg["batt_charged_wh"].sum(),
        curtailed_wh=mg["curtailed_wh"].sum(), load_bat_wh=mg["load_bat_wh"].sum(),
        heater_wh_rail=mg["heater_wh_rail"].sum(), heater_hours=mg["heater_hours"].sum(),
        selfdischarge_wh=mg["selfdischarge_wh"].sum(), ohmic_wh=mg["ohmic_wh"].sum(),
        sensors_wh=mg["sensors_wh"].sum(), gnss_wh=mg["gnss_wh"].sum(), comm_wh=mg["comm_wh"].sum(),
        mcu_wh=mg["mcu_wh"].sum(), sd_wh=mg["sd_wh"].sum(), imu_wh=mg["imu_wh"].sum(),
        soc_end_pct=mg["soc_end_pct"].last(), soc_min_pct=mg["soc_min_pct"].min(),
        t_enc_min_c=mg["t_enc_min_c"].min(), t_enc_mean_c=mg["t_enc_mean_c"].mean(),
        t_batt_min_c=mg["t_batt_min_c"].min(), t_batt_mean_c=mg["t_batt_mean_c"].mean(),
        charge_blocked_hours=mg["charge_blocked_hours"].sum(), data_gap_hours=mg["data_gap_hours"].sum(),
        hours_normal=mg["hours_normal"].sum(), hours_low_energy=mg["hours_low_energy"].sum(),
        hours_deep_sleep=mg["hours_deep_sleep"].sum(), hours_off=mg["hours_off"].sum(),
        pkt_sent=mg["pkt_sent"].sum(), pkt_dropped=mg["pkt_dropped"].sum(),
        backlog_pkts_max=mg["backlog_pkts_max"].max(),
    )).round(3).reset_index()
    return step, h, pan_h, d, mo


def subsystem_summary(h):
    mapping = [("MCU (STM32)", "load_mcu_wh"), ("IMU", "load_imu_wh"), ("GNSS", "load_gnss_wh"),
               ("Satellite modem", "load_comm_wh"), ("SD card", "load_sd_wh"),
               ("Water T / conductivity / depth", "load_ctd_wh"), ("pH", "load_ph_wh"),
               ("Turbidity", "load_turbidity_wh"), ("Dissolved oxygen", "load_do_wh"),
               ("Air T/RH/P/irradiance", "load_atmos_wh"), ("Internal sensors", "load_internal_wh"),
               ("Chlorophyll-a", "load_chl_wh"), ("Always-on sleep currents", "load_sleep_wh"),
               ("Heater (rail side)", "load_heater_wh"), ("Converter losses", "conv_loss_wh"),
               ("BMS/MPPT quiescent", "quiescent_wh"), ("Battery self-discharge", "selfdis_wh"),
               ("Battery efficiency + ohmic losses", "batt_loss_wh")]
    df = pd.DataFrame([dict(subsystem=nm, annual_wh=float(h[col].sum())) for nm, col in mapping])
    tot = df["annual_wh"].sum()
    df["share_pct"] = 100 * df["annual_wh"] / tot if tot > 0 else 0.0
    return df.round(3)


def kpis(cfg, h, d, events, S, extra):
    dmin = d.loc[d["soc_min_pct"].idxmin(), "date"]
    brownouts = [e for e in events if e["event"] == "MODE_CHANGE" and e["detail"].endswith("OFF_BROWNOUT")]
    op_min = cfg["batt"]["op_temp_range"][0]
    hours_off = float(d["hours_off"].sum())
    unmet = float(d["unmet_wh"].sum())
    return dict(
        scenario=cfg["name"], sim_days=int(len(d)),
        pv_potential_kwh=round(d["pv_potential_wh"].sum() / 1000, 3),
        pv_stored_in_battery_wh=round(d["batt_charged_wh"].sum(), 1),
        curtailed_wh=round(d["curtailed_wh"].sum(), 1),
        total_load_battery_side_wh=round(d["load_bat_wh"].sum(), 1),
        heater_wh_rail=round(d["heater_wh_rail"].sum(), 1), heater_hours=round(d["heater_hours"].sum(), 1),
        sensing_comm_logging_wh=round(d[["sensors_wh", "mcu_wh", "imu_wh", "gnss_wh", "comm_wh", "sd_wh",
                                         "sleep_wh"]].sum().sum(), 1),
        selfdischarge_wh=round(d["selfdischarge_wh"].sum(), 1),
        battery_ohmic_wh=round(d["ohmic_wh"].sum(), 2), battery_total_loss_wh=round(d["batt_loss_wh"].sum(), 2),
        mppt_loss_wh=round(d["mppt_loss_wh"].sum(), 2),
        min_soc_pct=round(float(d["soc_min_pct"].min()), 1), min_soc_date=str(dmin),
        end_soc_pct=round(float(d["soc_end_pct"].iloc[-1]), 1),
        hours_normal=round(float(d["hours_normal"].sum()), 1),
        hours_low_energy=round(float(d["hours_low_energy"].sum()), 1),
        hours_deep_sleep=round(float(d["hours_deep_sleep"].sum()), 1),
        hours_off_brownout=round(hours_off, 1), n_brownouts=len(brownouts),
        first_brownout=brownouts[0]["time"] if brownouts else None,
        unmet_load_wh=round(unmet, 3),
        survived_full_period=bool(hours_off == 0 and unmet < 1e-6),
        t_enc_min_c=round(float(d["t_enc_min_c"].min()), 1), t_enc_max_c=round(float(d["t_enc_max_c"].max()), 1),
        t_batt_min_c=round(float(d["t_batt_min_c"].min()), 1), t_batt_max_c=round(float(d["t_batt_max_c"].max()), 1),
        t_batt_below_op_min_h=round(float((S["t_batt"] < op_min).sum()) * cfg["dt_min"] / 60, 1),
        charge_blocked_hours=round(float(d["charge_blocked_hours"].sum()), 1),
        data_gap_hours=round(float(d["data_gap_hours"].sum()), 1),
        sensor_cycles=int(d["n_sensor_cycles"].sum()), gnss_fixes=int(d["n_gnss_fixes"].sum()),
        comm_windows=int(d["n_comm_windows"].sum()),
        sd_used_mb=round(float(d["storage_mb"].iloc[-1]), 1),
        wx_interpolated_hours=int(d["wx_interpolated_hours"].sum()),
        **extra,
    )


def simulate(cfg, wx, sst_series=None, albedo_series=None):
    pvd = build_pv(cfg, wx, albedo_series)
    L, S, events, tasks, sst, extra = run(cfg, wx, pvd, sst_series)
    step, h, pan_h, d, mo = build_logs(cfg, wx, pvd, L, S, sst)
    k = kpis(cfg, h, d, events, S, extra)
    return dict(step=step, h=h, pan_h=pan_h, d=d, mo=mo, events=events, tasks=tasks, S=S, kpi=k)


def write_outputs(res, cfg, outdir, baseline, write_step):
    if baseline:
        if write_step:
            res["step"].drop(columns=["hour", "date"]).to_csv(
                os.path.join(outdir, "step_log_10min.csv.gz"), index=False, compression="gzip")
        res["h"].drop(columns=["date"]).to_csv(os.path.join(outdir, "hourly_log.csv"), index=False)
        res["pan_h"].to_csv(os.path.join(outdir, "pv_panels_hourly.csv"), index=False)
        res["d"].to_csv(os.path.join(outdir, "daily_summary.csv"), index=False)
        res["mo"].to_csv(os.path.join(outdir, "monthly_summary.csv"), index=False)
        subsystem_summary(res["h"]).to_csv(os.path.join(outdir, "subsystem_energy_annual.csv"), index=False)
        pd.DataFrame(res["events"]).to_csv(os.path.join(outdir, "event_log.csv"), index=False)
        pd.DataFrame(res["tasks"]).to_csv(os.path.join(outdir, "task_log.csv"), index=False)
        with open(os.path.join(outdir, "config_used.json"), "w") as f:
            json.dump(cfg, f, indent=2, default=str)
        with open(os.path.join(outdir, "kpis.json"), "w") as f:
            json.dump(res["kpi"], f, indent=2, default=str)
    else:
        sdir = os.path.join(outdir, "scenarios")
        os.makedirs(sdir, exist_ok=True)
        name = cfg["name"]
        res["d"].to_csv(os.path.join(sdir, f"{name}_daily.csv"), index=False)
        cols = ["time", "soc_pct", "t_enc_c_end", "t_batt_c_end", "mode", "load_heater_wh", "pv_potential_wh",
                "backlog_pkts"]
        res["h"][[c for c in cols if c in res["h"].columns]].to_csv(
            os.path.join(sdir, f"{name}_hourly_core.csv"), index=False)
        pd.DataFrame(res["events"]).to_csv(os.path.join(sdir, f"{name}_events.csv"), index=False)
        with open(os.path.join(sdir, f"{name}_config.json"), "w") as f:
            json.dump(cfg, f, indent=2, default=str)


# ----------------------------------------------------------------------------
# SCENARIOS
# ----------------------------------------------------------------------------
def scenarios():
    base = copy.deepcopy(CFG)
    out = {"baseline": base}

    def add(name, fn):
        c = copy.deepcopy(base)
        c["name"] = name
        fn(c)
        out[name] = c

    add("spec_heater_8C", lambda c: c["heater"].update(policy="thermostat"))
    add("sd_not_power_gated", lambda c: c["sd"].update(power_gated=False))
    add("panel_icing_accumulation", lambda c: c["pv"]["icing"].update(enabled=True))

    def hi_rate(c):
        c["modes"]["intervals_h"]["sensors"][3] = 5 / 60
        c["modes"]["intervals_h"]["gnss"][3] = 0.25
        c["modes"]["intervals_h"]["comm"][3] = 1.0
    add("high_rate_5min_sampling", hi_rate)
    add("no_ice_albedo_open_water", lambda c: c["pv"].update(albedo_monthly=[0.08] * 12))

    def l89(c):
        c["gnss"]["model"] = "l89ha"
        c["sensors"] = [(*s[:8], (3,)) if s[0] == "chlorophyll_a" else s for s in c["sensors"]]
    add("L89HA_GNSS_plus_chlorophyll", l89)
    add("comm_outage_30d_winter", lambda c: c["comm"].update(outages=[(152.0, 182.0)]))
    add("link_70pct_success", lambda c: c["comm"].update(link_success_prob=0.7))
    add("battery_fade_20pct_per_year", lambda c: c["batt"].update(fade_per_year=0.20))
    add("panel_failure_day60", lambda c: c["pv"].update(panel_failures=[dict(panel=0, day=60, factor=0.0)]))
    add("sd_512MB_stop_when_full", lambda c: c["sd"].update(capacity_gb=0.512, full_policy="stop"))
    return out


# ----------------------------------------------------------------------------
# MONTE CARLO
# ----------------------------------------------------------------------------
MC_RANGES = dict(            # [ASSUMED] illustrative uniform ranges - replace with justified distributions
    pv_scale=(0.70, 1.10),
    batt_nominal_wh=(150.0, 192.0),
    heater_power_w=(5.0, 8.0),
    mppt_eff=(0.90, 0.97),
    k_xps=(0.028, 0.040),
    motion=(0.85, 0.98),
    fade_per_year=(0.0, 0.05),
    r_int_ohm=(0.04, 0.12),
)
MC_KEYS = ["survived_full_period", "hours_off_brownout", "n_brownouts", "unmet_load_wh", "min_soc_pct",
           "end_soc_pct", "t_batt_min_c", "t_batt_below_op_min_h", "heater_wh_rail", "packets_dropped",
           "sd_dropped_mb", "charge_blocked_hours"]


def mc_cfg(base, rng, idx):
    c = copy.deepcopy(base)
    c["name"] = f"mc_{idx:04d}"
    s = {k: float(rng.uniform(*v)) for k, v in MC_RANGES.items()}
    c["pv"]["scale"] = s["pv_scale"]
    c["batt"]["nominal_wh"] = s["batt_nominal_wh"]
    c["heater"]["power_w"] = s["heater_power_w"]
    c["pv"]["mppt_eff"] = s["mppt_eff"]
    c["thermal"]["k_xps"] = s["k_xps"]
    c["pv"]["motion"] = s["motion"]
    c["batt"]["fade_per_year"] = s["fade_per_year"]
    c["batt"]["r_int_ohm"] = s["r_int_ohm"]
    c["comm"]["rng_seed"] = int(rng.integers(0, 2**31 - 1))
    return c, s


def _mc_worker(job):
    cfg, s, wx, sst_s, alb_s = job
    try:
        res = simulate(cfg, wx, sst_s, alb_s)
        return {"run": cfg["name"], **s, **{k: res["kpi"][k] for k in MC_KEYS}, "error": ""}
    except Exception as exc:          # one bad sample must not kill the whole batch
        return {"run": cfg["name"], **s, "error": repr(exc)}


def wilson(k, n, z=1.96):
    if n == 0:
        return (None, None)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    hw = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (round(c - hw, 4), round(c + hw, 4))


def run_monte_carlo(base, wx, n_runs, workers, seed, sst_s, alb_s, outdir):
    rng = np.random.default_rng(seed)
    jobs = []
    for idx in range(n_runs):
        c, s = mc_cfg(base, rng, idx)
        jobs.append((c, s, wx, sst_s, alb_s))
    rows = []
    if workers <= 1:
        for j, job in enumerate(jobs):
            rows.append(_mc_worker(job))
            if (j + 1) % 10 == 0:
                log.info("Monte Carlo: %d/%d runs done", j + 1, n_runs)
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for r in ex.map(_mc_worker, jobs, chunksize=1):
                rows.append(r)
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(outdir, "mc_runs.csv"), index=False)
    ok = df[df["error"] == ""] if "error" in df.columns else df
    nok = len(ok)
    summary = dict(runs=n_runs, completed=nok, failed_runs=n_runs - nok, seed=seed,
                   ranges={k: list(v) for k, v in MC_RANGES.items()},
                   note="Probabilities are only as meaningful as the input ranges; these ranges are placeholders.")
    if nok:
        surv = int(ok["survived_full_period"].astype(bool).sum())
        summary.update(
            p_survive=round(surv / nok, 4), p_survive_95ci=wilson(surv, nok),
            p_brownout=round(float((ok["hours_off_brownout"] > 0).mean()), 4),
            min_soc_pct_p05=round(float(ok["min_soc_pct"].quantile(0.05)), 2),
            min_soc_pct_p50=round(float(ok["min_soc_pct"].quantile(0.50)), 2),
            min_soc_pct_p95=round(float(ok["min_soc_pct"].quantile(0.95)), 2),
            p_t_batt_below_op_min=round(float((ok["t_batt_below_op_min_h"] > 0).mean()), 4),
            p_sd_data_loss=round(float((ok["sd_dropped_mb"] > 0).mean()), 4),
            p_packet_loss=round(float((ok["packets_dropped"] > 0).mean()), 4),
        )
        corr = ok[list(MC_RANGES) + ["min_soc_pct"]].astype(float).corr(method="spearman")["min_soc_pct"]
        summary["spearman_vs_min_soc"] = {k: (None if pd.isna(corr[k]) else round(float(corr[k]), 3))
                                          for k in MC_RANGES}
    with open(os.path.join(outdir, "mc_summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    return summary


# ----------------------------------------------------------------------------
# SELF-TEST
# ----------------------------------------------------------------------------
def _synthetic_power_csv(path, n_days=20, basis="LST", gap=None, drop_col=None, no_header=False):
    hrs = pd.date_range("2025-01-01", periods=n_days * 24, freq="h")
    hr = hrs.hour.values.astype(float)
    df = pd.DataFrame(dict(
        YEAR=hrs.year, MO=hrs.month, DY=hrs.day, HR=hrs.hour,
        ALLSKY_SFC_SW_DWN=np.clip(450 * np.sin(np.pi * (hr - 4) / 16), 0, None).round(2),
        T2M=(-1.0 + 2.0 * np.sin(np.pi * (hr - 6) / 12)).round(2), RH2M=85.0, WS10M=7.0))
    if gap is not None:
        df.loc[gap[0]:gap[1] - 1, "T2M"] = -999
    if drop_col:
        df = df.drop(columns=[drop_col])
    with open(path, "w", newline="") as f:
        f.write("-BEGIN HEADER-\nNASA/POWER synthetic test file\n"
                f"Dates (month/day/year): 01/01/2025 through 12/31/2025 in {basis}\n-END HEADER-\n")
        df.to_csv(f, index=False, header=not no_header)


def selftest():
    import tempfile
    results = []

    def check(name, cond, info=""):
        results.append((name, bool(cond), info))

    def raises(fn, exc=WeatherError):
        try:
            fn()
        except exc:
            return True
        except Exception:
            return False
        return False

    prev = log.level
    log.setLevel(logging.ERROR)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "wx.csv")
            _synthetic_power_csv(p, 20)
            wx, qc = load_weather(p, 20, 40.0)
            check("loader: 480 hourly rows", len(wx) == 480, len(wx))
            check("loader: clean file not interpolated", qc["hours_interpolated"] == 0, qc["hours_interpolated"])

            p2 = os.path.join(tmp, "nohdr.csv")
            _synthetic_power_csv(p2, 20, no_header=True)
            check("loader: missing YEAR header rejected", raises(lambda: load_weather(p2, 20, 40.0)))
            p3 = os.path.join(tmp, "nocol.csv")
            _synthetic_power_csv(p3, 20, drop_col="WS10M")
            check("loader: missing column rejected", raises(lambda: load_weather(p3, 20, 40.0)))
            check("loader: short file rejected", raises(lambda: load_weather(p, 30, 40.0)))
            wxs, _ = load_weather(p, 30, 40.0, allow_short=True)
            check("loader: --allow-short runs shorter", len(wxs) == 480)
            p4 = os.path.join(tmp, "gap.csv")
            _synthetic_power_csv(p4, 20, gap=(100, 110))
            check("loader: 10 h gap rejected", raises(lambda: load_weather(p4, 20, 40.0)))
            _, qg = load_weather(p4, 20, 40.0, allow_gaps=True)
            check("loader: gap flagged when allowed", qg["hours_interpolated"] == 10, qg["hours_interpolated"])
            wxu, qu = load_weather(p, 20, 40.0, time_basis="UTC")
            check("loader: UTC shifted to solar time",
                  int(np.argmax(wxu["ALLSKY_SFC_SW_DWN"].values[24:48])) > int(np.argmax(wx["ALLSKY_SFC_SW_DWN"].values[24:48])))

            cfg = copy.deepcopy(CFG)
            cfg["n_days"] = 20
            check("config: baseline validates", isinstance(validate_config(cfg), list))
            bad = copy.deepcopy(cfg)
            bad["batt"]["cutoff_soc"] = 0.5
            check("config: bad SOC thresholds rejected", raises(lambda: validate_config(bad), ConfigError))

            r = simulate(cfg, wx)
            k, S, d = r["kpi"], r["S"], r["d"]
            check("energy balance closes", abs(k["energy_balance_residual_wh"]) < 1e-6, k["energy_balance_residual_wh"])
            check("no NaN/inf in state", all(np.isfinite(v).all() for v in S.values()))
            check("SOC within [0, 100]", S["soc_pct"].min() >= 0 and S["soc_pct"].max() <= 100 + 1e-9)
            check("stored energy <= capacity", np.all(S["E"] <= S["cap_wh"] + 1e-9))
            check("ohmic losses computed", d["ohmic_wh"].sum() > 0)
            check("battery and enclosure temperatures differ", np.max(np.abs(S["t_batt"] - S["t_enc"])) > 1e-6)
            eta = cfg["pv"]["mppt_eff"]
            check("MPPT loss uses harvested (not curtailed) energy",
                  d["mppt_loss_wh"].sum() < d["pv_potential_wh"].sum() * (1 / eta - 1) - 1e-6
                  if d["curtailed_wh"].sum() > 0 else True)

            c2 = copy.deepcopy(cfg)
            c2["sd"].update(capacity_gb=1e-5, full_policy="stop")
            r2 = simulate(c2, wx)
            check("SD capacity enforced (stop)", r2["S"]["storage_mb"].max() <= 0.01 + 1e-9
                  and r2["kpi"]["sd_dropped_mb"] > 0)

            c3 = copy.deepcopy(cfg)
            c3["comm"]["outages"] = [(2.0, 5.0)]
            r3 = simulate(c3, wx)
            check("comm outage builds backlog", r3["kpi"]["backlog_max"] > 0, r3["kpi"]["backlog_max"])
            check("backlog drains after outage", r3["kpi"]["backlog_end"] == 0, r3["kpi"]["backlog_end"])
            check("comm outage costs no data", r3["kpi"]["packets_dropped"] == 0)

            c4 = copy.deepcopy(cfg)
            c4["pv"]["panel_failures"] = [dict(panel=0, day=0, factor=0.0)]
            r4 = simulate(c4, wx)
            check("panel failure reduces PV", r4["kpi"]["pv_potential_kwh"] < k["pv_potential_kwh"])
    finally:
        log.setLevel(prev)

    width = max(len(nm) for nm, _, _ in results)
    for nm, ok, info in results:
        print(f"{'PASS' if ok else 'FAIL'}  {nm.ljust(width)}  {info if not ok else ''}")
    n_fail = sum(1 for _, ok, _ in results if not ok)
    print(f"\n{len(results) - n_fail}/{len(results)} checks passed")
    return 0 if n_fail == 0 else 1


# ----------------------------------------------------------------------------
# PROVENANCE + MAIN
# ----------------------------------------------------------------------------
def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description="BOYANT 365-day buoy energy/thermal simulation")
    ap.add_argument("--weather", help="NASA POWER hourly CSV")
    ap.add_argument("--out", default="logs")
    ap.add_argument("--scenarios", action="store_true", help="also run sensitivity / failure scenarios")
    ap.add_argument("--monte-carlo", type=int, default=0, metavar="N", help="run N Monte Carlo samples")
    ap.add_argument("--workers", type=int, default=1, help="processes for Monte Carlo")
    ap.add_argument("--seed", type=int, default=2026, help="Monte Carlo seed")
    ap.add_argument("--sst", help="optional SST CSV (time,value in degC)")
    ap.add_argument("--albedo", help="optional surface albedo CSV (time,value in 0..1)")
    ap.add_argument("--allow-gaps", action="store_true")
    ap.add_argument("--allow-short", action="store_true")
    ap.add_argument("--max-gap-h", type=float, default=None)
    ap.add_argument("--time-basis", choices=["auto", "LST", "UTC"], default=None)
    ap.add_argument("--no-step-log", action="store_true", help="skip the large 10-min step log")
    ap.add_argument("--selftest", action="store_true", help="run built-in checks and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)s %(message)s")

    if a.selftest:
        return selftest()
    if not a.weather:
        ap.error("--weather is required (or use --selftest)")

    try:
        warns = validate_config(CFG)
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2
    for w in warns:
        log.warning(w)

    W = CFG["weather"]
    try:
        wx, qc = load_weather(a.weather, CFG["n_days"], CFG["site"]["lon"],
                              max_gap_h=a.max_gap_h if a.max_gap_h is not None else W["max_gap_h"],
                              allow_gaps=a.allow_gaps or W["allow_gaps"],
                              allow_short=a.allow_short or W["allow_short"],
                              time_basis=a.time_basis or W["time_basis"])
        sst_s = load_series_csv(a.sst, "SST", -2.5, 35.0) if a.sst else None
        alb_s = load_series_csv(a.albedo, "albedo", 0.0, 1.0) if a.albedo else None
    except WeatherError as exc:
        log.error("input data error: %s", exc)
        return 2
    log.info("weather: %s -> %s, %d h used, %d h interpolated, basis %s",
             qc["start"], qc["end"], qc["hours_used"], qc["hours_interpolated"], qc["time_basis"])

    os.makedirs(a.out, exist_ok=True)
    meta = dict(sim_version=SIM_VERSION, run_started_utc=datetime.now(timezone.utc).isoformat(),
                python=sys.version, platform=platform.platform(), numpy=np.__version__, pandas=pd.__version__,
                argv=sys.argv if argv is None else argv, weather_file=os.path.abspath(a.weather),
                weather_sha256=_sha256(a.weather),
                sst_file=os.path.abspath(a.sst) if a.sst else None,
                sst_sha256=_sha256(a.sst) if a.sst else None,
                albedo_file=os.path.abspath(a.albedo) if a.albedo else None,
                albedo_sha256=_sha256(a.albedo) if a.albedo else None,
                sst_source="file" if a.sst else "sinusoidal model [ASSUMED]",
                albedo_source="file" if a.albedo else "monthly table [ASSUMED]",
                weather_qc=qc, config_warnings=warns)

    all_cfgs = scenarios() if a.scenarios else {"baseline": copy.deepcopy(CFG)}
    write_step = CFG["output"]["write_step_log"] and not a.no_step_log
    comp, failed = [], []
    for name, cfg in all_cfgs.items():
        try:
            for w in validate_config(cfg):
                if name != "baseline":
                    log.warning("[%s] %s", name, w)
            res = simulate(cfg, wx, sst_s, alb_s)
            k = res["kpi"]
            comp.append(k)
            print(f"[{name}] survived={k['survived_full_period']} | min SOC {k['min_soc_pct']}% on {k['min_soc_date']} | "
                  f"T_batt min {k['t_batt_min_c']} C | heater {k['heater_wh_rail']} Wh | "
                  f"brownout h {k['hours_off_brownout']} | packets dropped {k['packets_dropped']}")
            if abs(k["energy_balance_residual_wh"]) > 1e-3:
                log.warning("[%s] energy balance residual %.4g Wh", name, k["energy_balance_residual_wh"])
            write_outputs(res, cfg, a.out, baseline=(name == "baseline"), write_step=write_step)
        except Exception:
            log.exception("scenario '%s' failed", name)
            failed.append(name)
    if a.scenarios and comp:
        pd.DataFrame(comp).to_csv(os.path.join(a.out, "scenario_comparison.csv"), index=False)

    if a.monte_carlo > 0:
        log.info("Monte Carlo: %d runs on %d worker(s)", a.monte_carlo, max(1, a.workers))
        try:
            summ = run_monte_carlo(copy.deepcopy(CFG), wx, a.monte_carlo, a.workers, a.seed, sst_s, alb_s, a.out)
            meta["monte_carlo"] = summ
            if "p_survive" in summ:
                print(f"[monte-carlo] P(survive) = {summ['p_survive']} (95% CI {summ['p_survive_95ci']}), "
                      f"P(brownout) = {summ['p_brownout']}")
        except Exception:
            log.exception("Monte Carlo failed")
            failed.append("monte_carlo")

    meta["run_finished_utc"] = datetime.now(timezone.utc).isoformat()
    meta["failed"] = failed
    with open(os.path.join(a.out, "run_metadata.json"), "w") as f:
        json.dump(meta, f, indent=2, default=str)
    print("done ->", a.out, "" if not failed else f"(failed: {failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
