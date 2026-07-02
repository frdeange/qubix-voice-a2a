"""
Approach B (model mode) - voice client.

Connects Voice Live directly to the gpt-realtime model for the lowest-latency
speech-to-speech experience. Because model mode has no server-side agent tools,
the orchestrator reaches the specialist through a Voice Live function tool
(`ask_office_specialist`) whose handler calls the specialist over A2A.

Run this on a machine WITH a microphone and speakers (your Windows host, not the
devcontainer):
    python approach_b_model_mode/voice_orchestrator.py

Prerequisites:
- `az login` with access to the Foundry project.
- The specialist agent already created and A2A-enabled (create_specialist_agent.py).

Audio handling is adapted from the official Voice Live Python quickstart.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import queue
import signal
import sys
from datetime import datetime
from typing import Any, Dict, Optional, TYPE_CHECKING, cast

import pyaudio
from dotenv import load_dotenv
from azure.identity.aio import AzureCliCredential
from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    AudioInputTranscriptionOptions,
    AzureSemanticVadMultilingual,
    AzureStandardVoice,
    FunctionCallOutputItem,
    FunctionTool,
    InputAudioFormat,
    InterimResponseTrigger,
    ItemType,
    LlmInterimResponseConfig,
    Modality,
    OutputAudioFormat,
    RequestSession,
    ServerEventType,
    Tool,
    ToolChoiceLiteral,
)

from specialist_a2a_client import ask_specialist

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

# Instructions steer the realtime model to delegate Office questions.
ORCHESTRATOR_INSTRUCTIONS = (
    "You are a friendly voice orchestrator. Detect the language the user speaks "
    "and always reply in that same language. Begin with one short welcome "
    "sentence in both English and Spanish, then continue in the user's "
    "language. Answer general questions yourself. For anything about Microsoft "
    "Office or Microsoft 365 (Outlook, Teams, Word, Excel, SharePoint, "
    "OneDrive), call the ask_office_specialist function and read back its "
    "answer. Say a short filler like 'let me check that' before calling the "
    "function. Keep replies short and conversational."
)


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
                    break

                if packet.seq_num < self.playback_base:
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
    """Voice client that talks directly to the realtime model in model mode."""

    def __init__(self, endpoint, credential, model, voice, instructions):
        self.endpoint = endpoint
        self.credential = credential
        self.model = model
        self.voice = voice
        self.instructions = instructions
        self.connection: Optional["VoiceLiveConnection"] = None
        self.audio_processor: Optional[AudioProcessor] = None
        self.conversation_started = False
        self._active_response = False
        self._response_done = False
        self._pending_function_call: Optional[Dict[str, Any]] = None

    async def start(self) -> None:
        """Open the Voice Live session and process events until interrupted."""
        try:
            async with connect(
                endpoint=self.endpoint,
                credential=self.credential,
                model=self.model,
            ) as connection:
                self.connection = connection
                self.audio_processor = AudioProcessor(connection)

                await self._setup_session()
                self.audio_processor.start_playback()

                print("\n" + "=" * 65)
                print("VOICE ORCHESTRATOR READY (Approach B - model mode)")
                print("Start speaking. Press Ctrl+C to exit.")
                print("=" * 65 + "\n")

                await self._process_events()
        finally:
            if self.audio_processor:
                self.audio_processor.shutdown()

    async def _setup_session(self) -> None:
        """Configure the realtime session, voice, VAD and the specialist tool."""
        voice_config = (
            AzureStandardVoice(name=self.voice) if "-" in self.voice else self.voice
        )

        specialist_tool: Tool = FunctionTool(
            name="ask_office_specialist",
            description=(
                "Ask the Microsoft Office / Microsoft 365 specialist a question "
                "and get an answer. Use for Outlook, Teams, Word, Excel, "
                "SharePoint and OneDrive."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The user's Office or Microsoft 365 question.",
                    }
                },
                "required": ["question"],
            },
        )

        session_config = RequestSession(
            modalities=[Modality.TEXT, Modality.AUDIO],
            instructions=self.instructions,
            voice=voice_config,
            input_audio_format=InputAudioFormat.PCM16,
            output_audio_format=OutputAudioFormat.PCM16,
            # Multilingual VAD auto-detects the language; auto_truncate keeps the
            # transcript aligned with what the user heard after a barge-in.
            turn_detection=AzureSemanticVadMultilingual(
                threshold=0.5,
                prefix_padding_ms=300,
                silence_duration_ms=500,
                auto_truncate=True,
            ),
            # No language pinned -> automatic language detection.
            input_audio_transcription=AudioInputTranscriptionOptions(model="whisper-1"),
            # Interim responses bridge the wait while the specialist is queried.
            interim_response=LlmInterimResponseConfig(
                triggers=[InterimResponseTrigger.TOOL, InterimResponseTrigger.LATENCY],
                latency_threshold_ms=100,
                instructions=(
                    "Give a short, friendly filler in the user's language while "
                    "processing. Do not say you lack real-time access when calling tools."
                ),
            ),
            tools=[specialist_tool],
            tool_choice=ToolChoiceLiteral.AUTO,
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
            if not self.conversation_started:
                self.conversation_started = True
                await conn.response.create()  # proactive greeting
            ap.start_capture()

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
            # The realtime model finished; now run any requested function call.
            if self._pending_function_call and "arguments" in self._pending_function_call:
                await self._execute_function_call(self._pending_function_call)
                self._pending_function_call = None

        elif event.type == ServerEventType.CONVERSATION_ITEM_CREATED:
            if event.item.type == ItemType.FUNCTION_CALL:
                self._pending_function_call = {
                    "name": event.item.name,
                    "call_id": event.item.call_id,
                    "previous_item_id": event.item.id,
                }
                print(f"Calling function: {event.item.name}")

        elif event.type == ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE:
            if (
                self._pending_function_call
                and event.call_id == self._pending_function_call["call_id"]
            ):
                self._pending_function_call["arguments"] = event.arguments

        elif event.type == ServerEventType.ERROR:
            logger.error("Voice Live error: %s", event.error.message)

    async def _execute_function_call(self, call_info: Dict[str, Any]) -> None:
        """Run the specialist tool and feed the result back to the model."""
        conn = self.connection
        assert conn is not None

        if call_info["name"] != "ask_office_specialist":
            logger.error("Unknown function: %s", call_info["name"])
            return

        try:
            args = json.loads(call_info["arguments"]) if call_info["arguments"] else {}
        except json.JSONDecodeError:
            args = {}
        question = args.get("question", "")

        try:
            answer = await ask_specialist(question)
        except Exception as exc:  # noqa: BLE001 - surface tool errors to the model
            logger.exception("Specialist A2A call failed")
            answer = f"The specialist could not be reached: {exc}"

        output = FunctionCallOutputItem(
            call_id=call_info["call_id"], output=json.dumps({"answer": answer})
        )
        await conn.conversation.item.create(
            previous_item_id=call_info["previous_item_id"], item=output
        )
        await conn.response.create()


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
    model = os.environ.get("REALTIME_MODEL", "gpt-realtime")
    voice = os.environ.get("VOICE_NAME", "en-US-Ava:DragonHDLatestNeural")

    credential = AzureCliCredential()

    assistant = VoiceOrchestrator(
        endpoint=endpoint,
        credential=credential,
        model=model,
        voice=voice,
        instructions=ORCHESTRATOR_INSTRUCTIONS,
    )

    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        asyncio.run(assistant.start())
    except KeyboardInterrupt:
        print("\nVoice orchestrator shut down. Goodbye!")


if __name__ == "__main__":
    _check_audio_devices()
    print("Qubix Voice Orchestrator - Approach B (model mode)")
    print("=" * 65)
    main()
