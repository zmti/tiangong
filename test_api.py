import sys
sys.path.insert(0, r'E:\dev\agent-service')

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

# Test the default endpoint (no provider)
response = client.get("/v1/models")
print("=== GET /v1/models (no provider) ===")
print("Status code:", response.status_code)
print("Response JSON:", response.json())

# Test with provider parameter
response2 = client.get("/v1/models?provider=deepseek")
print("\n=== GET /v1/models?provider=deepseek ===")
print("Status code:", response2.status_code)
print("Response JSON:", response2.json())