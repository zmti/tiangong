# UI迁移指南 v1 → v2

## 📦 文件变更清单

### 新增文件
- `style-redesign.css` - 主样式文件 (新版本)
- `preview.html` - 设计系统预览页
- `DESIGN.md` - 设计令牌说明
- `DESIGN_GUIDE.md` - 完整设计文档

### 保留文件
- `style.css` - 原样式 (保持不变)
- `style-message.css` - 消息样式 (保持不变)
- `style-command.css` - 命令面板样式 (保持不变)
- `style-workflow.css` - 工作流样式 (保持不变)

---

## 🔄 迁移步骤

### 步骤1: 更新HTML引用

在 `index.html` 中，将样式引用顺序调整为：

```html
<!-- 旧版样式 (保留作为fallback) -->
<link rel="stylesheet" href="style.css">
<link rel="stylesheet" href="style-command.css">
<link rel="stylesheet" href="style-message.css">
<link rel="stylesheet" href="style-workflow.css">

<!-- 新版样式 (优先加载) -->
<link rel="stylesheet" href="style-redesign.css">
```

### 步骤2: 更新CSS类名

#### 导航栏
```css
/* 旧 */
#iconBar { width: 58px; }
.nav-btn { width: 40px; height: 38px; }

/* 新 */
.nav-sidebar { width: 64px; }
.nav-btn { width: 44px; height: 44px; }
```

#### 会话面板
```css
/* 旧 */
#sessionPanel { width: 248px; }

/* 新 */
.session-panel { width: 280px; }
```

#### 消息气泡
```css
/* 旧 */
.msg.user .answer-doc { border-radius: var(--radius) var(--radius) 3px var(--radius); }
.msg.bot .answer-doc { border-radius: var(--radius) var(--radius) var(--radius) 3px; }

/* 新 */
.msg.user .answer-doc { border-radius: var(--radius-lg) var(--radius-lg) 4px var(--radius-lg); }
.msg.bot .answer-doc { border-radius: var(--radius-lg) var(--radius-lg) 4px var(--radius-lg); }
```

#### 输入框
```css
/* 旧 */
.composer { border-radius: var(--radius); }

/* 新 */
.composer { border-radius: var(--radius-xl); }
```

### 步骤3: 更新JavaScript

检查以下JavaScript文件中的DOM操作：

1. `js/session.js` - 会话列表渲染
2. `js/messages.js` - 消息渲染
3. `js/chat.js` - 聊天逻辑
4. `js/settings.js` - 设置面板

确保选择的CSS类名与新设计一致。

### 步骤4: 测试主题切换

```javascript
// 测试代码
document.body.setAttribute('data-theme', 'dark');
// 检查所有组件的颜色变化
```

---

## 🎯 关键改进点

### 1. 色彩系统
- **主色调**: 从 #7c5cfc 改为 #6366f1 (更中性的靛蓝)
- **强调色**: 从 #38bdf8 改为 #06b6d4 (更清晰的原色青)
- **语义色**: 统一使用 Tailwind CSS 标准色

### 2. 圆角体系
- 新增 `--radius-xs: 4px` 用于小元素
- 统一使用 `--radius-*` 变量
- 消息气泡采用不对称圆角

### 3. 阴影层次
- 从2层增加到4层
- 每层有明确的用途
- 深色主题阴影更强

### 4. 间距系统
- 统一使用 4px 网格
- 新增 `--space-*` 变量
- 更好的视觉节奏

### 5. 动效优化
- 统一缓动函数
- 缩短动画时长
- 添加 spring 效果

---

## 🧪 测试清单

### 功能测试
- [ ] 导航栏点击切换正常
- [ ] 会话列表展开/收起正常
- [ ] 消息发送/接收正常
- [ ] 输入框自动高度调整正常
- [ ] 右侧面板展开/收起正常
- [ ] 主题切换正常
- [ ] 命令面板唤起正常

### 视觉测试
- [ ] 深色主题显示正常
- [ ] 浅色主题显示正常
- [ ] 颜色对比度达标
- [ ] 图标大小一致
- [ ] 间距统一

### 响应式测试
- [ ] 桌面端 (> 1024px) 正常
- [ ] 平板端 (768-1024px) 正常
- [ ] 手机端 (< 768px) 正常
- [ ] 小屏手机 (< 480px) 正常

### 性能测试
- [ ] 页面加载速度正常
- [ ] 滚动流畅无卡顿
- [ ] 动画60fps
- [ ] 内存占用正常

### 可访问性测试
- [ ] 键盘导航正常
- [ ] 屏幕阅读器可读
- [ ] 焦点可见性良好
- [ ] 高对比度模式正常

---

## 🔧 常见问题

### Q: 样式不生效？
A: 检查CSS加载顺序，确保 `style-redesign.css` 在 `style.css` 之后加载。

### Q: 深色主题不工作？
A: 检查 `body` 上的 `data-theme="dark"` 属性是否正确设置。

### Q: 动画不流畅？
A: 检查是否启用了 GPU 加速，关闭不必要的 `will-change`。

### Q: 响应式布局错乱？
A: 检查媒体查询断点是否与预期一致。

---

## 📚 参考资料

- [Design Tokens](./DESIGN.md) - 设计令牌详细说明
- [Design Guide](./DESIGN_GUIDE.md) - 完整设计指南
- [Preview](./preview.html) - 在线预览页面

---

**版本**: 1.0.0
**创建日期**: 2025-01-15
**维护者**: AI Assistant
