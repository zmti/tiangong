package handler

import (
	"encoding/json"
	"net/http"
)

// HealthCheck 健康检查
func HealthCheck(w http.ResponseWriter, r *http.Request) {
	json.NewEncoder(w).Encode(map[string]interface{}{
		"status":    "ok",
		"timestamp": "now",
	})
}
