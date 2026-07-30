import ctypes
import json
import socket
import struct
import threading
import time
import tkinter as tk
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from statistics import median
from tkinter import ttk
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# 확인하려는 서버 주소로 변경하세요.
SERVER_URL = "https://store.frommyarti.com/"

# 프로그램 버전
APP_VERSION = "1.6.0"

# 예약 좌클릭 설정입니다. 빈 시각은 예약 기능을 비활성화합니다.
# 입력 예시: "2026-07-30 12:00:00.000"
TARGET_CLICK_TIME = "2026-07-30 12:00:00.000"
CLICK_MAX_LATE_SECONDS = 2
CLICK_NOT_BEFORE_MS = 20

# 프로그램에서 변경한 목표시각을 저장할 파일
SETTINGS_PATH = Path(__file__).with_name("settings.json")

# 서버 시간 재동기화 간격(초)과 한 번에 측정할 횟수
SYNC_INTERVAL_SECONDS = 10
SYNC_SAMPLE_COUNT = 5
SERVER_OFFSET_CONFIRMATIONS = 3

# PC 시계의 밀리초 오차를 자동 보정할 NTP 설정
NTP_SERVERS = (
    "time.google.com",
    "time.cloudflare.com",
    "time.windows.com",
    "pool.ntp.org",
)
NTP_SAMPLE_COUNT = len(NTP_SERVERS)
NTP_TIMEOUT_SECONDS = 1
NTP_EPOCH_DELTA = 2_208_988_800

# 화면 시간 갱신 간격(밀리초)
DISPLAY_INTERVAL_MS = 10


TARGET_PART_NAMES = (
    "year",
    "month",
    "day",
    "hour",
    "minute",
    "second",
    "millisecond",
)


def target_parts_from_text(target_text):
    target = datetime.strptime(target_text, "%Y-%m-%d %H:%M:%S.%f")
    return {
        "year": str(target.year),
        "month": f"{target.month:02d}",
        "day": f"{target.day:02d}",
        "hour": f"{target.hour:02d}",
        "minute": f"{target.minute:02d}",
        "second": f"{target.second:02d}",
        "millisecond": f"{target.microsecond // 1000:03d}",
    }


def target_text_from_parts(parts):
    millisecond = int(parts["millisecond"])
    if not 0 <= millisecond <= 999:
        raise ValueError("millisecond out of range")

    target = datetime(
        int(parts["year"]),
        int(parts["month"]),
        int(parts["day"]),
        int(parts["hour"]),
        int(parts["minute"]),
        int(parts["second"]),
        millisecond * 1000,
    )
    return target.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def load_target_parts():
    default_parts = target_parts_from_text(TARGET_CLICK_TIME)
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        saved_parts = {
            name: str(saved[name]).strip()
            for name in TARGET_PART_NAMES
        }
        return target_parts_from_text(target_text_from_parts(saved_parts))
    except (
        OSError,
        KeyError,
        OverflowError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return default_parts


def save_target_parts(parts):
    SETTINGS_PATH.write_text(
        json.dumps(parts, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def get_cursor_position():
    point = wintypes.POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
        return None
    return point.x, point.y


def click_current_position():
    cursor_position = get_cursor_position()
    if cursor_position is None:
        raise OSError("현재 마우스 좌표를 읽지 못했습니다.")

    user32 = ctypes.windll.user32
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.01)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    return cursor_position


class ScheduledLeftClick:
    def __init__(self, clock, target_text, click_action):
        self.clock = clock
        self.click_action = click_action
        self._lock = threading.Lock()
        self._generation = 0
        self.target_utc = None
        self.completed = True
        self.triggered = False
        self.status = "좌클릭 예약: 비활성"
        self.arm(target_text)

    def arm(self, target_text):
        target_text = target_text.strip()
        if not target_text:
            with self._lock:
                self._generation += 1
                self.target_utc = None
                self.completed = True
                self.triggered = False
                self.status = "좌클릭 예약: 비활성"
            return True

        try:
            local_target = datetime.strptime(
                target_text,
                "%Y-%m-%d %H:%M:%S.%f",
            )
        except ValueError:
            with self._lock:
                self._generation += 1
                self.target_utc = None
                self.completed = True
                self.triggered = False
                self.status = (
                    "좌클릭 예약 오류: YYYY-MM-DD HH:MM:SS.mmm 형식 필요"
                )
            return False

        local_timezone = datetime.now().astimezone().tzinfo
        target_utc = local_target.replace(
            tzinfo=local_timezone
        ).astimezone(timezone.utc)

        with self._lock:
            self._generation += 1
            self.target_utc = target_utc
            self.completed = False
            self.triggered = False
            self.status = (
                f"좌클릭 예약: {target_text} | 현재 마우스 위치"
            )
        return True

    def reject(self, status):
        with self._lock:
            self._generation += 1
            self.target_utc = None
            self.completed = True
            self.triggered = False
            self.status = status

    def check(self):
        with self._lock:
            if self.completed or self.target_utc is None:
                return None
            target_utc = self.target_utc
            generation = self._generation

        server_utc, _, _ = self.clock.snapshot()
        if server_utc is None:
            with self._lock:
                if generation == self._generation:
                    self.status = "좌클릭 예약: 서버 시간 동기화 대기 중"
            return None

        remaining_seconds = (target_utc - server_utc).total_seconds()
        safe_remaining_seconds = (
            remaining_seconds + CLICK_NOT_BEFORE_MS / 1000
        )
        if safe_remaining_seconds > 0:
            return safe_remaining_seconds

        if -remaining_seconds > CLICK_MAX_LATE_SECONDS:
            with self._lock:
                if generation == self._generation:
                    self.completed = True
                    self.status = (
                        "좌클릭 예약 취소: 목표 시각이 이미 지났습니다."
                    )
            return None

        with self._lock:
            if generation != self._generation or self.completed:
                return None
            self.completed = True

        try:
            x, y = self.click_action()
            with self._lock:
                if generation == self._generation:
                    self.triggered = True
                    self.status = (
                        f"좌클릭 실행 완료: 현재 좌표 ({x}, {y})"
                    )
        except OSError as error:
            with self._lock:
                if generation == self._generation:
                    self.status = f"좌클릭 실행 실패: {error}"

        return None

    def run(self, should_run):
        while should_run():
            remaining_seconds = self.check()

            if remaining_seconds is None:
                time.sleep(0.02)
            elif remaining_seconds > 1:
                time.sleep(min(0.1, remaining_seconds - 0.5))
            elif remaining_seconds > 0.02:
                time.sleep(0.005)
            else:
                time.sleep(0)


class ServerClock:
    def __init__(self, url):
        self.url = url
        self._lock = threading.Lock()
        self._server_utc = None
        self._synced_monotonic = None
        self._latency_ms = None
        self._ntp_offset_seconds = None
        self._stable_server_offset_seconds = 0
        self._pending_server_offset_seconds = None
        self._pending_offset_confirmations = 0
        self._offset_status = ""
        self._status = "서버 시간 동기화 대기 중"

    def _fetch_ntp_sample(self, ntp_server):
        packet = b"\x1b" + 47 * b"\0"

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(NTP_TIMEOUT_SECONDS)
            started = time.time()
            client.sendto(packet, (ntp_server, 123))
            data, _ = client.recvfrom(48)
            finished = time.time()

        if len(data) < 48:
            raise ValueError("NTP 응답 길이가 올바르지 않습니다.")

        values = struct.unpack("!12I", data[:48])
        received = (
            values[8] - NTP_EPOCH_DELTA + values[9] / 2**32
        )
        transmitted = (
            values[10] - NTP_EPOCH_DELTA + values[11] / 2**32
        )
        offset_seconds = (
            (received - started) + (transmitted - finished)
        ) / 2
        network_delay_seconds = (
            (finished - started) - (transmitted - received)
        )

        return {
            "server": ntp_server,
            "offset_seconds": offset_seconds,
            "delay_seconds": max(0.0, network_delay_seconds),
        }

    def _fetch_sample(self):
        started_wall_ns = time.time_ns()
        started_monotonic_ns = time.perf_counter_ns()

        request = Request(
            self.url,
            method="HEAD",
            headers={
                "User-Agent": "Mozilla/5.0 ServerTimeViewer/1.0",
                "Cache-Control": "no-cache",
            },
        )
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            if error.code not in (405, 501):
                raise
            request = Request(
                self.url,
                method="GET",
                headers={
                    "User-Agent": "Mozilla/5.0 ServerTimeViewer/1.0",
                    "Cache-Control": "no-cache",
                    "Range": "bytes=0-0",
                },
            )
            response = urlopen(request, timeout=5)

        with response:
            date_header = response.headers.get("Date")

        finished_monotonic_ns = time.perf_counter_ns()
        finished_wall_ns = time.time_ns()
        if not date_header:
            raise ValueError("응답에 Date 헤더가 없습니다.")

        server_utc = parsedate_to_datetime(date_header)
        if server_utc.tzinfo is None:
            server_utc = server_utc.replace(tzinfo=timezone.utc)
        else:
            server_utc = server_utc.astimezone(timezone.utc)

        return {
            "server_second": int(server_utc.timestamp()),
            "midpoint_wall_ns": (
                started_wall_ns + finished_wall_ns
            ) // 2,
            "latency_ns": finished_monotonic_ns - started_monotonic_ns,
            "finished_wall_ns": finished_wall_ns,
            "finished_monotonic_ns": finished_monotonic_ns,
        }

    def _select_stable_server_offset(self, measured_offset, samples):
        if measured_offset == self._stable_server_offset_seconds:
            self._pending_server_offset_seconds = None
            self._pending_offset_confirmations = 0
            self._offset_status = ""
            return self._stable_server_offset_seconds

        measured_count = sum(
            sample["offset_seconds"] == measured_offset
            for sample in samples
        )
        if measured_count != len(samples):
            self._pending_server_offset_seconds = None
            self._pending_offset_confirmations = 0
            self._offset_status = ""
            return self._stable_server_offset_seconds

        if self._pending_server_offset_seconds == measured_offset:
            self._pending_offset_confirmations += 1
        else:
            self._pending_server_offset_seconds = measured_offset
            self._pending_offset_confirmations = 1

        self._offset_status = (
            f", 초 확인 {self._pending_offset_confirmations}"
            f"/{SERVER_OFFSET_CONFIRMATIONS}"
        )
        if (
            self._pending_offset_confirmations
            < SERVER_OFFSET_CONFIRMATIONS
        ):
            return self._stable_server_offset_seconds

        self._stable_server_offset_seconds = measured_offset
        self._pending_server_offset_seconds = None
        self._pending_offset_confirmations = 0
        self._offset_status = ""
        return self._stable_server_offset_seconds

    def sync(self):
        ntp_samples = []
        for index in range(NTP_SAMPLE_COUNT):
            ntp_server = NTP_SERVERS[index % len(NTP_SERVERS)]
            try:
                ntp_samples.append(
                    self._fetch_ntp_sample(ntp_server)
                )
            except (OSError, ValueError, TimeoutError):
                pass

        if ntp_samples:
            ntp_offset_seconds = median(
                sample["offset_seconds"]
                for sample in ntp_samples
            )
            with self._lock:
                self._ntp_offset_seconds = ntp_offset_seconds
            ntp_status = (
                f"NTP 자동 {ntp_offset_seconds * 1000:+.1f} ms "
                f"({len(ntp_samples)}/{NTP_SAMPLE_COUNT})"
            )
        else:
            with self._lock:
                ntp_offset_seconds = self._ntp_offset_seconds
            if ntp_offset_seconds is None:
                with self._lock:
                    self._server_utc = None
                    self._status = "동기화 실패: NTP 서버 연결 불가"
                return
            else:
                ntp_status = (
                    f"NTP 재사용 {ntp_offset_seconds * 1000:+.1f} ms"
                )

        samples = []
        last_error = None

        for _ in range(SYNC_SAMPLE_COUNT):
            try:
                samples.append(self._fetch_sample())
            except (HTTPError, URLError, ValueError, TimeoutError) as error:
                last_error = error

        if not samples:
            with self._lock:
                self._status = f"동기화 실패: {last_error}"
            return

        for sample in samples:
            corrected_midpoint_second = int(
                sample["midpoint_wall_ns"] / 1_000_000_000
                + ntp_offset_seconds
            )
            sample["offset_seconds"] = (
                sample["server_second"] - corrected_midpoint_second
            )

        measured_offset = int(
            median(sample["offset_seconds"] for sample in samples)
        )
        selected_offset = 0
        self._stable_server_offset_seconds = 0
        self._pending_server_offset_seconds = None
        self._pending_offset_confirmations = 0
        self._offset_status = ""
        server_offset_status = "서버 보정 없음"
        matching_samples = [
            sample
            for sample in samples
            if sample["offset_seconds"] in (
                selected_offset,
                measured_offset,
            )
        ]
        best_sample = min(
            matching_samples or samples,
            key=lambda sample: sample["latency_ns"],
        )

        estimated_server_timestamp = (
            best_sample["finished_wall_ns"] / 1_000_000_000
            + ntp_offset_seconds
            + selected_offset
        )
        estimated_server_utc = datetime.fromtimestamp(
            estimated_server_timestamp,
            tz=timezone.utc,
        )

        with self._lock:
            self._server_utc = estimated_server_utc
            self._synced_monotonic = (
                best_sample["finished_monotonic_ns"] / 1_000_000_000
            )
            self._latency_ms = best_sample["latency_ns"] / 1_000_000
            self._status = (
                f"동기화 정상 | {ntp_status} | "
                f"{server_offset_status}{self._offset_status}"
            )

    def snapshot(self):
        with self._lock:
            if self._server_utc is None:
                return None, self._status, self._latency_ms

            elapsed = time.monotonic() - self._synced_monotonic
            current_utc = self._server_utc + timedelta(seconds=elapsed)
            return current_utc, self._status, self._latency_ms


class ServerTimeViewer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"실시간 서버 시간 v{APP_VERSION}")
        self.geometry("520x380")
        self.resizable(False, False)
        self.overrideredirect(True)

        saved_target_parts = load_target_parts()
        saved_target_text = target_text_from_parts(saved_target_parts)
        self.clock = ServerClock(SERVER_URL)
        self.click_scheduler = ScheduledLeftClick(
            self.clock,
            saved_target_text,
            click_current_position,
        )
        self._running = True
        self._last_move_refresh_ns = 0
        self._drag_offset_x = 0
        self._drag_offset_y = 0
        self._restore_override_pending = False

        self.url_text = tk.StringVar(value=SERVER_URL)
        self.time_text = tk.StringVar(value="--:--:--.---")
        self.date_text = tk.StringVar(value="----년 --월 --일")
        self.status_text = tk.StringVar(value="서버 시간 동기화 대기 중")
        self.latency_text = tk.StringVar(value="응답 지연: -- ms")
        self.target_part_text = {
            name: tk.StringVar(value=saved_target_parts[name])
            for name in TARGET_PART_NAMES
        }
        self.schedule_text = tk.StringVar(
            value=self.click_scheduler.status
        )
        self.cursor_text = tk.StringVar(
            value="현재 마우스 좌표: X=--, Y=--"
        )

        self._build_ui()
        self.bind("<Map>", self._restore_after_minimize)
        self.after(10, self._show_in_taskbar)
        self.protocol("WM_DELETE_WINDOW", self._close)

        threading.Thread(target=self._sync_loop, daemon=True).start()
        threading.Thread(
            target=self.click_scheduler.run,
            args=(lambda: self._running,),
            daemon=True,
        ).start()
        self._update_display()

    def _build_ui(self):
        outer = tk.Frame(
            self,
            background="#d9d9d9",
            borderwidth=1,
            relief="solid",
        )
        outer.pack(fill="both", expand=True)

        title_bar = tk.Frame(
            outer,
            height=32,
            background="#f0f0f0",
        )
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)

        title_label = tk.Label(
            title_bar,
            text=f"  ◒  실시간 서버 시간 v{APP_VERSION}",
            background="#f0f0f0",
            foreground="#555555",
            anchor="w",
        )
        title_label.pack(side="left", fill="both", expand=True)

        close_button = tk.Button(
            title_bar,
            text="×",
            command=self._close,
            borderwidth=0,
            background="#f0f0f0",
            activebackground="#e81123",
            activeforeground="white",
            width=5,
        )
        close_button.pack(side="right", fill="y")

        minimize_button = tk.Button(
            title_bar,
            text="—",
            command=self._minimize,
            borderwidth=0,
            background="#f0f0f0",
            activebackground="#dddddd",
            width=5,
        )
        minimize_button.pack(side="right", fill="y")

        for drag_widget in (title_bar, title_label):
            drag_widget.bind("<ButtonPress-1>", self._start_drag)
            drag_widget.bind("<B1-Motion>", self._drag_window)

        frame = ttk.Frame(outer, padding=18)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="대상 서버").pack(anchor="w")
        ttk.Label(
            frame,
            textvariable=self.url_text,
            foreground="#555555",
        ).pack(anchor="w", pady=(2, 14))

        ttk.Label(
            frame,
            textvariable=self.time_text,
            font=("Consolas", 30, "bold"),
        ).pack()
        ttk.Label(
            frame,
            textvariable=self.date_text,
            font=("맑은 고딕", 11),
        ).pack(pady=(2, 14))

        schedule_frame = ttk.Frame(frame)
        schedule_frame.pack(fill="x", pady=(0, 10))
        ttk.Label(schedule_frame, text="목표 클릭 시각").pack(anchor="w")
        target_input_frame = ttk.Frame(schedule_frame)
        target_input_frame.pack(fill="x", pady=(4, 0))

        target_fields = (
            ("year", "년", 5),
            ("month", "월", 3),
            ("day", "일", 3),
            ("hour", "시", 3),
            ("minute", "분", 3),
            ("second", "초", 3),
            ("millisecond", "ms", 4),
        )
        for name, label, width in target_fields:
            target_entry = ttk.Entry(
                target_input_frame,
                textvariable=self.target_part_text[name],
                width=width,
                justify="center",
            )
            target_entry.pack(side="left", padx=(0, 2))
            target_entry.bind("<Return>", self._apply_click_schedule)
            ttk.Label(target_input_frame, text=label).pack(
                side="left",
                padx=(0, 4),
            )

        ttk.Button(
            target_input_frame,
            text="예약 적용",
            command=self._apply_click_schedule,
        ).pack(side="right")

        ttk.Separator(frame).pack(fill="x", pady=(0, 10))
        ttk.Label(frame, textvariable=self.status_text).pack(anchor="w")
        ttk.Label(frame, textvariable=self.latency_text).pack(anchor="w")
        ttk.Label(frame, textvariable=self.schedule_text).pack(anchor="w")
        ttk.Label(frame, textvariable=self.cursor_text).pack(anchor="w")

    def _apply_click_schedule(self, _event=None):
        entered_parts = {
            name: value.get().strip()
            for name, value in self.target_part_text.items()
        }
        try:
            target_text = target_text_from_parts(entered_parts)
            normalized_parts = target_parts_from_text(target_text)
            save_target_parts(normalized_parts)
        except (OSError, OverflowError, TypeError, ValueError) as error:
            self.click_scheduler.reject(
                f"좌클릭 예약 오류: 날짜/시간 또는 설정 파일 확인 ({error})"
            )
            self.schedule_text.set(self.click_scheduler.status)
            return

        for name, value in normalized_parts.items():
            self.target_part_text[name].set(value)

        self.click_scheduler.arm(target_text)
        self.schedule_text.set(self.click_scheduler.status)

    def _sync_loop(self):
        while self._running:
            self.clock.sync()
            for _ in range(SYNC_INTERVAL_SECONDS * 10):
                if not self._running:
                    return
                time.sleep(0.1)

    def _render_display(self):
        server_utc, status, latency_ms = self.clock.snapshot()
        self.status_text.set(status)
        self.schedule_text.set(self.click_scheduler.status)

        cursor_position = get_cursor_position()
        if cursor_position is not None:
            x, y = cursor_position
            self.cursor_text.set(f"현재 마우스 좌표: X={x}, Y={y}")

        if latency_ms is not None:
            self.latency_text.set(f"응답 지연: {latency_ms:.1f} ms")

        if server_utc is not None:
            local_time = server_utc.astimezone()
            self.time_text.set(local_time.strftime("%H:%M:%S.%f")[:-3])
            self.date_text.set(local_time.strftime("%Y년 %m월 %d일"))

    def _update_display(self):
        self._render_display()

        if self._running:
            self.after(DISPLAY_INTERVAL_MS, self._update_display)

    def _start_drag(self, event):
        self._drag_offset_x = event.x_root - self.winfo_x()
        self._drag_offset_y = event.y_root - self.winfo_y()

    def _drag_window(self, event):
        x = event.x_root - self._drag_offset_x
        y = event.y_root - self._drag_offset_y
        self.geometry(f"+{x}+{y}")

        now_ns = time.perf_counter_ns()
        if now_ns - self._last_move_refresh_ns < 10_000_000:
            return

        self._last_move_refresh_ns = now_ns
        self._render_display()
        self.update_idletasks()

    def _minimize(self):
        self._restore_override_pending = True
        self.overrideredirect(False)
        self.iconify()

    def _restore_after_minimize(self, _event):
        if not self._restore_override_pending or self.state() != "normal":
            return

        self._restore_override_pending = False
        self.after_idle(self._restore_custom_frame)

    def _restore_custom_frame(self):
        self.overrideredirect(True)
        self._show_in_taskbar()

    def _show_in_taskbar(self):
        self.update_idletasks()
        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_long,
        ]

        window_handle = user32.GetParent(self.winfo_id())
        extended_style = user32.GetWindowLongW(window_handle, -20)
        extended_style &= ~0x00000080
        extended_style |= 0x00040000
        user32.SetWindowLongW(window_handle, -20, extended_style)

        self.withdraw()
        self.after(10, self.deiconify)

    def _close(self):
        self._running = False
        self.destroy()


if __name__ == "__main__":
    app = ServerTimeViewer()
    app.mainloop()
