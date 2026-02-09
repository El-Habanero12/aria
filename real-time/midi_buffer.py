"""Thread-safe rolling buffer of timestamped MIDI messages."""

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class TimestampedMidiMsg:
    """A MIDI message with its reception timestamp."""
    msg_type: str  # 'note_on', 'note_off', 'control_change'
    note: Optional[int] = None  # 0-127
    velocity: Optional[int] = None  # 0-127, or None for note_off
    control: Optional[int] = None  # CC number (e.g., 64 for sustain)
    value: Optional[int] = None  # CC value (0-127)
    timestamp: float = 0.0  # time.monotonic() when received
    pulse: Optional[int] = None  # MIDI clock pulse index (24ppqn)


class RollingMidiBuffer:
    """
    Thread-safe rolling buffer maintaining MIDI messages from the last N seconds.
    Automatically discards old messages.
    """

    def __init__(self, window_seconds: float = 4.0):
        """
        Args:
            window_seconds: Keep messages from the last N seconds.
        """
        self.window_seconds = window_seconds
        self.buffer = deque()  # List of TimestampedMidiMsg
        self.lock = threading.RLock()
        self.start_time = time.monotonic()  # Reference for relative timestamps

    def add_message(self, msg_type: str, **kwargs) -> None:
        """
        Add a MIDI message to the buffer.

        Args:
            msg_type: 'note_on', 'note_off', or 'control_change'
            **kwargs: Other attributes (note, velocity, control, value, etc.)
        """
        timestamp = time.monotonic()
        msg = TimestampedMidiMsg(msg_type=msg_type, timestamp=timestamp, **kwargs)

        with self.lock:
            self.buffer.append(msg)
            self._trim_old_messages()

    def get_messages(self) -> List[TimestampedMidiMsg]:
        """
        Return a copy of all messages currently in the buffer.
        """
        with self.lock:
            self._trim_old_messages()
            return list(self.buffer)

    def clear(self) -> None:
        """Clear the buffer."""
        with self.lock:
            self.buffer.clear()

    def get_duration_seconds(self) -> float:
        """Get the time span of messages currently in buffer."""
        with self.lock:
            if not self.buffer:
                return 0.0
            span = self.buffer[-1].timestamp - self.buffer[0].timestamp
            return span

    def _trim_old_messages(self) -> None:
        """Remove messages older than window_seconds. Called with lock held."""
        now = time.monotonic()
        cutoff = now - self.window_seconds

        while self.buffer and self.buffer[0].timestamp < cutoff:
            self.buffer.popleft()
