"""System audio-activated ducking.

Monitors system audio output (what you hear from speakers) and reduces music
volume when other audio is detected, providing automatic ducking when other
apps play sound.

The ducking level and timing are configurable:
- `duck_level`: How much to reduce volume (0.0 = silent, 1.0 = no change)
- `attack_ms`: How quickly to duck when audio starts
- `release_ms`: How quickly to restore volume when audio stops
- `threshold`: Audio detection sensitivity (0.0-1.0)
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
    """Configuration for system audio ducking."""

    enabled: bool = False
    #: Volume level when ducking (0.0 = silent, 1.0 = full volume)
    duck_level: float = 0.2
    #: Time to reach duck level when audio starts (milliseconds)
    attack_ms: float = 50.0
    #: Time to restore full volume when audio stops (milliseconds)
    release_ms: float = 300.0
    #: Audio detection threshold (0.0-1.0, higher = less sensitive)
    threshold: float = 0.01
    #: Minimum audio duration to trigger ducking (milliseconds)
    hold_ms: float = 100.0
    #: Loopback device name (None = auto-detect WASAPI loopback)
    loopback_device: str | None = None


class DuckingController:
    """Controls system audio-activated volume ducking.

    Monitors system audio output (loopback) in a background thread and provides
    a volume multiplier that smoothly transitions between 1.0 (full volume) and
    the configured duck level when other audio is detected.
    """

    def __init__(self, config: DuckingConfig | None = None) -> None:
        self._config = config or DuckingConfig()
        self._current_gain = 1.0
        self._target_gain = 1.0
        self._audio_active = False
        self._last_audio_time = 0.0
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
        """Whether external audio is currently detected."""
        return self._audio_active

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
        """Start monitoring system audio output."""
        if self._running:
            return True

        if not self._config.enabled:
            return False

        try:
            import sounddevice as sd  # noqa: PLC0415
        except ImportError:
            _log.warning(
                "ducking.sounddevice_missing",
                detail="sounddevice package required for audio ducking",
            )
            return False

        try:
            self._running = True
            self._thread = threading.Thread(
                target=self._monitor_loop, daemon=True, name="audio-ducking"
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
        """Stop monitoring system audio."""
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
        self._audio_active = False
        _log.info("ducking.stopped")

    def _find_loopback_device(self, sd: Any) -> int | None:
        """Find a WASAPI loopback device for capturing system audio."""
        devices = sd.query_devices()

        # First, look for explicit loopback devices
        for i, d in enumerate(devices):
            name = d["name"].lower()
            # Check for WASAPI loopback or virtual audio cables
            if d["max_input_channels"] > 0:
                if "loopback" in name:
                    _log.info("ducking.found_loopback", device=d["name"], index=i)
                    return i
                if "stereo mix" in name:
                    _log.info("ducking.found_stereo_mix", device=d["name"], index=i)
                    return i
                if "what u hear" in name:
                    _log.info("ducking.found_what_u_hear", device=d["name"], index=i)
                    return i
                if "cable output" in name or "vb-audio" in name:
                    _log.info("ducking.found_virtual_cable", device=d["name"], index=i)
                    return i

        # If user specified a device, try to find it
        if self._config.loopback_device:
            target = self._config.loopback_device.lower()
            for i, d in enumerate(devices):
                if target in d["name"].lower() and d["max_input_channels"] > 0:
                    _log.info("ducking.found_configured_device", device=d["name"], index=i)
                    return i

        _log.warning(
            "ducking.no_loopback_found",
            detail="No loopback device found. Enable 'Stereo Mix' in Windows Sound settings, "
                   "or install a virtual audio cable like VB-Cable.",
        )
        return None

    def _monitor_loop(self) -> None:
        """Background thread that monitors system audio and updates gain."""
        import sounddevice as sd  # noqa: PLC0415

        sample_rate = 44100
        block_size = int(sample_rate * 0.05)  # 50ms blocks

        def audio_callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
            if status:
                return

            # Calculate RMS energy
            rms = np.sqrt(np.mean(indata**2))

            with self._lock:
                now = time.monotonic()

                # Audio detection with hysteresis
                if rms > self._config.threshold:
                    self._last_audio_time = now
                    if not self._audio_active:
                        self._audio_active = True
                        self._target_gain = self._config.duck_level
                        _log.debug("ducking.audio_detected", rms=round(rms, 4))
                elif self._audio_active:
                    # Check hold time
                    hold_seconds = self._config.hold_ms / 1000.0
                    if now - self._last_audio_time > hold_seconds:
                        self._audio_active = False
                        self._target_gain = 1.0
                        _log.debug("ducking.audio_ended")

        try:
            # Find loopback device
            device = self._find_loopback_device(sd)

            if device is None:
                _log.error(
                    "ducking.no_device",
                    detail="Cannot start ducking without a loopback device",
                )
                self._running = False
                return

            device_info = sd.query_devices(device)
            channels = min(2, device_info["max_input_channels"])

            self._stream = sd.InputStream(
                device=device,
                channels=channels,
                samplerate=sample_rate,
                blocksize=block_size,
                callback=audio_callback,
            )
            self._stream.start()
            _log.info(
                "ducking.loopback_opened",
                device=device_info["name"],
                channels=channels,
                sample_rate=sample_rate,
            )

            # Gain smoothing loop
            while self._running:
                self._update_gain()
                time.sleep(0.01)  # 10ms update rate

        except Exception as e:
            _log.error("ducking.monitor_error", error=str(e), exc_info=True)
        finally:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            self._running = False

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
