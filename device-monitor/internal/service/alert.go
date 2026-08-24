package service

import (
	"database/sql"
	"fmt"
	"time"

	"github.com/device-monitor/internal/model"
)

// AlertService 告警服务
type AlertService struct {
	db *sql.DB
}

// NewAlertService 创建告警服务
func NewAlertService(db *sql.DB) *AlertService {
	return &AlertService{db: db}
}

// ListAlerts 获取告警列表
func (s *AlertService) ListAlerts(filter model.AlertListFilter) ([]model.Alert, error) {
	query := `SELECT id, device_id, alert_type, severity, title, description, value, threshold, 
	          status, created_at, acknowledged_at, resolved_at
	          FROM alerts WHERE 1=1`
	args := []interface{}{}

	if filter.DeviceID != "" {
		query += " AND device_id = ?"
		args = append(args, filter.DeviceID)
	}
	if filter.Status != "" {
		query += " AND status = ?"
		args = append(args, filter.Status)
	}
	if filter.Severity != "" {
		query += " AND severity = ?"
		args = append(args, filter.Severity)
	}

	query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
	args = append(args, filter.Limit, filter.Offset)

	rows, err := s.db.Query(query, args...)
	if err != nil {
		return nil, fmt.Errorf("查询告警列表失败: %w", err)
	}
	defer rows.Close()

	var alerts []model.Alert
	for rows.Next() {
		var a model.Alert
		if err := rows.Scan(&a.ID, &a.DeviceID, &a.AlertType, &a.Severity, &a.Title, &a.Description,
			&a.Value, &a.Threshold, &a.Status, &a.CreatedAt, &a.AcknowledgedAt, &a.ResolvedAt); err != nil {
			return nil, fmt.Errorf("扫描告警数据失败: %w", err)
		}
		alerts = append(alerts, a)
	}
	return alerts, nil
}

// CreateAlert 创建告警
func (s *AlertService) CreateAlert(deviceID, alertType, severity, title, description string, value, threshold float64) (*model.Alert, error) {
	now := time.Now()
	query := `INSERT INTO alerts (device_id, alert_type, severity, title, description, value, threshold, status, created_at)
	          VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)`
	result, err := s.db.Exec(query, deviceID, alertType, severity, title, description, value, threshold, now)
	if err != nil {
		return nil, fmt.Errorf("创建告警失败: %w", err)
	}

	id, _ := result.LastInsertId()
	return &model.Alert{
		ID:          id,
		DeviceID:    deviceID,
		AlertType:   alertType,
		Severity:    severity,
		Title:       title,
		Description: description,
		Value:       value,
		Threshold:   threshold,
		Status:      "active",
		CreatedAt:   now,
	}, nil
}

// AcknowledgeAlert 确认告警
func (s *AlertService) AcknowledgeAlert(id int64) error {
	now := time.Now()
	query := `UPDATE alerts SET status = 'acknowledged', acknowledged_at = ? WHERE id = ? AND status = 'active'`
	_, err := s.db.Exec(query, now, id)
	if err != nil {
		return fmt.Errorf("确认告警失败: %w", err)
	}
	return nil
}

// ResolveAlert 解决告警
func (s *AlertService) ResolveAlert(id int64) error {
	now := time.Now()
	query := `UPDATE alerts SET status = 'resolved', resolved_at = ? WHERE id = ? AND status IN ('active', 'acknowledged')`
	_, err := s.db.Exec(query, now, id)
	if err != nil {
		return fmt.Errorf("解决告警失败: %w", err)
	}
	return nil
}

// CheckThreshold 检查阈值告警
func (s *AlertService) CheckThreshold(deviceID string, metricType string, value float64, threshold float64) error {
	var severity string
	var title string
	var description string

	switch metricType {
	case "temperature":
		if value > threshold {
			severity = "high"
			title = fmt.Sprintf("设备 %s 温度过高", deviceID)
			description = fmt.Sprintf("当前温度 %.1f°C 超过阈值 %.1f°C", value, threshold)
		}
	case "cpu":
		if value > threshold {
			severity = "medium"
			title = fmt.Sprintf("设备 %s CPU 使用率过高", deviceID)
			description = fmt.Sprintf("当前 CPU 使用率 %.1f%% 超过阈值 %.1f%%", value, threshold)
		}
	default:
		return nil
	}

	if severity != "" {
		_, err := s.CreateAlert(deviceID, "threshold", severity, title, description, value, threshold)
		return err
	}
	return nil
}
