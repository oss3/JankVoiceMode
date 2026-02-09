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

IMPORTANT: The PTT daemon MUST be running. There is no file-based fallback.
Start the daemon with: cd ~/Dev/code/JankSDK/elixir && mix run --no-halt
"""

import logging
import os
import socket
import threading
import time
from typing import Callable, Optional
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class PTTState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    SPEAKING = "speaking"
    UNKNOWN = "unknown"


class PTTDaemonNotRunning(Exception):
    """Raised when the PTT daemon is not running."""
    pass


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

    def require_daemon(self) -> None:
        """Raise PTTDaemonNotRunning if daemon is not available."""
        if not self.is_daemon_running():
            raise PTTDaemonNotRunning(
                "PTT daemon not running. Start with: cd ~/Dev/code/JankSDK/elixir && mix run --no-halt"
            )

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
                    time.sleep(1.0)
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


# Module-level singleton
_client: Optional[PTTClient] = None


def get_ptt_client() -> PTTClient:
    """Get the singleton PTT client.

    Raises PTTDaemonNotRunning if daemon is not available.
    """
    global _client
    if _client is None:
        _client = PTTClient()
    return _client
