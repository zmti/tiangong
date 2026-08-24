package model

import (
	"sync"
	"time"
)

// DeviceType 设备类型
type DeviceType string

const (
	TypeSensor    DeviceType = "sensor"    // 传感器
	TypeCamera    DeviceType = "camera"    // 摄像头
	TypeController DeviceType = "controller" // 控制器
	TypeGateway   DeviceType = "gateway"   // 网关
)

// DeviceStatus 设备状态
type DeviceStatus string

const (
	StatusOnline  DeviceStatus = "online"   // 在线
	StatusOffline DeviceStatus = "offline"  // 离线
	StatusError   DeviceStatus = "error"    // 错误
)

// DeviceMetric 设备指标
type DeviceMetric struct {
	Timestamp time.Time `json:"timestamp"`
	Name      string    `json:"name"`
	Value     float64   `json:"value"`
	Unit      string    `json:"unit"`
}

// Device 设备结构
type Device struct {
	ID          string         `json:"id"`
	Name        string         `json:"name"`
	Type        DeviceType     `json:"type"`
	Status      DeviceStatus   `json:"status"`
	Location    string         `json:"location"`
	Metrics     []DeviceMetric `json:"metrics"`
	LastUpdate  time.Time      `json:"last_update"`
	CreatedAt   time.Time      `json:"created_at"`
	mu          sync.RWMutex   `json:"-"`
}

// AlertRule 告警规则
type AlertRule struct {
	ID          string    `json:"id"`
	DeviceID    string    `json:"device_id"`
	MetricName  string    `json:"metric_name"`
	Condition   string    `json:"condition"` // gt, lt, eq
	Threshold   float64   `json:"threshold"`
	Message     string    `json:"message"`
	Enabled     bool      `json:"enabled"`
	CreatedAt   time.Time `json:"created_at"`
}

// Alert 告警记录
type Alert struct {
	ID        string    `json:"id"`
	DeviceID  string    `json:"device_id"`
	RuleID    string    `json:"rule_id"`
	Message   string    `json:"message"`
	Severity  string    `json:"severity"` // info, warning, critical
	Timestamp time.Time `json:"timestamp"`
	Resolved  bool      `json:"resolved"`
}

// 创建新设备
func NewDevice(id, name string, dtype DeviceType, location string) *Device {
	now := time.Now()
	return &Device{
		ID:        id,
		Name:      name,
		Type:      dtype,
		Status:    StatusOnline,
		Location:  location,
		Metrics:   make([]DeviceMetric, 0),
		LastUpdate: now,
		CreatedAt:  now,
	}
}

// 添加指标数据
func (d *Device) AddMetric(name string, value float64, unit string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	
	metric := DeviceMetric{
		Timestamp: time.Now(),
		Name:      name,
		Value:     value,
		Unit:      unit,
	}
	
	d.Metrics = append(d.Metrics, metric)
	d.LastUpdate = time.Now()
	
	// 保留最近100条记录
	if len(d.Metrics) > 100 {
		d.Metrics = d.Metrics[len(d.Metrics)-100:]
	}
}

// 获取最新指标值
func (d *Device) GetLatestMetric(name string) (float64, bool) {
	d.mu.RLock()
	defer d.mu.RUnlock()
	
	for i := len(d.Metrics) - 1; i >= 0; i-- {
		if d.Metrics[i].Name == name {
			return d.Metrics[i].Value, true
		}
	}
	return 0, false
}
