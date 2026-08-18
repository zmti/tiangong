# 设备监控系统

一个基于Python的设备监控系统，支持实时监控、告警管理和Web界面。

## 功能特性

- 📊 **实时监控**: 自动采集CPU、内存、磁盘等性能指标
- 🔔 **智能告警**: 支持多级告警阈值（警告/严重），带冷却机制
- 🌐 **Web界面**: 直观的实时监控Dashboard
- 📱 **多设备管理**: 支持添加和管理多个监控设备
- 💾 **数据持久化**: 设备配置和告警记录自动保存

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 运行监控

```bash
# 同时运行监控和Web界面
python -m device_monitor.main --mode both

# 仅运行监控
python -m device_monitor.main --mode monitor

# 仅运行Web界面
python -m device_monitor.main --mode web
```

### 3. 访问Web界面

打开浏览器访问: http://localhost:5000

### 4. 添加设备

```bash
# 启动时添加设备
python -m device_monitor.main --add-device "服务器1" 192.168.1.100 server

# 或在Web界面点击"添加设备"
```

## 命令行参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--mode` | 运行模式: monitor/web/both | both |
| `--interval` | 监控间隔(秒) | 1 |
| `--host` | Web服务器主机 | 0.0.0.0 |
| `--port` | Web服务器端口 | 5000 |
| `--add-device` | 启动时添加设备 | - |

## 项目结构

```
device_monitor/
├── __init__.py
├── main.py              # 主入口
├── config.py            # 配置文件
├── models.py            # 数据模型
├── collector.py         # 数据采集
├── alert_engine.py      # 告警引擎
├── monitor.py           # 监控主循环
├── web_interface.py     # Web界面
└── requirements.txt     # 依赖列表
```

## 告警阈值配置

在 `config.py` 中修改告警阈值：

```python
ALERT_THRESHOLDS = {
    "cpu": {"warning": 70, "critical": 90},
    "memory": {"warning": 75, "critical": 90},
    "disk": {"warning": 80, "critical": 95}
}
```

## 数据文件

- `data/devices.json` - 设备配置
- `data/alerts.log` - 告警日志
- `data/monitor.log` - 运行日志

## 注意事项

- 远程设备采集使用模拟数据，实际部署需集成SSH/SNMP等协议
- 默认监控间隔为1秒，可根据需求调整
- 告警冷却时间为300秒（5分钟），避免频繁告警
