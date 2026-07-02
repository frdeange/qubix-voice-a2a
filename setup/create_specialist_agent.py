"""
Create the Office specialist agent and expose it as an A2A endpoint.

What this script does:
1. Creates a Foundry prompt agent connected to the Work IQ MCP server.
2. Enables incoming A2A on that agent (agent card + A2A protocol) so the
   orchestrator can call it.

Run this inside the devcontainer:  python setup/create_specialist_agent.py

Prerequisites:
- `az login` with an account that has the "Foundry User" role on the project.
- A Work IQ connection already created in the Foundry portal (the OAuth flow is
  handled there); its name goes in WORKIQ_MCP_CONNECTION_NAME.
"""

import os
import sys

import requests
from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition, MCPTool

load_dotenv()

# Foundry token scope used for both the SDK and the REST call.
FOUNDRY_SCOPE = "https://ai.azure.com/.default"

PROJECT_ENDPOINT = os.environ["PROJECT_ENDPOINT"]
SPECIALIST_AGENT_NAME = os.environ["SPECIALIST_AGENT_NAME"]
MODEL_DEPLOYMENT_NAME = os.environ["MODEL_DEPLOYMENT_NAME"]

WORKIQ_MCP_SERVER_URL = os.environ.get("WORKIQ_MCP_SERVER_URL", "")
WORKIQ_MCP_SERVER_LABEL = os.environ.get("WORKIQ_MCP_SERVER_LABEL", "workiq")
WORKIQ_MCP_CONNECTION_NAME = os.environ.get("WORKIQ_MCP_CONNECTION_NAME", "")

# Instructions kept short and voice-friendly: the orchestrator asks this agent
# focused Microsoft Office / Microsoft 365 questions through A2A.
SPECIALIST_INSTRUCTIONS = (
    "You are a Microsoft Office and Microsoft 365 specialist. "
    "Answer questions about Outlook, Teams, Word, Excel, SharePoint and "
    "OneDrive. Use the Work IQ MCP tools to look up real data when needed. "
    "Keep answers short and clear because they will be read out loud."
)


def build_mcp_tool() -> MCPTool:
    """Build the Work IQ MCP tool from the environment configuration."""
    if not WORKIQ_MCP_SERVER_URL:
        sys.exit(
            "WORKIQ_MCP_SERVER_URL is not set. Add the Work IQ MCP server URL "
            "to your .env file before running this script."
        )

    # require_approval="never" keeps the voice flow smooth for the PoC.
    # In production, require approval for tools that write or change data.
    tool = MCPTool(
        server_label=WORKIQ_MCP_SERVER_LABEL,
        server_url=WORKIQ_MCP_SERVER_URL,
        require_approval="never",
    )
    if WORKIQ_MCP_CONNECTION_NAME:
        tool.project_connection_id = WORKIQ_MCP_CONNECTION_NAME
    return tool


def create_specialist_agent(project: AIProjectClient) -> None:
    """Create (or add a new version of) the specialist agent."""
    agent = project.agents.create_version(
        agent_name=SPECIALIST_AGENT_NAME,
        definition=PromptAgentDefinition(
            model=MODEL_DEPLOYMENT_NAME,
            instructions=SPECIALIST_INSTRUCTIONS,
            tools=[build_mcp_tool()],
        ),
    )
    print(f"Specialist agent created: {agent.name} (version {agent.version})")


def enable_incoming_a2a(credential: DefaultAzureCredential) -> None:
    """Enable incoming A2A on the specialist via the Foundry REST API.

    Setting the agent card is only supported through REST today, so we send a
    single PATCH that sets both the agent card and the enabled protocols.
    """
    token = credential.get_token(FOUNDRY_SCOPE).token
    url = f"{PROJECT_ENDPOINT}/agents/{SPECIALIST_AGENT_NAME}?api-version=v1"
    body = {
        "agent_card": {
            "description": "Microsoft Office and Microsoft 365 specialist.",
            "version": "1.0",
            "skills": [
                {
                    "id": "office-qa",
                    "name": "Office Q&A",
                    "description": (
                        "Answers Microsoft Office / Microsoft 365 questions "
                        "using Work IQ."
                    ),
                }
            ],
        },
        "agent_endpoint": {"protocols": ["responses", "a2a"]},
    }

    response = requests.patch(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=30,
    )
    response.raise_for_status()
    print("Incoming A2A enabled on the specialist agent.")

    a2a_base = (
        f"{PROJECT_ENDPOINT}/agents/{SPECIALIST_AGENT_NAME}/endpoint/protocols/a2a"
    )
    print(f"A2A base URL:      {a2a_base}")
    print(f"Agent card (v1.0): {a2a_base}/agentCard/v1.0")


def main() -> None:
    credential = DefaultAzureCredential()
    project = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=credential)

    create_specialist_agent(project)
    enable_incoming_a2a(credential)


if __name__ == "__main__":
    main()
