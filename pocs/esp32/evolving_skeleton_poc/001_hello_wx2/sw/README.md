# PoC Software - ESP32-C3-based “Hello WX2”

← Back to the [PoC overview](../README.md) · See also: [Hardware](../hw/README.md)

⚠️ Before running the software, read the Safety and Disclaimer sections of the [PoC overview](../README.md).

## What Changed Since the PC-based PoC

**Same behavior, new platform.** The Python application was ported to the ESP32-C3 with AI assistance (see the [PoC overview](../README.md)).

## Overview / BOM

<table>
  <thead>
    <tr><th>Component</th><th>Details</th></tr>
  </thead>
  <tbody>
    <tr><td>MCU</td><td>Seeed Studio XIAO ESP32-C3</td></tr>
    <tr><td>Framework</td><td>Arduino framework for ESP32 (no external libraries required)</td></tr>
    <tr><td>WX2 interface</td><td>UART1, 1200 baud, 8N1 via the RS-232 transceiver on the PoC base board (see <a href=../hw/README.md>Hardware</a>)</td></tr>
    <tr><td>Shelly interface</td><td>Wi-Fi / local HTTP RPC API</td></tr>
    <tr><td>Application</td><td><a href=fxs_poc_skeleton/fxs_poc_skeleton.ino><code>wx2_hello_world.ino</code></a></td></tr>
    <tr><td>Configuration</td><td><code>secrets.h</code> for Wi-Fi credentials and Shelly IP address</td></tr>
  </tbody>
</table>

## ESP32 Application

The ESP32 application moves the basic fumeXsense control loop from the PC onto an embedded target.

The XIAO ESP32-C3 communicates with the Weller WX2 through the PoC hardware, evaluates the channel state and controls the fume extractor through a Shelly Plug S Gen3.

The implementation deliberately remains simple and synchronous. It establishes the first working embedded skeleton:

**WX2 → RS-232 → ESP32-C3 → Wi-Fi → Shelly Plug → 230 V → Fume Extractor**

### WX2 Communication

The WX2 communication implements the Weller serial protocol documented in application note **WAN12-0006** (protocol version 4.1, WX firmware 064 or newer).

- **Serial link:** 1200 baud, 8N1 using UART1
- **Protocol:** 7-byte response frames; every received frame is checksum-checked
- **Polling:** `Q` channel status, `R` actual temperature, `S` set temperature and `Y` connected tools
- **Remote mode:** `remote1` enabled during startup
- **Station identification:** WX station type queried during startup

See [Hardware](../hw/README.md) for details of the physical WX2 interface.

### Extractor Control

The extractor is controlled from the WX2 channel state:

- At least one channel ON → extractor ON
- No channel ON → extractor OFF after a fixed 30 s run-on delay
- Shelly state validated every 10 s
- External Shelly state changes, for example through the physical button, are detected and adopted
- Warning if the Shelly relay is ON but measured power remains below 5 W

The Shelly Plug S Gen3 is controlled through its local HTTP RPC API. No cloud connection is required for normal operation.

### Serial Output

The application prints the current WX2 and extractor state to the USB serial console, for example:

```text
—————————
CH1: ON        CH2: STANDBY
Actual temp  CH1: 349.8 C   CH2: 28.4 C
Set temp     CH1: 350.0 C   CH2: 350.0 C
Tools        CH1: WXMP      CH2: WXMT
Extractor: desired=ON  actual=ON  23.7 W
```

The high-level system features are described in the [PoC overview](../README.md).

## Known Limitations

This is intentionally simple PoC software developed for rapid functional validation, not a production software baseline.

- Blocking, synchronous implementation
- Limited error handling and recovery
- No Shelly authentication support
- Configuration such as Shelly IP, run-on delay and GPIO assignment is fixed at compile time
- No quiet mode in standby; WX2 polling continues while no channel is ON
- On WX2 communication loss, the last known channel state is retained
- Known issue: CR/LF bytes are discarded by the receive routine, which can cause sporadic checksum failures if the checksum itself is `0x0D` or `0x0A`
- No automated tests; tested with a single hardware setup

The implementation intentionally prioritizes a short feedback loop and transparent behavior over production-level software architecture.

See the [PoC overview](../README.md) for the overall PoC maturity and lessons learned.

## Build & Run

### Prerequisites

- Seeed Studio XIAO ESP32-C3
- Arduino IDE with ESP32 board support
- WX2 connected through the PoC base board and ESP32 shield (see [Hardware](../hw/README.md))
- Shelly Plug S Gen3 connected to the local Wi-Fi network
- Shelly authentication disabled

### Setup

Copy the example configuration:

```bash
cp secrets.h.example secrets.h
```

Configure the local network and Shelly address in `secrets.h`:

```cpp
#define SECRET_WIFI_SSID “your-ssid”
#define SECRET_WIFI_PASS “your-password”
#define SECRET_SHELLY_IP “192.168.x.x”
```

> 🔒 `secrets.h` contains your Wi-Fi credentials. It is listed in `.gitignore` and must never be committed to the repository.

Recommended: assign a fixed IP address to the Shelly using a DHCP reservation or static IP configuration.

### Build & Flash

Open the Arduino sketch, select the **XIAO ESP32-C3** board and flash it to the device.

Open the serial monitor at **115200 baud**.

At startup, the application:

1. Initializes the WX2 UART.
2. Connects to Wi-Fi.
3. Enables WX2 remote mode.
4. Identifies the WX station.
5. Reads the current Shelly state.
6. Starts polling the WX2 and controlling the extractor.

← Back to the [PoC overview](../README.md) · See also: [Hardware](../hw/README.md)