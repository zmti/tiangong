/**
 * 工作流设计器 - 拖拽式 DAG 编辑器
 * 支持节点拖拽、连线、条件分支、循环等
 */
class WorkflowDesigner {
    constructor(container, options = {}) {
        this.container = typeof container === 'string' 
            ? document.querySelector(container) 
            : container;
        this.options = {
            nodeTypes: ['start', 'end', 'agent', 'tool', 'condition', 'loop', 'http', 'sleep', 'script'],
            ...options
        };
        this.nodes = new Map();
        this.edges = new Map();
        this.selectedNode = null;
        this.dragging = null;
        this.connecting = null;
        this.nodeIdCounter = 0;
        this.init();
    }

    init() {
        this.render();
        this.bindEvents();
        this.loadToolbar();
    }

    render() {
        this.container.innerHTML = `
            <div class="workflow-designer">
                <div class="toolbar">
                    <div class="toolbar-group">
                        <button class="tool-btn" data-action="add-node" data-type="agent">🤖 Agent</button>
                        <button class="tool-btn" data-action="add-node" data-type="tool">🔧 工具</button>
                        <button class="tool-btn" data-action="add-node" data-type="condition">❓ 条件</button>
                        <button class="tool-btn" data-action="add-node" data-type="loop">🔄 循环</button>
                        <button class="tool-btn" data-action="add-node" data-type="http">🌐 HTTP</button>
                        <button class="tool-btn" data-action="add-node" data-type="sleep">⏱️ 延时</button>
                        <button class="tool-btn" data-action="add-node" data-type="script">📝 脚本</button>
                    </div>
                    <div class="toolbar-group">
                        <button class="tool-btn" data-action="run">▶️ 运行</button>
                        <button class="tool-btn" data-action="save">💾 保存</button>
                        <button class="tool-btn" data-action="clear">🗑️ 清空</button>
                    </div>
                </div>
                <div class="canvas" id="workflow-canvas">
                    <svg class="edges-svg" id="edges-svg">
                        <defs>
                            <marker id="arrowhead" markerWidth="10" markerHeight="7" 
                                    refX="9" refY="3.5" orient="auto">
                                <polygon points="0 0, 10 3.5, 0 7" fill="#666"/>
                            </marker>
                        </defs>
                    </svg>
                    <div class="nodes-container" id="nodes-container"></div>
                </div>
                <div class="properties-panel" id="properties-panel" style="display:none;">
                    <h3>节点属性</h3>
                    <div class="property-group">
                        <label>名称</label>
                        <input type="text" id="prop-name" placeholder="节点名称">
                    </div>
                    <div class="property-group">
                        <label>类型</label>
                        <select id="prop-type">
                            <option value="agent">Agent</option>
                            <option value="tool">工具</option>
                            <option value="condition">条件</option>
                            <option value="loop">循环</option>
                            <option value="http">HTTP</option>
                            <option value="sleep">延时</option>
                            <option value="script">脚本</option>
                        </select>
                    </div>
                    <div class="property-group" id="prop-config">
                        <label>配置</label>
                        <textarea id="prop-config-json" placeholder='{"key": "value"}'></textarea>
                    </div>
                    <button class="tool-btn" data-action="delete-node">删除节点</button>
                </div>
            </div>
        `;
    }

    loadToolbar() {
        // 添加工具栏事件
        this.container.querySelectorAll('.tool-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const action = e.currentTarget.dataset.action;
                const type = e.currentTarget.dataset.type;
                this.handleToolbarAction(action, type);
            });
        });
    }

    handleToolbarAction(action, type) {
        switch (action) {
            case 'add-node':
                this.addNode(type);
                break;
            case 'run':
                this.runWorkflow();
                break;
            case 'save':
                this.saveWorkflow();
                break;
            case 'clear':
                this.clearCanvas();
                break;
            case 'delete-node':
                this.deleteSelectedNode();
                break;
        }
    }

    addNode(type, x = 100, y = 100, config = {}) {
        const nodeId = `node_${++this.nodeIdCounter}`;
        const node = {
            id: nodeId,
            type: type,
            name: `${type}_node_${this.nodeIdCounter}`,
            x: x,
            y: y,
            config: config,
            branches: {}
        };
        this.nodes.set(nodeId, node);
        this.renderNode(node);
        this.selectNode(nodeId);
        return nodeId;
    }

    renderNode(node) {
        const nodeEl = document.createElement('div');
        nodeEl.className = `workflow-node node-${node.type}`;
        nodeEl.id = node.id;
        nodeEl.style.left = `${node.x}px`;
        nodeEl.style.top = `${node.y}px`;
        
        const icons = {
            start: '🚀', end: '🏁', agent: '🤖', tool: '🔧',
            condition: '❓', loop: '🔄', http: '🌐', sleep: '⏱️', script: '📝'
        };
        
        nodeEl.innerHTML = `
            <div class="node-header">
                <span class="node-icon">${icons[node.type] || '📦'}</span>
                <span class="node-name">${node.name}</span>
            </div>
            <div class="node-ports">
                <div class="port port-in" data-port="in" data-type="input"></div>
                <div class="port port-out" data-port="out" data-type="output"></div>
            </div>
        `;
        
        this.bindNodeEvents(nodeEl, node);
        document.getElementById('nodes-container').appendChild(nodeEl);
    }

    bindNodeEvents(el, node) {
        // 拖拽
        el.addEventListener('mousedown', (e) => {
            if (e.target.classList.contains('port')) return;
            this.startDrag(e, node);
        });
        
        // 点击选择
        el.addEventListener('click', (e) => {
            if (!e.target.classList.contains('port')) {
                this.selectNode(node.id);
            }
        });
        
        // 连线开始
        const outputPort = el.querySelector('.port-out');
        outputPort.addEventListener('mousedown', (e) => {
            e.stopPropagation();
            this.startConnection(node.id, e);
        });
        
        // 连线结束
        const inputPort = el.querySelector('.port-in');
        inputPort.addEventListener('mouseup', (e) => {
            e.stopPropagation();
            if (this.connecting) {
                this.endConnection(node.id);
            }
        });
    }

    startDrag(e, node) {
        this.dragging = node;
        const startX = e.clientX - node.x;
        const startY = e.clientY - node.y;
        
        const onMove = (e) => {
            if (!this.dragging) return;
            this.dragging.x = e.clientX - startX;
            this.dragging.y = e.clientY - startY;
            const el = document.getElementById(this.dragging.id);
            if (el) {
                el.style.left = `${this.dragging.x}px`;
                el.style.top = `${this.dragging.y}px`;
            }
            this.updateEdges();
        };
        
        const onUp = () => {
            this.dragging = null;
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
        };
        
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    }

    startConnection(sourceId, e) {
        this.connecting = sourceId;
        const svg = document.getElementById('edges-svg');
        
        const tempLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        tempLine.setAttribute('class', 'temp-edge');
        tempLine.setAttribute('stroke', '#999');
        tempLine.setAttribute('stroke-width', '2');
        tempLine.setAttribute('stroke-dasharray', '5,5');
        svg.appendChild(tempLine);
        
        const sourceEl = document.getElementById(sourceId);
        const sourceRect = sourceEl.getBoundingClientRect();
        const canvasRect = this.container.getBoundingClientRect();
        
        const startX = sourceRect.right - canvasRect.left;
        const startY = sourceRect.top + sourceRect.height / 2 - canvasRect.top;
        
        const onMove = (e) => {
            const endX = e.clientX - canvasRect.left;
            const endY = e.clientY - canvasRect.top;
            tempLine.setAttribute('x1', startX);
            tempLine.setAttribute('y1', startY);
            tempLine.setAttribute('x2', endX);
            tempLine.setAttribute('y2', endY);
        };
        
        const onUp = (e) => {
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
            svg.removeChild(tempLine);
            
            // 检查是否悬停在另一个节点的 input port 上
            const target = document.elementFromPoint(e.clientX, e.clientY);
            if (target && target.classList.contains('port-in')) {
                const targetId = target.closest('.workflow-node').id;
                if (targetId !== sourceId) {
                    this.addEdge(sourceId, targetId);
                }
            }
            this.connecting = null;
        };
        
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    }

    addEdge(sourceId, targetId) {
        const edgeId = `edge_${sourceId}_${targetId}`;
        this.edges.set(edgeId, {
            id: edgeId,
            source: sourceId,
            target: targetId,
            condition: null
        });
        this.renderEdge(this.edges.get(edgeId));
    }

    renderEdge(edge) {
        const svg = document.getElementById('edges-svg');
        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('class', 'edge');
        path.setAttribute('id', edge.id);
        path.setAttribute('fill', 'none');
        path.setAttribute('stroke', '#666');
        path.setAttribute('stroke-width', '2');
        path.setAttribute('marker-end', 'url(#arrowhead)');
        svg.appendChild(path);
        this.updateEdgePath(edge);
    }

    updateEdgePath(edge) {
        const sourceEl = document.getElementById(edge.source);
        const targetEl = document.getElementById(edge.target);
        if (!sourceEl || !targetEl) return;
        
        const canvasRect = this.container.getBoundingClientRect();
        const sourceRect = sourceEl.getBoundingClientRect();
        const targetRect = targetEl.getBoundingClientRect();
        
        const x1 = sourceRect.right - canvasRect.left;
        const y1 = sourceRect.top + sourceRect.height / 2 - canvasRect.top;
        const x2 = targetRect.left - canvasRect.left;
        const y2 = targetRect.top + targetRect.height / 2 - canvasRect.top;
        
        const path = document.getElementById(edge.id);
        if (path) {
            const dx = Math.abs(x2 - x1) * 0.5;
            const d = `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
            path.setAttribute('d', d);
        }
    }

    updateEdges() {
        this.edges.forEach(edge => this.updateEdgePath(edge));
    }

    selectNode(nodeId) {
        // 取消之前的选择
        document.querySelectorAll('.workflow-node.selected').forEach(el => {
            el.classList.remove('selected');
        });
        
        this.selectedNode = nodeId;
        const el = document.getElementById(nodeId);
        if (el) el.classList.add('selected');
        
        // 显示属性面板
        const node = this.nodes.get(nodeId);
        if (node) {
            const panel = document.getElementById('properties-panel');
            panel.style.display = 'block';
            document.getElementById('prop-name').value = node.name;
            document.getElementById('prop-type').value = node.type;
            document.getElementById('prop-config-json').value = JSON.stringify(node.config, null, 2);
        }
    }

    deleteSelectedNode() {
        if (!this.selectedNode) return;
        const nodeId = this.selectedNode;
        
        // 删除相关边
        const edgesToRemove = [];
        this.edges.forEach((edge, edgeId) => {
            if (edge.source === nodeId || edge.target === nodeId) {
                edgesToRemove.push(edgeId);
            }
        });
        edgesToRemove.forEach(id => this.edges.delete(id));
        
        // 删除节点
        this.nodes.delete(nodeId);
        document.getElementById(nodeId)?.remove();
        
        this.selectedNode = null;
        document.getElementById('properties-panel').style.display = 'none';
    }

    runWorkflow() {
        const workflow = this.exportWorkflow();
        if (!workflow.entry_node) {
            alert('请设置入口节点');
            return;
        }
        
        console.log('运行工作流:', workflow);
        // 调用后端 API 运行
        fetch('/v1/workflows/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(workflow)
        })
        .then(res => res.json())
        .then(data => console.log('运行结果:', data))
        .catch(err => console.error('运行失败:', err));
    }

    saveWorkflow() {
        const workflow = this.exportWorkflow();
        localStorage.setItem('current_workflow', JSON.stringify(workflow));
        console.log('保存工作流:', workflow);
        alert('工作流已保存到本地');
    }

    clearCanvas() {
        if (!confirm('确定要清空画布吗？')) return;
        this.nodes.clear();
        this.edges.clear();
        this.selectedNode = null;
        document.getElementById('nodes-container').innerHTML = '';
        document.getElementById('edges-svg').innerHTML = '<defs><marker id="arrowhead" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto"><polygon points="0 0, 10 3.5, 0 7" fill="#666"/></marker></defs>';
        document.getElementById('properties-panel').style.display = 'none';
        this.nodeIdCounter = 0;
    }

    exportWorkflow() {
        return {
            nodes: Array.from(this.nodes.values()).map(n => ({
                id: n.id,
                type: n.type,
                name: n.name,
                config: n.config,
                branches: n.branches
            })),
            edges: Array.from(this.edges.values()).map(e => ({
                source: e.source,
                target: e.target,
                condition: e.condition
            })),
            entry_node: this.nodes.find(n => n.type === 'start')?.id
        };
    }

    importWorkflow(data) {
        this.clearCanvas();
        data.nodes.forEach(node => {
            this.addNode(node.type, node.x || 100, node.y || 100, node.config);
        });
        data.edges.forEach(edge => {
            this.addEdge(edge.source, edge.target);
        });
    }
}

// 导出
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { WorkflowDesigner };
}
