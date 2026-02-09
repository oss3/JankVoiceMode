"""PTT daemon client for VoiceMode.

Connects to the Elixir PTT daemon via Unix socket for reliable
push-to-talk state management.

The daemon runs at ~/.voicemode/ptt.sock and supports:
- TOGGLE: Toggle PTT state (idle→listening, listening→idle, speaking→listening)
- START: Explicit start listening
- STOP: Stop listening
- INTERRUPT: Return to idle
- STATUS: Get current state
- SUBSCRIBE: Subscribe to state change events

States: idle, listening, speaking
"""

import asyncio
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Callable, Optional
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class PTTState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    SPEAKING = "speaking"
    UNKNOWN = "unknown"


@dataclass
class PTTConfig:
    """Configuration for PTT daemon connection."""
    socket_path: str = "~/.voicemode/ptt.sock"
    timeout: float = 5.0


class PTTClient:
    """Client for the PTT Elixir daemon.

    Provides both synchronous command sending and async event subscription.
    """

    def __init__(self, config: Optional[PTTConfig] = None):
        self.config = config or PTTConfig()
        self.socket_path = os.path.expanduser(self.config.socket_path)
        self._subscriber_socket: Optional[socket.socket] = None
        self._subscriber_thread: Optional[threading.Thread] = None
        self._subscriber_callback: Optional[Callable[[PTTState, PTTState], None]] = None
        self._subscriber_running = False

    def is_daemon_running(self) -> bool:
        """Check if the PTT daemon is running and accepting connections."""
        try:
            result = self._send_command("STATUS")
            return result in ("idle", "listening", "speaking")
        except Exception:
            return False

    def toggle(self) -> PTTState:
        """Toggle PTT state.

        - From idle → listening
        - From listening → idle
        - From speaking → listening (interrupts speech)
        """
        result = self._send_command("TOGGLE")
        return self._parse_state(result)

    def start(self) -> PTTState:
        """Start listening. No-op if already listening."""
        result = self._send_command("START")
        return self._parse_state(result)

    def stop(self) -> PTTState:
        """Stop listening. No-op if not listening."""
        result = self._send_command("STOP")
        return self._parse_state(result)

    def interrupt(self) -> PTTState:
        """Interrupt current activity and return to idle."""
        result = self._send_command("INTERRUPT")
        return self._parse_state(result)

    def set_speaking(self) -> PTTState:
        """Set state to speaking (call when TTS starts)."""
        result = self._send_command("SET_SPEAKING")
        return self._parse_state(result)

    def status(self) -> PTTState:
        """Get current PTT state."""
        result = self._send_command("STATUS")
        return self._parse_state(result)

    def subscribe(self, callback: Callable[[PTTState, PTTState], None]) -> None:
        """Subscribe to state change events.

        The callback receives (old_state, new_state) on each change.
        This runs in a background thread.
        """
        if self._subscriber_running:
            raise RuntimeError("Already subscribed")

        self._subscriber_callback = callback
        self._subscriber_running = True
        self._subscriber_thread = threading.Thread(
            target=self._subscriber_loop,
            daemon=True,
            name="PTTSubscriber"
        )
        self._subscriber_thread.start()

    def unsubscribe(self) -> None:
        """Stop subscription."""
        self._subscriber_running = False
        if self._subscriber_socket:
            try:
                self._subscriber_socket.close()
            except Exception:
                pass
        if self._subscriber_thread:
            self._subscriber_thread.join(timeout=1.0)

    def _send_command(self, command: str) -> str:
        """Send a command to the daemon and return the response."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.config.timeout)
        try:
            sock.connect(self.socket_path)
            sock.send(f"{command}\n".encode())
            response = sock.recv(1024).decode().strip()
            return response
        finally:
            sock.close()

    def _parse_state(self, response: str) -> PTTState:
        """Parse a state string from the daemon."""
        response = response.lower().strip()
        if response == "idle":
            return PTTState.IDLE
        elif response == "listening":
            return PTTState.LISTENING
        elif response == "speaking":
            return PTTState.SPEAKING
        else:
            logger.warning(f"Unknown PTT state: {response}")
            return PTTState.UNKNOWN

    def _subscriber_loop(self) -> None:
        """Background thread that listens for state change events."""
        while self._subscriber_running:
            try:
                self._subscriber_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._subscriber_socket.settimeout(30.0)  # Long timeout for subscription
                self._subscriber_socket.connect(self.socket_path)
                self._subscriber_socket.send(b"SUBSCRIBE\n")

                # Read the OK response
                response = self._subscriber_socket.recv(1024).decode().strip()
                if response != "OK":
                    logger.warning(f"Unexpected subscribe response: {response}")
                    continue

                logger.debug("PTT subscriber connected")

                # Listen for state change events
                buffer = ""
                while self._subscriber_running:
                    try:
                        data = self._subscriber_socket.recv(1024).decode()
                        if not data:
                            break

                        buffer += data
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            self._handle_event(line.strip())

                    except socket.timeout:
                        continue
                    except Exception as e:
                        logger.debug(f"Subscriber recv error: {e}")
                        break

            except Exception as e:
                if self._subscriber_running:
                    logger.debug(f"PTT subscriber connection error: {e}, reconnecting...")
                    asyncio.sleep(1.0)
            finally:
                if self._subscriber_socket:
                    try:
                        self._subscriber_socket.close()
                    except Exception:
                        pass

    def _handle_event(self, event: str) -> None:
        """Handle a state change event from the daemon."""
        # Events are in format: STATE:new_state
        if event.startswith("STATE:"):
            new_state_str = event[6:]
            new_state = self._parse_state(new_state_str)
            if self._subscriber_callback:
                # We don't have the old state here, pass UNKNOWN
                self._subscriber_callback(PTTState.UNKNOWN, new_state)


class PTTFileClient:
    """Fallback PTT client using file-based signaling.

    Used when the Elixir daemon is not running.
    """

    def __init__(self, base_dir: str = "~/.voicemode"):
        self.base_dir = Path(os.path.expanduser(base_dir))
        self.toggle_file = self.base_dir / "push-to-talk-toggle"
        self.start_file = self.base_dir / "push-to-talk-start"
        self.stop_file = self.base_dir / "push-to-talk-stop"
        self._state = PTTState.IDLE

    def is_daemon_running(self) -> bool:
        """File-based client is always 'running'."""
        return True

    def toggle(self) -> PTTState:
        """Toggle using file check."""
        if self._state == PTTState.IDLE:
            self._state = PTTState.LISTENING
        else:
            self._state = PTTState.IDLE
        return self._state

    def check_toggle_file(self) -> bool:
        """Check if toggle file exists and delete it."""
        if self.toggle_file.exists():
            try:
                self.toggle_file.unlink()
                return True
            except Exception:
                pass
        return False

    def check_start_file(self) -> bool:
        """Check if start file exists and delete it."""
        if self.start_file.exists():
            try:
                self.start_file.unlink()
                return True
            except Exception:
                pass
        return False

    def check_stop_file(self) -> bool:
        """Check if stop file exists and delete it."""
        if self.stop_file.exists():
            try:
                self.stop_file.unlink()
                return True
            except Exception:
                pass
        return False

    def status(self) -> PTTState:
        return self._state


def get_ptt_client() -> PTTClient:
    """Get a PTT client, preferring the daemon if available."""
    client = PTTClient()
    if client.is_daemon_running():
        logger.info("Using PTT daemon")
        return client
    else:
        logger.info("PTT daemon not running, using file-based fallback")
        # Return the file-based client as fallback
        # Note: This has a different interface, callers need to handle both
        return PTTFileClient()
