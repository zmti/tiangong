"""熔断器状态提升为类属性:跨实例共享,provider 故障对后续请求真正生效。

复现场景:
旧实现 `LLMClient.__init__` 把 `_circuit_fails` / `_circuit_open_until` 挂在
**实例**属性上,但 `chat.py` 每个请求 `new LLMClient()` —— 熔断状态跨请求清零,
provider 真挂了也不会对下一个请求开路。本测试直接验证:
- 连续失败达到阈值 → `_is_circuit_open` 返回 True
- 下一个"新实例"(模拟新请求)看到同一 provider 仍被熔断(跨实例共享)
- 冷却结束后恢复
"""
import time

from app.config import settings
from app.core.llm import LLMClient


def test_circuit_opens_after_threshold_failures():
    settings.agent_llm_circuit_threshold = 3
    settings.agent_llm_circuit_cooldown = 60.0
    provider = "test-provider"

    # 模拟 3 次新实例(对应 3 个请求)各自失败
    for _ in range(3):
        c = LLMClient.__new__(LLMClient)  # 不走 __init__(无 provider 配置校验)
        c.provider = provider
        c.model = "m"
        c._record_failure(provider)

    # 第 4 个"新实例"(下一个请求)应看到熔断已开
    c4 = LLMClient.__new__(LLMClient)
    c4.provider = provider
    c4.model = "m"
    assert c4._is_circuit_open(provider), "连续 3 次失败后,后续实例应看到熔断开启"


def test_circuit_closes_after_cooldown():
    settings.agent_llm_circuit_threshold = 1
    settings.agent_llm_circuit_cooldown = 0.05  # 短冷却,便于测试
    provider = "test-provider-2"

    c = LLMClient.__new__(LLMClient)
    c.provider = provider
    c.model = "m"
    c._record_failure(provider)
    assert c._is_circuit_open(provider), "刚失败应处于冷却期"

    time.sleep(0.1)  # 冷却结束
    assert not c._is_circuit_open(provider), "冷却结束后熔断应恢复关闭"


def test_success_resets_circuit():
    settings.agent_llm_circuit_threshold = 2
    settings.agent_llm_circuit_cooldown = 60.0
    provider = "test-provider-3"

    c = LLMClient.__new__(LLMClient)
    c.provider = provider
    c.model = "m"
    c._record_failure(provider)  # 1 次失败,未到阈值
    c._record_success(provider)   # 成功清零
    c._record_failure(provider)   # 又失败 1 次,仍未到阈值
    assert not c._is_circuit_open(provider), "成功清零后,1 次失败不足以熔断"


def test_threshold_zero_disables_circuit():
    settings.agent_llm_circuit_threshold = 0
    provider = "test-provider-4"

    c = LLMClient.__new__(LLMClient)
    c.provider = provider
    c.model = "m"
    for _ in range(10):
        c._record_failure(provider)
    assert not c._is_circuit_open(provider), "阈值为 0 时熔断关闭,不应触发"
