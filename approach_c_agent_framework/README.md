# Approach C — Microsoft Agent Framework (in-process)

This is the test behind the recommendation in the email: **orchestrate the two
agents in-process with the Microsoft Agent Framework (MAF)** instead of an A2A
hop, so the user's identity flows all the way to Work IQ with no interactive
OAuth in the turn.

Uses **MAF Python 1.10.0** and `FoundryChatClient`.

## What it builds

```
orchestrator ──(office_specialist as tool)──▶ office-specialist ──(Work IQ MCP)──▶ M365
```

- `orchestrator` — the voice orchestrator. It keeps control of the turn and uses
  the specialist as an in-process tool (`specialist.as_tool(...)`).
- `office-specialist` — a single agent that talks to Work IQ through MAF's native
  MCP client (`MCPStreamableHTTPTool`).
- The **signed-in user's token** is acquired out of band and sent as the MCP
  `Authorization` header, so Work IQ acts on behalf of that user.

Because both agents run in the same process, there is no HTTP A2A hop between
them and no per-turn context passing over the wire. The Work IQ token is
acquired once, interactively, **before** the conversation, so nothing in the
turn needs an interactive OAuth. The token is put on the `httpx` client so it is
sent on every request, including the MCP `initialize` handshake.

## Files

| File | Purpose |
| --- | --- |
| `agents.py` | Builds the orchestrator + specialist (Work IQ MCP as a tool). |
| `auth.py` | Device-code sign-in to get the user's delegated Work IQ token. |
| `run_test.py` | Runs the end-to-end test. |

## Run

```bash
pip install -r approach_c_agent_framework/requirements.txt
az login          # into the Foundry tenant (used for the model calls)
python approach_c_agent_framework/run_test.py
```

The script:

1. Prompts a device-code sign-in to get the Work IQ token (out of band).
2. Connects the Work IQ MCP server natively, authorized as that user.
3. Asks a general question (no tools) and then Office questions, so the
   orchestrator delegates to the specialist, which calls Work IQ as the user.

Environment values come from the shared repo `.env` (`PROJECT_ENDPOINT`,
`MODEL_DEPLOYMENT_NAME`, `TENANT_ID`, `CLIENT_ID`, `WORKIQ_SCOPE`).

## What to expect

- The MAF orchestration (orchestrator → specialist in-process) works with no A2A
  and no interactive OAuth in the turn.
- The Work IQ call returns data **only** for a Microsoft 365 Copilot-licensed
  account. Without a license it returns `Forbidden`. On a non-managed device,
  Conditional Access token protection can also block the sign-in.
