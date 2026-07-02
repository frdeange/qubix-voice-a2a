"""
Approach A (agent mode): create the orchestrator agent.

What this script does:
1. Creates a RemoteA2A connection that points to the specialist's A2A endpoint.
2. Creates a Foundry prompt agent (the orchestrator) that uses the A2A tool to
   delegate to the specialist, and stores the Voice Live session settings in the
   agent metadata so Voice Live applies them automatically.

Run this AFTER setup/create_specialist_agent.py:
    python approach_a_agent_mode/create_orchestrator_agent.py

Prerequisites:
- `az login` with the "Foundry User" role on the project.
- SUBSCRIPTION_ID, RESOURCE_GROUP, FOUNDRY_ACCOUNT set in .env (needed to create
  the connection through the management plane).
"""

import json
import os

import requests
from dotenv import load_dotenv
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition, A2APreviewTool

load_dotenv()

FOUNDRY_SCOPE = "https://ai.azure.com/.default"
MANAGEMENT_SCOPE = "https://management.azure.com/.default"

PROJECT_ENDPOINT = os.environ["PROJECT_ENDPOINT"]
PROJECT_NAME = os.environ["PROJECT_NAME"]
ORCHESTRATOR_AGENT_NAME = os.environ["ORCHESTRATOR_AGENT_NAME"]
SPECIALIST_AGENT_NAME = os.environ["SPECIALIST_AGENT_NAME"]
MODEL_DEPLOYMENT_NAME = os.environ["MODEL_DEPLOYMENT_NAME"]
VOICE_NAME = os.environ.get("VOICE_NAME", "en-US-Ava:DragonHDLatestNeural")

SUBSCRIPTION_ID = os.environ["SUBSCRIPTION_ID"]
RESOURCE_GROUP = os.environ["RESOURCE_GROUP"]
FOUNDRY_ACCOUNT = os.environ["FOUNDRY_ACCOUNT"]
A2A_CONNECTION_NAME = os.environ["A2A_CONNECTION_NAME"]

# The orchestrator is voice-first. It handles small talk itself and delegates
# Microsoft Office / Microsoft 365 questions to the specialist via A2A.
ORCHESTRATOR_INSTRUCTIONS = (
    "You are a friendly voice orchestrator. Detect the language the user speaks "
    "and always reply in that same language. Answer general questions yourself. "
    "For anything about Microsoft Office or Microsoft 365 (Outlook, Teams, "
    "Word, Excel, SharePoint, OneDrive), use the A2A tool to ask the Office "
    "specialist and relay the answer. Keep replies short and conversational "
    "because they are read out loud."
)

# Voice Live session settings stored on the agent so the service applies them.
VOICE_LIVE_CONFIG = {
    "session": {
        "voice": {
            "name": VOICE_NAME,
            "type": "azure-standard",
            "temperature": 0.8,
        },
        "input_audio_transcription": {"model": "azure-speech"},
        "turn_detection": {
            "type": "azure_semantic_vad",
            # Multilingual end-of-utterance detection (auto language detection).
            "end_of_utterance_detection": {
                "model": "semantic_detection_v1_multilingual"
            },
            # Keep the transcript aligned with what the user actually heard
            # when they interrupt the agent (barge-in).
            "auto_truncate": True,
        },
        "input_audio_noise_reduction": {"type": "azure_deep_noise_suppression"},
        "input_audio_echo_cancellation": {"type": "server_echo_cancellation"},
    }
}


def chunk_config(config_json: str, limit: int = 512) -> dict:
    """Split the Voice Live config into <=512 char metadata entries."""
    metadata = {"microsoft.voice-live.configuration": config_json[:limit]}
    remaining = config_json[limit:]
    chunk_num = 1
    while remaining:
        metadata[f"microsoft.voice-live.configuration.{chunk_num}"] = remaining[:limit]
        remaining = remaining[limit:]
        chunk_num += 1
    return metadata


def create_a2a_connection(credential: DefaultAzureCredential) -> None:
    """Create the RemoteA2A connection to the specialist (management plane)."""
    token = credential.get_token(MANAGEMENT_SCOPE).token
    target_a2a_url = (
        f"{PROJECT_ENDPOINT}/agents/{SPECIALIST_AGENT_NAME}/endpoint/protocols/a2a"
    )
    url = (
        f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
        f"/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.CognitiveServices/accounts/{FOUNDRY_ACCOUNT}"
        f"/projects/{PROJECT_NAME}/connections/{A2A_CONNECTION_NAME}"
        f"?api-version=2025-04-01-preview"
    )
    body = {
        "properties": {
            # Agent identity token: the calling agent authenticates with its own
            # platform-assigned identity when reaching the specialist over A2A.
            "authType": "AgenticIdentityToken",
            "category": "RemoteA2A",
            "target": target_a2a_url,
            "audience": "https://ai.azure.com",
            "Credentials": {},
            # Relative card path (NO leading slash) so it resolves under the
            # target's project/agent path, not the host root. A leading slash
            # makes it resolve to the host root and fails the same-agent check.
            "metadata": {"AgentCardPath": "agentCard/v1.0"},
        }
    }

    response = requests.put(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=30,
    )
    response.raise_for_status()
    print(f"A2A connection created: {A2A_CONNECTION_NAME} -> {target_a2a_url}")


def create_orchestrator_agent(project: AIProjectClient) -> None:
    """Create the orchestrator agent with the A2A tool and voice config."""
    a2a_connection = project.connections.get(A2A_CONNECTION_NAME)
    tool = A2APreviewTool(project_connection_id=a2a_connection.id)

    agent = project.agents.create_version(
        agent_name=ORCHESTRATOR_AGENT_NAME,
        definition=PromptAgentDefinition(
            model=MODEL_DEPLOYMENT_NAME,
            instructions=ORCHESTRATOR_INSTRUCTIONS,
            tools=[tool],
        ),
        metadata=chunk_config(json.dumps(VOICE_LIVE_CONFIG)),
    )
    print(f"Orchestrator agent created: {agent.name} (version {agent.version})")


def connection_exists(project: AIProjectClient) -> bool:
    """Return True if the A2A connection already exists (data-plane check)."""
    try:
        project.connections.get(A2A_CONNECTION_NAME)
        return True
    except ResourceNotFoundError:
        return False


def main() -> None:
    credential = DefaultAzureCredential()
    project = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=credential)

    # Creating the connection uses the management plane (ARM), which may require
    # MFA. If the connection already exists (e.g. created from Cloud Shell), we
    # skip that step and just create the agent.
    if connection_exists(project):
        print(f"A2A connection '{A2A_CONNECTION_NAME}' already exists; skipping creation.")
    else:
        create_a2a_connection(credential)

    create_orchestrator_agent(project)


if __name__ == "__main__":
    main()
