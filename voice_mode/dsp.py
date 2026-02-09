"""Audio DSP processing chain for voicemode TTS output.

Signal chain: Input → EQ → LA-2A Leveler → Compressor → Limiter → Output

This module provides broadcast-style processing to improve TTS audio:
- EQ: Shape frequency response (cut mud, add presence)
- LA-2A: Smooth optical-style leveling (program-dependent compression)
- Compressor: Dynamic range control
- Limiter: Prevent clipping (always last)
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

try:
    from scipy.signal import butter, sosfilt
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False

logger = logging.getLogger("voicemode.dsp")

# Set up dedicated DSP log file so levels are visible even when running as MCP
_dsp_log_file = Path.home() / ".voicemode" / "logs" / "dsp.log"
_dsp_log_file.parent.mkdir(parents=True, exist_ok=True)
_dsp_handler = logging.FileHandler(_dsp_log_file, mode='a')
_dsp_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
logger.addHandler(_dsp_handler)
logger.setLevel(logging.INFO)


@dataclass
class DSPConfig:
    """Configuration for DSP processing chain."""
    enabled: bool = True
    sample_rate: int = 24000

    # Pre-gain (before processing)
    pre_gain_db: float = 0.0

    # EQ settings (simple 3-band)
    eq_low_gain_db: float = 0.0      # Below 200Hz
    eq_mid_gain_db: float = 0.0      # 200Hz - 4kHz
    eq_high_gain_db: float = 0.0     # Above 4kHz
    eq_low_freq: float = 200.0
    eq_high_freq: float = 4000.0

    # LA-2A style leveling amplifier (optical compressor)
    # Simple controls like the original: just gain and peak reduction
    leveler_enabled: bool = True
    leveler_gain_db: float = 6.0     # Output gain
    leveler_peak_reduction: float = 3.0  # Amount of gain reduction in dB

    # Compressor settings
    compressor_enabled: bool = True
    compressor_threshold_db: float = -18.0
    compressor_ratio: float = 4.0
    compressor_attack_ms: float = 10.0
    compressor_release_ms: float = 100.0
    compressor_makeup_db: float = 0.0

    # Limiter settings (always last)
    limiter_enabled: bool = True
    limiter_ceiling_db: float = -1.0
    limiter_release_ms: float = 50.0

    # Final output gain (attenuation only, after limiter)
    # Range: -inf to 0 dB (unity). Lets you turn down if output is too hot.
    output_gain_db: float = 0.0


def db_to_linear(db: float) -> float:
    """Convert decibels to linear gain."""
    return 10.0 ** (db / 20.0)


def linear_to_db(linear: float) -> float:
    """Convert linear gain to decibels."""
    if linear <= 0:
        return -100.0
    return 20.0 * np.log10(linear)


class SimpleEQ:
    """Simple 3-band EQ using butterworth filters."""

    def __init__(self, sample_rate: int, low_freq: float = 200.0, high_freq: float = 4000.0):
        self.sample_rate = sample_rate
        self.low_freq = low_freq
        self.high_freq = high_freq
        self._build_filters()

    def _build_filters(self):
        """Build the filter coefficients."""
        if not SCIPY_AVAILABLE:
            self.sos_low = None
            self.sos_mid_low = None
            self.sos_mid_high = None
            self.sos_high = None
            return

        nyquist = self.sample_rate / 2.0

        # Low band: lowpass at low_freq
        self.sos_low = butter(2, self.low_freq / nyquist, btype='low', output='sos')

        # Mid band: bandpass between low_freq and high_freq
        self.sos_mid_low = butter(2, self.low_freq / nyquist, btype='high', output='sos')
        self.sos_mid_high = butter(2, self.high_freq / nyquist, btype='low', output='sos')

        # High band: highpass at high_freq
        self.sos_high = butter(2, self.high_freq / nyquist, btype='high', output='sos')

    def process(self, samples: np.ndarray, low_gain_db: float, mid_gain_db: float, high_gain_db: float) -> np.ndarray:
        """Process audio through 3-band EQ."""
        if not SCIPY_AVAILABLE:
            logger.warning("scipy not available, skipping EQ")
            return samples

        low_gain = db_to_linear(low_gain_db)
        mid_gain = db_to_linear(mid_gain_db)
        high_gain = db_to_linear(high_gain_db)

        # Split into bands
        low = sosfilt(self.sos_low, samples) * low_gain
        mid = sosfilt(self.sos_mid_high, sosfilt(self.sos_mid_low, samples)) * mid_gain
        high = sosfilt(self.sos_high, samples) * high_gain

        return low + mid + high


class LA2ALeveler:
    """LA-2A style optical leveling amplifier.

    The LA-2A is known for its smooth, program-dependent compression.
    It uses an optical cell (T4B) which gives it a unique attack/release
    characteristic that varies with the input signal.

    Key characteristics:
    - Slow attack (10-100ms depending on signal)
    - Program-dependent release (40ms to several seconds)
    - Soft knee compression
    - Very musical, transparent sound
    """

    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        self.gain_reduction = 1.0  # Current gain reduction (linear)
        self.optical_cell = 0.0    # Simulated optical cell state

        # Optical cell time constants (program-dependent)
        self.attack_coef = np.exp(-1.0 / (0.010 * sample_rate))   # ~10ms attack
        self.release_coef = np.exp(-1.0 / (0.060 * sample_rate))  # ~60ms base release

    def process(self, samples: np.ndarray, peak_reduction_db: float, output_gain_db: float) -> np.ndarray:
        """Process audio through LA-2A style leveler."""
        if peak_reduction_db <= 0:
            return samples * db_to_linear(output_gain_db)

        output = np.zeros_like(samples)
        output_gain = db_to_linear(output_gain_db)

        # Target gain reduction
        target_reduction = db_to_linear(-peak_reduction_db)

        for i in range(len(samples)):
            sample = samples[i]
            sample_abs = abs(sample)

            # Optical cell simulation - responds to signal level
            # The cell "charges" with signal and "discharges" slowly
            if sample_abs > self.optical_cell:
                # Attack - cell responds to peaks
                self.optical_cell = sample_abs + self.attack_coef * (self.optical_cell - sample_abs)
            else:
                # Release - program-dependent, slower for sustained signals
                # This gives the LA-2A its characteristic smooth release
                release_time = 0.040 + 0.5 * self.optical_cell  # 40ms to 540ms
                release_coef = np.exp(-1.0 / (release_time * self.sample_rate))
                self.optical_cell = sample_abs + release_coef * (self.optical_cell - sample_abs)

            # Calculate gain reduction based on optical cell
            # Soft knee - compression gradually increases
            if self.optical_cell > 0.1:
                # Amount of reduction scales with cell value
                reduction_amount = min(1.0, self.optical_cell)
                self.gain_reduction = 1.0 - reduction_amount * (1.0 - target_reduction)
            else:
                # Below threshold, minimal reduction
                self.gain_reduction = 1.0 + 0.9 * (self.gain_reduction - 1.0)

            output[i] = sample * self.gain_reduction * output_gain

        return output


class Compressor:
    """Standard compressor with attack/release envelope."""

    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        self.envelope = 0.0
        self.gain_reduction = 1.0

    def process(self, samples: np.ndarray, threshold_db: float, ratio: float,
                attack_ms: float, release_ms: float, makeup_db: float) -> np.ndarray:
        """Process audio through compressor."""
        threshold = db_to_linear(threshold_db)
        makeup = db_to_linear(makeup_db)

        attack_coef = np.exp(-1.0 / (attack_ms / 1000.0 * self.sample_rate))
        release_coef = np.exp(-1.0 / (release_ms / 1000.0 * self.sample_rate))

        output = np.zeros_like(samples)

        for i in range(len(samples)):
            sample = samples[i]
            sample_abs = abs(sample)

            # Envelope follower
            if sample_abs > self.envelope:
                self.envelope = sample_abs + attack_coef * (self.envelope - sample_abs)
            else:
                self.envelope = sample_abs + release_coef * (self.envelope - sample_abs)

            # Calculate gain reduction
            if self.envelope > threshold:
                # Over threshold - apply compression
                over_db = linear_to_db(self.envelope / threshold)
                reduction_db = over_db * (1.0 - 1.0 / ratio)
                self.gain_reduction = db_to_linear(-reduction_db)
            else:
                # Under threshold - no compression
                self.gain_reduction = 1.0

            output[i] = sample * self.gain_reduction * makeup

        return output


class Limiter:
    """Lookahead limiter to prevent clipping."""

    def __init__(self, sample_rate: int, lookahead_ms: float = 5.0):
        self.sample_rate = sample_rate
        self.lookahead_samples = int(lookahead_ms / 1000.0 * sample_rate)
        self.gain = 1.0
        self.gain_target = 1.0

    def process(self, samples: np.ndarray, ceiling_db: float, release_ms: float) -> np.ndarray:
        """Process audio through limiter."""
        ceiling = db_to_linear(ceiling_db)
        release_coef = np.exp(-1.0 / (release_ms / 1000.0 * self.sample_rate))

        # Lookahead buffer
        buffer_size = len(samples) + self.lookahead_samples
        buffer = np.zeros(buffer_size)
        buffer[self.lookahead_samples:] = samples

        output = np.zeros_like(samples)

        for i in range(len(samples)):
            # Look ahead for peaks
            lookahead_end = i + self.lookahead_samples
            lookahead_window = buffer[i:lookahead_end + 1]
            peak = np.max(np.abs(lookahead_window))

            # Calculate required gain reduction
            if peak * self.gain > ceiling:
                self.gain_target = ceiling / peak

            # Smooth gain changes
            if self.gain_target < self.gain:
                # Attack - fast
                self.gain = self.gain_target
            else:
                # Release - slow
                self.gain = self.gain_target + release_coef * (self.gain - self.gain_target)

            output[i] = samples[i] * self.gain

        return output


class DSPChain:
    """Complete DSP processing chain."""

    def __init__(self, config: Optional[DSPConfig] = None):
        self.config = config or DSPConfig()
        self._init_processors()

    def _init_processors(self):
        """Initialize all processors."""
        sr = self.config.sample_rate

        self.eq = SimpleEQ(sr, self.config.eq_low_freq, self.config.eq_high_freq)
        self.leveler = LA2ALeveler(sr)
        self.compressor = Compressor(sr)
        self.limiter = Limiter(sr)

    def update_config(self, config: DSPConfig):
        """Update configuration and reinitialize if sample rate changed."""
        if config.sample_rate != self.config.sample_rate:
            self.config = config
            self._init_processors()
        else:
            self.config = config

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Process audio through the complete DSP chain.

        Chain: Pre-gain → EQ → LA-2A → Compressor → Limiter
        """
        if not self.config.enabled:
            return samples

        # Ensure float32
        if samples.dtype != np.float32:
            samples = samples.astype(np.float32)

        # Calculate input RMS for logging
        input_rms = np.sqrt(np.mean(samples ** 2))
        input_rms_db = linear_to_db(input_rms) if input_rms > 0 else -100.0
        input_peak = np.max(np.abs(samples))
        input_peak_db = linear_to_db(input_peak) if input_peak > 0 else -100.0

        # Pre-gain
        if self.config.pre_gain_db != 0:
            samples = samples * db_to_linear(self.config.pre_gain_db)

        # EQ
        if SCIPY_AVAILABLE and (self.config.eq_low_gain_db != 0 or
                                 self.config.eq_mid_gain_db != 0 or
                                 self.config.eq_high_gain_db != 0):
            samples = self.eq.process(
                samples,
                self.config.eq_low_gain_db,
                self.config.eq_mid_gain_db,
                self.config.eq_high_gain_db
            )

        # LA-2A Leveler
        if self.config.leveler_enabled:
            samples = self.leveler.process(
                samples,
                self.config.leveler_peak_reduction,
                self.config.leveler_gain_db
            )

        # Compressor
        if self.config.compressor_enabled:
            samples = self.compressor.process(
                samples,
                self.config.compressor_threshold_db,
                self.config.compressor_ratio,
                self.config.compressor_attack_ms,
                self.config.compressor_release_ms,
                self.config.compressor_makeup_db
            )

        # Limiter (always last in processing)
        if self.config.limiter_enabled:
            samples = self.limiter.process(
                samples,
                self.config.limiter_ceiling_db,
                self.config.limiter_release_ms
            )

        # Final output gain (attenuation only, after limiter)
        if self.config.output_gain_db < 0:
            samples = samples * db_to_linear(self.config.output_gain_db)

        # Calculate output RMS for logging
        output_rms = np.sqrt(np.mean(samples ** 2))
        output_rms_db = linear_to_db(output_rms) if output_rms > 0 else -100.0
        output_peak = np.max(np.abs(samples))
        output_peak_db = linear_to_db(output_peak) if output_peak > 0 else -100.0

        logger.info(
            f"DSP: input RMS={input_rms_db:.1f}dB peak={input_peak_db:.1f}dB → "
            f"output RMS={output_rms_db:.1f}dB peak={output_peak_db:.1f}dB "
            f"(gain={output_rms_db - input_rms_db:+.1f}dB)"
        )

        return samples.astype(np.float32)


# Default chain instance
_default_chain: Optional[DSPChain] = None


def load_config_from_voicemode() -> DSPConfig:
    """Load DSP config from voicemode config module."""
    try:
        from voice_mode import config as vm_config

        config = DSPConfig(
            enabled=getattr(vm_config, 'DSP_ENABLED', True),
            pre_gain_db=getattr(vm_config, 'DSP_PRE_GAIN_DB', 0.0),
            eq_low_gain_db=getattr(vm_config, 'DSP_EQ_LOW_GAIN_DB', -2.0),
            eq_mid_gain_db=getattr(vm_config, 'DSP_EQ_MID_GAIN_DB', 0.0),
            eq_high_gain_db=getattr(vm_config, 'DSP_EQ_HIGH_GAIN_DB', 1.5),
            leveler_enabled=getattr(vm_config, 'DSP_LEVELER_ENABLED', True),
            leveler_gain_db=getattr(vm_config, 'DSP_LEVELER_GAIN_DB', 6.0),
            leveler_peak_reduction=getattr(vm_config, 'DSP_LEVELER_PEAK_REDUCTION', 4.0),
            compressor_enabled=getattr(vm_config, 'DSP_COMPRESSOR_ENABLED', True),
            compressor_threshold_db=getattr(vm_config, 'DSP_COMPRESSOR_THRESHOLD_DB', -18.0),
            compressor_ratio=getattr(vm_config, 'DSP_COMPRESSOR_RATIO', 3.0),
            compressor_attack_ms=getattr(vm_config, 'DSP_COMPRESSOR_ATTACK_MS', 10.0),
            compressor_release_ms=getattr(vm_config, 'DSP_COMPRESSOR_RELEASE_MS', 100.0),
            compressor_makeup_db=getattr(vm_config, 'DSP_COMPRESSOR_MAKEUP_DB', 0.0),
            limiter_enabled=getattr(vm_config, 'DSP_LIMITER_ENABLED', True),
            limiter_ceiling_db=getattr(vm_config, 'DSP_LIMITER_CEILING_DB', -1.0),
            limiter_release_ms=getattr(vm_config, 'DSP_LIMITER_RELEASE_MS', 50.0),
            output_gain_db=min(0.0, getattr(vm_config, 'DSP_OUTPUT_GAIN_DB', 0.0)),  # Clamp to <= 0
        )

        # Log DSP config on load
        logger.info(
            f"DSP config loaded: enabled={config.enabled}, "
            f"pre_gain={config.pre_gain_db}dB, "
            f"leveler_gain={config.leveler_gain_db}dB (peak_red={config.leveler_peak_reduction}), "
            f"comp_makeup={config.compressor_makeup_db}dB, "
            f"limiter_ceiling={config.limiter_ceiling_db}dB"
        )
        return config
    except ImportError:
        logger.warning("Could not load voicemode config, using defaults")
        return DSPConfig()


def get_default_chain() -> DSPChain:
    """Get or create the default DSP chain with voicemode config."""
    global _default_chain
    if _default_chain is None:
        config = load_config_from_voicemode()
        _default_chain = DSPChain(config)
    return _default_chain


def process_audio(samples: np.ndarray, config: Optional[DSPConfig] = None) -> np.ndarray:
    """Process audio through DSP chain.

    Convenience function that uses the default chain or creates one with the given config.

    Args:
        samples: Audio samples (numpy array)
        config: Optional DSP configuration

    Returns:
        Processed audio samples
    """
    if config is not None:
        chain = DSPChain(config)
        return chain.process(samples)
    else:
        return get_default_chain().process(samples)
