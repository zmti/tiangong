package main

import (
	"fmt"
	"log"
	"os"
	"time"

	"device-monitor/api"
	"device-monitor/model"
	"device-monitor/monitor"
	"device-monitor/storage"
)

func main() {
	// 创建存储
	store, err := storage.NewFileStorage("./data")
	if err != nil {
		log.Fatalf("初始化存储失败: %v", err)
	}
	
	// 创建监控服务
	monitorSvc := monitor.NewMonitorService(store)
	
	// 添加示例设备
	addSampleDevices(monitorSvc)
	
	// 添加告警规则
	addSampleAlertRules(monitorSvc)
	
	// 启动模拟数据采集
	monitorSvc.StartSimulation(2 * time.Second)
	
	// 启动 HTTP API
	server := api.NewServer(monitorSvc, ":8080")
	
	fmt.Println("========================================")
	fmt.Println("   设备监控系统已启动")
	fmt.Println("========================================")
	fmt.Println("API 地址: http://localhost:8080")
	fmt.Println()
	fmt.Println("可用接口:")
	fmt.Println("  GET  /health              - 健康检查")
	fmt.Println("  GET  /api/devices         - 获取所有设备")
	fmt.Println("  POST /api/devices         - 添加设备")
	fmt.Println("  GET  /api/devices/:id     - 获取设备详情")
	fmt.Println("  DEL  /api/devices/:id     - 删除设备")
	fmt.Println("  POST /api/metrics         - 更新指标")
	fmt.Println("  GET  /api/alerts          - 获取告警列表")
	fmt.Println("  POST /api/alerts/:id      - 解决告警")
	fmt.Println("  POST /api/rules           - 添加告警规则")
	fmt.Println("========================================")
	
	// 优雅关闭
	go func() {
		sigChan := make(chan os.Signal, 1)
		// 注意: 实际项目中需要 import "os/signal" 和 "syscall"
		_ = sigChan
	}()
	
	if err := server.Start(); err != nil {
		log.Fatalf("服务器启动失败: %v", err)
	}
}

// addSampleDevices 添加示例设备
func addSampleDevices(m *monitor.MonitorService) {
	devices := []*model.Device{
		model.NewDevice("temp-001", "温度传感器-1", model.TypeSensor, "机房A"),
		model.NewDevice("temp-002", "温度传感器-2", model.TypeSensor, "机房B"),
		model.NewDevice("cam-001", "摄像头-大门", model.TypeCamera, "入口"),
		model.NewDevice("ctrl-001", "空调控制器", model.TypeController, "机房A"),
		model.NewDevice("gw-001", "网关-1", model.TypeGateway, "机房A"),
	}
	
	for _, device := range devices {
		if err := m.AddDevice(device); err != nil {
			log.Printf("添加设备 %s 失败: %v", device.ID, err)
		} else {
			fmt.Printf("✓ 已添加设备: %s (%s)\n", device.Name, device.Type)
		}
	}
}

// addSampleAlertRules 添加示例告警规则
func addSampleAlertRules(m *monitor.MonitorService) {
	rules := []*model.AlertRule{
		{
			ID:         "rule_temp_high",
			DeviceID:   "",
			MetricName: "temperature",
			Condition:  "gt",
			Threshold:  30.0,
			Message:    "温度过高告警",
			Enabled:    true,
		},
		{
			ID:         "rule_temp_low",
			DeviceID:   "",
			MetricName: "temperature",
			Condition:  "lt",
			Threshold:  10.0,
			Message:    "温度过低告警",
			Enabled:    true,
		},
		{
			ID:         "rule_humidity_high",
			DeviceID:   "",
			MetricName: "humidity",
			Condition:  "gt",
			Threshold:  70.0,
			Message:    "湿度过高告警",
			Enabled:    true,
		},
		{
			ID:         "rule_cpu_high",
			DeviceID:   "",
			MetricName: "cpu_load",
			Condition:  "gt",
			Threshold:  80.0,
			Message:    "CPU 负载过高告警",
			Enabled:    true,
		},
		{
			ID:         "rule_latency_high",
			DeviceID:   "",
			MetricName: "latency",
			Condition:  "gt",
			Threshold:  50.0,
			Message:    "网络延迟过高告警",
			Enabled:    true,
		},
	}
	
	for _, rule := range rules {
		m.AddAlertRule(rule)
		fmt.Printf("✓ 已添加告警规则: %s\n", rule.Message)
	}
}
