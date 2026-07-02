# Approach B — model mode

The orchestrator talks to `gpt-realtime-1.5` **directly** for the lowest-latency
voice experience. Model mode has no server-side agent tools, so the specialist is
reached through a Voice Live **function tool** (`ask_office_specialist`) whose
handler calls the specialist over **A2A**.

## Files

- [`voice_orchestrator.py`](voice_orchestrator.py) — the Voice Live client
  (model mode) with the specialist function tool. Run on a host with a
  microphone and speakers.
- [`specialist_a2a_client.py`](specialist_a2a_client.py) — helper that sends a
  question to the specialist's A2A endpoint and returns the answer.

There is no orchestrator agent to create for this approach: only the shared
specialist (`setup/create_specialist_agent.py`) is needed.

## Run

```bash
# Windows host
pip install -r requirements.txt
python voice_orchestrator.py
```

Requires `az login`. Lower latency than Approach A, but tool calls are handled
client-side rather than by a Foundry agent.
