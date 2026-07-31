(() => {
  'use strict';

  const baseRelationshipLoader = window.loadRelationshipManagement;
  const socialVirtualState = {
    cutover: null,
    profile: null,
    templates: [],
    events: [],
    audits: [],
    eventsBound: false,
  };

  function ensureSocialVirtualMarkup() {
    if (!$('socialProtocolMount')) {
      const root = document.createElement('div');
      root.id = 'socialVirtualMount';
      $('view-relationship').append(root);
      root.innerHTML = `
        <section class="panel social-protocol-panel" aria-labelledby="socialProtocolTitle">
          <div class="panel-head"><div><p class="section-index">INTERACTION TRACE</p><h2 id="socialProtocolTitle">群聊互动判断</h2><p class="compact-note">这里说明是否已经判断、为什么暂时不插话，以及回复是否真正进入 Delivery/ACK 链路。</p></div><span id="socialProtocolBadge" class="badge">未载入</span></div>
          <div id="socialProtocolMount" class="panel-body"></div>
        </section>
        <details class="panel virtual-life-panel advanced-social-panel">
          <summary><span><span class="section-index">ADVANCED CONFIGURATION</span><strong id="virtualLifeTitle">虚拟生活（仅私有预览）</strong></span><span id="virtualLifeVersion" class="meta">版本 0</span></summary>
          <div id="virtualLifeMount" class="panel-body"></div>
        </details>`;
    }
    if ($('socialVirtualCutoverForm')) return;
    $('socialProtocolMount').innerHTML = `
      <div id="socialOpportunityLedger" class="social-ledger" aria-live="polite"><div class="empty-state">正在读取互动判断。</div></div>
      <details class="advanced-social-panel social-gate-panel">
        <summary><span><span class="section-index">GATE</span><strong>互动 Gate 配置</strong></span><span class="meta">低频配置</span></summary>
        <p class="compact-note">reply、join、start 共用同一套“为什么现在、聊什么、用什么姿态”协议。群参与模式仍在 QQ 配置页编辑。</p>
        <form id="socialVirtualCutoverForm" class="social-cutover-form">
        <label class="checkbox-line"><input id="socialProtocolEnabled" type="checkbox">启用统一社交机会协议</label>
        <label class="checkbox-line"><input id="virtualLifeFeatureEnabled" type="checkbox">启用 Virtual Life 管理能力</label>
        <button id="saveSocialVirtualCutoverBtn" class="secondary" type="submit">保存 Gate 开关</button>
        <div id="socialVirtualCutoverStatus" class="provider-status" role="status" aria-live="polite">尚未载入。</div>
        </form>
      </details>`;
    const weekdays = ['一', '二', '三', '四', '五', '六', '日'].map((label, index) => (
      `<label><input name="virtualTemplateDay" type="checkbox" value="${index}" checked>${label}</label>`
    )).join('');
    $('virtualLifeMount').innerHTML = `
      <div class="virtual-boundary-note" role="note"><strong>虚拟事实边界</strong><span>这些事件是可见的虚拟生活片段，不是现实经历。V1 不会自动发送 QQ 消息。</span></div>
      <form id="virtualLifeProfileForm" class="form-stack">
        <div class="virtual-profile-grid">
          <label class="checkbox-line"><input id="virtualLifeEnabled" type="checkbox">生成并显示虚拟事件</label>
          <label>时区<input id="virtualLifeTimezone" type="text" maxlength="80" value="Asia/Shanghai" required autocomplete="off"></label>
          <label>活跃开始<input id="virtualLifeActiveStart" type="time" value="08:00" required></label>
          <label>活跃结束<input id="virtualLifeActiveEnd" type="time" value="23:00" required></label>
          <label>分享边界<select id="virtualLifeSharePolicy"><option value="private_preview_only">仅本人预览</option><option value="private_reviewable">可进入本人审核</option><option value="disabled">完全禁用</option></select></label>
          <label>保留天数<input id="virtualLifeRetentionDays" type="number" min="1" max="3650" value="90" required></label>
          <label>生成方式<select id="virtualLifeGenerationMode"><option value="manual_or_daily_visible">手动或每日可见</option><option value="manual_only">仅手动</option></select></label>
        </div>
        <label>虚拟地点（每行一个）<textarea id="virtualLifePlaces" rows="3" maxlength="1200" placeholder="云端书房&#10;虚拟花园"></textarea></label>
        <label>禁用类别（每行一个）<textarea id="virtualLifeBlockedCategories" rows="2" maxlength="800" placeholder="例如：outing"></textarea></label>
        <button id="saveVirtualLifeProfileBtn" class="primary" type="submit">保存虚拟生活配置</button>
        <div id="virtualLifeProfileStatus" class="provider-status" role="status" aria-live="polite">尚未载入。</div>
      </form>
      <details class="virtual-template-editor"><summary>添加或编辑活动模板</summary>
        <form id="virtualLifeTemplateForm" class="form-stack">
          <input id="virtualTemplateId" type="hidden"><input id="virtualTemplateVersion" type="hidden" value="0">
          <div class="virtual-profile-grid">
            <label>类别<input id="virtualTemplateCategory" type="text" maxlength="80" required placeholder="reading"></label>
            <label>标题模板<input id="virtualTemplateTitle" type="text" maxlength="200" required placeholder="在{place}读一会儿书"></label>
            <label>虚拟地点<input id="virtualTemplatePlace" type="text" maxlength="120" placeholder="云端书房"></label>
            <label>开始窗口<input id="virtualTemplateStart" type="time" value="09:00" required></label>
            <label>结束窗口<input id="virtualTemplateEnd" type="time" value="22:00" required></label>
            <label>权重<input id="virtualTemplateWeight" type="number" min="1" max="100" value="1" required></label>
            <label>可分享级别<select id="virtualTemplateShareLevel"><option value="private">仅私有</option><option value="reviewable">可审核</option></select></label>
            <label class="checkbox-line"><input id="virtualTemplateEnabled" type="checkbox" checked>启用模板</label>
          </div>
          <label>描述模板<textarea id="virtualTemplateDescription" rows="2" maxlength="1000" placeholder="明确标记为虚拟生活片段。"></textarea></label>
          <fieldset><legend>活跃星期</legend><div class="virtual-weekdays">${weekdays}</div></fieldset>
          <button id="saveVirtualLifeTemplateBtn" class="secondary" type="submit">保存活动模板</button>
          <div id="virtualLifeTemplateStatus" class="provider-status" role="status" aria-live="polite">尚未保存模板。</div>
        </form>
      </details>
      <div class="virtual-life-actions"><button id="generateVirtualLifeEventBtn" class="primary" type="button">生成今天的虚拟事件</button><span id="virtualLifeGenerateStatus" class="provider-status" role="status" aria-live="polite">不会触发消息发送。</span></div>
      <div class="virtual-life-columns">
        <section aria-labelledby="virtualTemplatesTitle"><h3 id="virtualTemplatesTitle">活动模板</h3><div id="virtualLifeTemplateList" class="virtual-list"><div class="empty-state">暂无模板。</div></div></section>
        <section aria-labelledby="virtualEventsTitle"><h3 id="virtualEventsTitle">事件与审计</h3><div id="virtualLifeEventList" class="virtual-list" aria-live="polite"><div class="empty-state">暂无事件。</div></div></section>
      </div>`;
  }

  function svStatus(id, message, tone = '') {
    const node = $(id);
    if (!node) return;
    node.textContent = message;
    node.className = `provider-status${tone ? ` ${tone}` : ''}`;
  }

  function svKey(prefix) {
    return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function svLines(value) {
    return String(value || '').split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
  }

  function svDate(value) {
    if (!value) return '未知时间';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }

  function renderCutover(result) {
    socialVirtualState.cutover = result;
    const social = result.flags?.social_opportunity_v1?.enabled === true;
    const virtual = result.flags?.virtual_life_v1?.enabled === true;
    $('socialProtocolEnabled').checked = social;
    $('virtualLifeFeatureEnabled').checked = virtual;
    $('socialProtocolBadge').textContent = social ? '协议已启用' : '协议未启用';
    $('socialProtocolBadge').className = `badge${social ? ' green' : ''}`;
    svStatus('socialVirtualCutoverStatus', `统一社交：${social ? '启用' : '关闭'} · Virtual Life：${virtual ? '启用' : '关闭'}`, social || virtual ? 'ok' : '');
  }

  function describeOpportunityDecision(item) {
    const reasonLabels = {
      group_disabled: '群作用域当时未开启：全局 Gate 已开，但该群策略在这次决策时关闭。',
      mention_required: '需要明确 @ 助手后才会处理。',
      active_reply_disabled: '该群未允许主动回复，保持静默。',
      engagement_below_threshold: '当前话题参与价值不足，暂不插话。',
      model_engagement_declined: '模型判断当前没有足够自然的切入点，暂不插话。',
      model_engagement_approved: '模型确认当前话题可以自然参与。',
      model_no_reply: '当前没有自然切入点，暂不打断群聊。',
      duplicate_group_message: '与前一条群消息重复，暂不重复处理。',
      natural_deferred: '等待下一轮消息，避免抢话。',
      quiet_gap: '仍在安静窗口内，先合并连续消息，避免抢话。',
      burst_coalescing: '当前消息密度较高，已合并为一次后续判断。',
      daily_reply_budget: '已达到本群当天的主动参与上限。',
      participation_threshold: '模型判断的参与把握不足，保持静默。',
      group_classifier_failed: '参与判断模型未能完成，本次未发送，等待下一次自然触发。',
      group_delivery_not_queued: '已经决定参与，但回复未进入投递队列；这是一条需要处理的运行异常。',
      group_participation_worker_failed: '参与工作器异常结束，本次未发送；需要查看运行证据。',
      invalid_model_social_contract: '模型返回不满足互动协议，本次未发送。',
      explicit_mention: '检测到明确提及，进入回复流程。',
      direct_private: '这是私聊消息，进入直接回复流程。',
    };
    const decision = item.decision || {};
    const reasonCode = decision.reason_code || decision.reason || item.status || 'unknown';
    const action = decision.action || '';
    const waiting = decision.phase === 'awaiting_final_decision';
    const isReply = action === 'reply' || action === 'send' || action === 'contextual_participation';
    const isSilent = !waiting && !isReply;
    const reasonLabel = reasonLabels[reasonCode] || (
      isReply ? '服务端已确认这次可以参与；具体投递状态以 Delivery/ACK 为准。'
        : '模型已完成参与判断，当前保持不插话。'
    );
    if (waiting) {
      return {
        tone: 'pending',
        title: '正在等待最终判断',
        detail: '已记录互动机会；尚未形成最终参与或静默结果，因此不会把它当成待办或已发送。',
        reasonCode,
        reasonLabel: '仍在等待服务端形成最终决定。',
      };
    }
    if (isReply) {
      return {
        tone: 'positive',
        title: '已决定参与',
        detail: '已进入回复链路；只有 Delivery 出现 confirmed ACK，才代表对方可见。',
        reasonCode,
        reasonLabel,
      };
    }
    return {
      tone: 'muted',
      title: '已完成判断：暂不插话',
      detail: reasonLabels[reasonCode] || '服务端已完成本次参与判断；当前不发送回复。',
      reasonCode,
      reasonLabel,
      isSilent,
    };
  }

  function renderOpportunityCards(items) {
    const labels = { reply: '回复', join: '加入群聊', start: '主动发起' };
    return items.length ? items.map((item) => {
      const decision = item.decision || {};
      const candidate = (item.candidates || []).find((entry) => entry.id === decision.topic_candidate_id);
      const state = describeOpportunityDecision(item);
      const decisionPhase = decision.phase === 'awaiting_final_decision' ? '等待最终判断' : '最终判断';
      return `
        <article class="social-ledger-card">
          <div class="social-ledger-head">
            <strong>${escapeHtml(labels[item.kind] || item.kind)}</strong>
            <span class="badge${state.tone === 'positive' ? ' green' : ''}">${escapeHtml(state.title)}</span>
          </div>
          <div class="social-decision-banner ${escapeHtml(state.tone)}">
            <strong>${escapeHtml(state.title)}</strong>
            <span>${escapeHtml(state.detail)}</span>
          </div>
          <dl>
            <dt>判断依据</dt><dd>${escapeHtml(state.reasonLabel)}</dd>
            <dt>决策阶段</dt><dd>${escapeHtml(decisionPhase)}</dd>
            ${decision.delivery_id ? `<dt>Delivery</dt><dd>${escapeHtml(decision.delivery_id)} ${escapeHtml(decision.feedback_state || '等待 ACK 状态')}</dd>` : ''}
          </dl>
          <details class="social-audit-details">
            <summary>查看审计字段</summary>
            <dl>
              <dt>为什么现在</dt><dd>${escapeHtml(decision.why_now || '未提供')}</dd>
              <dt>话题证据</dt><dd>${escapeHtml(candidate?.summary || decision.topic_candidate_id || '未提供')}</dd>
              <dt>姿态</dt><dd>${escapeHtml(decision.approach || '未提供')}</dd>
              <dt>作用域</dt><dd>${escapeHtml(`${item.subject_type}:${item.subject_id}`)}</dd>
              <dt>策略原码</dt><dd><code>${escapeHtml(state.reasonCode)}</code></dd>
            </dl>
          </details>
          <small>${escapeHtml(svDate(item.created_at))}</small>
        </article>`;
    }).join('') : '<div class="empty-state">暂无社交裁决。</div>';
  }

  function renderOpportunities(pendingItems, historyItems) {
    const pending = Array.isArray(pendingItems) ? pendingItems : [];
    const history = Array.isArray(historyItems) ? historyItems : [];
    const pendingMarkup = pending.length
      ? renderOpportunityCards(pending)
      : '<div class="empty-state">当前没有等待最终判断的互动机会。</div>';
    const historyMarkup = history.length
      ? `<details class="social-history"><summary>最近已完成的判断（${history.length}）；不代表待办</summary><div class="social-history-list">${renderOpportunityCards(history)}</div></details>`
      : '<div class="empty-state">暂无历史决策。</div>';
    $('socialOpportunityLedger').innerHTML = `
      <section class="social-ledger-section" aria-labelledby="socialPendingTitle">
        <div class="social-ledger-section-head"><div><p class="section-index">CURRENT</p><h3 id="socialPendingTitle">正在判断</h3></div><span class="meta">${pending.length} 条</span></div>
        ${pendingMarkup}
      </section>
      <section class="social-ledger-section social-history-section" aria-labelledby="socialHistoryTitle">
        <div class="social-ledger-section-head"><div><p class="section-index">HISTORY</p><h3 id="socialHistoryTitle">已完成判断</h3></div><span class="meta">不代表待办</span></div>
        ${historyMarkup}
      </section>`;
  }

  function renderProfile(result) {
    const profile = result.profile || {};
    socialVirtualState.profile = profile;
    $('virtualLifeEnabled').checked = Boolean(profile.enabled);
    $('virtualLifeTimezone').value = profile.timezone || 'Asia/Shanghai';
    $('virtualLifeActiveStart').value = profile.active_start || '08:00';
    $('virtualLifeActiveEnd').value = profile.active_end || '23:00';
    $('virtualLifeSharePolicy').value = profile.share_policy || 'private_preview_only';
    $('virtualLifeRetentionDays').value = Number(profile.retention_days || 90);
    $('virtualLifeGenerationMode').value = profile.generation_mode || 'manual_or_daily_visible';
    $('virtualLifePlaces').value = (profile.virtual_places || []).join('\n');
    $('virtualLifeBlockedCategories').value = (profile.blocked_categories || []).join('\n');
    $('virtualLifeVersion').textContent = `版本 ${Number(profile.version || 0)}`;
    svStatus('virtualLifeProfileStatus', result.feature_enabled ? '配置已载入；事件始终标记为虚拟。' : '配置已载入；请先启用 Virtual Life Gate 才能生成事件。', result.feature_enabled ? 'ok' : '');
  }

  function renderTemplates(items) {
    socialVirtualState.templates = items;
    $('virtualLifeTemplateList').innerHTML = items.length ? items.map((item) => `
      <article class="virtual-list-card">
        <div class="virtual-list-head"><strong>${escapeHtml(item.title_template)}</strong><span class="badge${item.enabled ? ' green' : ''}">${item.enabled ? '启用' : '关闭'}</span></div>
        <p>${escapeHtml(item.category)} · ${escapeHtml(item.virtual_place || '默认虚拟地点')}</p>
        <small>${escapeHtml(item.window_start)}–${escapeHtml(item.window_end)} · 权重 ${escapeHtml(item.weight)} · 版本 ${escapeHtml(item.version)}</small>
        <button class="secondary" type="button" data-virtual-template-edit="${escapeHtml(item.id)}">编辑模板</button>
      </article>`).join('') : '<div class="empty-state">暂无模板。请先添加一个活动模板。</div>';
  }

  function renderEvents(events, audits) {
    socialVirtualState.events = events;
    socialVirtualState.audits = audits;
    const latestAudit = new Map();
    audits.forEach((item) => { if (!latestAudit.has(item.event_id)) latestAudit.set(item.event_id, item); });
    $('virtualLifeEventList').innerHTML = events.length ? events.map((item) => {
      const audit = latestAudit.get(item.id);
      const action = item.status === 'deleted' ? 'restore' : 'delete';
      return `
        <article class="virtual-list-card">
          <div class="virtual-list-head"><strong>${escapeHtml(item.title)}</strong><span class="virtual-fact-badge">虚拟事件</span></div>
          <p>${escapeHtml(item.description || '无描述')}</p>
          <small>${escapeHtml(item.virtual_place || '虚拟空间')} · ${escapeHtml(svDate(item.starts_at))} · 版本 ${escapeHtml(item.version)}</small>
          <small>最近审计：${escapeHtml(audit ? `${audit.action} · ${svDate(audit.created_at)}` : '未载入')}</small>
          <div class="virtual-event-actions"><button class="secondary" type="button" data-virtual-event-action="${action}" data-event-id="${escapeHtml(item.id)}" data-event-version="${escapeHtml(item.version)}">${action === 'restore' ? '恢复' : '软删除'}</button></div>
        </article>`;
    }).join('') : '<div class="empty-state">暂无事件。生成前不会自动产生或发送任何消息。</div>';
  }

  async function loadSocialVirtual() {
    const [cutover, pending, history, profile, templates, events, audits] = await Promise.all([
      bridge('/assistant/social-virtual/cutover'),
      bridge('/assistant/social/opportunities?status=open&limit=20'),
      bridge('/assistant/social/opportunities?status=decided&limit=20'),
      bridge('/assistant/virtual-life/profile'),
      bridge('/assistant/virtual-life/templates'),
      bridge('/assistant/virtual-life/events?include_deleted=1&limit=50'),
      bridge('/assistant/virtual-life/audits?limit=100'),
    ]);
    renderCutover(cutover);
    renderOpportunities(pending.opportunities || [], history.opportunities || []);
    renderProfile(profile);
    renderTemplates(templates.templates || []);
    renderEvents(events.events || [], audits.audits || []);
  }

  async function saveCutover(event) {
    event.preventDefault();
    const current = socialVirtualState.cutover;
    if (!current?.contract_checksum) return;
    const button = $('saveSocialVirtualCutoverBtn');
    button.disabled = true;
    svStatus('socialVirtualCutoverStatus', '正在原子应用 Gate 开关。', 'pending');
    try {
      const result = await bridge('/assistant/social-virtual/cutover', {
        method: 'POST',
        headers: { 'Idempotency-Key': svKey('social-virtual-cutover') },
        body: JSON.stringify({
          contract_checksum: current.contract_checksum,
          social_enabled: $('socialProtocolEnabled').checked,
          virtual_life_enabled: $('virtualLifeFeatureEnabled').checked,
        }),
      });
      renderCutover(result);
      setConnection('社交与虚拟生活 Gate 已应用并回读。', 'ok');
    } catch (error) {
      svStatus('socialVirtualCutoverStatus', error.message || String(error), 'error');
      throw error;
    } finally {
      button.disabled = false;
    }
  }

  async function saveProfile(event) {
    event.preventDefault();
    const profile = socialVirtualState.profile || { version: 0 };
    const button = $('saveVirtualLifeProfileBtn');
    button.disabled = true;
    svStatus('virtualLifeProfileStatus', '正在保存并回读配置。', 'pending');
    try {
      const result = await bridge('/assistant/virtual-life/profile', {
        method: 'POST', headers: { 'Idempotency-Key': svKey('virtual-profile') },
        body: JSON.stringify({
          expected_version: Number(profile.version || 0),
          enabled: $('virtualLifeEnabled').checked,
          timezone: $('virtualLifeTimezone').value.trim(),
          active_start: $('virtualLifeActiveStart').value,
          active_end: $('virtualLifeActiveEnd').value,
          share_policy: $('virtualLifeSharePolicy').value,
          retention_days: Number($('virtualLifeRetentionDays').value || 90),
          generation_mode: $('virtualLifeGenerationMode').value,
          virtual_places: svLines($('virtualLifePlaces').value),
          blocked_categories: svLines($('virtualLifeBlockedCategories').value),
        }),
      });
      renderProfile(result);
      setConnection('虚拟生活配置已保存。', 'ok');
    } catch (error) {
      svStatus('virtualLifeProfileStatus', error.message || String(error), 'error');
      throw error;
    } finally {
      button.disabled = false;
    }
  }

  function resetTemplateForm() {
    $('virtualTemplateId').value = '';
    $('virtualTemplateVersion').value = '0';
  }

  async function saveTemplate(event) {
    event.preventDefault();
    const button = $('saveVirtualLifeTemplateBtn');
    button.disabled = true;
    svStatus('virtualLifeTemplateStatus', '正在保存模板。', 'pending');
    try {
      const result = await bridge('/assistant/virtual-life/templates', {
        method: 'POST', headers: { 'Idempotency-Key': svKey('virtual-template') },
        body: JSON.stringify({
          id: $('virtualTemplateId').value,
          expected_version: Number($('virtualTemplateVersion').value || 0),
          category: $('virtualTemplateCategory').value.trim(),
          title_template: $('virtualTemplateTitle').value.trim(),
          description_template: $('virtualTemplateDescription').value.trim(),
          virtual_place: $('virtualTemplatePlace').value.trim(),
          active_days: Array.from(document.querySelectorAll('input[name="virtualTemplateDay"]:checked')).map((node) => Number(node.value)),
          window_start: $('virtualTemplateStart').value,
          window_end: $('virtualTemplateEnd').value,
          weight: Number($('virtualTemplateWeight').value || 1),
          share_level: $('virtualTemplateShareLevel').value,
          enabled: $('virtualTemplateEnabled').checked,
        }),
      });
      const existing = socialVirtualState.templates.filter((item) => item.id !== result.template.id);
      renderTemplates([...existing, result.template]);
      $('virtualTemplateId').value = result.template.id;
      $('virtualTemplateVersion').value = result.template.version;
      svStatus('virtualLifeTemplateStatus', `模板已保存，版本 ${result.template.version}。`, 'ok');
    } catch (error) {
      svStatus('virtualLifeTemplateStatus', error.message || String(error), 'error');
      throw error;
    } finally {
      button.disabled = false;
    }
  }

  function editTemplate(id) {
    const item = socialVirtualState.templates.find((entry) => entry.id === id);
    if (!item) return;
    $('virtualTemplateId').value = item.id;
    $('virtualTemplateVersion').value = item.version;
    $('virtualTemplateCategory').value = item.category;
    $('virtualTemplateTitle').value = item.title_template;
    $('virtualTemplateDescription').value = item.description_template || '';
    $('virtualTemplatePlace').value = item.virtual_place || '';
    $('virtualTemplateStart').value = item.window_start;
    $('virtualTemplateEnd').value = item.window_end;
    $('virtualTemplateWeight').value = item.weight;
    $('virtualTemplateShareLevel').value = item.share_level;
    $('virtualTemplateEnabled').checked = Boolean(item.enabled);
    document.querySelectorAll('input[name="virtualTemplateDay"]').forEach((node) => { node.checked = (item.active_days || []).includes(Number(node.value)); });
    const details = $('virtualLifeTemplateForm').closest('details');
    details.open = true;
    $('virtualTemplateCategory').focus();
  }

  async function generateEvent() {
    const button = $('generateVirtualLifeEventBtn');
    button.disabled = true;
    svStatus('virtualLifeGenerateStatus', '正在确定性生成虚拟事件；不会发送消息。', 'pending');
    try {
      const result = await bridge('/assistant/virtual-life/generate', {
        method: 'POST', headers: { 'Idempotency-Key': svKey('virtual-generate') }, body: '{}',
      });
      svStatus('virtualLifeGenerateStatus', `已生成“${result.event.title}”；事实边界：virtual，Delivery：禁止。`, 'ok');
      await loadSocialVirtual();
    } catch (error) {
      svStatus('virtualLifeGenerateStatus', error.message || String(error), 'error');
      throw error;
    } finally {
      button.disabled = false;
    }
  }

  async function actOnEvent(button) {
    button.disabled = true;
    try {
      await bridge('/assistant/virtual-life/events/action', {
        method: 'POST', headers: { 'Idempotency-Key': svKey(`virtual-${button.dataset.virtualEventAction}`) },
        body: JSON.stringify({
          event_id: button.dataset.eventId,
          action: button.dataset.virtualEventAction,
          expected_version: Number(button.dataset.eventVersion),
          reason: '管理员在 Virtual Life 页面执行',
        }),
      });
      await loadSocialVirtual();
    } finally {
      button.disabled = false;
    }
  }

  function bindSocialVirtualEvents() {
    if (socialVirtualState.eventsBound || !$('socialVirtualCutoverForm')) return;
    socialVirtualState.eventsBound = true;
    $('socialVirtualCutoverForm').addEventListener('submit', (event) => saveCutover(event).catch((error) => setConnection(error.message || String(error), 'error')));
    $('virtualLifeProfileForm').addEventListener('submit', (event) => saveProfile(event).catch((error) => setConnection(error.message || String(error), 'error')));
    $('virtualLifeTemplateForm').addEventListener('submit', (event) => saveTemplate(event).catch((error) => setConnection(error.message || String(error), 'error')));
    $('generateVirtualLifeEventBtn').addEventListener('click', () => generateEvent().catch((error) => setConnection(error.message || String(error), 'error')));
    $('virtualLifeTemplateList').addEventListener('click', (event) => {
      const button = event.target.closest('[data-virtual-template-edit]');
      if (button) editTemplate(button.dataset.virtualTemplateEdit);
    });
    $('virtualLifeEventList').addEventListener('click', (event) => {
      const button = event.target.closest('[data-virtual-event-action]');
      if (button) actOnEvent(button).catch((error) => setConnection(error.message || String(error), 'error'));
    });
    resetTemplateForm();
  }

  window.loadRelationshipManagement = async function loadRelationshipAndSocialVirtual() {
    if (typeof baseRelationshipLoader === 'function') await baseRelationshipLoader();
    ensureSocialVirtualMarkup();
    bindSocialVirtualEvents();
    await loadSocialVirtual();
  };
})();
