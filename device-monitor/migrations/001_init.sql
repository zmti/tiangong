-- 设备监控系统数据库初始化脚本

-- 创建数据库
CREATE DATABASE IF NOT EXISTS device_monitor CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE device_monitor;

-- 设备表
CREATE TABLE IF NOT EXISTS devices (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    type VARCHAR(50) NOT NULL COMMENT '设备类型: server, sensor, camera, router',
    ip_address VARCHAR(45) NOT NULL,
    status ENUM('online', 'offline', 'warning', 'error') DEFAULT 'offline',
    location VARCHAR(200),
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_type (type),
    INDEX idx_status (status),
    INDEX idx_ip (ip_address)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 设备指标表
CREATE TABLE IF NOT EXISTS device_metrics (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    device_id VARCHAR(36) NOT NULL,
    metric_type VARCHAR(50) NOT NULL COMMENT '指标类型: cpu, memory, disk, temperature, network',
    metric_value DECIMAL(10, 2) NOT NULL,
    unit VARCHAR(20) NOT NULL DEFAULT 'percent',
    collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE,
    INDEX idx_device_metric (device_id, metric_type),
    INDEX idx_collected_at (collected_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 告警记录表
CREATE TABLE IF NOT EXISTS alerts (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    device_id VARCHAR(36) NOT NULL,
    alert_type VARCHAR(50) NOT NULL COMMENT '告警类型: threshold, anomaly, offline',
    severity ENUM('low', 'medium', 'high', 'critical') DEFAULT 'medium',
    title VARCHAR(200) NOT NULL,
    description TEXT,
    value DECIMAL(10, 2),
    threshold DECIMAL(10, 2),
    status ENUM('active', 'acknowledged', 'resolved') DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    acknowledged_at TIMESTAMP NULL,
    resolved_at TIMESTAMP NULL,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE,
    INDEX idx_device_alert (device_id, status),
    INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 设备配置表
CREATE TABLE IF NOT EXISTS device_configs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    device_id VARCHAR(36) NOT NULL,
    config_key VARCHAR(100) NOT NULL,
    config_value TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE,
    UNIQUE KEY uk_device_config (device_id, config_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 插入示例数据
INSERT INTO devices (id, name, type, ip_address, status, location, description) VALUES
('dev-001', 'Web服务器-01', 'server', '192.168.1.100', 'online', '机房A-机架1', '主Web服务器'),
('dev-002', '数据库服务器', 'server', '192.168.1.101', 'online', '机房A-机架2', 'MySQL主数据库'),
('dev-003', '温度传感器-01', 'sensor', '192.168.1.200', 'online', '机房A', '环境温度监测'),
('dev-004', '摄像头-01', 'camera', '192.168.1.201', 'offline', '入口', '大门监控摄像头'),
('dev-005', '核心交换机', 'router', '192.168.1.1', 'online', '机房A-网络柜', '核心网络设备');

-- 插入示例指标数据
INSERT INTO device_metrics (device_id, metric_type, metric_value, unit) VALUES
('dev-001', 'cpu', 45.50, 'percent'),
('dev-001', 'memory', 68.20, 'percent'),
('dev-001', 'disk', 55.00, 'percent'),
('dev-002', 'cpu', 32.10, 'percent'),
('dev-002', 'memory', 78.50, 'percent'),
('dev-003', 'temperature', 24.50, 'celsius'),
('dev-005', 'cpu', 15.30, 'percent');
