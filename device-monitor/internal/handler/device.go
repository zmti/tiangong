package handler

import (
	"encoding/json"
	"net/http"
	"strconv"

	"github.com/device-monitor/internal/model"
	"github.com/device-monitor/internal/service"
	"github.com/gorilla/mux"
)

// DeviceHandler 设备处理器
type DeviceHandler struct {
	deviceService *service.DeviceService
	alertService  *service.AlertService
}

// NewDeviceHandler 创建设备处理器
func NewDeviceHandler(deviceService *service.DeviceService) *DeviceHandler {
	return &DeviceHandler{
		deviceService: deviceService,
		alertService:  service.NewAlertService(nil), // 需要传入真实的db
	}
}

// ListDevices 获取设备列表
func (h *DeviceHandler) ListDevices(w http.ResponseWriter, r *http.Request) {
	status := r.URL.Query().Get("status")
	deviceType := r.URL.Query().Get("type")
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	offset, _ := strconv.Atoi(r.URL.Query().Get("offset"))

	if limit == 0 {
		limit = 20
	}
	if offset == 0 {
		offset = 0
	}

	devices, err := h.deviceService.ListDevices(status, deviceType, limit, offset)
	if err != nil {
		json.NewEncoder(w).Encode(map[string]interface{}{
			"code":    500,
			"message": err.Error(),
		})
		return
	}

	json.NewEncoder(w).Encode(map[string]interface{}{
		"code":    0,
		"message": "success",
		"data":    devices,
	})
}

// CreateDevice 创建设备
func (h *DeviceHandler) CreateDevice(w http.ResponseWriter, r *http.Request) {
	var req model.DeviceCreate
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		json.NewEncoder(w).Encode(map[string]interface{}{
			"code":    400,
			"message": "请求参数错误: " + err.Error(),
		})
		return
	}

	device, err := h.deviceService.CreateDevice(req)
	if err != nil {
		json.NewEncoder(w).Encode(map[string]interface{}{
			"code":    500,
			"message": err.Error(),
		})
		return
	}

	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(map[string]interface{}{
		"code":    0,
		"message": "success",
		"data":    device,
	})
}

// GetDevice 获取设备详情
func (h *DeviceHandler) GetDevice(w http.ResponseWriter, r *http.Request) {
	vars := mux.Vars(r)
	id := vars["id"]

	device, err := h.deviceService.GetDevice(id)
	if err != nil {
		json.NewEncoder(w).Encode(map[string]interface{}{
			"code":    404,
			"message": err.Error(),
		})
		return
	}

	json.NewEncoder(w).Encode(map[string]interface{}{
		"code":    0,
		"message": "success",
		"data":    device,
	})
}

// UpdateDevice 更新设备
func (h *DeviceHandler) UpdateDevice(w http.ResponseWriter, r *http.Request) {
	vars := mux.Vars(r)
	id := vars["id"]

	var req model.DeviceUpdate
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		json.NewEncoder(w).Encode(map[string]interface{}{
			"code":    400,
			"message": "请求参数错误: " + err.Error(),
		})
		return
	}

	device, err := h.deviceService.UpdateDevice(id, req)
	if err != nil {
		json.NewEncoder(w).Encode(map[string]interface{}{
			"code":    500,
			"message": err.Error(),
		})
		return
	}

	json.NewEncoder(w).Encode(map[string]interface{}{
		"code":    0,
		"message": "success",
		"data":    device,
	})
}

// DeleteDevice 删除设备
func (h *DeviceHandler) DeleteDevice(w http.ResponseWriter, r *http.Request) {
	vars := mux.Vars(r)
	id := vars["id"]

	if err := h.deviceService.DeleteDevice(id); err != nil {
		json.NewEncoder(w).Encode(map[string]interface{}{
			"code":    500,
			"message": err.Error(),
		})
		return
	}

	json.NewEncoder(w).Encode(map[string]interface{}{
		"code":    0,
		"message": "success",
	})
}

// GetMetrics 获取设备指标历史数据
func (h *DeviceHandler) GetMetrics(w http.ResponseWriter, r *http.Request) {
	vars := mux.Vars(r)
	deviceID := vars["id"]
	metricType := r.URL.Query().Get("type")
	hours, _ := strconv.Atoi(r.URL.Query().Get("hours"))

	if hours == 0 {
		hours = 24
	}

	metrics, err := h.deviceService.GetMetrics(deviceID, metricType, hours)
	if err != nil {
		json.NewEncoder(w).Encode(map[string]interface{}{
			"code":    500,
			"message": err.Error(),
		})
		return
	}

	json.NewEncoder(w).Encode(map[string]interface{}{
		"code":    0,
		"message": "success",
		"data":    metrics,
	})
}

// ReportMetric 上报设备指标
func (h *DeviceHandler) ReportMetric(w http.ResponseWriter, r *http.Request) {
	vars := mux.Vars(r)
	deviceID := vars["id"]

	var req model.MetricReport
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		json.NewEncoder(w).Encode(map[string]interface{}{
			"code":    400,
			"message": "请求参数错误: " + err.Error(),
		})
		return
	}

	metric, err := h.deviceService.ReportMetric(deviceID, req)
	if err != nil {
		json.NewEncoder(w).Encode(map[string]interface{}{
			"code":    500,
			"message": err.Error(),
		})
		return
	}

	// 检查阈值告警
	if req.MetricType == "temperature" || req.MetricType == "cpu" {
		// TODO: 从配置获取阈值
		threshold := 80.0
		if err := h.alertService.CheckThreshold(deviceID, req.MetricType, req.MetricValue, threshold); err != nil {
			// 告警创建失败不影响指标上报
		}
	}

	json.NewEncoder(w).Encode(map[string]interface{}{
		"code":    0,
		"message": "success",
		"data":    metric,
	})
}

// GetLatestMetrics 获取设备最新指标
func (h *DeviceHandler) GetLatestMetrics(w http.ResponseWriter, r *http.Request) {
	vars := mux.Vars(r)
	deviceID := vars["id"]

	metrics, err := h.deviceService.GetLatestMetrics(deviceID)
	if err != nil {
		json.NewEncoder(w).Encode(map[string]interface{}{
			"code":    500,
			"message": err.Error(),
		})
		return
	}

	json.NewEncoder(w).Encode(map[string]interface{}{
		"code":    0,
		"message": "success",
		"data":    metrics,
	})
}
