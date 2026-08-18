"""
设备监控系统 - 主入口
"""

import argparse
import logging
import signal
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from config import (
    MONITOR_INTERVAL, ALERT_THRESHOLDS, ALERT_COOLDOWN,
    WEB_HOST, WEB_PORT, LOG_LEVEL
)
from models import MonitorConfig
from monitor import Monitor
from web_interface import run_web_interface

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description='设备监控系统')
    parser.add_argument('--mode', choices=['monitor', 'web', 'both'], default='both',
                       help='运行模式: monitor(仅监控), web(仅Web界面), both(两者)')
    parser.add_argument('--interval', type=int, default=MONITOR_INTERVAL,
                       help=f'监控间隔(秒), 默认{MONITOR_INTERVAL}')
    parser.add_argument('--host', default=WEB_HOST,
                       help=f'Web服务器主机, 默认{WEB_HOST}')
    parser.add_argument('--port', type=int, default=WEB_PORT,
                       help=f'Web服务器端口, 默认{WEB_PORT}')
    parser.add_argument('--add-device', nargs=3, metavar=('NAME', 'IP', 'TYPE'),
                       help='启动时添加设备: --add-device "服务器1" 192.168.1.100 server')
    return parser.parse_args()


def main():
    """主函数"""
    args = parse_args()
    
    # 创建配置
    config = MonitorConfig(
        alert_thresholds=ALERT_THRESHOLDS,
        alert_cooldown=ALERT_COOLDOWN,
        monitor_interval=args.interval
    )
    
    # 创建监控器
    monitor = Monitor(config)
    
    # 添加启动设备
    if args.add_device:
        from models import Device
        device = Device(
            device_id=f"device_{len(monitor.devices)}",
            name=args.add_device[0],
            ip_address=args.add_device[1],
            device_type=args.add_device[2]
        )
        monitor.add_device(device)
        logger.info(f"已添加设备: {device.name} ({device.ip_address})")
    
    # 设置信号处理
    def signal_handler(sig, frame):
        logger.info("收到停止信号")
        monitor.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 根据模式运行
    if args.mode in ['monitor', 'both']:
        # 启动监控线程
        import threading
        monitor_thread = threading.Thread(target=monitor.run, daemon=True)
        monitor_thread.start()
        logger.info("监控线程已启动")
    
    if args.mode in ['web', 'both']:
        # 运行Web界面（阻塞）
        run_web_interface(monitor, args.host, args.port)
    elif args.mode == 'monitor':
        # 仅监控模式，保持运行
        try:
            while True:
                import time
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("监控已停止")


if __name__ == '__main__':
    main()
