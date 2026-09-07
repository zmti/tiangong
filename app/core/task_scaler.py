"""任务复杂度自动缩放器:根据任务类型动态调整 Agent 配置。

功能:
1. 任务复杂度分类(simple/medium/complex/hard)
2. 自动调整 max_steps(根据任务复杂度)
3. 自动调整 max_tools(根据任务复杂度)
4. 自动调整 timeout(根据任务复杂度)
5. 自动选择模式(simple=auto, complex=sequential/ensemble)

使用示例:
    from app.core.task_scaler import TaskScaler
    scaler = TaskScaler()
    config = scaler.scale(user_input)
    agent = Agent(max_steps=config.max_steps, ...)
"""
from __future__ import annotations

from typing import Optional

# 任务复杂度关键词库
_COMPLEX_KEYWORDS = (
    # 代码开发类
    "代码", "程序", "函数", "类", "接口", "API", "后端", "前端", "算法",
    "正则", "脚本", "开发", "实现", "编写", "重构", "调试", "修复", "测试",
    # 数据分析类
    "分析", "计算", "比较", "对比", "汇总", "整理", "数据库", "查询",
    "报表", "报告", "图表", "预测", "统计", "Excel", "CSV", "表格",
    # 搜索研究类
    "搜索", "研究", "调研", "搜集", "总结", "归纳",
    # 文件操作类
    "文件", "目录", "下载", "上传", "读取", "写入", "编辑", "创建",
    # 系统设计类
    "设计", "规划", "架构", "系统", "项目", "模块", "组件",
    # 学习类
    "学习", "教程", "解释", "说明", "文档", "帮助",
)

_SIMPLE_KEYWORDS = (
    "你好", "您好", "hi", "hello", "谢谢", "你是谁", "在吗", "早安", "晚安",
    "再见", "测试", "test", "随便", "哈哈", "嗯", "哦", "好的", "明白",
)

# 复杂度配置映射
COMPLEXITY_CONFIG = {
    "simple": {
        "max_steps": 20,
        "max_tools": 15,
        "timeout": 60,
        "mode": "auto",
        "context_budget": 100000,
        "description": "简单问答/问候",
    },
    "medium": {
        "max_steps": 40,
        "max_tools": 25,
        "timeout": 120,
        "mode": "auto",
        "context_budget": 150000,
        "description": "中等复杂度任务(单次操作)",
    },
    "complex": {
        "max_steps": 80,
        "max_tools": 40,
        "timeout": 180,
        "mode": "sequential",
        "context_budget": 200000,
        "description": "复杂任务(多步骤/多工具)",
    },
    "hard": {
        "max_steps": 150,
        "max_tools": 50,
        "timeout": 300,
        "mode": "hierarchical",
        "context_budget": 300000,
        "description": "超复杂任务(大型项目/长期运行)",
    },
}


class TaskScaler:
    """任务复杂度自动缩放器。"""

    def __init__(self):
        self._history: list[dict] = []
        self._max_history = 1000

    def classify(self, message: str) -> str:
        """分类任务复杂度。

        Returns:
            'simple' / 'medium' / 'complex' / 'hard'
        """
        msg = (message or "").strip()
        if not msg:
            return "simple"

        # 直接关键词匹配
        complex_matches = sum(1 for kw in _COMPLEX_KEYWORDS if kw in msg)
        simple_matches = sum(1 for kw in _SIMPLE_KEYWORDS if kw in msg)

        # 基于关键词匹配分类
        if simple_matches >= 2 and complex_matches == 0:
            return "simple"

        if complex_matches >= 4:
            return "hard"
        if complex_matches >= 2:
            return "complex"
        if complex_matches >= 1:
            if len(msg) > 50:
                return "complex"
            return "medium"

        # 基于长度分类
        if len(msg) > 200:
            return "complex"
        if len(msg) > 100:
            return "medium"
        if len(msg) > 30:
            return "simple"

        return "simple"

    def scale(self, message: str, override: Optional[dict] = None) -> dict:
        """根据任务复杂度缩放配置。

        Args:
            message: 用户输入消息
            override: 用户指定的配置覆盖

        Returns:
            缩放后的配置字典
        """
        complexity = self.classify(message)
        config = COMPLEXITY_CONFIG.get(complexity, COMPLEXITY_CONFIG["simple"]).copy()
        config["complexity"] = complexity

        # 应用用户覆盖
        if override:
            for key in ["max_steps", "max_tools", "timeout", "mode", "context_budget"]:
                if key in override and override[key] is not None:
                    config[key] = override[key]

        # 记录到历史
        self._record(message, complexity, config)

        return config

    def _record(self, message: str, complexity: str, config: dict) -> None:
        """记录分类历史。"""
        self._history.append({
            "message": message[:100],
            "complexity": complexity,
            "config": {k: v for k, v in config.items() if k != "description"},
            "timestamp": __import__("time").time(),
        })
        # 保持历史大小
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

    def get_stats(self) -> dict:
        """获取分类统计。"""
        if not self._history:
            return {"total": 0, "distribution": {}}

        from collections import Counter
        dist = Counter(h["complexity"] for h in self._history)
        return {
            "total": len(self._history),
            "distribution": dict(dist),
            "recent": self._history[-10:],
        }


# 全局实例
_task_scaler: Optional[TaskScaler] = None


def get_scaler() -> TaskScaler:
    """获取全局 TaskScaler 实例。"""
    global _task_scaler
    if _task_scaler is None:
        _task_scaler = TaskScaler()
    return _task_scaler


def classify_task(message: str) -> str:
    """便捷函数:分类任务复杂度。"""
    return get_scaler().classify(message)


def scale_task(message: str, override: Optional[dict] = None) -> dict:
    """便捷函数:获取缩放后的配置。"""
    return get_scaler().scale(message, override)
