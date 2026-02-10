#!/usr/bin/env python
"""VoiceMode MCP Server - Modular version using FastMCP patterns."""

import atexit
import os
import platform
import subprocess

# Note: audioop deprecation warning is suppressed in tools/__init__.py
# (right before pydub is imported) to ensure it's applied after numpy/scipy
# filters are added

# Extend PATH to include common tool locations before any imports that might need them
# MCP servers run in isolated environments that may not inherit shell PATH
if platform.system() == "Darwin":
    # macOS: Add Homebrew paths (Intel and Apple Silicon)
    homebrew_paths = ["/opt/homebrew/bin", "/usr/local/bin"]
    current_path = os.environ.get("PATH", "")
    paths_to_add = [p for p in homebrew_paths if p not in current_path]
    if paths_to_add:
        os.environ["PATH"] = ":".join(paths_to_add) + ":" + current_path

from fastmcp import FastMCP

# Create FastMCP instance
mcp = FastMCP("voicemode")

# Import shared configuration and utilities
from . import config

# Auto-import all tools, prompts, and resources
# The __init__.py files in each directory handle the imports
from . import tools
from . import prompts
from . import resources

# Global reference to PTT daemon process
_ptt_daemon_process = None


def start_ptt_daemon():
    """Start the PTT Elixir daemon if not already running.

    Returns the process if started, or None if already running or failed.
    """
    global _ptt_daemon_process
    import logging
    from pathlib import Path
    from .ptt_client import PTTClient

    logger = logging.getLogger("voicemode")

    # Check if daemon is already running
    client = PTTClient()
    if client.is_daemon_running():
        logger.info("PTT daemon already running")
        return None

    # Find JankSDK directory (sibling of JankVoiceMode)
    voicemode_dir = Path(__file__).parent.parent
    janksdk_elixir = voicemode_dir.parent / "JankSDK" / "elixir"

    if not janksdk_elixir.exists():
        logger.warning(f"JankSDK not found at {janksdk_elixir}, PTT daemon will not start")
        return None

    # Start the daemon
    logger.info(f"Starting PTT daemon from {janksdk_elixir}")
    try:
        # Run mix in background - the daemon will stay alive even if this process exits
        _ptt_daemon_process = subprocess.Popen(
            ["mix", "run", "-e", "Jank.PTT.start(); Process.sleep(:infinity)", "--no-halt"],
            cwd=str(janksdk_elixir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True  # Detach from parent process group
        )

        # Wait briefly for daemon to start
        import time
        for _ in range(10):  # Wait up to 1 second
            time.sleep(0.1)
            if client.is_daemon_running():
                logger.info(f"PTT daemon started (PID: {_ptt_daemon_process.pid})")
                return _ptt_daemon_process

        logger.warning("PTT daemon started but not responding yet")
        return _ptt_daemon_process

    except Exception as e:
        logger.error(f"Failed to start PTT daemon: {e}")
        return None


def stop_ptt_daemon():
    """Stop the PTT daemon if we started it."""
    global _ptt_daemon_process
    import logging

    logger = logging.getLogger("voicemode")

    if _ptt_daemon_process is not None:
        logger.info("Stopping PTT daemon")
        try:
            _ptt_daemon_process.terminate()
            _ptt_daemon_process.wait(timeout=2.0)
        except Exception as e:
            logger.warning(f"Error stopping PTT daemon: {e}")
            try:
                _ptt_daemon_process.kill()
            except Exception:
                pass
        _ptt_daemon_process = None


# Main entry point
def main():
    """Run the VoiceMode MCP server."""
    import sys
    from .config import setup_logging, EVENT_LOG_ENABLED, EVENT_LOG_DIR
    from .utils import initialize_event_logger
    from .utils.ffmpeg_check import check_ffmpeg, check_ffprobe, get_install_instructions
    from pathlib import Path

    # Note: Warning filters are set at module level (top of file) to catch
    # deprecation warnings from imports before main() is called

    # For MCP mode (stdio transport), we need to let the server start
    # so the LLM can see error messages in tool responses
    # MCP servers use stdio with stdin/stdout connected to pipes, not terminals
    is_mcp_mode = not sys.stdin.isatty() or not sys.stdout.isatty()
    
    # Check FFmpeg availability
    ffmpeg_installed, _ = check_ffmpeg()
    ffprobe_installed, _ = check_ffprobe()
    ffmpeg_available = ffmpeg_installed and ffprobe_installed
    
    if not ffmpeg_available and not is_mcp_mode:
        # Interactive mode - show error and exit
        print("\n" + "="*60)
        print("⚠️  FFmpeg Installation Required")
        print("="*60)
        print(get_install_instructions())
        print("="*60 + "\n")
        print("❌ Voice Mode cannot start without FFmpeg.")
        print("Please install FFmpeg and try again.\n")
        sys.exit(1)
    
    # Set up logging
    logger = setup_logging()
    
    # Log version information
    from .version import __version__
    logger.info(f"Starting VoiceMode v{__version__}")
    
    # Log FFmpeg status for MCP mode
    if not ffmpeg_available:
        logger.warning("FFmpeg is not installed - audio conversion features will not work")
        logger.warning("Voice features will fail with helpful error messages")
        # Store this globally so tools can check it
        config.FFMPEG_AVAILABLE = False
    else:
        config.FFMPEG_AVAILABLE = True
    
    # Initialize event logger
    if EVENT_LOG_ENABLED:
        event_logger = initialize_event_logger(
            log_dir=Path(EVENT_LOG_DIR),
            enabled=True
        )
        logger.info(f"Event logging enabled, writing to {EVENT_LOG_DIR}")
    else:
        logger.info("Event logging disabled")

    # Start PTT daemon (for push-to-talk support)
    start_ptt_daemon()
    atexit.register(stop_ptt_daemon)

    # Run the server
    mcp.run(transport="stdio")

if __name__ == "__main__":
    main()