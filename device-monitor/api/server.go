package api

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"

	"device-monitor/model"
	"device-monitor/monitor"
)

// Server HTTP API 服务器
type Server struct {
	monitor *monitor.MonitorService
	addr    string
}

// NewServer 创建 API 服务器
func NewServer(monitorSvc *monitor.MonitorService, addr string) *Server {
	return &Server{
		monitor: monitorSvc,
		addr:    addr,
	}
}

// Start 启动服务器
func (s *Server) Start() error {
	mux := http.NewServeMux()
	
	// 设备相关接口
	mux.HandleFunc("/api/devices", s.handleDevices)
	mux.HandleFunc("/api/devices/", s.handleDevice)
	
	// 指标相关接口
	mux.HandleFunc("/api/metrics", s.handleMetrics)
	
	// 告警相关接口
	mux.HandleFunc("/api/alerts", s.handleAlerts)
	mux.HandleFunc("/api/alerts/", s.handleAlert)
	
	// 规则相关接口
	mux.HandleFunc("/api/rules", s.handleRules)
	
	// 健康检查
	mux.HandleFunc("/health", s.handleHealth)
	
	fmt.Printf("服务器启动在 %s\n", s.addr)
	return http.ListenAndServe(s.addr, mux)
}

// handleHealth 健康检查
func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
}

// handleDevices 处理设备列表
func (s *Server) handleDevices(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	
	switch r.Method {
	case http.MethodGet:
		devices := s.monitor.GetAllDevices()
		json.NewEncoder(w).Encode(map[string]interface{}{
			"total":    len(devices),
			"devices":  devices,
		})
		
	case http.MethodPost:
		var device model.Device
		if err := json.NewDecoder(r.Body).Decode(&device); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		
		if device.ID == "" || device.Name == "" {
			http.Error(w, "ID 和 Name 不能为空", http.StatusBadRequest)
			return
		}
		
		if err := s.monitor.AddDevice(&device); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		
		w.WriteHeader(http.StatusCreated)
		json.NewEncoder(w).Encode(device)
		
	default:
		http.Error(w, "方法不允许", http.StatusMethodNotAllowed)
	}
}

// handleDevice 处理单个设备
func (s *Server) handleDevice(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	
	id := strings.TrimPrefix(r.URL.Path, "/api/devices/")
	if id == "" {
		http.Error(w, "设备ID不能为空", http.StatusBadRequest)
		return
	}
	
	switch r.Method {
	case http.MethodGet:
		device, err := s.monitor.GetDevice(id)
		if err != nil {
			http.Error(w, err.Error(), http.StatusNotFound)
			return
		}
		json.NewEncoder(w).Encode(device)
		
	case http.MethodDelete:
		if err := s.monitor.DeleteDevice(id); err != nil {
			http.Error(w, err.Error(), http.StatusNotFound)
			return
		}
		w.WriteHeader(http.StatusNoContent)
		
	default:
		http.Error(w, "方法不允许", http.StatusMethodNotAllowed)
	}
}

// handleMetrics 处理指标更新
func (s *Server) handleMetrics(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	
	if r.Method != http.MethodPost {
		http.Error(w, "方法不允许", http.StatusMethodNotAllowed)
		return
	}
	
	var req struct {
		DeviceID   string  `json:"device_id"`
		MetricName string  `json:"metric_name"`
		Value      float64 `json:"value"`
		Unit       string  `json:"unit"`
	}
	
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	
	if err := s.monitor.UpdateMetric(req.DeviceID, req.MetricName, req.Value, req.Unit); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	
	json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
}

// handleAlerts 处理告警列表
func (s *Server) handleAlerts(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	
	switch r.Method {
	case http.MethodGet:
		deviceID := r.URL.Query().Get("device_id")
		limit := 50
		if l := r.URL.Query().Get("limit"); l != "" {
			fmt.Sscanf(l, "%d", &limit)
		}
		
		alerts := s.monitor.GetAlerts(deviceID, limit)
		json.NewEncoder(w).Encode(map[string]interface{}{
			"total":  len(alerts),
			"alerts": alerts,
		})
		
	default:
		http.Error(w, "方法不允许", http.StatusMethodNotAllowed)
	}
}

// handleAlert 处理单个告警
func (s *Server) handleAlert(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	
	id := strings.TrimPrefix(r.URL.Path, "/api/alerts/")
	
	switch r.Method {
	case http.MethodPost:
		s.monitor.ResolveAlert(id)
		json.NewEncoder(w).Encode(map[string]string{"status": "resolved"})
		
	default:
		http.Error(w, "方法不允许", http.StatusMethodNotAllowed)
	}
}

// handleRules 处理告警规则
func (s *Server) handleRules(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	
	if r.Method != http.MethodPost {
		http.Error(w, "方法不允许", http.StatusMethodNotAllowed)
		return
	}
	
	var rule model.AlertRule
	if err := json.NewDecoder(r.Body).Decode(&rule); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	
	rule.ID = fmt.Sprintf("rule_%d", time.Now().UnixNano())
	rule.CreatedAt = time.Now()
	rule.Enabled = true
	
	s.monitor.AddAlertRule(&rule)
	
	json.NewEncoder(w).Encode(rule)
}
