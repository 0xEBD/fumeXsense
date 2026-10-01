# PoC Software — PC-based “Hello WX2”

← Back to the [PoC overview](../README.md) · See also: [Hardware](../hw/README.md)

⚠️ Before running the tool, read the Safety and Disclaimer sections of the [PoC overview](../README.md).

## Overview / BOM

<table>
  <thead>
    <tr><th>Component</th><th>Details</th></tr>
  </thead>
  <tbody>
    <tr><td>Python</td><td>Python 3.8 or newer, developed and tested on Windows 11</td></tr>
    <tr><td>pyserial</td><td>Serial communication with the WX2</td></tr>
    <tr><td>matplotlib</td><td>Live temperature graph (version 3.6 or newer; optional, the tool runs without the graph)</td></tr>
    <tr><td>tkinter, urllib, json, csv</td><td>Python standard library: GUI, Shelly HTTP API, settings, session logging</td></tr>
        <tr><td>Python Application</td><td><a href=“wx2_control.py”><code>wx2_control.py</code></a> (single file)</td></tr>

  </tbody>
</table>

## Python Application

![Python application (left) with live data, fume extractor status and temperature graph, next to the WX2 station display (right)](../assets/fxs_p002_sw_gui.jpg)

*Screenshot from the demo video: the set temperature of 230 °C sent to CH2 via the GUI appears on the station display. Status values may briefly differ from the display due to the 2 s polling interval.*

The Python application implements the Weller serial protocol from application note **WAN12-0006** (protocol version 4.1, WX firmware 064 or newer) and closes the fumeXsense control loop on the PC. A Tkinter GUI provides monitoring, configuration and interaction.

**WX2 Communication**

- **Serial link:** 1200 baud, 8N1, no handshake. A single worker thread serializes polls and user commands, so they never interleave on the wire.
- **Protocol:** 7-byte frames (2-character tag, 4 ASCII digits, checksum = sum of the 6 ASCII bytes mod 256). Every reply is checksum-checked; only valid data is used for control and logging.
- **Polling:** `R`, `S` and `Q` every cycle (default 2000 ms, minimum 1000 ms); `Y`, `T` and `U` every 5th cycle. In *Quiet in standby* mode, only `Q` is polled while no channel is `ON`, so serial traffic cannot wake the station.

**Functionality**

- **Live data:** Tool, status (`OFF` / `ON` / `STANDBY` / `AUTOOFF`), actual, set and preset temperatures, plus unit ID and firmware version.
- **Remote control:** Remote mode (`remote0/1/2`), channel status (`q1`), set and preset temperatures, fingerswitch and raw commands.
- **Live graph:** Temperatures per channel, extractor ON shading, status-change markers, zoom and pan.

**Data & Configuration**

- **Session logging:** Autosave of every sample to a CSV file in `~/wx2_sessions/`, plus manual CSV export.
- **Settings:** COM port, Shelly IP and UI options are persisted in `~/.wx2_control.json`.

The high-level system features are described in the [PoC overview](../README.md#features).


## Known Limitations

This is PoC software developed for rapid functional validation, not a production software baseline.

- No automatic serial reconnect after a disconnect
- No Shelly authentication support
- Tested on Windows 11 with a single setup
- No automated tests

See the [PoC overview](../) for the overall PoC maturity and lessons learned.

## Build & Run

### Prerequisites

- Python 3.8 or newer
- Python packages:
  - `pyserial` (serial communication with the WX2)
  - `matplotlib` 3.6 or newer (live graph, optional)
- WX2 connected to the PC through the serial interface (see [Hardware](../hw/README.md))
- Shelly Plug S Gen3 reachable in the local network, authentication disabled

### Setup

1. Connect the WX2 serial interface to the PC via the “PoC Module - PC-based”.
2. Install dependencies:
   ```bash
   pip install pyserial matplotlib
   ```
3. Add the Shelly Plug S Gen3 to your local Wi-Fi using the Shelly Smart Control mobile app. The PC and the plug must be in the same network.
4. In the Shelly app, note the IP address of the plug and disable authentication (device, Settings, Authentication).
5. Recommended: assign a fixed IP to the plug (DHCP reservation in your router or static IP in the Shelly app), so the address does not change after a router restart.

### Run

```bash
cd sw
python wx2_control.py
```

In the GUI: select the COM port and click **Connect**, enter the Shelly IP and click **Connect**, then enable **Poll**. Serial settings (1200 8N1) are fixed by the WX2 and need no configuration.

The application establishes communication with the WX2, displays the received data, and switches the fume extractor according to the detected station state.

← Back to the [PoC overview](../README.md) · See also: [Hardware](../hw/README.md)