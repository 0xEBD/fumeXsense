#!/usr/bin/env python3
"""
Weller WX2 Remote Control GUI
==============================
Implements the serial protocol from Weller Application Note WAN12-0006
(serial protocol WX - Ver. 4.1, since WX firmware 064).

Interface settings (fixed by the WX):
    1200 baud, 8 data bits, no parity, 1 stop bit, no handshake

Requirements:
    pip install pyserial matplotlib

Run on Windows:
    python wx2_control.py
"""

import csv
import json
import math
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    raise SystemExit("pyserial is required:  pip install pyserial")

try:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import (FigureCanvasTkAgg,
                                                   NavigationToolbar2Tk)
    HAVE_MPL = True
except ImportError:
    HAVE_MPL = False

# ---------------------------------------------------------------------------
# Protocol layer (WAN12-0006)
# ---------------------------------------------------------------------------

BAUD = 1200

STATUS_NAMES = {0: "OFF", 1: "ON", 2: "STANDBY", 3: "AUTOOFF"}

TOOL_NAMES = {
    0: "NOTOOL",
    1: "WXP120",
    2: "WXP200",
    3: "WXMP",
    4: "WXMT",
    5: "WXP65",
    6: "WXP80",
    7: "WXB200",
}

UNIT_IDS = {
    1: "WX 1",
    2: "WX 2",
    3: "WX 2D",
    4: "WX 2A",
    5: "WX 1D",
    6: "WX 1A",
}


def checksum(payload: str) -> int:
    """Checksum per section 1.4: sum of the 6 ASCII values, modulo 256.

    The app note writes "(v1 + ... + v6) - 256* ... if sum > 255 then -256",
    which is a repeated subtraction, i.e. mod 256.
    """
    return sum(payload.encode("ascii")) % 256


def frame_set_command(prefix: str, value: int) -> bytes:
    """Build a 7-byte set frame, e.g. s1 + 2500 + checksum byte.

    prefix: two chars like 's1', 'q1', 't2', 'x1', ...
    value:  0..9999, transmitted as 4 ASCII digits
    """
    if not (0 <= value <= 9999):
        raise ValueError("value out of range 0..9999")
    payload = f"{prefix}{value:04d}"
    if len(payload) != 6:
        raise ValueError("payload must be 6 chars")
    return payload.encode("ascii") + bytes([checksum(payload)])


def parse_response(raw: bytes):
    """Parse a response consisting of one or more 7-byte blocks:
    <letter><channel digit><4 digits><checksum byte>

    Returns a list of dicts: {'tag': 'R1', 'value': 2500, 'csum_ok': True}
    Unparseable trailing bytes are returned under key 'garbage'.
    """
    blocks = []
    i = 0
    while i + 7 <= len(raw):
        chunk = raw[i:i + 7]
        payload, cs = chunk[:6], chunk[6]
        try:
            text = payload.decode("ascii")
        except UnicodeDecodeError:
            break
        if not (text[0].isalnum() or text[0] in "?"):
            break
        if not text[2:6].isdigit():
            break
        blocks.append({
            "tag": text[:2],
            "value": int(text[2:6]),
            "csum_ok": checksum(text) == cs,
        })
        i += 7
    return blocks, raw[i:]


# ---------------------------------------------------------------------------
# Serial worker thread
# ---------------------------------------------------------------------------

class WXLink:
    """Owns the serial port. All I/O funnels through a single worker thread
    so polled reads and user commands never interleave on the wire.

    At 1200 baud a character takes ~8.3 ms; a 14-byte answer ~120 ms.
    """

    def __init__(self, on_event):
        self.on_event = on_event          # callback(kind, data) -> GUI queue
        self.ser = None
        self.cmd_q = queue.Queue()
        self.worker = None
        self.running = False

    # -- lifecycle ----------------------------------------------------------

    def open(self, port: str):
        # drop any commands left over from a previous session so a stale
        # set command can't replay on reconnect
        try:
            while True:
                self.cmd_q.get_nowait()
        except queue.Empty:
            pass
        self.ser = serial.Serial(
            port=port,
            baudrate=BAUD,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
            timeout=0.15,       # inter-byte read timeout
        )
        self.running = True
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def close(self):
        self.running = False
        if self.worker:
            self.worker.join(timeout=2.0)
            self.worker = None
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    # -- public API (thread-safe) --------------------------------------------

    def submit(self, tx: bytes, label: str, expect_reply: bool = True,
               reply_budget: float = 1.2):
        """Queue a raw frame for transmission. reply_budget limits how long
        we listen for an answer (set commands are not documented to answer,
        so they use a short budget)."""
        self.cmd_q.put((tx, label, expect_reply, reply_budget))

    def flush_polls(self):
        """Drop all queued poll commands; keep user-initiated ones."""
        kept = []
        try:
            while True:
                item = self.cmd_q.get_nowait()
                if not item[1].startswith("poll"):
                    kept.append(item)
        except queue.Empty:
            pass
        for item in kept:
            self.cmd_q.put(item)

    # -- worker ---------------------------------------------------------------

    def _run(self):
        while self.running:
            try:
                tx, label, expect_reply, budget = self.cmd_q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.ser.reset_input_buffer()
                self.ser.write(tx)
                self.ser.flush()
                self.on_event("tx", (label, tx))
                if expect_reply:
                    rx = self._read_reply(budget)
                    self.on_event("rx", (label, rx))
            except serial.SerialException as e:
                self.on_event("error", str(e))
                self.running = False
                self.on_event("disconnected", None)

    def _read_reply(self, budget: float = 1.2) -> bytes:
        """Collect bytes until the line goes quiet.

        Overall time budget per command; done after two consecutive empty
        timeout reads once at least one byte arrived (idle gap ~0.3 s >>
        char time at 1200 Bd).
        """
        buf = bytearray()
        deadline = time.monotonic() + budget
        idle = 0
        while time.monotonic() < deadline:
            chunk = self.ser.read(32)
            if chunk:
                buf.extend(chunk)
                idle = 0
            else:
                if buf:
                    idle += 1
                    if idle >= 2:
                        break
        return bytes(buf)


# ---------------------------------------------------------------------------
# Shelly Plug S Gen3 client (fume extractor)
# ---------------------------------------------------------------------------

class ShellyPlug:
    """Minimal client for a Shelly Plug S Gen3 (Gen2+/Gen3 RPC API over HTTP).

    Endpoints used:
        GET /rpc/Switch.GetStatus?id=0
            -> {"output": bool, "apower": W, "voltage": V, "current": A, ...}
        GET /rpc/Switch.Set?id=0&on=true|false

    Note: device authentication must be disabled (Shelly app -> device ->
    Settings -> Authentication), otherwise requests fail with HTTP 401.
    """

    POLL_S = 2.0

    def __init__(self, on_event):
        self.on_event = on_event          # callback(kind, data) -> GUI queue
        self.ip = None
        self.running = False
        self.worker = None
        self.cmd_q = queue.Queue()

    def start(self, ip: str):
        self.ip = ip.strip()
        self.running = True
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def stop(self):
        self.running = False
        if self.worker:
            self.worker.join(timeout=3.0)
            self.worker = None

    def set_output(self, on: bool):
        """Thread-safe: queue a relay command."""
        self.cmd_q.put(bool(on))

    # -- worker ---------------------------------------------------------------

    def _rpc(self, path: str) -> dict:
        url = f"http://{self.ip}/rpc/{path}"
        with urllib.request.urlopen(url, timeout=2.0) as r:
            return json.loads(r.read().decode("utf-8"))

    def _run(self):
        next_poll = 0.0
        while self.running:
            try:
                # relay commands first
                try:
                    on = self.cmd_q.get(timeout=0.2)
                    self._rpc(f"Switch.Set?id=0&on={'true' if on else 'false'}")
                    self.on_event("shelly_set", on)
                    next_poll = 0.0            # refresh status right away
                except queue.Empty:
                    pass
                # periodic status poll
                if self.running and time.monotonic() >= next_poll:
                    status = self._rpc("Switch.GetStatus?id=0")
                    self.on_event("shelly_status", status)
                    next_poll = time.monotonic() + self.POLL_S
            except Exception as e:
                self.on_event("shelly_error", str(e))
                time.sleep(2.0)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class WX2App(tk.Tk):
    POLL_SEQUENCE = ("R", "S", "Q")          # polled every cycle
    SLOW_SEQUENCE = ("Y",)                   # polled every Nth cycle

    def __init__(self):
        super().__init__()
        self.title("Weller WX2 Remote Control  (WAN12-0006)")
        self.minsize(760, 560)

        self.link = WXLink(self._link_event)
        self.gui_q = queue.Queue()
        self.polling = tk.BooleanVar(value=False)

        # persisted settings (Shelly IP, last COM port, UI options)
        self.settings_path = os.path.join(os.path.expanduser("~"),
                                          ".wx2_control.json")
        self.settings = self._load_settings()

        self.poll_interval_ms = tk.IntVar(
            value=int(self.settings.get("poll_interval_ms", 2000)))
        self.autosave = tk.BooleanVar(
            value=bool(self.settings.get("autosave", True)))
        self._as_file = None                   # autosave CSV handle
        self._as_writer = None
        self._as_path = None
        self._status_dirty = {1: False, 2: False}  # user edited status combos
        self._fume_lowpower = 0                # consecutive low-power polls
        self._fume_alerted = False             # extractor alert latched
        self._shelly_err_last = 0.0            # rate limit for error log
        self._poll_job = None
        self._slow_counter = 0

        # graph data: {(channel, series): deque[(t_rel_seconds, temp_c)]}
        # 50k samples/series ~ 35 h at a 2.5 s poll -> whole-session recording
        self.GRAPH_SERIES = ("actual", "set", "preset1", "preset2")
        self.graph_data = {(ch, k): deque(maxlen=50000)
                           for ch in (1, 2) for k in self.GRAPH_SERIES}
        self.graph_t0 = time.monotonic()
        self.graph_wall_t0 = time.time()       # for absolute timestamps in CSV

        # fume extractor (Shelly Plug S Gen3)
        self.shelly = ShellyPlug(self._link_event)
        self.ch_status = {1: None, 2: None}   # last known WX status per channel
        self.ch_tool = {1: None, 2: None}     # last known tool name per channel
        self._fume_desired = None              # last commanded relay state
        self._fume_off_job = None              # pending delayed-off timer
        self.fume_graph = deque(maxlen=50000)  # (t_rel_seconds, relay_on)
        self.status_events = deque(maxlen=5000)  # (t_rel_s, ch, status_int)

        self._build_ui()
        self._refresh_ports()
        self.after(50, self._drain_gui_queue)
        self.after(1000, self._graph_tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI --

    def _build_ui(self):
        pad = dict(padx=6, pady=4)

        # --- connection bar ---------------------------------------------------
        top = ttk.LabelFrame(self, text="Connection")
        top.pack(fill="x", **pad)

        ttk.Label(top, text="Port:").grid(row=0, column=0, sticky="w", **pad)
        self.port_cb = ttk.Combobox(top, width=28, state="readonly")
        self.port_cb.grid(row=0, column=1, **pad)
        ttk.Button(top, text="Refresh", command=self._refresh_ports)\
            .grid(row=0, column=2, **pad)
        self.btn_connect = ttk.Button(top, text="Connect", command=self._toggle_connect)
        self.btn_connect.grid(row=0, column=3, **pad)
        ttk.Label(top, text="1200 8N1, no handshake").grid(row=0, column=4, **pad)

        # remote mode
        ttk.Label(top, text="Remote:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Button(top, text="Enable (remote1)",
                   command=lambda: self._send_ascii("remote1", "remote1")) \
            .grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(top, text="Enable + button lock (remote2)",
                   command=lambda: self._send_ascii("remote2", "remote2")) \
            .grid(row=1, column=2, columnspan=2, sticky="ew", **pad)
        ttk.Button(top, text="Disable (remote0)",
                   command=lambda: self._send_ascii("remote0", "remote0",
                                                    expect_reply=False)) \
            .grid(row=1, column=4, sticky="ew", **pad)

        # --- live data ---------------------------------------------------------
        live = ttk.LabelFrame(self, text="Live data")
        live.pack(fill="x", **pad)

        cols = ("Channel", "Tool", "Status", "Actual °C", "Set °C",
                "Preset 1 °C", "Preset 2 °C")
        for c, name in enumerate(cols):
            ttk.Label(live, text=name, font=("TkDefaultFont", 9, "bold"))\
                .grid(row=0, column=c, **pad)

        self.live_vars = {}
        for ch in (1, 2):
            ttk.Label(live, text=f"CH{ch}").grid(row=ch, column=0, **pad)
            for c, key in enumerate(("tool", "status", "actual", "set",
                                     "preset1", "preset2"), start=1):
                var = tk.StringVar(value="--")
                self.live_vars[(ch, key)] = var
                ttk.Label(live, textvariable=var, width=11, anchor="center",
                          relief="groove").grid(row=ch, column=c, **pad)

        self.unit_var = tk.StringVar(value="Unit: --   FW: --")
        ttk.Label(live, textvariable=self.unit_var).grid(
            row=3, column=0, columnspan=4, sticky="w", **pad)

        pollf = ttk.Frame(live)
        pollf.grid(row=3, column=4, columnspan=3, sticky="e", **pad)
        ttk.Checkbutton(pollf, text="Poll", variable=self.polling,
                        command=self._poll_toggled).pack(side="left")
        self.quiet_standby = tk.BooleanVar(
            value=bool(self.settings.get("quiet_standby", True)))
        ttk.Checkbutton(pollf, text="Quiet in standby",
                        variable=self.quiet_standby).pack(side="left",
                                                          padx=(8, 0))
        ttk.Label(pollf, text="every").pack(side="left", padx=(8, 2))
        ttk.Spinbox(pollf, from_=1000, to=10000, increment=500, width=6,
                    textvariable=self.poll_interval_ms).pack(side="left")
        ttk.Label(pollf, text="ms").pack(side="left", padx=(2, 0))
        ttk.Button(pollf, text="Read once", command=self._poll_once)\
            .pack(side="left", padx=(10, 0))

        # --- control -------------------------------------------------------------
        ctrl = ttk.LabelFrame(self, text="Control")
        ctrl.pack(fill="x", **pad)

        # channel status q1: digits (CH1, CH2, 0, 0)
        ttk.Label(ctrl, text="Status:").grid(row=0, column=0, sticky="w", **pad)
        self.status_ch = {}
        for i, ch in enumerate((1, 2)):
            var = tk.StringVar(value="ON")
            self.status_ch[ch] = var
            ttk.Label(ctrl, text=f"CH{ch}").grid(row=0, column=1 + 2 * i, sticky="e")
            cb = ttk.Combobox(ctrl, values=("OFF", "ON"), width=5,
                              state="readonly", textvariable=var)
            cb.grid(row=0, column=2 + 2 * i, **pad)
            # mark as user-edited so live-status syncing leaves it alone
            cb.bind("<<ComboboxSelected>>",
                    lambda _e, c=ch: self._status_dirty.__setitem__(c, True))
        ttk.Button(ctrl, text="Apply status (q1)", command=self._apply_status)\
            .grid(row=0, column=5, sticky="ew", **pad)

        # temperature rows: (label, prefix, description)
        rows = (
            ("Set temp", "s", "Set temperature"),
            ("Preset 1", "t", "Preset temperature 1"),
            ("Preset 2", "u", "Preset temperature 2"),
        )
        self.temp_entries = {}
        for r, (label, prefix, _desc) in enumerate(rows, start=1):
            ttk.Label(ctrl, text=f"{label} [°C]:").grid(row=r, column=0,
                                                        sticky="w", **pad)
            for i, ch in enumerate((1, 2)):
                ttk.Label(ctrl, text=f"CH{ch}").grid(row=r, column=1 + 2 * i,
                                                     sticky="e")
                e = ttk.Entry(ctrl, width=7)
                e.grid(row=r, column=2 + 2 * i, **pad)
                self.temp_entries[(prefix, ch)] = e
            ttk.Button(ctrl, text=f"Send ({prefix}1/{prefix}2)",
                       command=lambda p=prefix: self._apply_temps(p))\
                .grid(row=r, column=5, sticky="ew", **pad)

        # fingerswitch
        ttk.Label(ctrl, text="Fingerswitch [s]:").grid(row=4, column=0,
                                                       sticky="w", **pad)
        self.fs_entries = {}
        for i, ch in enumerate((1, 2)):
            ttk.Label(ctrl, text=f"CH{ch}").grid(row=4, column=1 + 2 * i, sticky="e")
            e = ttk.Entry(ctrl, width=7)
            e.insert(0, "0")
            e.grid(row=4, column=2 + 2 * i, **pad)
            self.fs_entries[ch] = e
        ttk.Button(ctrl, text="Trigger (x1/x2)", command=self._apply_fingerswitch)\
            .grid(row=4, column=5, sticky="ew", **pad)

        # raw command
        ttk.Label(ctrl, text="Raw:").grid(row=5, column=0, sticky="w", **pad)
        self.raw_entry = ttk.Entry(ctrl, width=24)
        self.raw_entry.grid(row=5, column=1, columnspan=3, sticky="ew", **pad)
        self.raw_csum = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctrl, text="append checksum",
                        variable=self.raw_csum).grid(row=5, column=4, **pad)
        ttk.Button(ctrl, text="Send raw", command=self._send_raw)\
            .grid(row=5, column=5, sticky="ew", **pad)

        # --- fume extractor -----------------------------------------------------
        fume = ttk.LabelFrame(self, text="Fume extractor (Shelly Plug S Gen3)")
        fume.pack(fill="x", **pad)

        ttk.Label(fume, text="IP:").grid(row=0, column=0, sticky="w", **pad)
        self.shelly_ip = ttk.Entry(fume, width=16)
        self.shelly_ip.insert(0, self.settings.get("shelly_ip", ""))
        self.shelly_ip.grid(row=0, column=1, **pad)
        self.btn_shelly = ttk.Button(fume, text="Connect",
                                     command=self._toggle_shelly)
        self.btn_shelly.grid(row=0, column=2, **pad)

        # status indicator dot + relay state
        self.fume_dot = tk.Canvas(fume, width=16, height=16,
                                  highlightthickness=0)
        self._fume_dot_id = self.fume_dot.create_oval(2, 2, 14, 14,
                                                      fill="gray", outline="")
        self.fume_dot.grid(row=0, column=3, **pad)
        self.fume_relay_var = tk.StringVar(value="offline")
        ttk.Label(fume, textvariable=self.fume_relay_var, width=8)\
            .grid(row=0, column=4, **pad)

        # power values
        self.fume_power_var = tk.StringVar(value="-- W")
        self.fume_current_var = tk.StringVar(value="-- A")
        ttk.Label(fume, textvariable=self.fume_power_var, width=9,
                  anchor="e", relief="groove").grid(row=0, column=5, **pad)
        ttk.Label(fume, textvariable=self.fume_current_var, width=9,
                  anchor="e", relief="groove").grid(row=0, column=6, **pad)

        # automation rule
        self.fume_auto = tk.BooleanVar(
            value=bool(self.settings.get("fume_auto", True)))
        ttk.Checkbutton(fume, text="Auto: on while soldering",
                        variable=self.fume_auto).grid(row=1, column=0,
                                                      columnspan=2,
                                                      sticky="w", **pad)
        ttk.Label(fume, text="Watch:").grid(row=1, column=2, sticky="e", **pad)
        self.fume_watch = ttk.Combobox(fume, values=("CH1", "CH2", "CH1+CH2"),
                                       width=8, state="readonly")
        watch = self.settings.get("fume_watch", "CH1+CH2")
        self.fume_watch.set(watch if watch in ("CH1", "CH2", "CH1+CH2")
                            else "CH1+CH2")
        self.fume_watch.grid(row=1, column=3, columnspan=2, sticky="w", **pad)
        ttk.Label(fume, text="Off delay [s]:").grid(row=1, column=5,
                                                    sticky="e", **pad)
        self.fume_delay = tk.IntVar(
            value=int(self.settings.get("fume_delay", 10)))
        ttk.Spinbox(fume, from_=0, to=600, increment=5, width=5,
                    textvariable=self.fume_delay).grid(row=1, column=6,
                                                       sticky="w", **pad)
        ttk.Button(fume, text="On", width=5,
                   command=lambda: self._fume_manual(True))\
            .grid(row=1, column=7, **pad)
        ttk.Button(fume, text="Off", width=5,
                   command=lambda: self._fume_manual(False))\
            .grid(row=1, column=8, **pad)

        # warning banner: shown when the relay is ON but the extractor draws
        # no real power; purely informational, hidden while everything is ok
        self.fume_warn = tk.Label(fume, text="", bg="#c0392b", fg="white",
                                  font=("TkDefaultFont", 10, "bold"),
                                  anchor="w", padx=8, pady=4)
        self.fume_warn.grid(row=2, column=0, columnspan=9,
                            sticky="ew", padx=6, pady=(2, 6))
        self.fume_warn.grid_remove()

        # --- bottom tabs: graph + log -------------------------------------------
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, **pad)

        self.graph_tab = ttk.Frame(self.nb)
        self.nb.add(self.graph_tab, text="Graph")
        self._build_graph(self.graph_tab)

        logf = ttk.Frame(self.nb)
        self.nb.add(logf, text="Log (TX/RX)")
        self.log = tk.Text(logf, height=10, state="disabled",
                           font=("Consolas", 9))
        self.log.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=sb.set)

        self.statusbar = tk.StringVar(value="Disconnected")
        ttk.Label(self, textvariable=self.statusbar, anchor="w",
                  relief="sunken").pack(fill="x", side="bottom")

    # ------------------------------------------------------------------ graph --

    GRAPH_WINDOWS = {"1 min": 60, "5 min": 300, "15 min": 900,
                     "1 h": 3600, "all": None}

    def _build_graph(self, parent):
        if not HAVE_MPL:
            ttk.Label(parent, text="matplotlib is not installed.\n\n"
                                   "    pip install matplotlib\n\n"
                                   "then restart the tool to use the graph.",
                      justify="center").pack(expand=True)
            return

        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=6, pady=(4, 0))

        # channel selection
        ttk.Label(bar, text="Channels:").pack(side="left")
        self.g_ch = {}
        for ch in (1, 2):
            var = tk.BooleanVar(value=True)
            self.g_ch[ch] = var
            ttk.Checkbutton(bar, text=f"CH{ch}", variable=var)\
                .pack(side="left", padx=(4, 0))

        # series selection
        ttk.Label(bar, text="   Series:").pack(side="left")
        self.g_series = {}
        for key, label, default in (("actual", "Actual", True),
                                    ("set", "Set", True),
                                    ("preset1", "Preset 1", False),
                                    ("preset2", "Preset 2", False)):
            var = tk.BooleanVar(value=default)
            self.g_series[key] = var
            ttk.Checkbutton(bar, text=label, variable=var)\
                .pack(side="left", padx=(4, 0))

        # fume extractor status (shaded background while relay is ON)
        self.g_fume = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Extractor", variable=self.g_fume)\
            .pack(side="left", padx=(4, 0))

        # channel state-change markers (ON/OFF/STANDBY/AUTOOFF)
        self.g_events = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Events", variable=self.g_events)\
            .pack(side="left", padx=(4, 0))

        # time window
        ttk.Label(bar, text="   Window:").pack(side="left")
        self.g_window = ttk.Combobox(bar, values=list(self.GRAPH_WINDOWS),
                                     width=6, state="readonly")
        self.g_window.set("5 min")
        self.g_window.pack(side="left", padx=(4, 0))

        ttk.Button(bar, text="Reset graph", command=self._graph_reset)\
            .pack(side="right")
        ttk.Button(bar, text="Save CSV\u2026", command=self._graph_save)\
            .pack(side="right", padx=(0, 6))
        ttk.Checkbutton(bar, text="Autosave", variable=self.autosave)\
            .pack(side="right", padx=(0, 6))
        # Live: auto-follow incoming data; pauses automatically on zoom/pan
        self.g_live = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Live", variable=self.g_live,
                        command=self._live_toggled)\
            .pack(side="right", padx=(0, 6))

        # y-axis controls
        ybar = ttk.Frame(parent)
        ybar.pack(fill="x", padx=6)
        ttk.Label(ybar, text="Temp axis [°C]:   min").pack(side="left")
        self.g_ymin = ttk.Entry(ybar, width=5)
        self.g_ymin.insert(0, str(self.settings.get("y_min", 0)))
        self.g_ymin.pack(side="left", padx=(4, 10))
        ttk.Label(ybar, text="max").pack(side="left")
        self.g_ymax = ttk.Entry(ybar, width=5)
        self.g_ymax.insert(0, str(self.settings.get("y_max", 500)))
        self.g_ymax.pack(side="left", padx=(4, 10))
        self.g_yauto = tk.BooleanVar(
            value=bool(self.settings.get("y_auto", True)))
        ttk.Checkbutton(ybar, text="Auto max (highest measured + headroom)",
                        variable=self.g_yauto).pack(side="left")
        self._applied_ylim = self.GRAPH_YLIM_DEFAULT

        fig = Figure(figsize=(7, 2.6), dpi=100, tight_layout=True)
        self.g_ax = fig.add_subplot(111)
        self.g_canvas = FigureCanvasTkAgg(fig, master=parent)

        # matplotlib navigation toolbar: zoom-to-rect, pan, home, save image
        self.g_toolbar = NavigationToolbar2Tk(self.g_canvas, parent,
                                              pack_toolbar=False)
        self.g_toolbar.update()
        self.g_toolbar.pack(fill="x", padx=6)

        self.g_canvas.get_tk_widget().pack(fill="both", expand=True,
                                           padx=6, pady=4)
        # scroll wheel zooms around the cursor; any zoom/pan pauses Live
        self.g_canvas.mpl_connect("scroll_event", self._graph_scroll_zoom)
        self.g_canvas.mpl_connect("button_press_event",
                                  self._graph_button_press)

    def _live_toggled(self):
        if self.g_live.get():
            # leaving zoom mode: deactivate toolbar tools and redraw fresh
            if self.g_toolbar.mode:
                if str(self.g_toolbar.mode) == "zoom rect":
                    self.g_toolbar.zoom()
                elif str(self.g_toolbar.mode) == "pan/zoom":
                    self.g_toolbar.pan()
            self._graph_redraw()

    def _graph_button_press(self, event):
        # user grabbed zoom-rect or pan from the toolbar -> stop live redraws
        # so their view isn't wiped by the next update
        if self.g_toolbar.mode:
            self.g_live.set(False)

    # y-zoom stays disabled: the temperature axis is managed by the tool
    # (settable min/max or auto max); zoom acts on the time axis only
    GRAPH_YLIM_DEFAULT = (0.0, 500.0)

    def _compute_ylim(self, data_max):
        """Determine the temperature axis limits.

        min: from the entry, clamped at >= 0 (never negative).
        max: auto mode scales to the highest visible value plus ~8 % + 5 °C
             headroom, rounded up to 25 °C steps for a calm axis, capped at
             500 °C (hotter is not a real WX reading); manual mode takes the
             entry value as-is."""
        try:
            ymin = max(0.0, float(self.g_ymin.get().replace(",", ".")))
        except (ValueError, tk.TclError):
            ymin = 0.0
        if self.g_yauto.get():
            if data_max is None:
                ymax = self.GRAPH_YLIM_DEFAULT[1]
            else:
                ymax = min(500.0,
                           math.ceil((data_max * 1.08 + 5.0) / 25.0) * 25.0)
        else:
            try:
                ymax = float(self.g_ymax.get().replace(",", "."))
            except (ValueError, tk.TclError):
                ymax = self.GRAPH_YLIM_DEFAULT[1]
        if ymax <= ymin + 10.0:
            ymax = ymin + 10.0
        return (ymin, ymax)

    def _lock_ylim(self, ax):
        """ylim_changed callback: pin the temperature axis to the currently
        managed limits for every zoom/pan path (toolbar zoom-rect, pan,
        home). emit=False prevents this callback from re-triggering itself."""
        if ax.get_ylim() != self._applied_ylim:
            ax.set_ylim(*self._applied_ylim, emit=False)
            self.g_canvas.draw_idle()

    def _graph_scroll_zoom(self, event):
        if event.inaxes is not self.g_ax or event.xdata is None:
            return
        self.g_live.set(False)          # manual zoom pauses live mode
        scale = 0.8 if event.button == "up" else 1.25
        ax = self.g_ax
        lo, hi = ax.get_xlim()
        ax.set_xlim(event.xdata - (event.xdata - lo) * scale,
                    event.xdata + (hi - event.xdata) * scale)
        self.g_canvas.draw_idle()

    def _record(self, ch: int, key: str, temp_c: float):
        """Store a sample for the graph (called for checksum-clean data only)."""
        t = time.monotonic() - self.graph_t0
        self.graph_data[(ch, key)].append((t, temp_c))
        self._autosave_row(t, "temp", ch, key, f"{temp_c:.1f}")

    def _record_fume(self, on: bool):
        """Store the extractor relay state for the graph."""
        t = time.monotonic() - self.graph_t0
        self.fume_graph.append((t, bool(on)))
        self._autosave_row(t, "extractor", "", "relay", "1" if on else "0")

    def _record_event(self, ch: int, status: int):
        """Store a channel state change (ON/OFF/STANDBY/AUTOOFF) for the graph."""
        t = time.monotonic() - self.graph_t0
        self.status_events.append((t, ch, status))
        self._log(f"EVENT CH{ch} -> {STATUS_NAMES.get(status, status)}")
        self._autosave_row(t, "event", ch, STATUS_NAMES.get(status, str(status)),
                           str(status))

    # ---------------------------------------------------------- autosave --

    def _autosave_row(self, t, kind, ch, series, value):
        """Crash-proof session log: append each sample to a CSV as it
        arrives. File is created lazily on the first sample and flushed per
        row, so a crash loses at most the current line."""
        if not self.autosave.get():
            return
        try:
            if self._as_file is None:
                folder = os.path.join(os.path.expanduser("~"), "wx2_sessions")
                os.makedirs(folder, exist_ok=True)
                self._as_path = os.path.join(
                    folder, time.strftime("wx2_session_%Y%m%d_%H%M%S.csv"))
                self._as_file = open(self._as_path, "w", newline="",
                                     encoding="utf-8")
                self._as_writer = csv.writer(self._as_file)
                started = datetime.fromtimestamp(
                    self.graph_wall_t0).isoformat(timespec="seconds")
                self._as_writer.writerow(
                    [f"# WX2 session started {started}"])
                self._as_writer.writerow(["timestamp", "t_s", "type",
                                          "channel", "series", "value"])
                self._log(f"AUTOSAVE -> {self._as_path}")
            stamp = datetime.fromtimestamp(
                self.graph_wall_t0 + t).isoformat(timespec="milliseconds")
            self._as_writer.writerow([stamp, f"{t:.3f}", kind, ch,
                                      series, value])
            self._as_file.flush()
        except OSError as e:
            self._log(f"WARN autosave failed, disabling: {e}")
            self.autosave.set(False)

    def _autosave_close(self):
        if self._as_file is not None:
            try:
                self._as_file.close()
            except OSError:
                pass
            self._as_file = None
            self._as_writer = None

    def _collect_settings(self):
        """Gather current UI options into the settings dict for persistence."""
        try:
            self.settings.update({
                "poll_interval_ms": int(self.poll_interval_ms.get()),
                "quiet_standby": bool(self.quiet_standby.get()),
                "autosave": bool(self.autosave.get()),
                "fume_auto": bool(self.fume_auto.get()),
                "fume_watch": self.fume_watch.get(),
                "fume_delay": int(self.fume_delay.get()),
            })
            if HAVE_MPL and hasattr(self, "g_yauto"):
                self.settings["y_auto"] = bool(self.g_yauto.get())
                self.settings["y_min"] = self.g_ymin.get().strip() or "0"
                self.settings["y_max"] = self.g_ymax.get().strip() or "500"
        except (tk.TclError, ValueError):
            pass    # empty spinbox etc. - keep previous values

    def _graph_reset(self):
        for dq in self.graph_data.values():
            dq.clear()
        self.fume_graph.clear()
        self.status_events.clear()
        self.graph_t0 = time.monotonic()
        self.graph_wall_t0 = time.time()
        self._autosave_close()      # next sample starts a fresh session file
        if HAVE_MPL:
            self.g_ax.clear()
            self.g_canvas.draw_idle()

    def _graph_tick(self):
        # redraw only when the graph tab is visible AND live mode is on;
        # while paused (zoom/pan), data keeps recording but the view stays put
        if (HAVE_MPL and self.g_live.get()
                and self.nb.select() == str(self.graph_tab)):
            self._graph_redraw()
        self.after(1000, self._graph_tick)

    def _graph_redraw(self):
        ax = self.g_ax
        ax.clear()
        now = time.monotonic() - self.graph_t0
        window = self.GRAPH_WINDOWS.get(self.g_window.get())

        styles = {"actual": "-", "set": "--", "preset1": ":", "preset2": "-."}
        widths = {"actual": 1.6, "set": 1.1, "preset1": 1.0, "preset2": 1.0}
        colors = {1: "tab:blue", 2: "tab:orange"}
        labels = {"actual": "actual", "set": "set",
                  "preset1": "preset 1", "preset2": "preset 2"}

        plotted = False
        data_max = None
        for ch in (1, 2):
            if not self.g_ch[ch].get():
                continue
            tool = self.ch_tool[ch]
            ch_label = f"CH{ch} {tool}" if tool else f"CH{ch}"
            for key in self.GRAPH_SERIES:
                if not self.g_series[key].get():
                    continue
                data = self.graph_data[(ch, key)]
                if not data:
                    continue
                if window is not None:
                    cutoff = now - window
                    pts = [(t, v) for t, v in data if t >= cutoff]
                else:
                    pts = list(data)
                if not pts:
                    continue
                series_max = max(v for _, v in pts)
                data_max = (series_max if data_max is None
                            else max(data_max, series_max))
                # decimate for display: full data stays in the deques and in
                # the CSV; plotting >2500 pts/series every second is wasted
                if len(pts) > 2500:
                    step = (len(pts) + 2499) // 2500
                    pts = pts[::step]
                ax.plot([t / 60 for t, _ in pts], [v for _, v in pts],
                        styles[key], color=colors[ch],
                        linewidth=widths[key],
                        label=f"{ch_label} {labels[key]}")
                plotted = True

        ax.set_xlabel("time [min]")
        ax.set_ylabel("temperature [°C]")

        # extractor status: shaded background where the relay was ON
        if self.g_fume.get() and self.fume_graph:
            pts = list(self.fume_graph)
            if window is not None:
                cutoff = now - window
                before = [p for p in pts if p[0] < cutoff]
                pts = [p for p in pts if p[0] >= cutoff]
                if before:                       # carry state into the window
                    pts.insert(0, (cutoff, before[-1][1]))
            if pts:
                pts.append((now, pts[-1][1]))    # extend last state to 'now'
                ax.fill_between([t / 60 for t, _ in pts], 0, 1,
                                where=[v for _, v in pts], step="post",
                                transform=ax.get_xaxis_transform(),
                                color="tab:green", alpha=0.15, linewidth=0,
                                label="extractor ON")
                plotted = True

        # channel state-change events: vertical markers with state label
        if self.g_events.get() and self.status_events:
            events = list(self.status_events)
            if window is not None:
                cutoff = now - window
                events = [e for e in events if e[0] >= cutoff]
            for t, ch, st in events:
                if not self.g_ch[ch].get():
                    continue
                x = t / 60
                ax.axvline(x, color=colors[ch], linestyle=":",
                           linewidth=1.0, alpha=0.7)
                ax.annotate(f"CH{ch} {STATUS_NAMES.get(st, st)}",
                            xy=(x, 0.99 if ch == 1 else 0.86),
                            xycoords=("data", "axes fraction"),
                            rotation=90, va="top", ha="right",
                            fontsize=7, color=colors[ch], alpha=0.9)
                plotted = True

        ax.grid(True, alpha=0.3)
        handles, leg_labels = ax.get_legend_handles_labels()
        if leg_labels:
            ax.legend(loc="upper left", fontsize=8, ncols=2)
        if plotted and window is not None:
            ax.set_xlim(max(0.0, now - window) / 60,
                        max(now / 60, 0.02))
        # managed temperature axis (settable min/max or auto max);
        # ax.clear() above wiped the axes callbacks, so re-register the
        # lock for toolbar zoom/pan paths
        self._applied_ylim = self._compute_ylim(data_max)
        ax.set_ylim(*self._applied_ylim)
        ax.callbacks.connect("ylim_changed", self._lock_ylim)
        self.g_canvas.draw_idle()

    def _graph_save(self):
        """Export the entire recorded session (all series, extractor states,
        and channel events) to one long-format CSV, sorted by time."""
        rows = []
        for (ch, key), dq in self.graph_data.items():
            for t, v in dq:
                rows.append((t, "temp", ch, key, f"{v:.1f}"))
        for t, on in self.fume_graph:
            rows.append((t, "extractor", "", "relay", "1" if on else "0"))
        for t, ch, st in self.status_events:
            rows.append((t, "event", ch, STATUS_NAMES.get(st, str(st)),
                         str(st)))
        if not rows:
            messagebox.showinfo("Save session", "No data recorded yet.")
            return
        rows.sort(key=lambda r: r[0])

        default_name = time.strftime("wx2_session_%Y%m%d_%H%M%S.csv")
        path = filedialog.asksaveasfilename(
            title="Save session data",
            defaultextension=".csv",
            initialfile=default_name,
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                started = datetime.fromtimestamp(
                    self.graph_wall_t0).isoformat(timespec="seconds")
                w.writerow([f"# WX2 session started {started}"])
                w.writerow(["timestamp", "t_s", "type", "channel",
                            "series", "value"])
                for t, kind, ch, series, value in rows:
                    stamp = datetime.fromtimestamp(
                        self.graph_wall_t0 + t).isoformat(
                        timespec="milliseconds")
                    w.writerow([stamp, f"{t:.3f}", kind, ch, series, value])
        except OSError as e:
            messagebox.showerror("Save failed", str(e))
            return
        self._log(f"SESSION saved {len(rows)} rows -> {path}")

    # ------------------------------------------------------- fume extractor --

    def _toggle_shelly(self):
        if self.shelly.running:
            self._fume_cancel_off()
            self.shelly.stop()
            self.btn_shelly.config(text="Connect")
            self._fume_show_offline("offline")
            self._log("SHELLY disconnected")
            return
        ip = self.shelly_ip.get().strip()
        if not ip:
            messagebox.showwarning("No IP",
                                   "Enter the IP address of the Shelly plug.")
            return
        self._fume_desired = None
        self.shelly.start(ip)
        self.settings["shelly_ip"] = ip
        self._save_settings()
        self.btn_shelly.config(text="Disconnect")
        self._log(f"SHELLY connecting to {ip}")

    def _fume_manual(self, on: bool):
        """Manual On/Off buttons. Auto rule may override on the next Q poll."""
        if not self.shelly.running:
            messagebox.showwarning("Shelly", "Connect to the Shelly plug first.")
            return
        self._fume_cancel_off()
        self._fume_command(on)

    def _fume_command(self, on: bool):
        """Send relay command only if it differs from the last commanded state."""
        if self._fume_desired == on:
            return
        self._fume_desired = on
        self.shelly.set_output(on)

    def _fume_cancel_off(self):
        if self._fume_off_job:
            self.after_cancel(self._fume_off_job)
            self._fume_off_job = None

    def _fume_delayed_off(self):
        self._fume_off_job = None
        self._fume_command(False)

    def _fume_auto_eval(self):
        """Called after every checksum-clean Q answer.

        Soldering (status ON=1) on any watched channel -> extractor on.
        STANDBY/OFF/AUTOOFF on all watched channels    -> off after delay.
        """
        if not (self.fume_auto.get() and self.shelly.running):
            return
        chans = {"CH1": (1,), "CH2": (2,),
                 "CH1+CH2": (1, 2)}[self.fume_watch.get()]
        states = [self.ch_status[c] for c in chans]
        if all(s is None for s in states):
            return                      # no status seen yet
        active = any(s == 1 for s in states)
        if active:
            self._fume_cancel_off()
            self._fume_command(True)
        elif self._fume_desired is not False and self._fume_off_job is None:
            delay_ms = max(0, self.fume_delay.get()) * 1000
            if delay_ms == 0:
                self._fume_command(False)
            else:
                self._fume_off_job = self.after(delay_ms,
                                                self._fume_delayed_off)

    def _fume_show_offline(self, text: str):
        self.fume_dot.itemconfig(self._fume_dot_id, fill="gray")
        self.fume_relay_var.set(text)
        self.fume_power_var.set("-- W")
        self.fume_current_var.set("-- A")

    # ---------------------------------------------------------- connection --

    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_cb["values"] = ports
        if ports and not self.port_cb.get():
            last = self.settings.get("com_port")
            self.port_cb.set(last if last in ports else ports[0])

    def _toggle_connect(self):
        if self.link.ser:
            self._disconnect()
            return
        port = self.port_cb.get()
        if not port:
            messagebox.showwarning("No port", "Select a COM port first.")
            return
        try:
            self.link.open(port)
        except serial.SerialException as e:
            messagebox.showerror("Serial error", str(e))
            return
        self.btn_connect.config(text="Disconnect")
        self.settings["com_port"] = port
        self._save_settings()
        self.statusbar.set(f"Connected to {port} @ {BAUD} 8N1")
        self._log(f"--- connected to {port} ---")
        # identify unit and firmware once
        self._send_ascii("?", "unit-id")
        self._send_ascii("V", "firmware")

    def _disconnect(self):
        self.polling.set(False)
        self._poll_toggled()
        if self.link.ser:
            # leave remote mode so the WX display stops flashing "PC";
            # brief sleep lets the worker push the 7 bytes out at 1200 Bd
            self.link.submit(b"remote0", "remote0 (auto)", expect_reply=False)
            time.sleep(0.3)
        self.link.close()
        self.btn_connect.config(text="Connect")
        self.statusbar.set("Disconnected")
        self._log("--- disconnected ---")

    def _on_close(self):
        try:
            if self.link.ser:
                self._disconnect()
            self.shelly.stop()
            self._collect_settings()
            self._save_settings()
            self._autosave_close()
        finally:
            self.destroy()

    # -------------------------------------------------------------- sending --

    def _require_link(self) -> bool:
        if not self.link.ser:
            messagebox.showwarning("Not connected", "Connect to the WX2 first.")
            return False
        return True

    def _send_ascii(self, text: str, label: str, expect_reply: bool = True):
        if not self._require_link():
            return
        self.link.submit(text.encode("ascii"), label, expect_reply)

    def _send_set(self, prefix: str, value: int, label: str):
        if not self._require_link():
            return
        try:
            frame = frame_set_command(prefix, value)
        except ValueError as e:
            messagebox.showerror("Invalid value", str(e))
            return
        # Per app note, set commands are not documented to answer;
        # listen only briefly in case the unit echoes something.
        self.link.submit(frame, label, expect_reply=True, reply_budget=0.35)

    def _apply_status(self):
        v1 = 1 if self.status_ch[1].get() == "ON" else 0
        v2 = 1 if self.status_ch[2].get() == "ON" else 0
        # q1 has no "leave unchanged" digit: it always commands BOTH channels
        # with ON(1) or OFF(0). Warn if that would disturb a channel the user
        # didn't touch, or end a standby/autooff state.
        warnings = []
        for ch, v in ((1, v1), (2, v2)):
            live = self.ch_status[ch]
            if live in (2, 3) :
                warnings.append(
                    f"CH{ch} is in {STATUS_NAMES[live]}; sending "
                    f"{'ON' if v else 'OFF'} will end that state.")
            elif (live is not None and live != v
                  and not self._status_dirty[ch]):
                warnings.append(
                    f"CH{ch} was not changed by you, but q1 will command it "
                    f"{'ON' if v else 'OFF'} (currently "
                    f"{STATUS_NAMES.get(live, live)}).")
        if warnings:
            if not messagebox.askyesno(
                    "Apply status",
                    "q1 always sets BOTH channels:\n\n"
                    + "\n".join(warnings) + "\n\nSend anyway?"):
                return
        value = v1 * 1000 + v2 * 100
        self._send_set("q1", value, f"status q1 (CH1={v1}, CH2={v2})")
        self._status_dirty = {1: False, 2: False}

    def _apply_temps(self, prefix: str):
        for ch in (1, 2):
            raw = self.temp_entries[(prefix, ch)].get().strip()
            if not raw:
                continue
            try:
                temp_c = float(raw.replace(",", "."))
            except ValueError:
                messagebox.showerror("Invalid value",
                                     f"CH{ch}: '{raw}' is not a number")
                return
            value = int(round(temp_c * 10))       # protocol wants 1/10 °C
            self._send_set(f"{prefix}{ch}", value,
                           f"{prefix}{ch} = {temp_c:g} °C")

    def _apply_fingerswitch(self):
        for ch in (1, 2):
            raw = self.fs_entries[ch].get().strip()
            if not raw:
                continue
            try:
                seconds = int(raw)
            except ValueError:
                messagebox.showerror("Invalid value",
                                     f"CH{ch}: '{raw}' is not an integer")
                return
            if seconds > 0:
                self._send_set(f"x{ch}", seconds,
                               f"fingerswitch x{ch} = {seconds}s")

    def _send_raw(self):
        text = self.raw_entry.get()
        if not text:
            return
        if not self._require_link():
            return
        data = text.encode("ascii", errors="replace")
        if self.raw_csum.get():
            if len(text) != 6:
                messagebox.showerror(
                    "Checksum", "Checksum is defined over exactly 6 chars.")
                return
            data += bytes([checksum(text)])
        self.link.submit(data, f"raw '{text}'", expect_reply=True)

    # -------------------------------------------------------------- polling --

    def _poll_toggled(self):
        if self._poll_job:
            self.after_cancel(self._poll_job)
            self._poll_job = None
        if self.polling.get() and self.link.ser:
            self._poll_cycle()
        elif self.link.ser:
            # polling switched off: drop any queued poll commands so the
            # worker stops hitting the wire immediately (at 1200 baud a
            # backlog otherwise keeps transmitting for a long time)
            self.link.flush_polls()

    def _poll_once(self):
        if not self._require_link():
            return
        for cmd in self.POLL_SEQUENCE + self.SLOW_SEQUENCE + ("T", "U"):
            self._send_ascii(cmd, f"poll {cmd}")

    def _poll_cycle(self):
        if not (self.polling.get() and self.link.ser):
            return
        # Only queue a new batch when the previous one has fully drained.
        # A poll batch takes ~1.5-3 s at 1200 baud; if the interval is
        # shorter, we skip a tick instead of piling up a backlog.
        if self.link.cmd_q.empty():
            if self._standby_quiet_active():
                # minimal traffic while the station rests: status query only,
                # so serial activity can't kick the WX out of standby
                self._send_ascii("Q", "poll Q")
            else:
                for cmd in self.POLL_SEQUENCE:
                    self._send_ascii(cmd, f"poll {cmd}")
                self._slow_counter += 1
                if self._slow_counter >= 5:
                    self._slow_counter = 0
                    for cmd in self.SLOW_SEQUENCE + ("T", "U"):
                        self._send_ascii(cmd, f"poll {cmd}")
        self._poll_job = self.after(max(1000, self.poll_interval_ms.get()),
                                    self._poll_cycle)

    def _standby_quiet_active(self) -> bool:
        """True when quiet mode is enabled and no channel is ON.

        Statuses OFF(0)/STANDBY(2)/AUTOOFF(3) count as resting; unknown
        channels (never seen) are ignored. Requires at least one known
        status so a fresh connection still does full polls."""
        if not self.quiet_standby.get():
            return False
        known = [s for s in self.ch_status.values() if s is not None]
        return bool(known) and all(s != 1 for s in known)

    # ------------------------------------------------------------ RX parsing --

    def _link_event(self, kind, data):
        """Called from the worker thread: hand over to the GUI thread."""
        self.gui_q.put((kind, data))

    def _drain_gui_queue(self):
        try:
            while True:
                kind, data = self.gui_q.get_nowait()
                if kind == "tx":
                    label, tx = data
                    self._log(f"TX  {label:24s} {self._fmt_bytes(tx)}")
                elif kind == "rx":
                    label, rx = data
                    if rx:
                        self._log(f"RX  {label:24s} {self._fmt_bytes(rx)}")
                        self._handle_rx(rx)
                    else:
                        self._log(f"RX  {label:24s} (no answer)")
                elif kind == "error":
                    self._log(f"ERR {data}")
                elif kind == "disconnected":
                    self._disconnect()
                elif kind == "shelly_status":
                    out = bool(data.get("output"))
                    apower = data.get("apower", 0.0)
                    self.fume_dot.itemconfig(
                        self._fume_dot_id,
                        fill="#2ecc40" if out else "#666666")
                    self.fume_relay_var.set("ON" if out else "OFF")
                    self.fume_power_var.set(f"{apower:.1f} W")
                    self.fume_current_var.set(
                        f"{data.get('current', 0.0):.3f} A")
                    self._record_fume(out)
                    # reconcile external changes (plug button, Shelly app):
                    # adopt reality as new baseline so the dedup in
                    # _fume_command can't get stuck on a stale desired state
                    if (self._fume_desired is not None
                            and out != self._fume_desired):
                        self._log(f"SHELLY external change detected, relay "
                                  f"is {'ON' if out else 'OFF'}")
                        self._fume_desired = out
                    # extractor health: relay ON but no real power drawn
                    # -> fan unplugged, jammed, or dead. Red banner is
                    # informational only; nothing is switched or blocked.
                    if out and apower < 5.0:
                        self._fume_lowpower += 1
                        if self._fume_lowpower >= 3:
                            self.fume_warn.config(
                                text="\u26a0  FUME EXTRACTOR IS NOT RUNNING "
                                     f"\u2014 relay is ON but only "
                                     f"{apower:.1f} W are drawn. You are "
                                     "soldering WITHOUT extraction: check "
                                     "fan, plug and hose!")
                            self.fume_warn.grid()
                            if not self._fume_alerted:
                                self._fume_alerted = True
                                self._log("WARN extractor relay ON but "
                                          f"only {apower:.1f} W drawn "
                                          "- fan not running?")
                    else:
                        self._fume_lowpower = 0
                        if self._fume_alerted:
                            self._fume_alerted = False
                            self.fume_warn.grid_remove()
                            self._log("INFO extractor power draw normal "
                                      "again, warning cleared")
                elif kind == "shelly_set":
                    self._log(f"SHELLY relay -> {'ON' if data else 'OFF'}")
                    self._record_fume(bool(data))
                elif kind == "shelly_error":
                    self.fume_dot.itemconfig(self._fume_dot_id, fill="#e74c3c")
                    self.fume_relay_var.set("error")
                    # rate-limit: an unreachable plug would otherwise spam
                    # the log every 2 s
                    now_mono = time.monotonic()
                    if now_mono - self._shelly_err_last > 60.0:
                        self._shelly_err_last = now_mono
                        self._log(f"SHELLY error: {data}")
        except queue.Empty:
            pass
        self.after(50, self._drain_gui_queue)

    def _handle_rx(self, raw: bytes):
        blocks, garbage = parse_response(raw)
        for b in blocks:
            tag, value, ok = b["tag"], b["value"], b["csum_ok"]
            suffix = "" if ok else "  [CHECKSUM BAD]"
            kind, ch_char = tag[0], tag[1]
            ch = int(ch_char) if ch_char.isdigit() else 0

            if kind == "R" and ch in (1, 2):
                self.live_vars[(ch, "actual")].set(f"{value / 10:.1f}{suffix}")
                if ok:
                    self._record(ch, "actual", value / 10)
            elif kind == "S" and ch in (1, 2):
                self.live_vars[(ch, "set")].set(f"{value / 10:.1f}{suffix}")
                if ok:
                    self._record(ch, "set", value / 10)
            elif kind == "T" and ch in (1, 2):
                self.live_vars[(ch, "preset1")].set(f"{value / 10:.1f}{suffix}")
                if ok:
                    self._record(ch, "preset1", value / 10)
            elif kind == "U" and ch in (1, 2):
                self.live_vars[(ch, "preset2")].set(f"{value / 10:.1f}{suffix}")
                if ok:
                    self._record(ch, "preset2", value / 10)
            elif kind == "Q":
                digits = f"{value:04d}"
                for ch_i in (1, 2):
                    st = int(digits[ch_i - 1])
                    self.live_vars[(ch_i, "status")].set(
                        STATUS_NAMES.get(st, f"? {st}") + suffix)
                    if ok:
                        prev = self.ch_status[ch_i]
                        if prev is not None and prev != st:
                            self._record_event(ch_i, st)
                        self.ch_status[ch_i] = st
                        # keep the status combobox in sync with reality unless
                        # the user has picked a value they haven't applied yet
                        if not self._status_dirty[ch_i]:
                            self.status_ch[ch_i].set(
                                "ON" if st == 1 else "OFF")
                if ok:
                    self._fume_auto_eval()
            elif kind == "Y" and ch in (1, 2):
                self.live_vars[(ch, "tool")].set(
                    TOOL_NAMES.get(value, f"? {value}") + suffix)
                if ok:
                    self.ch_tool[ch] = TOOL_NAMES.get(value, f"?{value}")
            elif kind == "?":
                unit = UNIT_IDS.get(ch, f"ID {ch_char}")
                cur = self.unit_var.get()
                fw = cur.split("FW:")[-1].strip()
                self.unit_var.set(f"Unit: {unit} (index {value:04d})   FW: {fw}")
            elif kind == "V":
                cur = self.unit_var.get()
                unit = cur.split("FW:")[0].replace("Unit:", "").strip()
                self.unit_var.set(f"Unit: {unit}   FW: {value}")
        if garbage:
            self._log(f"    unparsed tail: {self._fmt_bytes(garbage)}")

    # -------------------------------------------------------------- helpers --

    def _load_settings(self) -> dict:
        try:
            with open(self.settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        try:
            with open(self.settings_path, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=2)
        except OSError as e:
            self._log(f"WARN could not save settings: {e}")

    @staticmethod
    def _fmt_bytes(data: bytes) -> str:
        printable = "".join(chr(b) if 32 <= b < 127 else f"\\x{b:02x}"
                            for b in data)
        hexes = " ".join(f"{b:02X}" for b in data)
        return f"'{printable}'  [{hexes}]"

    def _log(self, line: str):
        stamp = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"{stamp}  {line}\n")
        # cap the widget so long sessions don't bog down tkinter
        if int(self.log.index("end-1c").split(".")[0]) > 2000:
            self.log.delete("1.0", "201.0")
        self.log.see("end")
        self.log.configure(state="disabled")


if __name__ == "__main__":
    WX2App().mainloop()