"""Agent Collaboration Marketplace - 智能体协作市场

功能:
- 智能体发现/注册:按能力、角色、标签检索
- 智能体评价/评分:用户反馈、成功率、成本效益
- 智能体组合推荐:根据任务自动推荐最佳团队配置
- 智能体版本管理:发布、回滚、A/B 测试
- 智能体链路追踪:多智能体协作的可观测性
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Callable, Awaitable

import logging

logger = logging.getLogger(__name__)


class AgentCapability(Enum):
    """智能体能力标签"""
    RESEARCH = "research"           # 调研/信息收集
    CODING = "coding"               # 代码编写/修改
    CODE_REVIEW = "code_review"     # 代码审查
    DATA_ANALYSIS = "data_analysis" # 数据分析/可视化
    WRITING = "writing"             # 写作/文档
    SECURITY = "security"           # 安全审查
    TESTING = "testing"             # 测试/QA
    DEVOPS = "devops"               # 部署/运维
    PLANNING = "planning"           # 任务规划
    COORDINATION = "coordination"   # 协调/管理
    TRANSLATION = "translation"     # 翻译/本地化
    DESIGN = "design"               # 设计/架构
    CUSTOM = "custom"               # 自定义能力


class AgentStatus(Enum):
    """智能体状态"""
    DRAFT = "draft"                 # 草稿/未发布
    PUBLISHED = "published"         # 已发布/可用
    DEPRECATED = "deprecated"       # 已弃用
    ARCHIVED = "archived"           # 已归档


@dataclass
class AgentSpec:
    """智能体规格 - 定义智能体的能力边界"""
    name: str
    version: str = "1.0.0"
    description: str = ""
    capabilities: List[AgentCapability] = field(default_factory=list)
    # 输入/输出契约
    input_schema: Dict = field(default_factory=dict)   # JSON Schema
    output_schema: Dict = field(default_factory=dict)  # JSON Schema
    # 运行约束
    max_steps: int = 10
    timeout_seconds: float = 300.0
    max_cost_usd: float = 1.0
    # 依赖
    required_tools: List[str] = field(default_factory=list)
    required_models: List[str] = field(default_factory=list)  # ["provider:model"]
    # 兼容性
    compatible_agents: List[str] = field(default_factory=list)  # 可协作的智能体名
    # 元数据
    author: str = ""
    tags: List[str] = field(default_factory=list)
    docs_url: str = ""
    
    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "capabilities": [c.value for c in self.capabilities],
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "max_steps": self.max_steps,
            "timeout_seconds": self.timeout_seconds,
            "max_cost_usd": self.max_cost_usd,
            "required_tools": self.required_tools,
            "required_models": self.required_models,
            "compatible_agents": self.compatible_agents,
            "author": self.author,
            "tags": self.tags,
            "docs_url": self.docs_url,
        }


@dataclass
class AgentRating:
    """智能体评价"""
    agent_name: str
    version: str
    rater_id: str
    score: float                    # 1-5
    cost_score: float = 0           # 成本效益 1-5
    quality_score: float = 0        # 质量 1-5
    speed_score: float = 0          # 速度 1-5
    reliability_score: float = 0    # 可靠性 1-5
    comment: str = ""
    task_type: str = ""             # 任务类型
    success: bool = True
    timestamp: float = field(default_factory=time.time)
    
    def to_dict(self) -> Dict:
        return {
            "agent_name": self.agent_name,
            "version": self.version,
            "rater_id": self.rater_id,
            "score": self.score,
            "cost_score": self.cost_score,
            "quality_score": self.quality_score,
            "speed_score": self.speed_score,
            "reliability_score": self.reliability_score,
            "comment": self.comment,
            "task_type": self.task_type,
            "success": self.success,
            "timestamp": self.timestamp,
        }


@dataclass(eq=True, frozen=False)
class AgentRecord:
    """智能体注册记录"""
    spec: AgentSpec
    status: AgentStatus = AgentStatus.DRAFT
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    published_at: Optional[float] = None
    # 统计
    total_runs: int = 0
    successful_runs: int = 0
    total_cost_usd: float = 0.0
    avg_duration_seconds: float = 0.0
    # 评价
    ratings: List[AgentRating] = field(default_factory=list)
    # 版本历史
    version_history: List[Dict] = field(default_factory=list)
    
    def __hash__(self):
        return hash((self.spec.name, self.spec.version))
    
    def __eq__(self, other):
        if not isinstance(other, AgentRecord):
            return False
        return self.spec.name == other.spec.name and self.spec.version == other.spec.version
    
    def to_dict(self) -> Dict:
        d = asdict(self)
        d["spec"] = self.spec.to_dict()
        d["status"] = self.status.value
        d["ratings"] = [r.to_dict() for r in self.ratings]
        return d
    
    @property
    def success_rate(self) -> float:
        if self.total_runs == 0:
            return 0.0
        return self.successful_runs / self.total_runs
    
    @property
    def avg_rating(self) -> float:
        if not self.ratings:
            return 0.0
        return sum(r.score for r in self.ratings) / len(self.ratings)
    
    @property
    def weighted_score(self) -> float:
        """综合评分: 成功率 * 0.4 + 平均评分 * 0.4 + 成本效益 * 0.2"""
        cost_score = sum(r.cost_score for r in self.ratings) / len(self.ratings) if self.ratings else 0
        return self.success_rate * 0.4 + (self.avg_rating / 5.0) * 0.4 + (cost_score / 5.0) * 0.2


@dataclass
class TeamComposition:
    """团队组合推荐"""
    name: str
    description: str
    agents: List[Dict[str, Any]]  # [{"name": "", "role": "", "weight": 1.0}]
    estimated_cost_usd: float = 0.0
    estimated_duration_seconds: float = 0.0
    success_probability: float = 0.0
    compatible: bool = True
    
    def to_dict(self) -> Dict:
        return asdict(self)


class AgentRegistry:
    """智能体注册中心 - 市场核心"""
    
    def __init__(self):
        self._agents: Dict[str, AgentRecord] = {}  # name -> record
        self._by_capability: Dict[AgentCapability, Set[str]] = defaultdict(set)
        self._by_tag: Dict[str, Set[str]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._rating_lock = asyncio.Lock()
    
    async def register(self, spec: AgentSpec, author: str = "") -> AgentRecord:
        """注册新智能体"""
        async with self._lock:
            key = f"{spec.name}:{spec.version}"
            if key in self._agents:
                raise ValueError(f"Agent {spec.name} v{spec.version} already exists")
            
            record = AgentRecord(
                spec=spec,
                status=AgentStatus.PUBLISHED,
            )
            spec.author = author
            self._agents[key] = record
            self._index_agent(record)
            logger.info(f"Registered agent: {spec.name} v{spec.version}")
            return record
    
    def _index_agent(self, record: AgentRecord) -> None:
        for cap in record.spec.capabilities:
            self._by_capability[cap].add(record.spec.name)
        for tag in record.spec.tags:
            self._by_tag[tag].add(record.spec.name)
    
    async def unregister(self, name: str, version: str = "latest") -> bool:
        async with self._lock:
            key = f"{name}:{version}" if version != "latest" else f"{name}:{self._get_latest_version(name)}"
            if key not in self._agents:
                return False
            record = self._agents.pop(key)
            self._unindex_agent(record)
            return True
    
    def _unindex_agent(self, record: AgentRecord) -> None:
        for cap in record.spec.capabilities:
            self._by_capability[cap].discard(record.spec.name)
        for tag in record.spec.tags:
            self._by_tag[tag].discard(record.spec.name)
    
    def _get_latest_version(self, name: str) -> str:
        versions = []
        for key, record in self._agents.items():
            if record.spec.name == name:
                versions.append(record.spec.version)
        if not versions:
            return "1.0.0"
        # 简单版本排序
        return max(versions, key=lambda v: [int(x) for x in v.split(".")])
    
    def get(self, name: str, version: str = "latest") -> Optional[AgentRecord]:
        key = f"{name}:{version}" if version != "latest" else f"{name}:{self._get_latest_version(name)}"
        return self._agents.get(key)
    
    def search(
        self,
        capabilities: Optional[List[AgentCapability]] = None,
        tags: Optional[List[str]] = None,
        status: Optional[AgentStatus] = None,
        min_rating: float = 0.0,
        min_success_rate: float = 0.0,
        max_cost_usd: Optional[float] = None,
        query: str = "",
    ) -> List[AgentRecord]:
        """搜索智能体"""
        candidates = set(self._agents.values())
        
        if status:
            candidates = {r for r in candidates if r.status == status}
        
        if capabilities:
            cap_names = set()
            for cap in capabilities:
                cap_names.update(self._by_capability.get(cap, set()))
            candidates = candidates & {r for r in candidates if r.spec.name in cap_names}
        
        if tags:
            tag_names = set()
            for tag in tags:
                tag_names.update(self._by_tag.get(tag, set()))
            candidates = candidates & {r for r in candidates if r.spec.name in tag_names}
        
        if query:
            query_lower = query.lower()
            candidates = {r for r in candidates if 
                query_lower in r.spec.name.lower() or 
                query_lower in r.spec.description.lower()}
        
        results = []
        for record in candidates:
            if record.avg_rating < min_rating:
                continue
            if record.success_rate < min_success_rate:
                continue
            if max_cost_usd is not None and record.spec.max_cost_usd > max_cost_usd:
                continue
            results.append(record)
        
        # 按综合评分排序
        results.sort(key=lambda r: r.weighted_score, reverse=True)
        return results
    
    async def add_rating(self, rating: AgentRating) -> None:
        async with self._rating_lock:
            key = f"{rating.agent_name}:{rating.version}"
            if key in self._agents:
                self._agents[key].ratings.append(rating)
    
    async def update_status(self, name: str, version: str, status: AgentStatus) -> bool:
        async with self._lock:
            key = f"{name}:{version}"
            if key in self._agents:
                self._agents[key].status = status
                self._agents[key].updated_at = time.time()
                if status == AgentStatus.PUBLISHED:
                    self._agents[key].published_at = time.time()
                return True
            return False
    
    def list_all(self, status: Optional[AgentStatus] = None) -> List[AgentRecord]:
        agents = list(self._agents.values())
        if status:
            agents = [r for r in agents if r.status == status]
        return sorted(agents, key=lambda r: r.updated_at, reverse=True)
    
    def get_statistics(self) -> Dict:
        """获取市场统计"""
        total = len(self._agents)
        published = sum(1 for r in self._agents.values() if r.status == AgentStatus.PUBLISHED)
        by_capability = {cap.value: len(agents) for cap, agents in self._by_capability.items()}
        total_runs = sum(r.total_runs for r in self._agents.values())
        total_cost = sum(r.total_cost_usd for r in self._agents.values())
        
        return {
            "total_agents": total,
            "published": published,
            "by_capability": by_capability,
            "total_runs": total_runs,
            "total_cost_usd": total_cost,
        }


class TeamComposer:
    """智能体团队组合器 - 根据任务推荐最佳团队"""
    
    def __init__(self, registry: AgentRegistry):
        self.registry = registry
    
    async def recommend_team(
        self,
        task_description: str,
        required_capabilities: List[AgentCapability],
        team_size: int = 3,
        max_cost_usd: float = 5.0,
        preferred_agents: Optional[List[str]] = None,
    ) -> List[TeamComposition]:
        """根据任务推荐团队组合"""
        
        # 搜索可用智能体
        candidates = self.registry.search(
            capabilities=required_capabilities,
            status=AgentStatus.PUBLISHED,
            max_cost_usd=max_cost_usd / team_size if team_size > 0 else max_cost_usd,
        )
        
        if not candidates:
            return []
        
        # 优先考虑偏好智能体
        if preferred_agents:
            preferred = [c for c in candidates if c.spec.name in preferred_agents]
            others = [c for c in candidates if c.spec.name not in preferred_agents]
            candidates = preferred + others
        
        # 生成组合
        compositions = []
        
        # 简单策略：选择综合评分最高的 N 个互补智能体
        for i in range(min(3, len(candidates) // max(1, team_size) + 1)):
            team = candidates[i:i + team_size]
            if len(team) < 2:
                continue
            
            comp = self._build_composition(team, required_capabilities)
            if comp and comp.estimated_cost_usd <= max_cost_usd:
                compositions.append(comp)
        
        # 按综合评分排序
        compositions.sort(key=lambda c: c.success_probability * (1.0 - c.estimated_cost_usd / max(0.01, max_cost_usd)), reverse=True)
        
        return compositions[:5]
    
    def _build_composition(
        self, 
        agents: List[AgentRecord], 
        required_caps: List[AgentCapability],
    ) -> Optional[TeamComposition]:
        if not agents:
            return None
        
        # 检查能力覆盖
        all_caps = set()
        for a in agents:
            all_caps.update(a.spec.capabilities)
        
        coverage = len(all_caps & set(required_caps)) / max(1, len(required_caps))
        
        # 计算成本和时长
        total_cost = sum(a.spec.max_cost_usd for a in agents)
        total_duration = sum(a.spec.timeout_seconds for a in agents)
        
        # 成功概率估算
        avg_success = sum(a.success_rate for a in agents) / len(agents)
        avg_rating = sum(a.avg_rating for a in agents) / len(agents)
        success_prob = (avg_success + avg_rating / 5.0 + coverage) / 3.0
        
        # 检查兼容性
        compatible = True
        for a in agents:
            for other in agents:
                if a != other and a.spec.name not in other.spec.compatible_agents:
                    compatible = False
        
        return TeamComposition(
            name=f"team_{'_'.join(a.spec.name for a in agents)}",
            description=f"由 {len(agents)} 个智能体组成的团队",
            agents=[{
                "name": a.spec.name,
                "version": a.spec.version,
                "role": a.spec.capabilities[0].value if a.spec.capabilities else "general",
                "weight": a.weighted_score,
            } for a in agents],
            estimated_cost_usd=total_cost,
            estimated_duration_seconds=total_duration,
            success_probability=success_prob,
            compatible=compatible,
        )


class AgentMarketplace:
    """智能体市场 - 高层 API"""
    
    def __init__(self):
        self.registry = AgentRegistry()
        self.composer = TeamComposer(self.registry)
        self._event_handlers: Dict[str, List[Callable]] = defaultdict(list)
    
    # ===== 注册/管理 =====
    
    async def publish_agent(
        self,
        spec: AgentSpec,
        author: str = "",
    ) -> AgentRecord:
        """发布智能体到市场"""
        record = await self.registry.register(spec, author)
        await self._emit("agent_published", record.to_dict())
        return record
    
    async def unpublish_agent(self, name: str, version: str) -> bool:
        success = await self.registry.unregister(name, version)
        if success:
            await self._emit("agent_unpublished", {"name": name, "version": version})
        return success
    
    async def deprecate_agent(self, name: str, version: str) -> bool:
        return await self.registry.update_status(name, version, AgentStatus.DEPRECATED)
    
    # ===== 发现/搜索 =====
    
    def search_agents(
        self,
        capabilities: Optional[List[AgentCapability]] = None,
        tags: Optional[List[str]] = None,
        min_rating: float = 0.0,
        min_success_rate: float = 0.0,
        max_cost_usd: Optional[float] = None,
        query: str = "",
    ) -> List[AgentRecord]:
        return self.registry.search(
            capabilities=capabilities,
            tags=tags,
            status=AgentStatus.PUBLISHED,
            min_rating=min_rating,
            min_success_rate=min_success_rate,
            max_cost_usd=max_cost_usd,
            query=query,
        )
    
    def get_agent(self, name: str, version: str = "latest") -> Optional[AgentRecord]:
        return self.registry.get(name, version)
    
    def list_agents(self, status: Optional[AgentStatus] = None) -> List[AgentRecord]:
        return self.registry.list_all(status)
    
    # ===== 评价/反馈 =====
    
    async def rate_agent(
        self,
        agent_name: str,
        version: str,
        rater_id: str,
        score: float,
        cost_score: float = 0,
        quality_score: float = 0,
        speed_score: float = 0,
        reliability_score: float = 0,
        comment: str = "",
        task_type: str = "",
        success: bool = True,
    ) -> None:
        rating = AgentRating(
            agent_name=agent_name,
            version=version,
            rater_id=rater_id,
            score=score,
            cost_score=cost_score,
            quality_score=quality_score,
            speed_score=speed_score,
            reliability_score=reliability_score,
            comment=comment,
            task_type=task_type,
            success=success,
        )
        await self.registry.add_rating(rating)
        await self._emit("agent_rated", rating.to_dict())
    
    # ===== 团队推荐 =====
    
    async def recommend_team(
        self,
        task_description: str,
        required_capabilities: List[AgentCapability],
        team_size: int = 3,
        max_cost_usd: float = 5.0,
        preferred_agents: Optional[List[str]] = None,
    ) -> List[TeamComposition]:
        return await self.composer.recommend_team(
            task_description,
            required_capabilities,
            team_size,
            max_cost_usd,
            preferred_agents,
        )
    
    # ===== 事件系统 =====
    
    def on(self, event: str, handler: Callable) -> None:
        self._event_handlers[event].append(handler)
    
    async def _emit(self, event: str, data: Dict) -> None:
        for handler in self._event_handlers.get(event, []):
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(data)
                else:
                    handler(data)
            except Exception as e:
                logger.error(f"Event handler error for {event}: {e}")


# ===== 全局市场实例 =====

_global_marketplace: Optional[AgentMarketplace] = None
_marketplace_lock = asyncio.Lock()


async def get_marketplace() -> AgentMarketplace:
    """获取全局智能体市场实例"""
    global _global_marketplace
    if _global_marketplace is None:
        async with _marketplace_lock:
            if _global_marketplace is None:
                _global_marketplace = AgentMarketplace()
    return _global_marketplace


# ===== 便捷函数 =====

async def publish_agent(
    name: str,
    version: str,
    description: str,
    capabilities: List[AgentCapability],
    author: str = "",
    **kwargs,
) -> AgentRecord:
    """快速发布智能体"""
    spec = AgentSpec(
        name=name,
        version=version,
        description=description,
        capabilities=capabilities,
        **kwargs,
    )
    marketplace = await get_marketplace()
    return await marketplace.publish_agent(spec, author)


async def find_agents(
    capabilities: List[AgentCapability],
    **kwargs,
) -> List[AgentRecord]:
    """快速搜索智能体"""
    marketplace = await get_marketplace()
    return marketplace.search_agents(capabilities=capabilities, **kwargs)


async def recommend_team_for_task(
    task_description: str,
    required_capabilities: List[AgentCapability],
    **kwargs,
) -> List[TeamComposition]:
    """为任务推荐团队"""
    marketplace = await get_marketplace()
    return await marketplace.recommend_team(
        task_description,
        required_capabilities,
        **kwargs,
    )


# ===== 导出 =====

__all__ = [
    "AgentCapability",
    "AgentStatus",
    "AgentSpec",
    "AgentRating",
    "AgentRecord",
    "TeamComposition",
    "AgentRegistry",
    "TeamComposer",
    "AgentMarketplace",
    "get_marketplace",
    "publish_agent",
    "find_agents",
    "recommend_team_for_task",
]