#!/usr/bin/env python3

import json
import getpass
import time
import uuid

import requests
import websocket
from pycognito import Cognito


AWS_USER_POOL_ID = "us-west-2_lbiduhSwD"
AWS_CLIENT_ID = "3de110o697faq7avdchtf07h4v"

API_BASE = "https://prod-api.intellicenter.com"
INSTALLATIONS_URL = (
    API_BASE
    + "/service/api/installations?pageSize=100&page=1"
)


def get_token(cognito, user):
    token = (
        getattr(cognito, "access_token", None)
        or getattr(user, "access_token", None)
        or (getattr(user, "_metadata", {}) or {}).get("access_token")
    )

    if not token:
        raise RuntimeError("Could not obtain access token")

    return token


def wait_for_message(ws, message_id, timeout=15):
    end = time.time() + timeout

    while time.time() < end:
        ws.settimeout(min(5, max(1, end - time.time())))

        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue

        if not raw:
            continue

        try:
            msg = json.loads(raw)
        except Exception:
            continue

        if msg.get("messageID") == message_id:
            return msg

    return None


def query_body(ws, objnam):
    message_id = "QUERY_BODY_" + str(uuid.uuid4())

    query = {
        "condition": "",
        "objectList": [
            {
                "objnam": objnam,
                "keys": [
                    "OBJTYP",
                    "SUBTYP",
                    "SNAME",
                    "STATUS",
                    "TEMP",
                    "FILTER"
                ]
            }
        ],
        "command": "REQUESTPARAMLIST",
        "messageID": message_id
    }

    ws.send(json.dumps(query))

    return wait_for_message(
        ws,
        message_id
    )


def set_body(ws, objnam, status):
    message_id = (
        "QUERY_GET_POOL_SPA"
        + str(uuid.uuid4())
    )

    command = {
        "command": "SetParamList",
        "objectList": [
            {
                "objnam": objnam,
                "params": {
                    "STATUS": status
                }
            }
        ],
        "messageID": message_id
    }

    print("\nSending:")
    print(json.dumps(command, indent=2))

    ws.send(json.dumps(command))

    end = time.time() + 20

    while time.time() < end:
        ws.settimeout(5)

        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue

        if not raw:
            continue

        try:
            msg = json.loads(raw)
        except Exception:
            continue

        # Print direct response to our command, if any.
        if msg.get("messageID") == message_id:
            print("\nDirect response:")
            print(json.dumps(msg, indent=2))

        # Look for actual state confirmation.
        if msg.get("command") == "NotifyList":
            for obj in msg.get("objectList", []):
                if obj.get("objnam") != objnam:
                    continue

                params = obj.get("params", {})

                if "STATUS" in params:
                    actual = params["STATUS"]

                    print(
                        f"\nNotifyList confirmation: "
                        f"{objnam} STATUS={actual}"
                    )

                    if actual == status:
                        return True

    return False


# ------------------------------------------------------------
# Authenticate
# ------------------------------------------------------------

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

access_token = get_token(cognito, user)

print("Authentication successful.")


# ------------------------------------------------------------
# Find installation
# ------------------------------------------------------------

headers = {
    "Authorization": f"Bearer {access_token}",
    "access-token": access_token,
    "Accept": "application/json",
}

r = requests.get(
    INSTALLATIONS_URL,
    headers=headers,
    timeout=30
)

r.raise_for_status()
data = r.json()

if isinstance(data, list):
    installations = data
else:
    installations = (
        data.get("data")
        or data.get("installations")
        or []
    )

if not installations:
    raise RuntimeError(
        "No IntelliCenter installations found"
    )

installation = installations[0]
installation_id = installation["InstallationId"]

print(
    "Installation:",
    installation_id,
    installation.get("PoolName")
)


# ------------------------------------------------------------
# Connect
# ------------------------------------------------------------

ws_url = (
    "wss://prod-api.intellicenter.com/client"
    f"?id={installation_id}"
    f"&access-token={access_token}"
)

print("\nConnecting WebSocket...")

ws = websocket.create_connection(
    ws_url,
    timeout=15
)

print("Connected.")


# ------------------------------------------------------------
# Query pool body
# ------------------------------------------------------------

BODY = "B1101"

print(
    f"\nQuerying {BODY}..."
)

response = query_body(
    ws,
    BODY
)

if response:
    print(json.dumps(response, indent=2))
else:
    print(
        "No direct query response received."
    )


# ------------------------------------------------------------
# User chooses action
# ------------------------------------------------------------

print(
    "\nControl test for B1101 (Pool)"
)

choice = input(
    "Enter OFF, ON, or Q to quit: "
).strip().upper()

if choice == "Q":
    ws.close()
    print("No command sent.")
    raise SystemExit

if choice not in ("ON", "OFF"):
    ws.close()
    raise RuntimeError(
        "Choice must be ON, OFF, or Q"
    )

print(
    f"\nAbout to set B1101 STATUS={choice}"
)

confirm = input(
    "Type YES to send command: "
).strip().upper()

if confirm != "YES":
    ws.close()
    print("Command cancelled.")
    raise SystemExit


# ------------------------------------------------------------
# Send control
# ------------------------------------------------------------

success = set_body(
    ws,
    BODY,
    choice
)

if success:
    print(
        f"\nSUCCESS: IntelliCenter confirmed "
        f"B1101 STATUS={choice}"
    )
else:
    print(
        "\nNo matching NotifyList confirmation "
        "was received within 20 seconds."
    )


# ------------------------------------------------------------
# Verify with explicit read
# ------------------------------------------------------------

print(
    "\nReading B1101 back..."
)

response = query_body(
    ws,
    BODY
)

if response:
    print(json.dumps(response, indent=2))
else:
    print(
        "No readback response received."
    )

ws.close()

print("\nControl probe finished.")
