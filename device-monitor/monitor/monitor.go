package monitor

import (
	"fmt"
	"math/rand"
	"sync"
	"time"

	"device-monitor/model"
	"device-monitor/storage"
)

// MonitorService 监控服务
type MonitorService struct {
	devices   map[string]*model.Device
	storage   storage.Storage
	alerts    []*model.Alert
	rules     []*model.AlertRule
	mu        sync.RWMutex
	stopCh    chan struct{}
}

// NewMonitorService 创建监控服务
func NewMonitorService(store storage.Storage) *MonitorService {
	return &MonitorService{
		devices: make(map[string]*model.Device),
		storage: store,
		stopCh:  make(chan struct{}),
	}
}

// AddDevice 添加设备
func (m *MonitorService) AddDevice(device *model.Device) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	
	if _, exists := m.devices[device.ID]; exists {
		return fmt.Errorf("设备 %s 已存在", device.ID)
	}
	
	m.devices[device.ID] = device
	return m.storage.SaveDevice(device)
}

// GetDevice 获取设备
func (m *MonitorService) GetDevice(id string) (*model.Device, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	
	device, exists := m.devices[id]
	if !exists {
		return nil, fmt.Errorf("设备 %s 不存在", id)
	}
	return device, nil
}

// GetAllDevices 获取所有设备
func (m *MonitorService) GetAllDevices() []*model.Device {
	m.mu.RLock()
	defer m.mu.RUnlock()
	
	devices := make([]*model.Device, 0, len(m.devices))
	for _, device := range m.devices {
		devices = append(devices, device)
	}
	return devices
}

// DeleteDevice 删除设备
func (m *MonitorService) DeleteDevice(id string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	
	if _, exists := m.devices[id]; !exists {
		return fmt.Errorf("设备 %s 不存在", id)
	}
	
	delete(m.devices, id)
	return m.storage.DeleteDevice(id)
}

// UpdateMetric 更新设备指标
func (m *MonitorService) UpdateMetric(deviceID string, metricName string, value float64, unit string) error {
	m.mu.Lock()
	device, exists := m.devices[deviceID]
	m.mu.Unlock()
	
	if !exists {
		return fmt.Errorf("设备 %s 不存在", deviceID)
	}
	
	device.AddMetric(metricName, value, unit)
	
	// 检查告警规则
	m.checkAlertRules(device, metricName, value)
	
	// 持久化
	return m.storage.SaveDevice(device)
}

// checkAlertRules 检查告警规则
func (m *MonitorService) checkAlertRules(device *model.Device, metricName string, value float64) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	
	for _, rule := range m.rules {
		if !rule.Enabled {
			continue
		}
		if rule.DeviceID != device.ID && rule.DeviceID != "" {
			continue
		}
		if rule.MetricName != "" && rule.MetricName != metricName {
			continue
		}
		
		triggered := false
		switch rule.Condition {
		case "gt":
			triggered = value > rule.Threshold
		case "lt":
			triggered = value < rule.Threshold
		case "eq":
			triggered = value == rule.Threshold
		}
		
		if triggered {
			alert := &model.Alert{
				ID:       fmt.Sprintf("alert_%d", time.Now().UnixNano()),
				DeviceID: device.ID,
				RuleID:   rule.ID,
				Message:  fmt.Sprintf("%s: %s %s %.2f (阈值: %.2f)", device.Name, metricName, rule.Condition, value, rule.Threshold),
				Severity: m.getSeverity(rule.Threshold, value, rule.Condition),
				Timestamp: time.Now(),
			}
			m.alerts = append(m.alerts, alert)
			m.storage.SaveAlert(alert)
			fmt.Printf("[告警] %s\n", alert.Message)
		}
	}
}

// getSeverity 获取告警级别
func (m *MonitorService) getSeverity(threshold, value float64, condition string) string {
	diff := value - threshold
	if condition == "gt" {
		if diff > threshold*0.5 {
			return "critical"
		}
		return "warning"
	}
	if condition == "lt" {
		if diff < -threshold*0.5 {
			return "critical"
		}
		return "warning"
	}
	return "info"
}

// AddAlertRule 添加告警规则
func (m *MonitorService) AddAlertRule(rule *model.AlertRule) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.rules = append(m.rules, rule)
}

// GetAlerts 获取告警列表
func (m *MonitorService) GetAlerts(deviceID string, limit int) []*model.Alert {
	m.mu.RLock()
	defer m.mu.RUnlock()
	
	var alerts []*model.Alert
	for _, alert := range m.alerts {
		if deviceID != "" && alert.DeviceID != deviceID {
			continue
		}
		alerts = append(alerts, alert)
		if limit > 0 && len(alerts) >= limit {
			break
		}
	}
	return alerts
}

// ResolveAlert 解决告警
func (m *MonitorService) ResolveAlert(alertID string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	
	for _, alert := range m.alerts {
		if alert.ID == alertID {
			alert.Resolved = true
			break
		}
	}
}

// StartSimulation 启动模拟数据采集
func (m *MonitorService) StartSimulation(interval time.Duration) {
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		
		for {
			select {
			case <-ticker.C:
				m.simulateData()
			case <-m.stopCh:
				return
			}
		}
	}()
}

// simulateData 模拟设备数据
func (m *MonitorService) simulateData() {
	m.mu.RLock()
	devices := make([]*model.Device, 0, len(m.devices))
	for _, device := range m.devices {
		devices = append(devices, device)
	}
	m.mu.RUnlock()
	
	for _, device := range devices {
		switch device.Type {
		case model.TypeSensor:
			// 模拟温度传感器
			temp := 20 + rand.Float64()*15 // 20-35°C
			humidity := 40 + rand.Float64()*30 // 40-70%
			device.AddMetric("temperature", temp, "°C")
			device.AddMetric("humidity", humidity, "%")
			
			m.UpdateMetric(device.ID, "temperature", temp, "°C")
			m.UpdateMetric(device.ID, "humidity", humidity, "%")
			
		case model.TypeCamera:
			// 模拟摄像头状态
			status := "recording"
			if rand.Float64() > 0.8 {
				status = "idle"
			}
			device.AddMetric("status", 1, status)
			
		case model.TypeController:
			// 模拟控制器
			load := rand.Float64() * 100
			device.AddMetric("cpu_load", load, "%")
			m.UpdateMetric(device.ID, "cpu_load", load, "%")
			
		case model.TypeGateway:
			// 模拟网关
			latency := rand.Float64() * 100
			device.AddMetric("latency", latency, "ms")
			m.UpdateMetric(device.ID, "latency", latency, "ms")
		}
	}
}

// StopSimulation 停止模拟
func (m *MonitorService) StopSimulation() {
	close(m.stopCh)
}
