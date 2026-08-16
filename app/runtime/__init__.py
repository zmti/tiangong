"""Graph 运行时:图式编排 + 可持久化 checkpoint 状态。

对标 LangGraph 的核心抽象,但零第三方依赖:
- AgentState:节点间传递的唯一数据载体,可序列化落盘
- CheckpointManager:SQLite 持久化每个节点的状态快照,支持暂停/恢复/后台运行
- Graph:节点 + 边 + 条件路由 + 并行分支 + 循环(带步数预算),逐节点 checkpoint

用法示例见 app/core/agent.py 的 GraphAgent。
"""
from app.runtime.checkpoint import CheckpointManager
from app.runtime.graph import END, Graph, ParallelNode
from app.runtime.state import AgentState

__all__ = ["AgentState", "CheckpointManager", "Graph", "ParallelNode", "END"]
