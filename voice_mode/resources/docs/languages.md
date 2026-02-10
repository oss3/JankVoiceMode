# Non-English Language Support

## Overview

When speaking non-English languages, you may want to specify a `voice` appropriate for the target language. Default OpenAI voices speak non-English with an American accent, which may not be ideal.

## OpenAI Voices

OpenAI voices work for any language but maintain American English accent:
- `nova` - Female
- `shimmer` - Female
- `alloy` - Neutral
- `echo` - Male
- `fable` - Male
- `onyx` - Male

## Tips for Non-English

1. **STT (speech-to-text) automatically detects language** - no configuration needed
2. **TTS voice selection** depends on your configured TTS provider and its available voices
3. If your TTS provider supports language-specific voices, use them for better pronunciation
4. **Never use `coral` voice** - it's not supported

## Important Notes

1. **Always specify `voice`** when using non-English if your provider has language-specific voices
2. STT (speech-to-text) automatically detects language
3. Check your TTS provider's documentation for available language-specific voices

## See Also
- `voicemode-parameters` - Full parameter reference
- `voicemode-quickstart` - Basic usage examples
