/**
 * 工作流模板库
 * 提供常用的工作流模板
 */
const WorkflowTemplates = {
    // 简单问答流程
    simple_chat: {
        name: '简单问答',
        description: '基础的用户提问-Agent回答流程',
        nodes: [
            { id: 'start', type: 'start', name: '开始', x: 50, y: 200 },
            { id: 'agent', type: 'agent', name: 'Agent 回答', x: 250, y: 200, config: { input: '{{query}}' } },
            { id: 'end', type: 'end', name: '结束', x: 450, y: 200 }
        ],
        edges: [
            { source: 'start', target: 'agent' },
            { source: 'agent', target: 'end' }
        ]
    },

    // 带条件分支的问答
    conditional_chat: {
        name: '条件问答',
        description: '根据问题类型路由到不同处理流程',
        nodes: [
            { id: 'start', type: 'start', name: '开始', x: 50, y: 200 },
            { id: 'classify', type: 'condition', name: '问题分类', x: 200, y: 200, config: { expression: 'isTechnical(query)' } },
            { id: 'tech', type: 'agent', name: '技术回答', x: 350, y: 100, config: { input: '{{query}}' } },
            { id: 'general', type: 'agent', name: '通用回答', x: 350, y: 300, config: { input: '{{query}}' } },
            { id: 'end', type: 'end', name: '结束', x: 500, y: 200 }
        ],
        edges: [
            { source: 'start', target: 'classify' },
            { source: 'classify', target: 'tech', condition: 'result == "technical"' },
            { source: 'classify', target: 'general', condition: 'result == "general"' },
            { source: 'tech', target: 'end' },
            { source: 'general', target: 'end' }
        ]
    },

    // 代码审查流程
    code_review: {
        name: '代码审查',
        description: '代码提交 -> 分析 -> 评审 -> 报告',
        nodes: [
            { id: 'start', type: 'start', name: '开始', x: 50, y: 200 },
            { id: 'fetch', type: 'tool', name: '获取代码', x: 200, y: 200, config: { tool: 'git_fetch' } },
            { id: 'analyze', type: 'agent', name: '代码分析', x: 350, y: 200, config: { input: '分析代码质量和安全性' } },
            { id: 'review', type: 'condition', name: '质量检查', x: 500, y: 200, config: { expression: 'score < 80' } },
            { id: 'fix', type: 'agent', name: '自动修复', x: 650, y: 100, config: { input: '修复代码问题' } },
            { id: 'report', type: 'agent', name: '生成报告', x: 650, y: 300, config: { input: '生成审查报告' } },
            { id: 'end', type: 'end', name: '结束', x: 800, y: 200 }
        ],
        edges: [
            { source: 'start', target: 'fetch' },
            { source: 'fetch', target: 'analyze' },
            { source: 'analyze', target: 'review' },
            { source: 'review', target: 'fix', condition: 'needsFix' },
            { source: 'review', target: 'report', condition: '!needsFix' },
            { source: 'fix', target: 'report' },
            { source: 'report', target: 'end' }
        ]
    },

    // RAG 文档问答
    rag_qa: {
        name: 'RAG 文档问答',
        description: '检索知识库 -> 生成回答',
        nodes: [
            { id: 'start', type: 'start', name: '开始', x: 50, y: 200 },
            { id: 'search', type: 'tool', name: '检索知识库', x: 200, y: 200, config: { tool: 'rag_search' } },
            { id: 'generate', type: 'agent', name: '生成回答', x: 350, y: 200, config: { input: '基于检索结果回答' } },
            { id: 'end', type: 'end', name: '结束', x: 500, y: 200 }
        ],
        edges: [
            { source: 'start', target: 'search' },
            { source: 'search', target: 'generate' },
            { source: 'generate', target: 'end' }
        ]
    },

    // 定时任务流程
    scheduled_task: {
        name: '定时任务',
        description: '定时触发 -> 执行任务 -> 通知结果',
        nodes: [
            { id: 'start', type: 'start', name: '开始', x: 50, y: 200 },
            { id: 'sleep', type: 'sleep', name: '等待', x: 200, y: 200, config: { seconds: 3600 } },
            { id: 'execute', type: 'agent', name: '执行任务', x: 350, y: 200, config: { input: '执行定时任务' } },
            { id: 'notify', type: 'http', name: '发送通知', x: 500, y: 200, config: { method: 'POST', url: 'https://api.example.com/notify' } },
            { id: 'end', type: 'end', name: '结束', x: 650, y: 200 }
        ],
        edges: [
            { source: 'start', target: 'sleep' },
            { source: 'sleep', target: 'execute' },
            { source: 'execute', target: 'notify' },
            { source: 'notify', target: 'end' }
        ]
    },

    // 多智能体协作
    multi_agent: {
        name: '多智能体协作',
        description: '主智能体委派子任务并行执行',
        nodes: [
            { id: 'start', type: 'start', name: '开始', x: 50, y: 200 },
            { id: 'main', type: 'agent', name: '主 Agent', x: 200, y: 200, config: { input: '分析问题并分配任务' } },
            { id: 'research', type: 'agent', name: '研究员', x: 350, y: 100, config: { input: '进行资料研究' } },
            { id: 'coder', type: 'agent', name: '程序员', x: 350, y: 300, config: { input: '编写代码实现' } },
            { id: 'merge', type: 'tool', name: '合并结果', x: 500, y: 200, config: { tool: 'merge_results' } },
            { id: 'end', type: 'end', name: '结束', x: 650, y: 200 }
        ],
        edges: [
            { source: 'start', target: 'main' },
            { source: 'main', target: 'research' },
            { source: 'main', target: 'coder' },
            { source: 'research', target: 'merge' },
            { source: 'coder', target: 'merge' },
            { source: 'merge', target: 'end' }
        ]
    },

    // 数据管道
    data_pipeline: {
        name: '数据管道',
        description: '数据采集 -> 处理 -> 存储 -> 分析',
        nodes: [
            { id: 'start', type: 'start', name: '开始', x: 50, y: 200 },
            { id: 'collect', type: 'http', name: '采集数据', x: 200, y: 200, config: { method: 'GET', url: 'https://api.example.com/data' } },
            { id: 'process', type: 'script', name: '数据处理', x: 350, y: 200, config: { code: 'process_data(raw_data)' } },
            { id: 'store', type: 'tool', name: '存储', x: 500, y: 200, config: { tool: 'database_insert' } },
            { id: 'analyze', type: 'agent', name: '数据分析', x: 650, y: 200, config: { input: '分析存储的数据' } },
            { id: 'end', type: 'end', name: '结束', x: 800, y: 200 }
        ],
        edges: [
            { source: 'start', target: 'collect' },
            { source: 'collect', target: 'process' },
            { source: 'process', target: 'store' },
            { source: 'store', target: 'analyze' },
            { source: 'analyze', target: 'end' }
        ]
    }
};

// 导出
if (typeof module !== 'undefined' && module.exports) {
    module.exports = WorkflowTemplates;
}
