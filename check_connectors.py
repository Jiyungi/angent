"""Check existing Airbyte connectors and what's needed for Gmail."""
import os
import requests
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("AIRBYTE_CLIENT_ID")
CLIENT_SECRET = os.getenv("AIRBYTE_CLIENT_SECRET")
ORG_ID = os.getenv("AIRBYTE_ORGANIZATION_ID")

# Get token
print("Getting Airbyte token...")
token_resp = requests.post(
    "https://api.airbyte.com/v1/applications/token",
    headers={"Content-Type": "application/json", "Accept": "application/json"},
    json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "grant_type": "client_credentials"},
)
token = token_resp.json().get("access_token")
print(f"Token OK: {token[:15]}...")

# List existing agent connectors via the agents API
print("\nListing existing agent connectors...")
for base in ["https://api.airbyte.ai/api/v1/integrations/connectors"]:
    try:
        r = requests.get(
            base,
            params={"workspace_name": "default"},
            headers={"Authorization": f"Bearer {token}", "X-Organization-Id": ORG_ID, "Accept": "application/json"},
        )
        print(f"GET {base} -> {r.status_code}")
        print(r.text[:1500])
    except Exception as e:
        print(f"  error: {e}")
