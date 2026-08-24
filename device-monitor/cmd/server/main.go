package main

import (
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"

	_ "github.com/go-sql-driver/mysql"
	"github.com/device-monitor/internal/config"
	"github.com/device-monitor/internal/database"
	"github.com/device-monitor/internal/handler"
	"github.com/device-monitor/internal/service"
	"github.com/device-monitor/pkg/logger"
	"github.com/gorilla/mux"
)

func main() {
	// 解析命令行参数
	configFile := flag.String("config", ".env", "配置文件路径")
	flag.Parse()

	// 加载配置
	cfg, err := config.Load(*configFile)
	if err != nil {
		log.Fatalf("加载配置失败: %v", err)
	}

	// 初始化日志
	if err := logger.Init(cfg.LogLevel, cfg.LogFile); err != nil {
		log.Fatalf("初始化日志失败: %v", err)
	}

	// 初始化数据库
	db, err := database.Init(cfg)
	if err != nil {
		log.Fatalf("初始化数据库失败: %v", err)
	}
	defer db.Close()

	logger.Info("数据库连接成功")

	// 初始化服务
	deviceService := service.NewDeviceService(db)
	alertService := service.NewAlertService(db)

	// 初始化处理器
	deviceHandler := handler.NewDeviceHandler(deviceService)
	alertHandler := handler.NewAlertHandler(alertService)

	// 创建路由
	r := mux.NewRouter()

	// API v1 路由
	api := r.PathPrefix("/api/v1").Subrouter()

	// 设备相关路由
	api.HandleFunc("/devices", deviceHandler.ListDevices).Methods("GET")
	api.HandleFunc("/devices", deviceHandler.CreateDevice).Methods("POST")
	api.HandleFunc("/devices/{id}", deviceHandler.GetDevice).Methods("GET")
	api.HandleFunc("/devices/{id}", deviceHandler.UpdateDevice).Methods("PUT")
	api.HandleFunc("/devices/{id}", deviceHandler.DeleteDevice).Methods("DELETE")
	api.HandleFunc("/devices/{id}/metrics", deviceHandler.GetMetrics).Methods("GET")
	api.HandleFunc("/devices/{id}/metrics", deviceHandler.ReportMetric).Methods("POST")
	api.HandleFunc("/devices/{id}/metrics/latest", deviceHandler.GetLatestMetrics).Methods("GET")

	// 告警相关路由
	api.HandleFunc("/alerts", alertHandler.ListAlerts).Methods("GET")
	api.HandleFunc("/alerts/{id}/acknowledge", alertHandler.AcknowledgeAlert).Methods("POST")
	api.HandleFunc("/alerts/{id}/resolve", alertHandler.ResolveAlert).Methods("POST")

	// 健康检查
	r.HandleFunc("/health", handler.HealthCheck).Methods("GET")

	// 启动服务器
	addr := cfg.Host + ":" + cfg.Port
	logger.Infof("启动服务器: http://%s", addr)

	go func() {
		if err := r.ListenAndServe(addr); err != nil {
			logger.Errorf("服务器启动失败: %v", err)
		}
	}()

	// 优雅关闭
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit

	logger.Info("正在关闭服务器...")
	logger.Info("服务器已停止")
}
