"""
Acquire the signed-in user's delegated Work IQ token out of band (device code).

This mirrors what the web app does in the browser with MSAL: the user signs in
once, interactively, outside the voice turn. The resulting token is then handed
to the agents so they can call Work IQ on the user's behalf with no interactive
OAuth during the conversation.
"""

import os

import msal


def acquire_workiq_token() -> str:
    """Run a device-code flow and return a delegated Work IQ access token."""
    tenant = os.environ["TENANT_ID"]
    client_id = os.environ["CLIENT_ID"]
    scope = os.environ.get(
        "WORKIQ_SCOPE", "fdcc1f02-fc51-4226-8753-f668596af7f7/WorkIQAgent.Ask"
    )

    app = msal.PublicClientApplication(
        client_id, authority=f"https://login.microsoftonline.com/{tenant}"
    )
    flow = app.initiate_device_flow(scopes=[scope])
    if "user_code" not in flow:
        raise RuntimeError(
            f"Failed to start device flow: {flow.get('error_description', flow)}"
        )

    # Prompt the user to sign in (this is the one-time interactive step).
    print(flow["message"], flush=True)

    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(
            f"Token acquisition failed: {result.get('error_description', result)}"
        )
    return result["access_token"]
