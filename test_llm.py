import sys
sys.path.insert(0, r'E:\dev\agent-service')

from app.core.llm import LLMClient

# Test 1: LLMClient with no provider (default config)
try:
    c = LLMClient()
    print('Test 1 OK: provider =', c.provider, ', model =', c._model_override)
except Exception as e:
    print('Test 1 ERROR:', type(e).__name__, ':', e)

# Test 2: LLMClient with provider but no model
try:
    c = LLMClient('deepseek')
    print('Test 2 OK: provider =', c.provider, ', model =', c._model_override)
except Exception as e:
    print('Test 2 ERROR:', type(e).__name__, ':', e)

# Test 3: LLMClient with provider and model
try:
    c = LLMClient('deepseek', 'deepseek-chat')
    print('Test 3 OK: provider =', c.provider, ', model =', c._model_override)
except Exception as e:
    print('Test 3 ERROR:', type(e).__name__, ':', e)

# Test 4: LLMClient with invalid provider
try:
    c = LLMClient('invalid-provider')
    print('Test 4 OK: provider =', c.provider)
except Exception as e:
    print('Test 4 ERROR:', type(e).__name__, ':', e)