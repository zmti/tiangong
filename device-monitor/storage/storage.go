package storage

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	"device-monitor/model"
)

// Storage 数据存储接口
type Storage interface {
	SaveDevice(device *model.Device) error
	GetDevice(id string) (*model.Device, error)
	GetAllDevices() ([]*model.Device, error)
	DeleteDevice(id string) error
	SaveAlert(alert *model.Alert) error
	GetAlerts(deviceID string, limit int) ([]*model.Alert, error)
}

// FileStorage 基于文件的存储实现
type FileStorage struct {
	deviceDir string
	alertDir  string
	mu        sync.RWMutex
}

// NewFileStorage 创建文件存储
func NewFileStorage(basePath string) (*FileStorage, error) {
	deviceDir := filepath.Join(basePath, "devices")
	alertDir := filepath.Join(basePath, "alerts")
	
	if err := os.MkdirAll(deviceDir, 0755); err != nil {
		return nil, fmt.Errorf("创建设备目录失败: %w", err)
	}
	if err := os.MkdirAll(alertDir, 0755); err != nil {
		return nil, fmt.Errorf("创建告警目录失败: %w", err)
	}
	
	return &FileStorage{
		deviceDir: deviceDir,
		alertDir:  alertDir,
	}, nil
}

// SaveDevice 保存设备
func (fs *FileStorage) SaveDevice(device *model.Device) error {
	fs.mu.Lock()
	defer fs.mu.Unlock()
	
	data, err := json.MarshalIndent(device, "", "  ")
	if err != nil {
		return fmt.Errorf("序列化设备数据失败: %w", err)
	}
	
	filename := filepath.Join(fs.deviceDir, device.ID+".json")
	return os.WriteFile(filename, data, 0644)
}

// GetDevice 获取设备
func (fs *FileStorage) GetDevice(id string) (*model.Device, error) {
	fs.mu.RLock()
	defer fs.mu.RUnlock()
	
	filename := filepath.Join(fs.deviceDir, id+".json")
	data, err := os.ReadFile(filename)
	if err != nil {
		return nil, fmt.Errorf("读取设备文件失败: %w", err)
	}
	
	var device model.Device
	if err := json.Unmarshal(data, &device); err != nil {
		return nil, fmt.Errorf("反序列化设备数据失败: %w", err)
	}
	
	return &device, nil
}

// GetAllDevices 获取所有设备
func (fs *FileStorage) GetAllDevices() ([]*model.Device, error) {
	fs.mu.RLock()
	defer fs.mu.RUnlock()
	
	files, err := os.ReadDir(fs.deviceDir)
	if err != nil {
		return nil, fmt.Errorf("读取设备目录失败: %w", err)
	}
	
	var devices []*model.Device
	for _, file := range files {
		if file.IsDir() {
			continue
		}
		
		data, err := os.ReadFile(filepath.Join(fs.deviceDir, file.Name()))
		if err != nil {
			continue
		}
		
		var device model.Device
		if err := json.Unmarshal(data, &device); err != nil {
			continue
		}
		devices = append(devices, &device)
	}
	
	return devices, nil
}

// DeleteDevice 删除设备
func (fs *FileStorage) DeleteDevice(id string) error {
	fs.mu.Lock()
	defer fs.mu.Unlock()
	
	filename := filepath.Join(fs.deviceDir, id+".json")
	return os.Remove(filename)
}

// SaveAlert 保存告警
func (fs *FileStorage) SaveAlert(alert *model.Alert) error {
	fs.mu.Lock()
	defer fs.mu.Unlock()
	
	data, err := json.MarshalIndent(alert, "", "  ")
	if err != nil {
		return fmt.Errorf("序列化告警数据失败: %w", err)
	}
	
	filename := filepath.Join(fs.alertDir, fmt.Sprintf("%d_%s.json", time.Now().UnixNano(), alert.ID))
	return os.WriteFile(filename, data, 0644)
}

// GetAlerts 获取告警列表
func (fs *FileStorage) GetAlerts(deviceID string, limit int) ([]*model.Alert, error) {
	fs.mu.RLock()
	defer fs.mu.RUnlock()
	
	files, err := os.ReadDir(fs.alertDir)
	if err != nil {
		return nil, fmt.Errorf("读取告警目录失败: %w", err)
	}
	
	var alerts []*model.Alert
	for _, file := range files {
		if file.IsDir() {
			continue
		}
		
		data, err := os.ReadFile(filepath.Join(fs.alertDir, file.Name()))
		if err != nil {
			continue
		}
		
		var alert model.Alert
		if err := json.Unmarshal(data, &alert); err != nil {
			continue
		}
		
		if deviceID != "" && alert.DeviceID != deviceID {
			continue
		}
		
		alerts = append(alerts, &alert)
		
		if limit > 0 && len(alerts) >= limit {
			break
		}
	}
	
	return alerts, nil
}
