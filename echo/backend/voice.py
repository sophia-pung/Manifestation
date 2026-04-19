"""
ElevenLabs TTS wrapper with macOS `say` fallback.

speak(text) plays audio through system speakers.
interrupt() cancels current playback immediately.

Uses asyncio subprocess for non-blocking playback.
"""

import asyncio
import logging
import os
import tempfile

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

_current_proc: asyncio.subprocess.Process | None = None
_speak_lock = asyncio.Lock()


async def interrupt():
    global _current_proc
    if _current_proc and _current_proc.returncode is None:
        try:
            _current_proc.terminate()
        except ProcessLookupError:
            pass
        _current_proc = None


async def speak(text: str):
    """Speak text aloud. Interrupts any currently playing audio."""
    await interrupt()
    async with _speak_lock:
        if ELEVENLABS_API_KEY:
            await _speak_elevenlabs(text)
        else:
            log.warning("No ElevenLabs key — falling back to macOS say")
            await _speak_macos(text)


async def _speak_elevenlabs(text: str):
    global _current_proc
    try:
        # Import here so missing package doesn't break the whole app
        from elevenlabs import ElevenLabs

        client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

        # Generate audio bytes
        audio_bytes = b""
        response = client.text_to_speech.convert(
            voice_id=ELEVENLABS_VOICE_ID,
            text=text,
            model_id="eleven_turbo_v2",
            output_format="mp3_44100_128",
        )
        for chunk in response:
            if chunk:
                audio_bytes += chunk

        # Write to temp file and play
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(audio_bytes)
            tmp_path = f.name

        _current_proc = await asyncio.create_subprocess_exec(
            "afplay", tmp_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await _current_proc.wait()
        os.unlink(tmp_path)

    except Exception as e:
        log.warning("ElevenLabs failed (%s) — falling back to macOS say", e)
        await _speak_macos(text)


async def _speak_macos(text: str):
    global _current_proc
    _current_proc = await asyncio.create_subprocess_exec(
        "say", text,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await _current_proc.wait()
