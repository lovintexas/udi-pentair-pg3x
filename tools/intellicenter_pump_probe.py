#!/usr/bin/env python3

import json
import getpass
import uuid

import requests
import websocket
from pycognito import Cognito

AWS_USER_POOL_ID = "us-west-2_lbiduhSwD"
AWS_CLIENT_ID = "3de110o697faq7avdchtf07h4v"

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

access_token = (
    getattr(cognito, "access_token", None)
    or getattr(user, "access_token", None)
    or (getattr(user, "_metadata", {}) or {}).get("access_token")
)

if not access_token:
    raise RuntimeError("Could not obtain access token")

print("Authentication successful.")

# ------------------------------------------------------------
# Find IntelliCenter installation
# ------------------------------------------------------------

url = (
    "https://prod-api.intellicenter.com"
    "/service/api/installations?pageSize=100&page=1"
)

headers = {
    "Authorization": f"Bearer {access_token}",
    "access-token": access_token,
    "Accept": "application/json",
}

r = requests.get(url, headers=headers, timeout=30)
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
    raise RuntimeError("No IntelliCenter installations found")

installation = installations[0]
installation_id = installation["InstallationId"]

print("Installation ID:", installation_id)
print("Pool:", installation.get("PoolName"))

# ------------------------------------------------------------
# Connect WebSocket
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
# Exact pump GETPARAMLIST used by IntelliCenter2
# ------------------------------------------------------------

message_id = "QUERY_GET_PUMPS" + str(uuid.uuid4())

query = {
    "condition": "OBJTYP = PUMP",
    "objectList": [
        {
            "objnam": "ALL",
            "keys": [
                "OBJNAM",
                "OBJTYP",
                "BODY",
                "SHARE,",
                "STATIC",
                "LISTORD",
                "SUBTYP",
                "HNAME",
                "SNAME",
                "CIRCUIT",
                "RPM",
                "GPM",
                "PWR",
                "STATUS",
                "PRIMTIM",
                "SYSTIM",
                "ABSMAX",
                "ABSMIN",
                "MAX",
                "MIN",
                "MINF",
                "MAXF",
                "SETTMP",
                "SETTMPNC",
                "PRIMFLO",
                "COMUART",
                "PRIOR",
                "VER"
            ]
        }
    ],
    "command": "GETPARAMLIST",
    "messageID": message_id
}

print("\nSending read-only pump query...")
ws.send(json.dumps(query))

# ------------------------------------------------------------
# Wait specifically for our response
# ------------------------------------------------------------

while True:
    raw = ws.recv()

    if not raw:
        continue

    try:
        msg = json.loads(raw)
    except Exception:
        continue

    # Ignore unrelated live NotifyList traffic.
    if msg.get("messageID") != message_id:
        continue

    print("\nReceived matching response:")
    print(json.dumps(msg, indent=2))
    break

ws.close()

print("\nPump probe finished.")
