    const dNote = '历史送达记录，非待发送；确认后重试。';

    function taskRow(task, includeActions) {
      const status = task.status || '';
      const canCancel = status === 'queued' || status === 'running';
      const canRetry = status === 'failed' || status === 'timeout' || status === 'cancelled';
      const id = escapeHtml(task.id || '');
      const source = task.source || (task.user_id ? 'qq' : 'admin');
      const delivery = task.delivery_status || (source === 'qq' ? 'pending' : '');
      const cells = [
        `<td><button class="link" data-action="detail" data-id="${id}" type="button">#${id}</button></td>`,
        `<td><span class="badge ${badgeForStatus(status)}">${escapeHtml(statusLabels[status] || status || '?')}</span></td>`,
        `<td><span class="badge ${source === 'qq' ? 'blue' : ''}">${escapeHtml(source)}</span></td>`,
        `<td>${escapeHtml(task.sandbox || '?')}</td>`,
        `<td><span class="badge ${badgeForDelivery(delivery)}">${escapeHtml(delivery || '-')}</span></td>`,
        `<td>${escapeHtml(task.duration ?? '')}</td>`,
        `<td>${escapeHtml(task.summary || '')}</td>`,
      ];
      if (includeActions) {
        cells.push(`<td><div class="actions">
          <button class="secondary" data-action="detail" data-id="${id}" type="button">查看</button>
          <button class="secondary" data-action="retry" data-id="${id}" type="button" ${canRetry ? '' : 'disabled'}>重试</button>
          <button class="danger" data-action="cancel" data-id="${id}" type="button" ${canCancel ? '' : 'disabled'}>取消</button>
        </div></td>`);
      }
      return `<tr>${cells.join('')}</tr>`;
    }

    function renderTasks(tasks) {
      state.lastTasks = tasks;
      if (!tasks.length) {
        $('taskRows').innerHTML = '<tr><td colspan="8" class="empty">暂无任务。</td></tr>';
        return;
      }
      $('taskRows').innerHTML = tasks.map((task) => taskRow(task, true)).join('');
    }

    const approvalDecisionInFlight = new Set();

    function approvalIdempotencyKey(approvalId, decision) {
      const suffix = window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
      return `web-${decision}-${approvalId}-${suffix}`;
    }

    function renderFormalApprovals(items = []) {
      const pending = items.filter((item) => item.status === 'pending');
      $('formalApprovalList').innerHTML = pending.length ? pending.map((item) => {
        const id = escapeHtml(item.id || '');
        const version = Number(item.version || 0);
        const descriptionId = `approval-description-${id.replace(/[^a-zA-Z0-9_-]/g, '')}`;
        return `<article class="approval-card" data-approval-card data-approval-id="${id}" data-approval-version="${version}">
          <div class="approval-copy">
            <div><span class="status-chip amber">等待确认</span></div>
            <h3>${escapeHtml(item.action_summary || '风险操作等待确认')}</h3>
            <p id="${descriptionId}">批准后，助手只会执行当前卡片绑定的动作；动作参数变化会要求重新确认。</p>
            <div class="approval-meta">
              <div><span>目标环境</span><strong>${escapeHtml(item.target_environment || '服务器')}</strong></div>
              <div><span>当前超时</span><strong>${escapeHtml(item.timeout_seconds || 0)} 秒</strong></div>
              <div><span>确认码</span><strong class="mono">${escapeHtml(item.code || '—')}</strong></div>
            </div>
            <small>有效期至 ${escapeHtml(compactTimestamp(item.expires_at))} · 来自 ${escapeHtml(item.requested_channel || '未知渠道')}</small>
          </div>
          <div class="approval-controls">
            <label>修改超时后批准
              <input data-approval-timeout type="number" min="30" max="900" value="${escapeHtml(item.timeout_seconds || 180)}">
            </label>
            <label>拒绝原因（可选）
              <input data-approval-reason type="text" maxlength="300" placeholder="例如：现在不允许修改生产环境">
            </label>
            <div class="approval-actions" aria-describedby="${descriptionId}">
              <button class="primary" type="button" data-approval-action="approve">批准</button>
              <button class="secondary" type="button" data-approval-action="edit">修改后批准</button>
              <button class="danger" type="button" data-approval-action="reject">拒绝</button>
            </div>
          </div>
        </article>`;
      }).join('') : '<div class="empty-state">目前没有等待确认的操作。需要审批时，助手会先暂停任务。</div>';
    }

    async function loadFormalApprovals() {
      const result = await bridge('/assistant/approvals?status=pending&limit=50');
      renderFormalApprovals(result.items || []);
    }

    async function decideFormalApproval(button) {
      const card = button.closest('[data-approval-card]');
      if (!card) return;
      const approvalId = card.dataset.approvalId || '';
      const decision = button.dataset.approvalAction || '';
      const expectedVersion = Number(card.dataset.approvalVersion || 0);
      if (!approvalId || !expectedVersion || approvalDecisionInFlight.has(approvalId)) return;
      const timeout = Number(card.querySelector('[data-approval-timeout]')?.value || 0);
      const reason = String(card.querySelector('[data-approval-reason]')?.value || '').trim();
      const payload = { decision, expected_version: expectedVersion, reason };
      if (decision === 'edit') payload.edit_patch = { timeout_seconds: timeout };
      approvalDecisionInFlight.add(approvalId);
      card.querySelectorAll('button, input').forEach((control) => { control.disabled = true; });
      $('formalApprovalStatus').textContent = decision === 'reject' ? '正在拒绝操作。' : '正在提交批准决定。';
      try {
        await bridge(`/assistant/approvals/${encodeURIComponent(approvalId)}/decision`, {
          method: 'POST',
          headers: { 'Idempotency-Key': approvalIdempotencyKey(approvalId, decision) },
          body: JSON.stringify(payload),
        });
        $('formalApprovalStatus').textContent = decision === 'reject' ? '操作已拒绝，任务不会执行。' : '操作已批准，任务已进入队列。';
        setConnection($('formalApprovalStatus').textContent, 'ok');
        await loadFormalApprovals();
        await loadTasks();
        await loadExecutionOverview();
      } catch (error) {
        $('formalApprovalStatus').textContent = error.message || String(error);
        setConnection($('formalApprovalStatus').textContent, 'error');
        await loadFormalApprovals().catch(() => {});
      } finally {
        approvalDecisionInFlight.delete(approvalId);
        card.querySelectorAll('button, input').forEach((control) => { control.disabled = false; });
      }
    }

    const taskTimelineKindLabels = {
      received: '接收',
      status: '执行',
      approval: '确认',
      revision: '版本',
      feedback: '反馈',
      checkpoint: '步骤',
      artifact: '成品',
    };
    const taskTimelineKinds = new Set(Object.keys(taskTimelineKindLabels));

    function renderTaskTimeline(payload = {}) {
      const goal = payload.goal || {};
      const events = Array.isArray(payload.events) ? payload.events : [];
      $('taskTimelineTitle').textContent = `任务时间线 · ${goal.title || '未命名任务'}`;
      $('taskTimelineList').innerHTML = events.length ? events.map((event) => {
        const eventKind = taskTimelineKinds.has(event.kind) ? event.kind : 'status';
        const statusChange = event.from_status || event.to_status
          ? `${runStatusLabels[event.from_status] || event.from_status || '开始'} → ${runStatusLabels[event.to_status] || event.to_status || '已更新'}`
          : '';
        return `<li class="task-timeline-item ${eventKind}">
          <span class="task-timeline-kind">${escapeHtml(taskTimelineKindLabels[eventKind])}</span>
          <strong>${escapeHtml(event.label || '任务记录已更新')}</strong>
          ${statusChange ? `<small>${escapeHtml(statusChange)}</small>` : ''}
          <time datetime="${escapeHtml(event.created_at || '')}">${escapeHtml(compactTimestamp(event.created_at))}</time>
        </li>`;
      }).join('') : '<li class="empty-state">这项任务还没有可展示的进度记录。</li>';
      $('taskTimelinePanel').classList.remove('hidden');
      $('taskTimelinePanel').scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
      $('closeTaskTimelineBtn').focus({ preventScroll: true });
    }

    async function loadTaskTimeline(goalId) {
      const result = await bridge(`/assistant/tasks/${encodeURIComponent(goalId)}/timeline?limit=200`);
      renderTaskTimeline(result);
    }

    const runStatusLabels = {
      queued: '等待执行',
      running: '执行中',
      waiting_approval: '等待批准',
      succeeded: '运行成功',
      failed: '运行失败',
      timed_out: '运行超时',
      cancelled: '已取消',
      interrupted: '已中断',
    };
    const goalStatusLabels = {
      draft: '草稿',
      active: '推进中',
      waiting_user: '等待用户确认',
      completed: '目标完成',
      failed: '目标失败',
      cancelled: '已取消',
      superseded: '已替代',
    };

    function executionTone(status) {
      if (['failed', 'timed_out', 'interrupted'].includes(status)) return 'red';
      if (['waiting_user', 'waiting_approval'].includes(status)) return 'amber';
      if (['completed', 'succeeded'].includes(status)) return 'green';
      return 'blue';
    }

    function compactTimestamp(value) {
      if (!value) return '—';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return date.toLocaleString('zh-CN', {
        month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
      });
    }

    function compatibilityExecutionPayload() {
      const tasks = state.lastTasks || [];
      const runs = tasks.map((task) => ({
        id: `task-${task.id}`,
        goal_id: `goal-${task.id}`,
        legacy_task_id: task.id,
        status: task.status === 'done' ? 'succeeded' : task.status === 'timeout' ? 'timed_out' : task.status,
        strategy: task.sandbox === 'workspace-write' ? 'sandbox' : 'direct',
        capability_id: 'codex.task',
        summary: task.summary || task.origin_message || '',
        updated_at: task.updated_at || task.finished_at || task.created_at || '',
      }));
      const goals = tasks.map((task, index) => ({
        id: `goal-${task.id}`,
        current_run_id: `task-${task.id}`,
        legacy_root_task_id: task.id,
        title: task.origin_message || task.summary || `兼容任务 #${task.id}`,
        status: runs[index].status === 'succeeded' ? 'completed' : ['running', 'queued'].includes(runs[index].status) ? 'active' : runs[index].status === 'waiting_approval' ? 'waiting_user' : 'failed',
        completion_policy: 'auto',
        updated_at: runs[index].updated_at,
      }));
      const countBy = (items) => items.reduce((result, item) => {
        result[item.status] = Number(result[item.status] || 0) + 1;
        return result;
      }, {});
      return {
        compatibility: true,
        overview: {
          goals: { total: goals.length, counts: countBy(goals) },
          runs: { total: runs.length, counts: countBy(runs) },
          evidence: 0,
          run_events: 0,
        },
        goals,
        runs,
        evidence: [],
        deliveries: { pending: tasks.filter((task) => task.delivery_status === 'pending').length },
      };
    }

    function renderExecutionOverview(payload = {}) {
      state.executionOverview = payload;
      const overview = payload.overview || payload;
      const goalStats = overview.goals || { total: 0, counts: {} };
      const runStats = overview.runs || { total: 0, counts: {} };
      const goalCounts = goalStats.counts || {};
      const runCounts = runStats.counts || {};
      const goals = Array.isArray(payload.goals) ? payload.goals : [];
      const runs = Array.isArray(payload.runs) ? payload.runs : [];
      const evidence = Array.isArray(payload.evidence) ? payload.evidence : [];
      const deliveries = payload.deliveries || payload.outbox || {};
      const activeGoalCount = Number(goalCounts.active || 0);
      const activeRunCount = Number(runCounts.running || 0) + Number(runCounts.queued || 0);
      const attentionCount = Math.max(Number(goalCounts.waiting_user || 0), Number(runCounts.waiting_approval || 0))
        + Number(runCounts.failed || 0) + Number(runCounts.timed_out || 0);
      const evidenceCount = Number(overview.evidence || evidence.length || 0);
      $('executionSummary').innerHTML = [
        ['推进中的任务', activeGoalCount, `共 ${goalStats.total || goals.length} 项任务`, 'blue'],
        ['正在执行', activeRunCount, `共 ${runStats.total || runs.length} 次尝试`, activeRunCount ? 'blue' : 'green'],
        ['需要处理', attentionCount, '等待确认、失败或超时', attentionCount ? 'amber' : 'green'],
        ['结果依据', evidenceCount, '可追溯的验证记录', evidenceCount ? 'green' : 'amber'],
      ].map(([label, value, detail, tone], index) => `<article class="insight-card ${tone}"><span class="insight-index">0${index + 1}</span><p>${escapeHtml(label)}</p><strong>${escapeHtml(value)}</strong><small>${escapeHtml(detail)}</small></article>`).join('');

      const runById = new Map(runs.map((run) => [run.id, run]));
      const orderedGoals = [...goals].sort((left, right) => String(right.updated_at || '').localeCompare(String(left.updated_at || '')));
      $('goalRunGrid').innerHTML = orderedGoals.length ? orderedGoals.map((goal) => {
        const run = runById.get(goal.current_run_id) || runs.find((item) => item.goal_id === goal.id) || {};
        const status = goal.status || 'draft';
        const tone = executionTone(status);
        const cardClass = tone === 'red' ? 'failed' : tone === 'amber' ? 'waiting' : '';
        const legacyTaskId = run.legacy_task_id || goal.legacy_root_task_id || '';
        const rawTitle = String(goal.title || '').trim();
        const displayTitle = /\?{3,}/.test(rawTitle) ? '历史任务（标题不可用）' : (rawTitle || '未命名任务');
        return `<article class="run-card ${cardClass}" data-collection-status="${escapeHtml(status)}">
          <div class="run-goal">
            <div><span class="entity-type">任务</span><h3>${escapeHtml(displayTitle)}</h3></div>
            <div><span class="status-chip ${tone}">${escapeHtml(goalStatusLabels[status] || status)}</span></div>
            <p>${goal.completion_policy === 'user_confirm' ? '执行完成后仍需要你的结果确认。' : '结果验证充分时，任务会自动完成。'}</p>
          </div>
          <div class="run-attempt">
            <p>${escapeHtml(run.summary || '尚未开始执行。')}</p>
            <div class="run-meta">
              <div><span>状态</span><strong>${escapeHtml(runStatusLabels[run.status] || run.status || '未运行')}</strong></div>
              <div><span>执行方式</span><strong>${escapeHtml(run.strategy || '自动选择')}</strong></div>
              <div><span>处理能力</span><strong>${escapeHtml(run.capability_id ? '助手执行' : '尚未分配')}</strong></div>
              <div><span>更新</span><strong>${escapeHtml(compactTimestamp(run.updated_at || goal.updated_at))}</strong></div>
            </div>
            <div class="entity-actions">
              <button class="secondary" type="button" data-task-timeline="${escapeHtml(goal.id || '')}">查看时间线</button>
              ${legacyTaskId ? `<button class="link" type="button" data-execution-task="${escapeHtml(legacyTaskId)}">查看技术详情</button>` : ''}
            </div>
          </div>
        </article>`;
      }).join('') : '<div class="empty-state">暂无任务。把一件事交给助手后，这里会显示当前进度和需要你的下一步。</div>';

    }

    async function loadExecutionOverview() {
      try {
        const result = await bridge('/execution/overview?limit=50');
        renderExecutionOverview(result);
      } catch (error) {
        // 滚动升级期间用真实 Task 做兼容投影；服务端升级完成后自动切回领域模型。
        const fallback = compatibilityExecutionPayload();
        fallback.compatibility_error = error.message || String(error);
        renderExecutionOverview(fallback);
      }
      await loadDeliveryOperations();
    }

    async function loadDeliveryOperations() {
      if (!$('deliveryOperationsPanel')) {
        $('executionSummary').insertAdjacentHTML('afterend', `<section id="deliveryOperationsPanel" class="panel hidden" aria-labelledby="deliveryOperationsTitle"><div class="panel-header"><div><h2 id="deliveryOperationsTitle">送达异常</h2><p id="deliveryOperationsNote" class="compact-note">${dNote}</p></div></div><div id="deliveryOperationsStatus" class="visually-hidden" role="status" aria-live="polite"></div><div class="table-wrap"><table><thead><tr><th>记录</th><th>来源</th><th>渠道</th><th>送达判断</th><th>尝试</th><th>原因</th><th>异常时间</th><th>操作</th></tr></thead><tbody id="deliveryDeadLetterRows"></tbody></table></div></section>`);
      }
      const panel = $('deliveryOperationsPanel');
      const rows = $('deliveryDeadLetterRows');
      try {
        const result = await bridge('/reliability/dead-letters');
        const items = Array.isArray(result.dead_letters) ? result.dead_letters : [];
        panel.classList.toggle('hidden', !items.length);
        $('deliveryOperationsNote').textContent = result.explanatory_note || dNote;
        rows.innerHTML = items.map((item) => {
          const ambiguous = item.delivery_certainty === 'ambiguous';
          return `<tr>
          <td><code>${escapeHtml(String(item.id || '').slice(0, 12))}</code></td>
          <td>${escapeHtml(item.source_kind || '未标注来源')}</td>
          <td>${escapeHtml(item.channel || '-')}</td>
          <td><span class="status-chip ${ambiguous ? 'amber' : 'red'}">${ambiguous ? '结果不确定' : '确认未送达'}</span></td>
          <td>${escapeHtml(`${item.attempt || 0}/${item.max_attempts || 0}`)}</td>
          <td><strong>${escapeHtml(item.error_kind || 'unknown')}</strong><br><small>${escapeHtml(item.error_summary || item.review_status || '')}</small></td><td>${escapeHtml(compactTimestamp(item.dead_lettered_at))}</td>
          <td><button class="secondary" type="button" data-delivery-requeue="${escapeHtml(item.id || '')}" data-delivery-ambiguous="${ambiguous ? '1' : '0'}">${ambiguous ? '接受重复风险并重试' : '确认后重试'}</button></td>
        </tr>`;
        }).join('');
      } catch (error) {
        panel.classList.remove('hidden');
        rows.innerHTML = `<tr><td colspan="8">${escapeHtml(error.message || String(error))}</td></tr>`;
      }
    }

    async function requeueDeadLetter(button) {
      const ambiguous = button.dataset.deliveryAmbiguous === '1';
      const warning = ambiguous
        ? '该消息可能已经发送成功。再次送达可能让用户收到重复内容。确认接受此风险并重试吗？'
        : '确认重新送达这条失败消息吗？';
      if (!window.confirm(warning)) return;
      button.disabled = true;
      try {
        await bridge(`/reliability/dead-letters/${encodeURIComponent(button.dataset.deliveryRequeue)}/requeue`, {
          method: 'POST', body: { confirm_requeue: true, confirm_duplicate_risk: ambiguous },
        });
        $('deliveryOperationsStatus').textContent = ambiguous ? '已接受重复风险，消息重新进入送达队列。' : '失败消息已重新进入送达队列。';
        await loadExecutionOverview();
      } catch (error) {
        setConnection(error.message || String(error), 'error');
        button.disabled = false;
      }
    }

