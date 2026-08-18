"""
设备监控系统 - 数据采集模块
负责采集本地或远程设备的性能指标
"""

import psutil
import platform
import socket
import time
from datetime import datetime
from typing import List, Dict, Optional

from models import Device, DeviceMetrics, DeviceStatus


class DataCollector:
    """数据采集器"""
    
    def __init__(self):
        self.local_hostname = platform.node()
        self.local_ip = self._get_local_ip()
    
    def _get_local_ip(self) -> str:
        """获取本地IP地址"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"
    
    def collect_local_metrics(self) -> DeviceMetrics:
        """采集本地设备指标"""
        # CPU使用率
        cpu_usage = psutil.cpu_percent(interval=0.1)
        
        # 内存使用率
        memory = psutil.virtual_memory()
        memory_usage = memory.percent
        
        # 磁盘使用率
        disk = psutil.disk_usage('/')
        disk_usage = disk.percent
        
        # 网络统计
        net_io = psutil.net_io_counters()
        network_in = net_io.bytes_recv / 1024  # KB
        network_out = net_io.bytes_sent / 1024  # KB
        
        # 负载平均（Linux/Mac）
        try:
            load_avg = os.getloadavg()[0] if hasattr(os, 'getloadavg') else 0.0
        except:
            load_avg = 0.0
        
        return DeviceMetrics(
            device_id="local",
            timestamp=datetime.now(),
            cpu_usage=cpu_usage,
            memory_usage=memory_usage,
            disk_usage=disk_usage,
            network_in=network_in,
            network_out=network_out,
            load_average=load_avg
        )
    
    def collect_remote_metrics(self, device: Device) -> Optional[DeviceMetrics]:
        """
        采集远程设备指标
        这里使用模拟数据，实际应用中可以通过SSH、SNMP等协议采集
        """
        # 模拟远程设备数据
        import random
        
        # 模拟数据波动
        base_cpu = random.uniform(20, 60)
        base_memory = random.uniform(30, 70)
        base_disk = random.uniform(40, 80)
        
        # 根据设备类型调整基线
        if device.device_type == "server":
            base_cpu += 20
            base_memory += 10
        elif device.device_type == "desktop":
            base_cpu -= 10
        
        # 添加随机波动
        cpu_usage = max(0, min(100, base_cpu + random.uniform(-10, 10)))
        memory_usage = max(0, min(100, base_memory + random.uniform(-5, 5)))
        disk_usage = max(0, min(100, base_disk + random.uniform(-2, 2)))
        
        return DeviceMetrics(
            device_id=device.device_id,
            timestamp=datetime.now(),
            cpu_usage=cpu_usage,
            memory_usage=memory_usage,
            disk_usage=disk_usage,
            network_in=random.uniform(10, 100),
            network_out=random.uniform(5, 50),
            load_average=random.uniform(0, 2)
        )
    
    def ping_device(self, ip_address: str, timeout: int = 2) -> bool:
        """检查设备是否在线"""
        import subprocess
        import sys
        
        if sys.platform == "win32":
            param = "-n"
        else:
            param = "-c"
        
        try:
            result = subprocess.run(
                ["ping", param, "1", ip_address],
                capture_output=True,
                timeout=timeout
            )
            return result.returncode == 0
        except:
            return False
    
    def update_device_status(self, device: Device) -> Device:
        """更新设备状态"""
        device.last_seen = datetime.now()
        
        # 检查设备是否在线
        is_online = self.ping_device(device.ip_address)
        
        if is_online:
            device.status = DeviceStatus.ONLINE
        else:
            device.status = DeviceStatus.OFFLINE
        
        return device


# 导入os模块
import os
