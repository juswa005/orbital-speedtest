from __future__ import annotations

import argparse
import curses
import json
import locale
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Sequence

from orbital_speedtest import __version__


SPINNER = ("|", "/", "-", "\\")


class SpeedtestError(RuntimeError):
    """Raised when the underlying speedtest command fails."""


@dataclass(slots=True)
class SpeedResult:
    download_bps: float
    upload_bps: float
    ping_ms: float
    server_name: str
    sponsor: str
    distance_km: float
    isp: str
    external_ip: str
    timestamp: str

    @property
    def download_mbps(self) -> float:
        return self.download_bps / 1_000_000

    @property
    def upload_mbps(self) -> float:
        return self.upload_bps / 1_000_000


@dataclass(slots=True)
class Assessment:
    label: str
    headline: str
    summary: str
    score: int


@dataclass(slots=True)
class AppState:
    running: bool = False
    started_at: float = 0.0
    status_message: str = "Press r to begin a speed test."
    result: SpeedResult | None = None
    assessment: Assessment | None = None
    error_message: str | None = None
    completed_at: float = 0.0


def parse_speedtest_json(payload: str) -> SpeedResult:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as error:
        raise SpeedtestError("speedtest-cli returned invalid JSON") from error

    try:
        server = data["server"]
        client = data["client"]
        return SpeedResult(
            download_bps=float(data["download"]),
            upload_bps=float(data["upload"]),
            ping_ms=float(data["ping"]),
            server_name=str(server["name"]),
            sponsor=str(server["sponsor"]),
            distance_km=float(server["d"]),
            isp=str(client["isp"]),
            external_ip=str(client["ip"]),
            timestamp=str(data["timestamp"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SpeedtestError("speedtest-cli JSON payload is missing expected fields") from error


def score_download(download_mbps: float) -> int:
    if download_mbps >= 150:
        return 5
    if download_mbps >= 75:
        return 4
    if download_mbps >= 25:
        return 3
    if download_mbps >= 10:
        return 2
    return 1


def score_upload(upload_mbps: float) -> int:
    if upload_mbps >= 40:
        return 5
    if upload_mbps >= 15:
        return 4
    if upload_mbps >= 5:
        return 3
    if upload_mbps >= 2:
        return 2
    return 1


def score_ping(ping_ms: float) -> int:
    if ping_ms <= 15:
        return 5
    if ping_ms <= 30:
        return 4
    if ping_ms <= 60:
        return 3
    if ping_ms <= 120:
        return 2
    return 1


def classify_result(result: SpeedResult) -> Assessment:
    download_score = score_download(result.download_mbps)
    upload_score = score_upload(result.upload_mbps)
    ping_score = score_ping(result.ping_ms)
    weighted = (download_score * 2 + upload_score + ping_score) / 4

    if weighted >= 4.5 and min(download_score, upload_score, ping_score) >= 4:
        return Assessment(
            label="Mission-grade",
            headline="Deep space link is exceptionally strong.",
            summary="Excellent for gaming, 4K streaming, cloud backups, and large multi-device workloads.",
            score=5,
        )
    if weighted >= 3.5:
        return Assessment(
            label="Fast",
            headline="Mission link is comfortably above everyday demand.",
            summary="Strong enough for video calls, heavy downloads, and multiple active devices.",
            score=4,
        )
    if weighted >= 2.5:
        return Assessment(
            label="Operational",
            headline="Connection is healthy for normal mission traffic.",
            summary="Good for browsing, work calls, and HD streaming, with some limits under heavy load.",
            score=3,
        )
    if weighted >= 1.5:
        return Assessment(
            label="Slow",
            headline="Link remains usable but the bandwidth ceiling is low.",
            summary="Expect delays on downloads, updates, uploads, and higher-quality streaming.",
            score=2,
        )
    return Assessment(
        label="Critical",
        headline="Mission link is degraded.",
        summary="Expect buffering, stalled transfers, and unreliable real-time communication.",
        score=1,
    )


def run_speedtest() -> SpeedResult:
    try:
        completed = subprocess.run(
            ["speedtest-cli", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except FileNotFoundError as error:
        raise SpeedtestError("speedtest-cli is not installed or not on PATH") from error
    except subprocess.TimeoutExpired as error:
        raise SpeedtestError("speedtest timed out before telemetry returned") from error

    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "speedtest-cli failed"
        raise SpeedtestError(message)

    return parse_speedtest_json(completed.stdout)


def plain_summary(result: SpeedResult, assessment: Assessment) -> str:
    return (
        f"Status: {assessment.label}\n"
        f"Download: {result.download_mbps:.2f} Mbps\n"
        f"Upload: {result.upload_mbps:.2f} Mbps\n"
        f"Ping: {result.ping_ms:.2f} ms\n"
        f"Server: {result.server_name} ({result.sponsor})\n"
        f"ISP: {result.isp}\n"
        f"IP: {result.external_ip}\n"
        f"Assessment: {assessment.summary}"
    )


def fit_text(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return text[:1]
    return text[: width - 1] + "…"


def draw_text(screen: curses.window, y: int, x: int, text: str, width: int | None = None, attr: int = 0) -> None:
    if y < 0 or x < 0:
        return
    if width is None:
        width = max(0, screen.getmaxyx()[1] - x)
    if width <= 0:
        return
    try:
        screen.addnstr(y, x, text, width, attr)
    except curses.error:
        return


def init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)
    curses.init_pair(2, curses.COLOR_BLUE, -1)
    curses.init_pair(3, curses.COLOR_GREEN, -1)
    curses.init_pair(4, curses.COLOR_YELLOW, -1)
    curses.init_pair(5, curses.COLOR_RED, -1)
    curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_CYAN)


def assessment_color(score: int | None) -> int:
    if score is None:
        return curses.color_pair(2)
    if score >= 5:
        return curses.color_pair(3)
    if score >= 4:
        return curses.color_pair(1)
    if score >= 3:
        return curses.color_pair(4)
    return curses.color_pair(5)


def mission_phase(elapsed: float) -> str:
    if elapsed < 3:
        return "Calibrating ground station"
    if elapsed < 8:
        return "Measuring downlink throughput"
    if elapsed < 13:
        return "Measuring uplink throughput"
    return "Crunching telemetry"


def draw_box(screen: curses.window, top: int, left: int, height: int, width: int, title: str) -> None:
    if height < 3 or width < 4:
        return
    draw_text(screen, top, left, "+" + "-" * (width - 2) + "+", width, curses.color_pair(2))
    draw_text(screen, top + 1, left, "|", 1, curses.color_pair(2))
    draw_text(screen, top + 1, left + 2, fit_text(title, width - 4), width - 4, curses.color_pair(1) | curses.A_BOLD)
    draw_text(screen, top + 1, left + width - 1, "|", 1, curses.color_pair(2))
    for row in range(2, height - 1):
        draw_text(screen, top + row, left, "|", 1, curses.color_pair(2))
        draw_text(screen, top + row, left + width - 1, "|", 1, curses.color_pair(2))
    draw_text(screen, top + height - 1, left, "+" + "-" * (width - 2) + "+", width, curses.color_pair(2))


def draw_header(screen: curses.window, width: int, state: AppState) -> int:
    draw_text(screen, 0, 0, " " * width, width, curses.color_pair(1))
    draw_text(screen, 0, 2, fit_text(" ORBITAL SPEEDTEST // DEEP SPACE TELEMETRY ", width - 4), width - 4, curses.color_pair(1) | curses.A_BOLD)
    status = state.status_message if not state.error_message else state.error_message
    attr = curses.color_pair(5) if state.error_message else curses.color_pair(2)
    draw_text(screen, 1, 2, fit_text(status, width - 4), width - 4, attr)
    return 3


def draw_live_panel(screen: curses.window, state: AppState, top: int, left: int, height: int, width: int) -> None:
    draw_box(screen, top, left, height, width, "Launch Status")
    if height < 5:
        return
    if state.running:
        elapsed = max(0.0, time.time() - state.started_at)
        spinner = SPINNER[int(elapsed * 6) % len(SPINNER)]
        lines = [
            f"{spinner} Test in progress",
            f"Phase     : {mission_phase(elapsed)}",
            f"Elapsed   : {elapsed:0.1f}s",
            "Telemetry stream remains active.",
        ]
        color = curses.color_pair(6)
    elif state.result and state.assessment:
        age = max(0, int(time.time() - state.completed_at)) if state.completed_at else 0
        lines = [
            f"Last test : {age}s ago",
            f"Rating    : {state.assessment.label}",
            f"Headline  : {state.assessment.headline}",
            "Press r to run another pass.",
        ]
        color = assessment_color(state.assessment.score)
    else:
        lines = [
            "Awaiting launch command.",
            "Press r to start a speed test.",
            "The result will be classified after telemetry returns.",
        ]
        color = curses.color_pair(4)

    for index, line in enumerate(lines, start=2):
        if index >= height - 1:
            break
        if index == 2 and state.running:
            draw_text(screen, top + index, left + 2, fit_text(line, width - 4), width - 4, color)
        else:
            draw_text(screen, top + index, left + 2, fit_text(line, width - 4), width - 4, color)


def draw_result_panel(screen: curses.window, state: AppState, top: int, left: int, height: int, width: int) -> None:
    draw_box(screen, top, left, height, width, "Telemetry")
    result = state.result
    if not result or height < 5:
        draw_text(screen, top + 2, left + 2, fit_text("No telemetry captured yet.", width - 4), width - 4, curses.color_pair(4))
        return

    lines = [
        f"Download  : {result.download_mbps:.2f} Mbps",
        f"Upload    : {result.upload_mbps:.2f} Mbps",
        f"Ping      : {result.ping_ms:.2f} ms",
        f"Server    : {result.server_name} ({result.sponsor})",
        f"Distance  : {result.distance_km:.1f} km",
        f"ISP       : {result.isp}",
        f"Public IP : {result.external_ip}",
    ]
    for index, line in enumerate(lines, start=2):
        if index >= height - 1:
            break
        draw_text(screen, top + index, left + 2, fit_text(line, width - 4), width - 4)


def draw_assessment_panel(screen: curses.window, state: AppState, top: int, left: int, height: int, width: int) -> None:
    draw_box(screen, top, left, height, width, "Assessment")
    if height < 5:
        return
    if not state.assessment:
        draw_text(screen, top + 2, left + 2, fit_text("No connection classification yet.", width - 4), width - 4, curses.color_pair(4))
        return

    assessment = state.assessment
    draw_text(screen, top + 2, left + 2, fit_text(assessment.label, width - 4), width - 4, assessment_color(assessment.score) | curses.A_BOLD)
    draw_text(screen, top + 4, left + 2, fit_text(assessment.headline, width - 4), width - 4)
    summary_lines = [assessment.summary]
    for index, line in enumerate(summary_lines, start=6):
        if index >= height - 1:
            break
        draw_text(screen, top + index, left + 2, fit_text(line, width - 4), width - 4, curses.color_pair(2))


def draw_footer(screen: curses.window, width: int, height: int) -> None:
    footer = "r run test | q quit"
    draw_text(screen, height - 2, 2, fit_text(footer, width - 4), width - 4, curses.color_pair(2))


def start_speedtest(state: AppState, events: queue.SimpleQueue[tuple[str, object]]) -> None:
    if state.running:
        return

    state.running = True
    state.started_at = time.time()
    state.error_message = None
    state.status_message = "Telemetry acquisition started."

    def worker() -> None:
        try:
            result = run_speedtest()
        except Exception as error:
            events.put(("error", str(error)))
            return
        events.put(("result", result))

    threading.Thread(target=worker, daemon=True).start()


def handle_event(state: AppState, event: tuple[str, object]) -> None:
    kind, payload = event
    state.running = False
    if kind == "error":
        state.error_message = str(payload)
        state.status_message = "Speed test failed."
        return

    result = payload
    if not isinstance(result, SpeedResult):
        state.error_message = "Unexpected speedtest payload received"
        state.status_message = "Speed test failed."
        return

    state.result = result
    state.assessment = classify_result(result)
    state.completed_at = time.time()
    state.error_message = None
    state.status_message = f"Telemetry complete: {state.assessment.label}."


def run_tui(screen: curses.window) -> None:
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.timeout(200)
    screen.keypad(True)
    init_colors()

    state = AppState()
    events: queue.SimpleQueue[tuple[str, object]] = queue.SimpleQueue()

    start_speedtest(state, events)

    while True:
        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                break
            handle_event(state, event)

        height, width = screen.getmaxyx()
        screen.erase()
        if height < 18 or width < 76:
            draw_text(screen, 1, 2, "Terminal too small for Orbital Speedtest.", width - 4, curses.color_pair(5) | curses.A_BOLD)
            draw_text(screen, 3, 2, "Resize to at least 76x18 or press q to exit.", width - 4, curses.color_pair(4))
            screen.refresh()
        else:
            top = draw_header(screen, width, state)
            left_width = max(34, width // 3)
            right_width = width - left_width - 5
            telemetry_height = max(9, height - top - 4)
            assessment_height = max(7, telemetry_height // 2)
            result_height = telemetry_height - assessment_height - 1

            draw_live_panel(screen, state, top, 2, telemetry_height, left_width)
            draw_result_panel(screen, state, top, left_width + 4, result_height, right_width)
            draw_assessment_panel(screen, state, top + result_height + 1, left_width + 4, assessment_height, right_width)
            draw_footer(screen, width, height)
            screen.refresh()

        try:
            key = screen.get_wch()
        except curses.error:
            continue

        if key in ("q", "Q"):
            return
        if key in ("r", "R"):
            start_speedtest(state, events)


def run_once() -> int:
    try:
        result = run_speedtest()
        assessment = classify_result(result)
    except SpeedtestError as error:
        print(f"orbital-speedtest: {error}")
        return 1

    print(plain_summary(result, assessment))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NASA-themed internet speed test terminal UI")
    parser.add_argument("--once", action="store_true", help="run a single test and print plain-text results")
    parser.add_argument("--version", action="version", version=f"orbital-speedtest {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.once:
        return run_once()
    curses.wrapper(run_tui)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
