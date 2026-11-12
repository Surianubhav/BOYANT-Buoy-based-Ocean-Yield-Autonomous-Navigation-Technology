#!/usr/bin/env python3
"""
BOYANT - 365-day Southern Ocean deployment simulation
=====================================================
Team Treadmill | SIH 2026 | PS 26065

Physics-based, energy-aware simulation of the buoy at 63.67S 40.49E using the
NASA POWER hourly weather file (Jan 1 - Dec 31 2025, local solar time).

Time step : 10 min (weather interpolated from hourly data)
Outputs   : step / hourly / daily / monthly logs, task log, event log,
            per-panel PV log, subsystem energy summary, config dump.

Everything that is a number lives in CFG below. Values marked
[SHEET]   come from your component spreadsheet
[SLIDE]   come from your PDF slides / concept report
[ASSUMED] are placeholders I had to choose - please confirm or replace them.

Usage:
    python boyant_sim.py --weather POWER_....csv --out logs
    python boyant_sim.py --weather POWER_....csv --out logs --scenarios
"""
import argparse, copy, json, math, os
import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------------
CFG = dict(
    name="baseline",
    load_margin=1.0,                                                # multiplier on all measured loads (1.0 = datasheet values)
    site=dict(lat=-63.6686, lon=40.4935),                          # [SLIDE/CSV header]
    dt_min=10,                                                      # simulation step
    n_days=365,

    # ---------------- Solar array ----------------
    pv=dict(
        n_panels=8,                      # [SHEET] 8 panels, one per octagonal face
        v_mp=8.3, i_mp=0.85,             # [SHEET] (sheet had the units swapped: 8.3 V / 0.85 A)
        area_m2=0.03656, eff=0.193,      # [SHEET]  -> 1000*0.03656*0.193 = 7.06 W = Vmp*Imp  OK
        tilt_deg=45.0,                   # [SHEET geometry] cone R=300, H=300 -> 45 deg
        heading_deg=0.0,                 # buoy yaw (8 panels -> near-symmetric)
        temp_coeff=-0.0035,              # [ASSUMED] mono-Si power coeff /degC
        noct=45.0,                       # [ASSUMED]
        wiring="series_pairs",           # [ASSUMED] 4 adjacent pairs in series (2x8.3V > 14.6V charge V)
        albedo_monthly=[0.08, 0.08, 0.10, 0.25, 0.55, 0.60, 0.60, 0.60, 0.55, 0.50, 0.30, 0.10],  # [ASSUMED] open water -> pack ice/snow
        iam_b0=0.05,                     # ASHRAE incidence-angle modifier
        soiling=0.95,                    # [ASSUMED] salt film / spray
        motion=0.95,                     # [ASSUMED] wave heel + yaw losses
        wiring_eff=0.97,                 # [ASSUMED] blocking diodes, cable
        mppt_eff=0.95,                   # [SHEET]
        mppt_min_w=0.05,                 # [ASSUMED] below this the MPPT does not start
        ice_loss_factor=1.0,             # multiplier applied when T_air < ice_loss_below_c (1.0 = off)
        ice_loss_below_c=-2.0,
    ),

    # ---------------- Battery ----------------
    batt=dict(
        nominal_wh=192.0,                # [SHEET] 12.8 V x 15 Ah
        charge_eff=0.95, discharge_eff=0.95,   # [SHEET]
        self_discharge_per_month=0.03,   # [SHEET]
        soc0=0.95,                       # [ASSUMED] ship fully charged
        min_soc_policy=0.20,             # [SHEET] min SOC
        cutoff_soc=0.03,                 # [ASSUMED] BMS under-voltage (10 V) trip
        restart_soc=0.10,                # [ASSUMED] restart after brown-out
        charge_min_temp_c=0.0,           # [ASSUMED] BMS blocks LiFePO4 charging below 0 degC
        allow_cold_charge=False,
        op_temp_range=(-20.0, 60.0),     # [SHEET]
        cap_vs_temp=([-20, -10, 0, 10, 25, 40], [70, 80, 90, 95, 100, 100]),   # [SHEET] % capacity
        r_int_ohm=0.06,                  # [ASSUMED] pack internal resistance (voltage log only)
        ocv_soc=[0, .05, .10, .20, .30, .40, .50, .60, .70, .80, .90, .95, 1.0],
        ocv_cell=[2.90, 3.10, 3.20, 3.24, 3.26, 3.28, 3.29, 3.31, 3.33, 3.35, 3.40, 3.45, 3.55],  # [ASSUMED] LFP curve
    ),

    # ---------------- Power electronics ----------------
    pe=dict(
        eta_5v=0.9895, eta_3v3=0.9299,       # [ASSUMED] light-load synchronous buck efficiency (slide: up to 98 % at full load)
        quiescent_w=0.004,               # [ASSUMED] BMS + MPPT + regulator idle draw (battery side)
    ),

    # ---------------- Thermal ----------------
    thermal=dict(
        hdpe_t_m=0.008, k_hdpe=0.45,     # [SHEET]
        gap_t_m=0.010, k_air=0.026,      # [SHEET] 10 mm air gap (foil kills radiation -> conduction only)
        xps_t_m=0.050, k_xps=0.032,      # [SHEET]
        pc_t_m=0.005, k_pc=0.20,         # [SHEET thickness] k_PC [ASSUMED]
        area_m2=0.50,                    # [ASSUMED] mean insulation area of electronics compartment
        frac_water=0.60,                 # [ASSUMED] share of that area below the waterline
        bridge_ua=0.03,                  # [ASSUMED] W/K thermal bridges: cable glands, SS fasteners, keel conduit
        h_water=500.0,                   # W/m2K
        wind_factor=0.6,                 # site wind at hull height / 10 m wind
        heat_capacity_j_k=3500.0,        # [ASSUMED] battery + electronics + box
        inside_groups=["mcu", "gnss", "comm", "sd", "imu", "internal"],   # dissipate inside the box
        t_enc0=2.0,
        # sea surface temperature model (no SST file supplied)
        sst_mean=-0.7, sst_amp=1.1, sst_peak_doy=50, sst_floor=-1.8,      # [ASSUMED]
    ),

    # ---------------- Heater (PTC, 5 V) ----------------
    heater=dict(
        policy="adaptive",               # "adaptive" | "thermostat"
        power_w=6.0,                     # [SHEET] 4-8 W PTC, mid value
        # adaptive policy
        protect_on_c=-12.0, protect_off_c=-8.0, protect_min_soc=0.15,
        assist_on_c=0.5, assist_off_c=3.0, assist_max_soc=0.97,   # only warm up if battery can actually accept charge
                   # warm battery so BMS allows charging
        assist_pv_on_frac=0.6, assist_pv_hold_frac=0.3,   # PV surplus needed (fraction of heater power)
        # thermostat policy (spec: enclosure > 8 degC)
        thermo_on_c=8.0, thermo_off_c=10.0, thermo_min_soc=0.05,
    ),

    # ---------------- Mode controller ----------------
    modes=dict(
        deep_enter_soc=0.20, deep_exit_soc=0.30,
        low_enter_soc=0.50, low_exit_soc=0.65,
        low_enter_harvest_wh_day=5.0, low_exit_harvest_wh_day=10.0,   # 72 h rolling PV potential
        low_harvest_soc_gate=1.10,   # >1 = go to winter mode on low solar outlook even with a full battery (set 0.85 to only react to SOC)
        intervals_h={      # 0 = task disabled in that mode
            "sensors": {3: 0.25, 2: 2.0, 1: 12.0},
            "imu":     {3: 0.50, 2: 0.0, 1: 0.0},
            "gnss":    {3: 1.0,  2: 6.0, 1: 24.0},
            "comm":    {3: 6.0,  2: 24.0, 1: 48.0},
        },
        alert_min_gap_h=12.0,
    ),

    # ---------------- Loads ----------------
    # name, group, rail_V, I_active_mA, warmup_s, measure_s, count, channels, modes{3=NORMAL,2=LOW,1=DEEP}
    sensors=[
        ("DS18B20_water",   "ctd",      3.3,  1.5,  0.0,  0.75, 1, 1, (3, 2)),         # [SHEET]
        ("TDS_conductivity","ctd",      5.0,  6.0,  3.0,  1.0,  1, 2, (3, 2)),         # [SHEET] I,V ; warm-up [ASSUMED]; 2 ch = cond + derived salinity
        ("depth_pressure",  "ctd",      3.3,  1.0,  0.02, 0.2,  1, 1, (3, 2)),         # [ASSUMED] not in sheet
        ("pH",              "ph",       5.0, 10.0,  0.0, 60.0,  1, 1, (3,)),           # [SHEET] 5-10 mA, <=60 s
        ("turbidity",       "turb",     5.0, 30.0,  1.0,  0.5,  1, 1, (3,)),           # [SHEET] 30 mA max, <500 ms
        ("DO_optical",      "do",       3.3, 15.0,  2.0,  8.0,  1, 1, (3,)),           # [ASSUMED] not in sheet
        ("SHT20_air",       "atmos",    3.3,  0.33, 0.0,  0.1,  1, 2, (3, 2, 1)),      # [SHEET]
        ("BMP280",          "atmos",    3.3,  1.1,  0.002,0.01, 1, 2, (3, 2, 1)),      # [SHEET]
        ("photodiode_irr",  "atmos",    3.3,  0.1,  0.0,  0.05, 1, 1, (3, 2)),         # [ASSUMED]
        ("DHT22_internal",  "internal", 3.3,  1.5,  0.0,  2.0,  1, 2, (3, 2)),         # [SHEET]
        ("DS18B20_batt_elec","internal",3.3,  1.5,  0.0,  0.75, 2, 1, (3, 2, 1)),     # [SHEET] x2 (battery, electronics)
        ("chlorophyll_a",   "chl",      5.0, 30.0,  5.0,  5.0,  1, 1, ()),             # optional - disabled by default
    ],
    mcu=dict(ua_per_mhz=84.0, f_mhz=24.0, v=3.3, sleep_ua=2.05,       # [SHEET] 84 uA/MHz, 2.05 uA stop
             awake_base_s=2.0, awake_per_sensor_s=0.1, imu_duty=0.2),
    imu=dict(v=3.3, i_ma=3.4, burst_s=60.0, odr_hz=10.0, bytes_per_sample=100),   # [SHEET] 3.4 mA ; burst design [ASSUMED]
    gnss=dict(
        model="neo6m",
        neo6m=dict(v=3.3, i_acq_ma=47.0, i_trk_ma=37.0, cold_s=27.0, hot_s=1.0, backup_ua=22.0),   # [SHEET]
        l89ha=dict(v=3.3, i_acq_ma=32.0, i_trk_ma=32.0, cold_s=13.0, hot_s=1.0, backup_ua=51.0),   # [SHEET]
        track_s=5.0, ephemeris_valid_h=4.0,                                                        # [ASSUMED]
    ),
    comm=dict(v=3.3, tx_ma=100.0, tx_s=105.0, rx_ma=20.0, rx_s=15.0, payload_bytes=240,        # [ASSUMED] Lacuna LoRa uplink - no current in sheet
              sleep_ua=0.0),
    sd=dict(v=3.3, write_ma=75.0, sleep_ma=22.5, power_gated=True,                              # [SHEET] write 50-100 mA, idle 15-30 mA
            on_s_base=0.5, throughput_Bps=1.0e6, capacity_gb=32.0, bytes_per_meas=100),         # [SHEET]
    sleep_ua_always_on_3v3=dict(rtc_in_mcu=0.0, imu=6.0, ds18b20=3.0, sht20=0.4, bmp280=0.1, dht22=40.0),  # only if sensor_power_gating False
    sensor_power_gating=True,                                                                   # [SLIDE] power gating
)

MODE_NAMES = {0: "OFF_BROWNOUT", 1: "DEEP_SLEEP", 2: "LOW_ENERGY_WINTER", 3: "NORMAL_OBSERVATION"}


# ----------------------------------------------------------------------------
# WEATHER + SOLAR
# ----------------------------------------------------------------------------
def load_weather(path, n_days):
    with open(path, "r", errors="ignore") as f:
        lines = f.read().splitlines()
    k = [i for i, l in enumerate(lines) if l.startswith("YEAR")][0]
    df = pd.read_csv(path, skiprows=k)
    df = df.iloc[: n_days * 24].reset_index(drop=True)
    df[df == -999] = np.nan
    df = df.interpolate().bfill().ffill()
    df["dt"] = pd.to_datetime(dict(year=df.YEAR, month=df.MO, day=df.DY, hour=df.HR))
    return df


def solar_position(lat_deg, t_h):
    """t_h = hours since Jan 1 00:00 local solar time (array). Returns cos_zenith, azimuth(rad from N), declination, E0."""
    lat = np.radians(lat_deg)
    n_day = t_h / 24.0
    G = 2 * np.pi * n_day / 365.0
    dec = (0.006918 - 0.399912 * np.cos(G) + 0.070257 * np.sin(G) - 0.006758 * np.cos(2 * G)
           + 0.000907 * np.sin(2 * G) - 0.002697 * np.cos(3 * G) + 0.00148 * np.sin(3 * G))
    E0 = 1.000110 + 0.034221 * np.cos(G) + 0.001280 * np.sin(G) + 0.000719 * np.cos(2 * G) + 0.000077 * np.sin(2 * G)
    h = np.radians(15.0 * ((t_h % 24.0) - 12.0))
    cz = np.sin(lat) * np.sin(dec) + np.cos(lat) * np.cos(dec) * np.cos(h)
    A_s = np.arctan2(np.sin(h), np.cos(h) * np.sin(lat) - np.tan(dec) * np.cos(lat))   # from south, west +
    az = (A_s + np.pi) % (2 * np.pi)                                                    # from north, clockwise
    return cz, az, dec, E0


def erbs_kd(kt):
    kt = np.clip(kt, 0, 1)
    return np.where(kt <= 0.22, 1 - 0.09 * kt,
           np.where(kt <= 0.80, 0.9511 - 0.1604 * kt + 4.388 * kt**2 - 16.638 * kt**3 + 12.336 * kt**4, 0.165))


def build_pv(cfg, wx):
    """Pre-compute everything that does not depend on the controller (solar geometry + PV potential)."""
    pv = cfg["pv"]
    dt_h = cfg["dt_min"] / 60.0
    spp = int(round(1.0 / dt_h))                  # steps per hour
    n_hours = len(wx)
    n = n_hours * spp
    t_mid = (np.arange(n) + 0.5) * dt_h
    hour_idx = np.minimum((t_mid // 1).astype(int), n_hours - 1)

    cz, az_s, dec, E0 = solar_position(cfg["site"]["lat"], t_mid)
    elev = np.degrees(np.arcsin(np.clip(cz, -1, 1)))
    cz_pos = np.clip(cz, 0, None)
    etr_h = 1361.0 * E0 * cz_pos                                   # extraterrestrial on horizontal (W/m2)

    # distribute hourly GHI over sub-steps proportional to ETR (energy preserving)
    ghi_h = wx["ALLSKY_SFC_SW_DWN"].values
    mean_etr = etr_h.reshape(n_hours, spp).mean(axis=1)
    mean_etr_rep = np.repeat(mean_etr, spp)
    ghi_hr_rep = np.repeat(ghi_h, spp)
    with np.errstate(divide="ignore", invalid="ignore"):
        ghi = np.where(mean_etr_rep > 1e-3, ghi_hr_rep * etr_h / mean_etr_rep, ghi_hr_rep)
    kt = np.where(etr_h > 1.0, ghi / np.maximum(etr_h, 1.0), 0.0)

    # diffuse split (Erbs) ; below 8 deg elevation blend to 100 % diffuse
    kd = erbs_kd(kt)
    w = np.clip(elev / 8.0, 0, 1)
    kd = 1 - w * (1 - kd)
    dhi = kd * ghi
    beam_h = ghi - dhi                                              # beam on horizontal
    cz_floor = np.maximum(cz, np.sin(np.radians(2.0)))

    month = pd.DatetimeIndex(wx["dt"]).month.values[hour_idx]
    albedo = np.array(pv["albedo_monthly"])[month - 1]
    t_air = np.interp(t_mid, np.arange(n_hours) + 0.5, wx["T2M"].values)

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

    p_stc = pv["v_mp"] * pv["i_mp"]
    tcell = t_air[:, None] + (pv["noct"] - 20.0) / 800.0 * poa
    tfac = 1 + pv["temp_coeff"] * (tcell - 25.0)
    lowlight = 1 - 0.04 * np.clip(1 - poa / 200.0, 0, 1)
    p_pan = p_stc * poa / 1000.0 * tfac * lowlight                  # W per panel at MPP

    if pv["wiring"] == "series_pairs":
        pairs = p_pan.reshape(n, npan // 2, 2)
        p_str = 2 * pairs.min(axis=2)
        p_dc = p_str.sum(axis=1)
    else:
        p_dc = p_pan.sum(axis=1)
    if pv["ice_loss_factor"] != 1.0:
        p_dc = np.where(t_air < pv["ice_loss_below_c"], p_dc * pv["ice_loss_factor"], p_dc)
    p_net = p_dc * pv["soiling"] * pv["motion"] * pv["wiring_eff"]
    p_avail = np.where(p_net >= pv["mppt_min_w"], p_net * pv["mppt_eff"], 0.0)   # W at MPPT output

    return dict(n=n, spp=spp, dt_h=dt_h, t_mid=t_mid, hour_idx=hour_idx, elev=elev, az=np.degrees(az_s),
                ghi=ghi, dhi=dhi, poa=poa, p_pan=p_pan, p_dc_raw=p_dc, p_avail=p_avail, t_air=t_air,
                rh=np.interp(t_mid, np.arange(n_hours) + 0.5, wx["RH2M"].values),
                ws=np.interp(t_mid, np.arange(n_hours) + 0.5, wx["WS10M"].values),
                albedo=albedo, etr=etr_h)


# ----------------------------------------------------------------------------
# SIMULATION
# ----------------------------------------------------------------------------
def thermal_ua(cfg, wind_ms):
    th = cfg["thermal"]
    r_stack = (th["hdpe_t_m"] / th["k_hdpe"] + th["gap_t_m"] / th["k_air"]
               + th["xps_t_m"] / th["k_xps"] + th["pc_t_m"] / th["k_pc"])
    h_air = 5.7 + 3.8 * th["wind_factor"] * wind_ms
    ua_w = th["area_m2"] * th["frac_water"] / (r_stack + 1 / th["h_water"]) + th["bridge_ua"] * th["frac_water"]
    ua_a = th["area_m2"] * (1 - th["frac_water"]) / (r_stack + 1 / h_air) + th["bridge_ua"] * (1 - th["frac_water"])
    return ua_w, ua_a, r_stack


def sensor_cycle(cfg, mode):
    """Energy (J at the rail) by group for one sensor cycle in a given mode + MCU awake time + #measurements."""
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


def run(cfg, wx, pvd, keep_step_log=True):
    B, PE, TH, HT, MD = cfg["batt"], cfg["pe"], cfg["thermal"], cfg["heater"], cfg["modes"]
    dt_h, n = pvd["dt_h"], pvd["n"]
    dt_s = dt_h * 3600.0
    Cn = B["nominal_wh"]
    cap_T, cap_pct = B["cap_vs_temp"]
    f_cap = lambda T: np.interp(T, cap_T, cap_pct) / 100.0
    lam = -math.log(1 - B["self_discharge_per_month"]) / (30 * 24)

    # sea temperature
    doy = pvd["t_mid"] / 24.0 + 1
    sst = np.clip(TH["sst_mean"] + TH["sst_amp"] * np.cos(2 * np.pi * (doy - TH["sst_peak_doy"]) / 365.0), TH["sst_floor"], 5)

    # constant schedules / energies
    gcfg = cfg["gnss"]; gm = gcfg[gcfg["model"]]
    cm = cfg["comm"]; sd = cfg["sd"]; imu = cfg["imu"]; m = cfg["mcu"]
    mcu_run_w = m["v"] * m["ua_per_mhz"] * m["f_mhz"] * 1e-6

    # baseline always-on current (3.3 V rail)
    base_ua = m["sleep_ua"] + gm["backup_ua"] + cm["sleep_ua"]
    if not cfg["sensor_power_gating"]:
        base_ua += sum(cfg["sleep_ua_always_on_3v3"].values())
    base_w_rail = base_ua * 1e-6 * 3.3
    if not sd["power_gated"]:
        base_w_rail += sd["sleep_ma"] * 1e-3 * sd["v"]

    # state
    E = B["soc0"] * Cn
    T = TH["t_enc0"]
    mode = 3 if B["soc0"] > MD["low_enter_soc"] else 2
    heater_on = False; heater_reason = ""
    next_due = dict(sensors=0.0, imu=0.05, gnss=0.0, comm=1.0)
    last_fix = -1e9
    last_alert = -1e9
    prev_charge_ok = True
    storage_bytes = 0.0
    harvest_hist = []                                  # last 72 h hourly PV potential (Wh)
    hour_acc = 0.0
    events, tasks = [], []

    def ts(t_h):
        return (pd.Timestamp("2025-01-01") + pd.Timedelta(hours=float(t_h))).strftime("%Y-%m-%d %H:%M")

    def ev(t_h, kind, detail, **kw):
        events.append(dict(time=ts(t_h), t_hours=round(float(t_h), 3), event=kind, detail=detail, **kw))

    # step log arrays
    cols = ["pv_avail_wh", "pv_direct_wh", "charged_wh", "curtailed_wh", "load_bat_wh", "batt_discharge_wh",
            "selfdis_wh", "conv_loss_wh", "quiescent_wh", "heater_wh_rail", "unmet_wh",
            "e_mcu", "e_imu", "e_gnss", "e_comm", "e_sd", "e_ctd", "e_ph", "e_turb", "e_do", "e_atmos", "e_internal", "e_chl",
            "e_base_sleep", "heat_in_w", "ua_w", "ua_a"]
    L = {c: np.zeros(n) for c in cols}
    S = {c: np.zeros(n) for c in ["E", "soc_pct", "soc_nom_pct", "t_enc", "mode", "heater_on", "charge_ok", "volt", "curr",
                                   "sst", "storage_mb", "n_sensor", "n_imu", "n_gnss", "n_comm"]}

    for i in range(n):
        t = i * dt_h
        t_end = t + dt_h
        Ta, Ts, ws = pvd["t_air"][i], sst[i], pvd["ws"][i]
        pv_w = pvd["p_avail"][i]
        pv_wh = pv_w * dt_h
        feq = f_cap(T)
        E_eff = Cn * feq
        soc = min(1.0, E / E_eff) if E_eff > 0 else 0.0

        # ---- harvest history (72 h rolling, hourly resolution) ----
        hour_acc += pv_wh
        if (i + 1) % pvd["spp"] == 0:
            harvest_hist.append(hour_acc); hour_acc = 0.0
            if len(harvest_hist) > 72: harvest_hist.pop(0)
        harv_day = (sum(harvest_hist) / len(harvest_hist)) * 24.0 if len(harvest_hist) >= 72 else 1e9   # need a full 72 h window

        # ---- mode controller ----
        old_mode = mode
        if mode == 0:
            if soc >= B["restart_soc"] and pv_w > 0:
                mode = 1
        else:
            if soc <= B["cutoff_soc"]:
                mode = 0
            elif mode == 1:
                if soc >= MD["deep_exit_soc"]:
                    mode = 2
            else:
                if soc < MD["deep_enter_soc"]:
                    mode = 1
                elif mode == 3:
                    if soc < MD["low_enter_soc"] or (harv_day < MD["low_enter_harvest_wh_day"] and soc < MD["low_harvest_soc_gate"]):
                        mode = 2
                elif mode == 2:
                    if soc >= MD["low_exit_soc"] and harv_day >= MD["low_exit_harvest_wh_day"]:
                        mode = 3
        if mode != old_mode:
            ev(t, "MODE_CHANGE", f"{MODE_NAMES[old_mode]} -> {MODE_NAMES[mode]}", soc_pct=round(soc * 100, 1),
               t_enc=round(T, 1), harvest_wh_day=round(harv_day, 1) if harv_day < 1e8 else None)
            if mode == 0:
                last_fix = -1e9   # GNSS backup lost
            if mode > 0:
                for k in next_due:
                    iv = MD["intervals_h"][k].get(mode, 0.0)
                    next_due[k] = (t if old_mode == 0 else min(next_due[k], t + iv)) if iv > 0 else math.inf
            if mode == 1 and old_mode >= 2 and t - last_alert >= MD["alert_min_gap_h"]:
                next_due["comm"] = t; last_alert = t
                ev(t, "ALERT_QUEUED", "critical-SOC alert transmission scheduled")
            if old_mode == 0 and mode == 1:
                next_due["comm"] = t
                ev(t, "RECOVERY", "restarted after brown-out, status packet queued")

        # ---- scheduled tasks ----
        e5_task = 0.0
        e = {g: 0.0 for g in ["mcu", "imu", "gnss", "comm", "sd", "ctd", "ph", "turb", "do", "atmos", "internal", "chl"]}
        n_s = n_i = n_g = n_c = 0
        if mode > 0:
            iv = MD["intervals_h"]
            for k in next_due:
                if iv[k].get(mode, 0.0) > 0 and next_due[k] > t + iv[k][mode]:
                    next_due[k] = t + iv[k][mode]
                if iv[k].get(mode, 0.0) == 0:
                    next_due[k] = math.inf
            guard = 0
            while next_due["sensors"] < t_end and guard < 10:
                td = next_due["sensors"]; guard += 1
                grp, e_mcu, n_meas, e5c = sensor_cycle(cfg, mode)
                e5_task += e5c
                byt = n_meas * sd["bytes_per_meas"] + sd["bytes_per_meas"]
                on_s = sd["on_s_base"] + byt / sd["throughput_Bps"]
                e_sd = sd["v"] * sd["write_ma"] * 1e-3 * on_s
                for g_, v_ in grp.items(): e[g_] += v_
                e["mcu"] += e_mcu; e["sd"] += e_sd; storage_bytes += byt; n_s += 1
                tasks.append(dict(time=ts(td), t_hours=round(td, 3), task="SENSOR_CYCLE", mode=MODE_NAMES[mode],
                                  energy_J=round(sum(grp.values()) + e_mcu + e_sd, 3), bytes=byt,
                                  **{f"J_{k_}": round(v_, 3) for k_, v_ in grp.items()}, J_mcu=round(e_mcu, 3), J_sd=round(e_sd, 3)))
                next_due["sensors"] = td + iv["sensors"][mode]
            guard = 0
            while next_due["imu"] < t_end and iv["imu"].get(mode, 0) > 0 and guard < 10:
                td = next_due["imu"]; guard += 1
                e_imu = imu["v"] * imu["i_ma"] * 1e-3 * imu["burst_s"]
                e_mcu = mcu_run_w * imu["burst_s"] * m["imu_duty"]
                byt = imu["burst_s"] * imu["odr_hz"] * imu["bytes_per_sample"]
                on_s = sd["on_s_base"] + byt / sd["throughput_Bps"]
                e_sd = sd["v"] * sd["write_ma"] * 1e-3 * on_s
                e["imu"] += e_imu; e["mcu"] += e_mcu; e["sd"] += e_sd; storage_bytes += byt; n_i += 1
                tasks.append(dict(time=ts(td), t_hours=round(td, 3), task="IMU_BURST", mode=MODE_NAMES[mode],
                                  energy_J=round(e_imu + e_mcu + e_sd, 3), bytes=byt, J_imu=round(e_imu, 3),
                                  J_mcu=round(e_mcu, 3), J_sd=round(e_sd, 3)))
                next_due["imu"] = td + iv["imu"][mode]
            guard = 0
            while next_due["gnss"] < t_end and guard < 10:
                td = next_due["gnss"]; guard += 1
                cold = (td - last_fix) > gcfg["ephemeris_valid_h"]
                acq = gm["cold_s"] if cold else gm["hot_s"]
                e_g = gm["v"] * (gm["i_acq_ma"] * 1e-3 * acq + gm["i_trk_ma"] * 1e-3 * gcfg["track_s"])
                e_mcu = mcu_run_w * 2.0
                byt = sd["bytes_per_meas"]
                e_sd = sd["v"] * sd["write_ma"] * 1e-3 * (sd["on_s_base"] + byt / sd["throughput_Bps"])
                e["gnss"] += e_g; e["mcu"] += e_mcu; e["sd"] += e_sd; storage_bytes += byt; n_g += 1
                last_fix = td
                tasks.append(dict(time=ts(td), t_hours=round(td, 3), task="GNSS_FIX", mode=MODE_NAMES[mode],
                                  energy_J=round(e_g + e_mcu + e_sd, 3), bytes=byt, start_type="cold" if cold else "hot",
                                  time_to_fix_s=acq, J_gnss=round(e_g, 3), J_mcu=round(e_mcu, 3), J_sd=round(e_sd, 3)))
                next_due["gnss"] = td + iv["gnss"][mode]
            guard = 0
            while next_due["comm"] < t_end and guard < 10:
                td = next_due["comm"]; guard += 1
                e_c = cm["v"] * (cm["tx_ma"] * 1e-3 * cm["tx_s"] + cm["rx_ma"] * 1e-3 * cm["rx_s"])
                e_mcu = mcu_run_w * (cm["tx_s"] + cm["rx_s"]) * 0.3
                e["comm"] += e_c; e["mcu"] += e_mcu; n_c += 1
                tasks.append(dict(time=ts(td), t_hours=round(td, 3), task="SAT_COMM_WINDOW", mode=MODE_NAMES[mode],
                                  energy_J=round(e_c + e_mcu, 3), bytes=cm["payload_bytes"], J_comm=round(e_c, 3), J_mcu=round(e_mcu, 3)))
                next_due["comm"] = td + iv["comm"][mode]

        # ---- baseline sleep loads + margin ----
        e_base = base_w_rail * dt_s if mode > 0 else 0.0
        mg = cfg.get("load_margin", 1.0)
        if mg != 1.0:
            for k_ in e: e[k_] *= mg
            e5_task *= mg; e_base *= mg
        rail_J = {g: e[g] for g in e}
        e5 = e5_task                                   # joules drawn from the 5 V rail (pH, turbidity, TDS, chl-a)
        e33 = sum(e.values()) - e5 + e_base            # everything else on the 3.3 V rail
        e_rail_total_J = e5 + e33

        # ---- heater decision ----
        P_h = HT["power_w"]
        batt_h_w = P_h / PE["eta_5v"]
        load_bat_w_est = (e33 / PE["eta_3v3"] + e5 / PE["eta_5v"]) / dt_s + PE["quiescent_w"]
        surplus_w = pv_w - load_bat_w_est
        charge_ok = B["allow_cold_charge"] or (T >= B["charge_min_temp_c"])
        was_on = heater_on
        if mode == 0:
            heater_on = False; reason = ""
        elif HT["policy"] == "thermostat":
            if heater_on:
                heater_on = T < HT["thermo_off_c"] and soc > HT["thermo_min_soc"]
            else:
                heater_on = T < HT["thermo_on_c"] and soc > HT["thermo_min_soc"]
            reason = "thermostat"
        else:
            protect = False; assist = False
            if heater_reason == "protect" and heater_on:
                protect = T < HT["protect_off_c"] and soc > HT["protect_min_soc"]
            else:
                protect = T < HT["protect_on_c"] and soc > HT["protect_min_soc"]
            if mode >= 2:
                if heater_reason == "assist" and heater_on:
                    assist = T < HT["assist_off_c"] and surplus_w >= HT["assist_pv_hold_frac"] * batt_h_w and soc < HT["assist_max_soc"] + 0.02
                else:
                    assist = T < HT["assist_on_c"] and surplus_w >= HT["assist_pv_on_frac"] * batt_h_w and soc < HT["assist_max_soc"]
            heater_on = protect or assist
            reason = "protect" if protect else ("assist" if assist else "")
        if heater_on != was_on:
            ev(t, "HEATER_ON" if heater_on else "HEATER_OFF", reason if heater_on else heater_reason,
               t_enc=round(T, 2), soc_pct=round(soc * 100, 1), pv_w=round(pv_w, 2))
        heater_reason = reason
        e_heater_J = P_h * dt_s if heater_on else 0.0

        # ---- energy balance ----
        load_bat_wh = ((e33 / PE["eta_3v3"] + e5 / PE["eta_5v"] + e_heater_J / PE["eta_5v"]) / 3600.0
                       + (PE["quiescent_w"] * dt_h if mode > 0 else 0.0))
        rail_wh = (e_rail_total_J + e_heater_J) / 3600.0
        conv_loss_wh = load_bat_wh - rail_wh - (PE["quiescent_w"] * dt_h if mode > 0 else 0.0)
        direct = min(pv_wh, load_bat_wh)
        surplus = pv_wh - direct
        deficit = load_bat_wh - direct
        E_max = E_eff
        if charge_ok and surplus > 0:
            room = max(0.0, E_max - E)
            charged = min(surplus * B["charge_eff"], room)
            curtailed = surplus - charged / B["charge_eff"]
        else:
            charged, curtailed = 0.0, surplus
        draw = deficit / B["discharge_eff"]
        unmet = 0.0
        E_new = E + charged - draw
        if E_new < 0:
            unmet = -E_new * B["discharge_eff"]
            E_new = 0.0
        selfd = E_new * (1 - math.exp(-lam * dt_h))
        E_new -= selfd
        if charge_ok != prev_charge_ok:
            ev(t, "CHARGE_ALLOWED" if charge_ok else "CHARGE_INHIBITED_COLD", f"T_batt={T:.1f} C", pv_w=round(pv_w, 2))
            prev_charge_ok = charge_ok
        if unmet > 1e-9:
            ev(t, "UNMET_LOAD", f"{unmet:.3f} Wh could not be supplied (battery empty)")

        # ---- thermal update ----
        inside_J = sum(rail_J[g] for g in TH["inside_groups"]) + e_base * (1.0 if mode > 0 else 0.0)
        heat_w = (inside_J + e_heater_J) / dt_s
        heat_w += (load_bat_wh - rail_wh) / dt_h                     # converter + quiescent loss inside box
        heat_w += pv_w * (1 / PV_ETA(cfg) - 1)                       # MPPT loss
        heat_w += (charged / B["charge_eff"] - charged) / dt_h + (draw - deficit) / dt_h   # battery losses
        ua_w, ua_a, _ = thermal_ua(cfg, ws)
        ua = ua_w + ua_a
        T_inf = (ua_w * Ts + ua_a * Ta + heat_w) / ua
        tau = TH["heat_capacity_j_k"] / ua
        T = T_inf + (T - T_inf) * math.exp(-dt_s / tau)
        if T < B["op_temp_range"][0] or T > B["op_temp_range"][1]:
            if not events or events[-1].get("event") != "BATTERY_OUT_OF_SPEC_TEMP":
                ev(t, "BATTERY_OUT_OF_SPEC_TEMP", f"T_batt={T:.1f} C outside {B['op_temp_range']}")

        E = E_new
        V_ocv = 4 * np.interp(min(E / Cn, 1.0), B["ocv_soc"], B["ocv_cell"])
        I_batt = (charged - draw) / dt_h / max(V_ocv, 1.0)
        V_term = V_ocv + I_batt * B["r_int_ohm"]

        # ---- logging ----
        L["pv_avail_wh"][i] = pv_wh; L["pv_direct_wh"][i] = direct; L["charged_wh"][i] = charged
        L["curtailed_wh"][i] = curtailed; L["load_bat_wh"][i] = load_bat_wh; L["batt_discharge_wh"][i] = draw
        L["selfdis_wh"][i] = selfd; L["conv_loss_wh"][i] = conv_loss_wh
        L["quiescent_wh"][i] = PE["quiescent_w"] * dt_h if mode > 0 else 0.0
        L["heater_wh_rail"][i] = e_heater_J / 3600.0; L["unmet_wh"][i] = unmet
        for g in e: L["e_" + g][i] = e[g] / 3600.0
        L["e_base_sleep"][i] = e_base / 3600.0
        L["heat_in_w"][i] = heat_w; L["ua_w"][i] = ua_w; L["ua_a"][i] = ua_a
        S["E"][i] = E; S["soc_pct"][i] = 100 * min(1.0, E / (Cn * f_cap(T)))
        S["soc_nom_pct"][i] = 100 * E / Cn; S["t_enc"][i] = T; S["mode"][i] = mode
        S["heater_on"][i] = 1.0 if heater_on else 0.0; S["charge_ok"][i] = 1.0 if charge_ok else 0.0
        S["volt"][i] = V_term; S["curr"][i] = I_batt; S["sst"][i] = Ts
        S["storage_mb"][i] = storage_bytes / 1e6
        S["n_sensor"][i] = n_s; S["n_imu"][i] = n_i; S["n_gnss"][i] = n_g; S["n_comm"][i] = n_c

    return L, S, events, tasks, sst


def PV_ETA(cfg):
    return cfg["pv"]["mppt_eff"]


# ----------------------------------------------------------------------------
# POST-PROCESSING
# ----------------------------------------------------------------------------
def build_logs(cfg, wx, pvd, L, S, sst):
    n, spp = pvd["n"], pvd["spp"]
    dt_h = pvd["dt_h"]
    t0 = pd.Timestamp("2025-01-01")
    times = t0 + pd.to_timedelta(np.arange(n) * dt_h, unit="h")
    step = pd.DataFrame(dict(
        time=times, t_hours=np.arange(n) * dt_h, solar_elev_deg=pvd["elev"], solar_az_deg=pvd["az"],
        ghi_wm2=pvd["ghi"], dhi_wm2=pvd["dhi"], poa_mean_wm2=pvd["poa"].mean(axis=1), poa_max_wm2=pvd["poa"].max(axis=1),
        t_air_c=pvd["t_air"], rh_pct=pvd["rh"], wind_10m_ms=pvd["ws"], t_sea_c=sst, albedo=pvd["albedo"],
        pv_potential_w=pvd["p_avail"] , **L, **S))
    step["mode_name"] = step["mode"].map(MODE_NAMES)
    step["pv_potential_w"] = step["pv_avail_wh"] / dt_h

    # hourly
    step["hour"] = np.arange(n) // spp
    sums = [c for c in L if c not in ("heat_in_w", "ua_w", "ua_a")] + ["n_sensor", "n_imu", "n_gnss", "n_comm"]
    means = ["solar_elev_deg", "solar_az_deg", "ghi_wm2", "dhi_wm2", "poa_mean_wm2", "poa_max_wm2", "t_air_c", "rh_pct",
             "wind_10m_ms", "t_sea_c", "heat_in_w", "volt", "curr", "pv_potential_w", "ua_w", "ua_a"]
    g = step.groupby("hour")
    h = g[means].mean()
    for c in sums: h[c] = g[c].sum()
    h["time"] = g["time"].first()
    h["soc_pct"] = g["soc_pct"].last(); h["soc_nom_pct"] = g["soc_nom_pct"].last(); h["E_stored_wh"] = g["E"].last()
    h["t_enc_c_end"] = g["t_enc"].last(); h["t_enc_c_min"] = g["t_enc"].min(); h["t_enc_c_max"] = g["t_enc"].max()
    h["mode"] = g["mode"].last(); h["mode_name"] = h["mode"].map(MODE_NAMES)
    h["heater_on_min"] = g["heater_on"].sum() * cfg["dt_min"]
    h["charge_blocked_min"] = (1 - g["charge_ok"].mean()) * 60
    h["storage_mb"] = g["storage_mb"].last()
    h["data_gap_min"] = (g["mode"].apply(lambda s: (s == 0).sum())) * cfg["dt_min"]
    h = h.rename(columns={"pv_avail_wh": "pv_potential_wh", "charged_wh": "batt_charged_wh", "e_mcu": "load_mcu_wh",
                          "e_imu": "load_imu_wh", "e_gnss": "load_gnss_wh", "e_comm": "load_comm_wh", "e_sd": "load_sd_wh",
                          "e_ctd": "load_ctd_wh", "e_ph": "load_ph_wh", "e_turb": "load_turbidity_wh", "e_do": "load_do_wh",
                          "e_atmos": "load_atmos_wh", "e_internal": "load_internal_wh", "e_chl": "load_chl_wh",
                          "e_base_sleep": "load_sleep_wh", "heater_wh_rail": "load_heater_wh", "heat_in_w": "heat_in_avg_w",
                          "n_sensor": "n_sensor_cycles", "n_imu": "n_imu_bursts", "n_gnss": "n_gnss_fixes", "n_comm": "n_comm_windows",
                          "volt": "batt_voltage_v", "curr": "batt_current_a", "ua_w": "UA_water_WK", "ua_a": "UA_air_WK"})
    h["dt_local_solar"] = h["time"]
    h = h.reset_index(drop=True)
    first = ["time", "ghi_wm2", "dhi_wm2", "poa_mean_wm2", "poa_max_wm2", "solar_elev_deg", "solar_az_deg", "t_air_c", "rh_pct",
             "wind_10m_ms", "t_sea_c", "pv_potential_wh", "pv_direct_wh", "batt_charged_wh", "curtailed_wh",
             "soc_pct", "soc_nom_pct", "E_stored_wh", "batt_voltage_v", "batt_current_a", "t_enc_c_end", "t_enc_c_min", "t_enc_c_max",
             "mode", "mode_name", "heater_on_min", "charge_blocked_min", "data_gap_min"]
    h = h[first + [c for c in h.columns if c not in first and c not in ("dt_local_solar", "albedo")]]
    h["load_total_rail_wh"] = h[[c for c in h.columns if c.startswith("load_") and c.endswith("_wh") and c not in ("load_bat_wh",)]].sum(axis=1)
    num = h.select_dtypes("number").columns; h[num] = h[num].round(5)

    # panel log
    pan = pd.DataFrame({f"P{k+1}_w": pvd["p_pan"][:, k] for k in range(pvd["p_pan"].shape[1])})
    pan.insert(0, "hour", step["hour"].values)
    pan_h = pan.groupby("hour").mean()
    pan_h.insert(0, "time", h["time"].values)
    for k in range(pvd["poa"].shape[1]):
        pan_h[f"POA{k+1}_wm2"] = pd.DataFrame(pvd["poa"][:, k]).groupby(step["hour"].values).mean().values[:, 0]
    num = pan_h.select_dtypes("number").columns; pan_h[num] = pan_h[num].round(4)

    # daily
    h["date"] = pd.to_datetime(h["time"]).dt.date
    dg = h.groupby("date")
    d = pd.DataFrame(dict(
        ghi_kwh_m2=dg["ghi_wm2"].sum() / 1000, t_air_mean_c=dg["t_air_c"].mean(), t_air_min_c=dg["t_air_c"].min(),
        t_sea_c=dg["t_sea_c"].mean(),
        pv_potential_wh=dg["pv_potential_wh"].sum(), pv_direct_wh=dg["pv_direct_wh"].sum(), batt_charged_wh=dg["batt_charged_wh"].sum(),
        curtailed_wh=dg["curtailed_wh"].sum(), load_bat_wh=dg["load_bat_wh"].sum(), batt_discharge_wh=dg["batt_discharge_wh"].sum(),
        heater_wh_rail=dg["load_heater_wh"].sum(), heater_hours=dg["heater_on_min"].sum() / 60,
        selfdischarge_wh=dg["selfdis_wh"].sum(), conv_loss_wh=dg["conv_loss_wh"].sum(),
        sensors_wh=dg[["load_ctd_wh", "load_ph_wh", "load_turbidity_wh", "load_do_wh", "load_atmos_wh", "load_internal_wh", "load_chl_wh"]].sum().sum(axis=1),
        mcu_wh=dg["load_mcu_wh"].sum(), imu_wh=dg["load_imu_wh"].sum(), gnss_wh=dg["load_gnss_wh"].sum(),
        comm_wh=dg["load_comm_wh"].sum(), sd_wh=dg["load_sd_wh"].sum(), sleep_wh=dg["load_sleep_wh"].sum(),
        unmet_wh=dg["unmet_wh"].sum(),
        soc_end_pct=dg["soc_pct"].last(), soc_min_pct=dg["soc_pct"].min(), soc_max_pct=dg["soc_pct"].max(),
        t_enc_min_c=dg["t_enc_c_min"].min(), t_enc_max_c=dg["t_enc_c_max"].max(), t_enc_mean_c=dg["t_enc_c_end"].mean(),
        charge_blocked_hours=dg["charge_blocked_min"].sum() / 60, data_gap_hours=dg["data_gap_min"].sum() / 60,
        hours_normal=dg["mode"].apply(lambda s: (s == 3).sum()), hours_low_energy=dg["mode"].apply(lambda s: (s == 2).sum()),
        hours_deep_sleep=dg["mode"].apply(lambda s: (s == 1).sum()), hours_off=dg["mode"].apply(lambda s: (s == 0).sum()),
        n_sensor_cycles=dg["n_sensor_cycles"].sum(), n_gnss_fixes=dg["n_gnss_fixes"].sum(), n_comm_windows=dg["n_comm_windows"].sum(),
        n_imu_bursts=dg["n_imu_bursts"].sum(), storage_mb=dg["storage_mb"].last(),
    ))
    d["net_wh"] = d["batt_charged_wh"] - d["batt_discharge_wh"] - d["selfdischarge_wh"]
    d["load_no_heater_wh"] = d["load_bat_wh"] - d["heater_wh_rail"] / cfg["pe"]["eta_5v"]
    d["autonomy_days_at_7d_load"] = (h.groupby("date")["E_stored_wh"].last() /
                                      (d["load_no_heater_wh"].rolling(7, min_periods=1).mean() + 1e-9))
    d["day_of_year"] = np.arange(1, len(d) + 1)
    num = d.select_dtypes("number").columns; d[num] = d[num].round(4); d = d.reset_index()

    # monthly
    d["month"] = pd.to_datetime(d["date"]).dt.month
    mg = d.groupby("month")
    mo = pd.DataFrame(dict(
        ghi_kwh_m2=mg["ghi_kwh_m2"].sum(), t_air_mean_c=mg["t_air_mean_c"].mean(), t_air_min_c=mg["t_air_min_c"].min(),
        pv_potential_wh=mg["pv_potential_wh"].sum(), batt_charged_wh=mg["batt_charged_wh"].sum(), curtailed_wh=mg["curtailed_wh"].sum(),
        load_bat_wh=mg["load_bat_wh"].sum(), heater_wh_rail=mg["heater_wh_rail"].sum(), heater_hours=mg["heater_hours"].sum(),
        selfdischarge_wh=mg["selfdischarge_wh"].sum(), sensors_wh=mg["sensors_wh"].sum(), gnss_wh=mg["gnss_wh"].sum(),
        comm_wh=mg["comm_wh"].sum(), mcu_wh=mg["mcu_wh"].sum(), sd_wh=mg["sd_wh"].sum(), imu_wh=mg["imu_wh"].sum(),
        soc_end_pct=mg["soc_end_pct"].last(), soc_min_pct=mg["soc_min_pct"].min(),
        t_enc_min_c=mg["t_enc_min_c"].min(), t_enc_mean_c=mg["t_enc_mean_c"].mean(),
        charge_blocked_hours=mg["charge_blocked_hours"].sum(), data_gap_hours=mg["data_gap_hours"].sum(),
        hours_normal=mg["hours_normal"].sum(), hours_low_energy=mg["hours_low_energy"].sum(),
        hours_deep_sleep=mg["hours_deep_sleep"].sum(), hours_off=mg["hours_off"].sum(),
    )).round(3).reset_index()
    return step, h, pan_h, d, mo


def subsystem_summary(h, d):
    rows = []
    mapping = [("MCU (STM32)", "load_mcu_wh"), ("IMU", "load_imu_wh"), ("GNSS", "load_gnss_wh"), ("Satellite modem", "load_comm_wh"),
               ("SD card", "load_sd_wh"), ("Water T / conductivity / depth", "load_ctd_wh"), ("pH", "load_ph_wh"),
               ("Turbidity", "load_turbidity_wh"), ("Dissolved oxygen", "load_do_wh"), ("Air T/RH/P/irradiance", "load_atmos_wh"),
               ("Internal sensors", "load_internal_wh"), ("Chlorophyll-a", "load_chl_wh"), ("Always-on sleep currents", "load_sleep_wh"),
               ("Heater (rail side)", "load_heater_wh")]
    for name, col in mapping:
        rows.append(dict(subsystem=name, annual_wh_rail_side=h[col].sum()))
    rows.append(dict(subsystem="Converter losses", annual_wh_rail_side=h["conv_loss_wh"].sum()))
    rows.append(dict(subsystem="BMS/MPPT quiescent", annual_wh_rail_side=h["quiescent_wh"].sum()))
    rows.append(dict(subsystem="Battery self-discharge", annual_wh_rail_side=h["selfdis_wh"].sum()))
    df = pd.DataFrame(rows)
    tot = df["annual_wh_rail_side"].sum()
    df["share_pct"] = 100 * df["annual_wh_rail_side"] / tot if tot > 0 else 0
    return df.round(3)


def kpis(cfg, h, d, events, S):
    ok = len(d)
    soc_min = d["soc_min_pct"].min()
    dmin = d.loc[d["soc_min_pct"].idxmin(), "date"]
    cons = d["batt_discharge_wh"].sum() + d["selfdischarge_wh"].sum()
    E_end = S["E"][-1]; E_start = cfg["batt"]["soc0"] * cfg["batt"]["nominal_wh"]
    closure = (E_end - E_start) - (d["batt_charged_wh"].sum() - d["batt_discharge_wh"].sum() - d["selfdischarge_wh"].sum() + d["unmet_wh"].sum() * 0)
    return dict(
        scenario=cfg["name"],
        pv_potential_kwh_year=round(d["pv_potential_wh"].sum() / 1000, 3),
        pv_to_battery_wh=round(d["batt_charged_wh"].sum(), 1),
        curtailed_wh=round(d["curtailed_wh"].sum(), 1),
        total_load_battery_side_wh=round(d["load_bat_wh"].sum(), 1),
        heater_wh_rail=round(d["heater_wh_rail"].sum(), 1), heater_hours=round(d["heater_hours"].sum(), 1),
        sensing_comm_logging_wh=round(d[["sensors_wh", "mcu_wh", "imu_wh", "gnss_wh", "comm_wh", "sd_wh", "sleep_wh"]].sum().sum(), 1),
        selfdischarge_wh=round(d["selfdischarge_wh"].sum(), 1),
        min_soc_pct=round(soc_min, 1), min_soc_date=str(dmin), end_soc_pct=round(d["soc_end_pct"].iloc[-1], 1),
        hours_normal=int(d["hours_normal"].sum()), hours_low_energy=int(d["hours_low_energy"].sum()),
        hours_deep_sleep=int(d["hours_deep_sleep"].sum()), hours_off_brownout=int(d["hours_off"].sum()),
        data_gap_hours=round(d["data_gap_hours"].sum(), 1),
        charge_blocked_hours=round(d["charge_blocked_hours"].sum(), 1),
        unmet_load_wh=round(d["unmet_wh"].sum(), 3),
        t_enc_min_c=round(d["t_enc_min_c"].min(), 1), t_enc_max_c=round(d["t_enc_max_c"].max(), 1),
        sensor_cycles=int(d["n_sensor_cycles"].sum()), gnss_fixes=int(d["n_gnss_fixes"].sum()), comm_windows=int(d["n_comm_windows"].sum()),
        sd_used_mb=round(d["storage_mb"].iloc[-1], 1),
        energy_balance_residual_wh=round(float(closure), 4),
    )


# ----------------------------------------------------------------------------
# SCENARIOS
# ----------------------------------------------------------------------------
def scenarios():
    base = copy.deepcopy(CFG)
    out = {"baseline": base}
    c = copy.deepcopy(base); c["name"] = "spec_heater_8C"; c["heater"]["policy"] = "thermostat"; out[c["name"]] = c
    c = copy.deepcopy(base); c["name"] = "sd_not_power_gated"; c["sd"]["power_gated"] = False; out[c["name"]] = c
    c = copy.deepcopy(base); c["name"] = "panel_icing_70pct_below_-2C"; c["pv"]["ice_loss_factor"] = 0.3; out[c["name"]] = c
    c = copy.deepcopy(base); c["name"] = "high_rate_5min_sampling"
    c["modes"]["intervals_h"]["sensors"][3] = 5 / 60; c["modes"]["intervals_h"]["gnss"][3] = 0.25
    c["modes"]["intervals_h"]["comm"][3] = 1.0; out[c["name"]] = c
    c = copy.deepcopy(base); c["name"] = "no_ice_albedo_open_water"; c["pv"]["albedo_monthly"] = [0.08] * 12; out[c["name"]] = c
    c = copy.deepcopy(base); c["name"] = "L89HA_GNSS_plus_chlorophyll"; c["gnss"]["model"] = "l89ha"
    c["sensors"] = [(*s[:8], (3,)) if s[0] == "chlorophyll_a" else s for s in c["sensors"]]; out[c["name"]] = c
    return out


def save_all(cfg, outdir, wx, pvd, write_step=True):
    L, S, events, tasks, sst = run(cfg, wx, pvd)
    step, h, pan_h, d, mo = build_logs(cfg, wx, pvd, L, S, sst)
    return step, h, pan_h, d, mo, events, tasks, S


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weather", required=True)
    ap.add_argument("--out", default="logs")
    ap.add_argument("--scenarios", action="store_true", help="also run sensitivity scenarios")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    wx = load_weather(a.weather, CFG["n_days"])
    all_cfgs = scenarios() if a.scenarios else {"baseline": CFG}
    comp = []
    for name, cfg in all_cfgs.items():
        pvd = build_pv(cfg, wx)
        step, h, pan_h, d, mo, events, tasks, S = save_all(cfg, a.out, wx, pvd)
        k = kpis(cfg, h, d, events, S)
        comp.append(k)
        print(f"[{name}] min SOC {k['min_soc_pct']}% on {k['min_soc_date']} | heater {k['heater_wh_rail']} Wh | "
              f"brownout h {k['hours_off_brownout']} | load {k['total_load_battery_side_wh']} Wh")
        if name == "baseline":
            step.drop(columns=["hour"]).to_csv(os.path.join(a.out, "step_log_10min.csv.gz"), index=False, compression="gzip")
            h.drop(columns=["date"]).to_csv(os.path.join(a.out, "hourly_log.csv"), index=False)
            pan_h.to_csv(os.path.join(a.out, "pv_panels_hourly.csv"), index=False)
            d.to_csv(os.path.join(a.out, "daily_summary.csv"), index=False)
            mo.to_csv(os.path.join(a.out, "monthly_summary.csv"), index=False)
            subsystem_summary(h, d).to_csv(os.path.join(a.out, "subsystem_energy_annual.csv"), index=False)
            pd.DataFrame(events).to_csv(os.path.join(a.out, "event_log.csv"), index=False)
            pd.DataFrame(tasks).to_csv(os.path.join(a.out, "task_log.csv"), index=False)
            json.dump(cfg, open(os.path.join(a.out, "config_used.json"), "w"), indent=2, default=str)
            ua_w, ua_a, rs = thermal_ua(cfg, 0.0)
        else:
            os.makedirs(os.path.join(a.out, "scenarios"), exist_ok=True)
            d.to_csv(os.path.join(a.out, "scenarios", f"{name}_daily.csv"), index=False)
            h[["time", "soc_pct", "t_enc_c_end", "mode", "load_heater_wh", "pv_potential_wh"]].to_csv(
                os.path.join(a.out, "scenarios", f"{name}_hourly_core.csv"), index=False)
    if a.scenarios:
        pd.DataFrame(comp).to_csv(os.path.join(a.out, "scenario_comparison.csv"), index=False)
    print("done ->", a.out)


if __name__ == "__main__":
    main()
