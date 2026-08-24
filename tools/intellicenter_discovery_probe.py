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

SENSITIVE_KEYS = {
    "PASSWRD",
    "PASSWORD",
    "EMAIL",
    "PHONE",
    "ADDRESS",
    "NAME",
    "TOKEN",
    "ZIP",
    "CITY",
    "STATE",
    "COUNTRY",
    "LOCX",
    "LOCY",
}

QUERY_CLASSES = {
    "PUMP": [
        "OBJNAM", "OBJTYP", "BODY", "STATIC",
        "LISTORD", "SUBTYP", "HNAME", "SNAME",
        "CIRCUIT", "RPM", "GPM", "PWR", "STATUS",
        "PRIMTIM", "SYSTIM", "ABSMAX", "ABSMIN",
        "MAX", "MIN", "MINF", "MAXF", "SETTMP",
        "SETTMPNC", "PRIMFLO", "COMUART", "PRIOR",
        "VER"
    ],

    "PMPCIRC": [
        "OBJNAM", "OBJTYP", "STATIC", "PARENT",
        "BODY", "CIRCUIT", "SPEED", "SELECT"
    ],

    "CIRCUIT": [
        "OBJNAM", "OBJTYP", "SUBTYP", "STATUS",
        "BODY", "SNAME", "HNAME", "FREEZE",
        "DNTSTP", "TIME", "FEATR", "USAGE",
        "LIMIT", "USE", "SHOMNU", "CHILD"
    ],

    "BODY": [
        "OBJNAM", "OBJTYP", "SUBTYP", "SNAME",
        "LISTORD", "FILTER", "LOTMP", "TEMP",
        "HITMP", "HTSRC", "PRIM", "SEC",
        "ACT1", "ACT2", "ACT3", "ACT4",
        "CIRCUIT", "SPEED", "BOOST", "SELECT",
        "STATUS", "HTMODE", "LSTTMP", "HEATER",
        "VOL", "MANUAL", "HNAME", "MODE"
    ],

    "HEATER": [
        "OBJNAM", "OBJREV", "OBJTYP", "SUBTYP",
        "STATIC", "LISTORD", "PARENT", "BODY",
        "SHARE", "SNAME", "HNAME", "RLY",
        "DLY", "COMUART", "START", "STOP",
        "STATUS", "PERMIT", "SHOMNU", "COOL",
        "ACT", "HTMODE", "TIME", "BOOST",
        "TIMOUT", "READY", "HEATING", "MODE"
    ],

    "SENSE": [
        "OBJNAM", "OBJTYP", "SUBTYP", "SNAME",
        "HNAME", "STATUS", "SOURCE", "PROBE",
        "CALIB", "BODY", "PARENT"
    ],

    "CHEM": [
        "OBJNAM", "OBJTYP", "SUBTYP", "SNAME",
        "HNAME", "LISTORD", "BODY", "STATUS",
        "PHVAL", "ORPVAL", "SINDEX",
        "PHTNK", "ORPTNK", "ALK", "CALC",
        "CYACID", "SALT", "COMUART",
        "PHSET", "ORPSET", "QUALTY",
        "PRIM", "SEC", "SUPER", "TIMOUT"
    ],
}


def get_access_token(cognito, user):
    token = (
        getattr(cognito, "access_token", None)
        or getattr(user, "access_token", None)
        or (getattr(user, "_metadata", {}) or {}).get("access_token")
    )

    if not token:
        raise RuntimeError(
            "Could not obtain Cognito access token"
        )

    return token


def sanitize(params):
    result = {}

    for key, value in (params or {}).items():
        if key.upper() in SENSITIVE_KEYS:
            result[key] = "<redacted>"
        else:
            result[key] = value

    return result


def send_query(ws, objtyp, keys):
    message_id = (
        "DISCOVERY_"
        + objtyp
        + "_"
        + str(uuid.uuid4())
    )

    query = {
        "condition": f"OBJTYP = {objtyp}",
        "objectList": [
            {
                "objnam": "ALL",
                "keys": keys
            }
        ],
        "command": "GETPARAMLIST",
        "messageID": message_id
    }

    ws.send(json.dumps(query))

    deadline = time.time() + 15

    while time.time() < deadline:
        ws.settimeout(
            max(
                1,
                min(5, int(deadline - time.time()))
            )
        )

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

        if msg.get("messageID") != message_id:
            continue

        return msg

    raise TimeoutError(
        f"No matching response for {objtyp}"
    )


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

access_token = get_access_token(
    cognito,
    user
)

print("Authentication successful.")

# ------------------------------------------------------------
# Find IntelliCenter installation
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

print("Installations HTTP:", r.status_code)
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

print(
    "Installation count:",
    len(installations)
)

for i, installation in enumerate(installations):
    print(
        f"[{i}] "
        f"ID={installation.get('InstallationId')} "
        f"Pool={installation.get('PoolName')}"
    )

if len(installations) == 1:
    installation = installations[0]
else:
    index = int(
        input("Choose installation number: ")
    )
    installation = installations[index]

installation_id = installation["InstallationId"]

print(
    "\nUsing InstallationId:",
    installation_id
)

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
# Discover known object classes
# ------------------------------------------------------------

object_cache = {}

for objtyp, keys in QUERY_CLASSES.items():

    print(
        f"\n===== DISCOVERING {objtyp} ====="
    )

    try:
        response = send_query(
            ws,
            objtyp,
            keys
        )
    except Exception as exc:
        print(
            f"{objtyp} query failed:",
            exc
        )
        continue

    print(
        "Response:",
        response.get("response")
    )

    objects = response.get(
        "objectList",
        []
    )

    print(
        "Object count:",
        len(objects)
    )

    for obj in objects:
        objnam = obj.get("objnam")
        params = sanitize(
            obj.get("params", {})
        )

        if objnam:
            object_cache.setdefault(
                objnam,
                {}
            ).update(params)

        print(
            json.dumps(
                {
                    "objnam": objnam,
                    "params": params
                },
                indent=2
            )
        )

# ------------------------------------------------------------
# Listen for live deltas
# ------------------------------------------------------------

print(
    "\n===== LIVE DELTAS ====="
)

print(
    "Listening for 30 seconds..."
)

end_time = time.time() + 30

while time.time() < end_time:

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

    if msg.get("command") != "NotifyList":
        continue

    for obj in msg.get("objectList", []):
        objnam = obj.get("objnam")
        params = sanitize(
            obj.get("params", {})
        )

        if not objnam or not params:
            continue

        # Ignore security-like objects
        if objnam.startswith("U"):
            continue

        old = object_cache.setdefault(
            objnam,
            {}
        )

        changed = {}

        for key, value in params.items():
            if old.get(key) != value:
                changed[key] = value
                old[key] = value

        if changed:
            print(
                f"{objnam}: "
                + ", ".join(
                    f"{k}={v}"
                    for k, v in changed.items()
                )
            )

ws.close()

print(
    "\nDiscovery probe finished."
)
