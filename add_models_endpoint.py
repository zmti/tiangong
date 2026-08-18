#!/usr/bin/env python3
# Add /v1/models endpoint to chat.py

with open(r'E:\dev\agent-service\app\api\chat.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Check if models endpoint already exists
if '/v1/models' in content or 'list_models' in content:
    print('/v1/models endpoint already exists')
    exit(0)

# Models endpoint code
models_endpoint = '''
# ---- Model 配置接口 ----

# 各 provider 的默认模型
_PROVIDER_DEFAULT_MODELS: dict[str, list[dict]] = {
    "deepseek": [
        {"id": "deepseek-chat", "name": "DeepSeek Chat", "speed": "fast", "quality": "good"},
        {"id": "deepseek-coder", "name": "DeepSeek Coder", "speed": "fast", "quality": "excellent"},
        {"id": "deepseek-reasoner", "name": "DeepSeek R1", "speed": "slow", "quality": "excellent"},
    ],
    "qwen": [
        {"id": "qwen-plus", "name": "Qwen Plus", "speed": "fast", "quality": "good"},
        {"id": "qwen-turbo", "name": "Qwen Turbo", "speed": "very_fast", "quality": "fair"},
        {"id": "qwen-max", "name": "Qwen Max", "speed": "medium", "quality": "excellent"},
        {"id": "qwen3.7-flash", "name": "Qwen 3.7 Flash", "speed": "very_fast", "quality": "good"},
    ],
    "openai": [
        {"id": "gpt-4o-mini", "name": "GPT-4o Mini", "speed": "very_fast", "quality": "good"},
        {"id": "gpt-4o", "name": "GPT-4o", "speed": "fast", "quality": "excellent"},
        {"id": "gpt-4-turbo", "name": "GPT-4 Turbo", "speed": "medium", "quality": "excellent"},
        {"id": "o1-mini", "name": "O1 Mini", "speed": "slow", "quality": "excellent"},
        {"id": "o1", "name": "O1", "speed": "very_slow", "quality": "outstanding"},
    ],
    "compat": [
        {"id": "agnes-2.0-flash", "name": "Agnes 2.0 Flash", "speed": "fast", "quality": "good"},
        {"id": "agnes-2.5-flash", "name": "Agnes 2.5 Flash", "speed": "very_fast", "quality": "good"},
        {"id": "agnes-2.5-pro", "name": "Agnes 2.5 Pro", "speed": "medium", "quality": "excellent"},
        {"id": "agnes-2.5-pro-alpha", "name": "Agnes 2.5 Pro Alpha", "speed": "medium", "quality": "outstanding"},
    ],
    "ollama": [],
}

# Provider 分组信息
_PROVIDER_GROUPS = [
    {"id": "agnes", "name": "Agnes AI", "providers": ["compat"]},
    {"id": "deepseek", "name": "DeepSeek", "providers": ["deepseek"]},
    {"id": "qwen", "name": "阿里云 Qwen", "providers": ["qwen"]},
    {"id": "openai", "name": "OpenAI", "providers": ["openai"]},
    {"id": "local", "name": "本地模型", "providers": ["ollama"]},
]


@router.get("/models")
async def list_models(provider: str | None = Query(None, description="指定 provider，留空返回所有")) -> dict:
    """获取可用模型列表。"""
    if provider:
        provider = provider.lower()
        if provider in _PROVIDER_DEFAULT_MODELS:
            models = _PROVIDER_DEFAULT_MODELS[provider]
            configured = False
            if provider == "deepseek":
                configured = bool(settings.deepseek_api_key)
            elif provider == "qwen":
                configured = bool(settings.qwen_api_key)
            elif provider == "openai":
                import os
                configured = bool(os.environ.get("OPENAI_API_KEY"))
            elif provider == "compat":
                configured = bool(settings.openai_compat_api_key)
            elif provider == "ollama":
                configured = True
            return {
                "provider": provider,
                "configured": configured,
                "models": models,
            }
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"未知的 provider: {provider}")
    
    current_provider = settings.agent_llm_provider.lower()
    default_model = None
    if current_provider == "deepseek":
        default_model = settings.deepseek_model
    elif current_provider == "qwen":
        default_model = settings.qwen_model
    elif current_provider == "openai":
        default_model = "gpt-4o-mini"
    elif current_provider == "compat":
        default_model = settings.openai_compat_model
    elif current_provider == "ollama":
        default_model = settings.ollama_model
    
    return {
        "provider": current_provider,
        "model": default_model,
        "available_providers": [
            {"id": p["id"], "name": p["name"], "providers": p["providers"]}
            for p in _PROVIDER_GROUPS
        ],
    }


'''

# Insert before @router.get("/providers")
if '@router.get("/providers")' in content:
    content = content.replace('@router.get("/providers")', models_endpoint + '@router.get("/providers")')
    with open(r'E:\dev\agent-service\app\api\chat.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print('Added /v1/models endpoint')
else:
    print('Could not find insertion point')
