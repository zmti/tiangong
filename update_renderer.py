#!/usr/bin/env python3
# Update renderer.js to add model selection feature

import re

# Read renderer.js
with open(r'E:\dev\agent-service\desktop\src\ui\renderer.js', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Add modelSelect to els
old_els = "providerSelect: $('providerSelect'),\n    modeSelect: $('modeSelect'),"
new_els = "providerSelect: $('providerSelect'),\n    modelSelect: $('modelSelect'),\n    modeSelect: $('modeSelect'),"
content = content.replace(old_els, new_els)

# 2. Add loadModels function after loadProviders
load_models_func = '''
  /* ---------- 加载模型列表 ---------- */
  async function loadModels(providerId) {
    if (!providerId) {
      App.els.modelSelect.innerHTML = '<option value="">请先选择提供商</option>';
      return;
    }
    try {
      const res = await App.api('/v1/models?provider=' + encodeURIComponent(providerId));
      if (!res.ok) {
        App.els.modelSelect.innerHTML = '<option value="">加载失败</option>';
        return;
      }
      const data = await res.json();
      App.els.modelSelect.innerHTML = '';
      if (!data.models || data.models.length === 0) {
        const opt = document.createElement('option');
        opt.value = data.default_model || '';
        opt.textContent = data.default_model ? data.default_model + ' (默认)' : '无可用模型';
        App.els.modelSelect.appendChild(opt);
      } else {
        data.models.forEach((m) => {
          const opt = document.createElement('option');
          opt.value = m.id;
          opt.textContent = m.name + (m.speed ? ' [' + m.speed + ']' : '');
          opt.title = (m.quality || '') + (m.speed ? ' | Speed: ' + m.speed : '');
          if (m.id === data.default_model) opt.selected = true;
          App.els.modelSelect.appendChild(opt);
        });
      }
      App._modelsData = data;
    } catch (_) {
      App.els.modelSelect.innerHTML = '<option value="">加载失败</option>';
    }
  }
'''

# Insert loadModels after loadProviders
old_load_providers = '''  /* ---------- 模型 Provider ---------- */
  async function loadProviders() {
    try {
      const res = await App.api('/v1/providers');
      if (!res.ok) return;
      const data = await res.json();
      App.els.providerSelect.innerHTML = '';
      (data.providers || []).forEach((p) => {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = p.name + (p.configured ? '' : ' (未配置)');
        opt.disabled = !p.configured;
        App.els.providerSelect.appendChild(opt);
      });
      if (data.default) App.els.providerSelect.value = data.default;
    } catch (_) { /* 忽略 */ }
  }'''

new_load_providers = '''  /* ---------- 模型 Provider ---------- */
  async function loadProviders() {
    try {
      const res = await App.api('/v1/providers');
      if (!res.ok) return;
      const data = await res.json();
      App.els.providerSelect.innerHTML = '';
      (data.providers || []).forEach((p) => {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = p.name + (p.configured ? '' : ' (未配置)');
        opt.disabled = !p.configured;
        App.els.providerSelect.appendChild(opt);
      });
      if (data.default) App.els.providerSelect.value = data.default;
      // Load models for default provider
      if (data.default) {
        await loadModels(data.default);
      }
      // Add change listener for provider selection
      App.els.providerSelect.onchange = async (e) => {
        await loadModels(e.target.value);
      };
    } catch (_) { /* 忽略 */ }
  }'''

content = content.replace(old_load_providers, new_load_providers + load_models_func)

# Write back
with open(r'E:\dev\agent-service\desktop\src\ui\renderer.js', 'w', encoding='utf-8') as f:
    f.write(content)

print('Updated renderer.js')
print('Has modelSelect:', 'modelSelect' in content)
print('Has loadModels:', 'loadModels' in content)
print('Has provider change:', 'providerSelect.onchange' in content)
