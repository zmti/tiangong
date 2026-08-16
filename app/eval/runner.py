"""黄金用例回归运行器。

用法(在 agent-service 目录):
    python -m app.eval.runner                 # 全量用例
    python -m app.eval.runner calc,time       # 只跑指定用例
    AGENT_TOOL_ROUTER_ENABLED=false python -m app.eval.runner   # 对比:关掉工具路由
    python -m app.eval.runner --json out.json --baseline app/eval/baseline.json  # CI:落盘 + 对比基线

输出:每个用例 pass/fail、是否命中期望工具、步数与耗时;最后汇总准确率。
CI 用法:--baseline 存在时,任一基线为 pass 的用例本次失败即退出码 1(回归门禁)。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time

from app.core.agent import Agent
from app.core.llm import LLMClient
from app.eval.cases import EVAL_CASES
from app.tools.registry import get_registry


def _check(result, case: dict) -> tuple[bool, list[str]]:
    used = [s.get("tool") for s in result.steps if s["type"] == "tool"]
    reasons: list[str] = []
    expect_tool = case.get("expect_tool")
    if expect_tool and expect_tool not in used:
        reasons.append(f"未调用期望工具 {expect_tool} (实际: {used or '无'})")
    kw = case.get("expect_keyword")
    if kw and kw not in result.answer:
        reasons.append(f"答案未包含关键词 '{kw}': {(result.answer or '')[:80]}")
    # 多步任务:期望工具之后还必须调用另一个工具
    after = case.get("tool_after")
    if after and expect_tool:
        try:
            i = used.index(expect_tool)
            if after not in used[i + 1:]:
                reasons.append(f"期望在 {expect_tool} 之后继续调用 {after} (实际后续: {used[i + 1:] or '无'})")
        except ValueError:
            pass  # expect_tool 已失败,不重复报
    return (not reasons, reasons)


async def run_case(case: dict, judge: bool = False, judge_llm=None) -> dict:
    t0 = time.time()
    try:
        agent = Agent(llm=LLMClient(), registry=get_registry(), session_id=f"eval-{case['id']}")
        result = await asyncio.wait_for(agent.run(case["user_input"]), timeout=120)
        ok, reasons = _check(result, case)
        out = {
            "id": case["id"],
            "ok": ok,
            "reasons": reasons,
            "steps": len(result.steps),
            "seconds": round(time.time() - t0, 1),
        }
        if judge:
            from app.eval.judge import judge_result

            score = await judge_result(case, result, judge_llm)
            out["judge"] = score
        return out
    except Exception as e:  # noqa: BLE001
        return {
            "id": case["id"],
            "ok": False,
            "reasons": [f"执行异常: {e}"],
            "steps": 0,
            "seconds": round(time.time() - t0, 1),
        }


def save_json(results: list[dict], path: str) -> None:
    """把结果写入 JSON 文件(供 CI 对比与历史存档)。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"results": results,
                   "passed": sum(1 for r in results if r["ok"]),
                   "total": len(results)}, f, ensure_ascii=False, indent=2)


def compare_baseline(results: list[dict], baseline: dict) -> list[str]:
    """对比基线:返回回归描述列表(空=无回归)。

    baseline 格式: {"cases": {"calc": true, ...}} —— 基线里为 true 的用例,
    本次结果必须仍为 true,否则视为回归。
    """
    regressions = []
    cases = (baseline or {}).get("cases", {})
    by_id = {r["id"]: r for r in results}
    for case_id, expected_pass in cases.items():
        if not expected_pass:
            continue
        r = by_id.get(case_id)
        if r is None:
            regressions.append(f"{case_id}: 基线为 pass 但本次未运行")
        elif not r["ok"]:
            regressions.append(f"{case_id}: 基线 pass -> 本次 FAIL ({'; '.join(r['reasons'])})")
    return regressions


async def _run_all(cases: list[dict], judge: bool = False, judge_llm=None) -> list[dict]:
    return await asyncio.gather(*[run_case(c, judge=judge, judge_llm=judge_llm) for c in cases])


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent 黄金用例回归")
    parser.add_argument("ids", nargs="*", help="用例 id 子集,默认全部")
    parser.add_argument("--category", dest="category", default="", help="只跑指定分类(basic/memory/web/browser/db/chart/file/code/rag/multi_agent/planning)")
    parser.add_argument("--json", dest="json_path", default="", help="结果 JSON 输出路径")
    parser.add_argument("--baseline", dest="baseline", default="", help="基线 JSON 路径(存在则做回归门禁)")
    parser.add_argument("--judge", action="store_true", help="启用 LLM-as-Judge 质量打分")
    parser.add_argument("--judge-model", dest="judge_model", default="", help="评测模型名(默认复用主模型)")
    parser.add_argument("--judge-provider", dest="judge_provider", default="", help="评测模型 provider(默认复用主 provider)")
    args = parser.parse_args()

    cases = [c for c in EVAL_CASES if not args.ids or c["id"] in args.ids]
    if args.category:
        cases = [c for c in cases if c.get("category") == args.category]
    if not cases:
        categories = sorted({c.get("category", "?") for c in EVAL_CASES})
        print("没有匹配的用例/分类。可选 id:", [c["id"] for c in EVAL_CASES], "| 分类:", categories)
        return 1

    from app.config import settings

    judge_llm = None
    if args.judge:
        from app.core.llm import LLMClient

        judge_llm = LLMClient(args.judge_provider, args.judge_model or None) if args.judge_provider else (
            LLMClient(model=args.judge_model) if args.judge_model else None
        )

    print(f"运行 {len(cases)} 个用例 | 工具路由: {'开启' if settings.agent_tool_router_enabled else '关闭'}"
          + (" | LLM-judge 开启" if args.judge else "") + "\n")
    results = asyncio.run(_run_all(cases, judge=args.judge, judge_llm=judge_llm))
    passed = sum(1 for r in results if r["ok"])
    for r in results:
        mark = "PASS" if r["ok"] else "FAIL"
        detail = "; ".join(r["reasons"]) if r["reasons"] else ""
        line = f"[{mark}] {r['id']:<8} {r['seconds']:>5.1f}s {r['steps']:>3}步  {detail}"
        if args.judge and r.get("judge"):
            j = r["judge"]
            line += (f" | success={j.get('task_success', '-')} "
                     f"correct={j.get('correctness', '-')} "
                     f"faith={j.get('faithfulness', '-')} "
                     f"help={j.get('helpfulness', '-')}")
        print(line)
    print(f"\n--- 通过 {passed}/{len(results)} ({passed * 100 // len(results)}%) ---")
    if args.judge:
        scored = [r["judge"] for r in results if r.get("judge")]
        if scored:
            avg = lambda k: round(sum(float(s.get(k, 0) or 0) for s in scored) / len(scored), 2)
            print(f"--- LLM-judge 均值: success={avg('task_success')} "
                  f"correctness={avg('correctness')} faithfulness={avg('faithfulness')} "
                  f"helpfulness={avg('helpfulness')} (n={len(scored)}) ---")

    if args.json_path:
        save_json(results, args.json_path)
        print(f"结果已写入 {args.json_path}")

    exit_code = 0
    if args.baseline:
        if not os.path.exists(args.baseline):
            print(f"基线文件不存在: {args.baseline}")
            exit_code = 1
        else:
            with open(args.baseline, "r", encoding="utf-8") as f:
                baseline = json.load(f)
            regressions = compare_baseline(results, baseline)
            if regressions:
                print("\n!! 回归检出:")
                for line in regressions:
                    print(f"  - {line}")
                exit_code = 1
            else:
                print("\n无回归,对比基线通过。")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
