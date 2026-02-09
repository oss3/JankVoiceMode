"""Non-blocking audio player using callback-based playback.

This module provides a queue-based audio playback system that allows multiple
concurrent audio streams without blocking or interference.

Includes optional DSP processing (EQ, compression, limiting) for TTS output.

Supports PTT (push-to-talk) interrupt: if the user triggers PTT during playback,
audio stops immediately so they can start speaking.
"""

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import sounddevice as sd

from voice_mode.dsp import DSPChain, DSPConfig, get_default_chain

logger = logging.getLogger("voicemode.audio_player")

# PTT file paths (same as in config.py, but we avoid circular import)
PTT_START_FILE = Path.home() / ".voicemode" / "push-to-talk-start"
PTT_TOGGLE_FILE = Path.home() / ".voicemode" / "push-to-talk-toggle"


class NonBlockingAudioPlayer:
    """Non-blocking audio player using callback-based playback.

    This player uses a queue-based callback system to play audio without blocking
    the calling thread. It allows multiple instances to play audio concurrently
    by leveraging the system's audio mixing capabilities (Core Audio on macOS,
    PulseAudio/ALSA on Linux).

    Example:
        player = NonBlockingAudioPlayer()
        player.play(audio_samples, sample_rate=24000)
        player.wait()  # Wait for playback to complete
    """

    def __init__(self, buffer_size: int = 2048, dsp_enabled: bool = True):
        """Initialize the audio player.

        Args:
            buffer_size: Size of audio buffer chunks for callback (default: 2048)
            dsp_enabled: Enable DSP processing (EQ, compression, limiting)
        """
        self.buffer_size = buffer_size
        self.audio_queue: Optional[queue.Queue] = None
        self.stream: Optional[sd.OutputStream] = None
        self.playback_complete = threading.Event()
        self.playback_error: Optional[Exception] = None
        self.dsp_enabled = dsp_enabled
        self.dsp_chain: Optional[DSPChain] = None

    def _audio_callback(self, outdata, frames, time_info, status):
        """Callback function called by sounddevice for each audio buffer.

        Args:
            outdata: Output buffer to fill with audio data
            frames: Number of frames requested
            time_info: Timing information
            status: Status flags
        """
        if status:
            logger.warning(f"Audio callback status: {status}")

        try:
            # Get audio chunk from queue
            chunk = self.audio_queue.get_nowait()

            # Handle end-of-stream marker
            if chunk is None:
                outdata[:] = 0
                self.playback_complete.set()
                raise sd.CallbackStop()

            # Fill output buffer
            chunk_len = len(chunk)
            if chunk_len < frames:
                # Partial chunk - pad with zeros
                if chunk.ndim == 1:
                    # Mono audio - reshape for sounddevice
                    outdata[:chunk_len, 0] = chunk
                    outdata[chunk_len:, 0] = 0
                else:
                    # Multi-channel audio
                    outdata[:chunk_len] = chunk
                    outdata[chunk_len:] = 0
                # Mark playback complete after this chunk
                self.playback_complete.set()
                raise sd.CallbackStop()
            else:
                if chunk.ndim == 1:
                    # Mono audio - reshape for sounddevice
                    outdata[:, 0] = chunk[:frames]
                else:
                    # Multi-channel audio
                    outdata[:] = chunk[:frames]

        except queue.Empty:
            # No data available - output silence
            outdata[:] = 0
            logger.debug("Audio queue empty - outputting silence")

    def play(self, samples: np.ndarray, sample_rate: int, blocking: bool = False):
        """Play audio samples using non-blocking callback system.

        Args:
            samples: Audio samples to play (numpy array)
            sample_rate: Sample rate in Hz
            blocking: If True, wait for playback to complete before returning

        Raises:
            Exception: If playback error occurs
        """
        # Reset state
        self.playback_complete.clear()
        self.playback_error = None

        # Ensure samples are float32
        if samples.dtype != np.float32:
            samples = samples.astype(np.float32)

        # Apply DSP processing (EQ, compression, limiting)
        if self.dsp_enabled:
            try:
                if self.dsp_chain is None:
                    self.dsp_chain = get_default_chain()
                    # Update sample rate if needed
                    if self.dsp_chain.config.sample_rate != sample_rate:
                        self.dsp_chain.config.sample_rate = sample_rate
                        self.dsp_chain._init_processors()
                samples = self.dsp_chain.process(samples)
            except Exception as e:
                logger.warning(f"DSP processing failed, using raw audio: {e}")

        # Determine number of channels
        if samples.ndim == 1:
            channels = 1
        else:
            channels = samples.shape[1]

        # Create queue and fill with audio chunks
        self.audio_queue = queue.Queue()

        # Split samples into chunks
        for i in range(0, len(samples), self.buffer_size):
            chunk = samples[i:i + self.buffer_size]
            self.audio_queue.put(chunk)

        # Add end-of-stream marker
        self.audio_queue.put(None)

        # Create and start output stream
        try:
            self.stream = sd.OutputStream(
                samplerate=sample_rate,
                channels=channels,
                callback=self._audio_callback,
                blocksize=self.buffer_size,
                dtype=np.float32
            )
            self.stream.start()

            if blocking:
                self.wait()

        except Exception as e:
            self.playback_error = e
            logger.error(f"Error starting audio playback: {e}")
            raise

    def wait(self, timeout: Optional[float] = None):
        """Wait for playback to complete.

        Args:
            timeout: Maximum time to wait in seconds (None = wait forever)

        Raises:
            Exception: If playback error occurred
        """
        # Wait for playback to complete
        if not self.playback_complete.wait(timeout=timeout):
            logger.warning("Playback wait timed out")

        # Stop and close stream
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

        # Raise any error that occurred during playback
        if self.playback_error:
            raise self.playback_error

    def stop(self):
        """Stop playback immediately."""
        self.playback_complete.set()
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

        # Clear queue
        if self.audio_queue:
            while not self.audio_queue.empty():
                try:
                    self.audio_queue.get_nowait()
                except queue.Empty:
                    break

    def wait_with_ptt_interrupt(
        self,
        timeout: Optional[float] = None,
        poll_interval: float = 0.05
    ) -> Tuple[bool, bool]:
        """Wait for playback to complete, but stop early if PTT is triggered.

        This allows users to interrupt TTS playback by pressing their PTT button,
        which is useful when they've already read the text and want to respond.

        Checks for both:
        - PTT_TOGGLE_FILE (unified toggle, deleted on detection)
        - PTT_START_FILE (legacy start file, NOT deleted - consumed by recording logic)

        When PTT toggle is detected during playback, the toggle file is deleted
        and ptt_interrupted is set to True. The caller can then skip the PTT
        wait loop and start recording immediately.

        Args:
            timeout: Maximum time to wait in seconds (None = wait forever)
            poll_interval: How often to check for PTT signal (default: 50ms)

        Returns:
            Tuple of (playback_completed, ptt_interrupted):
                - playback_completed: True if playback finished naturally
                - ptt_interrupted: True if playback was stopped due to PTT

        Raises:
            Exception: If playback error occurred
        """
        start_time = time.time()
        ptt_interrupted = False

        while True:
            # Check if playback completed naturally
            if self.playback_complete.is_set():
                break

            # Check for timeout
            if timeout is not None:
                elapsed = time.time() - start_time
                if elapsed >= timeout:
                    logger.warning("Playback wait timed out")
                    break

            # Check for PTT interrupt signal (toggle file takes precedence)
            try:
                if PTT_TOGGLE_FILE.exists():
                    logger.info("PTT toggle detected during TTS playback - interrupting audio")
                    # Delete the toggle file to signal it was consumed during TTS
                    # The caller will know to skip PTT wait since ptt_interrupted=True
                    PTT_TOGGLE_FILE.unlink()
                    ptt_interrupted = True
                    self.stop()
                    break
                elif PTT_START_FILE.exists():
                    logger.info("PTT start detected during TTS playback - interrupting audio")
                    # Don't delete the start file - let the recording logic consume it
                    ptt_interrupted = True
                    self.stop()
                    break
            except OSError as e:
                logger.debug(f"Error checking PTT file: {e}")

            # Brief sleep before next check
            time.sleep(poll_interval)

        # Stop and close stream if still active
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

        # Raise any error that occurred during playback
        if self.playback_error:
            raise self.playback_error

        playback_completed = self.playback_complete.is_set() and not ptt_interrupted
        return playback_completed, ptt_interrupted
