"""
Internet EKG - corrected & production-hardened version.

DEBUG:
- Set DEBUG = True for lightweight runtime logging.
- Assertions are used in critical data-paths to fail early in invalid states.
"""

from __future__ import annotations

import bisect
import csv
import datetime as dt
import json
import math
import os
import queue
import re
import socket
import subprocess
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Deque, Dict, List, Optional, Tuple

import requests

import sys
# Konsol kapalıysa hata almamak için stdout'u çöp kutusuna yönlendir
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
    sys.stderr = open(os.devnull, 'w')


try:
    import speedtest
except ImportError:
    speedtest = None

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None
    Image = None
    ImageDraw = None

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
DEBUG = False
DNS_LOOKUP_LOCK = threading.Lock()

CSV_FILE = "internet_log.csv"
INTERVAL_SETTINGS_FILE = "interval_defaults.json"

DEFAULT_INTERVALS_SECONDS: Dict[str, int] = {
    "local_ping": 2,
    "global_ping": 2,
    "dns": 30,
    "http": 60,
    "speedtest": 3 * 60 * 60,
}

UNIT_SECONDS: Dict[str, int] = {
    "Saniye": 1,
    "Dakika": 60,
    "Saat": 3600,
}

DEFAULT_LOCAL_GATEWAY = "192.168.1.1"
DEFAULT_GLOBAL_TARGET = "8.8.8.8"
DEFAULT_DNS_DOMAIN = "youtube.com"
DEFAULT_HTTP_URL = "http://google.com"

PING_TIMEOUT_MS = 1200
DNS_TIMEOUT_S = 5
HTTP_TIMEOUT_S = 8

GRAPH_WINDOW_SECONDS = 300
GRAPH_POINTS_LIMIT = 300

CSV_COLUMNS = [
    "Tarih_Saat",
    "Lokal_Ping_ms",
    "Global_Ping_ms",
    "DNS_ms",
    "HTTP_ms",
    "Download_Mbps",
    "Upload_Mbps",
    "Durum_Hatalar",
]


def debug_log(msg: str) -> None:
    if DEBUG:
        print(f"[DEBUG] {msg}")


def now_str() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_csv_exists(path: str) -> None:
    if Path(path).exists():
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()


def normalize_ms(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:.2f}"


def normalize_mbps(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:.2f}"


def parse_ping_output(output: str) -> Optional[float]:
    less_match = re.search(r"time<\s*(\d+(?:\.\d+)?)\s*ms", output, flags=re.IGNORECASE)
    if less_match:
        return max(0.5, float(less_match.group(1)) / 2.0)

    eq_match = re.search(r"time=\s*(\d+(?:\.\d+)?)\s*ms", output, flags=re.IGNORECASE)
    if eq_match:
        return float(eq_match.group(1))
    return None


def ping_once(host: str, timeout_ms: int = PING_TIMEOUT_MS) -> Tuple[Optional[float], str]:
    if os.name == "nt":
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), host]
    else:
        timeout_s = max(1, math.ceil(timeout_ms / 1000))
        cmd = ["ping", "-c", "1", "-W", str(timeout_s), host]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
    except OSError as exc:
        return None, f"Ping_Command_Error:{type(exc).__name__}"

    combined = f"{result.stdout}\n{result.stderr}"
    latency = parse_ping_output(combined)

    if result.returncode == 0 and latency is not None:
        return latency, "OK"

    text_lower = combined.lower()
    if "timed out" in text_lower or "zaman asimi" in text_lower or "zaman aşımı" in text_lower:
        return None, "Timeout"
    return None, "Ping_Fail"


def dns_lookup_ms(domain: str) -> Tuple[Optional[float], str]:
    """
    DNS lookup with scoped timeout restore.

    Critical fix:
    - Restores previous global timeout to avoid cross-thread side effects.
    """
    started = time.perf_counter()
    with DNS_LOOKUP_LOCK:
        old_timeout = socket.getdefaulttimeout()
        try:
            socket.setdefaulttimeout(DNS_TIMEOUT_S)
            socket.gethostbyname(domain)
            return (time.perf_counter() - started) * 1000, "OK"
        except socket.timeout:
            return None, "DNS_Timeout"
        except socket.gaierror:
            return None, "DNS_Error"
        except OSError as exc:
            return None, f"DNS_Exception:{type(exc).__name__}"
        finally:
            socket.setdefaulttimeout(old_timeout)


def http_get_ms(url: str) -> Tuple[Optional[float], str]:
    started = time.perf_counter()
    try:
        resp = requests.get(url, timeout=HTTP_TIMEOUT_S)
        elapsed_ms = (time.perf_counter() - started) * 1000
        return (elapsed_ms, "OK") if resp.status_code == 200 else (elapsed_ms, f"HTTP_{resp.status_code}")
    except requests.Timeout:
        return None, "HTTP_Timeout"
    except requests.ConnectionError:
        return None, "HTTP_Connection_Error"
    except requests.RequestException as exc:
        return None, f"HTTP_Exception:{type(exc).__name__}"


def run_speedtest_mbps() -> Tuple[Optional[float], Optional[float], str]:
    if speedtest is None:
        return None, None, "Speedtest_Module_Not_Installed"

    try:
        st = speedtest.Speedtest()
        st.get_best_server()
        down_bps = st.download()
        up_bps = st.upload(pre_allocate=False)
        return down_bps / 1_000_000, up_bps / 1_000_000, "OK"
    except Exception as exc:  # speedtest library is not strict on exception types
        return None, None, f"Speedtest_Error:{type(exc).__name__}"


def build_empty_row() -> Dict[str, str]:
    return {
        "Tarih_Saat": now_str(),
        "Lokal_Ping_ms": "N/A",
        "Global_Ping_ms": "N/A",
        "DNS_ms": "N/A",
        "HTTP_ms": "N/A",
        "Download_Mbps": "N/A",
        "Upload_Mbps": "N/A",
        "Durum_Hatalar": "OK",
    }


def queue_row(log_q: queue.Queue, row: Dict[str, str]) -> None:
    try:
        log_q.put_nowait(row)
    except queue.Full:
        fallback = build_empty_row()
        fallback["Durum_Hatalar"] = "Logger_Queue_Full"
        try:
            log_q.put_nowait(fallback)
        except queue.Full:
            debug_log("Logger queue full: dropping row")
            pass


def logging_worker(log_q: queue.Queue, stop_event: threading.Event, csv_path: str) -> None:
    ensure_csv_exists(csv_path)
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        while not stop_event.is_set() or not log_q.empty():
            try:
                row = log_q.get(timeout=0.5)
            except queue.Empty:
                continue
            writer.writerow(row)
            f.flush()
            log_q.task_done()


def periodic_runner_dynamic(
    name: str,
    interval_getter: Callable[[], int],
    stop_event: threading.Event,
    task_fn: Callable[[], None],
) -> None:
    while not stop_event.is_set():
        started = time.monotonic()
        try:
            task_fn()
        except Exception as exc:
            debug_log(f"[{name}] unexpected task exception: {exc}")

        try:
            interval_s = max(1.0, float(interval_getter()))
        except Exception:
            interval_s = 1.0

        elapsed = time.monotonic() - started
        sleep_for = max(0.0, interval_s - elapsed)
        end_wait = time.monotonic() + sleep_for
        while not stop_event.is_set() and time.monotonic() < end_wait:
            time.sleep(min(0.2, end_wait - time.monotonic()))


# -----------------------------------------------------------------------------
# Monitoring service
# -----------------------------------------------------------------------------
class MonitorService:
    """Background measurement service."""

    def __init__(self, csv_file: str, ui_q: queue.Queue, intervals: Optional[Dict[str, int]] = None) -> None:
        self.csv_file = csv_file
        self.ui_q = ui_q
        self.log_q: queue.Queue = queue.Queue(maxsize=5000)
        self.stop_event = threading.Event()
        self.started = False
        self.threads: List[threading.Thread] = []
        self.logger_thread: Optional[threading.Thread] = None

        self.config_lock = threading.Lock()
        self.interval_lock = threading.Lock()

        self.targets = {
            "local_gateway": DEFAULT_LOCAL_GATEWAY,
            "global_target": DEFAULT_GLOBAL_TARGET,
            "dns_domain": DEFAULT_DNS_DOMAIN,
            "http_url": DEFAULT_HTTP_URL,
        }

        self.intervals = dict(DEFAULT_INTERVALS_SECONDS)
        if intervals:
            for key, value in intervals.items():
                if key in self.intervals:
                    self.intervals[key] = max(1, int(value))

    def set_targets(self, local_gateway: str, global_target: str, dns_domain: str, http_url: str) -> None:
        with self.config_lock:
            self.targets["local_gateway"] = local_gateway.strip()
            self.targets["global_target"] = global_target.strip()
            self.targets["dns_domain"] = dns_domain.strip()
            self.targets["http_url"] = http_url.strip()

        self._emit_ui(
            "Config",
            "Hedefler guncellendi",
            f"GW={local_gateway}, G={global_target}, DNS={dns_domain}, HTTP={http_url}",
        )

    def get_targets(self) -> Dict[str, str]:
        with self.config_lock:
            return dict(self.targets)

    def set_intervals(self, intervals: Dict[str, int]) -> None:
        with self.interval_lock:
            for key, value in intervals.items():
                if key in self.intervals:
                    self.intervals[key] = max(1, int(value))

    def get_intervals(self) -> Dict[str, int]:
        with self.interval_lock:
            return dict(self.intervals)

    def get_interval(self, key: str) -> int:
        with self.interval_lock:
            return int(self.intervals.get(key, 1))

    def _emit_ui(self, metric: str, value: str, status: str, raw_ms: Optional[float] = None) -> None:
        payload = {
            "time": now_str(),
            "metric": metric,
            "value": value,
            "status": status,
            "raw_ms": raw_ms,
            "epoch": time.time(),
        }
        try:
            self.ui_q.put_nowait(payload)
        except queue.Full:
            pass

    def _record(self, metric_key: str, value: str, status: str) -> None:
        row = build_empty_row()
        row[metric_key] = value
        row["Durum_Hatalar"] = status
        queue_row(self.log_q, row)

    def start(self) -> None:
        if self.started:
            return

        self.stop_event.clear()
        self.logger_thread = threading.Thread(
            target=logging_worker,
            args=(self.log_q, self.stop_event, self.csv_file),
            name="logger",
            daemon=True,
        )
        self.logger_thread.start()

        def local_ping_task() -> None:
            target = self.get_targets()["local_gateway"]
            ms, status = ping_once(target)
            value = normalize_ms(ms)
            full_status = f"LOCAL_{status}({target})"
            self._record("Lokal_Ping_ms", value, full_status)
            self._emit_ui("Lokal Ping", value, full_status, raw_ms=ms)

        def global_ping_task() -> None:
            target = self.get_targets()["global_target"]
            ms, status = ping_once(target)
            value = normalize_ms(ms)
            full_status = f"GLOBAL_{status}({target})"
            self._record("Global_Ping_ms", value, full_status)
            self._emit_ui("Global Ping", value, full_status, raw_ms=ms)

        def dns_task() -> None:
            domain = self.get_targets()["dns_domain"]
            ms, status = dns_lookup_ms(domain)
            value = normalize_ms(ms)
            full_status = f"{status}({domain})"
            self._record("DNS_ms", value, full_status)
            self._emit_ui("DNS", value, full_status)

        def http_task() -> None:
            url = self.get_targets()["http_url"]
            ms, status = http_get_ms(url)
            value = normalize_ms(ms)
            full_status = f"{status}({url})"
            self._record("HTTP_ms", value, full_status)
            self._emit_ui("HTTP", value, full_status)

        def speedtest_task() -> None:
            down, up, status = run_speedtest_mbps()
            row = build_empty_row()
            row["Download_Mbps"] = normalize_mbps(down)
            row["Upload_Mbps"] = normalize_mbps(up)
            row["Durum_Hatalar"] = status
            queue_row(self.log_q, row)

            shown = f"Down: {normalize_mbps(down)} | Up: {normalize_mbps(up)}"
            self._emit_ui("Speedtest", shown, status)

        self.threads = [
            threading.Thread(
                target=periodic_runner_dynamic,
                args=("local_ping", lambda: self.get_interval("local_ping"), self.stop_event, local_ping_task),
                daemon=True,
            ),
            threading.Thread(
                target=periodic_runner_dynamic,
                args=("global_ping", lambda: self.get_interval("global_ping"), self.stop_event, global_ping_task),
                daemon=True,
            ),
            threading.Thread(
                target=periodic_runner_dynamic,
                args=("dns", lambda: self.get_interval("dns"), self.stop_event, dns_task),
                daemon=True,
            ),
            threading.Thread(
                target=periodic_runner_dynamic,
                args=("http", lambda: self.get_interval("http"), self.stop_event, http_task),
                daemon=True,
            ),
            threading.Thread(
                target=periodic_runner_dynamic,
                args=("speedtest", lambda: self.get_interval("speedtest"), self.stop_event, speedtest_task),
                daemon=True,
            ),
        ]
        for t in self.threads:
            t.start()

        self.started = True

    def stop(self) -> None:
        if not self.started:
            return
        self.stop_event.set()
        for t in self.threads:
            t.join(timeout=2)
        if self.logger_thread is not None:
            self.logger_thread.join(timeout=3)
        self.started = False


# -----------------------------------------------------------------------------
# CSV plotting window (generic)
# -----------------------------------------------------------------------------
@dataclass
class HoverPoint:
    x: float
    y: float
    value: float
    label: str
    color: str


class CsvGraphWindow:
    def __init__(self, master: tk.Tk, title: str, series_specs: List[Tuple[str, str, str]], y_unit: str) -> None:
        self.window = tk.Toplevel(master)
        self.window.title(title)
        self.window.geometry("1180x760")
        self.window.minsize(980, 620)

        self.series_specs = series_specs
        self.y_unit = y_unit

        self.csv_path_var = tk.StringVar(value="CSV secilmedi")
        self.status_var = tk.StringVar(value="Bir CSV dosyasi secin.")
        self.visible_stats_var = tk.StringVar(value="Gorunen alan min/max: -")

        self.series_data: Dict[str, List[Tuple[float, float]]] = {k: [] for k, _, _ in series_specs}
        self.series_enabled: Dict[str, bool] = {k: True for k, _, _ in series_specs}

        now_epoch = time.time()
        self.data_start = now_epoch
        self.data_end = now_epoch
        self.window_seconds = 60.0
        self.view_end = now_epoch
        self.min_window_seconds = 10.0

        self.drag_x: Optional[int] = None
        self.drag_view_end: Optional[float] = None
        self.legend_hitboxes: List[Tuple[str, int, int, int, int]] = []
        self.hover_points: List[HoverPoint] = []
        self.hover_text_id: Optional[int] = None

        self._build_ui()
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.window, padding=10)
        frame.pack(fill="both", expand=True)

        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="CSV Yukle", command=self._choose_csv).pack(side="left")
        ttk.Label(toolbar, textvariable=self.csv_path_var).pack(side="left", padx=(12, 0))

        ttk.Label(frame, textvariable=self.status_var).pack(anchor="w", pady=(8, 3))
        ttk.Label(frame, textvariable=self.visible_stats_var).pack(anchor="w", pady=(0, 8))
        ttk.Label(
            frame,
            text="Mouse tekeri: zoom | Sol tus surukle: saga/sola kaydir | Legend cift tik: aktif/pasif",
        ).pack(anchor="w", pady=(0, 8))

        self.canvas = tk.Canvas(frame, bg="#0b1022", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.canvas.bind("<Configure>", lambda _e: self._render())
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)
        self.canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_drag_end)
        self.canvas.bind("<Double-Button-1>", self._on_legend_double_click)
        self.canvas.bind("<Motion>", self._on_mouse_move)
        self.canvas.bind("<Leave>", self._on_mouse_leave)

    def _plot_bounds(self) -> Tuple[int, int, int, int]:
        w = max(840, self.canvas.winfo_width())
        h = max(420, self.canvas.winfo_height())
        return 64, 18, w - 18, h - 40

    def _parse_float(self, raw: str) -> Optional[float]:
        if not raw or raw.strip().upper() == "N/A":
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    def _choose_csv(self) -> None:
        selected = filedialog.askopenfilename(
            title="CSV dosyasi sec",
            filetypes=[("CSV", "*.csv"), ("Tum dosyalar", "*.*")],
        )
        if selected:
            self._load_csv(selected)

    def _load_csv(self, path: str) -> None:
        parsed: Dict[str, List[Tuple[float, float]]] = {k: [] for k, _, _ in self.series_specs}
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    stamp = row.get("Tarih_Saat", "")
                    try:
                        epoch = dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").timestamp()
                    except ValueError:
                        continue
                    for key, _, _ in self.series_specs:
                        val = self._parse_float(str(row.get(key, "")))
                        if val is not None:
                            parsed[key].append((epoch, val))
        except OSError as exc:
            messagebox.showerror("CSV Hatasi", f"Dosya okunamadi: {exc}")
            return

        all_ts = [ts for values in parsed.values() for ts, _ in values]
        if not all_ts:
            messagebox.showwarning("CSV Bos", "Secilen dosyada cizilebilir sayisal veri bulunamadi.")
            return

        for key in parsed:
            parsed[key].sort(key=lambda x: x[0])

        self.series_data = parsed
        self.data_start = min(all_ts)
        self.data_end = max(all_ts)
        self.view_end = self.data_end
        self.window_seconds = max(1.0, self.data_end - self.data_start)
        self._clamp_view()

        self.csv_path_var.set(path)
        self.status_var.set(
            f"Yuklendi | Kayit araligi: {dt.datetime.fromtimestamp(self.data_start)} - "
            f"{dt.datetime.fromtimestamp(self.data_end)}"
        )
        self._render()

    def _clamp_view(self) -> None:
        span = max(1.0, self.data_end - self.data_start)
        min_window = self.min_window_seconds if span > self.min_window_seconds else 1.0
        self.window_seconds = max(min_window, min(self.window_seconds, span))
        self.view_end = min(self.view_end, self.data_end)
        self.view_end = max(self.view_end, self.data_start + self.window_seconds)

    def _visible_points(self) -> Dict[str, List[Tuple[float, float]]]:
        """
        Medium fix:
        Use bisect to avoid scanning full series each frame.
        """
        start = self.view_end - self.window_seconds
        result: Dict[str, List[Tuple[float, float]]] = {}
        for key, pts in self.series_data.items():
            ts_only = [t for t, _ in pts]
            left = bisect.bisect_left(ts_only, start)
            right = bisect.bisect_right(ts_only, self.view_end)
            result[key] = pts[left:right]
        return result

    def _on_mouse_wheel(self, event) -> None:
        if self.data_end <= self.data_start:
            return
        left, top, right, bottom = self._plot_bounds()
        if not (left <= event.x <= right and top <= event.y <= bottom):
            return
        factor = 0.8 if event.delta > 0 else 1.25
        span = max(1.0, self.data_end - self.data_start)
        min_window = self.min_window_seconds if span > self.min_window_seconds else 1.0
        new_window = max(min_window, min(span, self.window_seconds * factor))

        ratio = (event.x - left) / max(1.0, right - left)
        current_start = self.view_end - self.window_seconds
        cursor_time = current_start + ratio * self.window_seconds

        new_start = cursor_time - ratio * new_window
        self.window_seconds = new_window
        self.view_end = new_start + new_window
        self._clamp_view()
        self._render()

    def _on_drag_start(self, event) -> None:
        left, top, right, bottom = self._plot_bounds()
        if left <= event.x <= right and top <= event.y <= bottom:
            self.drag_x = event.x
            self.drag_view_end = self.view_end

    def _on_drag_move(self, event) -> None:
        if self.drag_x is None or self.drag_view_end is None:
            return
        left, _, right, _ = self._plot_bounds()
        dx = event.x - self.drag_x
        shift = (dx / max(1.0, right - left)) * self.window_seconds
        self.view_end = self.drag_view_end - shift
        self._clamp_view()
        self._render()

    def _on_drag_end(self, _event) -> None:
        self.drag_x = None
        self.drag_view_end = None

    def _on_legend_double_click(self, event) -> None:
        for key, x1, y1, x2, y2 in self.legend_hitboxes:
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                self.series_enabled[key] = not self.series_enabled[key]
                self._render()
                return

    def _on_mouse_move(self, event) -> None:
        nearest: Optional[HoverPoint] = None
        max_dist2 = 100
        for hp in self.hover_points:
            dx = event.x - hp.x
            dy = event.y - hp.y
            d2 = dx * dx + dy * dy
            if d2 <= max_dist2:
                nearest = hp
                max_dist2 = d2

        if nearest is None:
            self._on_mouse_leave(None)
            return

        if self.hover_text_id is not None:
            self.canvas.delete(self.hover_text_id)
        self.hover_text_id = self.canvas.create_text(
            event.x + 12,
            event.y - 12,
            anchor="w",
            fill=nearest.color,
            text=f"{nearest.label}: {nearest.value:.2f} {self.y_unit}",
            font=("Segoe UI", 9, "bold"),
        )

    def _on_mouse_leave(self, _event) -> None:
        if self.hover_text_id is not None:
            self.canvas.delete(self.hover_text_id)
            self.hover_text_id = None

    def _render(self) -> None:
        self.canvas.delete("all")
        self.hover_points.clear()
        self.hover_text_id = None

        left, top, right, bottom = self._plot_bounds()
        self.canvas.create_rectangle(left, top, right, bottom, outline="#4b567e", width=1)

        visible = self._visible_points()
        active_vals = [v for key, pts in visible.items() if self.series_enabled.get(key, True) for _, v in pts]
        max_y = max(20.0, max(active_vals) * 1.1 if active_vals else 100.0)

        for i in range(6):
            y = top + ((bottom - top) * i / 5)
            self.canvas.create_line(left, y, right, y, fill="#1e2a4a", dash=(2, 4))
            y_val = max_y * (1 - i / 5)
            self.canvas.create_text(left - 8, y, anchor="e", fill="#b0bee8", text=f"{y_val:.0f} {self.y_unit}")

        view_start = self.view_end - self.window_seconds
        for i in range(6):
            ratio = i / 5
            x = left + (right - left) * ratio
            ts = view_start + self.window_seconds * ratio
            self.canvas.create_line(x, top, x, bottom, fill="#16203b", dash=(1, 6))
            self.canvas.create_text(
                x, bottom + 14, anchor="n", fill="#7f91c9", text=dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            )

        plot_w = max(1.0, right - left)
        plot_h = max(1.0, bottom - top)

        for key, label, color in self.series_specs:
            if not self.series_enabled.get(key, True):
                continue
            pts = visible.get(key, [])
            if len(pts) < 2:
                continue

            line_coords: List[float] = []
            for ts, val in pts:
                x = left + ((ts - view_start) / self.window_seconds) * plot_w
                y = bottom - (val / max_y) * plot_h
                line_coords.extend([x, y])
                self.hover_points.append(HoverPoint(x, y, val, label, color))

            self.canvas.create_line(line_coords, fill=color, width=2, smooth=False)
            lx, ly = line_coords[-2], line_coords[-1]
            self.canvas.create_oval(lx - 2, ly - 2, lx + 2, ly + 2, fill=color, outline=color)

        self.legend_hitboxes.clear()
        legend_x = left + 8
        for key, label, color in self.series_specs:
            enabled = self.series_enabled.get(key, True)
            text_color = "#d7defa" if enabled else "#9aa3ba"
            self.canvas.create_rectangle(legend_x, top + 8, legend_x + 10, top + 18, fill=color, outline=color)
            text_id = self.canvas.create_text(legend_x + 16, top + 13, anchor="w", fill=text_color, text=label)
            bbox = self.canvas.bbox(text_id)
            if bbox:
                x1 = legend_x
                y1 = top + 6
                x2 = max(legend_x + 12, bbox[2])
                y2 = top + 20
                self.legend_hitboxes.append((key, x1, y1, x2, y2))
                legend_x = x2 + 22
            else:
                legend_x += 140

        stats = []
        for key, label, _ in self.series_specs:
            if not self.series_enabled.get(key, True):
                continue
            vals = [v for _, v in visible.get(key, [])]
            if vals:
                stats.append(f"{label} Min:{min(vals):.2f} Max:{max(vals):.2f}")
        self.visible_stats_var.set(" | ".join(stats) if stats else "Gorunen alanda aktif seri verisi yok")
        self.status_var.set(
            f"Gorunen aralik: {dt.datetime.fromtimestamp(view_start)} - "
            f"{dt.datetime.fromtimestamp(self.view_end)} | Zoom: {int(self.window_seconds)} sn"
        )


# -----------------------------------------------------------------------------
# Quality scoring window
# -----------------------------------------------------------------------------
class QualityGraphWindow:
    def __init__(self, app: "InternetEkgApp", title: str, mode: str) -> None:
        assert mode in {"local", "internet"}
        self.app = app
        self.mode = mode

        self.window = tk.Toplevel(app.root)
        self.window.title(title)
        self.window.geometry("1180x760")
        self.window.minsize(980, 620)

        self.csv_path = os.path.abspath(CSV_FILE)
        self.csv_path_var = tk.StringVar(value=self.csv_path)
        self.status_var = tk.StringVar(value="Bir CSV secin veya canli izleyin.")
        self.visible_stats_var = tk.StringVar(value="Gorunen skor min/max: -")
        self.live_var = tk.BooleanVar(value=True)

        now_epoch = time.time()
        self.series: List[Tuple[float, float]] = []
        self.data_start = now_epoch
        self.data_end = now_epoch
        self.window_seconds = 60.0
        self.view_end = now_epoch
        self.min_window_seconds = 10.0
        self.base_interval_seconds = 1

        self.drag_x: Optional[int] = None
        self.drag_view_end: Optional[float] = None
        self.hover_points: List[Tuple[float, float, float]] = []
        self.hover_text_id: Optional[int] = None

        self._build_ui()
        self.window.protocol("WM_DELETE_WINDOW", self._on_close)

        self._rebuild_series()
        self._schedule_live_refresh()

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.window, padding=10)
        frame.pack(fill="both", expand=True)

        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x")

        ttk.Button(toolbar, text="CSV Yukle", command=self._choose_csv).pack(side="left")
        ttk.Checkbutton(toolbar, text="Canli (aktif CSV)", variable=self.live_var, command=self._on_live_toggle).pack(
            side="left", padx=(8, 8)
        )
        ttk.Label(toolbar, textvariable=self.csv_path_var).pack(side="left")

        ttk.Label(frame, textvariable=self.status_var).pack(anchor="w", pady=(8, 3))
        ttk.Label(frame, textvariable=self.visible_stats_var).pack(anchor="w", pady=(0, 8))
        ttk.Label(frame, text="Mouse tekeri: zoom | Sol tus surukle: saga/sola kaydir").pack(anchor="w", pady=(0, 8))

        self.canvas = tk.Canvas(frame, bg="#0b1022", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.canvas.bind("<Configure>", lambda _e: self._render())
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)
        self.canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_drag_end)
        self.canvas.bind("<Motion>", self._on_mouse_move)
        self.canvas.bind("<Leave>", self._on_mouse_leave)

    def _on_live_toggle(self) -> None:
        if self.live_var.get():
            self.csv_path = os.path.abspath(CSV_FILE)
            self.csv_path_var.set(self.csv_path)
        self._rebuild_series()

    def _choose_csv(self) -> None:
        selected = filedialog.askopenfilename(
            title="CSV dosyasi sec",
            filetypes=[("CSV", "*.csv"), ("Tum dosyalar", "*.*")],
        )
        if selected:
            self.live_var.set(False)
            self.csv_path = selected
            self.csv_path_var.set(selected)
            self._rebuild_series()

    def _on_close(self) -> None:
        self.live_var.set(False)
        self.window.destroy()

    def _schedule_live_refresh(self) -> None:
        # Runtime fix: avoid TclError after window destroy.
        try:
            if self.window.winfo_exists():
                if self.live_var.get():
                    self._rebuild_series()
                self.window.after(2000, self._schedule_live_refresh)
        except tk.TclError:
            return

    def _current_base_interval(self) -> int:
        intervals = self.app.collect_intervals_from_ui()
        if intervals is None:
            intervals = self.app.service.get_intervals()

        if self.mode == "local":
            return max(1, int(intervals.get("local_ping", 1)))

        # Requirement: use the minimum interval among related measurements.
        return max(
            1,
            min(
                int(intervals.get("global_ping", 1)),
                int(intervals.get("dns", 1)),
                int(intervals.get("http", 1)),
            ),
        )

    @staticmethod
    def _row_float(row: Dict[str, str], key: str) -> Optional[float]:
        raw = str(row.get(key, ""))
        if not raw or raw.upper() == "N/A":
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    def _parse_rows(self, path: str) -> List[Dict[str, object]]:
        rows: List[Dict[str, object]] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    stamp = row.get("Tarih_Saat", "")
                    try:
                        ts = dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").timestamp()
                    except ValueError:
                        continue

                    status = str(row.get("Durum_Hatalar") or "").upper()
                    rows.append(
                        {
                            "ts": ts,
                            "status": status,
                            "local": self._row_float(row, "Lokal_Ping_ms"),
                            "global": self._row_float(row, "Global_Ping_ms"),
                            "dns": self._row_float(row, "DNS_ms"),
                            "http": self._row_float(row, "HTTP_ms"),
                        }
                    )
        except OSError as exc:
            debug_log(f"quality csv read error: {exc}")
            return []

        rows.sort(key=lambda x: x["ts"])
        return rows

    @staticmethod
    def _stddev(values: List[float]) -> float:
        if len(values) < 2:
            return 0.0
        avg = sum(values) / len(values)
        var = sum((v - avg) ** 2 for v in values) / len(values)
        return math.sqrt(var)

    def _compute_local_score(self, rows: List[Dict[str, object]]) -> Optional[float]:
        local_values = [float(r["local"]) for r in rows if r["local"] is not None]
        local_attempts = [r for r in rows if str(r["status"]).startswith("LOCAL_") or r["local"] is not None]
        if not local_attempts or not local_values:
            return None

        local_success = [r for r in local_attempts if r["local"] is not None and "TIMEOUT" not in str(r["status"])]
        uptime_ratio = len(local_success) / len(local_attempts)
        loss_pct = (1.0 - uptime_ratio) * 100.0
        p_uptime = max(0.0, 40.0 - (loss_pct * 2.0))

        avg_ping = sum(local_values) / len(local_values)
        p_latency = 30.0 if avg_ping <= 5.0 else max(0.0, 30.0 - ((avg_ping - 5.0) * 2.0))

        jitter = self._stddev(local_values)
        p_jitter = 30.0 if jitter <= 3.0 else max(0.0, 30.0 - ((jitter - 3.0) * 3.0))
        return p_uptime + p_latency + p_jitter

    def _compute_internet_score(self, rows: List[Dict[str, object]]) -> Optional[float]:
        global_values = [float(r["global"]) for r in rows if r["global"] is not None]
        dns_values = [float(r["dns"]) for r in rows if r["dns"] is not None]
        http_values = [float(r["http"]) for r in rows if r["http"] is not None]

        global_attempts = [r for r in rows if str(r["status"]).startswith("GLOBAL_") or r["global"] is not None]
        # Logical fix: include HTTPS status patterns too.
        http_attempts = [
            r
            for r in rows
            if r["http"] is not None
            or str(r["status"]).startswith("HTTP_")
            or "HTTP://" in str(r["status"])
            or "HTTPS://" in str(r["status"])
        ]

        attempts = len(global_attempts) + len(http_attempts)
        if attempts == 0 or not global_values or not dns_values or not http_values:
            return None

        global_success = [r for r in global_attempts if r["global"] is not None and "TIMEOUT" not in str(r["status"])]
        http_success = [r for r in http_attempts if r["http"] is not None and "TIMEOUT" not in str(r["status"])]
        success_ratio = (len(global_success) + len(http_success)) / attempts
        loss_pct = (1.0 - success_ratio) * 100.0
        p_uptime = max(0.0, 40.0 - (loss_pct * 2.0))

        avg_global = sum(global_values) / len(global_values)
        jitter = self._stddev(global_values)
        p_route = 30.0
        if avg_global > 30.0:
            p_route -= ((avg_global - 30.0) / 5.0) * 2.0
        if jitter > 10.0:
            p_route -= ((jitter - 10.0) / 2.0) * 2.0
        p_route = max(0.0, p_route)

        avg_dns = sum(dns_values) / len(dns_values)
        avg_http = sum(http_values) / len(http_values)
        p_service = 30.0
        if avg_dns > 50.0:
            p_service -= ((avg_dns - 50.0) / 10.0)
        if avg_http > 200.0:
            p_service -= ((avg_http - 200.0) / 20.0)
        p_service = max(0.0, p_service)

        return p_uptime + p_route + p_service

    def _build_score_series(self, rows: List[Dict[str, object]], interval_s: int) -> List[Tuple[float, float]]:
        if not rows:
            return []

        out: List[Tuple[float, float]] = []
        start_ts = int(float(rows[0]["ts"]))
        end_ts = int(float(rows[-1]["ts"]))
        bucket_start = start_ts
        cursor = 0
        n = len(rows)

        while bucket_start <= end_ts:
            bucket_end = bucket_start + interval_s
            bucket_rows: List[Dict[str, object]] = []
            while cursor < n and float(rows[cursor]["ts"]) < bucket_end:
                if float(rows[cursor]["ts"]) >= bucket_start:
                    bucket_rows.append(rows[cursor])
                cursor += 1

            score = self._compute_local_score(bucket_rows) if self.mode == "local" else self._compute_internet_score(bucket_rows)
            if score is not None:
                out.append((bucket_end, max(0.0, min(100.0, score))))
            bucket_start = bucket_end

        return out

    def _rebuild_series(self) -> None:
        path = self.csv_path if not self.live_var.get() else os.path.abspath(CSV_FILE)
        self.csv_path = path
        self.csv_path_var.set(path)

        prev_view_end = self.view_end
        prev_window = self.window_seconds
        had_prev = bool(self.series)

        rows = self._parse_rows(path)
        if not rows:
            self.series = []
            self.status_var.set("Skor cizmek icin uygun veri bulunamadi.")
            self._render()
            return

        self.base_interval_seconds = self._current_base_interval()
        self.series = self._build_score_series(rows, self.base_interval_seconds)
        if not self.series:
            self.status_var.set("Skor olusturmak icin yeterli olcum yok.")
            self._render()
            return

        self.data_start = self.series[0][0]
        self.data_end = self.series[-1][0]

        # Logical fix: keep user viewport while live-refreshing.
        if had_prev:
            self.view_end = prev_view_end
            self.window_seconds = prev_window
        else:
            self.view_end = self.data_end
            self.window_seconds = max(1.0, self.data_end - self.data_start)

        self._clamp_view()
        mode_label = "Lokal Ag Kalitesi" if self.mode == "local" else "Internet Kalitesi"
        self.status_var.set(
            f"{mode_label} | Hesap adimi: {self.base_interval_seconds} sn (ilgili olcumlerdeki en kisa aralik)"
        )
        self._render()

    def _clamp_view(self) -> None:
        span = max(1.0, self.data_end - self.data_start)
        min_window = self.min_window_seconds if span > self.min_window_seconds else 1.0
        self.window_seconds = max(min_window, min(self.window_seconds, span))
        self.view_end = min(self.view_end, self.data_end)
        self.view_end = max(self.view_end, self.data_start + self.window_seconds)

    def _plot_bounds(self) -> Tuple[int, int, int, int]:
        w = max(840, self.canvas.winfo_width())
        h = max(420, self.canvas.winfo_height())
        return 64, 18, w - 18, h - 40

    def _on_mouse_wheel(self, event) -> None:
        if self.data_end <= self.data_start:
            return
        left, top, right, bottom = self._plot_bounds()
        if not (left <= event.x <= right and top <= event.y <= bottom):
            return

        factor = 0.8 if event.delta > 0 else 1.25
        span = max(1.0, self.data_end - self.data_start)
        min_window = self.min_window_seconds if span > self.min_window_seconds else 1.0
        new_window = max(min_window, min(span, self.window_seconds * factor))

        ratio = (event.x - left) / max(1.0, right - left)
        start = self.view_end - self.window_seconds
        cursor_time = start + ratio * self.window_seconds
        new_start = cursor_time - ratio * new_window

        self.window_seconds = new_window
        self.view_end = new_start + new_window
        self._clamp_view()
        self._render()

    def _on_drag_start(self, event) -> None:
        left, top, right, bottom = self._plot_bounds()
        if left <= event.x <= right and top <= event.y <= bottom:
            self.drag_x = event.x
            self.drag_view_end = self.view_end

    def _on_drag_move(self, event) -> None:
        if self.drag_x is None or self.drag_view_end is None:
            return
        left, _, right, _ = self._plot_bounds()
        dx = event.x - self.drag_x
        shift = (dx / max(1.0, right - left)) * self.window_seconds
        self.view_end = self.drag_view_end - shift
        self._clamp_view()
        self._render()

    def _on_drag_end(self, _event) -> None:
        self.drag_x = None
        self.drag_view_end = None

    def _on_mouse_move(self, event) -> None:
        nearest = None
        max_d2 = 100
        for x, y, v in self.hover_points:
            dx = event.x - x
            dy = event.y - y
            d2 = dx * dx + dy * dy
            if d2 <= max_d2:
                nearest = (x, y, v)
                max_d2 = d2

        if nearest is None:
            self._on_mouse_leave(None)
            return
        if self.hover_text_id is not None:
            self.canvas.delete(self.hover_text_id)
        self.hover_text_id = self.canvas.create_text(
            event.x + 12,
            event.y - 12,
            anchor="w",
            fill="#f9f2a7",
            text=f"Skor: {nearest[2]:.2f}",
            font=("Segoe UI", 9, "bold"),
        )

    def _on_mouse_leave(self, _event) -> None:
        if self.hover_text_id is not None:
            self.canvas.delete(self.hover_text_id)
            self.hover_text_id = None

    def _render(self) -> None:
        self.canvas.delete("all")
        self.hover_points.clear()
        self.hover_text_id = None

        left, top, right, bottom = self._plot_bounds()
        self.canvas.create_rectangle(left, top, right, bottom, outline="#4b567e", width=1)

        if not self.series:
            self.visible_stats_var.set("Gorunen skor min/max: -")
            return

        start = self.view_end - self.window_seconds
        visible = [(ts, val) for ts, val in self.series if start <= ts <= self.view_end]

        for i in range(6):
            y = top + ((bottom - top) * i / 5)
            y_val = 100 * (1 - i / 5)
            self.canvas.create_line(left, y, right, y, fill="#1e2a4a", dash=(2, 4))
            self.canvas.create_text(left - 8, y, anchor="e", fill="#b0bee8", text=f"{y_val:.0f}")

        for i in range(6):
            ratio = i / 5
            x = left + (right - left) * ratio
            ts = start + self.window_seconds * ratio
            self.canvas.create_line(x, top, x, bottom, fill="#16203b", dash=(1, 6))
            self.canvas.create_text(
                x, bottom + 14, anchor="n", fill="#7f91c9", text=dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            )

        if len(visible) >= 2:
            line: List[float] = []
            pw = max(1.0, right - left)
            ph = max(1.0, bottom - top)
            for ts, val in visible:
                x = left + ((ts - start) / self.window_seconds) * pw
                y = bottom - (val / 100.0) * ph
                line.extend([x, y])
                self.hover_points.append((x, y, val))
            self.canvas.create_line(line, fill="#f9f2a7", width=2, smooth=False)

        if visible:
            vals = [v for _, v in visible]
            self.visible_stats_var.set(f"Gorunen skor Min:{min(vals):.2f} Max:{max(vals):.2f}")
        else:
            self.visible_stats_var.set("Gorunen alanda skor verisi yok")


# -----------------------------------------------------------------------------
# Main app
# -----------------------------------------------------------------------------
class InternetEkgApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Internet EKG")
        self.root.geometry("980x720")
        self.root.minsize(900, 620)

        self.ui_q: queue.Queue = queue.Queue(maxsize=2000)
        self.interval_defaults = self._load_interval_defaults()
        self.service = MonitorService(CSV_FILE, self.ui_q, intervals=self.interval_defaults)

        self.last_values: Dict[str, tk.StringVar] = {
            "Lokal Ping": tk.StringVar(value="N/A"),
            "Global Ping": tk.StringVar(value="N/A"),
            "DNS": tk.StringVar(value="N/A"),
            "HTTP": tk.StringVar(value="N/A"),
            "Speedtest": tk.StringVar(value="N/A"),
        }

        targets = self.service.get_targets()
        self.gateway_var = tk.StringVar(value=targets["local_gateway"])
        self.global_var = tk.StringVar(value=targets["global_target"])
        self.dns_var = tk.StringVar(value=targets["dns_domain"])
        self.http_var = tk.StringVar(value=targets["http_url"])

        self.status_var = tk.StringVar(value="Hazir")
        self.last_update_var = tk.StringVar(value="Son Guncelleme: -")

        self.local_ping_series: Deque[Tuple[float, float]] = deque(maxlen=GRAPH_POINTS_LIMIT)
        self.global_ping_series: Deque[Tuple[float, float]] = deque(maxlen=GRAPH_POINTS_LIMIT)
        self.graph_scale_max = 120.0

        self.csv_graph_window: Optional[CsvGraphWindow] = None
        self.speedtest_graph_window: Optional[CsvGraphWindow] = None
        self.local_quality_window: Optional[QualityGraphWindow] = None
        self.internet_quality_window: Optional[QualityGraphWindow] = None

        self.interval_metric_keys = ["Lokal Ping", "Global Ping", "DNS", "HTTP", "Speedtest"]
        self.metric_to_interval_key = {
            "Lokal Ping": "local_ping",
            "Global Ping": "global_ping",
            "DNS": "dns",
            "HTTP": "http",
            "Speedtest": "speedtest",
        }
        self.interval_value_vars: Dict[str, tk.StringVar] = {}
        self.interval_unit_vars: Dict[str, tk.StringVar] = {}
        self.interval_value_boxes: Dict[str, ttk.Combobox] = {}

        self.tray_icon = None
        self.tray_thread = None
        self.is_in_tray = False

        self._build_ui()
        self._poll_ui_queue()
        self._refresh_graph()

        self.root.protocol("WM_DELETE_WINDOW", self.minimize_to_tray)

    def _load_interval_defaults(self) -> Dict[str, int]:
        defaults = dict(DEFAULT_INTERVALS_SECONDS)
        if not Path(INTERVAL_SETTINGS_FILE).exists():
            return defaults
        try:
            with open(INTERVAL_SETTINGS_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, json.JSONDecodeError):
            return defaults
        if not isinstance(loaded, dict):
            return defaults

        for key in defaults:
            raw = loaded.get(key)
            if isinstance(raw, (int, float)):
                defaults[key] = max(1, int(raw))
        return defaults

    def _seconds_to_value_unit(self, seconds: int) -> Tuple[int, str]:
        if seconds % UNIT_SECONDS["Saat"] == 0:
            return max(1, seconds // UNIT_SECONDS["Saat"]), "Saat"
        if seconds % UNIT_SECONDS["Dakika"] == 0:
            return max(1, seconds // UNIT_SECONDS["Dakika"]), "Dakika"
        return max(1, int(seconds)), "Saniye"

    def _value_unit_to_seconds(self, value_text: str, unit_text: str) -> Optional[int]:
        if unit_text not in UNIT_SECONDS:
            return None
        try:
            value = int(value_text)
        except ValueError:
            return None
        if value < 1:
            return None
        return value * UNIT_SECONDS[unit_text]

    def _on_interval_unit_changed(self, interval_key: str) -> None:
        unit = self.interval_unit_vars[interval_key].get()
        values = [str(i) for i in range(1, 25)] if unit == "Saat" else [str(i) for i in range(1, 60)]
        box = self.interval_value_boxes[interval_key]
        current = self.interval_value_vars[interval_key].get()
        box["values"] = values
        if current not in values:
            self.interval_value_vars[interval_key].set(values[0])

    def collect_intervals_from_ui(self) -> Optional[Dict[str, int]]:
        collected: Dict[str, int] = {}
        for key in DEFAULT_INTERVALS_SECONDS:
            val_text = self.interval_value_vars[key].get()
            unit_text = self.interval_unit_vars[key].get()
            sec = self._value_unit_to_seconds(val_text, unit_text)
            if sec is None:
                return None
            collected[key] = sec
        return collected

    def apply_intervals(self) -> bool:
        intervals = self.collect_intervals_from_ui()
        if intervals is None:
            messagebox.showwarning("Aralik Hatasi", "Olcum araligi icin gecerli sayi ve birim secin.")
            return False
        self.service.set_intervals(intervals)
        self.interval_defaults = dict(intervals)
        self._append_log("Olcum araliklari guncellendi: " + ", ".join(f"{k}={v}s" for k, v in intervals.items()))
        return True

    def save_interval_defaults(self) -> None:
        intervals = self.collect_intervals_from_ui()
        if intervals is None:
            messagebox.showwarning("Aralik Hatasi", "Default kaydetmeden once gecerli deger secin.")
            return
        try:
            with open(INTERVAL_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(intervals, f, indent=2)
        except OSError as exc:
            messagebox.showerror("Kaydetme Hatasi", f"Default ayarlar kaydedilemedi: {exc}")
            return
        self.interval_defaults = dict(intervals)
        self.service.set_intervals(intervals)
        self._append_log("Olcum araliklari default olarak kaydedildi.")
        messagebox.showinfo("Kaydedildi", "Olcum araligi default ayarlari kaydedildi.")

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=10)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Internet EKG - Canli Ag Izleme", font=("Segoe UI", 14, "bold")).pack(anchor="w")

        target_box = ttk.LabelFrame(frame, text="Hedef Ayarlari", padding=10)
        target_box.pack(fill="x", pady=(8, 8))

        ttk.Label(target_box, text="Gateway (Lokal Ping):").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(target_box, textvariable=self.gateway_var, width=28).grid(row=0, column=1, sticky="w", padx=6, pady=3)

        ttk.Label(target_box, text="Global Ping Hedefi:").grid(row=0, column=2, sticky="w", pady=3)
        ttk.Entry(target_box, textvariable=self.global_var, width=28).grid(row=0, column=3, sticky="w", padx=6, pady=3)

        ttk.Label(target_box, text="DNS Domain:").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(target_box, textvariable=self.dns_var, width=28).grid(row=1, column=1, sticky="w", padx=6, pady=3)

        ttk.Label(target_box, text="HTTP URL:").grid(row=1, column=2, sticky="w", pady=3)
        ttk.Entry(target_box, textvariable=self.http_var, width=28).grid(row=1, column=3, sticky="w", padx=6, pady=3)

        ttk.Button(target_box, text="Hedefleri Uygula", command=self.apply_targets).grid(
            row=2, column=3, sticky="e", padx=6, pady=(6, 0)
        )

        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=(0, 8))

        self.start_btn = ttk.Button(controls, text="Izlemeyi Baslat", command=self.start_monitoring)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(controls, text="Durdur", command=self.stop_monitoring, state="disabled")
        self.stop_btn.pack(side="left", padx=8)

        ttk.Button(controls, text="Sistem Tepsisine Kucult", command=self.minimize_to_tray).pack(side="left", padx=8)
        ttk.Button(controls, text="CSV Grafik Penceresi", command=self.open_csv_graph_window).pack(side="left", padx=8)
        ttk.Button(controls, text="Speedtest Grafik", command=self.open_speedtest_graph_window).pack(side="left", padx=8)
        ttk.Button(controls, text="Lokal Ag Kalitesi", command=self.open_local_quality_window).pack(side="left", padx=8)
        ttk.Button(controls, text="Internet Kalitesi", command=self.open_internet_quality_window).pack(side="left", padx=8)

        ttk.Label(controls, textvariable=self.status_var, foreground="#0a5").pack(side="left", padx=12)

        metrics_box = ttk.LabelFrame(frame, text="Son Olcumler", padding=10)
        metrics_box.pack(fill="x", pady=(0, 8))

        ttk.Label(metrics_box, text="Olcum", width=14).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Label(metrics_box, text="Deger", width=52).grid(row=0, column=1, sticky="w", pady=2)
        ttk.Label(metrics_box, text="Aralik", width=8).grid(row=0, column=2, sticky="w", pady=2)
        ttk.Label(metrics_box, text="Birim", width=8).grid(row=0, column=3, sticky="w", pady=2)

        row = 1
        for metric_name in self.interval_metric_keys:
            var = self.last_values[metric_name]
            key = self.metric_to_interval_key[metric_name]
            default_seconds = self.interval_defaults.get(key, DEFAULT_INTERVALS_SECONDS[key])
            default_value, default_unit = self._seconds_to_value_unit(default_seconds)

            value_var = tk.StringVar(value=str(default_value))
            unit_var = tk.StringVar(value=default_unit)

            ttk.Label(metrics_box, text=f"{metric_name}:", width=14).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Label(metrics_box, textvariable=var, width=52).grid(row=row, column=1, sticky="w", pady=2)

            value_box = ttk.Combobox(metrics_box, textvariable=value_var, width=6, state="readonly")
            value_box.grid(row=row, column=2, sticky="w", pady=2, padx=(4, 2))

            unit_box = ttk.Combobox(metrics_box, textvariable=unit_var, width=8, state="readonly")
            unit_box["values"] = ["Saniye", "Dakika", "Saat"]
            unit_box.grid(row=row, column=3, sticky="w", pady=2, padx=(2, 2))

            self.interval_value_vars[key] = value_var
            self.interval_unit_vars[key] = unit_var
            self.interval_value_boxes[key] = value_box

            unit_box.bind("<<ComboboxSelected>>", lambda _e, k=key: self._on_interval_unit_changed(k))
            self._on_interval_unit_changed(key)
            row += 1

        ttk.Button(metrics_box, text="Araliklari Uygula", command=self.apply_intervals).grid(
            row=row, column=2, sticky="w", pady=(6, 2), padx=(4, 2)
        )
        ttk.Button(metrics_box, text="Default Olarak Kaydet", command=self.save_interval_defaults).grid(
            row=row, column=3, sticky="w", pady=(6, 2), padx=(2, 2)
        )

        ttk.Label(frame, textvariable=self.last_update_var).pack(anchor="w", pady=(0, 6))

        chart_frame = ttk.LabelFrame(frame, text="Ping Trendi (Son 5 Dakika)", padding=8)
        chart_frame.pack(fill="both", expand=True)

        self.chart_pane = ttk.Panedwindow(chart_frame, orient="vertical")
        self.chart_pane.pack(fill="both", expand=True)

        graph_wrap = ttk.Frame(self.chart_pane)
        log_wrap = ttk.Frame(self.chart_pane)
        self.chart_pane.add(graph_wrap, weight=3)
        self.chart_pane.add(log_wrap, weight=2)

        self.graph_canvas = tk.Canvas(graph_wrap, height=260, bg="#0f1220", highlightthickness=0)
        self.graph_canvas.pack(fill="both", expand=True)

        legend = ttk.Frame(chart_frame)
        legend.pack(fill="x", pady=(5, 4))
        ttk.Label(legend, text="Mavi: Lokal Ping   |   Turuncu: Global Ping").pack(anchor="w")
        ttk.Label(
            legend,
            text="Grafik boyutunu ayarlamak icin grafik ile log arasindaki cizgiyi fare ile surukleyin.",
        ).pack(anchor="w")

        self.log_box = tk.Text(log_wrap, height=10, wrap="none")
        self.log_box.pack(fill="both", expand=True)
        self.log_box.configure(state="disabled")

    def open_csv_graph_window(self) -> None:
        if self.csv_graph_window and self.csv_graph_window.window.winfo_exists():
            self.csv_graph_window.window.deiconify()
            self.csv_graph_window.window.lift()
            self.csv_graph_window.window.focus_force()
            return
        self.csv_graph_window = CsvGraphWindow(
            self.root,
            title="CSV Grafik Inceleme",
            series_specs=[
                ("Lokal_Ping_ms", "Lokal Ping", "#43a2ff"),
                ("Global_Ping_ms", "Global Ping", "#ff9e57"),
                ("DNS_ms", "DNS", "#6de28d"),
                ("HTTP_ms", "HTTP", "#f2d15b"),
            ],
            y_unit="ms",
        )

    def open_speedtest_graph_window(self) -> None:
        if self.speedtest_graph_window and self.speedtest_graph_window.window.winfo_exists():
            self.speedtest_graph_window.window.deiconify()
            self.speedtest_graph_window.window.lift()
            self.speedtest_graph_window.window.focus_force()
            return
        self.speedtest_graph_window = CsvGraphWindow(
            self.root,
            title="Speedtest CSV Grafik",
            series_specs=[
                ("Download_Mbps", "Download", "#4cc3ff"),
                ("Upload_Mbps", "Upload", "#ffa24d"),
            ],
            y_unit="Mbps",
        )

    def open_local_quality_window(self) -> None:
        if self.local_quality_window and self.local_quality_window.window.winfo_exists():
            self.local_quality_window.window.deiconify()
            self.local_quality_window.window.lift()
            self.local_quality_window.window.focus_force()
            return
        self.local_quality_window = QualityGraphWindow(self, title="Lokal Ag Kalite Puani", mode="local")

    def open_internet_quality_window(self) -> None:
        if self.internet_quality_window and self.internet_quality_window.window.winfo_exists():
            self.internet_quality_window.window.deiconify()
            self.internet_quality_window.window.lift()
            self.internet_quality_window.window.focus_force()
            return
        self.internet_quality_window = QualityGraphWindow(self, title="Internet Kalite Puani", mode="internet")

    def _append_log(self, line: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{now_str()}] {line}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _validate_targets(self) -> Tuple[bool, str]:
        local_gateway = self.gateway_var.get().strip()
        global_target = self.global_var.get().strip()
        dns_domain = self.dns_var.get().strip()
        http_url = self.http_var.get().strip()

        if not all([local_gateway, global_target, dns_domain, http_url]):
            return False, "Tum hedef alanlari doldurulmalidir."
        if not (http_url.startswith("http://") or http_url.startswith("https://")):
            return False, "HTTP URL http:// veya https:// ile baslamalidir."
        return True, ""

    def apply_targets(self) -> bool:
        valid, err = self._validate_targets()
        if not valid:
            messagebox.showwarning("Hedef Hatasi", err)
            return False
        self.service.set_targets(
            local_gateway=self.gateway_var.get(),
            global_target=self.global_var.get(),
            dns_domain=self.dns_var.get(),
            http_url=self.http_var.get(),
        )
        self._append_log("Yeni hedef ayarlari uygulandi.")
        return True

    def start_monitoring(self) -> None:
        if not self.apply_targets():
            return
        if not self.apply_intervals():
            return
        debug_log("Monitoring started")
        self.service.start()
        self.status_var.set("Calisiyor")
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._append_log("Izleme baslatildi.")

    def stop_monitoring(self) -> None:
        debug_log("Monitoring stopped")
        self.service.stop()
        self.status_var.set("Durduruldu")
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self._append_log("Izleme durduruldu.")

    def _prune_old_points(self) -> None:
        cutoff = time.time() - GRAPH_WINDOW_SECONDS
        while self.local_ping_series and self.local_ping_series[0][0] < cutoff:
            self.local_ping_series.popleft()
        while self.global_ping_series and self.global_ping_series[0][0] < cutoff:
            self.global_ping_series.popleft()

    def _draw_series(
        self,
        points: List[Tuple[float, float]],
        color: str,
        min_t: float,
        max_t: float,
        max_y: float,
        w: int,
        h: int,
    ) -> None:
        if len(points) < 2:
            return
        left, top, right, bottom = 40, 12, w - 12, h - 24
        pw = max(1, right - left)
        ph = max(1, bottom - top)
        span = max(1.0, max_t - min_t)

        line: List[float] = []
        for ts, val in points:
            x = left + ((ts - min_t) / span) * pw
            y = bottom - (val / max_y) * ph
            line.extend([x, y])

        self.graph_canvas.create_line(line, fill=color, width=2, smooth=False)
        lx, ly = line[-2], line[-1]
        self.graph_canvas.create_oval(lx - 3, ly - 3, lx + 3, ly + 3, fill=color, outline=color)

    @staticmethod
    def _percentile(values: List[float], q: float) -> float:
        if not values:
            return 0.0
        vals = sorted(values)
        if len(vals) == 1:
            return vals[0]
        pos = (len(vals) - 1) * q
        lo, hi = math.floor(pos), math.ceil(pos)
        if lo == hi:
            return vals[lo]
        return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)

    @staticmethod
    def _calc_stats(points: List[Tuple[float, float]]) -> Optional[Tuple[float, float, float, float]]:
        if not points:
            return None
        vals = [v for _, v in points]
        return min(vals), max(vals), sum(vals) / len(vals), vals[-1]

    def _refresh_graph(self) -> None:
        self._prune_old_points()
        self.graph_canvas.delete("all")

        w = max(700, self.graph_canvas.winfo_width())
        h = max(220, self.graph_canvas.winfo_height())
        left, top, right, bottom = 40, 12, w - 12, h - 24
        self.graph_canvas.create_rectangle(left, top, right, bottom, outline="#444a66", width=1)

        now_ts = time.time()
        min_t = now_ts - GRAPH_WINDOW_SECONDS

        local = list(self.local_ping_series)
        global_ = list(self.global_ping_series)
        vals = [v for _, v in local] + [v for _, v in global_]
        if not vals:
            self.graph_scale_max = 120.0
        else:
            p95 = self._percentile(vals, 0.95)
            peak = max(vals)
            target = max(30.0, p95 * 1.35, peak * 1.05)
            self.graph_scale_max = target if target > self.graph_scale_max else (self.graph_scale_max * 0.9 + target * 0.1)
        max_y = self.graph_scale_max

        for i in range(1, 6):
            y = top + ((bottom - top) * i / 5)
            self.graph_canvas.create_line(left, y, right, y, fill="#1f2740", dash=(3, 4))
            self.graph_canvas.create_text(left - 6, y, anchor="e", text=f"{max_y * (1 - i / 5):.0f}", fill="#9aa7d8")

        for minute_back in range(1, 5):
            tick_ts = now_ts - minute_back * 60
            x = left + ((tick_ts - min_t) / GRAPH_WINDOW_SECONDS) * (right - left)
            self.graph_canvas.create_line(x, top, x, bottom, fill="#1a2036", dash=(2, 6))
            self.graph_canvas.create_text(x, bottom + 12, anchor="center", text=f"-{minute_back} dk", fill="#7f8ab8")

        self._draw_series(local, "#43a2ff", min_t, now_ts, max_y, w, h)
        self._draw_series(global_, "#ff9e57", min_t, now_ts, max_y, w, h)

        self.graph_canvas.create_text(left + 3, top + 8, anchor="w", text=f"Olcek ust sinir: {max_y:.0f} ms", fill="#d7defa")
        self.graph_canvas.create_text(right - 3, bottom + 12, anchor="e", text="Simdi", fill="#d7defa")
        self.graph_canvas.create_text(left + 3, bottom + 12, anchor="w", text="-5 dk", fill="#d7defa")

        ls = self._calc_stats(local)
        gs = self._calc_stats(global_)
        sy = top + 8
        if ls:
            self.graph_canvas.create_text(
                right - 8,
                sy,
                anchor="ne",
                fill="#86c6ff",
                text=f"Lokal Son:{ls[3]:.1f} Ort:{ls[2]:.1f} Min:{ls[0]:.1f} Max:{ls[1]:.1f}",
            )
            sy += 16
        if gs:
            self.graph_canvas.create_text(
                right - 8,
                sy,
                anchor="ne",
                fill="#ffc08f",
                text=f"Global Son:{gs[3]:.1f} Ort:{gs[2]:.1f} Min:{gs[0]:.1f} Max:{gs[1]:.1f}",
            )

        self.root.after(1000, self._refresh_graph)

    def _poll_ui_queue(self) -> None:
        try:
            while True:
                item = self.ui_q.get_nowait()
                metric = str(item["metric"])
                value = str(item["value"])
                status = str(item["status"])
                stamp = str(item["time"])

                if metric in self.last_values:
                    self.last_values[metric].set(value)

                if metric == "Lokal Ping" and item.get("raw_ms") is not None:
                    self.local_ping_series.append((float(item["epoch"]), float(item["raw_ms"])))
                if metric == "Global Ping" and item.get("raw_ms") is not None:
                    self.global_ping_series.append((float(item["epoch"]), float(item["raw_ms"])))

                self.last_update_var.set(f"Son Guncelleme: {stamp}")
                self._append_log(f"{metric} -> {value} | {status}")
        except queue.Empty:
            pass

        self.root.after(350, self._poll_ui_queue)

    def _create_tray_image(self):
        if Image is None or ImageDraw is None:
            return None
        image = Image.new("RGB", (64, 64), "#1f6aa5")
        draw = ImageDraw.Draw(image)
        draw.rectangle((8, 8, 56, 56), fill="#123456")
        draw.text((18, 20), "EKG", fill="#ffffff")
        return image

    def _tray_show(self, icon, item):
        self.root.after(0, self.restore_from_tray)

    def _tray_quit(self, icon, item):
        self.root.after(0, self._on_close)

    def _run_tray_icon(self):
        if pystray is None:
            return
        icon_image = self._create_tray_image()
        if icon_image is None:
            return
        menu = pystray.Menu(pystray.MenuItem("Goster", self._tray_show), pystray.MenuItem("Cikis", self._tray_quit))
        self.tray_icon = pystray.Icon("internet_ekg", icon_image, "Internet EKG", menu)
        self.tray_icon.run()

    def minimize_to_tray(self) -> None:
        if pystray is None:
            self._append_log("Tepsi ozelligi icin: pip install pystray pillow")
            self.root.iconify()
            return
        if self.is_in_tray:
            return
        if self.tray_icon is not None:
            return
        if self.tray_thread is not None and self.tray_thread.is_alive():
            return
        self.is_in_tray = True
        self.root.withdraw()
        self._append_log("Uygulama sistem tepsisine kucultuldu.")
        self.tray_thread = threading.Thread(target=self._run_tray_icon, daemon=True)
        self.tray_thread.start()

    def restore_from_tray(self) -> None:
        if self.tray_icon is not None:
            self.tray_icon.stop()
            self.tray_icon = None
        self.is_in_tray = False
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self._append_log("Uygulama tepsiden geri yuklendi.")

    def _on_close(self) -> None:
        if self.tray_icon is not None:
            self.tray_icon.stop()
            self.tray_icon = None
        self.service.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = InternetEkgApp(root)
    # DEBUG: basic invariant checks
    assert app.service is not None
    root.mainloop()


if __name__ == "__main__":
    main()
