import pandas as pd, numpy as np, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
L="logs/"
h=pd.read_csv(L+"hourly_log.csv",parse_dates=["time"]); d=pd.read_csv(L+"daily_summary.csv",parse_dates=["date"])
sc={n:pd.read_csv(f"{L}scenarios/{n}_daily.csv",parse_dates=["date"]) for n in ["spec_heater_8C","sd_not_power_gated","panel_icing_70pct_below_-2C"]}
plt.rcParams.update({"font.size":9,"axes.grid":True,"grid.alpha":.3})
fmt=mdates.DateFormatter("%b")

# Fig 1: battery, temperatures, energy
fig,ax=plt.subplots(4,1,figsize=(11,11),sharex=True)
ax[0].plot(d.date,d.soc_end_pct,label="SOC (vs temperature-derated capacity)");ax[0].plot(h.time,h.soc_nom_pct,lw=.6,alpha=.7,label="SOC (vs 192 Wh nominal)")
ax[0].axhline(20,color="r",ls="--",lw=.8);ax[0].set_ylabel("SOC %");ax[0].legend(loc="lower left");ax[0].set_ylim(0,105);ax[0].set_title("BOYANT 365-day Southern Ocean simulation (63.67S, 40.49E, 2025 weather)")
ax[1].plot(h.time,h.t_air_c,lw=.5,color="tab:blue",label="Air 2 m");ax[1].plot(h.time,h.t_sea_c,color="tab:cyan",label="Sea (model)");ax[1].plot(h.time,h.t_enc_c_end,lw=.8,color="tab:red",label="Battery / enclosure")
ax[1].axhline(0,color="k",lw=.8);ax[1].set_ylabel("Temp °C");ax[1].legend(loc="lower left",ncol=3)
ax[2].fill_between(d.date,d.pv_potential_wh,color="gold",alpha=.8,label="PV potential");ax[2].plot(d.date,d.load_bat_wh,color="k",label="Total load (battery side)")
ax[2].set_yscale("symlog",linthresh=1);ax[2].set_ylabel("Wh / day (symlog)");ax[2].legend(loc="upper right")
mode=h.set_index("time")["mode"].resample("D").mean()
ax[3].fill_between(mode.index,0,1,where=mode>=2.5,color="tab:green",alpha=.5,step="mid",label="NORMAL")
ax[3].fill_between(mode.index,0,1,where=(mode<2.5)&(mode>=1.5),color="tab:orange",alpha=.6,step="mid",label="LOW-ENERGY / WINTER")
ax[3].fill_between(mode.index,0,1,where=(mode<1.5)&(mode>=.5),color="tab:purple",alpha=.6,step="mid",label="DEEP SLEEP")
ax[3].fill_between(mode.index,0,1,where=mode<.5,color="k",alpha=.7,step="mid",label="OFF (brown-out)")
hd=h.set_index("time")["heater_on_min"].resample("D").sum()/60
ax[3].bar(hd.index,hd.values/24,width=1,color="red",label="heater duty",alpha=.9);ax[3].set_ylim(0,1);ax[3].set_yticks([]);ax[3].legend(ncol=5,loc="upper center",fontsize=7)
ax[3].set_ylabel("Mode");ax[3].xaxis.set_major_formatter(fmt)
plt.tight_layout();plt.savefig(L+"fig1_battery_thermal_modes.png",dpi=130);plt.close()

# Fig 2: where the energy goes
s=pd.read_csv(L+"subsystem_energy_annual.csv");s=s[s.annual_wh_rail_side>0.5].sort_values("annual_wh_rail_side")
fig,ax=plt.subplots(1,2,figsize=(12,4.5))
ax[0].barh(s.subsystem,s.annual_wh_rail_side,color="tab:blue");ax[0].set_xlabel("Wh / year");ax[0].set_title("Annual energy by consumer (baseline)")
for y,v in enumerate(s.annual_wh_rail_side): ax[0].text(v,y,f" {v:.1f}",va="center",fontsize=8)
m=pd.read_csv(L+"monthly_summary.csv")
ax[1].bar(m.month,m.pv_potential_wh/1000,color="gold",label="PV potential");ax[1].set_xlabel("Month");ax[1].set_ylabel("kWh / month");ax[1].set_title("Solar potential vs ~14 Wh/month typical load");ax[1].set_xticks(range(1,13))
plt.tight_layout();plt.savefig(L+"fig2_energy_breakdown.png",dpi=130);plt.close()

# Fig 3: scenarios
fig,ax=plt.subplots(2,1,figsize=(11,6.5),sharex=True)
ax[0].plot(d.date,d.soc_end_pct,lw=2,label="baseline (adaptive heater)")
ax[0].plot(sc["spec_heater_8C"].date,sc["spec_heater_8C"].soc_end_pct,label="heater holds enclosure >8°C")
ax[0].plot(sc["sd_not_power_gated"].date,sc["sd_not_power_gated"].soc_end_pct,label="SD module left powered (22 mA idle)")
ax[0].plot(sc["panel_icing_70pct_below_-2C"].date,sc["panel_icing_70pct_below_-2C"].soc_end_pct,ls="--",label="panels iced (-70 % below -2°C)")
ax[0].set_ylabel("End-of-day SOC %");ax[0].legend(fontsize=8);ax[0].set_title("Sensitivity: what actually breaks the energy budget")
ax[1].plot(d.date,d.t_enc_mean_c,label="baseline");ax[1].plot(sc["spec_heater_8C"].date,sc["spec_heater_8C"].t_enc_mean_c,label="8°C heater");ax[1].axhline(0,color="k",lw=.8)
ax[1].set_ylabel("Mean enclosure °C");ax[1].legend();ax[1].xaxis.set_major_formatter(fmt)
plt.tight_layout();plt.savefig(L+"fig3_scenarios.png",dpi=130);plt.close()
