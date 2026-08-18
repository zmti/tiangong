"""
设备监控系统 - 数据模型
定义设备、告警等核心数据结构
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import json


class AlertLevel(Enum):
    """告警级别"""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class DeviceStatus(Enum):
    """设备状态"""
    ONLINE = "online"
    OFFLINE = "offline"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class DeviceMetrics:
    """设备指标数据"""
    device_id: str
    timestamp: datetime
    cpu_usage: float = 0.0
    memory_usage: float = 0.0
    disk_usage: float = 0.0
    network_in: float = 0.0  # KB/s
    network_out: float = 0.0  # KB/s
    load_average: float = 0.0
    
    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "timestamp": self.timestamp.isoformat(),
            "cpu_usage": round(self.cpu_usage, 2),
            "memory_usage": round(self.memory_usage, 2),
            "disk_usage": round(self.disk_usage, 2),
            "network_in": round(self.network_in, 2),
            "network_out": round(self.network_out, 2),
            "load_average": round(self.load_average, 2)
        }


@dataclass
class Alert:
    """告警信息"""
    alert_id: str
    device_id: str
    alert_level: AlertLevel
    metric_name: str
    current_value: float
    threshold: float
    message: str
    timestamp: datetime
    acknowledged: bool = False
    
    def to_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "device_id": self.device_id,
            "alert_level": self.alert_level.value,
            "metric_name": self.metric_name,
            "current_value": round(self.current_value, 2),
            "threshold": round(self.threshold, 2),
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "acknowledged": self.acknowledged
        }


@dataclass
class Device:
    """设备信息"""
    device_id: str
    name: str
    ip_address: str
    device_type: str  # server, desktop, network, etc.
    status: DeviceStatus = DeviceStatus.OFFLINE
    last_seen: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.now)
    metrics_history: list = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "ip_address": self.ip_address,
            "device_type": self.device_type,
            "status": self.status.value,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "created_at": self.created_at.isoformat(),
            "metrics_history": [m.to_dict() for m in self.metrics_history[-10:]]  # 只保留最近10条
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Device':
        device = cls(
            device_id=data["device_id"],
            name=data["name"],
            ip_address=data["ip_address"],
            device_type=data["device_type"],
            status=DeviceStatus(data.get("status", "offline"))
        )
        if data.get("last_seen"):
            device.last_seen = datetime.fromisoformat(data["last_seen"])
        if data.get("created_at"):
            device.created_at = datetime.fromisoformat(data["created_at"])
        return device


@dataclass
class MonitorConfig:
    """监控配置"""
    alert_thresholds: dict = field(default_factory=lambda: {
        "cpu": {"warning": 70, "critical": 90},
        "memory": {"warning": 75, "critical": 90},
        "disk": {"warning": 80, "critical": 95}
    })
    alert_cooldown: int = 300  # 秒
    monitor_interval: int = 1  # 秒
    
    def to_dict(self) -> dict:
        return {
            "alert_thresholds": self.alert_thresholds,
            "alert_cooldown": self.alert_cooldown,
            "monitor_interval": self.monitor_interval
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'MonitorConfig':
        return cls(
            alert_thresholds=data.get("alert_thresholds", cls.alert_thresholds),
            alert_cooldown=data.get("alert_cooldown", cls.alert_cooldown),
            monitor_interval=data.get("monitor_interval", cls.monitor_interval)
        )
