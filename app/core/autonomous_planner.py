# AutonomousPlanner - deprecated, use planner.py instead
# This module is kept for backward compatibility

from app.core.planner import PlanStep, TaskPlan, generate_plan, apply_plan_tool  # noqa: F401

# Alias for backward compatibility
class AutonomousPlanner(TaskPlan):
    """已弃用: 使用 planner.py 的 TaskPlan"""
    pass
