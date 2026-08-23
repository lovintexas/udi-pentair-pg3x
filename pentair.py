#!/usr/bin/env python3

import sys
import os
import hashlib
import threading

import boto3
import markdown2
import requests
import udi_interface
from pycognito import Cognito
from requests_aws4auth import AWS4Auth


LOGGER = udi_interface.LOGGER

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

        self.username = None
        self.password = None

        self.pumps = {}
        self.colorsyncs = {}

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
        polyglot.start()

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
