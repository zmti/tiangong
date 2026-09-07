/**
 * UI组件示例 - 基于v2设计系统
 * 
 * 此文件展示了如何使用新的设计令牌和组件类
 */

// ═══════════════════════════════════════════
// 1. 色彩使用示例
// ═══════════════════════════════════════════

/**
 * 使用主色调
 */
const primaryButton = {
  backgroundColor: 'var(--primary)',
  color: '#ffffff',
  boxShadow: 'var(--shadow-primary)'
};

/**
 * 使用语义色
 */
const statusColors = {
  success: {
    bg: 'var(--ok-soft)',
    text: 'var(--ok)',
    border: 'rgba(16, 185, 129, 0.2)'
  },
  error: {
    bg: 'var(--err-soft)',
    text: 'var(--err)',
    border: 'rgba(239, 68, 68, 0.2)'
  },
  warning: {
    bg: 'var(--warn-soft)',
    text: 'var(--warn)',
    border: 'rgba(245, 158, 11, 0.2)'
  },
  info: {
    bg: 'var(--info-soft)',
    text: 'var(--info)',
    border: 'rgba(59, 130, 246, 0.2)'
  }
};

/**
 * 背景层级使用
 */
const backgrounds = {
  page: 'var(--bg-base)',      // 页面底色
  card: 'var(--bg-surface)',   // 卡片背景
  elevated: 'var(--bg-elev)'   // 悬浮元素背景
};

// ═══════════════════════════════════════════
// 2. 圆角使用示例
// ═══════════════════════════════════════════

const borderRadius = {
  xs: 'var(--radius-xs)',    // 4px - 图标按钮
  sm: 'var(--radius-sm)',    // 8px - 小按钮
  md: 'var(--radius-md)',    // 12px - 卡片
  lg: 'var(--radius-lg)',    // 16px - 大面板
  xl: 'var(--radius-xl)'     // 24px - 特殊组件
};

// ═══════════════════════════════════════════
// 3. 阴影使用示例
// ═══════════════════════════════════════════

const shadows = {
  xs: 'var(--shadow-xs)',    // 微悬浮
  sm: 'var(--shadow-sm)',    // 卡片悬浮
  md: 'var(--shadow-md)',    // 弹窗
  lg: 'var(--shadow-lg)',    // 模态框
  primary: 'var(--shadow-primary)'  // 主色调阴影
};

// ═══════════════════════════════════════════
// 4. 间距使用示例
// ═══════════════════════════════════════════

const spacing = {
  xs: 'var(--space-xs)',   // 4px
  sm: 'var(--space-sm)',   // 8px
  md: 'var(--space-md)',   // 16px
  lg: 'var(--space-lg)',   // 24px
  xl: 'var(--space-xl)'    // 32px
};

// ═══════════════════════════════════════════
// 5. 组件样式示例
// ═══════════════════════════════════════════

/**
 * 按钮样式
 */
function createButton(type = 'primary', size = 'md') {
  const base = {
    padding: `${spacing.sm} ${spacing.md}`,
    borderRadius: borderRadius.md,
    fontSize: '13px',
    fontWeight: '500',
    transition: `all ${150}ms var(--ease-out)`
  };
  
  const types = {
    primary: {
      ...base,
      backgroundColor: 'var(--primary)',
      color: '#ffffff',
      border: 'none'
    },
    secondary: {
      ...base,
      backgroundColor: 'var(--bg-elev)',
      color: 'var(--text-primary)',
      border: '1px solid var(--border)'
    },
    danger: {
      ...base,
      backgroundColor: 'var(--err-soft)',
      color: 'var(--err)',
      border: '1px solid rgba(239, 68, 68, 0.2)'
    }
  };
  
  return types[type] || types.primary;
}

/**
 * 输入框样式
 */
function createInput() {
  return {
    padding: `${spacing.sm} ${spacing.md}`,
    borderRadius: borderRadius.md,
    border: '1px solid var(--border)',
    backgroundColor: 'var(--bg-elev)',
    color: 'var(--text-primary)',
    fontSize: '14px',
    outline: 'none',
    transition: 'all 150ms ease-out',
    
    ':focus': {
      borderColor: 'var(--primary)',
      backgroundColor: 'var(--bg-surface)',
      boxShadow: '0 0 0 3px var(--primary-soft)'
    },
    
    '::placeholder': {
      color: 'var(--text-faint)'
    }
  };
}

/**
 * 卡片样式
 */
function createCard() {
  return {
    backgroundColor: 'var(--bg-surface)',
    borderRadius: borderRadius.lg,
    border: '1px solid var(--border)',
    padding: spacing.lg,
    boxShadow: 'var(--shadow-sm)',
    transition: 'all 150ms ease-out',
    
    ':hover': {
      boxShadow: 'var(--shadow-md)',
      transform: 'translateY(-1px)'
    }
  };
}

/**
 * 消息气泡样式
 */
function createMessageBubble(isUser = false) {
  return {
    padding: `${spacing.md} ${spacing.lg}`,
    borderRadius: isUser 
      ? `${borderRadius.lg} ${borderRadius.lg} 4px ${borderRadius.lg}`
      : `${borderRadius.lg} ${borderRadius.lg} 4px ${borderRadius.lg}`,
    maxWidth: '85%',
    wordBreak: 'break-word',
    lineHeight: '1.6',
    fontSize: '14px',
    
    ...(isUser ? {
      backgroundColor: 'linear-gradient(135deg, var(--primary), var(--primary-dark))',
      color: '#ffffff',
      marginLeft: 'auto',
      boxShadow: '0 2px 16px var(--primary-soft)'
    } : {
      backgroundColor: 'var(--bg-surface)',
      color: 'var(--text-primary)',
      marginRight: 'auto',
      border: '1px solid var(--border)'
    })
  };
}

/**
 * 导航项样式
 */
function createNavItem(isActive = false) {
  return {
    display: 'flex',
    alignItems: 'center',
    gap: spacing.md,
    padding: `${spacing.sm} ${spacing.md}`,
    borderRadius: borderRadius.md,
    color: isActive ? 'var(--primary)' : 'var(--text-muted)',
    backgroundColor: isActive ? 'var(--primary-soft)' : 'transparent',
    cursor: 'pointer',
    transition: 'all 150ms ease-out',
    
    ':hover': {
      backgroundColor: 'var(--bg-hover)',
      color: isActive ? 'var(--primary)' : 'var(--primary)'
    },
    
    ...(isActive ? {
      position: 'relative',
      '&::before': {
        content: "''",
        position: 'absolute',
        left: '-10px',
        top: '50%',
        transform: 'translateY(-50%)',
        width: '3px',
        height: '24px',
        borderRadius: '0 2px 2px 0',
        backgroundColor: 'var(--primary)'
      }
    } : {})
  };
}

/**
 * 进度条样式
 */
function createProgressBar(value, max = 100) {
  const percentage = (value / max) * 100;
  let color = 'var(--primary)';
  
  if (percentage > 80) color = 'var(--warn)';
  if (percentage > 95) color = 'var(--err)';
  
  return {
    container: {
      height: '6px',
      backgroundColor: 'var(--bg-hover)',
      borderRadius: '3px',
      overflow: 'hidden'
    },
    fill: {
      height: '100%',
      width: `${percentage}%`,
      backgroundColor: color,
      borderRadius: '3px',
      transition: 'width 400ms ease'
    }
  };
}

// ═══════════════════════════════════════════
// 6. 动画配置
// ═══════════════════════════════════════════

const animations = {
  // 淡入
  fadeIn: {
    animation: 'fadeIn 250ms ease-out both'
  },
  
  // 滑入
  slideInUp: {
    animation: 'slideInUp 250ms ease-out both'
  },
  
  // 弹出
  popIn: {
    animation: 'popIn 250ms cubic-bezier(0.34, 1.56, 0.64, 1) both'
  },
  
  // 脉冲
  pulse: {
    animation: 'pulse 2s infinite'
  }
};

// ═══════════════════════════════════════════
// 7. 响应式配置
// ═══════════════════════════════════════════

const breakpoints = {
  sm: '480px',
  md: '768px',
  lg: '1024px',
  xl: '1280px'
};

const responsive = {
  // 移动端导航
  mobileNav: `
    @media (max-width: ${breakpoints.md}) {
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
    }
  `,
  
  // 小屏幕会话栏
  smallSessionPanel: `
    @media (max-width: ${breakpoints.md}) {
      .session-panel {
        position: fixed;
        left: 0;
        top: 0;
        height: 100vh;
        z-index: 150;
        transform: translateX(-100%);
        width: 280px;
      }
      
      .session-panel.open {
        transform: translateX(0);
      }
    }
  `
};

// ═══════════════════════════════════════════
// 8. 工具函数
// ═══════════════════════════════════════════

/**
 * 获取CSS变量值
 */
function getCSSVariable(name, theme = 'light') {
  const element = document.documentElement;
  element.setAttribute('data-theme', theme);
  return getComputedStyle(element).getPropertyValue(name).trim();
}

/**
 * 创建主题样式
 */
function createThemeStyles(theme = 'light') {
  const isDark = theme === 'dark';
  
  return {
    backgroundColor: isDark ? 'var(--bg-base)' : 'var(--bg-base)',
    color: isDark ? 'var(--text-primary)' : 'var(--text-primary)',
    borderColor: isDark ? 'var(--border)' : 'var(--border)'
  };
}

/**
 * 生成渐变背景
 */
function createGradient(startColor, endColor, angle = 135) {
  return `linear-gradient(${angle}deg, ${startColor}, ${endColor})`;
}

/**
 * 生成阴影滤镜
 */
function createShadowFilter(level = 'sm') {
  const shadows = {
    xs: '0 1px 2px rgba(15, 23, 42, 0.04)',
    sm: '0 2px 8px rgba(15, 23, 42, 0.06)',
    md: '0 4px 16px rgba(15, 23, 42, 0.08)',
    lg: '0 8px 32px rgba(15, 23, 42, 0.10)'
  };
  return shadows[level] || shadows.sm;
}

/**
 * 生成文字颜色
 */
function getTextColor(level = 'primary') {
  const colors = {
    primary: 'var(--text-primary)',
    secondary: 'var(--text-secondary)',
    muted: 'var(--text-muted)',
    faint: 'var(--text-faint)'
  };
  return colors[level] || colors.primary;
}

// 导出
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    colors: { primary, status: statusColors, backgrounds },
    radius: borderRadius,
    shadows,
    spacing,
    buttons: { createButton },
    inputs: { createInput },
    cards: { createCard },
    messages: { createMessageBubble },
    navigation: { createNavItem },
    progress: { createProgressBar },
    animations,
    breakpoints,
    responsive,
    utils: {
      getCSSVariable,
      createThemeStyles,
      createGradient,
      createShadowFilter,
      getTextColor
    }
  };
}
