# 设备监控系统配置

# 监控间隔（秒）
MONITOR_INTERVAL = 1

# 告警阈值配置
ALERT_THRESHOLDS = {
    "cpu": {
        "warning": 70,      # CPU使用率超过70%警告
        "critical": 90      # CPU使用率超过90%严重告警
    },
    "memory": {
        "warning": 75,      # 内存使用率超过75%警告
        "critical": 90      # 内存使用率超过90%严重告警
    },
    "disk": {
        "warning": 80,      # 磁盘使用率超过80%警告
        "critical": 95      # 磁盘使用率超过95%严重告警
    }
}

# 告警冷却时间（秒）- 避免频繁告警
ALERT_COOLDOWN = 300

# 数据存储路径
DATA_DIR = "data"
ALERT_LOG = "data/alerts.log"
DEVICE_STATE_FILE = "data/devices.json"

# Web界面配置
WEB_HOST = "0.0.0.0"
WEB_PORT = 5000

# 日志配置
LOG_LEVEL = "INFO"
LOG_FILE = "data/monitor.log"
