package service

import (
	"database/sql"
	"fmt"
	"time"

	"github.com/device-monitor/internal/model"
	"github.com/google/uuid"
)

// DeviceService 设备服务
type DeviceService struct {
	db *sql.DB
}

// NewDeviceService 创建设备服务
func NewDeviceService(db *sql.DB) *DeviceService {
	return &DeviceService{db: db}
}

// ListDevices 获取设备列表
func (s *DeviceService) ListDevices(status, deviceType string, limit, offset int) ([]model.Device, error) {
	query := `SELECT id, name, type, ip_address, status, location, description, created_at, updated_at 
	          FROM devices WHERE 1=1`
	args := []interface{}{}

	if status != "" {
		query += " AND status = ?"
		args = append(args, status)
	}
	if deviceType != "" {
		query += " AND type = ?"
		args = append(args, deviceType)
	}
	query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
	args = append(args, limit, offset)

	rows, err := s.db.Query(query, args...)
	if err != nil {
		return nil, fmt.Errorf("查询设备列表失败: %w", err)
	}
	defer rows.Close()

	var devices []model.Device
	for rows.Next() {
		var d model.Device
		if err := rows.Scan(&d.ID, &d.Name, &d.Type, &d.IPAddress, &d.Status, &d.Location, &d.Description, &d.CreatedAt, &d.UpdatedAt); err != nil {
			return nil, fmt.Errorf("扫描设备数据失败: %w", err)
		}
		devices = append(devices, d)
	}
	return devices, nil
}

// GetDevice 获取设备详情
func (s *DeviceService) GetDevice(id string) (*model.Device, error) {
	var d model.Device
	query := `SELECT id, name, type, ip_address, status, location, description, created_at, updated_at 
	          FROM devices WHERE id = ?`
	err := s.db.QueryRow(query, id).Scan(
		&d.ID, &d.Name, &d.Type, &d.IPAddress, &d.Status, &d.Location, &d.Description, &d.CreatedAt, &d.UpdatedAt,
	)
	if err != nil {
		if err == sql.ErrNoRows {
			return nil, fmt.Errorf("设备不存在: %s", id)
		}
		return nil, fmt.Errorf("查询设备失败: %w", err)
	}
	return &d, nil
}

// CreateDevice 创建设备
func (s *DeviceService) CreateDevice(req model.DeviceCreate) (*model.Device, error) {
	id := uuid.New().String()
	now := time.Now()

	query := `INSERT INTO devices (id, name, type, ip_address, status, location, description, created_at, updated_at)
	          VALUES (?, ?, ?, ?, 'offline', ?, ?, ?, ?)`
	_, err := s.db.Exec(query, id, req.Name, req.Type, req.IPAddress, req.Location, req.Description, now, now)
	if err != nil {
		return nil, fmt.Errorf("创建设备失败: %w", err)
	}

	return &model.Device{
		ID:          id,
		Name:        req.Name,
		Type:        req.Type,
		IPAddress:   req.IPAddress,
		Status:      "offline",
		Location:    req.Location,
		Description: req.Description,
		CreatedAt:   now,
		UpdatedAt:   now,
	}, nil
}

// UpdateDevice 更新设备
func (s *DeviceService) UpdateDevice(id string, req model.DeviceUpdate) (*model.Device, error) {
	// 检查设备是否存在
	existing, err := s.GetDevice(id)
	if err != nil {
		return nil, err
	}

	// 构建更新语句
	query := `UPDATE devices SET name=?, type=?, ip_address=?, status=?, location=?, description=?, updated_at=? WHERE id=?`
	_, err = s.db.Exec(query,
		req.Name, req.Type, req.IPAddress, req.Status, req.Location, req.Description, time.Now(), id,
	)
	if err != nil {
		return nil, fmt.Errorf("更新设备失败: %w", err)
	}

	existing.Name = req.Name
	existing.Type = req.Type
	existing.IPAddress = req.IPAddress
	existing.Status = req.Status
	existing.Location = req.Location
	existing.Description = req.Description
	existing.UpdatedAt = time.Now()

	return existing, nil
}

// DeleteDevice 删除设备
func (s *DeviceService) DeleteDevice(id string) error {
	// 检查设备是否存在
	if _, err := s.GetDevice(id); err != nil {
		return err
	}

	query := `DELETE FROM devices WHERE id = ?`
	_, err := s.db.Exec(query, id)
	if err != nil {
		return fmt.Errorf("删除设备失败: %w", err)
	}
	return nil
}

// GetMetrics 获取设备指标历史数据
func (s *DeviceService) GetMetrics(deviceID string, metricType string, hours int) ([]model.Metric, error) {
	query := `SELECT id, device_id, metric_type, metric_value, unit, collected_at 
	          FROM device_metrics WHERE device_id = ?`
	args := []interface{}{deviceID}

	if metricType != "" {
		query += " AND metric_type = ?"
		args = append(args, metricType)
	}

	query += ` AND collected_at >= DATE_SUB(NOW(), INTERVAL ? HOUR)
	          ORDER BY collected_at DESC
	          LIMIT 1000`
	args = append(args, hours)

	rows, err := s.db.Query(query, args...)
	if err != nil {
		return nil, fmt.Errorf("查询指标数据失败: %w", err)
	}
	defer rows.Close()

	var metrics []model.Metric
	for rows.Next() {
		var m model.Metric
		if err := rows.Scan(&m.ID, &m.DeviceID, &m.MetricType, &m.MetricValue, &m.Unit, &m.CollectedAt); err != nil {
			return nil, fmt.Errorf("扫描指标数据失败: %w", err)
		}
		metrics = append(metrics, m)
	}
	return metrics, nil
}

// ReportMetric 上报设备指标
func (s *DeviceService) ReportMetric(deviceID string, req model.MetricReport) (*model.Metric, error) {
	// 检查设备是否存在
	if _, err := s.GetDevice(deviceID); err != nil {
		return nil, err
	}

	if req.Unit == "" {
		req.Unit = "percent"
	}

	now := time.Now()
	query := `INSERT INTO device_metrics (device_id, metric_type, metric_value, unit, collected_at)
	          VALUES (?, ?, ?, ?, ?)`
	result, err := s.db.Exec(query, deviceID, req.MetricType, req.MetricValue, req.Unit, now)
	if err != nil {
		return nil, fmt.Errorf("上报指标失败: %w", err)
	}

	id, _ := result.LastInsertId()

	return &model.Metric{
		ID:          id,
		DeviceID:    deviceID,
		MetricType:  req.MetricType,
		MetricValue: req.MetricValue,
		Unit:        req.Unit,
		CollectedAt: now,
	}, nil
}

// GetLatestMetrics 获取设备最新指标
func (s *DeviceService) GetLatestMetrics(deviceID string) (map[string]model.Metric, error) {
	query := `SELECT dm1.metric_type, dm1.metric_value, dm1.unit, dm1.collected_at
	          FROM device_metrics dm1
	          INNER JOIN (
	              SELECT metric_type, MAX(collected_at) as max_time
	              FROM device_metrics
	              WHERE device_id = ?
	              GROUP BY metric_type
	          ) dm2 ON dm1.metric_type = dm2.metric_type AND dm1.collected_at = dm2.max_time`

	rows, err := s.db.Query(query, deviceID)
	if err != nil {
		return nil, fmt.Errorf("查询最新指标失败: %w", err)
	}
	defer rows.Close()

	result := make(map[string]model.Metric)
	for rows.Next() {
		var m model.Metric
		if err := rows.Scan(&m.MetricType, &m.MetricValue, &m.Unit, &m.CollectedAt); err != nil {
			return nil, fmt.Errorf("扫描指标数据失败: %w", err)
		}
		m.DeviceID = deviceID
		result[m.MetricType] = m
	}
	return result, nil
}
