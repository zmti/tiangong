package logger

import (
	"fmt"
	"log"
	"os"
	"path/filepath"
	"runtime"
	"time"
)

// LogLevel 日志级别
type LogLevel int

const (
	DEBUG LogLevel = iota
	INFO
	WARN
	ERROR
	FATAL
)

var currentLevel LogLevel = INFO

// Init 初始化日志
func Init(level, logFile string) error {
	switch level {
	case "debug":
		currentLevel = DEBUG
	case "info":
		currentLevel = INFO
	case "warn":
		currentLevel = WARN
	case "error":
		currentLevel = ERROR
	}

	// 创建日志目录
	if logFile != "" {
		dir := filepath.Dir(logFile)
		if err := os.MkdirAll(dir, 0755); err != nil {
			return fmt.Errorf("创建日志目录失败: %w", err)
		}

		f, err := os.OpenFile(logFile, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0644)
		if err != nil {
			return fmt.Errorf("打开日志文件失败: %w", err)
		}
		log.SetOutput(f)
	}

	log.SetFlags(log.Ldate | log.Ltime | log.Lshortfile)
	return nil
}

func formatMessage(message string) string {
	return fmt.Sprintf("[%s] %s", time.Now().Format("2006-01-02 15:04:05"), message)
}

func callerInfo() string {
	_, file, line, _ := runtime.Caller(2)
	short := filepath.Base(file)
	return fmt.Sprintf("%s:%d", short, line)
}

// Debug 打印调试日志
func Debug(v ...interface{}) {
	if currentLevel <= DEBUG {
		log.Printf("[DEBUG] %s: %v", callerInfo(), v...)
	}
}

// Info 打印信息日志
func Info(v ...interface{}) {
	if currentLevel <= INFO {
		log.Printf("[INFO] %s: %v", callerInfo(), v...)
	}
}

// Warn 打印警告日志
func Warn(v ...interface{}) {
	if currentLevel <= WARN {
		log.Printf("[WARN] %s: %v", callerInfo(), v...)
	}
}

// Error 打印错误日志
func Error(v ...interface{}) {
	if currentLevel <= ERROR {
		log.Printf("[ERROR] %s: %v", callerInfo(), v...)
	}
}

// Debugf 格式化打印调试日志
func Debugf(format string, args ...interface{}) {
	if currentLevel <= DEBUG {
		log.Printf("[DEBUG] %s: "+format, append([]interface{}{callerInfo()}, args...)...)
	}
}

// Infof 格式化打印信息日志
func Infof(format string, args ...interface{}) {
	if currentLevel <= INFO {
		log.Printf("[INFO] %s: "+format, append([]interface{}{callerInfo()}, args...)...)
	}
}

// Warnf 格式化打印警告日志
func Warnf(format string, args ...interface{}) {
	if currentLevel <= WARN {
		log.Printf("[WARN] %s: "+format, append([]interface{}{callerInfo()}, args...)...)
	}
}

// Errorf 格式化打印错误日志
func Errorf(format string, args ...interface{}) {
	if currentLevel <= ERROR {
		log.Printf("[ERROR] %s: "+format, append([]interface{}{callerInfo()}, args...)...)
	}
}

// Fatal 打印致命错误并退出
func Fatal(v ...interface{}) {
	log.Printf("[FATAL] %s: %v", callerInfo(), v...)
	os.Exit(1)
}

// Fatalf 格式化打印致命错误并退出
func Fatalf(format string, args ...interface{}) {
	log.Printf("[FATAL] %s: "+format, append([]interface{}{callerInfo()}, args...)...)
	os.Exit(1)
}
