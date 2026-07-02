"""
Approach A (agent mode) - voice client.

Connects Voice Live to the orchestrator Foundry agent. Voice Live handles the
audio (speech-to-text + text-to-speech); the orchestrator agent does the
reasoning with its model and delegates Office questions to the specialist
through its A2A tool.

Run this on a machine WITH a microphone and speakers (your Windows host, not the
devcontainer):
    python approach_a_agent_mode/voice_orchestrator.py

Prerequisites:
- `az login` (Entra ID is required for agent mode; API keys are not supported).
- The orchestrator agent already created (create_orchestrator_agent.py).

Audio handling is adapted from the official Voice Live Python quickstart.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import queue
import signal
import sys
from datetime import datetime
from typing import Any, Optional, TYPE_CHECKING, cast

import pyaudio
from dotenv import load_dotenv
from azure.identity.aio import AzureCliCredential
from azure.ai.voicelive.aio import connect, AgentSessionConfig
from azure.ai.voicelive.models import (
    InputAudioFormat,
    Modality,
    OutputAudioFormat,
    RequestSession,
    ServerEventType,
    MessageItem,
    InputTextContentPart,
    LlmInterimResponseConfig,
    InterimResponseTrigger,
)

if TYPE_CHECKING:
    from azure.ai.voicelive.aio import VoiceLiveConnection

_script_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_script_dir, "..", ".env"), override=True)

os.makedirs(os.path.join(_script_dir, "logs"), exist_ok=True)
_timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
logging.basicConfig(
    filename=os.path.join(_script_dir, "logs", f"{_timestamp}_voicelive.log"),
    filemode="w",
    format="%(asctime)s:%(name)s:%(levelname)s:%(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


class AudioProcessor:
    """Real-time microphone capture and speaker playback (PCM16, 24kHz, mono)."""

    loop: asyncio.AbstractEventLoop

    class AudioPlaybackPacket:
        """A packet queued for playback."""

        def __init__(self, seq_num: int, data: Optional[bytes]):
            self.seq_num = seq_num
            self.data = data

    def __init__(self, connection: "VoiceLiveConnection") -> None:
        self.connection = connection
        self.audio = pyaudio.PyAudio()

        self.format = pyaudio.paInt16
        self.channels = 1
        self.rate = 24000
        self.chunk_size = 1200  # 50ms

        self.input_stream: Optional[pyaudio.Stream] = None
        self.playback_queue: "queue.Queue[AudioProcessor.AudioPlaybackPacket]" = queue.Queue()
        self.playback_base = 0
        self.next_seq_num = 0
        self.output_stream: Optional[pyaudio.Stream] = None

    def start_capture(self) -> None:
        """Start streaming microphone audio to Voice Live."""

        def _capture_callback(in_data, _frame_count, _time_info, _status_flags):
            audio_base64 = base64.b64encode(in_data).decode("utf-8")
            asyncio.run_coroutine_threadsafe(
                self.connection.input_audio_buffer.append(audio=audio_base64),
                self.loop,
            )
            return (None, pyaudio.paContinue)

        if self.input_stream:
            return

        self.loop = asyncio.get_event_loop()
        self.input_stream = self.audio.open(
            format=self.format,
            channels=self.channels,
            rate=self.rate,
            input=True,
            frames_per_buffer=self.chunk_size,
            stream_callback=_capture_callback,
        )
        logger.info("Started audio capture")

    def start_playback(self) -> None:
        """Start the speaker playback stream."""
        if self.output_stream:
            return

        remaining = bytes()

        def _playback_callback(_in_data, frame_count, _time_info, _status_flags):
            nonlocal remaining
            frame_count *= pyaudio.get_sample_size(pyaudio.paInt16)

            out = remaining[:frame_count]
            remaining = remaining[frame_count:]

            while len(out) < frame_count:
                try:
                    packet = self.playback_queue.get_nowait()
                except queue.Empty:
                    out = out + bytes(frame_count - len(out))
                    continue

                if not packet or not packet.data:
                    # None packet marks the end of the stream.
                    break

                if packet.seq_num < self.playback_base:
                    # Skip audio that was cancelled by a barge-in.
                    if len(remaining) > 0:
                        remaining = bytes()
                    continue

                num_to_take = frame_count - len(out)
                out = out + packet.data[:num_to_take]
                remaining = packet.data[num_to_take:]

            if len(out) >= frame_count:
                return (out, pyaudio.paContinue)
            return (out, pyaudio.paComplete)

        self.output_stream = self.audio.open(
            format=self.format,
            channels=self.channels,
            rate=self.rate,
            output=True,
            frames_per_buffer=self.chunk_size,
            stream_callback=_playback_callback,
        )
        logger.info("Audio playback ready")

    def _next_seq_num(self) -> int:
        seq = self.next_seq_num
        self.next_seq_num += 1
        return seq

    def queue_audio(self, audio_data: Optional[bytes]) -> None:
        """Queue an audio chunk for playback."""
        self.playback_queue.put(
            AudioProcessor.AudioPlaybackPacket(self._next_seq_num(), audio_data)
        )

    def skip_pending_audio(self) -> None:
        """Drop queued audio (used when the user interrupts the agent)."""
        self.playback_base = self._next_seq_num()

    def shutdown(self) -> None:
        """Release audio resources."""
        if self.input_stream:
            self.input_stream.stop_stream()
            self.input_stream.close()
            self.input_stream = None

        if self.output_stream:
            self.skip_pending_audio()
            self.queue_audio(None)
            self.output_stream.stop_stream()
            self.output_stream.close()
            self.output_stream = None

        if self.audio:
            self.audio.terminate()
        logger.info("Audio processor cleaned up")


class VoiceOrchestrator:
    """Voice client that talks to the orchestrator agent in agent mode."""

    def __init__(self, endpoint, credential, agent_name, project_name, agent_version=None):
        self.endpoint = endpoint
        self.credential = credential
        self.agent_config: AgentSessionConfig = {
            "agent_name": agent_name,
            "agent_version": agent_version,
            "project_name": project_name,
        }
        self.connection: Optional["VoiceLiveConnection"] = None
        self.audio_processor: Optional[AudioProcessor] = None
        self.greeting_sent = False
        self._active_response = False
        self._response_done = False

    async def start(self) -> None:
        """Open the Voice Live session and process events until interrupted."""
        try:
            async with connect(
                endpoint=self.endpoint,
                credential=self.credential,
                api_version="2026-01-01-preview",
                agent_config=self.agent_config,
            ) as connection:
                self.connection = connection
                self.audio_processor = AudioProcessor(connection)

                await self._setup_session()
                self.audio_processor.start_playback()

                print("\n" + "=" * 65)
                print("VOICE ORCHESTRATOR READY (Approach A - agent mode)")
                print("Start speaking. Press Ctrl+C to exit.")
                print("=" * 65 + "\n")

                await self._process_events()
        finally:
            if self.audio_processor:
                self.audio_processor.shutdown()

    async def _setup_session(self) -> None:
        """Send the session configuration.

        Voice and turn detection are stored on the agent (see the setup script),
        so here we only add an interim response to cover tool-call latency (for
        example while the orchestrator queries the specialist over A2A).
        """
        interim = LlmInterimResponseConfig(
            triggers=[InterimResponseTrigger.TOOL, InterimResponseTrigger.LATENCY],
            latency_threshold_ms=100,
            instructions=(
                "Give a short, friendly filler in the user's language while "
                "processing. Do not say you lack real-time access when calling tools."
            ),
        )
        session_config = RequestSession(
            modalities=[Modality.TEXT, Modality.AUDIO],
            input_audio_format=InputAudioFormat.PCM16,
            output_audio_format=OutputAudioFormat.PCM16,
            interim_response=interim,
        )
        assert self.connection is not None
        await self.connection.session.update(session=session_config)
        logger.info("Session configuration sent")

    async def _process_events(self) -> None:
        assert self.connection is not None
        async for event in self.connection:
            await self._handle_event(event)

    async def _handle_event(self, event: Any) -> None:
        ap = self.audio_processor
        conn = self.connection
        assert ap is not None and conn is not None

        if event.type == ServerEventType.SESSION_UPDATED:
            logger.info("Session ready: %s", event.session.id)

            # Proactive greeting so the assistant speaks first.
            if not self.greeting_sent:
                self.greeting_sent = True
                await conn.conversation.item.create(
                    item=MessageItem(
                        role="system",
                        content=[
                            InputTextContentPart(
                                text=(
                                    "Greet the user with one short, friendly "
                                    "sentence in both English and Spanish, then "
                                    "continue in whatever language they use."
                                )
                            )
                        ],
                    )
                )
                await conn.response.create()

            ap.start_capture()

        elif event.type == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
            print(f"You said: {event.get('transcript', '')}")

        elif event.type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
            print(f"Agent: {event.get('transcript', '')}")

        elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            print("Listening...")
            ap.skip_pending_audio()
            if self._active_response and not self._response_done:
                try:
                    await conn.response.cancel()
                except Exception as exc:  # noqa: BLE001 - benign barge-in race
                    if "no active response" not in str(exc).lower():
                        logger.warning("Cancel failed: %s", exc)

        elif event.type == ServerEventType.RESPONSE_CREATED:
            self._active_response = True
            self._response_done = False

        elif event.type == ServerEventType.RESPONSE_AUDIO_DELTA:
            ap.queue_audio(event.delta)

        elif event.type == ServerEventType.RESPONSE_DONE:
            self._active_response = False
            self._response_done = True

        elif event.type == ServerEventType.ERROR:
            logger.error("Voice Live error: %s", event.error.message)


def _check_audio_devices() -> None:
    """Make sure a microphone and speakers are available."""
    p = pyaudio.PyAudio()
    try:
        def _has_channels(key):
            return any(
                cast(float, p.get_device_info_by_index(i).get(key, 0) or 0) > 0
                for i in range(p.get_device_count())
            )

        if not _has_channels("maxInputChannels"):
            sys.exit("No audio input devices found. Please check your microphone.")
        if not _has_channels("maxOutputChannels"):
            sys.exit("No audio output devices found. Please check your speakers.")
    finally:
        p.terminate()


def main() -> None:
    endpoint = os.environ["VOICELIVE_ENDPOINT"]
    project_name = os.environ["PROJECT_NAME"]
    agent_name = os.environ["ORCHESTRATOR_AGENT_NAME"]
    agent_version = os.environ.get("AGENT_VERSION")

    # Entra ID is required for agent mode.
    credential = AzureCliCredential()

    assistant = VoiceOrchestrator(
        endpoint=endpoint,
        credential=credential,
        agent_name=agent_name,
        project_name=project_name,
        agent_version=agent_version,
    )

    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        asyncio.run(assistant.start())
    except KeyboardInterrupt:
        print("\nVoice orchestrator shut down. Goodbye!")


if __name__ == "__main__":
    _check_audio_devices()
    print("Qubix Voice Orchestrator - Approach A (agent mode)")
    print("=" * 65)
    main()
