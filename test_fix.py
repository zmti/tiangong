import sys
sys.path.insert(0, r'E:\dev\agent-service')

# Test that the module can be imported and the function exists
from app.api.chat import list_models
print("Module imported successfully")
print("list_models function:", list_models)

# Check the function signature
import inspect
sig = inspect.signature(list_models)
print("Signature:", sig)