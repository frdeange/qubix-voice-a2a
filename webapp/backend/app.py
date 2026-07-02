"""
Qubix voice web app - FastAPI backend.

Bridges the browser (over a WebSocket) to Azure Voice Live (model mode,
gpt-realtime). The browser handles the microphone and speaker and the user
sign-in (MSAL); it sends the user's delegated Work IQ token once at session
start. When the model calls the `ask_office` tool, this backend queries Work IQ
with that user token, so Work IQ acts on behalf of the signed-in user.

Run:  uvicorn app:app --host 0.0.0.0 --port 8000
"""

import asyncio
import base64
import json
import os
from contextlib import suppress
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from azure.core.credentials import AzureKeyCredential
from azure.identity.aio import DefaultAzureCredential
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

from workiq import ask_workiq

# Load the shared repo .env (two levels up from webapp/backend).
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

VOICELIVE_ENDPOINT = os.environ["VOICELIVE_ENDPOINT"]
REALTIME_MODEL = os.environ.get("REALTIME_MODEL", "gpt-realtime")
VOICE_NAME = os.environ.get("VOICE_NAME", "en-US-Ava:DragonHDLatestNeural")
API_KEY = os.environ.get("AZURE_VOICELIVE_API_KEY")

# Values the frontend needs for MSAL sign-in (exposed via /config).
TENANT_ID = os.environ.get("TENANT_ID", "")
CLIENT_ID = os.environ.get("CLIENT_ID", "")
WORKIQ_SCOPE = os.environ.get(
    "WORKIQ_SCOPE", "fdcc1f02-fc51-4226-8753-f668596af7f7/WorkIQAgent.Ask"
)

INSTRUCTIONS = (
    "You are a friendly voice assistant. Detect the language the user speaks and "
    "always reply in that same language. For Microsoft Office / Microsoft 365 "
    "questions (Outlook, Teams, Word, Excel, SharePoint, OneDrive, calendar, "
    "email), call the ask_office function and read back its answer. Say a short "
    "filler like 'let me check that' before calling it. Keep replies short and "
    "conversational."
)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/config")
def config() -> dict:
    """Sign-in configuration consumed by the frontend."""
    return {"tenantId": TENANT_ID, "clientId": CLIENT_ID, "workiqScope": WORKIQ_SCOPE}


def _credential():
    """Voice Live auth: API key if provided, otherwise Entra (az login)."""
    return AzureKeyCredential(API_KEY) if API_KEY else DefaultAzureCredential()


class VoiceBridge:
    """Bridges one browser WebSocket to one Voice Live session."""

    def __init__(self, ws: WebSocket, user_token: str):
        self.ws = ws
        self.user_token = user_token
        self.connection = None
        self._active_response = False
        self._response_done = False
        self._pending: Optional[dict] = None

    async def run(self) -> None:
        credential = _credential()
        try:
            async with connect(
                endpoint=VOICELIVE_ENDPOINT,
                credential=credential,
                model=REALTIME_MODEL,
                api_version="2026-01-01-preview",
            ) as connection:
                self.connection = connection
                await self._setup_session()

                browser = asyncio.create_task(self._pump_browser_to_vl())
                service = asyncio.create_task(self._pump_vl_to_browser())
                done, pending = await asyncio.wait(
                    {browser, service}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
        finally:
            if hasattr(credential, "close"):
                with suppress(Exception):
                    await credential.close()

    async def _setup_session(self) -> None:
        voice = AzureStandardVoice(name=VOICE_NAME) if "-" in VOICE_NAME else VOICE_NAME
        office_tool: Tool = FunctionTool(
            name="ask_office",
            description=(
                "Answer Microsoft Office / Microsoft 365 questions (Outlook, "
                "Teams, Word, Excel, SharePoint, OneDrive, calendar, email) using "
                "the signed-in user's Work IQ data."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The user's Office / Microsoft 365 question.",
                    }
                },
                "required": ["question"],
            },
        )
        session = RequestSession(
            modalities=[Modality.TEXT, Modality.AUDIO],
            instructions=INSTRUCTIONS,
            voice=voice,
            input_audio_format=InputAudioFormat.PCM16,
            output_audio_format=OutputAudioFormat.PCM16,
            turn_detection=AzureSemanticVadMultilingual(
                threshold=0.5,
                prefix_padding_ms=300,
                silence_duration_ms=500,
                auto_truncate=True,
            ),
            input_audio_transcription=AudioInputTranscriptionOptions(model="whisper-1"),
            interim_response=LlmInterimResponseConfig(
                triggers=[InterimResponseTrigger.TOOL, InterimResponseTrigger.LATENCY],
                latency_threshold_ms=100,
                instructions=(
                    "Give a short, friendly filler in the user's language while "
                    "processing. Do not say you lack real-time access when calling tools."
                ),
            ),
            tools=[office_tool],
            tool_choice=ToolChoiceLiteral.AUTO,
        )
        await self.connection.session.update(session=session)

    async def _pump_browser_to_vl(self) -> None:
        """Forward microphone audio from the browser to Voice Live."""
        try:
            while True:
                raw = await self.ws.receive_text()
                message = json.loads(raw)
                if message.get("type") == "audio":
                    await self.connection.input_audio_buffer.append(audio=message["data"])
                elif message.get("type") == "stop":
                    break
        except WebSocketDisconnect:
            pass

    async def _pump_vl_to_browser(self) -> None:
        """Forward Voice Live events (audio, transcripts, tool calls) to the browser."""
        async for event in self.connection:
            await self._handle_event(event)

    async def _send(self, payload: dict) -> None:
        with suppress(Exception):
            await self.ws.send_text(json.dumps(payload))

    async def _handle_event(self, event: Any) -> None:
        conn = self.connection

        if event.type == ServerEventType.SESSION_UPDATED:
            await self._send({"type": "ready"})
            await conn.response.create()  # proactive greeting

        elif event.type == ServerEventType.RESPONSE_AUDIO_DELTA:
            # event.delta is raw PCM16 bytes; browser expects base64.
            await self._send(
                {"type": "audio", "data": base64.b64encode(event.delta).decode("utf-8")}
            )

        elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            await self._send({"type": "barge_in"})
            if self._active_response and not self._response_done:
                with suppress(Exception):
                    await conn.response.cancel()

        elif event.type == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
            await self._send({"type": "user_text", "text": event.get("transcript", "")})

        elif event.type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
            await self._send({"type": "agent_text", "text": event.get("transcript", "")})

        elif event.type == ServerEventType.RESPONSE_CREATED:
            self._active_response = True
            self._response_done = False

        elif event.type == ServerEventType.RESPONSE_DONE:
            self._active_response = False
            self._response_done = True
            if self._pending and "arguments" in self._pending:
                await self._execute_function_call(self._pending)
                self._pending = None

        elif event.type == ServerEventType.CONVERSATION_ITEM_CREATED:
            if event.item.type == ItemType.FUNCTION_CALL:
                self._pending = {
                    "name": event.item.name,
                    "call_id": event.item.call_id,
                    "previous_item_id": event.item.id,
                }
                await self._send({"type": "tool_call", "name": event.item.name})

        elif event.type == ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE:
            if self._pending and event.call_id == self._pending["call_id"]:
                self._pending["arguments"] = event.arguments

        elif event.type == ServerEventType.ERROR:
            await self._send({"type": "error", "message": event.error.message})

    async def _execute_function_call(self, call: dict) -> None:
        conn = self.connection
        if call["name"] != "ask_office":
            return
        try:
            args = json.loads(call["arguments"]) if call["arguments"] else {}
        except json.JSONDecodeError:
            args = {}
        question = args.get("question", "")

        try:
            answer = await ask_workiq(self.user_token, question)
        except Exception as exc:  # noqa: BLE001 - surface tool errors to the model
            answer = f"Work IQ could not be reached: {exc}"

        await conn.conversation.item.create(
            previous_item_id=call["previous_item_id"],
            item=FunctionCallOutputItem(
                call_id=call["call_id"], output=json.dumps({"answer": answer})
            ),
        )
        await conn.response.create()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    # First message must carry the user's Work IQ token.
    start = json.loads(await ws.receive_text())
    user_token = start.get("workiq_token", "")
    if not user_token:
        await ws.send_text(json.dumps({"type": "error", "message": "Missing Work IQ token"}))
        await ws.close()
        return

    try:
        await VoiceBridge(ws, user_token).run()
    except WebSocketDisconnect:
        pass
    finally:
        with suppress(Exception):
            await ws.close()
