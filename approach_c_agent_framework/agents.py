"""
Two in-process agents built with Microsoft Agent Framework (FoundryChatClient).

The specialist connects to the Work IQ MCP server natively with
``MCPStreamableHTTPTool``. The orchestrator uses the specialist as an in-process
tool. Everything runs in one process, so there is no A2A hop and no interactive
OAuth in the turn: the user's Work IQ token is acquired out of band and sent as
the MCP Authorization header.
"""

from agent_framework import Agent, MCPStreamableHTTPTool


def build_specialist(client, workiq: MCPStreamableHTTPTool) -> Agent:
    """The Office specialist: a single agent whose tools are the Work IQ MCP."""
    return Agent(
        client=client,
        name="office-specialist",
        instructions=(
            "You are a Microsoft 365 / Office specialist. Use the Work IQ tools to "
            "answer questions about Outlook mail, calendar, Teams, and files. Never "
            "invent data; rely on the tools."
        ),
        tools=[workiq],
    )


def build_orchestrator(client, specialist: Agent) -> Agent:
    """The voice orchestrator: it uses the specialist as an in-process tool."""
    office_tool = specialist.as_tool(
        name="office_specialist",
        description=(
            "Delegate Microsoft 365 / Office questions (mail, calendar, Teams, files) "
            "to the Office specialist agent."
        ),
        arg_name="question",
        arg_description="The Office / Microsoft 365 question to answer.",
    )
    return Agent(
        client=client,
        name="orchestrator",
        instructions=(
            "You are a voice orchestrator. Keep answers short and conversational. For "
            "Microsoft 365 / Office questions, call the office_specialist tool and read "
            "back its answer. Answer anything else directly."
        ),
        tools=[office_tool],
    )
