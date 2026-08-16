"""代码级自愈:run_code/run_terminal 失败时,LLM 生成修复 patch 并自动重试。

闭环(对齐主流 coding agent):
1. 收集失败输出(错误信息/栈/测试失败断言)
2. LLM 基于"失败输出 + 相关源码"生成修复方案(schema: 改哪个文件、怎么改)
3. 应用修复(write_file / edit_file 语义),记录 file 版本
4. 重跑原命令,校验是否通过;未通过则再修复(最多 agent_code_heal_rounds 轮)

任何一步失败都以"未修复"返回,由上级反思逻辑接管,不阻塞主流程。
"""
from __future__ import annotations

import asyncio
import json


def extract_error_text(result_str: str, cap: int = 2500) -> str:
    """从工具结果 JSON 字符串中提取失败输出文本(stderr/stdout/error)。"""
    if not result_str:
        return ""
    try:
        data = json.loads(result_str)
        if isinstance(data, dict):
            err = str(data.get("error", "") or "")
            stderr = str(data.get("stderr", "") or "")
            stdout = str(data.get("stdout", "") or "")
            out = (err or stderr or stdout).strip()
            return out[:cap]
    except (json.JSONDecodeError, TypeError):
        pass
    return str(result_str)[:cap]


def _mark_still_failing(result_str: str) -> str:
    """修复后仍失败时在结果里打标记,供上层收敛(避免无意义的反复自愈)。"""
    marker = '\n[heal] 修复后重跑仍失败,请人工检查或换思路'
    try:
        data = json.loads(result_str)
        if isinstance(data, dict):
            data["heal_still_failing"] = True
            return json.dumps(data, ensure_ascii=False) + marker
    except (json.JSONDecodeError, TypeError):
        pass
    return result_str + marker


async def heal_code_failure(
    llm,
    user_input: str,
    tool_name: str,
    arguments: str,
    result_str: str,
    max_rounds: int = 2,
) -> dict:
    """自愈一个代码工具失败。返回 {'healed': bool, 'rounds': int, 'steps': [...], 'final': str}。

    只处理代码类工具(run_code / run_terminal);其他工具失败原样返回。
    """
    if tool_name not in ("run_code", "run_terminal"):
        return {"healed": False, "rounds": 0, "steps": [], "final": result_str}
    if not settings_code_heal_enabled():
        return {"healed": False, "rounds": 0, "steps": [], "final": result_str}
    if getattr(llm, "provider", "") == "mock":
        return {"healed": False, "rounds": 0, "steps": [], "final": result_str}

    from app.tools.registry import get_registry

    error_text = extract_error_text(result_str)
    if not error_text:
        return {"healed": False, "rounds": 0, "steps": [], "final": result_str}

    registry = get_registry()
    write_file = registry.get("write_file")
    edit_file = registry.get("edit_file")
    run_terminal = registry.get("run_terminal")
    if run_terminal is None:
        return {"healed": False, "rounds": 0, "steps": [], "final": result_str}

    steps: list[dict] = []
    current = result_str
    healed = False
    schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["fix_file", "rerun_only", "no_fix_available"]},
            "file": {"type": "string", "description": "需要修复的文件路径(绝对路径或相对)"},
            "old": {"type": "string", "description": "要替换的原始代码片段(需精确匹配)"},
            "new": {"type": "string", "description": "替换后的代码片段"},
            "explanation": {"type": "string"},
        },
        "required": ["action", "explanation"],
    }

    for round_no in range(max_rounds):
        prompt = _build_heal_prompt(user_input, tool_name, arguments, error_text, round_no)
        try:
            data = await llm.chat_json_schema(
                [
                    {"role": "system", "content": (
                        "你是代码修复专家。根据失败输出诊断问题,输出修复方案 JSON。"
                        "action=fix_file 时必须给出 file + old + new(精确代码替换);"
                        "无法确定修复方案时返回 action=no_fix_available。"
                    )},
                    {"role": "user", "content": prompt},
                ],
                schema,
                temperature=0.1,
            )
        except Exception:  # noqa: BLE001
            break
        if not isinstance(data, dict):
            break
        action = data.get("action", "")
        explanation = str(data.get("explanation", ""))[:300]

        if action == "fix_file":
            file_path, applied = await _apply_fix(write_file, edit_file, data)
            steps.append({
                "round": round_no + 1,
                "action": "fix_file",
                "file": file_path or "",
                "applied": applied,
                "explanation": explanation,
            })
            if not applied:
                error_text = error_text + "\n[修复尝试未应用,换一种方式]"
                continue
        elif action == "rerun_only":
            steps.append({"round": round_no + 1, "action": "rerun_only", "explanation": explanation})
        elif action == "no_fix_available":
            steps.append({"round": round_no + 1, "action": "no_fix_available", "explanation": explanation})
            break

        # 重跑原命令(修复后确认通过与否)
        rerun_cmd = _rerun_command(tool_name, arguments)
        if not rerun_cmd:
            break
        try:
            rerun = await run_terminal.run(json.dumps(rerun_cmd, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            error_text = f"重跑命令失败: {e}"
            continue
        current = rerun
        steps.append({"round": round_no + 1, "action": "rerun", "result": _short(current)})

        if _is_success(current):
            healed = True
            break
        error_text = extract_error_text(current)
        if not error_text:
            break

    if not healed:
        current = _mark_still_failing(current)
    return {"healed": healed, "rounds": len(steps), "steps": steps, "final": current}


def _build_heal_prompt(user_input: str, tool_name: str, arguments: str, error_text: str, round_no: int) -> str:
    try:
        args = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else {}
    except (json.JSONDecodeError, TypeError):
        args = {}
    if not isinstance(args, dict):
        args = {}
    command = args.get("command", "")
    code = args.get("code", "")
    lines = [
        f"用户任务: {user_input}",
        f"失败的工具: {tool_name}",
        f"命令/代码参数:\n{command or code or json.dumps(args, ensure_ascii=False)[:500]}",
        f"失败输出(第 {round_no + 1} 轮):\n{error_text[:2500]}",
        "请依据上面的失败输出给出修复方案。若是语法/异常错误,定位源码并给出精确替换的 old/new;",
        "若只是临时性故障,可返回 rerun_only 重跑;无法确定则返回 no_fix_available。",
    ]
    return "\n".join(lines)


async def _apply_fix(write_file, edit_file, data: dict) -> tuple[str, bool]:
    """应用修复(优先 write_file 全量写,失败回退 edit_file 局部替换)。"""
    file_path = str(data.get("file", "")).strip()
    old = str(data.get("old", ""))
    new = str(data.get("new", ""))
    if not file_path:
        return "", False
    applied = False
    if write_file is not None:
        try:
            result = await write_file.run({"path": file_path, "content": new})
            applied = _result_ok(result)
        except Exception:  # noqa: BLE001
            applied = False
    if not applied and edit_file is not None and old:
        try:
            ed = await edit_file.run({"path": file_path, "old_text": old, "new_text": new,
                                       "dry_run": False})
            applied = _result_ok(ed)
        except Exception:  # noqa: BLE001
            applied = False
    return file_path, applied


def _rerun_command(tool_name: str, arguments: str) -> dict:
    """从原参数构造重跑命令(run_terminal 直接复用;run_code 转成 python -c)。"""
    try:
        args = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else {}
    except (json.JSONDecodeError, TypeError):
        args = {}
    if not isinstance(args, dict):
        args = {}
    if tool_name == "run_terminal":
        cmd = str(args.get("command", "")).strip()
        if not cmd:
            return {}
        return {"command": cmd}
    if tool_name == "run_code":
        code = str(args.get("code", "")).strip()
        if not code:
            return {}
        return {"command": f"python -c {json.dumps(code)}"}
    return {}


def _is_success(result_str: str) -> bool:
    try:
        data = json.loads(result_str)
        if isinstance(data, dict):
            if data.get("success") is False or data.get("blocked") is True:
                return False
            if data.get("error"):
                return False
        return True
    except (json.JSONDecodeError, TypeError):
        return True


def _result_ok(result_str: str) -> bool:
    try:
        data = json.loads(result_str)
        if isinstance(data, dict):
            return not (data.get("error") or data.get("blocked") is True)
        return True
    except (json.JSONDecodeError, TypeError):
        return True


def _short(text: str, cap: int = 400) -> str:
    text = str(text or "")
    return text[:cap]


def settings_code_heal_enabled() -> bool:
    from app.config import settings

    return bool(settings.agent_code_self_heal)