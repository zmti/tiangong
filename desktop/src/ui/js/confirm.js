/* 工具确认模块:在聊天内容区展示待审批卡片 */
(function () {
  App.Confirmation = {
    _runId: null,
    _responded: {},
    _pendingTimer: null,

    /* 在消息流中内联渲染确认卡片 */
    show: function (requests, runId) {
      if (!requests || !requests.length) return;
      App.Confirmation._runId = runId;
      // 找到当前 bot 消息的 answer 之前插入
      const msgs = App.els.messages;
      const lastMsg = msgs.lastElementChild;
      if (!lastMsg) return;

      for (var i = 0; i < requests.length; i++) {
        var r = requests[i];
        var card = document.createElement('div');
        card.className = 'confirm-inline';
        card.dataset.confId = r.confirmation_id;

        var head = document.createElement('div');
        head.className = 'confirm-inline-head';
        var icon = document.createElement('span');
        icon.className = 'confirm-inline-icon';
        icon.textContent = _toolIcon(r.tool);
        var name = document.createElement('span');
        name.className = 'confirm-inline-name';
        name.textContent = r.tool || '工具';
        head.appendChild(icon);
        head.appendChild(name);

        var reason = document.createElement('div');
        reason.className = 'confirm-inline-reason';
        reason.textContent = r.reason || '';

        var args = document.createElement('div');
        args.className = 'confirm-inline-args';
        try {
          var parsed = typeof r.arguments === 'string' ? JSON.parse(r.arguments) : r.arguments;
          args.textContent = JSON.stringify(parsed, null, 2);
        } catch (_) { args.textContent = String(r.arguments || ''); }

        var actions = document.createElement('div');
        actions.className = 'confirm-inline-actions';

        var approveBtn = document.createElement('button');
        approveBtn.className = 'confirm-btn approve';
        approveBtn.textContent = '✓ 批准';
        (function(id) {
          approveBtn.addEventListener('click', function() { App.Confirmation.respond(id, true); });
        })(r.confirmation_id);

        var rejectBtn = document.createElement('button');
        rejectBtn.className = 'confirm-btn reject';
        rejectBtn.textContent = '✗ 拒绝';
        (function(id) {
          rejectBtn.addEventListener('click', function() { App.Confirmation.respond(id, false); });
        })(r.confirmation_id);

        actions.appendChild(approveBtn);
        actions.appendChild(rejectBtn);

        card.appendChild(head);
        card.appendChild(reason);
        card.appendChild(args);
        card.appendChild(actions);

        // 插入到最后一个 bot 消息的 answer 之前
        const answer = lastMsg.querySelector('.answer-doc');
        if (answer) {
          lastMsg.insertBefore(card, answer);
        } else {
          lastMsg.appendChild(card);
        }
      }
    },

    /* 轮询后端待审批列表(用于页面打开时恢复中断状态) */
    poll: function () {
      if (App.Confirmation._pendingTimer) return;
      App.apiJSON('/v1/confirmations/pending').then(function (res) {
        if (res && res.pending && res.pending.length > 0) {
          // 清除旧卡片后重新渲染
          App.Confirmation.clear();
          var requests = res.pending.map(function (p) {
            return {
              confirmation_id: p.id,
              tool: p.action,
              arguments: JSON.stringify(p.params || {}),
              reason: p.description || '',
            };
          });
          App.Confirmation.show(requests, null);
        }
      }).catch(function (_) {});
    },

    respond: function (confId, approved) {
      if (App.Confirmation._responded[confId]) return;
      App.Confirmation._responded[confId] = true;

      // 立即从 DOM 移除
      var card = document.querySelector('.confirm-inline[data-conf-id="' + confId + '"]');
      if (card) card.remove();

      App.apiJSON('/v1/confirmations/' + confId + '/respond', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approved: approved }),
      }).then(function (res) {
        if (res && res.status === 'ok' && App.Confirmation._runId) {
          App.Chat.resume(App.Confirmation._runId, { [confId]: { approved: approved } });
        }
      }).catch(function (e) {
        App.appendMessage('批准失败: ' + e.message, 'error');
      });
    },

    clear: function () {
      document.querySelectorAll('.confirm-inline').forEach(function(el) { el.remove(); });
      if (App.Confirmation._pendingTimer) {
        clearInterval(App.Confirmation._pendingTimer);
        App.Confirmation._pendingTimer = null;
      }
      App.Confirmation._runId = null;
      App.Confirmation._responded = {};
    }
  };

  function _toolIcon(name) {
    var icons = {
      run_terminal: '⚡', run_code: '💻', write_file: '✏️', read_file: '📖',
      edit_file: '🔧', git: '📦', send_email: '📧', delete_email: '🗑️',
      generate_image: '🖼️', generate_video: '🎬', delegate_task: '🔀',
    };
    var n = (name || '').toLowerCase();
    for (var k in icons) { if (n.indexOf(k) !== -1) return icons[k]; }
    return '🔨';
  }
})();
