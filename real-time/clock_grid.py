"""Pulse-based clock grid for Ableton MIDI clock (24 ppqn).

ClockGrid listens to a specified MIDI clock input port and maintains a pulse
counter. It detects block boundaries of N measures and invokes registered
callbacks when a boundary is reached.

Usage:
    grid = ClockGrid(clock_port_name="ARIA_CLOCK", measures=2, beats_per_bar=4)
    grid.register_boundary_callback(cb)
    grid.start()
    ...
    grid.stop()
"""

import logging
import threading
import time
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

PPQN = 24  # MIDI clock pulses per quarter note


class ClockGrid:
    def __init__(self, clock_port_name: str = "ARIA_CLOCK", measures: int = 2, beats_per_bar: int = 4):
        self.clock_port_name = clock_port_name
        self.measures = measures
        self.beats_per_bar = beats_per_bar

        self.pulses_per_bar = self.beats_per_bar * PPQN
        self.pulses_per_block = max(1, int(self.measures) * self.pulses_per_bar)

        self.lock = threading.RLock()
        self.clock_port = None
        self.running = False
        self.is_running = False  # MIDI transport running state (start/stop)
        self.pulse_count = 0
        self.last_clock_time = None

        self.thread: Optional[threading.Thread] = None
        self.boundary_callbacks: List[Callable[[int], None]] = []
        self.last_pulse_log_time = time.monotonic()

    def register_boundary_callback(self, cb: Callable[[int], None]):
        with self.lock:
            self.boundary_callbacks.append(cb)

    def start(self):
        try:
            import mido
        except Exception:
            raise ImportError("mido is required. Install with: pip install mido")

        try:
            port_name = self._resolve_port_name()
            self.clock_port = mido.open_input(port_name)
            logger.info(f"ClockGrid: opened clock port {port_name}")
        except Exception as e:
            logger.error(f"ClockGrid: failed to open clock port '{self.clock_port_name}': {e}")
            raise

        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=1.0)
        if self.clock_port:
            try:
                self.clock_port.close()
            except Exception:
                pass
            logger.info("ClockGrid: clock port closed")

    def _resolve_port_name(self) -> str:
        import mido
        avail = mido.get_input_names()
        if self.clock_port_name in avail:
            return self.clock_port_name
        matched = [p for p in avail if p.startswith(self.clock_port_name)]
        if matched:
            return matched[0]
        return self.clock_port_name

    def _loop(self):
        logger.info("ClockGrid: listening for MIDI clock messages")
        try:
            while self.running:
                for msg in self.clock_port.iter_pending():
                    self._handle_msg(msg)
                time.sleep(0.001)
        except Exception as e:
            logger.exception(f"ClockGrid loop error: {e}")

    def _handle_msg(self, msg):
        # mido expresses clock messages with .type of 'start', 'stop', 'continue', 'clock'
        if msg.type == 'start':
            with self.lock:
                self.is_running = True
                self.pulse_count = 0
                self.last_clock_time = None
                logger.info("ClockGrid: MIDI START")
        elif msg.type == 'continue':
            with self.lock:
                self.is_running = True
                logger.debug("ClockGrid: MIDI CONTINUE")
        elif msg.type == 'stop':
            with self.lock:
                self.is_running = False
                logger.info("ClockGrid: MIDI STOP")
        elif msg.type == 'clock':
            self._handle_pulse()

    def _handle_pulse(self):
        now = time.monotonic()
        callbacks = []  # Always initialize to prevent UnboundLocalError
        boundary_pulse = None

        with self.lock:
            self.pulse_count += 1
            self.last_clock_time = now

            # Log pulse count once per second
            if now - self.last_pulse_log_time >= 1.0:
                logger.info(f"ClockGrid pulse update: count={self.pulse_count}, running={self.is_running}")
                self.last_pulse_log_time = now

            # Detect block boundary when pulse_count is a multiple of pulses_per_block
            if self.is_running and self.pulse_count % self.pulses_per_block == 0:
                boundary_pulse = self.pulse_count
                logger.info(f"ClockGrid: block boundary pulse={boundary_pulse} (measures={self.measures})")
                # Call callbacks without holding lock to avoid deadlocks
                callbacks = list(self.boundary_callbacks)

        # invoke callbacks outside lock
        for cb in callbacks:
            try:
                cb(boundary_pulse)
            except Exception:
                logger.exception("ClockGrid: boundary callback error")

    def get_pulse_count(self) -> int:
        with self.lock:
            return int(self.pulse_count)

    def get_is_running(self) -> bool:
        with self.lock:
            return bool(self.is_running)

    def get_pulses_per_block(self) -> int:
        return int(self.pulses_per_block)

    def get_pulses_per_bar(self) -> int:
        return int(self.pulses_per_bar)