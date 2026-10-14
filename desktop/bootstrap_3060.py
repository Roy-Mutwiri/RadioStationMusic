# ruff: noqa: E501, S310
"""RTX 3060-specific installer for Trade Fix Radio.

Extends the base installer with optimized settings for RTX 3060 GPUs (12GB)
running alongside other GPU-intensive applications.
"""
from __future__ import annotations

from bootstrap import Installer, Layout, InstallError, Progress, Bytes, LM_MODEL
from pathlib import Path


class Installer3060(Installer):
    """Installer with RTX 3060-optimized configuration."""

    def _step_config(self) -> None:
        """Write RTX 3060-optimized config with lower VRAM threshold and auto-start."""
        device = self._default_output_device()
        real_music = (self.l.state / "ace-models.done").is_file()

        lines = [
            "# Trade Fix Radio - RTX 3060 Optimized Configuration",
            "# Built for RTX 3060 (12GB) with other apps running (TikTok, NoxVM, etc.)",
            "# Delete state/config.done to regenerate this file.",
            "TRADEFIX_MODE=development",
            "",
            "# Real market prices - no API key needed",
            "TRADEFIX_MARKET__FEED=public",
            "",
            "# Sound output",
            "TRADEFIX_AUDIO__SINK=sounddevice",
        ]

        if device is not None:
            name, host_api = device
            lines += [
                f"# Detected at install time: {name} ({host_api})",
                "TRADEFIX_AUDIO__DEVICE_NAME=default",
                f"TRADEFIX_AUDIO__DEVICE_HOST_API={host_api}",
            ]
            self.log(f"    sound: following the Windows default output (now {name})")
        else:
            lines += ["TRADEFIX_AUDIO__SINK=null_sink"]
            self.log("    no output device found; the station will run silently")

        lines += [""]

        if real_music:
            lines += [
                "# ACE-Step AI Music Generator - RTX 3060 optimized",
                "TRADEFIX_GENERATION__PROVIDER=ace_step",
                "TRADEFIX_GENERATION__ACE_STEP__BASE_URL=http://127.0.0.1:8001",
                "TRADEFIX_GENERATION__ACE_STEP__WORKER_PROCESS=true",
                f"TRADEFIX_GENERATION__ACE_STEP__WORKER_DIRECTORY={self.l.ace_step.as_posix()}",
                f"TRADEFIX_GENERATION__ACE_STEP__LM_MODEL={LM_MODEL}",
                "",
                "# RTX 3060 OPTIMIZATION: Lower VRAM threshold",
                "# Default is 1800MB but RTX 3060 with other apps has ~600-800MB free",
                "# Measured generation need: ~1100MB, so 600MB threshold works",
                "TRADEFIX_GENERATION__ACE_STEP__MIN_FREE_VRAM_MB=600",
                "",
                "# Auto-start: Skip priming wait, go on air immediately",
                "TRADEFIX_RADIO__PRIME_ENABLED=false",
                "TRADEFIX_RADIO__MINIMUM_FRESH_TRACKS=1",
                "TRADEFIX_RADIO__TARGET_FRESH_MINUTES=1",
            ]
        else:
            lines += [
                "# No NVIDIA GPU with enough memory was found, so the station uses mock",
                "# generator (synthetic placeholder audio). Install ACE-Step to change this.",
                "TRADEFIX_GENERATION__PROVIDER=mock",
            ]

        (self.l.app / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.log("    RTX 3060-optimized config written")


# Re-export everything from bootstrap
from bootstrap import default_install_dir, InstallError, Layout

__all__ = ["Installer3060", "InstallError", "Layout", "default_install_dir"]
