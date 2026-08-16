SYSTEM_PROMPT = """你是一个智能体(Agent),通过调用工具来完成任务。
请严格按照以下规则工作:

1. 当需要外部信息时,调用工具获取。
2. 工具调用使用 JSON 格式输出,单独一行:
   {{"tool": "工具名", "arguments": {{...}}}}
3. 每次只调用一个工具,等待工具结果后再继续。
4. 得到工具结果后,基于结果回答用户。
5. 如果工具无法解决问题,如实告知用户。
6. 最终回答使用自然语言,简洁清晰。

可用工具:
{}

当前时间:{}
"""


def build_system_prompt(tools_desc: str, now_str: str, custom: str = "") -> str:
    base = SYSTEM_PROMPT.format(tools_desc, now_str)
    if custom:
        base = custom + "\n\n" + base
    return base


def build_tools_description(tool_schemas: list[dict]) -> str:
    lines = []
    for s in tool_schemas:
        fn = s.get("function", s)
        name = fn.get("name", "?")
        desc = fn.get("description", "")
        params = fn.get("parameters", {})
        props = params.get("properties", {})
        param_lines = []
        for pname, pinfo in props.items():
            required = "必填" if pname in params.get("required", []) else "可选"
            param_lines.append(f"      - {pname}: {pinfo.get('description', pinfo.get('type',''))} ({required})")
        param_str = "\n".join(param_lines) if param_lines else "      (无参数)"
        lines.append(f"- {name}: {desc}\n{param_str}")
    return "\n".join(lines)


TOOL_CALL_PATTERN = r'\{\s*"tool"\s*:\s*"([^"]+)"\s*,\s*"arguments"\s*:\s*(\{(?:[^{}]|\{[^{}]*\})*\})\s*\}'
