package config

import (
	"fmt"
	"os"
	"strconv"

	"github.com/joho/godotenv"
)

// Config 系统配置
type Config struct {
	Host         string
	Port         string
	DBDriver     string
	DBHost       string
	DBPort       string
	DBUser       string
	DBPassword   string
	DBName       string
	DBSSLMode    string
	LogLevel     string
	LogFile      string
	AlertEmail   string
	AlertTemp    float64
	AlertCPU     float64
}

// Load 加载配置
func Load(envFile string) (*Config, error) {
	// 加载 .env 文件
	if err := godotenv.Load(envFile); err != nil {
		// 文件不存在时忽略，使用环境变量
		if !os.IsNotExist(err) {
			return nil, fmt.Errorf("加载环境变量文件失败: %w", err)
		}
	}

	cfg := &Config{
		Host:         getEnv("SERVER_HOST", "0.0.0.0"),
		Port:         getEnv("SERVER_PORT", "8080"),
		DBDriver:     getEnv("DB_DRIVER", "mysql"),
		DBHost:       getEnv("DB_HOST", "localhost"),
		DBPort:       getEnv("DB_PORT", "3306"),
		DBUser:       getEnv("DB_USER", "root"),
		DBPassword:   getEnv("DB_PASSWORD", ""),
		DBName:       getEnv("DB_NAME", "device_monitor"),
		DBSSLMode:    getEnv("DB_SSL_MODE", "false"),
		LogLevel:     getEnv("LOG_LEVEL", "info"),
		LogFile:      getEnv("LOG_FILE", "logs/app.log"),
		AlertEmail:   getEnv("ALERT_EMAIL", ""),
		AlertTemp:    getFloatEnv("ALERT_THRESHOLD_TEMP", 80.0),
		AlertCPU:     getFloatEnv("ALERT_THRESHOLD_CPU", 90.0),
	}

	return cfg, nil
}

func getEnv(key, defaultVal string) string {
	if val, ok := os.LookupEnv(key); ok {
		return val
	}
	return defaultVal
}

func getFloatEnv(key string, defaultVal float64) float64 {
	if val, ok := os.LookupEnv(key); ok {
		if f, err := strconv.ParseFloat(val, 64); err == nil {
			return f
		}
	}
	return defaultVal
}

// GetDSN 获取数据库连接字符串
func (c *Config) GetDSN() string {
	if c.DBDriver == "postgres" {
		return fmt.Sprintf("host=%s port=%s user=%s password=%s dbname=%s sslmode=%s",
			c.DBHost, c.DBPort, c.DBUser, c.DBPassword, c.DBName, c.DBSSLMode)
	}
	// MySQL
	return fmt.Sprintf("%s:%s@tcp(%s:%s)/%s?charset=utf8mb4&parseTime=True&loc=Local",
		c.DBUser, c.DBPassword, c.DBHost, c.DBPort, c.DBName)
}
