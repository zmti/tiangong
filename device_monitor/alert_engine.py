"""
设备监控系统 - 告警引擎
负责检查阈值并生成告警
"""

import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from collections import defaultdict

from models import (
    Device, DeviceMetrics, Alert, AlertLevel, DeviceStatus,
    MonitorConfig
)


class AlertEngine:
    """告警引擎"""
    
    def __init__(self, config: MonitorConfig):
        self.config = config
        self.alerts: List[Alert] = []
        self.last_alert_time: Dict[str, datetime] = defaultdict(datetime.min)
        self.max_alerts = 1000  # 最多保留的告警数
    
    def check_metrics(self, device: Device, metrics: DeviceMetrics) -> List[Alert]:
        """
        检查设备指标是否触发告警
        返回新生成的告警列表
        """
        new_alerts = []
        
        # 检查CPU
        cpu_alert = self._check_threshold(
            device, metrics, "cpu_usage", metrics.cpu_usage,
            self.config.alert_thresholds.get("cpu", {})
        )
        if cpu_alert:
            new_alerts.append(cpu_alert)
        
        # 检查内存
        mem_alert = self._check_threshold(
            device, metrics, "memory_usage", metrics.memory_usage,
            self.config.alert_thresholds.get("memory", {})
        )
        if mem_alert:
            new_alerts.append(mem_alert)
        
        # 检查磁盘
        disk_alert = self._check_threshold(
            device, metrics, "disk_usage", metrics.disk_usage,
            self.config.alert_thresholds.get("disk", {})
        )
        if disk_alert:
            new_alerts.append(disk_alert)
        
        # 更新设备状态
        self._update_device_status(device, new_alerts)
        
        # 添加新告警
        self.alerts.extend(new_alerts)
        
        # 清理旧告警
        self._cleanup_alerts()
        
        return new_alerts
    
    def _check_threshold(
        self,
        device: Device,
        metrics: DeviceMetrics,
        metric_name: str,
        current_value: float,
        thresholds: dict
    ) -> Optional[Alert]:
        """检查单个指标是否超过阈值"""
        # 检查冷却时间
        alert_key = f"{device.device_id}_{metric_name}"
        now = datetime.now()
        
        if (now - self.last_alert_time[alert_key]).total_seconds() < self.config.alert_cooldown:
            return None
        
        # 确定告警级别
        alert_level = None
        threshold_value = 0
        
        if current_value >= thresholds.get("critical", 100):
            alert_level = AlertLevel.CRITICAL
            threshold_value = thresholds["critical"]
        elif current_value >= thresholds.get("warning", 80):
            alert_level = AlertLevel.WARNING
            threshold_value = thresholds["warning"]
        
        if alert_level is None:
            return None
        
        # 生成告警消息
        metric_names = {
            "cpu_usage": "CPU使用率",
            "memory_usage": "内存使用率",
            "disk_usage": "磁盘使用率"
        }
        
        message = f"{metric_names.get(metric_name, metric_name)}达到{current_value:.1f}%，超过阈值{threshold_value}%"
        
        # 创建告警
        alert = Alert(
            alert_id=str(uuid.uuid4())[:8],
            device_id=device.device_id,
            alert_level=alert_level,
            metric_name=metric_name,
            current_value=current_value,
            threshold=threshold_value,
            message=message,
            timestamp=now
        )
        
        # 更新最后告警时间
        self.last_alert_time[alert_key] = now
        
        return alert
    
    def _update_device_status(self, device: Device, new_alerts: List[Alert]):
        """根据告警更新设备状态"""
        if not new_alerts:
            # 没有新告警，恢复在线状态
            device.status = DeviceStatus.ONLINE
            return
        
        # 检查是否有严重告警
        has_critical = any(a.alert_level == AlertLevel.CRITICAL for a in new_alerts)
        has_warning = any(a.alert_level == AlertLevel.WARNING for a in new_alerts)
        
        if has_critical:
            device.status = DeviceStatus.CRITICAL
        elif has_warning:
            device.status = DeviceStatus.WARNING
        else:
            device.status = DeviceStatus.ONLINE
    
    def _cleanup_alerts(self):
        """清理旧告警，保留最新的"""
        if len(self.alerts) > self.max_alerts:
            # 按时间排序，保留最新的
            self.alerts.sort(key=lambda x: x.timestamp, reverse=True)
            self.alerts = self.alerts[:self.max_alerts]
    
    def get_alerts(
        self,
        device_id: Optional[str] = None,
        alert_level: Optional[AlertLevel] = None,
        limit: int = 50
    ) -> List[Alert]:
        """获取告警列表"""
        alerts = self.alerts
        
        if device_id:
            alerts = [a for a in alerts if a.device_id == device_id]
        
        if alert_level:
            alerts = [a for a in alerts if a.alert_level == alert_level]
        
        # 按时间倒序
        alerts.sort(key=lambda x: x.timestamp, reverse=True)
        
        return alerts[:limit]
    
    def acknowledge_alert(self, alert_id: str) -> bool:
        """确认告警"""
        for alert in self.alerts:
            if alert.alert_id == alert_id:
                alert.acknowledged = True
                return True
        return False
    
    def get_alert_summary(self) -> Dict[str, int]:
        """获取告警统计摘要"""
        summary = {
            "total": len(self.alerts),
            "active": 0,
            "warning": 0,
            "critical": 0,
            "acknowledged": 0
        }
        
        for alert in self.alerts:
            if not alert.acknowledged:
                summary["active"] += 1
            if alert.alert_level == AlertLevel.WARNING:
                summary["warning"] += 1
            elif alert.alert_level == AlertLevel.CRITICAL:
                summary["critical"] += 1
            if alert.acknowledged:
                summary["acknowledged"] += 1
        
        return summary
