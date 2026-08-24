package handler

import (
	"encoding/json"
	"net/http"
	"strconv"

	"github.com/device-monitor/internal/model"
	"github.com/device-monitor/internal/service"
	"github.com/gorilla/mux"
)

// AlertHandler 告警处理器
type AlertHandler struct {
	alertService *service.AlertService
}

// NewAlertHandler 创建告警处理器
func NewAlertHandler(alertService *service.AlertService) *AlertHandler {
	return &AlertHandler{alertService: alertService}
}

// ListAlerts 获取告警列表
func (h *AlertHandler) ListAlerts(w http.ResponseWriter, r *http.Request) {
	filter := model.AlertListFilter{
		DeviceID: r.URL.Query().Get("device_id"),
		Status:   r.URL.Query().Get("status"),
		Severity: r.URL.Query().Get("severity"),
		Limit:    20,
		Offset:   0,
	}

	if limitStr := r.URL.Query().Get("limit"); limitStr != "" {
		filter.Limit, _ = strconv.Atoi(limitStr)
	}
	if offsetStr := r.URL.Query().Get("offset"); offsetStr != "" {
		filter.Offset, _ = strconv.Atoi(offsetStr)
	}

	alerts, err := h.alertService.ListAlerts(filter)
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
		"data":    alerts,
	})
}

// AcknowledgeAlert 确认告警
func (h *AlertHandler) AcknowledgeAlert(w http.ResponseWriter, r *http.Request) {
	vars := mux.Vars(r)
	id, _ := strconv.ParseInt(vars["id"], 10, 64)

	if err := h.alertService.AcknowledgeAlert(id); err != nil {
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

// ResolveAlert 解决告警
func (h *AlertHandler) ResolveAlert(w http.ResponseWriter, r *http.Request) {
	vars := mux.Vars(r)
	id, _ := strconv.ParseInt(vars["id"], 10, 64)

	if err := h.alertService.ResolveAlert(id); err != nil {
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
