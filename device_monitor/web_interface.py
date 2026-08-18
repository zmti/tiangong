"""
设备监控系统 - Web监控界面
使用Flask提供Web监控界面
"""

from flask import Flask, render_template_string, jsonify, request
from datetime import datetime
import json

app = Flask(__name__)

# 全局监控器引用（由主程序注入）
monitor = None


# HTML模板
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>设备监控系统</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f5f7fa;
            color: #333;
        }
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 20px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        .header h1 { font-size: 24px; }
        .header .status { font-size: 14px; opacity: 0.9; }
        .container { max-width: 1400px; margin: 20px auto; padding: 0 20px; }
        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }
        .stat-card {
            background: white;
            padding: 20px;
            border-radius: 10px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
        }
        .stat-card h3 { font-size: 14px; color: #666; margin-bottom: 8px; }
        .stat-card .value { font-size: 32px; font-weight: bold; color: #333; }
        .stat-card.warning .value { color: #f39c12; }
        .stat-card.danger .value { color: #e74c3c; }
        .stat-card.success .value { color: #27ae60; }
        .section {
            background: white;
            border-radius: 10px;
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
        }
        .section h2 { font-size: 18px; margin-bottom: 15px; padding-bottom: 10px; border-bottom: 2px solid #f0f0f0; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #eee; }
        th { background: #f8f9fa; font-weight: 600; color: #666; }
        tr:hover { background: #f8f9fa; }
        .badge {
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 500;
        }
        .badge-online { background: #d4edda; color: #155724; }
        .badge-offline { background: #f8d7da; color: #721c24; }
        .badge-warning { background: #fff3cd; color: #856404; }
        .badge-critical { background: #f8d7da; color: #721c24; }
        .badge-info { background: #d1ecf1; color: #0c5460; }
        .progress-bar {
            width: 100%;
            height: 8px;
            background: #e9ecef;
            border-radius: 4px;
            overflow: hidden;
        }
        .progress-fill {
            height: 100%;
            border-radius: 4px;
            transition: width 0.3s;
        }
        .progress-fill.low { background: #27ae60; }
        .progress-fill.medium { background: #f39c12; }
        .progress-fill.high { background: #e74c3c; }
        .btn {
            padding: 8px 16px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
            transition: all 0.2s;
        }
        .btn-primary { background: #667eea; color: white; }
        .btn-primary:hover { background: #5a67d8; }
        .btn-danger { background: #e74c3c; color: white; }
        .btn-danger:hover { background: #c0392b; }
        .btn-success { background: #27ae60; color: white; }
        .btn-success:hover { background: #219a52; }
        .form-group { margin-bottom: 15px; }
        .form-group label { display: block; margin-bottom: 5px; font-weight: 500; }
        .form-group input, .form-group select {
            width: 100%;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 6px;
            font-size: 14px;
        }
        .modal {
            display: none;
            position: fixed;
            top: 0; left: 0;
            width: 100%; height: 100%;
            background: rgba(0,0,0,0.5);
            z-index: 1000;
        }
        .modal.show { display: flex; align-items: center; justify-content: center; }
        .modal-content {
            background: white;
            padding: 30px;
            border-radius: 10px;
            width: 90%;
            max-width: 500px;
        }
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        .modal-header h3 { margin: 0; }
        .close { font-size: 24px; cursor: pointer; color: #999; }
        .close:hover { color: #333; }
        .refresh-btn {
            position: fixed;
            bottom: 20px;
            right: 20px;
            width: 50px;
            height: 50px;
            border-radius: 50%;
            background: #667eea;
            color: white;
            border: none;
            font-size: 20px;
            cursor: pointer;
            box-shadow: 0 4px 12px rgba(102,126,234,0.4);
        }
        .refresh-btn:hover { background: #5a67d8; }
    </style>
</head>
<body>
    <div class="header">
        <h1>🖥️ 设备监控系统</h1>
        <div class="status" id="status">加载中...</div>
    </div>
    
    <div class="container">
        <div class="stats">
            <div class="stat-card">
                <h3>设备总数</h3>
                <div class="value" id="total-devices">0</div>
            </div>
            <div class="stat-card success">
                <h3>在线设备</h3>
                <div class="value" id="online-devices">0</div>
            </div>
            <div class="stat-card warning">
                <h3>告警数量</h3>
                <div class="value" id="alert-count">0</div>
            </div>
            <div class="stat-card danger">
                <h3>严重告警</h3>
                <div class="value" id="critical-count">0</div>
            </div>
        </div>
        
        <div class="section">
            <h2>设备列表</h2>
            <table>
                <thead>
                    <tr>
                        <th>设备名称</th>
                        <th>IP地址</th>
                        <th>类型</th>
                        <th>状态</th>
                        <th>CPU</th>
                        <th>内存</th>
                        <th>磁盘</th>
                        <th>最后更新</th>
                        <th>操作</th>
                    </tr>
                </thead>
                <tbody id="device-table"></tbody>
            </table>
            <button class="btn btn-primary" onclick="showAddDeviceModal()" style="margin-top:15px">
                + 添加设备
            </button>
        </div>
        
        <div class="section">
            <h2>告警记录</h2>
            <table>
                <thead>
                    <tr>
                        <th>时间</th>
                        <th>设备</th>
                        <th>级别</th>
                        <th>指标</th>
                        <th>当前值</th>
                        <th>阈值</th>
                        <th>消息</th>
                        <th>操作</th>
                    </tr>
                </thead>
                <tbody id="alert-table"></tbody>
            </table>
        </div>
    </div>
    
    <!-- 添加设备模态框 -->
    <div class="modal" id="addDeviceModal">
        <div class="modal-content">
            <div class="modal-header">
                <h3>添加设备</h3>
                <span class="close" onclick="closeModal()">&times;</span>
            </div>
            <form id="addDeviceForm">
                <div class="form-group">
                    <label>设备名称</label>
                    <input type="text" name="name" required>
                </div>
                <div class="form-group">
                    <label>IP地址</label>
                    <input type="text" name="ip" required>
                </div>
                <div class="form-group">
                    <label>设备类型</label>
                    <select name="device_type">
                        <option value="server">服务器</option>
                        <option value="desktop">桌面机</option>
                        <option value="network">网络设备</option>
                        <option value="other">其他</option>
                    </select>
                </div>
                <button type="submit" class="btn btn-success" style="width:100%">添加设备</button>
            </form>
        </div>
    </div>
    
    <button class="refresh-btn" onclick="refreshData()" title="刷新数据">↻</button>
    
    <script>
        let refreshTimer = null;
        
        async function fetchData() {
            try {
                const response = await fetch('/api/status');
                const data = await response.json();
                updateStats(data);
                
                const devicesResponse = await fetch('/api/devices');
                const devices = await devicesResponse.json();
                renderDevices(devices);
                
                const alertsResponse = await fetch('/api/alerts?limit=20');
                const alerts = await alertsResponse.json();
                renderAlerts(alerts);
            } catch (e) {
                console.error('获取数据失败:', e);
            }
        }
        
        function updateStats(data) {
            document.getElementById('status').textContent = 
                data.is_running ? '监控运行中' : '监控已停止';
            document.getElementById('total-devices').textContent = data.device_count;
            document.getElementById('alert-count').textContent = data.alert_summary.active;
            document.getElementById('critical-count').textContent = data.alert_summary.critical;
            
            // 统计在线设备
            fetch('/api/devices').then(r => r.json()).then(devices => {
                const online = devices.filter(d => d.status === 'online').length;
                document.getElementById('online-devices').textContent = online;
            });
        }
        
        function renderDevices(devices) {
            const tbody = document.getElementById('device-table');
            tbody.innerHTML = devices.map(d => {
                const lastMetrics = d.metrics_history.length > 0 ? d.metrics_history[d.metrics_history.length - 1] : null;
                const cpu = lastMetrics ? lastMetrics.cpu_usage : 0;
                const mem = lastMetrics ? lastMetrics.memory_usage : 0;
                const disk = lastMetrics ? lastMetrics.disk_usage : 0;
                
                return \`
                    <tr>
                        <td><strong>\${d.name}</strong></td>
                        <td>\${d.ip_address}</td>
                        <td>\${d.device_type}</td>
                        <td><span class="badge badge-\${d.status}">\${getStatusText(d.status)}</span></td>
                        <td>
                            <div class="progress-bar">
                                <div class="progress-fill \${getProgressClass(cpu)}" style="width:\${cpu}%"></div>
                            </div>
                            <small>\${cpu.toFixed(1)}%</small>
                        </td>
                        <td>
                            <div class="progress-bar">
                                <div class="progress-fill \${getProgressClass(mem)}" style="width:\${mem}%"></div>
                            </div>
                            <small>\${mem.toFixed(1)}%</small>
                        </td>
                        <td>
                            <div class="progress-bar">
                                <div class="progress-fill \${getProgressClass(disk)}" style="width:\${disk}%"></div>
                            </div>
                            <small>\${disk.toFixed(1)}%</small>
                        </td>
                        <td>\${d.last_seen ? new Date(d.last_seen).toLocaleTimeString() : '从未'}</td>
                        <td>
                            <button class="btn btn-danger" onclick="removeDevice('\${d.device_id}')">删除</button>
                        </td>
                    </tr>
                \`;
            }).join('');
        }
        
        function renderAlerts(alerts) {
            const tbody = document.getElementById('alert-table');
            tbody.innerHTML = alerts.map(a => \`
                <tr>
                    <td>\${new Date(a.timestamp).toLocaleString()}</td>
                    <td>\${a.device_id}</td>
                    <td><span class="badge badge-\${a.alert_level}">\${a.alert_level.toUpperCase()}</span></td>
                    <td>\${getMetricName(a.metric_name)}</td>
                    <td>\${a.current_value.toFixed(1)}%</td>
                    <td>\${a.threshold}%</td>
                    <td>\${a.message}</td>
                    <td>
                        <button class="btn btn-primary" onclick="acknowledgeAlert('\${a.alert_id}')">确认</button>
                    </td>
                </tr>
            \`).join('');
        }
        
        function getProgressClass(value) {
            if (value < 70) return 'low';
            if (value < 90) return 'medium';
            return 'high';
        }
        
        function getStatusText(status) {
            const map = {
                'online': '在线',
                'offline': '离线',
                'warning': '警告',
                'critical': '严重'
            };
            return map[status] || status;
        }
        
        function getMetricName(metric) {
            const map = {
                'cpu_usage': 'CPU',
                'memory_usage': '内存',
                'disk_usage': '磁盘'
            };
            return map[metric] || metric;
        }
        
        function showAddDeviceModal() {
            document.getElementById('addDeviceModal').classList.add('show');
        }
        
        function closeModal() {
            document.getElementById('addDeviceModal').classList.remove('show');
        }
        
        document.getElementById('addDeviceForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const formData = new FormData(e.target);
            const data = {
                name: formData.get('name'),
                ip: formData.get('ip'),
                device_type: formData.get('device_type')
            };
            
            try {
                const response = await fetch('/api/devices', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                });
                const result = await response.json();
                if (result.success) {
                    closeModal();
                    e.target.reset();
                    refreshData();
                }
            } catch (e) {
                alert('添加设备失败');
            }
        });
        
        async function removeDevice(deviceId) {
            if (!confirm('确定要删除此设备吗？')) return;
            try {
                await fetch(\`/api/devices/\${deviceId}\`, { method: 'DELETE' });
                refreshData();
            } catch (e) {
                alert('删除设备失败');
            }
        }
        
        async function acknowledgeAlert(alertId) {
            try {
                await fetch('/api/alerts/acknowledge', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ alert_id: alertId })
                });
                refreshData();
            } catch (e) {
                alert('确认告警失败');
            }
        }
        
        function refreshData() {
            fetchData();
        }
        
        // 自动刷新
        fetchData();
        refreshTimer = setInterval(refreshData, 5000);
    </script>
</body>
</html>
"""


def create_app(monitor_instance):
    """创建Flask应用"""
    global monitor
    monitor = monitor_instance
    
    @app.route('/')
    def index():
        return render_template_string(HTML_TEMPLATE)
    
    @app.route('/api/status')
    def api_status():
        return jsonify(monitor.get_status())
    
    @app.route('/api/devices', methods=['GET'])
    def api_get_devices():
        devices = monitor.get_all_devices()
        return jsonify([d.to_dict() for d in devices])
    
    @app.route('/api/devices', methods=['POST'])
    def api_add_device():
        data = request.json
        from models import Device
        device = Device(
            device_id=f"device_{int(datetime.now().timestamp())}",
            name=data['name'],
            ip_address=data['ip'],
            device_type=data['device_type']
        )
        monitor.add_device(device)
        return jsonify({'success': True, 'device_id': device.device_id})
    
    @app.route('/api/devices/<device_id>', methods=['DELETE'])
    def api_remove_device(device_id):
        monitor.remove_device(device_id)
        return jsonify({'success': True})
    
    @app.route('/api/alerts', methods=['GET'])
    def api_get_alerts():
        device_id = request.args.get('device_id')
        limit = int(request.args.get('limit', 50))
        alerts = monitor.alert_engine.get_alerts(device_id=device_id, limit=limit)
        return jsonify([a.to_dict() for a in alerts])
    
    @app.route('/api/alerts/acknowledge', methods=['POST'])
    def api_acknowledge_alert():
        data = request.json
        success = monitor.alert_engine.acknowledge_alert(data['alert_id'])
        return jsonify({'success': success})
    
    return app


def run_web_interface(monitor_instance, host='0.0.0.0', port=5000):
    """运行Web界面"""
    app = create_app(monitor_instance)
    print(f"启动Web监控界面: http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
