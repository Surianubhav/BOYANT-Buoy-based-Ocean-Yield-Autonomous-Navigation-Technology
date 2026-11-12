# BOYANT – LTspice Power Management Simulations

LTspice simulations of the BOYANT buoy's power management system, focusing on efficient voltage conversion, battery heating, and sensor power control.

## 1. Synchronous Buck Converters

The system uses synchronous buck converters to step down the 12.8V battery supply to 3.3V and 5V.

Unlike conventional buck converters that use a diode, synchronous converters use a second MOSFET for current conduction. This reduces power losses, minimises heat generation, and improves energy efficiency — particularly important for battery-powered systems operating for extended periods.

**Reported simulation efficiencies:**

| Converter | Efficiency |
|---|---:|
| 12.8V → 3.3V | 98.95% |
| 12.8V → 5V | 92.99% |

*Reported simulation values; hardware efficiency requires experimental validation.*

### 1.1 3.3V Synchronous Buck Converter

Converts the 12.8V battery supply into 3.3V for microcontrollers and low-voltage sensors.

<p align="center">
  <img src="3v_sync_buck_schem.jpeg" alt="3.3V synchronous buck converter schematic" width="85%">
  <br>
  <em>3.3V synchronous buck converter circuit schematic</em>
</p>

<p align="center">
  <img src="3v_sync_buck.jpeg" alt="3.3V buck converter simulation waveform" width="85%">
  <br>
  <em>Simulated output voltage and startup response</em>
</p>

### 1.2 5V Synchronous Buck Converter

Converts the 12.8V battery supply into 5V for peripherals and electronic modules. The switching duty cycle is adjusted to obtain the required output voltage.

<p align="center">
  <img src="5v_sync_buck_schem.jpeg" alt="5V synchronous buck converter schematic" width="85%">
  <br>
  <em>5V synchronous buck converter circuit schematic</em>
</p>

<p align="center">
  <img src="5v_sync_buck.jpeg" alt="5V buck converter simulation waveform" width="85%">
  <br>
  <em>Simulated output voltage and startup response</em>
</p>

---

## 2. Battery Temperature-Controlled Heater

A MOSFET-based switching stage designed to control a resistive heating element. The N-channel MOSFET acts as a low-side switch, controlling current through the heater.

This provides a foundation for maintaining battery temperature in cold environments.

<p align="center">
  <img src="battery_temp_controlled_heater_mosfet_stage_schem.jpeg" alt="Battery heater MOSFET circuit schematic" width="85%">
  <br>
  <em>Battery heater MOSFET switching circuit</em>
</p>

<p align="center">
  <img src="battery_temp_controlled_heater_mosfet_stage.jpeg" alt="Battery heater switching simulation waveform" width="85%">
  <br>
  <em>Simulated heater switching behaviour</em>
</p>

*Note: The current simulation demonstrates timed switching. Temperature sensing and automatic feedback control are future integration steps.*

---

## 3. Sensor Power Switching

A P-channel MOSFET-based high-side switching circuit designed to control power supplied to sensors.

Sensors can be switched OFF when not required, reducing unnecessary standby power consumption and improving battery utilisation.

<p align="center">
  <img src="sensor_switching_schem.jpeg" alt="Sensor power switching circuit schematic" width="85%">
  <br>
  <em>3.3V sensor power switching schematic</em>
</p>

<p align="center">
  <img src="sensor_switching.jpeg" alt="Sensor switching simulation waveform" width="85%">
  <br>
  <em>Simulated sensor power switching behaviour</em>
</p>

---

## 4. Components & Tools

| Component | Specification |
|---|---|
| Simulation software | LTspice |
| Input supply | 12.8V |
| N-channel MOSFET | Si4410DY |
| P-channel MOSFET | Si4431DY |
| Inductor | 22µH |
| Capacitor | 100µF |
| Switching frequency | 200kHz |

## 5. Future Improvements

- Closed-loop voltage regulation and soft-start.
- Overcurrent and overvoltage protection.
- MOSFET gate-driver integration.
- Thermal analysis and optimisation.
- Temperature-feedback-based heater control.
- Hardware implementation and efficiency validation.

---

**Project:** BOYANT – Buoy-based Ocean Yield & Autonomous Navigation Technology  
**Simulation Environment:** LTspice
