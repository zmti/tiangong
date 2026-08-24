package model

import "time"

// Device 设备模型
type Device struct {
	ID          string    `json:"id"`
	Name        string    `json:"name"`
	Type        string    `json:"type"`
	IPAddress   string    `json:"ip_address"`
	Status      string    `json:"status"`
	Location    string    `json:"location"`
	Description string    `json:"description"`
	CreatedAt   time.Time `json:"created_at"`
	UpdatedAt   time.Time `json:"updated_at"`
}

// DeviceCreate 创建设备请求
type DeviceCreate struct {
	Name        string `json:"name" binding:"required"`
	Type        string `json:"type" binding:"required"`
	IPAddress   string `json:"ip_address" binding:"required"`
	Location    string `json:"location"`
	Description string `json:"description"`
}

// DeviceUpdate 更新设备请求
type DeviceUpdate struct {
	Name        string `json:"name"`
	Type        string `json:"type"`
	IPAddress   string `json:"ip_address"`
	Status      string `json:"status"`
	Location    string `json:"location"`
	Description string `json:"description"`
}

// Metric 设备指标模型
type Metric struct {
	ID          int64     `json:"id"`
	DeviceID    string    `json:"device_id"`
	MetricType  string    `json:"metric_type"`
	MetricValue float64   `json:"metric_value"`
	Unit        string    `json:"unit"`
	CollectedAt time.Time `json:"collected_at"`
}

// MetricReport 上报指标请求
type MetricReport struct {
	MetricType  string  `json:"metric_type" binding:"required"`
	MetricValue float64 `json:"metric_value" binding:"required"`
	Unit        string  `json:"unit"`
}

// Alert 告警模型
type Alert struct {
	ID           int64     `json:"id"`
	DeviceID     string    `json:"device_id"`
	AlertType    string    `json:"alert_type"`
	Severity     string    `json:"severity"`
	Title        string    `json:"title"`
	Description  string    `json:"description"`
	Value        float64   `json:"value"`
	Threshold    float64   `json:"threshold"`
	Status       string    `json:"status"`
	CreatedAt    time.Time `json:"created_at"`
	AcknowledgedAt *time.Time `json:"acknowledged_at,omitempty"`
	ResolvedAt   *time.Time `json:"resolved_at,omitempty"`
}

// AlertListFilter 告警列表过滤条件
type AlertListFilter struct {
	DeviceID string
	Status   string
	Severity string
	Limit    int
	Offset   int
}
