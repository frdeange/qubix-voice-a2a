# Approach A — agent mode

The orchestrator is a Foundry **prompt agent** (`gpt-5`) with a native **A2A tool**
that delegates to the Office specialist. Voice Live wraps the agent to provide the
voice experience.

## Files

- [`create_orchestrator_agent.py`](create_orchestrator_agent.py) — creates the A2A
  connection to the specialist and the orchestrator agent (with the Voice Live
  session settings stored in its metadata). Run in the devcontainer.
- [`voice_orchestrator.py`](voice_orchestrator.py) — the Voice Live client
  (agent mode). Run on a host with a microphone and speakers.

## Run

```bash
# Devcontainer: create the orchestrator (run setup/create_specialist_agent.py first)
python create_orchestrator_agent.py

# Windows host: talk to it
pip install -r requirements.txt
python voice_orchestrator.py
```

Requires `az login`. Agent mode uses Entra ID auth (no API keys).
