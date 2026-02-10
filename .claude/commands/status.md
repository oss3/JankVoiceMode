---
description: Check the status of VoiceMode services
---

# /voicemode:status

Check the status of VoiceMode services.

## Usage

```
/voicemode:status
```

## Description

Shows the current status of VoiceMode services including Whisper (STT) and other configured services.

## Implementation

Use the `mcp__voicemode__service` tool:

```json
{
  "service_name": "whisper",
  "action": "status"
}
```

Check all services:

```bash
# Check Whisper (STT)
mcp__voicemode__service service_name=whisper action=status

# Check LiveKit (optional)
mcp__voicemode__service service_name=livekit action=status
```

## Output

Shows for each service:
- Running status
- Resource usage
- Endpoint availability
