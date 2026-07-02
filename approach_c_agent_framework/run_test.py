"""
PoC: orchestrator + Office specialist, in-process, with Microsoft Agent Framework.

What this test does:
  1. Acquire the user's Work IQ token out of band (device code) - the one-time
     interactive sign-in, before any conversation.
  2. Connect the Work IQ MCP server natively (MCPStreamableHTTPTool), authorized
     as that user.
  3. Build two agents: the specialist (Work IQ MCP) and the orchestrator, which
     uses the specialist as an in-process tool.
  4. Ask a general question (no tools) and a couple of Office questions.

There is no A2A hop and no interactive OAuth in the turn: the identity flows by
construction because the app holds the user's token and sends it as the MCP
Authorization header.

Prerequisites:
  - `az login` into the Foundry tenant (used for the model calls).
  - A Microsoft 365 Copilot-licensed account for the Work IQ step, or that call
    comes back Forbidden.
"""

import asyncio
import logging
import os

import httpx
from agent_framework import MCPStreamableHTTPTool
from agent_framework.foundry import FoundryChatClient
from azure.identity import AzureCliCredential
from dotenv import load_dotenv

from agents import build_orchestrator, build_specialist
from auth import acquire_workiq_token

# Load the shared repo .env (one level up).
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("qubix.maf")

QUESTIONS = [
    "In one short sentence, what can you help me with?",  # general, no tool
    "What's on my calendar today?",                       # office -> specialist -> Work IQ
    "Do I have any unread emails from my manager?",
]


async def main() -> None:
    # 1) One-time interactive sign-in, out of band (before the conversation).
    token = acquire_workiq_token()

    client = FoundryChatClient(
        project_endpoint=os.environ["PROJECT_ENDPOINT"],
        model=os.environ["MODEL_DEPLOYMENT_NAME"],
        credential=AzureCliCredential(),
    )

    # 2) Connect the Work IQ MCP server natively, authorized as the user.
    #    The token goes on the httpx client so it is sent on every request,
    #    including the MCP initialize handshake (not just tool calls).
    workiq_url = os.environ["WORKIQ_MCP_SERVER_URL"]
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, follow_redirects=True
    ) as http_client:
        workiq = MCPStreamableHTTPTool(
            name="workiq",
            url=workiq_url,
            http_client=http_client,
            load_prompts=False,
            approval_mode="never_require",
        )
        async with workiq:
            logger.info("Work IQ tools: %s", [f.name for f in workiq.functions])

            # 3) Build the agents and run the conversation.
            specialist = build_specialist(client, workiq)
            orchestrator = build_orchestrator(client, specialist)
            for question in QUESTIONS:
                print(f"\nUser: {question}")
                result = await orchestrator.run(question)
                print(f"Assistant: {result.text}")


if __name__ == "__main__":
    asyncio.run(main())
