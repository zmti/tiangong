/**
 * TianGong Desktop UI v2.0 现代化重构 - 设计说明
 * 
 * 设计理念: 合理、高效、现代、友好
 */

/* ============================================================
   1. 设计令牌体系升级
   ============================================================ */

/* --- 色彩系统 --- */
/* 
 * 原有: 紫色(#7c5cfc)为主色调，蓝色(#38bdf8)为强调色
 * 新版: 靛蓝(#6366f1)为主色调，青色(#06b6d4)为强调色
 * 
 * 改进点:
 * - 主色调更中性，减少视觉疲劳
 * - 语义色更明确，符合行业惯例
 * - 背景层级从2层增加到3层，层次更清晰
 */

:root {
  /* 主色调: 靛蓝 */
  --primary: #6366f1;
  --primary-light: #818cf8;
  --primary-dark: #4f46e5;
  --primary-soft: rgba(99, 102, 241, 0.10);
  --primary-border: rgba(99, 102, 241, 0.25);
  
  /* 强调色: 青色 */
  --accent: #06b6d4;
  --accent-soft: rgba(6, 182, 212, 0.10);
  
  /* 语义色 */
  --ok: #10b981;       /* 成功: 绿色 */
  --err: #ef4444;      /* 错误: 红色 */
  --warn: #f59e0b;     /* 警告: 琥珀色 */
  --info: #3b82f6;     /* 信息: 蓝色 */
  
  /* 背景层级: 3层体系 */
  --bg-base: #f1f5f9;    /* 最底层: 页面背景 */
  --bg-surface: #ffffff; /* 中间层: 卡片/面板 */
  --bg-elev: #f8fafc;    /* 最高层: 悬浮元素 */
  
  /* 文字层级: 4级 */
  --text-primary: #0f172a;   /* 一级: 标题/重要内容 */
  --text-secondary: #475569; /* 二级: 正文 */
  --text-muted: #94a3b8;     /* 三级: 次要信息 */
  --text-faint: #cbd5e1;     /* 四级: 提示/占位 */
  
  /* 边框 */
  --border: rgba(148, 163, 184, 0.25);
  --border-strong: rgba(148, 163, 184, 0.50);
  
  /* 圆角: 统一8px体系 */
  --radius-xs: 4px;
  --radius-sm: 8px;
  --radius-md: 12px;
  --radius-lg: 16px;
  --radius-xl: 24px;
  
  /* 阴影: 层次化 */
  --shadow-xs: 0 1px 2px rgba(15, 23, 42, 0.04);
  --shadow-sm: 0 2px 8px rgba(15, 23, 42, 0.06);
  --shadow-md: 0 4px 16px rgba(15, 23, 42, 0.08);
  --shadow-lg: 0 8px 32px rgba(15, 23, 42, 0.10);
  
  /* 间距: 4px网格 */
  --space-xs: 4px;
  --space-sm: 8px;
  --space-md: 16px;
  --space-lg: 24px;
  --space-xl: 32px;
  
  /* 动效 */
  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
  --ease-spring: cubic-bezier(0.34, 1.56, 0.64, 1);
  --duration-fast: 150ms;
  --duration-normal: 250ms;
  --duration-slow: 400ms;
}

/* 深色主题 */
[data-theme="dark"] {
  --primary: #818cf8;
  --primary-light: #a5b4fc;
  --primary-dark: #6366f1;
  --primary-soft: rgba(129, 140, 248, 0.12);
  --primary-border: rgba(129, 140, 248, 0.30);
  --accent: #22d3ee;
  --accent-soft: rgba(34, 211, 238, 0.12);
  
  --bg-base: #0b0f19;
  --bg-surface: #111827;
  --bg-elev: #1a2035;
  --bg-hover: rgba(129, 140, 248, 0.06);
  
  --text-primary: #f1f5f9;
  --text-secondary: #94a3b8;
  --text-muted: #64748b;
  --text-faint: #475569;
  
  --border: rgba(255, 255, 255, 0.06);
  --border-strong: rgba(255, 255, 255, 0.12);
  
  --shadow-xs: 0 1px 2px rgba(0, 0, 0, 0.30);
  --shadow-sm: 0 2px 8px rgba(0, 0, 0, 0.40);
  --shadow-md: 0 4px 16px rgba(0, 0, 0, 0.50);
  --shadow-lg: 0 8px 32px rgba(0, 0, 0, 0.60);
}

/* ============================================================
   2. 布局系统重构
   ============================================================ */

/* --- 三栏布局 --- */
/* 
 * 原布局: 图标栏(58px) + 会话栏(248px) + 主区域 + 右侧面板(440px)
 * 新布局: 图标栏(64px) + 会话栏(280px) + 主区域 + 右侧面板(440px)
 * 
 * 改进点:
 * - 图标栏加宽至64px，提升点击舒适度
 * - 会话栏加宽至280px，显示更多内容
 * - 统一使用CSS变量控制宽度，便于动态调整
 */

.app-layout {
  display: flex;
  height: 100vh;
  overflow: hidden;
  background: var(--bg-base);
}

.nav-sidebar {
  width: 64px;
  flex-shrink: 0;
  background: var(--bg-surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: var(--space-md) 0;
  z-index: 100;
  transition: width var(--duration-normal) var(--ease-out);
}

.session-panel {
  width: 280px;
  flex-shrink: 0;
  background: var(--bg-surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  transition: width var(--duration-normal) var(--ease-out);
}

/* --- 消息区域 --- */
#messages {
  flex: 1 1 auto;
  overflow-y: auto;
  padding: var(--space-lg) clamp(20px, 5%, 40px);
  scroll-behavior: smooth;
  background: var(--bg-base);
}

/* ============================================================
   3. 组件样式增强
   ============================================================ */

/* --- 导航按钮 --- */
.nav-btn {
  width: 44px;
  height: 44px;
  border-radius: var(--radius-md);
  color: var(--text-muted);
  transition: all var(--duration-fast) var(--ease-out);
}

.nav-btn:hover {
  background: var(--bg-hover);
  color: var(--primary);
}

.nav-btn.active {
  background: var(--primary-soft);
  color: var(--primary);
}

.nav-btn.active::before {
  content: '';
  position: absolute;
  left: -10px;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 24px;
  border-radius: 0 2px 2px 0;
  background: var(--primary);
}

/* --- 输入框 --- */
.composer {
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm);
  max-width: 900px;
  margin: 0 auto;
}

.composer:focus-within {
  border-color: var(--primary);
  box-shadow: 0 0 0 4px var(--primary-soft), var(--shadow-md);
}

#input {
  font-size: 14px;
  line-height: 1.65;
}

/* --- 按钮 --- */
.mini-btn {
  border-radius: var(--radius-md);
  transition: all var(--duration-fast);
}

.mini-btn:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

/* ============================================================
   4. 消息样式优化
   ============================================================ */

.msg {
  animation: msgSlideIn 0.25s var(--ease-out) both;
}

@keyframes msgSlideIn {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}

.answer-doc {
  padding: 12px 16px;
  border-radius: var(--radius-lg);
  line-height: 1.65;
}

.msg.user .answer-doc {
  background: linear-gradient(135deg, var(--primary), var(--primary-dark));
  border-radius: var(--radius-lg) var(--radius-lg) 4px var(--radius-lg);
}

.msg.bot .answer-doc {
  border-radius: var(--radius-lg) var(--radius-lg) 4px var(--radius-lg);
}

/* ============================================================
   5. 动画与过渡
   ============================================================ */

/* 欢迎页入场 */
@keyframes welcomeIn {
  from { 
    opacity: 0; 
    transform: translateY(20px) scale(0.97); 
  }
  to { 
    opacity: 1; 
    transform: translateY(0) scale(1); 
  }
}

.welcome-inner {
  animation: welcomeIn 0.4s var(--ease-out) both;
}

/* 弹窗弹出 */
@keyframes cdPop {
  from { 
    transform: scale(0.94) translateY(10px); 
    opacity: 0; 
  }
  to { 
    transform: scale(1) translateY(0); 
    opacity: 1; 
  }
}

.confirm-dialog {
  animation: cdPop 0.25s var(--ease-spring);
}

/* Toast通知 */
.toast {
  animation: toastIn 0.3s var(--ease-spring);
}

@keyframes toastIn {
  from {
    opacity: 0;
    transform: translateX(20px);
  }
  to {
    opacity: 1;
    transform: translateX(0);
  }
}

/* ============================================================
   6. 响应式断点
   ============================================================ */

/* 平板 (< 1024px) */
@media (max-width: 1024px) {
  .session-panel {
    width: 240px;
  }
  
  body.side-open #sidePanel {
    width: 360px;
  }
}

/* 小屏 (< 768px) */
@media (max-width: 768px) {
  .nav-sidebar {
    position: fixed;
    left: 0;
    top: 0;
    height: 100vh;
    z-index: 200;
    transform: translateX(-100%);
    width: 200px;
  }
  
  .nav-sidebar.open {
    transform: translateX(0);
  }
  
  .session-panel {
    position: fixed;
    left: 0;
    top: 0;
    height: 100vh;
    z-index: 150;
    transform: translateX(-100%);
  }
  
  .session-panel.open {
    transform: translateX(0);
  }
}

/* 手机 (< 480px) */
@media (max-width: 480px) {
  .nav-sidebar {
    width: 56px;
  }
  
  .composer {
    border-radius: var(--radius-lg);
  }
  
  .message-avatar {
    width: 28px;
    height: 28px;
  }
}

/* ============================================================
   7. 可访问性优化
   ============================================================ */

/* 焦点可见性 */
button:focus-visible,
input:focus-visible,
select:focus-visible,
textarea:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 2px;
}

/* 减少动画 */
@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}

/* 高对比度 */
@media (prefers-contrast: high) {
  :root {
    --border: rgba(148, 163, 184, 0.50);
    --text-muted: #64748b;
  }
}

/* ============================================================
   8. 性能优化
   ============================================================ */

/* GPU加速 */
.nav-btn,
.mini-btn,
.tool-btn,
.welcome-chip {
  will-change: transform;
}

/* 防闪烁 */
.app-layout,
.nav-sidebar,
.session-panel,
.main-content {
  backface-visibility: hidden;
}

/* 滚动性能 */
#messages,
.session-list,
.side-content {
  scroll-behavior: smooth;
  -webkit-overflow-scrolling: touch;
}

/* ============================================================
   9. 新特性预览
   ============================================================ */

/* 工具卡片悬浮效果 */
.tool-card {
  transition: all var(--duration-fast) var(--ease-out);
}

.tool-card:hover {
  transform: translateY(-1px);
  box-shadow: var(--shadow-sm);
}

/* 模式徽章 */
.mode-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 10px;
  border-radius: var(--radius-xl);
  background: var(--primary-soft);
  color: var(--primary);
  font-size: 11px;
  font-weight: 600;
}

/* 上下文进度条 */
.ctx-bar {
  height: 4px;
  border-radius: 2px;
  background: var(--bg-hover);
  overflow: hidden;
}

.ctx-fill {
  height: 100%;
  border-radius: 2px;
  background: var(--primary);
  transition: width 0.4s ease, background 0.3s ease;
}

/* 执行步骤流 */
.step-row {
  transition: background var(--duration-fast);
}

.step-row:hover {
  background: var(--bg-hover);
}

/* ============================================================
   10. 字体系统
   ============================================================ */

:root {
  --font: "Segoe UI Variable", "Segoe UI", system-ui, 
          -apple-system, "Microsoft YaHei", "PingFang SC", sans-serif;
  --mono: "JetBrains Mono", "Cascadia Code", "Fira Code", 
           Consolas, "Courier New", monospace;
}
