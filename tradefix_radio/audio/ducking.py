"""Voice-activated audio ducking.

Monitors microphone input and reduces music volume when voice is detected,
providing a "talkover" effect for live commentary or voice chat.

The ducking level and timing are configurable:
- `duck_level`: How much to reduce volume (0.0 = silent, 1.0 = no change)
- `attack_ms`: How quickly to duck when voice starts
- `release_ms`: How quickly to restore volume when voice stops
- `threshold`: Voice detection sensitivity (0.0-1.0)
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import structlog

_log = structlog.get_logger(__name__)

__all__ = ["DuckingController", "DuckingConfig"]


@dataclass
class DuckingConfig:
    """Configuration for voice ducking."""

    enabled: bool = False
    #: Volume level when ducking (0.0 = silent, 1.0 = full volume)
    duck_level: float = 0.2
    #: Time to reach duck level when voice starts (milliseconds)
    attack_ms: float = 50.0
    #: Time to restore full volume when voice stops (milliseconds)
    release_ms: float = 300.0
    #: Voice detection threshold (0.0-1.0, higher = less sensitive)
    threshold: float = 0.02
    #: Minimum voice duration to trigger ducking (milliseconds)
    hold_ms: float = 100.0
    #: Input device name (None = default microphone)
    input_device: str | None = None


class DuckingController:
    """Controls voice-activated volume ducking.

    Monitors microphone input in a background thread and provides a volume
    multiplier that smoothly transitions between 1.0 (full volume) and
    the configured duck level when voice is detected.
    """

    def __init__(self, config: DuckingConfig | None = None) -> None:
        self._config = config or DuckingConfig()
        self._current_gain = 1.0
        self._target_gain = 1.0
        self._voice_active = False
        self._last_voice_time = 0.0
        self._running = False
        self._thread: threading.Thread | None = None
        self._stream: Any = None
        self._lock = threading.Lock()
        self._on_change: Callable[[float], None] | None = None

    @property
    def config(self) -> DuckingConfig:
        return self._config

    @property
    def enabled(self) -> bool:
        return self._config.enabled and self._running

    @property
    def gain(self) -> float:
        """Current volume multiplier (0.0-1.0)."""
        return self._current_gain

    @property
    def is_voice_active(self) -> bool:
        """Whether voice is currently detected."""
        return self._voice_active

    def set_on_change(self, callback: Callable[[float], None] | None) -> None:
        """Set callback for gain changes (for UI updates)."""
        self._on_change = callback

    def update_config(self, **kwargs: Any) -> None:
        """Update ducking configuration."""
        for key, value in kwargs.items():
            if hasattr(self._config, key):
                setattr(self._config, key, value)

        if "enabled" in kwargs:
            if kwargs["enabled"] and not self._running:
                self.start()
            elif not kwargs["enabled"] and self._running:
                self.stop()

    def start(self) -> bool:
        """Start monitoring microphone input."""
        if self._running:
            return True

        if not self._config.enabled:
            return False

        try:
            import sounddevice as sd  # noqa: PLC0415
        except ImportError:
            _log.warning(
                "ducking.sounddevice_missing",
                detail="sounddevice package required for voice ducking",
            )
            return False

        try:
            self._running = True
            self._thread = threading.Thread(
                target=self._monitor_loop, daemon=True, name="voice-ducking"
            )
            self._thread.start()
            _log.info(
                "ducking.started",
                threshold=self._config.threshold,
                duck_level=self._config.duck_level,
            )
            return True
        except Exception as e:
            self._running = False
            _log.error("ducking.start_failed", error=str(e))
            return False

    def stop(self) -> None:
        """Stop monitoring microphone input."""
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._current_gain = 1.0
        self._target_gain = 1.0
        self._voice_active = False
        _log.info("ducking.stopped")

    def apply_gain(self, audio: np.ndarray) -> np.ndarray:
        """Apply current ducking gain to audio buffer."""
        if not self._config.enabled or self._current_gain >= 0.999:
            return audio
        return (audio * self._current_gain).astype(audio.dtype)

    def _monitor_loop(self) -> None:
        """Background thread that monitors microphone and updates gain."""
        import sounddevice as sd  # noqa: PLC0415

        sample_rate = 16000
        block_size = int(sample_rate * 0.05)  # 50ms blocks

        def audio_callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
            if status:
                return

            # Calculate RMS energy
            rms = np.sqrt(np.mean(indata**2))

            with self._lock:
                now = time.monotonic()

                # Voice detection with hysteresis
                if rms > self._config.threshold:
                    self._last_voice_time = now
                    if not self._voice_active:
                        self._voice_active = True
                        self._target_gain = self._config.duck_level
                elif self._voice_active:
                    # Check hold time
                    hold_seconds = self._config.hold_ms / 1000.0
                    if now - self._last_voice_time > hold_seconds:
                        self._voice_active = False
                        self._target_gain = 1.0

        try:
            # Find input device
            device = None
            if self._config.input_device:
                devices = sd.query_devices()
                for i, d in enumerate(devices):
                    if (
                        self._config.input_device.lower() in d["name"].lower()
                        and d["max_input_channels"] > 0
                    ):
                        device = i
                        break

            self._stream = sd.InputStream(
                device=device,
                channels=1,
                samplerate=sample_rate,
                blocksize=block_size,
                callback=audio_callback,
            )
            self._stream.start()
            _log.info(
                "ducking.mic_opened",
                device=self._stream.device,
                sample_rate=sample_rate,
            )

            # Gain smoothing loop
            while self._running:
                self._update_gain()
                time.sleep(0.01)  # 10ms update rate

        except Exception as e:
            _log.error("ducking.monitor_error", error=str(e))
        finally:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None

    def _update_gain(self) -> None:
        """Smoothly interpolate gain toward target."""
        with self._lock:
            if abs(self._current_gain - self._target_gain) < 0.001:
                if self._current_gain != self._target_gain:
                    self._current_gain = self._target_gain
                    if self._on_change:
                        self._on_change(self._current_gain)
                return

            # Calculate interpolation rate based on attack/release
            if self._target_gain < self._current_gain:
                # Attacking (ducking down)
                rate = 10.0 / max(1.0, self._config.attack_ms)
            else:
                # Releasing (volume up)
                rate = 10.0 / max(1.0, self._config.release_ms)

            # Exponential smoothing
            self._current_gain += (self._target_gain - self._current_gain) * rate
            self._current_gain = max(0.0, min(1.0, self._current_gain))

            if self._on_change:
                self._on_change(self._current_gain)
