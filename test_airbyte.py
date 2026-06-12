"""Quick test to verify Airbyte credentials work."""
import os
import requests
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("AIRBYTE_CLIENT_ID")
CLIENT_SECRET = os.getenv("AIRBYTE_CLIENT_SECRET")

# Step 1: Get an access token
print("🔑 Requesting access token from Airbyte...")
token_resp = requests.post(
    "https://api.airbyte.com/v1/applications/token",
    headers={"Content-Type": "application/json", "Accept": "application/json"},
    json={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials",
    },
)

if token_resp.status_code != 200:
    print(f"❌ Token request failed: {token_resp.status_code}")
    print(token_resp.text)
    exit(1)

token = token_resp.json().get("access_token")
print(f"✅ Got access token: {token[:20]}...")

# Step 2: List workspaces to confirm access works
print("\n📂 Listing workspaces...")
ws_resp = requests.get(
    "https://api.airbyte.com/v1/workspaces",
    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
)

if ws_resp.status_code == 200:
    data = ws_resp.json()
    workspaces = data.get("data", [])
    print(f"✅ Airbyte connection works! Found {len(workspaces)} workspace(s):")
    for ws in workspaces:
        print(f"   - {ws.get('name', 'unnamed')} (ID: {ws.get('workspaceId', 'N/A')})")
else:
    print(f"❌ Workspace list failed: {ws_resp.status_code}")
    print(ws_resp.text)
