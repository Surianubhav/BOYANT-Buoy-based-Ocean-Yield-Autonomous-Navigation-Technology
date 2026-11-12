#!/usr/bin/env python3
"""
BOYANT Results Visualizer
Turns BOYANT simulation CSV/JSON logs into presentation-friendly charts and
a self-contained HTML dashboard.

Usage:
    pip install pandas matplotlib numpy
    python3 visualize_boyant.py --logs logs_scenarios --out buoyant_report
"""

import argparse
import json
import math
from pathlib import Path
import textwrap

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def read_csv(path):
    return pd.read_csv(path) if path.exists() else None


def save_fig(fig, path):
    fig.tight_layout()
    fig.savefig(path, dpi=170, bbox_inches="tight")
    plt.close(fig)


def nice_name(value):
    return str(value).replace("_", " ").replace("L89HA", "L89HA")


def main():
    parser = argparse.ArgumentParser(description="Create a visual BOYANT simulation report.")
    parser.add_argument("--logs", default="logs", help="Folder containing BOYANT output CSV/JSON files")
    parser.add_argument("--out", default="boyant_report", help="Folder for generated charts and HTML report")
    args = parser.parse_args()

    logs = Path(args.logs).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    daily = read_csv(logs / "daily_summary.csv")
    monthly = read_csv(logs / "monthly_summary.csv")
    hourly = read_csv(logs / "hourly_log.csv")
    subsystems = read_csv(logs / "subsystem_energy_annual.csv")
    scenarios = read_csv(logs / "scenario_comparison.csv")
    if daily is None or hourly is None:
        raise SystemExit("Could not find daily_summary.csv and hourly_log.csv in: " + str(logs))

    kpi_path = logs / "kpis.json"
    kpis = json.loads(kpi_path.read_text()) if kpi_path.exists() else {}

    # Parse dates safely.
    daily["date"] = pd.to_datetime(daily["date"])
    hourly["time"] = pd.to_datetime(hourly["time"])
    if monthly is not None and "month" in monthly:
        monthly["month"] = monthly["month"].astype(str)

    images = []

    # 1. Battery state of charge over the year.
    fig, ax = plt.subplots(figsize=(12, 4.8))
    ax.plot(daily["date"], daily["soc_min_pct"], label="Daily minimum SOC", linewidth=1.8)
    ax.plot(daily["date"], daily["soc_end_pct"], label="End-of-day SOC", linewidth=1.5, alpha=0.85)
    ax.axhline(20, linestyle="--", linewidth=1, label="20% reference")
    ax.set(title="Battery charge throughout the simulated year",
           xlabel="Date", ylabel="Battery state of charge (%)")
    ax.set_ylim(0, 105)
    ax.grid(True, alpha=.25)
    ax.legend(ncol=3)
    p = out / "01_battery_soc.png"; save_fig(fig, p); images.append(p.name)

    # 2. Battery and enclosure temperature.
    fig, ax = plt.subplots(figsize=(12, 4.8))
    ax.plot(daily["date"], daily["t_batt_min_c"], label="Battery daily minimum", linewidth=1.6)
    ax.plot(daily["date"], daily["t_batt_mean_c"], label="Battery daily mean", linewidth=1.4)
    if "t_enc_mean_c" in daily:
        ax.plot(daily["date"], daily["t_enc_mean_c"], label="Enclosure daily mean", linewidth=1.2, alpha=.8)
    ax.set(title="Thermal performance in the Southern Ocean environment",
           xlabel="Date", ylabel="Temperature (°C)")
    ax.grid(True, alpha=.25); ax.legend(ncol=3)
    p = out / "02_temperature.png"; save_fig(fig, p); images.append(p.name)

    # 3. Daily energy budget.
    fig, ax = plt.subplots(figsize=(12, 4.8))
    ax.plot(daily["date"], daily["pv_potential_wh"], label="Potential solar generation", linewidth=1.5)
    ax.plot(daily["date"], daily["load_bat_wh"], label="Battery-side load", linewidth=1.5)
    if "heater_wh_rail" in daily:
        ax.plot(daily["date"], daily["heater_wh_rail"], label="Heater energy", linewidth=1.2)
    ax.set(title="Daily energy generation versus energy demand",
           xlabel="Date", ylabel="Energy (Wh/day)")
    ax.grid(True, alpha=.25); ax.legend()
    p = out / "03_daily_energy.png"; save_fig(fig, p); images.append(p.name)

    # 4. Monthly seasonal profile.
    if monthly is not None and len(monthly):
        fig, ax = plt.subplots(figsize=(12, 4.8))
        x = np.arange(len(monthly))
        width = .38
        ax.bar(x - width/2, monthly["pv_potential_wh"], width, label="Potential solar")
        ax.bar(x + width/2, monthly["load_bat_wh"], width, label="Battery-side load")
        ax.set_xticks(x, monthly["month"], rotation=35, ha="right")
        ax.set(title="Seasonal energy profile by month", ylabel="Energy (Wh/month)")
        ax.grid(axis="y", alpha=.25); ax.legend()
        p = out / "04_monthly_energy.png"; save_fig(fig, p); images.append(p.name)

    # 5. Energy consumption by subsystem.
    if subsystems is not None and len(subsystems):
        subsystems = subsystems.sort_values("annual_wh", ascending=True)
        fig, ax = plt.subplots(figsize=(10, max(5, .36 * len(subsystems))))
        ax.barh(subsystems["subsystem"], subsystems["annual_wh"])
        ax.set(title="Where the buoy uses its energy", xlabel="Annual energy consumption (Wh)")
        ax.grid(axis="x", alpha=.25)
        for i, v in enumerate(subsystems["annual_wh"]):
            ax.text(v, i, f"  {v:.1f} Wh", va="center", fontsize=8)
        p = out / "05_subsystem_energy.png"; save_fig(fig, p); images.append(p.name)

    # 6. Operating mode hours.
    mode_cols = [c for c in ["hours_normal", "hours_low_energy", "hours_deep_sleep", "hours_off"] if c in daily.columns]
    if mode_cols:
        totals = daily[mode_cols].sum()
        fig, ax = plt.subplots(figsize=(9, 4.8))
        labels = [c.replace("hours_", "").replace("_", " ").title() for c in totals.index]
        bars = ax.bar(labels, totals.values)
        ax.set(title="How the buoy spent its 8,760 operating hours",
               ylabel="Hours during the year")
        ax.grid(axis="y", alpha=.25)
        for bar, value in zip(bars, totals.values):
            ax.text(bar.get_x()+bar.get_width()/2, bar.get_height(), f"{value:,.0f} h",
                    ha="center", va="bottom", fontsize=9)
        p = out / "06_operating_modes.png"; save_fig(fig, p); images.append(p.name)

    # 7. Scenario comparison: survival and minimum SOC.
    if scenarios is not None and len(scenarios):
        s = scenarios.copy()
        s["scenario_label"] = s["scenario"].map(nice_name)
        s = s.sort_values("min_soc_pct", ascending=True)
        fig, ax = plt.subplots(figsize=(12, max(6, .48 * len(s))))
        colors = ["#c0392b" if (not bool(v) or float(b) > 0) else "#27864b"
                  for v, b in zip(s["survived_full_period"], s["hours_off_brownout"])]
        bars = ax.barh(s["scenario_label"], s["min_soc_pct"], color=colors)
        ax.set(title="Stress-test scenarios: lowest battery charge reached",
               xlabel="Minimum state of charge (%)")
        ax.set_xlim(0, 105); ax.grid(axis="x", alpha=.25)
        for bar, val in zip(bars, s["min_soc_pct"]):
            ax.text(val + 1, bar.get_y()+bar.get_height()/2, f"{val:.1f}%",
                    va="center", fontsize=8)
        p = out / "07_scenario_min_soc.png"; save_fig(fig, p); images.append(p.name)

        if "hours_off_brownout" in s:
            s2 = s.sort_values("hours_off_brownout", ascending=True)
            fig, ax = plt.subplots(figsize=(12, max(6, .48 * len(s2))))
            bars = ax.barh(s2["scenario_label"], s2["hours_off_brownout"], color="#c0392b")
            ax.set(title="Stress-test scenarios: time spent in brownout/off state",
                   xlabel="Hours")
            ax.grid(axis="x", alpha=.25)
            for bar, val in zip(bars, s2["hours_off_brownout"]):
                if val > 0:
                    ax.text(val + max(s2["hours_off_brownout"].max()*.01, 1),
                            bar.get_y()+bar.get_height()/2, f"{val:,.0f} h",
                            va="center", fontsize=8)
            p = out / "08_scenario_brownout.png"; save_fig(fig, p); images.append(p.name)

    # 8. High-level hourly SOC and temperature trace, lightly downsampled.
    if "soc_pct" in hourly and "t_batt_c_end" in hourly:
        h = hourly.iloc[::6, :]
        fig, ax1 = plt.subplots(figsize=(12, 4.8))
        ax1.plot(h["time"], h["soc_pct"], label="Battery SOC", linewidth=1.2)
        ax1.set_ylabel("SOC (%)"); ax1.set_ylim(0, 105)
        ax2 = ax1.twinx()
        ax2.plot(h["time"], h["t_batt_c_end"], label="Battery temperature",
                 linewidth=1, alpha=.7, linestyle="--")
        ax2.set_ylabel("Battery temperature (°C)")
        ax1.set_title("System state at 6-hour intervals")
        ax1.grid(True, alpha=.2)
        lines = ax1.get_lines() + ax2.get_lines()
        ax1.legend(lines, [line.get_label() for line in lines], loc="best")
        p = out / "09_hourly_state.png"; save_fig(fig, p); images.append(p.name)

    def fmt(value, suffix="", digits=1):
        try:
            if value is None or (isinstance(value, float) and math.isnan(value)):
                return "—"
            return f"{float(value):,.{digits}f}{suffix}"
        except (TypeError, ValueError):
            return str(value)

    cards = [
        ("Simulation period", f'{int(kpis.get("sim_days", len(daily)))} days'),
        ("Minimum battery SOC", fmt(kpis.get("min_soc_pct", daily["soc_min_pct"].min()), "%")),
        ("Ending battery SOC", fmt(kpis.get("end_soc_pct", daily["soc_end_pct"].iloc[-1]), "%")),
        ("Brownout duration", fmt(kpis.get("hours_off_brownout", daily.get("hours_off", pd.Series([0])).sum()), " h")),
        ("Solar potential", fmt(kpis.get("pv_potential_kwh", daily["pv_potential_wh"].sum()/1000), " kWh")),
        ("Heater energy", fmt(kpis.get("heater_wh_rail", daily["heater_wh_rail"].sum() if "heater_wh_rail" in daily else 0), " Wh")),
        ("Minimum battery temperature", fmt(kpis.get("t_batt_min_c", daily["t_batt_min_c"].min()), " °C")),
        ("Packets dropped", fmt(kpis.get("packets_dropped", daily["pkt_dropped"].sum() if "pkt_dropped" in daily else 0), "", 0)),
    ]

    cards_html = "".join(
        f'<div class="kpi"><div class="kpi-label">{label}</div><div class="kpi-value">{value}</div></div>'
        for label, value in cards
    )
    chart_html = "".join(
        f'<section class="chart"><h2>{Path(name).stem.replace("_", " ").title()}</h2><img src="{name}" alt="{name}"></section>'
        for name in images
    )

    scenario_html = ""
    if scenarios is not None and len(scenarios):
        cols = [c for c in ["scenario", "min_soc_pct", "end_soc_pct", "hours_off_brownout",
                            "survived_full_period", "packets_dropped"] if c in scenarios.columns]
        display = scenarios[cols].copy()
        display.columns = [c.replace("_", " ").title() for c in cols]
        scenario_html = "<h2>Scenario comparison</h2>" + display.to_html(index=False, border=0, classes="scenario-table", float_format=lambda x: f"{x:,.1f}")

    caveat = ("Interpretation: These are outputs of the configured simulation model, not measured buoy performance. "
              "Survival and reliability claims depend on the assumptions, component data, weather inputs and validation.")
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BOYANT Simulation Results</title>
<style>
:root{{--bg:#0b1220;--panel:#121d30;--text:#edf3fb;--muted:#a9b8cc;--accent:#64d7c5}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 Arial,sans-serif}}
main{{max-width:1180px;margin:auto;padding:36px 22px}} h1{{font-size:34px;margin:0}} .sub{{color:var(--muted);margin:8px 0 28px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:22px 0 30px}}
.kpi,.chart{{background:var(--panel);border:1px solid #26364d;border-radius:14px;padding:16px}}
.kpi-label{{color:var(--muted);font-size:13px}} .kpi-value{{font-size:27px;font-weight:700;margin-top:5px;color:var(--accent)}}
.chart{{margin:16px 0 22px}} .chart h2{{font-size:18px;margin:0 0 12px}} img{{width:100%;height:auto;display:block;border-radius:8px;background:white}}
h2{{margin-top:34px}} .scenario-table{{width:100%;border-collapse:collapse;background:var(--panel);font-size:13px}}
.scenario-table th,.scenario-table td{{padding:10px;border-bottom:1px solid #26364d;text-align:left}}
.scenario-table th{{color:var(--accent)}} .note{{margin-top:28px;padding:16px;border-left:4px solid #e6b85c;background:#20283a;color:#d7e0eb;border-radius:6px}}
footer{{color:var(--muted);font-size:12px;margin-top:26px}}
@media(max-width:600px){{main{{padding:22px 12px}}h1{{font-size:27px}}}}
</style></head><body><main>
<h1>BOYANT | Simulation Results</h1>
<p class="sub">365-day buoy system simulation · Visual summary generated from BOYANT output logs</p>
<div class="grid">{cards_html}</div>
{chart_html}
{scenario_html}
<div class="note"><b>Important:</b> {caveat}</div>
<footer>Generated by BOYANT Results Visualizer. Charts are derived from the supplied simulation CSV outputs.</footer>
</main></body></html>"""
    report_path = out / "index.html"
    report_path.write_text(html, encoding="utf-8")

    print(f"Created {len(images)} charts and dashboard:")
    print(report_path)
    print("Open index.html in your browser.")


if __name__ == "__main__":
    main()
