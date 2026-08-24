#!/usr/bin/env python3

import json
import getpass
import time

import requests
import websocket
from pycognito import Cognito


AWS_USER_POOL_ID = "us-west-2_lbiduhSwD"
AWS_CLIENT_ID = "3de110o697faq7avdchtf07h4v"

INTELLICENTER_API = "https://prod-api.intellicenter.com"
INSTALLATIONS_PATH = "/service/api/installations?pageSize=100&page=1"

SENSITIVE_KEYS = {
    "PASSWRD",
    "PASSWORD",
    "EMAIL",
    "PHONE",
    "ADDRESS",
    "NAME",
    "OWNERNAME",
    "OWNERID",
    "TOKEN",
    "ACCESS-TOKEN",
    "ZIP",
    "CITY",
    "STATE",
    "COUNTRY",
    "LOCX",
    "LOCY",
}

INTERESTING_TYPES = {
    "PUMP",
    "PMPCIRC",
    "CHEM",
    "SENSE",
    "HEATER",
    "BODY",
    "EXTINSTR",
    "SYSTEM",
}


def get_access_token(cognito, user):
    # pycognito versions expose tokens slightly differently.
    candidates = [
        getattr(cognito, "access_token", None),
        getattr(user, "access_token", None),
    ]

    metadata = getattr(user, "_metadata", {}) or {}
    candidates.append(metadata.get("access_token"))

    metadata = getattr(cognito, "_metadata", {}) or {}
    candidates.append(metadata.get("access_token"))

    for token in candidates:
        if token:
            return token

    raise RuntimeError(
        "Could not locate Cognito access token."
    )


def safe_params(params):
    result = {}

    for key, value in params.items():
        if key.upper() in SENSITIVE_KEYS:
            result[key] = "<redacted>"
        else:
            result[key] = value

    return result


username = input("Pentair username: ")
password = getpass.getpass("Pentair password: ")

print("\nAuthenticating...")

cognito = Cognito(
    AWS_USER_POOL_ID,
    AWS_CLIENT_ID,
    username=username
)

cognito.authenticate(password)
user = cognito.get_user()

access_token = get_access_token(cognito, user)

print("Authentication successful.")
print("Access token obtained (not displayed).")


# ------------------------------------------------------------
# IntelliCenter REST discovery
# ------------------------------------------------------------

url = INTELLICENTER_API + INSTALLATIONS_PATH

print("\nQuerying IntelliCenter installations...")

headers = {
    "Authorization": f"Bearer {access_token}",
    "access-token": access_token,
    "Accept": "application/json",
}

r = requests.get(
    url,
    headers=headers,
    timeout=30
)

print("Installations HTTP:", r.status_code)
r.raise_for_status()

data = r.json()

# API appears to return a JSON array.
if isinstance(data, list):
    installations = data
elif isinstance(data, dict):
    installations = (
        data.get("data")
        or data.get("installations")
        or []
    )
else:
    installations = []

print("Installation count:", len(installations))

if not installations:
    raise RuntimeError(
        "No IntelliCenter installations returned."
    )

for i, installation in enumerate(installations):
    print(
        f"[{i}] "
        f"ID={installation.get('InstallationId')} "
        f"Pool={installation.get('PoolName')} "
        f"Online={installation.get('Online')}"
    )

if len(installations) == 1:
    chosen = installations[0]
else:
    choice = int(
        input("Choose installation number: ")
    )
    chosen = installations[choice]

installation_id = chosen.get("InstallationId")

if not installation_id:
    raise RuntimeError(
        "Installation record has no InstallationId."
    )

print(
    "\nUsing InstallationId:",
    installation_id
)


# ------------------------------------------------------------
# IntelliCenter WebSocket
# ------------------------------------------------------------

ws_url = (
    f"wss://prod-api.intellicenter.com/client"
    f"?id={installation_id}"
    f"&access-token={access_token}"
)

print("\nOpening IntelliCenter WebSocket...")
print("(Token is deliberately not displayed.)")

ws = websocket.create_connection(
    ws_url,
    timeout=10
)

print("WebSocket connected.")
print("Passive listening for 60 seconds.")
print("NO commands will be sent.\n")

end_time = time.time() + 60

try:
    while time.time() < end_time:
        remaining = max(1, int(end_time - time.time()))
        ws.settimeout(min(10, remaining))

        try:
            message = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue

        if not message:
            continue

        try:
            payload = json.loads(message)
        except Exception:
            print("Non-JSON message:", repr(message[:200]))
            continue

        command = payload.get("command")

        if command not in (
            "NotifyList",
            "SendParamList",
        ):
            print(
                "Command:",
                command,
                "messageID:",
                payload.get("messageID")
            )
            continue

        objects = payload.get("objectList", [])

        for obj in objects:
            objnam = obj.get("objnam")
            params = obj.get("params", {}) or {}

            objtyp = params.get("OBJTYP")
            subtyp = params.get("SUBTYP")

            # Print recognized equipment or known useful
            # IntelliCenter object naming families.
            interesting = (
                objtyp in INTERESTING_TYPES
                or objnam.startswith("PMP")
                or objnam.startswith("p")
                or objnam.startswith("CH")
                or objnam.startswith("B")
                or objnam.startswith("H")
                or objnam.startswith("CVR")
                or objnam.startswith("SS")
                or objnam.startswith("_A")
            )

            if not interesting:
                continue

            print(
                json.dumps(
                    {
                        "command": command,
                        "objnam": objnam,
                        "OBJTYP": objtyp,
                        "SUBTYP": subtyp,
                        "params": safe_params(params),
                    },
                    indent=2
                )
            )

finally:
    ws.close()

print("\nProbe finished.")
