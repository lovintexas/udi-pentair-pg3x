#!/usr/bin/env python3

import sys
import os
import hashlib
import threading
import json
import uuid

import boto3
import markdown2
import requests
import udi_interface
from pycognito import Cognito
from requests_aws4auth import AWS4Auth
import websocket


LOGGER = udi_interface.LOGGER
VERSION = "1.1.0-beta1"

polyglot = udi_interface.Interface([])
controller = None
poll_lock = threading.Lock()


AWS_REGION = "us-west-2"
AWS_USER_POOL_ID = "us-west-2_lbiduhSwD"
AWS_CLIENT_ID = "3de110o697faq7avdchtf07h4v"
AWS_IDENTITY_POOL_ID = "us-west-2:6f950f85-af44-43d9-b690-a431f753e9aa"
AWS_COGNITO_ENDPOINT = "cognito-idp.us-west-2.amazonaws.com"

PENTAIR_ENDPOINT = "https://api.pentair.cloud"
PENTAIR_DEVICES_PATH = "/device/device-service/user/devices"
PENTAIR_DEVICE2_PATH = "/device2/device2-service/user/device"
PENTAIR_DEVICE_PATH = "/device/device-service/user/device/"


def make_address(prefix, value):
    digest = hashlib.md5(value.encode()).hexdigest()[:10]
    return prefix + digest


def field_value(fields, key):
    try:
        value = fields[key]
        if isinstance(value, dict):
            return value.get("value")
        return value
    except (KeyError, TypeError):
        return None


class PentairCloudClient:
    def __init__(self):
        self.username = None
        self.password = None
        self.cognito = None
        self.id_token = None
        self.aws_auth = None
        self.headers = None

    def authenticate(self, username, password):
        LOGGER.info("Authenticating with Pentair Cloud")

        self.username = username
        self.password = password

        cognito = Cognito(
            AWS_USER_POOL_ID,
            AWS_CLIENT_ID,
            username=username
        )

        cognito.authenticate(password)
        cognito.get_user()

        self.cognito = cognito
        self.id_token = cognito.get_user()._metadata["id_token"]

        identity = boto3.client(
            "cognito-identity",
            region_name=AWS_REGION
        )

        response = identity.get_id(
            IdentityPoolId=AWS_IDENTITY_POOL_ID,
            Logins={
                f"{AWS_COGNITO_ENDPOINT}/{AWS_USER_POOL_ID}":
                    self.id_token
            }
        )

        response = identity.get_credentials_for_identity(
            IdentityId=response["IdentityId"],
            Logins={
                f"{AWS_COGNITO_ENDPOINT}/{AWS_USER_POOL_ID}":
                    self.id_token
            }
        )

        creds = response["Credentials"]

        self.aws_auth = AWS4Auth(
            creds["AccessKeyId"],
            creds["SecretKey"],
            AWS_REGION,
            "execute-api",
            session_token=creds["SessionToken"]
        )

        self.headers = {
            "x-amz-id-token": self.id_token,
            "user-agent": "aws-amplify/4.3.10 react-native",
            "content-type": "application/json; charset=UTF-8"
        }

        LOGGER.info("Pentair Cloud authentication successful")

    def _request(self, method, url, **kwargs):
        """
        Make an authenticated Pentair Cloud request.

        Pentair's Cognito/AWS credentials expire periodically.  If the API
        returns 401 or 403, authenticate again and retry the request once.
        """
        for attempt in range(2):
            response = requests.request(
                method,
                url,
                auth=self.aws_auth,
                headers=self.headers,
                timeout=30,
                **kwargs
            )

            if (
                response.status_code in (401, 403)
                and attempt == 0
            ):
                LOGGER.warning(
                    "Pentair Cloud authentication expired "
                    f"(HTTP {response.status_code}); "
                    "re-authenticating"
                )

                if not self.username or not self.password:
                    response.raise_for_status()

                self.authenticate(
                    self.username,
                    self.password
                )

                continue

            response.raise_for_status()
            return response

        raise RuntimeError(
            "Pentair Cloud request failed after re-authentication"
        )

    def list_devices(self):
        response = self._request(
            "GET",
            PENTAIR_ENDPOINT + PENTAIR_DEVICES_PATH
        )

        return response.json().get("data", [])

    def put_device_payload(self, device_id, payload):
        response = self._request(
            "PUT",
            PENTAIR_ENDPOINT + PENTAIR_DEVICE_PATH + device_id,
            json={"payload": payload}
        )

        response.raise_for_status()

        data = response.json()

        try:
            code = data["data"]["code"]
        except Exception:
            code = None

        if code != "set_device_success":
            raise RuntimeError(
                f"Pentair command failed: {data}"
            )

        return data

    def start_program(self, device_id, program_id):
        LOGGER.info(
            f"Starting Pentair program {program_id}"
        )

        self.put_device_payload(
            device_id,
            {f"zp{program_id}e10": "3"}
        )

        # Pentair app also updates Last Active Program.
        self.put_device_payload(
            device_id,
            {"p2": "99"}
        )

    def stop_program(self, device_id, program_id):
        LOGGER.info(
            f"Stopping Pentair program {program_id}"
        )

        self.put_device_payload(
            device_id,
            {f"zp{program_id}e10": "2"}
        )

        self.put_device_payload(
            device_id,
            {"p2": str(program_id - 1)}
        )

    def set_colorsync_power(self, device_id, on):
        LOGGER.info(
            f"Setting Color Sync power to "
            f"{'On' if on else 'Off'}"
        )

        self.put_device_payload(
            device_id,
            {"d13": "1" if on else "0"}
        )

    def set_colorsync_mode(self, device_id, mode):
        LOGGER.info(
            f"Setting Color Sync mode to {mode}"
        )

        self.put_device_payload(
            device_id,
            {"d1": str(mode)}
        )

    def get_device_status(self, device_ids):
        if not device_ids:
            return []

        response = self._request(
            "POST",
            PENTAIR_ENDPOINT + PENTAIR_DEVICE2_PATH,
            json={"deviceIds": device_ids}
        )

        response.raise_for_status()

        return (
            response.json()
            .get("response", {})
            .get("data", [])
        )

    def get_pump_status(self, device_ids):
        return self.get_device_status(device_ids)


class PumpProgramNode(udi_interface.Node):
    id = "pentairprogram"

    drivers = [
        {"driver": "ST", "value": 0, "uom": 25},
    ]

    def __init__(
        self,
        polyglot,
        primary,
        address,
        name,
        program_id,
        pump_device_id
    ):
        super().__init__(
            polyglot,
            primary,
            address,
            name
        )

        self.program_id = program_id
        self.pump_device_id = pump_device_id
        self.running = False

    def set_running(self, running):
        self.running = bool(running)

        self.setDriver(
            "ST",
            1 if self.running else 0
        )

    def start_program(self, command=None):
        if controller is None:
            return

        try:
            controller.start_program(
                self.pump_device_id,
                self.program_id
            )
        except Exception as err:
            LOGGER.error(
                f"Error starting {self.name}: {err}"
            )

    def stop_program(self, command=None):
        if controller is None:
            return

        try:
            controller.stop_program(
                self.pump_device_id,
                self.program_id
            )
        except Exception as err:
            LOGGER.error(
                f"Error stopping {self.name}: {err}"
            )

    def query(self, command=None):
        if controller is not None:
            controller.query()

    commands = {
        "START": start_program,
        "STOP": stop_program,
        "QUERY": query,
    }


class PumpNode(udi_interface.Node):
    id = "pentairpump"

    drivers = [
        {"driver": "ST",  "value": 0, "uom": 25},
        {"driver": "GV1", "value": 0, "uom": 73},
        {"driver": "GV2", "value": 0, "uom": 51},
        {"driver": "GV3", "value": 0, "uom": 56},
        {"driver": "GV4", "value": 0, "uom": 56},
        {"driver": "GV5", "value": 0, "uom": 56},
        {"driver": "GV6", "value": 0, "uom": 56},
    ]

    def __init__(
        self,
        polyglot,
        primary,
        address,
        name,
        device_id
    ):
        super().__init__(
            polyglot,
            primary,
            address,
            name
        )

        self.device_id = device_id
        self.program_nodes = {}

    def update_from_response(self, device_response):
        fields = device_response.get("fields", {})

        online = device_response.get("online")
        if online is not None:
            self.setDriver(
                "ST",
                1 if online else 0
            )

        power = field_value(fields, "s18")
        if power is not None:
            self.setDriver(
                "GV1",
                int(float(power))
            )

        speed = field_value(fields, "s19")
        if speed is not None:
            self.setDriver(
                "GV2",
                float(speed) / 10
            )

        flow = field_value(fields, "s26")
        if flow is not None:
            self.setDriver(
                "GV3",
                float(flow) / 10
            )

        pressure = field_value(fields, "s17")
        if pressure is not None:
            self.setDriver(
                "GV4",
                float(pressure) / 100
            )

        alarm_code = field_value(fields, "s20")
        if alarm_code is not None:
            self.setDriver(
                "GV5",
                int(float(alarm_code))
            )

        running_raw = field_value(fields, "s14")

        running_program = 100

        if running_raw is not None:
            running_program = int(running_raw) + 1

        self.setDriver(
            "GV6",
            running_program
        )

        self.update_programs(
            fields,
            running_program
        )

    def update_programs(
        self,
        fields,
        running_program
    ):
        for program_id in range(1, 9):

            enabled = field_value(
                fields,
                f"zp{program_id}e13"
            )

            if enabled != "1":
                continue

            program_name = field_value(
                fields,
                f"zp{program_id}e2"
            )

            if not program_name:
                program_name = (
                    f"Program {program_id}"
                )

            program_key = (
                f"{self.device_id}:"
                f"{program_id}"
            )

            address = make_address(
                "r",
                program_key
            )

            if address not in self.program_nodes:

                node = PumpProgramNode(
                    polyglot,
                    controller.address,
                    address,
                    str(program_name),
                    program_id,
                    self.device_id
                )

                self.program_nodes[address] = node

                polyglot.addNode(node)

                LOGGER.info(
                    f"Added pump program "
                    f"{program_name} "
                    f"(Program {program_id})"
                )

            self.program_nodes[
                address
            ].set_running(
                program_id == running_program
            )

    def query(self, command=None):
        if controller is not None:
            controller.query()

    commands = {
        "QUERY": query,
    }


class ColorSyncNode(udi_interface.Node):
    id = "pentaircolorsync"

    drivers = [
        {"driver": "ST",  "value": 0, "uom": 25},
        {"driver": "GV1", "value": 0, "uom": 25},
        {"driver": "GV7", "value": 0, "uom": 25},
    ]

    MODES = {
        "RED": 0,
        "WHITE": 1,
        "MAGENTA": 2,
        "GREEN": 3,
        "BLUE": 4,
        "HOLD": 5,
        "RECALL": 6,
        "SAM": 7,
        "PARTY": 8,
        "ROMANCE": 9,
        "CARIBBEAN": 10,
        "AMERICAN": 11,
        "SUNSET": 12,
        "ROYAL": 13,
    }

    def __init__(
        self,
        polyglot,
        primary,
        address,
        name,
        device_id
    ):
        super().__init__(
            polyglot,
            primary,
            address,
            name
        )

        self.device_id = device_id

    def update_from_device(self, device):
        online = device.get("online")

        if online is not None:
            self.setDriver(
                "ST",
                1 if online else 0
            )

    def update_from_response(self, device_response):
        fields = device_response.get("fields", {})

        online = device_response.get("online")
        if online is not None:
            self.setDriver(
                "ST",
                1 if online else 0
            )

        power = field_value(fields, "d13")
        if power is not None:
            self.setDriver(
                "GV1",
                int(power)
            )

        mode = field_value(fields, "s8")
        if mode is None:
            mode = field_value(fields, "d1")

        if mode is not None:
            self.setDriver(
                "GV7",
                int(mode)
            )

    def power_on(self, command=None):
        self._set_power(True)

    def power_off(self, command=None):
        self._set_power(False)

    def _set_power(self, on):
        if controller is None:
            return

        try:
            controller.client.set_colorsync_power(
                self.device_id,
                on
            )

            # Immediately reflect the commanded state in IoX.
            self.setDriver(
                "GV1",
                1 if on else 0
            )

        except Exception as err:
            LOGGER.error(
                f"Color Sync power command failed: {err}"
            )

    def _set_mode(self, mode_name):
        if controller is None:
            return

        try:
            mode = self.MODES[mode_name]

            controller.client.set_colorsync_mode(
                self.device_id,
                mode
            )

            # Immediately reflect the commanded mode in IoX.
            self.setDriver(
                "GV7",
                int(mode)
            )

        except Exception as err:
            LOGGER.error(
                f"Color Sync mode command failed: {err}"
            )

    def red(self, command=None):
        self._set_mode("RED")

    def white(self, command=None):
        self._set_mode("WHITE")

    def magenta(self, command=None):
        self._set_mode("MAGENTA")

    def green(self, command=None):
        self._set_mode("GREEN")

    def blue(self, command=None):
        self._set_mode("BLUE")

    def hold(self, command=None):
        self._set_mode("HOLD")

    def recall(self, command=None):
        self._set_mode("RECALL")

    def sam(self, command=None):
        self._set_mode("SAM")

    def party(self, command=None):
        self._set_mode("PARTY")

    def romance(self, command=None):
        self._set_mode("ROMANCE")

    def caribbean(self, command=None):
        self._set_mode("CARIBBEAN")

    def american(self, command=None):
        self._set_mode("AMERICAN")

    def sunset(self, command=None):
        self._set_mode("SUNSET")

    def royal(self, command=None):
        self._set_mode("ROYAL")

    def query(self, command=None):
        if controller is not None:
            controller.query()

    commands = {
        "ON": power_on,
        "OFF": power_off,
        "RED": red,
        "WHITE": white,
        "MAGENTA": magenta,
        "GREEN": green,
        "BLUE": blue,
        "HOLD": hold,
        "RECALL": recall,
        "SAM": sam,
        "PARTY": party,
        "ROMANCE": romance,
        "CARIBBEAN": caribbean,
        "AMERICAN": american,
        "SUNSET": sunset,
        "ROYAL": royal,
        "QUERY": query,
    }




class IntelliCenterClient:
    API_BASE = "https://prod-api.intellicenter.com"

    def __init__(self, cloud_client):
        self.cloud_client = cloud_client
        self.access_token = None
        self.installation = None
        self.installation_id = None
        self.ws = None
        self.ws_lock = threading.RLock()

    def _get_access_token(self):
        cognito = self.cloud_client.cognito

        if cognito is None:
            raise RuntimeError(
                "Pentair Cognito session is not available"
            )

        user = cognito.get_user()

        token = (
            getattr(cognito, "access_token", None)
            or getattr(user, "access_token", None)
            or (
                getattr(user, "_metadata", {}) or {}
            ).get("access_token")
        )

        if not token:
            raise RuntimeError(
                "Could not obtain IntelliCenter access token"
            )

        self.access_token = token
        return token

    def find_installations(self):
        token = self._get_access_token()

        url = (
            self.API_BASE
            + "/service/api/installations"
            + "?pageSize=100&page=1"
        )

        headers = {
            "Authorization": f"Bearer {token}",
            "access-token": token,
            "Accept": "application/json",
        }

        response = requests.get(
            url,
            headers=headers,
            timeout=30
        )

        response.raise_for_status()
        data = response.json()

        if isinstance(data, list):
            installations = data
        else:
            installations = (
                data.get("data")
                or data.get("installations")
                or []
            )

        return installations

    def connect(self, installation=None):
        with self.ws_lock:
            if installation is not None:
                self.installation = installation
                self.installation_id = installation.get(
                    "InstallationId"
                )

            if not self.installation_id:
                raise RuntimeError(
                    "IntelliCenter installation has no InstallationId"
                )

            # Close any stale socket first.
            if self.ws is not None:
                try:
                    self.ws.close()
                except Exception:
                    pass

                self.ws = None

            token = self._get_access_token()

            ws_url = (
                "wss://prod-api.intellicenter.com/client"
                f"?id={self.installation_id}"
                f"&access-token={token}"
            )

            LOGGER.info(
                f"Connecting IntelliCenter WebSocket "
                f"for installation {self.installation_id}"
            )

            self.ws = websocket.create_connection(
                ws_url,
                timeout=15
            )

            LOGGER.info(
                "IntelliCenter WebSocket connected"
            )

            return True

    def is_connected(self):
        ws = self.ws

        if ws is None:
            return False

        try:
            return bool(ws.connected)
        except Exception:
            return False

    def ensure_connected(self):
        with self.ws_lock:
            if self.is_connected():
                return True

            LOGGER.warning(
                "IntelliCenter WebSocket is not connected; "
                "reconnecting"
            )

            return self.connect()

    def close(self):
        with self.ws_lock:
            if self.ws is not None:
                try:
                    self.ws.close()
                except Exception:
                    pass

            self.ws = None

    def send_json(self, message):
        data = json.dumps(message)

        for attempt in range(2):
            try:
                self.ensure_connected()

                with self.ws_lock:
                    self.ws.send(data)

                return

            except (
                websocket.WebSocketConnectionClosedException,
                BrokenPipeError,
                OSError
            ) as err:
                LOGGER.warning(
                    f"IntelliCenter WebSocket send failed: "
                    f"{err}"
                )

                self.close()

                if attempt == 0:
                    continue

                raise

        raise RuntimeError(
            "Unable to send IntelliCenter WebSocket message"
        )

    def query_objects(self, condition, keys):
        if self.ws is None:
            raise RuntimeError(
                "IntelliCenter WebSocket is not connected"
            )

        message_id = (
            "PG3X_QUERY_" + str(uuid.uuid4())
        )

        query = {
            "condition": condition,
            "objectList": [
                {
                    "objnam": "ALL",
                    "keys": keys
                }
            ],
            "command": "GETPARAMLIST",
            "messageID": message_id
        }

        self.send_json(query)

        while True:
            raw = self.ws.recv()

            if not raw:
                continue

            try:
                message = json.loads(raw)
            except Exception:
                continue

            if message.get("messageID") != message_id:
                continue

            return message.get("objectList", [])








class IntelliCenterLightNode(udi_interface.Node):
    id = "pentairiclight"

    drivers = [
        {"driver": "ST", "value": 0, "uom": 25},
        {"driver": "GV27", "value": 0, "uom": 25},
    ]

    LIGHT_ACTIONS = {
        "WHITE": "WHITER",
        "GREEN": "GREENR",
        "BLUE": "BLUER",
        "MAGENTA": "MAGNTAR",
        "RED": "REDR",
        "SAM": "SAMMOD",
        "PARTY": "PARTY",
        "ROMANCE": "ROMAN",
        "CARIBBEAN": "CARIB",
        "AMERICAN": "AMERCA",
        "SUNSET": "SSET",
        "ROYAL": "ROYAL",
        "HOLD": "HOLD",
        "RECALL": "RECALL",
    }

    USE_TO_MODE = {
        "WHITER": 0,
        "GREENR": 1,
        "BLUER": 2,
        "MAGNTAR": 3,
        "REDR": 4,
        "SAMMOD": 5,
        "PARTY": 6,
        "ROMAN": 7,
        "CARIB": 8,
        "AMERCA": 9,
        "SSET": 10,
        "ROYAL": 11,
        "12": 12,
        "13": 13,
        "HOLD": 12,
        "RECALL": 13,
    }

    def __init__(
        self,
        polyglot,
        primary,
        address,
        name,
        controller,
        objnam
    ):
        super().__init__(
            polyglot,
            primary,
            address,
            name
        )

        self.controller = controller
        self.objnam = objnam
        self.params = {}

    def update_from_params(self, params):
        if not params:
            return

        self.params.update(params)

        status = self.params.get("STATUS")

        if status is not None:
            self.setDriver(
                "ST",
                1 if str(status).upper() == "ON" else 0
            )

        # LIMIT is the cleanest readback: IntelliCenter reports
        # 0-11 for colors/shows, 12 for Hold and 13 for Recall.
        limit = self.params.get("LIMIT")

        if limit is not None:
            try:
                value = int(limit)

                if 0 <= value <= 13:
                    self.setDriver("GV27", value)
                    return
            except Exception:
                pass

        # USE provides the textual readback and is useful if
        # LIMIT is absent from a partial NotifyList.
        use = self.params.get("USE")

        if use is not None:
            mode = self.USE_TO_MODE.get(
                str(use).upper()
            )

            if mode is not None:
                self.setDriver("GV27", mode)

    def query(self, command=None):
        self.controller.query_intellicenter_object(
            self.objnam
        )

    def cmd_on(self, command=None):
        self.controller.set_intellicenter_param(
            self.objnam,
            "STATUS",
            "ON"
        )

    def cmd_off(self, command=None):
        self.controller.set_intellicenter_param(
            self.objnam,
            "STATUS",
            "OFF"
        )

    def set_light_action(self, action):
        code = self.LIGHT_ACTIONS[action]

        self.controller.set_intellicenter_params(
            self.objnam,
            {
                "ACT": code,
                "STATUS": "ON",
            }
        )

    def cmd_white(self, command=None):
        self.set_light_action("WHITE")

    def cmd_green(self, command=None):
        self.set_light_action("GREEN")

    def cmd_blue(self, command=None):
        self.set_light_action("BLUE")

    def cmd_magenta(self, command=None):
        self.set_light_action("MAGENTA")

    def cmd_red(self, command=None):
        self.set_light_action("RED")

    def cmd_sam(self, command=None):
        self.set_light_action("SAM")

    def cmd_party(self, command=None):
        self.set_light_action("PARTY")

    def cmd_romance(self, command=None):
        self.set_light_action("ROMANCE")

    def cmd_caribbean(self, command=None):
        self.set_light_action("CARIBBEAN")

    def cmd_american(self, command=None):
        self.set_light_action("AMERICAN")

    def cmd_sunset(self, command=None):
        self.set_light_action("SUNSET")

    def cmd_royal(self, command=None):
        self.set_light_action("ROYAL")

    def cmd_hold(self, command=None):
        self.set_light_action("HOLD")

    def cmd_recall(self, command=None):
        self.set_light_action("RECALL")

    commands = {
        "QUERY": query,
        "ON": cmd_on,
        "OFF": cmd_off,
        "WHITE": cmd_white,
        "GREEN": cmd_green,
        "BLUE": cmd_blue,
        "MAGENTA": cmd_magenta,
        "RED": cmd_red,
        "SAM": cmd_sam,
        "PARTY": cmd_party,
        "ROMANCE": cmd_romance,
        "CARIBBEAN": cmd_caribbean,
        "AMERICAN": cmd_american,
        "SUNSET": cmd_sunset,
        "ROYAL": cmd_royal,
        "HOLD": cmd_hold,
        "RECALL": cmd_recall,
    }


class IntelliCenterCircuitNode(udi_interface.Node):
    id = "pentairiccircuit"

    drivers = [
        {"driver": "ST", "value": 0, "uom": 25},
    ]

    def __init__(
        self,
        polyglot,
        primary,
        address,
        name,
        controller,
        objnam
    ):
        super().__init__(
            polyglot,
            primary,
            address,
            name
        )

        self.controller = controller
        self.objnam = objnam
        self.params = {}

    def update_from_params(self, params):
        if not params:
            return

        self.params.update(params)

        status = self.params.get("STATUS")

        if status is not None:
            self.setDriver(
                "ST",
                1 if str(status).upper() == "ON" else 0
            )

    def query(self, command=None):
        self.controller.query_intellicenter_object(
            self.objnam
        )

    def cmd_on(self, command=None):
        self.controller.set_intellicenter_param(
            self.objnam,
            "STATUS",
            "ON"
        )

    def cmd_off(self, command=None):
        self.controller.set_intellicenter_param(
            self.objnam,
            "STATUS",
            "OFF"
        )

    commands = {
        "QUERY": query,
        "ON": cmd_on,
        "OFF": cmd_off,
    }


class IntelliCenterChemNode(udi_interface.Node):
    id = "pentairicchem"

    drivers = [
        {"driver": "ST", "value": 1, "uom": 25},
        {"driver": "GV12", "value": 0, "uom": 56},
        {"driver": "GV13", "value": 0, "uom": 56},
        {"driver": "GV14", "value": 0, "uom": 56},
        {"driver": "GV15", "value": 0, "uom": 56},
        {"driver": "GV16", "value": 0, "uom": 56},
        {"driver": "GV17", "value": 0, "uom": 56},
        {"driver": "GV18", "value": 0, "uom": 56},
        {"driver": "GV19", "value": 0, "uom": 56},
    ]

    def __init__(self, polyglot, primary, address, name, controller, objnam):
        super().__init__(polyglot, primary, address, name)
        self.controller = controller
        self.objnam = objnam
        self.params = {}

    def update_from_params(self, params):
        if not params:
            return

        self.params.update(params)
        self.setDriver("ST", 1)

        mapping = (
            ("PHVAL", "GV12"),
            ("ORPVAL", "GV13"),
            ("SALT", "GV14"),
            ("ALK", "GV15"),
            ("CALC", "GV16"),
            ("CYACID", "GV17"),
            ("PHSET", "GV18"),
            ("ORPSET", "GV19"),
        )

        for key, driver in mapping:
            value = self.params.get(key)
            if value is None:
                continue
            try:
                self.setDriver(driver, float(value))
            except Exception:
                pass

    def query(self, command=None):
        self.controller.query_intellicenter_object(self.objnam)

    def _send_chem_config(self, changed_key, changed_value):
        required = (
            "SNAME",
            "SUBTYP",
            "BODY",
            "COMUART",
            "PHSET",
            "ORPSET",
            "ALK",
            "CALC",
            "CYACID",
            "PHTNKEN",
        )

        params = {}

        for key in required:
            value = self.params.get(key)

            if value is None:
                LOGGER.error(
                    f"Cannot update IntelliChem: "
                    f"missing {key}"
                )
                return

            params[key] = str(value)

        params[changed_key] = str(changed_value)

        LOGGER.info(
            f"Setting {self.name} "
            f"{changed_key}={changed_value}"
        )

        self.controller.set_intellicenter_params(
            self.objnam,
            params
        )

    def cmd_set_ph(self, command):
        try:
            query = command.get("query", {})

            raw_value = (
                query.get("value.uom25")
                or query.get("value.uom56")
                or query.get("value")
                or command.get("value")
            )

            if raw_value is None:
                LOGGER.error(
                    f"No pH setpoint in command: {command}"
                )
                return

            raw_num = int(round(float(raw_value)))

            if not 70 <= raw_num <= 76:
                LOGGER.error(
                    f"pH setpoint {raw_num} outside "
                    f"70-76 tenths; command not sent"
                )
                return

            value = raw_num / 10.0

            self._send_chem_config(
                "PHSET",
                f"{value:.1f}"
            )

        except Exception as err:
            LOGGER.error(
                f"Error setting {self.name} pH: {err}"
            )

    def cmd_set_orp(self, command):
        try:
            query = command.get("query", {})

            raw_value = (
                query.get("value.uom56")
                or query.get("value")
                or command.get("value")
            )

            if raw_value is None:
                LOGGER.error(
                    f"No ORP setpoint in command: {command}"
                )
                return

            value = int(round(float(raw_value)))

            if not 400 <= value <= 800:
                LOGGER.error(
                    f"ORP setpoint {value} outside "
                    f"400-800; command not sent"
                )
                return

            self._send_chem_config(
                "ORPSET",
                value
            )

        except Exception as err:
            LOGGER.error(
                f"Error setting {self.name} ORP: {err}"
            )

    commands = {
        "QUERY": query,
        "SET_PH": cmd_set_ph,
        "SET_ORP": cmd_set_orp,
    }


class IntelliCenterChlorNode(udi_interface.Node):
    id = "pentairicchlor"

    drivers = [
        {"driver": "ST", "value": 1, "uom": 25},
        {"driver": "GV20", "value": 0, "uom": 56},
        {"driver": "GV21", "value": 0, "uom": 51},
        {"driver": "GV22", "value": 0, "uom": 51},
        {"driver": "GV23", "value": 0, "uom": 25},
    ]

    def __init__(self, polyglot, primary, address, name, controller, objnam):
        super().__init__(polyglot, primary, address, name)
        self.controller = controller
        self.objnam = objnam
        self.params = {}

    def update_from_params(self, params):
        if not params:
            return

        self.params.update(params)
        self.setDriver("ST", 1)

        for key, driver in (
            ("SALT", "GV20"),
            ("PRIM", "GV21"),
            ("SEC", "GV22"),
        ):
            value = self.params.get(key)
            if value is None:
                continue
            try:
                self.setDriver(driver, float(value))
            except Exception:
                pass

        super_value = self.params.get("SUPER")
        if super_value is not None:
            self.setDriver(
                "GV23",
                1 if str(super_value).upper() == "ON" else 0
            )

    def query(self, command=None):
        self.controller.query_intellicenter_object(self.objnam)

    commands = {"QUERY": query}


class IntelliCenterHeaterNode(udi_interface.Node):
    id = "pentairicheater"

    drivers = [
        {"driver": "ST", "value": 0, "uom": 25},
        {"driver": "GV24", "value": 0, "uom": 25},
        {"driver": "GV25", "value": 0, "uom": 25},
        {"driver": "GV26", "value": 0, "uom": 25},
    ]

    def __init__(
        self,
        polyglot,
        primary,
        address,
        name,
        controller,
        objnam
    ):
        super().__init__(
            polyglot,
            primary,
            address,
            name
        )

        self.controller = controller
        self.objnam = objnam
        self.params = {}

    @staticmethod
    def _on(value):
        return 1 if str(value).upper() in (
            "ON",
            "HEATING",
            "COOLING",
            "READY"
        ) else 0

    def update_from_params(self, params):
        if not params:
            return

        self.params.update(params)

        mapping = (
            ("STATUS", "ST"),
            ("READY", "GV24"),
            ("HEATING", "GV25"),
            ("COOL", "GV26"),
        )

        for key, driver in mapping:
            if key in self.params:
                self.setDriver(
                    driver,
                    self._on(self.params[key])
                )

    def query(self, command=None):
        self.controller.query_intellicenter_object(
            self.objnam
        )

    commands = {
        "QUERY": query,
    }



class IntelliCenterBodyNode(udi_interface.Node):
    id = "pentairicbody"

    drivers = [
        {"driver": "ST", "value": 0, "uom": 25},
        {"driver": "CLITEMP", "value": 0, "uom": 17},
        {"driver": "GV28", "value": 0, "uom": 17},
        {"driver": "GV29", "value": 1, "uom": 25},
        {"driver": "GV30", "value": 0, "uom": 25},
    ]

    def __init__(
        self,
        polyglot,
        primary,
        address,
        name,
        controller,
        objnam
    ):
        super().__init__(
            polyglot,
            primary,
            address,
            name
        )

        self.controller = controller
        self.objnam = objnam
        self.params = {}

    def update_from_params(self, params):
        if not params:
            return

        self.params.update(params)

        status = self.params.get("STATUS")
        if status is not None:
            self.setDriver(
                "ST",
                1 if str(status).upper() == "ON" else 0
            )

        value = self.params.get("TEMP")

        if value is not None:
            try:
                self.setDriver("CLITEMP", float(value))
            except Exception:
                pass

        lotmp = self.params.get("LOTMP")
        if lotmp is not None:
            try:
                self.setDriver("GV28", float(lotmp))
            except Exception:
                pass

        mode = self.params.get("MODE")
        if mode is not None:
            try:
                self.setDriver("GV29", int(mode))
            except Exception:
                pass

        htmode = self.params.get("HTMODE")
        if htmode is not None:
            try:
                self.setDriver("GV30", int(htmode))
            except Exception:
                pass

    def query(self, command=None):
        self.controller.query_intellicenter_object(
            self.objnam
        )

    def cmd_on(self, command=None):
        self.controller.set_intellicenter_param(
            self.objnam,
            "STATUS",
            "ON"
        )

    def cmd_off(self, command=None):
        self.controller.set_intellicenter_param(
            self.objnam,
            "STATUS",
            "OFF"
        )

    def cmd_set_heat_temp(self, command):
        try:
            query = command.get("query", {})

            raw_value = (
                query.get("value.uom17")
                or query.get("value")
                or command.get("value")
            )

            if raw_value is None:
                LOGGER.error(
                    f"No heat temperature value in command: {command}"
                )
                return

            value = int(round(float(raw_value)))
            value = max(40, min(104, value))

            LOGGER.info(
                f"Setting {self.name} heat setpoint to {value}F"
            )

            self.controller.set_intellicenter_param(
                self.objnam,
                "LOTMP",
                str(value)
            )

        except Exception as err:
            LOGGER.error(
                f"Error setting {self.name} heat setpoint: {err}"
            )

    def cmd_set_heat_source(self, command):
        try:
            query = command.get("query", {})

            raw_value = (
                query.get("value.uom25")
                or query.get("value")
                or command.get("value")
            )

            if raw_value is None:
                LOGGER.error(
                    f"No heat source value in command: {command}"
                )
                return

            value = int(float(raw_value))
            value = max(1, min(5, value))

            LOGGER.info(
                f"Setting {self.name} heat source to {value}"
            )

            self.controller.set_intellicenter_param(
                self.objnam,
                "MODE",
                str(value)
            )

        except Exception as err:
            LOGGER.error(
                f"Error setting {self.name} heat source: {err}"
            )

    commands = {
        "QUERY": query,
        "ON": cmd_on,
        "OFF": cmd_off,
        "SET_HEAT_TEMP": cmd_set_heat_temp,
        "SET_HEAT_SOURCE": cmd_set_heat_source,
    }



class IntelliCenterPumpCircuitNode(udi_interface.Node):
    id = "pentairicpumpcirc"

    drivers = [
        {"driver": "GV31", "value": 0, "uom": 56},
        {"driver": "GV32", "value": 0, "uom": 25},
    ]

    def __init__(
        self,
        polyglot,
        primary,
        address,
        name,
        controller,
        objnam,
        parent_objnam=None,
        circuit_objnam=None
    ):
        super().__init__(
            polyglot,
            primary,
            address,
            name
        )

        self.controller = controller
        self.objnam = objnam
        self.parent_objnam = parent_objnam
        self.circuit_objnam = circuit_objnam
        self.params = {}
        self.pending_select = None

    def update_from_params(self, params):
        if not params:
            return

        self.params.update(params)

        if self.params.get("PARENT"):
            self.parent_objnam = self.params.get("PARENT")

        if self.params.get("CIRCUIT"):
            self.circuit_objnam = self.params.get("CIRCUIT")

        speed = self.params.get("SPEED")

        if speed is not None:
            try:
                self.setDriver(
                    "GV31",
                    float(speed)
                )
            except Exception:
                pass

        select = str(
            self.params.get("SELECT", "")
        ).upper()

        if select == "RPM":
            self.setDriver("GV32", 0)
            self.pending_select = "RPM"
        elif select == "GPM":
            self.setDriver("GV32", 1)
            self.pending_select = "GPM"
    def query(self, command=None):
        self.controller.query_intellicenter_object(
            self.objnam
        )

    def cmd_rpm_mode(self, command=None):
        self.pending_select = "RPM"

        self.setDriver("GV32", 0)
        self.setDriver("GV31", 0)

        LOGGER.info(
            f"{self.name} pending mode set to RPM"
        )

    def cmd_gpm_mode(self, command=None):
        self.pending_select = "GPM"

        self.setDriver("GV32", 1)
        self.setDriver("GV31", 0)

        LOGGER.info(
            f"{self.name} pending mode set to GPM"
        )

    def cmd_set_target(self, command):
        try:
            query = command.get("query", {})

            raw_value = (
                query.get("value.uom56")
                or query.get("value")
                or command.get("value")
            )

            if raw_value is None:
                LOGGER.error(
                    f"No pump target value in command: {command}"
                )
                return

            value = int(round(float(raw_value)))

            select = (
                self.pending_select
                or str(
                    self.params.get("SELECT", "RPM")
                ).upper()
            )

            if select == "RPM":
                if not 450 <= value <= 3450:
                    LOGGER.error(
                        f"RPM target {value} is outside "
                        f"450-3450; command not sent"
                    )
                    return

            elif select == "GPM":
                if not 20 <= value <= 140:
                    LOGGER.error(
                        f"GPM target {value} is outside "
                        f"20-140; command not sent"
                    )
                    return

            else:
                LOGGER.error(
                    f"Unknown pump mode {select}; "
                    f"command not sent"
                )
                return

            LOGGER.info(
                f"Setting {self.name} to "
                f"{value} {select}"
            )

            self.controller.set_intellicenter_params(
                self.objnam,
                {
                    "SPEED": str(value),
                    "SELECT": select,
                }
            )

        except Exception as err:
            LOGGER.error(
                f"Error setting {self.name} target: {err}"
            )

    commands = {
        "QUERY": query,
        "RPM_MODE": cmd_rpm_mode,
        "GPM_MODE": cmd_gpm_mode,
        "SET_PUMP_TARGET": cmd_set_target,
    }


class IntelliCenterPumpNode(udi_interface.Node):
    id = "pentairicpump"

    drivers = [
        {"driver": "ST", "value": 0, "uom": 25},
        {"driver": "GV1", "value": 0, "uom": 73},
        {"driver": "GV8", "value": 0, "uom": 56},
        {"driver": "GV3", "value": 0, "uom": 56},
    ]

    def __init__(
        self,
        polyglot,
        primary,
        address,
        name,
        controller,
        objnam
    ):
        super().__init__(
            polyglot,
            primary,
            address,
            name
        )

        self.controller = controller
        self.objnam = objnam
        self.params = {}

    def update_from_params(self, params):
        if not params:
            return

        self.params.update(params)

        rpm = self.params.get("RPM")
        gpm = self.params.get("GPM")
        pwr = self.params.get("PWR")

        try:
            rpm_num = int(float(rpm))
        except Exception:
            rpm_num = 0

        self.setDriver(
            "ST",
            1 if rpm_num > 0 else 0
        )

        if pwr is not None:
            try:
                self.setDriver("GV1", float(pwr))
            except Exception:
                pass

        if rpm is not None:
            try:
                self.setDriver("GV8", float(rpm))
            except Exception:
                pass

        if gpm is not None:
            try:
                self.setDriver("GV3", float(gpm))
            except Exception:
                pass

    def query(self, command=None):
        self.controller.query_intellicenter_object(
            self.objnam
        )

    commands = {
        "QUERY": query,
    }


class Controller(udi_interface.Node):
    id = "pentairctrl"

    drivers = [
        {"driver": "ST", "value": 0, "uom": 25},
    ]

    def __init__(
        self,
        polyglot,
        primary,
        address,
        name
    ):
        super().__init__(
            polyglot,
            primary,
            address,
            name
        )

        self.client = PentairCloudClient()
        self.intellicenter = IntelliCenterClient(
            self.client
        )

        self.username = None
        self.password = None

        self.pumps = {}
        self.colorsyncs = {}

        self.ic_bodies = {}
        self.ic_pumps = {}
        self.ic_heaters = {}
        self.ic_chem = {}
        self.ic_chlor = {}
        self.ic_circuits = {}
        self.ic_lights = {}
        self.ic_pumpcircs = {}

        # Metadata for every IntelliCenter CIRCUIT object,
        # including Pool/Spa/internal circuits that may not
        # become their own IoX nodes.  PMPCIRC uses this to
        # resolve CIRCUIT IDs to friendly names.
        self.ic_circuit_info = {}

        self.ic_listener_thread = None
        self.ic_listener_stop = threading.Event()

    def configure(self, params):
        self.username = params.get("username")
        self.password = params.get("password")

        if not self.username or not self.password:
            LOGGER.warning(
                "Please configure username and "
                "password in the plugin "
                "Configuration page."
            )

            self.setDriver("ST", 0)
            return

        self.connect()

    def connect(self):
        try:
            self.client.authenticate(
                self.username,
                self.password
            )

            self.discover_devices()

            self.setDriver("ST", 1)

            self.query()

        except Exception as err:
            LOGGER.error(
                f"Pentair Cloud connection "
                f"failed: {err}"
            )

            self.setDriver("ST", 0)

    def discover_devices(self):
        devices = self.client.list_devices()

        LOGGER.info(
            f"Pentair account returned "
            f"{len(devices)} device(s)"
        )

        if not devices:
            self.discover_intellicenter()

        for device in devices:

            device_type = device.get(
                "deviceType"
            )

            device_id = device.get(
                "deviceId"
            )

            product_info = (
                device.get("productInfo")
                or {}
            )

            nickname = (
                product_info.get("nickName")
                or device.get("pname")
                or device_type
                or "Pentair Device"
            )

            if not device_id:
                continue

            if device_type == "IF31":
                self.add_pump(
                    device_id,
                    nickname
                )

            elif device_type == "PLC1":
                self.add_colorsync(
                    device_id,
                    nickname
                )

            else:
                LOGGER.warning(
                    "Unsupported Pentair device: "
                    f"type={device_type}, "
                    f"name={nickname}, "
                    f"product={device.get('pname')}"
                )

    def add_ic_circuit(self, obj):
        objnam = str(obj.get("objnam", "")).strip()
        params = obj.get("params", {}) or {}

        if not objnam:
            return

        info = self.ic_circuit_info.setdefault(
            objnam,
            {}
        )
        info.update(params)

        name = str(
            params.get("SNAME", "")
        ).strip()

        subtype = str(
            params.get("SUBTYP", "")
        ).upper()

        # Ignore unnamed objects.
        if not name:
            return

        # IntelliCenter color lights use the same CIRCUIT
        # object type but SUBTYP=INTELLI.  Give them the
        # dedicated color/show node instead of a generic
        # On/Off circuit.
        if subtype == "INTELLI":
            address = make_address(
                "l",
                "intellicenter:" + objnam
            )

            node = self.ic_lights.get(address)

            if node is None:
                node = IntelliCenterLightNode(
                    polyglot,
                    self.address,
                    address,
                    name,
                    self,
                    objnam
                )

                self.ic_lights[address] = node
                polyglot.addNode(node)

                LOGGER.info(
                    f"Added IntelliCenter color light node: "
                    f"{name} ({objnam})"
                )

            node.update_from_params(params)
            return

        # IntelliCenter creates many internal helper objects
        # beginning with X or _.  These are not normal
        # user-facing circuits.
        if objnam.upper().startswith(("X", "_")):
            return

        # Pool and Spa filter circuits are already represented
        # by the BODY nodes.
        if subtype in ("POOL", "SPA"):
            return

        # Default unassigned AUX names are generally placeholders.
        # If we later identify a reliable "assigned" flag, we can
        # refine this rule.
        upper_name = name.upper()

        if upper_name.startswith("AUX "):
            suffix = upper_name[4:].strip()

            if suffix.isdigit():
                LOGGER.debug(
                    f"Skipping default AUX circuit "
                    f"{objnam}: {name}"
                )
                return

        address = make_address(
            "c",
            "intellicenter:" + objnam
        )

        node = self.ic_circuits.get(address)

        if node is None:
            node = IntelliCenterCircuitNode(
                polyglot,
                self.address,
                address,
                name,
                self,
                objnam
            )

            self.ic_circuits[address] = node
            polyglot.addNode(node)

            LOGGER.info(
                f"Added IntelliCenter circuit node: "
                f"{name} ({objnam}, {subtype})"
            )

        node.update_from_params(params)

    def add_ic_chem(self, obj):
        objnam = obj.get("objnam")
        params = obj.get("params", {}) or {}

        if not objnam:
            return

        subtype = str(params.get("SUBTYP", "")).upper()

        if subtype == "ICHEM":
            address = make_address("m", "intellicenter:" + objnam)

            node = self.ic_chem.get(address)

            if node is None:
                name = params.get("SNAME") or "IntelliChem"

                node = IntelliCenterChemNode(
                    polyglot,
                    self.address,
                    address,
                    name,
                    self,
                    objnam
                )

                self.ic_chem[address] = node
                polyglot.addNode(node)

                LOGGER.info(
                    f"Added IntelliChem node: {name} ({objnam})"
                )

            node.update_from_params(params)

        elif subtype == "ICHLOR":
            address = make_address("r", "intellicenter:" + objnam)

            node = self.ic_chlor.get(address)

            if node is None:
                name = params.get("SNAME") or "IntelliChlor"

                node = IntelliCenterChlorNode(
                    polyglot,
                    self.address,
                    address,
                    name,
                    self,
                    objnam
                )

                self.ic_chlor[address] = node
                polyglot.addNode(node)

                LOGGER.info(
                    f"Added IntelliChlor node: {name} ({objnam})"
                )

            node.update_from_params(params)

    def add_ic_heater(self, obj):
        objnam = obj.get("objnam")
        params = obj.get("params", {}) or {}

        if not objnam:
            return

        address = make_address(
            "h",
            "intellicenter:" + objnam
        )

        node = self.ic_heaters.get(address)

        if node is None:
            name = (
                params.get("SNAME")
                or objnam
            )

            node = IntelliCenterHeaterNode(
                polyglot,
                self.address,
                address,
                name,
                self,
                objnam
            )

            self.ic_heaters[address] = node
            polyglot.addNode(node)

            LOGGER.info(
                f"Added IntelliCenter heater node: "
                f"{name} ({objnam})"
            )

        node.update_from_params(params)

    def add_ic_body(self, obj):
        objnam = obj.get("objnam")
        params = obj.get("params", {}) or {}

        if not objnam:
            return

        address = make_address(
            "b",
            "intellicenter:" + objnam
        )

        node = self.ic_bodies.get(address)

        if node is None:
            name = (
                params.get("SNAME")
                or params.get("SUBTYP")
                or objnam
            )

            node = IntelliCenterBodyNode(
                polyglot,
                self.address,
                address,
                name,
                self,
                objnam
            )

            self.ic_bodies[address] = node
            polyglot.addNode(node)

            LOGGER.info(
                f"Added IntelliCenter body node: "
                f"{name} ({objnam})"
            )

        node.update_from_params(params)

    def add_ic_pumpcirc(self, obj):
        objnam = obj.get("objnam")
        params = obj.get("params", {}) or {}

        if not objnam:
            return

        parent = params.get("PARENT")
        circuit = params.get("CIRCUIT")

        address = make_address(
            "q",
            "intellicenter:" + objnam
        )

        node = self.ic_pumpcircs.get(address)

        if node is None:
            # Resolve the friendly circuit name dynamically.
            circuit_info = (
                self.ic_circuit_info.get(circuit, {})
                if circuit else {}
            )

            circuit_name = (
                circuit_info.get("SNAME")
                or circuit
                or objnam
            )

            # Resolve the parent pump's friendly name.
            pump_name = parent

            for pump_node in self.ic_pumps.values():
                if pump_node.objnam == parent:
                    pump_name = pump_node.name
                    break

            name = f"{circuit_name} - Pump Speed"

            node = IntelliCenterPumpCircuitNode(
                polyglot,
                self.address,
                address,
                name,
                self,
                objnam,
                parent,
                circuit
            )

            self.ic_pumpcircs[address] = node
            polyglot.addNode(node)

            LOGGER.info(
                f"Added IntelliCenter pump assignment: "
                f"{name} ({objnam}, "
                f"parent={parent}, circuit={circuit})"
            )

        node.update_from_params(params)

    def add_ic_pump(self, obj):
        objnam = obj.get("objnam")
        params = obj.get("params", {}) or {}

        if not objnam:
            return

        address = make_address(
            "i",
            "intellicenter:" + objnam
        )

        node = self.ic_pumps.get(address)

        if node is None:
            name = (
                params.get("SNAME")
                or params.get("SUBTYP")
                or objnam
            )

            node = IntelliCenterPumpNode(
                polyglot,
                self.address,
                address,
                name,
                self,
                objnam
            )

            self.ic_pumps[address] = node
            polyglot.addNode(node)

            LOGGER.info(
                f"Added IntelliCenter pump node: "
                f"{name} ({objnam})"
            )

        node.update_from_params(params)

    def get_ic_node_by_objnam(self, objnam):
        for node in self.ic_bodies.values():
            if node.objnam == objnam:
                return node

        for node in self.ic_pumps.values():
            if node.objnam == objnam:
                return node

        for node in self.ic_pumpcircs.values():
            if node.objnam == objnam:
                return node

        for node in self.ic_heaters.values():
            if node.objnam == objnam:
                return node

        for node in self.ic_chem.values():
            if node.objnam == objnam:
                return node

        for node in self.ic_chlor.values():
            if node.objnam == objnam:
                return node

        for node in self.ic_circuits.values():
            if node.objnam == objnam:
                return node

        for node in self.ic_lights.values():
            if node.objnam == objnam:
                return node

        return None

    def query_intellicenter_object(self, objnam):
        if self.intellicenter.ws is None:
            return

        message_id = (
            "PG3X_OBJECT_" + str(uuid.uuid4())
        )

        query = {
            "condition": "",
            "objectList": [
                {
                    "objnam": objnam,
                    "keys": [
                        "OBJNAM",
                        "OBJTYP",
                        "SUBTYP",
                        "SNAME",
                        "STATUS",
                        "TEMP",
                        "LOTMP",
                        "HITMP",
                        "HTMODE",
                        "RPM",
                        "GPM",
                        "PWR",
                        "PARENT",
                        "BODY",
                        "CIRCUIT",
                        "LISTORD",
                        "SPEED",
                        "SELECT",
                        "STATIC"
                    ]
                }
            ],
            "command": "REQUESTPARAMLIST",
            "messageID": message_id
        }

        self.intellicenter.send_json(
            query
        )

    def set_intellicenter_params(
        self,
        objnam,
        params
    ):
        if not isinstance(params, dict) or not params:
            raise ValueError(
                "IntelliCenter params must be a non-empty dict"
            )

        message = {
            "command": "SetParamList",
            "objectList": [
                {
                    "objnam": objnam,
                    "params": params
                }
            ],
            "messageID": (
                "PG3X_SET_" + str(uuid.uuid4())
            )
        }

        LOGGER.info(
            f"IntelliCenter command: "
            f"{objnam} {params}"
        )

        self.intellicenter.send_json(
            message
        )

    def set_intellicenter_param(
        self,
        objnam,
        key,
        value
    ):
        self.set_intellicenter_params(
            objnam,
            {
                key: value
            }
        )


    def handle_intellicenter_message(self, message):
        command = message.get("command")

        updates = []

        if command == "NotifyList":
            updates = (
                message.get("objectList", [])
                or []
            )

        elif command == "WriteParamList":
            for wrapper in (
                message.get("objectList", [])
                or []
            ):
                updates.extend(
                    wrapper.get("changes", [])
                    or []
                )

        else:
            return

        for obj in updates:
            objnam = obj.get("objnam")
            params = obj.get("params", {}) or {}

            if not objnam or not params:
                continue

            # Preserve CIRCUIT metadata even when the CIRCUIT
            # itself is intentionally not exposed as a node.
            objtyp = str(
                params.get("OBJTYP", "")
            ).upper()

            if objtyp == "CIRCUIT":
                info = self.ic_circuit_info.setdefault(
                    objnam,
                    {}
                )
                info.update(params)

            node = self.get_ic_node_by_objnam(
                objnam
            )

            if node is not None:
                LOGGER.debug(
                    f"IntelliCenter update "
                    f"{objnam}: {params}"
                )

                node.update_from_params(
                    params
                )

            elif objtyp == "PMPCIRC":
                # Allows a newly-created pump assignment to
                # appear without restarting the plugin.
                self.add_ic_pumpcirc(obj)

    def intellicenter_listener(self):
        LOGGER.info(
            "IntelliCenter listener started"
        )

        while not self.ic_listener_stop.is_set():

            try:
                self.intellicenter.ensure_connected()

                ws = self.intellicenter.ws

                # Periodically return from recv so we can
                # notice plugin shutdown.
                ws.settimeout(5)

                raw = ws.recv()

                if not raw:
                    continue

                try:
                    message = json.loads(raw)
                except Exception:
                    continue

                self.handle_intellicenter_message(
                    message
                )

            except websocket.WebSocketTimeoutException:
                continue

            except (
                websocket.WebSocketConnectionClosedException,
                BrokenPipeError,
                OSError
            ) as err:
                LOGGER.warning(
                    f"IntelliCenter WebSocket connection lost: "
                    f"{err}"
                )

                self.intellicenter.close()

                if not self.ic_listener_stop.wait(2):
                    LOGGER.info(
                        "Attempting IntelliCenter "
                        "WebSocket reconnect"
                    )

                continue

            except Exception as err:
                LOGGER.error(
                    f"IntelliCenter listener error: {err}"
                )

                self.intellicenter.close()

                if not self.ic_listener_stop.wait(5):
                    LOGGER.info(
                        "Attempting IntelliCenter "
                        "WebSocket reconnect"
                    )

                continue

        LOGGER.info(
            "IntelliCenter listener stopped"
        )

    def start_intellicenter_listener(self):
        if (
            self.ic_listener_thread is not None
            and self.ic_listener_thread.is_alive()
        ):
            return

        self.ic_listener_stop.clear()

        # A short timeout lets the listener periodically
        # check whether it has been asked to stop.
        if self.intellicenter.ws is not None:
            self.intellicenter.ws.settimeout(5)

        self.ic_listener_thread = threading.Thread(
            target=self.intellicenter_listener,
            name="IntelliCenterListener",
            daemon=True
        )

        self.ic_listener_thread.start()

    def discover_intellicenter(self):
        LOGGER.info(
            "Checking for IntelliCenter installations"
        )

        installations = (
            self.intellicenter.find_installations()
        )

        LOGGER.info(
            f"Pentair account returned "
            f"{len(installations)} IntelliCenter "
            f"installation(s)"
        )

        if not installations:
            return

        for installation in installations:
            installation_id = installation.get(
                "InstallationId"
            )

            name = (
                installation.get("PoolName")
                or installation.get("Name")
                or f"Installation {installation_id}"
            )

            LOGGER.info(
                f"IntelliCenter installation found: "
                f"{name} ({installation_id})"
            )

        # First implementation supports the first installation.
        installation = installations[0]

        self.intellicenter.connect(
            installation
        )

        LOGGER.info(
            "IntelliCenter WebSocket connected"
        )

        queries = [
            (
                "PUMP",
                "OBJTYP = PUMP",
                [
                    "OBJNAM", "OBJTYP", "SUBTYP",
                    "SNAME", "CIRCUIT", "RPM",
                    "GPM", "PWR", "STATUS"
                ]
            ),
            (
                "CIRCUIT",
                "OBJTYP = CIRCUIT",
                [
                    "OBJNAM", "OBJTYP", "SUBTYP",
                    "SNAME", "STATUS", "USAGE",
                    "USE"
                ]
            ),
            (
                "PMPCIRC",
                "OBJTYP=PMPCIRC",
                [
                    "OBJNAM", "OBJTYP", "PARENT",
                    "BODY", "CIRCUIT", "LISTORD",
                    "SPEED", "SELECT", "STATIC"
                ]
            ),
            (
                "BODY",
                "OBJTYP = BODY",
                [
                    "OBJNAM", "OBJTYP", "SUBTYP",
                    "SNAME", "STATUS", "TEMP",
                    "LOTMP", "MODE",
                    "FILTER", "HEATER", "HTSRC",
                    "HTMODE"
                ]
            ),
            (
                "HEATER",
                "OBJTYP = HEATER",
                [
                    "OBJNAM", "OBJTYP", "SUBTYP",
                    "SNAME", "BODY", "STATUS",
                    "READY", "HEATING", "COOL",
                    "MODE"
                ]
            ),
            (
                "SENSE",
                "OBJTYP = SENSE",
                [
                    "OBJNAM", "OBJTYP", "SNAME",
                    "SOURCE", "PROBE", "CALIB",
                    "STATUS"
                ]
            ),
            (
                "CHEM",
                "OBJTYP = CHEM",
                [
                    "OBJNAM", "OBJTYP", "SUBTYP",
                    "SNAME", "BODY",
                    "COMUART", "PHTNKEN",
                    "PHVAL", "ORPVAL",
                    "SALT", "ALK", "CALC",
                    "CYACID", "PHSET", "ORPSET",
                    "PRIM", "SEC", "SUPER"
                ]
            ),
        ]

        for label, condition, keys in queries:
            try:
                objects = (
                    self.intellicenter.query_objects(
                        condition,
                        keys
                    )
                )

                LOGGER.info(
                    f"IntelliCenter {label}: "
                    f"{len(objects)} object(s)"
                )

                for obj in objects:
                    LOGGER.info(
                        f"IntelliCenter {label} object: "
                        f"{obj.get('objnam')} "
                        f"{obj.get('params', {})}"
                    )

                    if label == "BODY":
                        self.add_ic_body(obj)

                    elif label == "PUMP":
                        self.add_ic_pump(obj)

                    elif label == "PMPCIRC":
                        self.add_ic_pumpcirc(obj)

                    elif label == "HEATER":
                        self.add_ic_heater(obj)

                    elif label == "CHEM":
                        self.add_ic_chem(obj)

                    elif label == "CIRCUIT":
                        self.add_ic_circuit(obj)

            except Exception as err:
                LOGGER.error(
                    f"IntelliCenter {label} discovery "
                    f"failed: {err}"
                )

        self.start_intellicenter_listener()

        LOGGER.info(
            "IntelliCenter discovery complete; "
            "live listener running"
        )

    def add_pump(
        self,
        device_id,
        nickname
    ):
        address = make_address(
            "p",
            device_id
        )

        if address in self.pumps:
            return

        node = PumpNode(
            polyglot,
            self.address,
            address,
            nickname,
            device_id
        )

        self.pumps[address] = node

        polyglot.addNode(node)

        LOGGER.info(
            f"Added IntelliFlo3 "
            f"node: {nickname}"
        )

    def add_colorsync(
        self,
        device_id,
        nickname
    ):
        address = make_address(
            "c",
            device_id
        )

        if address in self.colorsyncs:
            return

        node = ColorSyncNode(
            polyglot,
            self.address,
            address,
            nickname,
            device_id
        )

        self.colorsyncs[address] = node

        polyglot.addNode(node)

        LOGGER.info(
            f"Added Color Sync "
            f"node: {nickname}"
        )

    def get_pump_node_by_device_id(self, device_id):
        for node in self.pumps.values():
            if node.device_id == device_id:
                return node

        return None

    def start_program(self, device_id, program_id):
        pump = self.get_pump_node_by_device_id(
            device_id
        )

        if pump is None:
            raise RuntimeError(
                "Pump node not found"
            )

        # Stop any other program currently reported as running.
        for program_node in pump.program_nodes.values():
            if (
                program_node.running
                and program_node.program_id != program_id
            ):
                LOGGER.info(
                    f"Stopping currently active program "
                    f"{program_node.program_id} before "
                    f"starting program {program_id}"
                )

                self.client.stop_program(
                    device_id,
                    program_node.program_id
                )

                program_node.set_running(False)

        self.client.start_program(
            device_id,
            program_id
        )

        # Optimistically update the IoX program nodes.
        for program_node in pump.program_nodes.values():
            program_node.set_running(
                program_node.program_id == program_id
            )

    def stop_program(self, device_id, program_id):
        pump = self.get_pump_node_by_device_id(
            device_id
        )

        self.client.stop_program(
            device_id,
            program_id
        )

        if pump is not None:
            for program_node in pump.program_nodes.values():
                if program_node.program_id == program_id:
                    program_node.set_running(False)

    def query(self, command=None):
        try:
            devices = self.client.list_devices()

            by_id = {
                d.get("deviceId"): d
                for d in devices
                if d.get("deviceId")
            }

            for node in self.colorsyncs.values():
                device = by_id.get(
                    node.device_id
                )

                if device is not None:
                    node.update_from_device(
                        device
                    )

            colorsync_ids = [
                node.device_id
                for node in self.colorsyncs.values()
            ]

            if colorsync_ids:
                colorsync_status = (
                    self.client
                    .get_device_status(
                        colorsync_ids
                    )
                )

                by_colorsync_id = {
                    d.get("deviceId"): d
                    for d in colorsync_status
                    if d.get("deviceId")
                }

                for node in self.colorsyncs.values():
                    response = by_colorsync_id.get(
                        node.device_id
                    )

                    if response is not None:
                        node.update_from_response(
                            response
                        )

            pump_ids = [
                node.device_id
                for node in self.pumps.values()
            ]

            if pump_ids:
                pump_status = (
                    self.client
                    .get_pump_status(
                        pump_ids
                    )
                )

                by_pump_id = {
                    d.get("deviceId"): d
                    for d in pump_status
                    if d.get("deviceId")
                }

                for node in self.pumps.values():
                    response = by_pump_id.get(
                        node.device_id
                    )

                    if response is not None:
                        node.update_from_response(
                            response
                        )

            self.setDriver("ST", 1)

        except Exception as err:
            LOGGER.error(
                f"Pentair status update "
                f"failed: {err}"
            )

            self.setDriver("ST", 0)

    commands = {
        "QUERY": query,
    }


def custom_params_handler(params):
    global controller

    LOGGER.info(
        "Received Pentair custom parameters"
    )

    if not params:
        LOGGER.info(
            "No Pentair custom parameters supplied yet"
        )
        return

    if controller is None:

        controller = Controller(
            polyglot,
            "controller",
            "controller",
            "Pentair Cloud"
        )

        polyglot.addNode(controller)

    controller.configure(params)


def poll_handler(poll_type):
    if controller is None:
        return

    if poll_type != "shortPoll":
        return

    if not poll_lock.acquire(
        blocking=False
    ):
        LOGGER.warning(
            "Previous Pentair poll still "
            "running; skipping this poll"
        )
        return

    try:
        controller.query()

    finally:
        poll_lock.release()


def stop_handler():
    LOGGER.info(
        "Pentair Cloud plugin stopping"
    )

    polyglot.stop()


if __name__ == "__main__":
    try:
        polyglot.start(VERSION)

        polyglot.subscribe(
            polyglot.CUSTOMPARAMS,
            custom_params_handler
        )

        polyglot.subscribe(
            polyglot.POLL,
            poll_handler
        )

        polyglot.subscribe(
            polyglot.STOP,
            stop_handler
        )

        configuration_help = (
            "./configdoc.md"
        )

        if os.path.isfile(
            configuration_help
        ):
            cfgdoc = (
                markdown2.markdown_path(
                    configuration_help
                )
            )

            polyglot.setCustomParamsDoc(
                cfgdoc
            )

        polyglot.ready()
        polyglot.updateProfile()

        polyglot.runForever()

    except (
        KeyboardInterrupt,
        SystemExit
    ):
        sys.exit(0)

    except Exception:
        LOGGER.exception(
            "Unhandled Pentair exception"
        )

        polyglot.stop()
        sys.exit(1)
