"""
Approach B helper: call the specialist agent over A2A.

This module exposes a single async function, `ask_specialist`, that sends a
question to the specialist's A2A endpoint and returns its text answer. The voice
client (model mode) calls it from inside a function tool.

Uses the open-source A2A Python SDK (a2a-sdk). The specialist's agent card
requires Entra ID auth and lives at a custom path (agentCard/v1.0), so we attach
a bearer token to the httpx client and point the resolver at that path.
"""

import os

import httpx
from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types.a2a_pb2 import Role, SendMessageRequest

# Load the shared .env by path so this works no matter the working directory.
_script_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_script_dir, "..", ".env"))

FOUNDRY_SCOPE = "https://ai.azure.com/.default"

PROJECT_ENDPOINT = os.environ["PROJECT_ENDPOINT"]
SPECIALIST_AGENT_NAME = os.environ["SPECIALIST_AGENT_NAME"]

# Base path for A2A interactions with the specialist agent.
A2A_BASE_URL = (
    f"{PROJECT_ENDPOINT}/agents/{SPECIALIST_AGENT_NAME}/endpoint/protocols/a2a"
)
AGENT_CARD_PATH = "agentCard/v1.0"


def _extract_text(response) -> str:
    """Pull readable text out of an A2A response object (best effort)."""
    content = getattr(response, "content", None)
    if content:
        parts = [getattr(part, "text", "") for part in content]
        text = " ".join(p for p in parts if p).strip()
        if text:
            return text
    return str(response)


async def ask_specialist(question: str) -> str:
    """Send `question` to the specialist over A2A and return the answer text."""
    credential = DefaultAzureCredential()
    token = credential.get_token(FOUNDRY_SCOPE).token

    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(120.0),
    ) as httpx_client:
        resolver = A2ACardResolver(
            httpx_client=httpx_client,
            base_url=A2A_BASE_URL,
            agent_card_path=AGENT_CARD_PATH,
        )
        agent_card = await resolver.get_agent_card()

        client = await create_client(
            agent=agent_card,
            client_config=ClientConfig(streaming=False, httpx_client=httpx_client),
        )

        request = SendMessageRequest(
            message=new_text_message(question, role=Role.ROLE_USER)
        )

        answers = []
        async for response in client.send_message(request):
            answers.append(_extract_text(response))
        await client.close()

        return "\n".join(a for a in answers if a).strip() or "No answer received."
