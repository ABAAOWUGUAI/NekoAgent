(() => {
  'use strict';

  const statusLabels = {
    trial: '自动试用',
    stable: '已稳定',
    needs_confirmation: '待确认',
    conflicted: '存在冲突',
    candidate: '待观察',
    rejected: '已撤销',
  };
  const feedbackButtons = {
    trial: [['accept', '保留'], ['undo', '撤销']],
    stable: [['undo', '撤销']],
    needs_confirmation: [['accept', '确认'], ['reject', '拒绝']],
    conflicted: [['accept', '确认'], ['reject', '拒绝']],
  };

  function learningDate(value) {
    const date = new Date(value || '');
    return Number.isNaN(date.getTime()) ? String(value || '') : date.toLocaleString();
  }

  function ensureLearningMarkup() {
    if ($('view-growth')) return;
    const root = document.createElement('section');
    root.id = 'view-growth';
    root.className = 'view';
    root.setAttribute('aria-labelledby', 'growthHeading');
    root.innerHTML = `
      <div class="panel"><div class="panel-head"><div><h2 id="growthHeading">学习</h2></div><span id="learningFeatureBadge" class="badge">观察中</span></div>
      <div class="panel-body"><p class="compact-note">学习不是把所有记录都变成行为。这里只接纳明确、可撤销且有作用域的偏好；运行诊断只用于排障。</p>
      <div id="learningMetrics" class="overview-metrics"></div><p id="learningSignalExplanation" class="compact-note" role="status" aria-live="polite"></p>
      <form id="learningPolicyForm"><fieldset class="button-row"><legend>学习准入</legend><label class="checkbox-line"><input id="learningEnabled" type="checkbox">记录学习信号</label><label class="checkbox-line"><input id="lowRiskLearningEnabled" type="checkbox">自动试用私聊中的低风险表达偏好</label><label class="checkbox-line"><input id="ownerGroupExpressionFeedbackEnabled" type="checkbox" aria-describedby="ownerGroupExpressionFeedbackHelp">允许 Owner 在群内的明确表达纠正进入待确认候选</label><span id="ownerGroupExpressionFeedbackHelp" class="compact-note">只限说出该纠正的群；不会自动试用，确认后才应用。</span><button class="secondary" type="submit">保存策略</button><span id="learningPolicyStatus" class="provider-status" role="status" aria-live="polite"></span></fieldset></form>
      <details class="compact-note"><summary>学习边界与准入规则</summary><dl><dt>会形成候选</dt><dd>明确表达偏好；私聊低风险可限时试用，群内仅 Owner 且需确认。</dd><dt>只观察</dt><dd>送达、ACK、可靠性、执行结果和模型健康；它们不能变成语气或能力。</dd><dt>绝不自动学习</dt><dd>事实、记忆、知识、关系、权限、审批、网络、模型凭据、Skill、代码与敏感内容。</dd></dl></details></div></div>
      <div class="panel"><div class="panel-head"><div><h2>待确认的学习</h2></div><span class="meta">可撤销</span></div><div id="learningCandidateList" class="panel-body"><p class="empty-state">暂无候选。</p></div></div>
      <details class="panel"><summary class="panel-head"><span>最近的应用依据</span><span id="learningTraceMeta" class="meta">最近 8 条</span></summary><div id="learningTraceList" class="panel-body"><p class="empty-state">暂无应用记录。</p></div></details>`;
    $('contentViewport').append(root);
  }

  function renderLearningSummary(result) {
    const counts = result.counts || {};
    const admissions = result.signal_counts_by_admission || {};
    const metrics = [
      ['诊断观察', Number(admissions.diagnostic || 0)],
      ['可候选信号', Number(admissions.candidate || 0)],
      ['待确认', Number(counts.needs_confirmation || 0) + Number(counts.conflicted || 0)],
      ['自动试用', Number(counts.trial || 0)],
      ['已稳定', Number(counts.stable || 0)],
      ['已应用', Number(counts.applications_total || 0)],
      ['已反馈', Number(counts.feedback_total || 0)],
    ];
    $('learningMetrics').innerHTML = metrics.map(([label, value]) => (
      `<div class="overview-metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`
    )).join('');
    $('learningEnabled').checked = Boolean(result.feature_enabled);
    $('lowRiskLearningEnabled').checked = Boolean(result.low_risk_enabled);
    $('ownerGroupExpressionFeedbackEnabled').checked = Boolean(result.owner_group_expression_feedback_enabled);
    const active = Boolean(result.feature_enabled);
    $('learningFeatureBadge').textContent = active ? (result.low_risk_enabled ? '低风险试用已启用' : 'Shadow') : '已关闭';
    $('learningFeatureBadge').className = `badge${active ? ' green' : ''}`;
    const signals = result.signal_counts || {};
    const policy = result.candidate_policy || {};
    const signalExplanation = $('learningSignalExplanation');
    if (signalExplanation) {
      const signalSummary = Object.entries(signals).map(([type, count]) => `${type} ${count}`).join('；');
      signalExplanation.textContent = Number(counts.signals_total || 0)
        ? `已记录 ${Number(counts.signals_total || 0)} 条信号：${signalSummary || '已分类'}。诊断观察不会生成学习候选；${policy.operational_signals || ''}`
        : `当前还没有学习信号。${policy.automatic_scope || ''}`;
    }
    const candidates = result.candidates || [];
    $('learningCandidateList').innerHTML = candidates.length ? candidates.map((item) => {
      const value = item.value || {};
      const buttons = (feedbackButtons[item.status] || []).map(([type, label]) => (
        `<button class="secondary" type="button" data-learning-feedback="${escapeHtml(type)}" data-learning-candidate="${escapeHtml(item.id)}">${label}</button>`
      )).join('');
      return `<article class="memory-candidate-card">
        <div class="knowledge-card-meta"><span class="knowledge-kind">${escapeHtml(value.feedback_type || item.domain || '学习')}</span><span class="badge">${escapeHtml(statusLabels[item.status] || item.status)}</span></div>
        <p>${escapeHtml(value.style || '结构化偏好')}</p>
        <div class="knowledge-provenance"><span>${escapeHtml(item.scope_type)} · ${escapeHtml(item.subject_id)}</span><span>${Math.round(Number(item.confidence || 0) * 100)}% · ${learningDate(item.updated_at)}</span></div>
        <div class="button-row">${buttons}</div>
      </article>`;
    }).join('') : '<p class="empty-state">暂无学习候选。</p>';
  }

  function renderLearningTrace(result) {
    const items = result.items || [];
    if ($('learningTraceMeta')) {
      $('learningTraceMeta').textContent = items.length ? `${items.length} 条` : '暂无';
    }
    $('learningTraceList').innerHTML = items.length ? items.map((item) => (
      `<div class="knowledge-provenance"><span>${escapeHtml(item.domain)} · ${escapeHtml(item.decision)}</span><span>${escapeHtml(item.source_type)} · ${learningDate(item.created_at)}</span></div>`
    )).join('') : '<p class="empty-state">暂无应用记录。</p>';
  }

  async function loadLearningPanel() {
    ensureLearningMarkup();
    const [summary, trace] = await Promise.all([
      bridge('/assistant/learning'),
      bridge('/assistant/learning/trace?limit=8'),
    ]);
    renderLearningSummary(summary.result || summary);
    renderLearningTrace(trace.result || trace);
    return summary;
  }

  async function saveLearningPolicy(event) {
    event.preventDefault();
    const node = $('learningPolicyStatus');
    try {
      const result = await bridge('/assistant/learning/policy', {
        method: 'POST',
        body: JSON.stringify({
          enabled: $('learningEnabled').checked,
          low_risk: $('lowRiskLearningEnabled').checked,
          owner_group_expression_feedback: $('ownerGroupExpressionFeedbackEnabled').checked,
        }),
      });
      node.textContent = '策略已保存';
      node.className = 'provider-status ok';
      renderLearningSummary(result.result || result);
    } catch (error) {
      node.textContent = error.message || String(error);
      node.className = 'provider-status error';
    }
  }

  async function submitLearningFeedback(button) {
    button.disabled = true;
    try {
      await bridge('/assistant/learning/feedback', {
        method: 'POST',
        headers: { 'Idempotency-Key': `learning-ui-${button.dataset.learningCandidate}-${button.dataset.learningFeedback}` },
        body: JSON.stringify({
          candidate_id: button.dataset.learningCandidate,
          feedback_type: button.dataset.learningFeedback,
        }),
      });
      await loadLearningPanel();
    } catch (error) {
      setConnection(error.message || String(error), 'error');
      button.disabled = false;
    }
  }

  window.loadLearningPanel = loadLearningPanel;
  document.addEventListener('submit', (event) => {
    if (event.target?.id === 'learningPolicyForm') saveLearningPolicy(event);
  });
  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-learning-feedback]');
    if (button) submitLearningFeedback(button);
  });
})();
