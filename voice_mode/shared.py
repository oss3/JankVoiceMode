"""Shared initialization for voicemode."""

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

import httpx
import sounddevice as sd

# Import all configuration from config.py
from .config import (
    DEBUG, DEBUG_DIR, SAVE_AUDIO, AUDIO_DIR,
    AUDIO_FEEDBACK_ENABLED,
    OPENAI_API_KEY,
    PREFER_LOCAL,
    SAMPLE_RATE, CHANNELS,
    audio_operation_lock, service_processes,
    logger, disable_sounddevice_stderr_redirect
)

# All configuration imported from config.py
# Track if startup has been initialized
_startup_initialized = False


# Sounddevice workaround already applied in config.py


async def startup_initialization():
    """Initialize services on startup based on configuration"""
    global _startup_initialized
    
    if _startup_initialized:
        return
    
    _startup_initialized = True
    logger.info("Running startup initialization...")
    
    # Log initial status
    logger.info("Service initialization complete")


def cleanup_on_shutdown():
    """Cleanup function called on shutdown"""
    from voice_mode.core import cleanup as cleanup_clients
    
    # Cleanup OpenAI clients
    cleanup_clients()
    
    # Stop any services we started
    for name, process in service_processes.items():
        if process and process.poll() is None:
            logger.info(f"Stopping {name} service (PID: {process.pid})...")
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            logger.info(f"✓ {name} service stopped")
