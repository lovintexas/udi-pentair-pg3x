#!/usr/bin/env python3

import json
import getpass
import boto3
import requests
from pycognito import Cognito
from requests_aws4auth import AWS4Auth

AWS_REGION = "us-west-2"
AWS_USER_POOL_ID = "us-west-2_lbiduhSwD"
AWS_CLIENT_ID = "3de110o697faq7avdchtf07h4v"
AWS_IDENTITY_POOL_ID = "us-west-2:6f950f85-af44-43d9-b690-a431f753e9aa"
AWS_COGNITO_ENDPOINT = "cognito-idp.us-west-2.amazonaws.com"

PENTAIR_ENDPOINT = "https://api.pentair.cloud"

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

print("Authentication successful.")

# ---------------------------------------------------------
# Show SAFE Cognito attribute names, but not their values.
# ---------------------------------------------------------

print("\n=== Cognito user attribute names ===")

try:
    attrs = user._data.get("UserAttributes", [])
    for attr in attrs:
        print(attr.get("Name"))
except Exception as e:
    print("Could not inspect attributes:", e)

id_token = user._metadata["id_token"]

identity = boto3.client(
    "cognito-identity",
    region_name=AWS_REGION
)

r = identity.get_id(
    IdentityPoolId=AWS_IDENTITY_POOL_ID,
    Logins={
        f"{AWS_COGNITO_ENDPOINT}/{AWS_USER_POOL_ID}":
            id_token
    }
)

identity_id = r["IdentityId"]

r = identity.get_credentials_for_identity(
    IdentityId=identity_id,
    Logins={
        f"{AWS_COGNITO_ENDPOINT}/{AWS_USER_POOL_ID}":
            id_token
    }
)

creds = r["Credentials"]

aws_auth = AWS4Auth(
    creds["AccessKeyId"],
    creds["SecretKey"],
    AWS_REGION,
    "execute-api",
    session_token=creds["SessionToken"]
)

headers = {
    "x-amz-id-token": id_token,
    "user-agent": "aws-amplify/4.3.10 react-native",
    "content-type": "application/json; charset=UTF-8"
}

# ---------------------------------------------------------
# Known device endpoint
# ---------------------------------------------------------

url = (
    PENTAIR_ENDPOINT
    + "/device/device-service/user/devices"
)

print("\n=== Existing plugin discovery endpoint ===")
print(url)

response = requests.get(
    url,
    auth=aws_auth,
    headers=headers,
    timeout=30
)

print("HTTP:", response.status_code)

try:
    data = response.json()

    print("Top-level keys:", list(data.keys()))

    payload = data.get("data")

    print("data type:", type(payload).__name__)

    if isinstance(payload, list):
        print("device count:", len(payload))

    # Print only structure, not potentially sensitive values.
    if isinstance(payload, dict):
        print("data keys:", list(payload.keys()))

except Exception as e:
    print("Could not parse JSON:", e)

print("\n=== AWS identity ===")
print("Identity region:", identity_id.split(":")[0])

print("\nProbe complete.")
