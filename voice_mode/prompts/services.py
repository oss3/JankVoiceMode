"""Service management prompts for whisper."""

from voice_mode.server import mcp


@mcp.prompt(name="whisper")
def whisper_prompt(action: str = "status") -> str:
    """Manage Whisper speech-to-text service.
    
    Args:
        action: Service action (status, start, stop, restart, enable, disable, logs) or install request
    """
    valid_actions = ["status", "start", "stop", "restart", "enable", "disable", "logs"]
    
    # Check if user wants to install
    install_keywords = ["install", "setup", "configure", "download", "get"]
    if action.lower() in install_keywords or any(keyword in action.lower() for keyword in install_keywords):
        return "The user wants to install Whisper. Use the whisper_install tool to install the Whisper STT service."
    
    if action not in valid_actions:
        return f"Invalid action '{action}'. Use one of: {', '.join(valid_actions)}"
    
    return f"Use the service tool with service_name='whisper' and action='{action}'"