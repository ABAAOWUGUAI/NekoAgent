    function renderMetrics(result) {
      if (!$('metrics')) return;
      const counts = result.counts || {};
      const items = [
        ['total', result.total ?? 0, 'primary'],
        ['active', result.active ?? 0, 'primary'],
        ['queued', counts.queued ?? 0, ''],
        ['running', counts.running ?? 0, ''],
        ['done', counts.done ?? 0, ''],
        ['failed', counts.failed ?? 0, (counts.failed || counts.timeout) ? 'warn' : ''],
      ];
      $('metrics').innerHTML = items.map(([label, value, tone]) => (
        `<div class="metric ${tone}"><span>${escapeHtml(metricLabels[label] || label)}</span><strong>${escapeHtml(value)}</strong></div>`
      )).join('');
      const failed = Number(counts.failed || 0) + Number(counts.timeout || 0);
      if ($('overviewTaskBrief')) {
        setText(
          'overviewTaskBrief',
          `${result.active ?? 0} 个活跃任务，${failed} 个失败或超时`,
        );
      }
    }

    function auditTone(level) {
      if (level === 'critical') {
        return 'red';
      }
      if (level === 'attention' || level === 'warning') {
        return 'amber';
      }
      return 'blue';
    }

    function renderSystemAudit(result) {
      const findings = result?.findings || [];
      const critical = findings.filter((item) => item.severity === 'critical').length;
      const warning = findings.filter((item) => item.severity === 'warning').length;
      const score = result?.score ?? '-';
      const level = result?.level || 'unknown';
      const items = [
        ['分数', score, auditTone(level)],
        ['等级', level, auditTone(level)],
        ['严重', critical, critical ? 'red' : 'blue'],
        ['警告', warning, warning ? 'amber' : 'blue'],
        ['耗时', `${result?.duration ?? '-'}s`, ''],
      ];
      $('systemAuditSummary').innerHTML = items.map(([label, value, tone]) => (
        `<div class="summary-item"><span>${escapeHtml(label)}</span><strong class="${tone}">${escapeHtml(value)}</strong></div>`
      )).join('');
      $('systemAuditStatus').className = `provider-status ${level === 'healthy' ? 'ok' : level === 'critical' ? 'error' : 'pending'}`;
      $('systemAuditStatus').textContent = findings.length
        ? `发现 ${findings.length} 个问题，优先处理严重项。`
        : '未发现明显阻塞项。';
      if (!findings.length) {
        $('systemAuditRows').innerHTML = '<tr><td colspan="4" class="empty">未发现明显问题。</td></tr>';
        return;
      }
      $('systemAuditRows').innerHTML = findings.map((item) => {
        const tone = item.severity === 'critical' ? 'red' : item.severity === 'warning' ? 'amber' : '';
        return `<tr>
          <td><span class="badge ${tone}">${escapeHtml(item.severity || '')}</span></td>
          <td>${escapeHtml(item.area || '')}</td>
          <td><strong>${escapeHtml(item.title || '')}</strong><br><span class="compact-note">${escapeHtml(item.detail || '')}</span></td>
          <td>${escapeHtml(item.action || '')}</td>
        </tr>`;
      }).join('');
    }

    function renderSystemFramework(result) {
      const loops = result?.loops || [];
      const dimensions = result?.dimensions || [];
      const runtime = result?.runtime || {};
      setText('systemFrameworkMeta', `${runtime.chat_provider_preset || '-'} / work=${runtime.work_provider || '-'} / push=${runtime.result_push || '-'}`);
      $('systemFrameworkLoops').innerHTML = loops.map((item) => (
        `<div class="summary-item"><span>${escapeHtml(item.label || item.key)}</span><strong class="blue">${escapeHtml(item.provider || '')}</strong></div>`
      )).join('') || '<div class="empty">暂无系统框架数据。</div>';
      $('systemFrameworkDimensions').innerHTML = dimensions.map((item) => (
        `<div class="settings-row">
          <strong>${escapeHtml(item.dimension || '')} · ${escapeHtml(item.priority || '')}</strong>
          <p>${escapeHtml(item.problem || '')}</p>
          <p>${escapeHtml(item.fix || '')}</p>
        </div>`
      )).join('') || '<div class="empty">暂无优化项。</div>';
    }

    async function loadSystemFramework() {
      if (!state.authenticated || !$('systemFrameworkLoops')) {
        return;
      }
      const result = await bridge('/system/framework');
      renderSystemFramework(result);
    }

    async function loadSystemAudit() {
      if (!state.authenticated) {
        return;
      }
      $('systemAuditStatus').className = 'provider-status pending';
      $('systemAuditStatus').textContent = '正在执行系统体检。';
      const result = await bridge('/system/audit');
      renderSystemAudit(result);
    }

    function badgeForStatus(status) {
      if (['failed', 'timeout', 'cancelled'].includes(status)) {
        return 'red';
      }
      if (['running', 'queued', 'done'].includes(status)) {
        return 'blue';
      }
      return '';
    }

    function badgeForDelivery(status) {
      if (status === 'sent') {
        return 'blue';
      }
      if (status === 'failed') {
        return 'red';
      }
      if (['pending', 'skipped'].includes(status)) {
        return 'amber';
      }
      return '';
    }

    function formatCodegraphBrief(codegraph) {
      if (!codegraph) {
        return '';
      }
      const before = codegraph.before || codegraph;
      const after = codegraph.after;
      const parts = [];
      if (before.status) {
        parts.push(`before=${before.status}`);
      }
      if (before.action) {
        parts.push(`action=${before.action}`);
      }
      if (after && after.status) {
        parts.push(`after=${after.status}`);
      }
      if (after && after.action) {
        parts.push(`after_action=${after.action}`);
      }
      if (before.root || after?.root) {
        parts.push(`root=${before.root || after.root}`);
      }
      return parts.join(', ');
    }

    function taskOutputText(task) {
      if ((task.ok || task.status === 'done') && task.stdout) {
        return task.stdout;
      }
      return task.output || task.stdout || task.stderr || task.error || '(空)';
    }

    function formatTask(task) {
      const lines = [
        `任务 #${task.id || '?'}`,
        `状态: ${statusLabels[task.status] || task.status || '?'}`,
        `来源: ${task.source || 'admin'}`,
        `QQ用户: ${task.user_id || ''}`,
        `意图/模式: ${(task.intent || '') + (task.mode ? ' / ' + task.mode : '')}`,
        `沙箱: ${task.sandbox || '?'}`,
        `推送: ${task.delivery_status || ''}${task.delivered_at ? ' @ ' + task.delivered_at : ''}`,
        `推送错误: ${task.delivery_error || ''}`,
        `补充消息: ${task.pending_message_count ?? 0}`,
        `原始消息: ${task.origin_message || ''}`,
        `工作目录: ${task.cwd || '?'}`,
        `创建时间: ${task.created_at || '?'}`,
        `开始时间: ${task.started_at || '?'}`,
        `结束时间: ${task.finished_at || '?'}`,
        `耗时: ${task.duration ?? '?'} 秒`,
        `返回码: ${task.returncode ?? '?'}`,
        `错误类型: ${task.error_kind || ''}`,
        `来源任务: ${task.source_task_id || ''}`,
        '',
        taskOutputText(task),
      ];
      const codegraphText = formatCodegraphBrief(task.codegraph);
      if (codegraphText) {
        lines.splice(lines.length - 2, 0, `CodeGraph: ${codegraphText}`);
      }
      return lines.join('\n');
    }

    function renderHealthSummary(services, containers) {
      const downServices = services.filter((item) => !item.ok).length;
      const runningContainers = containers.filter((item) => String(item.state || '').toLowerCase() === 'running').length;
      const items = [
        ['服务异常', downServices, downServices ? 'red' : 'blue'],
        ['运行容器', runningContainers, 'blue'],
        ['容器总数', containers.length, ''],
      ];
      $('healthSummary').innerHTML = items.map(([label, value, tone]) => (
        `<div class="summary-item"><span>${escapeHtml(label)}</span><strong class="${tone}">${escapeHtml(value)}</strong></div>`
      )).join('');
      // The services view no longer renders the legacy serviceMeta node. Keep
      // this projection optional so a removed presentation-only target cannot
      // make the whole business-health refresh look failed.
      setText('serviceMeta', downServices ? `${downServices} 个服务需要检查` : '服务状态正常');
      if ($('overviewServiceStatus')) {
        setText('overviewServiceStatus', downServices ? `${downServices} 个服务异常` : 'systemd 服务正常');
        setBadge('overviewServiceBadge', downServices ? '检查' : '正常', downServices ? 'red' : 'blue');
        setText('overviewContainerStatus', `${runningContainers} / ${containers.length} 个容器运行中`);
        setBadge(
          'overviewContainerBadge',
          runningContainers === containers.length ? '正常' : '部分',
          runningContainers === containers.length ? 'blue' : 'amber',
        );
      }
    }

    function renderCodegraphStatus(result) {
      const pending = result.pending || {};
      const pendingTotal = Number(pending.added || 0) + Number(pending.modified || 0) + Number(pending.removed || 0);
      const ready = Boolean(result.ok && result.status === 'ready');
      const items = [
        ['状态', result.status || 'unknown', ready ? 'blue' : 'red'],
        ['文件', result.files ?? '-', ''],
        ['节点', result.nodes ?? '-', ''],
        ['待同步', pendingTotal, pendingTotal ? 'red' : 'blue'],
      ];
      $('codegraphSummary').innerHTML = items.map(([label, value, tone]) => (
        `<div class="summary-item"><span>${escapeHtml(label)}</span><strong class="${tone}">${escapeHtml(value)}</strong></div>`
      )).join('');
      $('codegraphMeta').textContent = result.root ? '已接入 MCP' : '等待项目';
      $('codegraphRoot').textContent = [
        `root: ${result.root || result.cwd || '?'}`,
        `backend: ${result.backend || '?'}`,
        `pending: added=${pending.added || 0}, modified=${pending.modified || 0}, removed=${pending.removed || 0}`,
      ].join('\n');
    }

    function renderOverviewCodex(codex) {
      if (!$('overviewCodexStatus')) return;
      const output = String(codex?.output || codex?.error || '').trim();
      const loggedIn = /logged in|chatgpt|authenticated/i.test(output);
      const failed = Boolean(codex?.error || codex?.ok === false);
      setText('overviewCodexStatus', loggedIn ? '已登录' : failed ? '状态异常' : '需确认');
      setText('overviewCodexDetail', output ? output.split('\n').slice(0, 2).join(' ') : 'Codex 状态未返回。');
    }

    function renderOverviewProject(project) {
      const current = project || {};
      setText('overviewProjectName', current.name || current.id || '未设置');
      setText('overviewProjectPath', current.path || '项目路径未设置。');
    }

    function renderOverviewProvider(settings) {
      const current = settings || {};
      const preset = state.providerPresets.find((item) => item.key === current.chat_provider_preset);
      const provider = preset?.label || (current.chat_provider === 'openai-compatible' ? 'OpenAI-compatible' : 'Codex CLI');
      const model = current.chat_model || (provider === 'Codex CLI' ? 'ChatGPT 登录态' : '未设置模型');
      const keyText = current.chat_provider === 'openai-compatible'
        ? (current.chat_api_key_set ? 'Key 已配置' : '缺少 Key')
        : '使用 Codex 登录态';
      setText('overviewProviderStatus', provider);
      setText('overviewProviderDetail', `${model}；${keyText}`);
    }

    function renderOverviewQq(result) {
      if (!result || result.ok === false) {
        setText('overviewQqStatus', '诊断失败');
        setText('overviewQqDetail', result?.error || 'QQ 链路诊断未返回。');
        return;
      }
      if (result.needs_login) {
        setText('overviewQqStatus', '需要扫码');
        setText('overviewQqDetail', result.qrcode_available ? 'QQ 登录态失效，QQ 链路页可扫码。' : 'QQ 登录态失效，等待二维码生成。');
        return;
      }
      if (result.bridge_reachable_from_astrbot === false) {
        setText('overviewQqStatus', 'Bridge 不可达');
        setText('overviewQqDetail', result.recommendation || '检查 AstrBot 到 bridge 的网络。');
        return;
      }
      setText('overviewQqStatus', '在线');
      setText('overviewQqDetail', result.recommendation || '白名单私聊链路可用。');
    }

    function assistantHomeTone(value) {
      if (['critical', 'failed', 'timed_out', 'dead_letter', 'unavailable'].includes(value)) return 'red';
      if (['high', 'waiting_user', 'waiting_approval', 'degraded'].includes(value)) return 'amber';
      if (['running', 'queued', 'active'].includes(value)) return 'blue';
      return 'green';
    }

    function renderAssistantHome(result = {}) {
      const assistant = result.assistant || {};
      const displayName = assistant.display_name || '当前助手';
      setText('homeAssistantName', displayName);
      setText('homeComposerAssistantName', displayName);
      setText(
        'homeAssistantIntro',
        assistant.status === 'active'
          ? '可以先聊两句，也可以直接交代一件要完成的事。'
          : '助手身份尚未完全就绪；仍可查看任务与待处理事项。',
      );

      const health = result.business_health || {};
      const healthStatus = health.status || 'degraded';
      const healthSummary = healthStatus === 'healthy'
        ? '日常链路正常'
        : healthStatus === 'unavailable'
          ? '日常链路不可用'
          : '有配置需要处理';
      setText('homeHealthSummary', healthSummary);
      $('homeHealthSummary').closest('.home-health-button')?.setAttribute(
        'data-health',
        healthStatus === 'healthy' ? 'healthy' : 'attention',
      );
      $('homeHealthDot').className = `home-health-dot ${assistantHomeTone(healthStatus)}`;

      const attention = result.attention || {};
      const attentionItems = Array.isArray(attention.items) ? attention.items : [];
      const attentionTotal = Number(attention.total ?? attentionItems.length);
      setText('homeAttentionCount', attentionTotal ? `${attentionTotal} 项` : '已清空');
      $('homeAttentionItems').innerHTML = attentionItems.length
        ? attentionItems.map((item) => `<article class="home-attention-item" data-priority="${escapeHtml(item.priority || 'medium')}">
            <div>
              <span class="status-chip ${assistantHomeTone(item.priority)}">${escapeHtml(item.status_label || '需要处理')}</span>
              <h3>${escapeHtml(item.title || '未命名事项')}</h3>
              <p>${escapeHtml(item.reason || '')}</p>
              <small>${escapeHtml(item.risk || '')}</small>
            </div>
            <button class="secondary" type="button" data-attention-jump="${escapeHtml((item.next_action || item.action)?.view || 'tasks')}">${escapeHtml((item.next_action || item.action)?.label || '查看')}</button>
          </article>`).join('')
        : '<div class="home-empty-state"><strong>现在没有事情等你处理</strong><p>审批、失败、投递异常和必要配置会统一出现在这里。</p></div>';

      const activeTasks = Array.isArray(result.active_tasks) ? result.active_tasks : [];
      $('homeActiveTasks').innerHTML = activeTasks.length
        ? activeTasks.map((item) => `<article class="home-task-card">
            <div>
              <span class="status-chip ${assistantHomeTone(item.status)}">${escapeHtml(item.status_label || item.status || '处理中')}</span>
              <h3>${escapeHtml(item.title || '未命名任务')}</h3>
              <small>${escapeHtml(compactTimestamp(item.updated_at))}</small>
            </div>
            ${item.legacy_task_id ? `<button class="link" type="button" data-home-task="${escapeHtml(item.legacy_task_id)}">查看任务</button>` : ''}
          </article>`).join('')
        : '<div class="home-empty-state"><strong>当前没有正在进行的任务</strong><p>你可以在上方直接交代一件事。</p></div>';

      const conversations = Array.isArray(result.recent_conversations) ? result.recent_conversations : [];
      $('homeRecentConversations').innerHTML = conversations.length
        ? conversations.map((item) => `<article class="home-conversation-card">
            <div>
              <span class="entity-type">${escapeHtml(item.channel_label || item.channel_type || '对话')}</span>
              <h3>${escapeHtml(item.status === 'active' ? '可以继续的对话' : '最近对话')}</h3>
              <small>${escapeHtml(compactTimestamp(item.updated_at))}</small>
            </div>
            <button class="link" type="button" data-conversation-jump="brain">查看记忆边界</button>
          </article>`).join('')
        : '<div class="home-empty-state"><strong>还没有最近对话</strong><p>开始一次对话后，这里只显示线程元数据，不暴露消息正文。</p></div>';

      const artifactProjection = result.recent_artifacts || {};
      const artifacts = Array.isArray(artifactProjection.items) ? artifactProjection.items : [];
      if (artifacts.length) {
        $('homeRecentArtifacts').className = 'home-card-list';
        $('homeRecentArtifacts').innerHTML = artifacts.map((item) => `<article class="home-task-card">
          <div><span class="entity-type">${escapeHtml(item.kind || '成品')}</span><h3>${escapeHtml(item.title || '未命名成品')}</h3><small>${escapeHtml(compactTimestamp(item.updated_at))}</small></div>
          <button class="link" type="button" data-jump="artifacts">打开成品中心</button>
        </article>`).join('');
      } else if (artifactProjection.capability_status === 'available') {
        $('homeRecentArtifacts').className = 'home-artifact-empty';
        $('homeRecentArtifacts').innerHTML = '<strong>还没有成品</strong><p>任务生成文件、报告、演示文稿或静态网站后，会在这里显示最近版本。</p>';
      } else {
        $('homeRecentArtifacts').className = 'home-artifact-empty';
        $('homeRecentArtifacts').innerHTML = '<strong>成品数据暂时不可用</strong><p>请进入成品中心查看详细状态。</p>';
      }
    }

    async function loadAssistantHome() {
      if (!state.authenticated) return;
      try {
        const result = await bridge('/assistant/home?limit=12');
        renderAssistantHome(result);
      } catch (error) {
        setText('homeHealthSummary', '日常数据读取失败');
        $('homeHealthSummary').closest('.home-health-button')?.setAttribute('data-health', 'attention');
        $('homeAttentionItems').innerHTML = '<div class="home-empty-state"><strong>暂时无法整理待处理事项</strong><p>请刷新当前页；如果仍失败，请进入运行环境查看业务健康。</p></div>';
        $('homeActiveTasks').innerHTML = '<div class="home-empty-state"><strong>任务状态暂时不可用</strong><p>页面没有使用缓存或虚假任务替代真实数据。</p></div>';
        $('homeRecentConversations').innerHTML = '<div class="home-empty-state"><strong>最近对话暂时不可用</strong><p>消息正文没有被读取或展示。</p></div>';
        throw error;
      }
    }

    function bindAssistantHome() {
      const home = $('view-overview');
      home?.addEventListener('click', (event) => {
        const taskButton = event.target.closest('[data-home-task]');
        if (taskButton) {
          switchView('tasks', { focusHeading: true });
          loadTask(taskButton.dataset.homeTask).catch((error) => setConnection(error.message || String(error), 'error'));
          return;
        }
        const jumpButton = event.target.closest('[data-attention-jump], [data-conversation-jump]');
        if (jumpButton) {
          switchView(jumpButton.dataset.attentionJump || jumpButton.dataset.conversationJump || 'tasks', { focusHeading: true });
        }
      });
    }

