"""
设备监控系统 - 监控主循环
负责定期采集数据并触发告警检查
"""

import time
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional
from pathlib import Path

from models import Device, DeviceMetrics, MonitorConfig, DeviceStatus
from collector import DataCollector
from alert_engine import AlertEngine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Monitor:
    """监控主控制器"""
    
    def __init__(self, config: MonitorConfig):
        self.config = config
        self.collector = DataCollector()
        self.alert_engine = AlertEngine(config)
        self.devices: Dict[str, Device] = {}
        self.is_running = False
        self._data_dir = Path("data")
        self._data_dir.mkdir(exist_ok=True)
        
        # 加载设备列表
        self._load_devices()
    
    def _load_devices(self):
        """从文件加载设备列表"""
        device_file = self._data_dir / "devices.json"
        if device_file.exists():
            try:
                with open(device_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for device_data in data:
                        device = Device.from_dict(device_data)
                        self.devices[device.device_id] = device
                logger.info(f"已加载 {len(self.devices)} 个设备")
            except Exception as e:
                logger.error(f"加载设备失败: {e}")
    
    def _save_devices(self):
        """保存设备列表到文件"""
        device_file = self._data_dir / "devices.json"
        try:
            data = [d.to_dict() for d in self.devices.values()]
            with open(device_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存设备失败: {e}")
    
    def add_device(self, device: Device):
        """添加设备"""
        self.devices[device.device_id] = device
        self._save_devices()
        logger.info(f"添加设备: {device.name} ({device.ip_address})")
    
    def remove_device(self, device_id: str):
        """移除设备"""
        if device_id in self.devices:
            del self.devices[device_id]
            self._save_devices()
            logger.info(f"移除设备: {device_id}")
    
    def get_device(self, device_id: str) -> Optional[Device]:
        """获取设备"""
        return self.devices.get(device_id)
    
    def get_all_devices(self) -> List[Device]:
        """获取所有设备"""
        return list(self.devices.values())
    
    def run_once(self):
        """执行一次监控循环"""
        logger.info("开始监控循环...")
        
        # 采集本地数据
        local_metrics = self.collector.collect_local_metrics()
        logger.info(f"本地指标: CPU={local_metrics.cpu_usage:.1f}%, "
                   f"内存={local_metrics.memory_usage:.1f}%, "
                   f"磁盘={local_metrics.disk_usage:.1f}%")
        
        # 检查并记录本地告警
        # 创建一个虚拟的本地设备
        local_device = Device(
            device_id="local",
            name="本地服务器",
            ip_address=self.collector.local_ip,
            device_type="server"
        )
        alerts = self.alert_engine.check_metrics(local_device, local_metrics)
        
        if alerts:
            for alert in alerts:
                logger.warning(f"告警 [{alert.alert_level.value}]: {alert.message}")
        
        # 采集远程设备数据
        for device_id, device in self.devices.items():
            if device.device_id == "local":
                continue
            
            try:
                # 更新设备状态
                device = self.collector.update_device_status(device)
                
                if device.status == DeviceStatus.ONLINE:
                    # 采集指标
                    metrics = self.collector.collect_remote_metrics(device)
                    
                    # 检查告警
                    alerts = self.alert_engine.check_metrics(device, metrics)
                    
                    if alerts:
                        for alert in alerts:
                            logger.warning(f"设备 [{device.name}] 告警 [{alert.alert_level.value}]: {alert.message}")
                    
                    # 保存指标历史
                    device.metrics_history.append(metrics)
                    if len(device.metrics_history) > 100:
                        device.metrics_history = device.metrics_history[-100:]
                else:
                    logger.info(f"设备 [{device.name}] 离线")
                    
            except Exception as e:
                logger.error(f"采集设备 [{device.name}] 数据失败: {e}")
        
        # 保存设备状态
        self._save_devices()
        
        logger.info("监控循环完成")
    
    def run(self):
        """运行监控服务"""
        self.is_running = True
        logger.info("启动设备监控系统...")
        logger.info(f"监控间隔: {self.config.monitor_interval}秒")
        logger.info(f"设备数量: {len(self.devices)}")
        
        while self.is_running:
            try:
                self.run_once()
                time.sleep(self.config.monitor_interval)
            except KeyboardInterrupt:
                logger.info("收到中断信号，停止监控")
                break
            except Exception as e:
                logger.error(f"监控循环出错: {e}", exc_info=True)
                time.sleep(self.config.monitor_interval)
        
        self.is_running = False
        logger.info("监控服务已停止")
    
    def stop(self):
        """停止监控"""
        self.is_running = False
    
    def get_status(self) -> Dict:
        """获取监控状态"""
        return {
            "is_running": self.is_running,
            "device_count": len(self.devices),
            "alert_summary": self.alert_engine.get_alert_summary(),
            "last_update": datetime.now().isoformat()
        }
