    function setSocialWorkspace(workspace, { load = true } = {}) {
      const next = ['groups', 'expressions', 'memes'].includes(workspace) ? workspace : 'groups';
      state.socialWorkspace = next;
      document.querySelectorAll('[data-social-workspace]').forEach((button) => {
        const active = button.dataset.socialWorkspace === next;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
      document.querySelectorAll('[data-social-pane]').forEach((pane) => {
        pane.classList.toggle('hidden', pane.dataset.socialPane !== next);
      });
      state.collectionBrowsers.forEach((browser) => browser.apply?.());
      if (load && next === 'memes' && state.authenticated && !state.memeDiscoveryLoaded) {
        loadMemeDiscovery()
          .then(() => setConnection('表情库已更新。', 'ok'))
          .catch((error) => setConnection(error.message || String(error), 'error'));
      }
    }
    function setMemeDiscoveryState(result = {}) {
      state.memeDiscovery = {
        jobs: result.jobs || [],
        candidates: result.candidates || [],
        providers: result.providers || [],
        counts: result.counts || {},
      };
      state.memeDiscoveryLoaded = true;
    }
    function candidateStatusMeta(status) {
      return {
        pending: ['待审核', 'blue'],
        approved: ['已批准', 'green'],
        rejected: ['已拒绝', 'amber'],
        duplicate: ['重复', 'amber'],
        failed: ['获取失败', 'red'],
      }[status] || [status || '未知', ''];
    }
    function renderMemeDiscovery() {
      const data = state.memeDiscovery || {};
      const candidates = data.candidates || [];
      const counts = data.counts || {};
      const summary = [
        ['全部候选', candidates.length, ''],
        ['待审核', counts.pending || 0, 'blue'],
        ['已批准', counts.approved || 0, 'green'],
        ['重复', counts.duplicate || 0, ''],
        ['失败', counts.failed || 0, Number(counts.failed) ? 'red' : ''],
      ];
      $('memeDiscoverySummary').innerHTML = summary.map(([label, value, tone]) => (
        `<div class="summary-item"><span>${escapeHtml(label)}</span><strong class="${tone}">${escapeHtml(value)}</strong></div>`
      )).join('');

      const currentProvider = $('memeDiscoveryProvider').value || 'auto';
      if ((data.providers || []).length) {
        $('memeDiscoveryProvider').innerHTML = data.providers.map((item) => (
          `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name || item.id)}</option>`
        )).join('');
        $('memeDiscoveryProvider').value = (data.providers || []).some((item) => item.id === currentProvider) ? currentProvider : 'auto';
      }

      $('memeCandidateGrid').innerHTML = candidates.length ? candidates.map((item) => {
        const [statusLabel, tone] = candidateStatusMeta(item.status);
        const title = item.title || item.id;
        const description = item.description || '来源未提供描述。';
        const provider = (data.providers || []).find((entry) => entry.id === item.provider)?.name || item.provider;
        const reviewable = ['pending', 'rejected'].includes(item.status) && item.preview_url;
        const sourceLink = item.source_page_url
          ? `<a class="meme-source-link" href="${escapeHtml(item.source_page_url)}" target="_blank" rel="noopener noreferrer">查看“${escapeHtml(title)}”来源</a>`
          : '<span class="compact-note">来源页不可用</span>';
        const preview = item.preview_url
          ? `<img class="meme-candidate-preview" src="${escapeHtml(item.preview_url)}" alt="">`
          : `<div class="meme-candidate-preview meme-candidate-placeholder"><span>${item.status === 'duplicate' ? '与现有资产重复，未再次保存' : '没有可用预览'}</span></div>`;
        const review = reviewable ? `<details class="candidate-review-editor">
          <summary>编辑并审核</summary>
          <div class="candidate-review-form" data-candidate-form="${escapeHtml(item.id)}">
            <label>资产名称<input data-candidate-field="name" value="${escapeHtml(title)}" maxlength="120"></label>
            <label>情绪<select data-candidate-field="emotion">${['daily', 'happy', 'comfort', 'playful', 'curious', 'work'].map((emotion) => `<option value="${emotion}" ${emotion === item.emotion ? 'selected' : ''}>${{ daily: '日常', happy: '开心', comfort: '安慰', playful: '玩笑', curious: '好奇', work: '工作' }[emotion]}</option>`).join('')}</select></label>
            <label class="wide-field">描述<textarea data-candidate-field="description" rows="3" maxlength="500">${escapeHtml(description)}</textarea></label>
            <label class="wide-field">标签<input data-candidate-field="tags" value="${escapeHtml(item.tags || '')}" maxlength="600"></label>
            <label>分组<input data-candidate-field="pack" value="discovered-${escapeHtml(item.provider || 'auto')}" maxlength="80"></label>
            <label>冷却分钟<input data-candidate-field="cooldown_minutes" type="number" min="0" max="10080" value="90"></label>
            <label>每日最多发送<input data-candidate-field="max_daily" type="number" min="1" max="100" value="3"></label>
            <label class="checkbox-line"><input data-candidate-field="enabled" type="checkbox">批准后立即启用</label>
            <div class="candidate-review-actions">
              <button class="primary" type="button" data-candidate-review="approve" data-candidate-id="${escapeHtml(item.id)}" aria-label="批准${escapeHtml(title)}并入库">批准入库</button>
              <button class="danger" type="button" data-candidate-review="reject" data-candidate-id="${escapeHtml(item.id)}" aria-label="拒绝${escapeHtml(title)}候选">拒绝候选</button>
            </div>
          </div>
        </details>` : '';
        return `<article class="meme-candidate-card ${item.preview_url ? '' : 'no-preview'}" data-candidate-card="${escapeHtml(item.id)}" data-collection-status="${escapeHtml(item.status || '')}" tabindex="-1">
          ${preview}
          <div class="meme-candidate-copy">
            <div class="meme-candidate-heading"><h3>${escapeHtml(title)}</h3><span class="status-chip ${tone}">${escapeHtml(statusLabel)}</span></div>
            <p class="meme-candidate-description">${escapeHtml(description)}</p>
            <dl class="meme-candidate-facts">
              <div><dt>来源</dt><dd>${escapeHtml(provider || '-')}</dd></div>
              <div><dt>许可</dt><dd>${escapeHtml(item.license_name || '待复核')}</dd></div>
              <div><dt>描述</dt><dd>${item.description_method === 'source_metadata' ? '来源元数据整理' : escapeHtml(item.description_method || '未标注')}</dd></div>
              ${item.error ? `<div><dt>失败</dt><dd>${escapeHtml(item.error)}</dd></div>` : ''}
            </dl>
            ${sourceLink}
          </div>
          ${review}
        </article>`;
      }).join('') : '<div class="empty">尚未查找表情候选。</div>';

      $('memeDiscoveryJobs').innerHTML = (data.jobs || []).length ? data.jobs.slice(0, 8).map((job) => {
        const [statusLabel, tone] = candidateStatusMeta(job.status === 'succeeded' ? 'approved' : job.status === 'failed' ? 'failed' : job.status === 'empty' ? 'rejected' : 'pending');
        return `<div class="discovery-job-item"><strong>${escapeHtml(job.query)}</strong><span class="status-chip ${tone}">${escapeHtml(statusLabel)}</span><span class="job-facts">${escapeHtml(job.provider)} · 发现 ${escapeHtml(job.discovered_count || 0)} · 候选 ${escapeHtml(job.imported_count || 0)} · 重复 ${escapeHtml(job.duplicate_count || 0)} · 失败 ${escapeHtml(job.failed_count || 0)}</span></div>`;
      }).join('') : '<div class="empty">暂无发现记录。</div>';
    }
    async function loadMemeDiscovery() {
      const result = await bridge('/assistant/memes/discovery');
      setMemeDiscoveryState(result);
      renderMemeDiscovery();
      preferPendingCandidateFilter(result.counts);
    }
    function preferPendingCandidateFilter(counts = {}) {
      if (Number(counts.pending || 0) <= 0) return;
      const browser = state.collectionBrowsers.get('memeCandidateGrid');
      const filter = document.querySelector('[data-collection-owner="memeCandidateGrid"] .collection-filter select');
      if (!browser || !filter) return;
      browser.filter = 'pending';
      browser.page = 1;
      filter.value = 'pending';
      browser.apply();
    }
    async function searchMemeCandidates(event) {
      event.preventDefault();
      if (!$('memeDiscoveryForm').reportValidity()) return;
      const button = $('memeDiscoverySubmitBtn');
      button.disabled = true;
      button.textContent = '正在查找……';
      $('memeCandidateGrid').setAttribute('aria-busy', 'true');
      $('memeDiscoveryStatus').textContent = '正在从受限来源查找并校验候选图片。';
      try {
        const result = await bridge('/assistant/memes/discovery/search', {
          method: 'POST',
          body: JSON.stringify({
            query: $('memeDiscoveryQuery').value.trim(),
            provider: $('memeDiscoveryProvider').value,
            limit: Number($('memeDiscoveryLimit').value || 8),
          }),
        });
        setMemeDiscoveryState(result);
        renderMemeDiscovery();
        const job = result.job || {};
        if (Number(job.imported_count || 0) > 0) preferPendingCandidateFilter(result.counts);
        $('memeDiscoveryStatus').textContent = job.status === 'failed'
          ? `查找失败：${job.error || '来源暂时不可用'}。`
          : `查找完成：发现 ${job.discovered_count || 0} 项，新增 ${job.imported_count || 0} 个待审候选，跳过 ${job.duplicate_count || 0} 个重复项。`;
      } finally {
        button.disabled = false;
        button.textContent = '查找候选';
        $('memeCandidateGrid').setAttribute('aria-busy', 'false');
      }
    }
    function candidateReviewPayload(candidateId, decision) {
      const form = document.querySelector(`[data-candidate-form="${CSS.escape(candidateId)}"]`);
      const field = (name) => form?.querySelector(`[data-candidate-field="${name}"]`);
      return {
        candidate_id: candidateId,
        decision,
        name: field('name')?.value.trim() || '',
        description: field('description')?.value.trim() || '',
        emotion: field('emotion')?.value || 'daily',
        tags: field('tags')?.value.trim() || '',
        pack: field('pack')?.value.trim() || '',
        cooldown_minutes: Number(field('cooldown_minutes')?.value || 90),
        max_daily: Number(field('max_daily')?.value || 3),
        enabled: field('enabled')?.checked ? '1' : '0',
      };
    }
    async function reviewMemeCandidate(candidateId, decision, button) {
      const candidate = (state.memeDiscovery.candidates || []).find((item) => item.id === candidateId);
      if (!candidate) return;
      if (decision === 'reject' && !window.confirm(`拒绝候选“${candidate.title || candidateId}”？`)) return;
      button.disabled = true;
      $('memeDiscoveryStatus').textContent = decision === 'approve' ? '正在批准并写入表情资产库。' : '正在记录拒绝结果。';
      try {
        const result = await bridge('/assistant/memes/discovery/review', {
          method: 'POST',
          body: JSON.stringify(candidateReviewPayload(candidateId, decision)),
        });
        setMemeDiscoveryState(result);
        state.memeAssets = result.memes || state.memeAssets; state.memeHealth = result.health || state.memeHealth;
        renderMemeDiscovery();
        renderSocialExperience();
        $('memeDiscoveryStatus').textContent = decision === 'approve'
          ? `“${candidate.title || candidateId}”已批准入库。`
          : `“${candidate.title || candidateId}”已拒绝。`;
        // The activated control disappears after review. Keep the user's
        // filter, then move focus to a visible continuation point instead of
        // dropping it on body. The reviewed card is preferred when visible;
        // a pending-only filter naturally moves to the next result.
        // WCAG 2.2 - 2.4.3 Focus Order.
        requestAnimationFrame(() => {
          const updated = document.querySelector(`[data-candidate-card="${CSS.escape(candidateId)}"]`);
          const nextVisible = document.querySelector('#memeCandidateGrid .meme-candidate-card:not([hidden])');
          if (updated && !updated.hidden) updated.focus();
          else if (nextVisible) nextVisible.focus();
          else $('memeDiscoveryStatus').focus();
        });
      } finally {
        button.disabled = false;
      }
    }

    function qqGroupAccessStatus(groupId) {
      const access = state.qqGroupAccess;
      if (!access) return { known: false, allowed: false, label: '状态未加载', reason: '正在读取 QQ 群准入配置。' };
      const settings = access.settings || {};
      const allowlisted = (access.group_allowlist || []).some((item) => (
        item.enabled !== false && String(item.group_id || '') === String(groupId || '').trim()
      ));
      const runtimeApplied = access.runtime?.state === 'applied';
      if (!access.feature_enabled) return { known: true, allowed: false, label: 'Gate 未启用', reason: 'QQ 访问控制 Gate 尚未启用。' };
      if (!settings.channel_enabled) return { known: true, allowed: false, label: '渠道已关闭', reason: 'QQ 渠道总开关当前关闭。' };
      if (!settings.group_chat_enabled) return { known: true, allowed: false, label: '群聊已关闭', reason: 'QQ 群聊总开关当前关闭。' };
      if (!allowlisted) return { known: true, allowed: false, label: '未加入白名单', reason: '该群尚未加入 QQ 群白名单。' };
      if (!runtimeApplied) return { known: true, allowed: false, label: '等待插件应用', reason: '配置已保存，但插件尚未确认应用当前版本。' };
      return { known: true, allowed: true, label: '已准入', reason: '该群已通过 QQ 渠道准入，可继续设置回复策略。' };
    }

    function renderGroupPolicyAccessStatus() {
      const target = $('groupPolicyAccessStatus');
      if (!target) return;
      const groupId = $('groupPolicyId').value.trim();
      if (!groupId) {
        target.textContent = state.qqGroupAccess
          ? '输入或选择群号后，将显示该群的渠道准入状态。'
          : '正在读取 QQ 群准入配置。';
        target.className = 'provider-status';
        return;
      }
      const access = qqGroupAccessStatus(groupId);
      target.textContent = `渠道准入：${access.label}。${access.reason}`;
      target.className = `provider-status ${access.allowed ? 'ok' : access.known ? 'error' : ''}`;
    }

    async function loadQqGroupAccess() {
      try {
        state.qqGroupAccess = await bridge('/qq/settings');
      } catch (error) {
        state.qqGroupAccess = null;
        console.warn('QQ group access state unavailable', error);
      }
      renderSocialExperience();
      renderGroupPolicyAccessStatus();
    }

    function renderSocialExperience() {
      const enabledGroups = state.groupPolicies.filter((item) => Number(item.enabled)).length;
      const allowedGroups = state.groupPolicies.filter((item) => qqGroupAccessStatus(item.group_id).allowed).length;
      const enabledHabits = state.expressionHabits.filter((item) => Number(item.enabled)).length;
      const approvedMemes = state.memeAssets.filter((item) => Number(item.enabled) && item.review_status === 'approved').length;
      const items = [
        ['群聊', `${enabledGroups} 策略 · ${allowedGroups} 准入`, allowedGroups ? 'green' : enabledGroups ? 'red' : ''],
        ['自然参与保护', state.naturalGroupParticipation?.feature_enabled ? '已启用' : '待启用', state.naturalGroupParticipation?.feature_enabled ? 'green' : 'amber'],
        ['表达习惯', `${enabledHabits}/${state.expressionHabits.length}`, enabledHabits ? 'blue' : ''],
        ['可发送表情', approvedMemes, approvedMemes ? 'green' : 'red'],
      ];
      $('socialExperienceSummary').innerHTML = items.map(([label, value, tone]) => (
        `<div class="summary-item"><span>${escapeHtml(label)}</span><strong class="${tone}">${escapeHtml(value)}</strong></div>`
      )).join('');
      const health = state.memeHealth || {};
      const healthTone = health.state === 'ready' ? 'green' : health.state === 'blocked' ? 'red' : 'amber';
      const healthLabel = { ready: '可用', degraded: '覆盖不足', blocked: '不可发送' }[health.state] || '未读取';
      const healthItems = [['状态', healthLabel, healthTone], ['可发送', `${health.available_files || 0}/${health.enabled_approved || 0}`, health.available_files ? 'green' : 'red'], ['待审核', health.pending_review || 0, health.pending_review ? 'amber' : ''], ['文件失效', health.missing_files || 0, health.missing_files ? 'red' : 'green']];
      const healthSummary = $('memePoolHealthSummary');
      if (healthSummary) healthSummary.innerHTML = healthItems.map(([label, value, tone]) => `<div class="summary-item"><span>${escapeHtml(label)}</span><strong class="${tone}">${escapeHtml(value)}</strong></div>`).join('');
      const healthReason = $('memePoolHealthReason');
      if (healthReason) {
        const labels = { daily: '日常', happy: '开心', comfort: '安慰', playful: '玩笑', curious: '好奇', work: '工作' };
        const gaps = (health.coverage_gaps || []).map((item) => labels[item] || item);
        healthReason.textContent = gaps.length ? `当前缺少：${gaps.join('、')}。候选池过小时，防重复、冷却和每日上限可能同时清空可发送结果。` : '六类基础场景均有可发送资产；选择失败时请查看最近选择原因。';
      }
      ensureGroupNaturalGuardFields();
      $('groupNaturalGuardEnabled').checked = Boolean(state.naturalGroupParticipation?.feature_enabled);

      $('groupPolicyRows').innerHTML = state.groupPolicies.length ? state.groupPolicies.map((item) => {
        const mode = item.participation_mode || (!Number(item.enabled) ? 'disabled' : Number(item.mention_only) ? 'mentions_only' : Number(item.active_reply) ? 'natural_participation' : 'directed_context');
        const strategy = { disabled: '关闭', mentions_only: '仅明确 @', directed_context: '@、回复或受控续接', natural_participation: `自然参与（强度 ${Number(item.reply_probability || 0).toFixed(2)}）` }[mode] || mode;
        const access = qqGroupAccessStatus(item.group_id);
        return `<tr>
          <td><strong>${escapeHtml(item.group_name || item.group_id)}</strong><br><span class="mono">${escapeHtml(item.group_id)}</span></td>
          <td><span class="badge ${access.allowed ? 'green' : 'red'}">${escapeHtml(access.label)}</span></td>
          <td><span class="badge ${Number(item.enabled) ? 'blue' : 'amber'}">${Number(item.enabled) ? '启用' : '关闭'}</span></td>
          <td>${escapeHtml(strategy)}</td>
          <td>${escapeHtml(item.cooldown_seconds || 180)}s</td>
          <td>${Number(item.allow_work) ? `<span class="badge amber">${escapeHtml(item.allowed_work_senders || '未填授权人')}</span>` : '关闭'}</td>
          <td>${escapeHtml(item.message_count || 0)} / ${escapeHtml(item.reply_count || 0)}</td>
          <td class="mono">${escapeHtml(item.last_reply_at || '-')}</td>
          <td><button class="secondary" type="button" data-group-edit="${escapeHtml(item.group_id)}">编辑</button></td>
        </tr>`;
      }).join('') : '<tr><td colspan="9" class="empty">暂无群记录；机器人收到已准入群的消息后会以关闭状态自动登记。</td></tr>';

      $('expressionRows').innerHTML = state.expressionHabits.length ? state.expressionHabits.map((item) => `<tr>
        <td>${escapeHtml(item.situation || '')}</td>
        <td>${escapeHtml(item.cues || '')}</td>
        <td class="quality-cell">${escapeHtml(item.style || '')}</td>
        <td><span class="badge">${escapeHtml(item.scope || '')}</span></td>
        <td>${item.subject_type === 'private_user' ? `私聊 · ${escapeHtml(item.subject_id || '')}` : item.subject_type === 'qq_group' ? `群聊 · ${escapeHtml(item.subject_id || '')}` : '全局默认'}</td>
        <td>${item.origin === 'user_feedback' ? '<span class="badge green">用户明确纠正</span>' : item.origin === 'system' ? '系统基线' : '管理员配置'}</td>
        <td>${escapeHtml(item.priority || 0)}</td>
        <td>${escapeHtml(item.use_count || 0)}</td>
        <td><span class="badge ${Number(item.enabled) ? 'blue' : 'amber'}">${Number(item.enabled) ? '启用' : '停用'}</span></td>
        <td><div class="actions"><button class="secondary" type="button" data-expression-edit="${escapeHtml(item.id)}">编辑</button><button class="secondary" type="button" data-expression-toggle="${escapeHtml(item.id)}">${Number(item.enabled) ? '停用' : '启用'}</button></div></td>
      </tr>`).join('') : '<tr><td colspan="10" class="empty">暂无表达习惯。</td></tr>';

      $('socialMemeGrid').innerHTML = state.memeAssets.length ? state.memeAssets.map((item) => {
        const localPreview = String(item.public_url || '').startsWith('/memes/assets/');
        const delivery = item.delivery || {};
        return `<div class="asset-item" data-collection-status="${escapeHtml(item.review_status || 'pending')}">
          ${localPreview ? `<img class="asset-thumb" src="${escapeHtml(item.public_url)}" alt="${escapeHtml(item.name || '表情包')}">` : '<div class="asset-thumb"></div>'}
          <div class="asset-copy">
            <strong>${escapeHtml(item.name || item.id)}</strong>
            ${item.description ? `<span>${escapeHtml(item.description)}</span>` : ''}
            <span>${escapeHtml(item.pack || 'default')} · ${escapeHtml(item.emotion || 'daily')} · ${escapeHtml(item.review_status || 'pending')}</span>
            <span>已发 ${escapeHtml(delivery.sent_count || item.usage_count || 0)} · 冷却 ${escapeHtml(item.cooldown_minutes || 0)} 分钟 · 每日 ${escapeHtml(item.max_daily || 0)}</span>
            <div class="actions"><span class="badge ${Number(item.enabled) ? 'blue' : 'amber'}">${Number(item.enabled) ? '启用' : '停用'}</span><button class="secondary" type="button" data-social-meme-toggle="${escapeHtml(item.id)}" ${item.review_status !== 'approved' ? 'disabled' : ''}>${Number(item.enabled) ? '停用' : '启用'}</button></div>
          </div>
        </div>`;
      }).join('') : '<div class="empty">暂无表情资产。</div>';
    }

    async function loadSocialExperience() {
      ensureGroupNaturalGuardFields();
      const [groups, expressions, memes, settings] = await Promise.all([
        bridge('/assistant/groups'),
        bridge('/assistant/expressions'),
        bridge('/assistant/memes'),
        bridge('/assistant/settings'),
      ]);
      state.groupPolicies = groups.groups || [];
      state.naturalGroupParticipation = groups.natural_participation || {};
      state.expressionHabits = expressions.habits || [];
      state.memeAssets = memes.memes || []; state.memeHealth = memes.health || null;
      renderAssistantSettings(settings.settings || {});
      renderSocialExperience();
      renderGroupPolicyAccessStatus();
      void loadQqGroupAccess();
      setSocialWorkspace(state.socialWorkspace, { load: false });
    }

    function ensureGroupNaturalGuardFields() {
      const target = $('groupNaturalGuardFields');
      if (!target || target.children.length) return;
      target.innerHTML = '<legend>自然参与与自动续聊 Gate</legend><label>突发窗口（秒）<input id="groupBurstWindowSeconds" type="number" min="5" max="300" value="12"></label><label>窗口消息上限<input id="groupBurstMaxMessages" type="number" min="2" max="30" value="6"></label><label>每日主动回复上限<input id="groupDailyReplyBudget" type="number" min="0" max="200" value="20"></label><label>安静间隔（秒）<input id="groupQuietGapSeconds" type="number" min="0" max="120" value="8" aria-describedby="groupContinuationHelp"></label><label>无 @ 续聊窗口（秒）<input id="groupContinuationWindowSeconds" type="number" min="15" max="600" value="120" aria-describedby="groupContinuationHelp"></label><label>无 @ 连续接话上限<input id="groupMaxAutoContinuations" type="number" min="1" max="3" value="2" aria-describedby="groupContinuationHelp"></label><span id="groupContinuationHelp" class="compact-note">普通群聊、无 @ 续聊都先过这里；明确 @ 与引用回复仍按直接对话优先处理。</span><label class="checkbox-line"><input id="groupNaturalGuardEnabled" type="checkbox">启用全局自然参与</label><button class="secondary" type="button" data-group-natural-cutover>应用总开关</button>';
    }

    function clearGroupPolicyForm() {
      ensureGroupNaturalGuardFields();
      $('groupPolicyId').value = ''; $('groupPolicyName').value = '';
      $('groupReplyProbability').value = '0.2'; $('groupCooldownSeconds').value = '180'; $('groupMaxContext').value = '40';
      $('groupBurstWindowSeconds').value = '12'; $('groupBurstMaxMessages').value = '6';
      $('groupDailyReplyBudget').value = '20'; $('groupQuietGapSeconds').value = '8';
      $('groupContinuationWindowSeconds').value = '120'; $('groupMaxAutoContinuations').value = '2';
      $('groupQuietStart').value = '23:30'; $('groupQuietEnd').value = '08:30';
      $('groupTimezone').value = 'Asia/Shanghai';
      $('groupWorkSenders').value = '';
      $('groupParticipationMode').value = 'mentions_only';
      $('groupAllowWork').checked = false;
      $('groupMemeEnabled').checked = false;
      renderGroupPolicyAccessStatus();
    }

    function editGroupPolicy(id) {
      ensureGroupNaturalGuardFields();
      const item = state.groupPolicies.find((entry) => String(entry.group_id) === String(id));
      if (!item) return;
      $('groupPolicyId').value = item.group_id || '';
      $('groupPolicyName').value = item.group_name || '';
      $('groupReplyProbability').value = item.reply_probability ?? 0.2;
      $('groupCooldownSeconds').value = item.cooldown_seconds || 180;
      $('groupMaxContext').value = item.max_context ?? 40;
      $('groupBurstWindowSeconds').value = item.burst_window_seconds || 12;
      $('groupBurstMaxMessages').value = item.burst_max_messages || 6;
      $('groupDailyReplyBudget').value = item.daily_reply_budget ?? 20;
      $('groupQuietGapSeconds').value = item.quiet_gap_seconds ?? 8;
      $('groupContinuationWindowSeconds').value = item.continuation_window_seconds ?? 120;
      $('groupMaxAutoContinuations').value = item.max_auto_continuations ?? 2;
      $('groupQuietStart').value = item.quiet_start || '23:30';
      $('groupQuietEnd').value = item.quiet_end || '08:30';
      $('groupTimezone').value = item.timezone || 'Asia/Shanghai';
      $('groupWorkSenders').value = item.allowed_work_senders || '';
      $('groupParticipationMode').value = item.participation_mode || (!Number(item.enabled) ? 'disabled' : Number(item.mention_only) ? 'mentions_only' : Number(item.active_reply) ? 'natural_participation' : 'directed_context');
      $('groupAllowWork').checked = Boolean(Number(item.allow_work));
      $('groupMemeEnabled').checked = Boolean(Number(item.meme_enabled));
      renderGroupPolicyAccessStatus();
    }

    async function saveGroupPolicy() {
      ensureGroupNaturalGuardFields();
      if (!state.qqGroupAccess) await loadQqGroupAccess();
      const groupId = $('groupPolicyId').value.trim();
      const access = qqGroupAccessStatus(groupId);
      if ($('groupParticipationMode').value !== 'disabled' && !access.allowed) {
        renderGroupPolicyAccessStatus();
        $('openQqAccessFromGroupBtn').focus();
        throw new Error(`不能启用群回复策略：${access.reason}`);
      }
      const payload = {
        group_id: groupId,
        group_name: $('groupPolicyName').value.trim(),
        reply_probability: Number($('groupReplyProbability').value || 0.2),
        cooldown_seconds: Number($('groupCooldownSeconds').value || 180),
        max_context: Number($('groupMaxContext').value || 40),
        burst_window_seconds: Number($('groupBurstWindowSeconds').value || 12),
        burst_max_messages: Number($('groupBurstMaxMessages').value || 6),
        daily_reply_budget: Number($('groupDailyReplyBudget').value || 0),
        quiet_gap_seconds: Number($('groupQuietGapSeconds').value || 0),
        continuation_window_seconds: Number($('groupContinuationWindowSeconds').value || 120),
        max_auto_continuations: Number($('groupMaxAutoContinuations').value || 2),
        quiet_start: $('groupQuietStart').value || '23:30',
        quiet_end: $('groupQuietEnd').value || '08:30',
        timezone: $('groupTimezone').value.trim() || 'Asia/Shanghai',
        allowed_work_senders: $('groupWorkSenders').value.trim(),
        participation_mode: $('groupParticipationMode').value,
        allow_work: $('groupAllowWork').checked ? '1' : '0',
        meme_enabled: $('groupMemeEnabled').checked ? '1' : '0',
      };
      const result = await bridge('/assistant/groups', { method: 'POST', body: JSON.stringify(payload) });
      state.groupPolicies = result.groups || [];
      state.naturalGroupParticipation = result.natural_participation || state.naturalGroupParticipation || {};
      renderSocialExperience();
      renderGroupPolicyAccessStatus();
      setConnection('群聊策略已保存。', 'ok');
    }

    async function setNaturalGroupParticipation() {
      ensureGroupNaturalGuardFields();
      let plan = state.naturalGroupParticipation || {};
      if (!plan.plan_checksum) {
        plan = await bridge('/assistant/groups/cutover');
      }
      const result = await bridge('/assistant/groups/cutover', {
        method: 'POST',
        body: JSON.stringify({
          enabled: $('groupNaturalGuardEnabled').checked,
          plan_checksum: plan.plan_checksum,
        }),
      });
      state.naturalGroupParticipation = result;
      renderSocialExperience();
      setConnection(result.feature_enabled ? '自然群参与已启用。' : '自然群参与已关闭。', 'ok');
    }

    function editExpression(id) {
      const item = state.expressionHabits.find((entry) => entry.id === id);
      if (!item) return;
      $('expressionSituation').dataset.id = item.id || '';
      $('expressionSituation').value = item.situation || '';
      $('expressionCues').value = item.cues || '';
      $('expressionStyle').value = item.style || '';
      $('expressionScope').value = item.scope || 'daily';
      $('expressionSubjectType').value = item.subject_type || 'global';
      $('expressionSubjectId').value = item.subject_id || '';
      $('expressionPriority').value = item.priority || 5;
      $('expressionEnabled').checked = Boolean(Number(item.enabled));
    }

    async function saveExpression(overrides = {}) {
      const payload = Object.assign({
        id: $('expressionSituation').dataset.id || '',
        situation: $('expressionSituation').value.trim(),
        cues: $('expressionCues').value.trim(),
        style: $('expressionStyle').value.trim(),
        scope: $('expressionScope').value,
        subject_type: $('expressionSubjectType').value,
        subject_id: $('expressionSubjectId').value.trim(),
        priority: Number($('expressionPriority').value || 5),
        enabled: $('expressionEnabled').checked ? '1' : '0',
      }, overrides);
      const result = await bridge('/assistant/expressions', { method: 'POST', body: JSON.stringify(payload) });
      state.expressionHabits = result.habits || [];
      renderSocialExperience();
      setConnection('表达习惯已保存。', 'ok');
    }

    async function toggleExpression(id) {
      const item = state.expressionHabits.find((entry) => entry.id === id);
      if (!item) return;
      const result = await bridge('/assistant/expressions', {
        method: 'POST',
        body: JSON.stringify(Object.assign({}, item, { enabled: Number(item.enabled) ? '0' : '1' })),
      });
      state.expressionHabits = result.habits || [];
      renderSocialExperience();
    }

    function fileAsDataUrl(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ''));
        reader.onerror = () => reject(reader.error || new Error('图片读取失败。'));
        reader.readAsDataURL(file);
      });
    }

    async function uploadMeme() {
      const file = $('memeUploadFile').files[0];
      if (!file) throw new Error('请选择图片。');
      $('uploadMemeBtn').disabled = true;
      $('memeUploadStatus').textContent = '正在上传并校验图片。';
      try {
        const data = await fileAsDataUrl(file);
        const result = await bridge('/assistant/memes/upload', {
          method: 'POST',
          body: JSON.stringify({
            data_base64: data,
            name: $('memeUploadName').value.trim() || file.name,
            emotion: $('memeUploadEmotion').value,
            pack: $('memeUploadPack').value.trim() || 'custom',
            tags: $('memeUploadTags').value.trim(),
            cooldown_minutes: Number($('memeCooldownMinutes').value || 60),
            max_daily: Number($('memeMaxDaily').value || 3),
            source: $('memeUploadSource').value.trim() || 'admin-upload',
            license_note: $('memeLicenseNote').value.trim(),
            review_status: 'approved',
            enabled: $('memeUploadEnabled').checked ? '1' : '0',
          }),
        });
        state.memeAssets = result.memes || []; state.memeHealth = result.health || state.memeHealth;
        renderSocialExperience();
        $('memeUploadFile').value = '';
        $('memeUploadStatus').textContent = '图片已入库。';
        setConnection('表情包已上传并入库。', 'ok');
      } finally {
        $('uploadMemeBtn').disabled = false;
      }
    }

    async function toggleSocialMeme(id) {
      const item = state.memeAssets.find((entry) => entry.id === id);
      if (!item) return;
      const result = await bridge('/assistant/memes', {
        method: 'POST',
        body: JSON.stringify(Object.assign({}, item, { enabled: Number(item.enabled) ? '0' : '1' })),
      });
      state.memeAssets = result.memes || []; state.memeHealth = result.health || state.memeHealth;
      renderSocialExperience();
    }

    const capabilityCopy = {
      'chat.reply': ['即时对话回复', '不调用工具，直接生成普通对话回复。'],
      'codex.sandbox': ['Codex 沙箱执行', '在明确的只读或可写沙箱中完成代码、文件与服务器工作。'],
      'platform.health.read': ['平台健康读取', '读取助手控制面的服务健康摘要。'],
      'task.status.read': ['运行状态读取', '读取已有 Goal Run 或兼容 Task 的状态。'],
      'clock.current.read': ['当前时间读取', '读取一个受支持时区的当前时间。'],
      'weather.forecast.read': ['普通天气预报', '通过 Open-Meteo 读取普通天气与短期预报；灾害预警不走此路径。'],
      'github.trending.read': ['GitHub 趋势读取', '通过受限适配器读取 GitHub Trending 快照。'],
      'meme.discovery.search': ['表情候选发现', '从受限来源发现并隔离表情候选；人工审核后才进入可发送资产库。'],
      'delivery.qq.send': ['QQ 结果投递', '将已生成的运行结果经 QQ 渠道适配器可靠投递。'],
    };

    function setCapabilityWorkspace(workspace, { focus = false } = {}) {
      const next = ['market', 'installed', 'contracts', 'network', 'skills'].includes(workspace) ? workspace : 'market';
      state.capabilityWorkspace = next;
      document.querySelectorAll('[data-capability-workspace]').forEach((button) => {
        const active = button.dataset.capabilityWorkspace === next;
        button.classList.toggle('active', active);
        button.setAttribute('aria-selected', active ? 'true' : 'false');
        button.tabIndex = active ? 0 : -1;
      });
      document.querySelectorAll('[data-capability-pane]').forEach((pane) => {
        pane.classList.toggle('hidden', pane.dataset.capabilityPane !== next);
      });
      $('skillNewBtn').classList.toggle('hidden', next !== 'skills');
      if (next === 'network' && !state.networkPolicyEvents.length) {
        window.loadNetworkPolicy?.().catch((error) => setConnection(error.message || String(error), 'error'));
      }
      state.collectionBrowsers.forEach((browser) => browser.apply?.());
      if (focus) {
        const pane = document.querySelector(`[data-capability-pane="${CSS.escape(next)}"]`);
        pane?.querySelector('h2')?.focus?.();
      }
    }

    function pluginOperationLabel(action) {
      return { install: '安装', update: '更新', uninstall: '卸载' }[action] || action;
    }

    function renderPluginMarketplace() {
      const meta = state.pluginMarketMeta || {};
      const counts = meta.counts || {};
      const source = state.pluginMarketSources.find((item) => Number(item.enabled)) || {};
      setText('pluginMarketSource', source.name || 'AstrBot 官方市场');
      setText(
        'pluginMarketStatus',
        meta.ok === false
          ? '市场暂时不可用，已安装插件不受影响'
          : `${counts.available ?? state.pluginMarketplace.length} 个插件${meta.stale ? ' · 正在使用缓存' : ' · 目录已同步'}`,
      );
      $('pluginMarketSummary').innerHTML = [
        ['可发现', counts.available ?? state.pluginMarketplace.length],
        ['已安装', counts.installed ?? state.pluginMarketplace.filter((item) => item.installed).length],
        ['可更新', counts.updates ?? state.pluginMarketplace.filter((item) => item.update_available).length],
        ['运行边界', 'AstrBot'],
      ].map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join('');

      const query = String(state.pluginMarketQuery || '').normalize('NFKC').toLocaleLowerCase('zh-CN').trim();
      const filteredPlugins = state.pluginMarketplace.filter((item) => {
        const categoryMatches = !state.pluginMarketCategory || item.category === state.pluginMarketCategory;
        const haystack = [item.display_name, item.id, item.author, item.description, ...(item.tags || [])].join(' ').normalize('NFKC').toLocaleLowerCase('zh-CN');
        return categoryMatches && (!query || haystack.includes(query));
      });
      const pages = Math.max(1, Math.ceil(filteredPlugins.length / state.pluginMarketPageSize));
      state.pluginMarketPage = Math.min(Math.max(1, state.pluginMarketPage), pages);
      const start = (state.pluginMarketPage - 1) * state.pluginMarketPageSize;
      const visiblePlugins = filteredPlugins.slice(start, start + state.pluginMarketPageSize);
      setText('pluginMarketCount', query || state.pluginMarketCategory ? `${filteredPlugins.length} / ${state.pluginMarketplace.length} 条` : `共 ${state.pluginMarketplace.length} 条`);
      setText('pluginMarketPageIndicator', `${state.pluginMarketPage} / ${pages} 页`);
      $('pluginMarketPreviousBtn').disabled = state.pluginMarketPage <= 1 || filteredPlugins.length === 0;
      $('pluginMarketNextBtn').disabled = state.pluginMarketPage >= pages || filteredPlugins.length === 0;
      $('pluginMarketNoResults').hidden = filteredPlugins.length > 0 || state.pluginMarketplace.length === 0;
      $('pluginMarketGrid').innerHTML = visiblePlugins.length ? visiblePlugins.map((item) => {
        const status = item.update_available ? ['可更新', 'amber'] : item.installed ? ['已安装', 'green'] : ['可安装', 'blue'];
        const tags = (item.tags || []).slice(0, 3).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join('');
        return `<article class="market-card" data-collection-category="${escapeHtml(item.category || '其他')}">
          <header><div><span class="market-runtime">${escapeHtml(item.runtime_label || 'AstrBot 兼容')}</span><h3>${escapeHtml(item.display_name || item.id)}</h3></div><span class="status-chip ${status[1]}">${status[0]}</span></header>
          <p>${escapeHtml(item.description || '该插件没有提供说明。')}</p>
          <div class="tag-row"><span class="tag">${escapeHtml(item.category || '其他')}</span>${tags}</div>
          <dl><div><dt>作者</dt><dd>${escapeHtml(item.author || '未标注')}</dd></div><div><dt>版本</dt><dd>${escapeHtml(item.version || '未标注')}</dd></div></dl>
          <footer><span class="market-risk">安装前检查权限</span><button class="secondary" type="button" data-market-detail="${escapeHtml(item.id)}">查看详情</button></footer>
        </article>`;
      }).join('') : `<div class="empty-state">${meta.ok === false ? '市场目录暂时不可用，请稍后同步。' : state.pluginMarketplace.length ? '没有匹配的插件。' : '市场中暂无可展示插件。'}</div>`;

      $('pluginMarketOperations').innerHTML = state.pluginMarketOperations.length ? state.pluginMarketOperations.map((item) => {
        const tone = item.status === 'succeeded' ? 'green' : item.status === 'running' ? 'blue' : item.status === 'rolled_back' ? 'amber' : 'red';
        const status = { succeeded: '成功', running: '进行中', failed: '失败', rolled_back: '已回滚' }[item.status] || item.status;
        return `<article class="entity-card compact-card"><header><div><span class="entity-type">${escapeHtml(pluginOperationLabel(item.action))}</span><h3>${escapeHtml(item.plugin_name || item.plugin_id)}</h3></div><span class="status-chip ${tone}">${escapeHtml(status)}</span></header><p class="entity-description">${escapeHtml(item.message || '无附加信息')}</p><footer class="entity-actions"><span class="mono">${escapeHtml(item.started_at || '')}</span></footer></article>`;
      }).join('') : '<div class="empty-state">暂无安装活动。</div>';

      if (state.selectedMarketPlugin) renderPluginMarketDetail(state.selectedMarketPlugin, { focus: false });
    }

    function renderPluginMarketDetail(pluginId, { focus = true } = {}) {
      const item = state.pluginMarketplace.find((entry) => entry.id === pluginId);
      if (!item) {
        state.selectedMarketPlugin = '';
        $('pluginMarketDetail').classList.add('hidden');
        return;
      }
      state.selectedMarketPlugin = pluginId;
      $('pluginMarketDetailTitle').textContent = item.display_name || item.id;
      const permissions = (item.permissions || []).map((permission) => `<li>${escapeHtml(permission)}</li>`).join('');
      const action = item.update_available ? 'update' : item.installed ? '' : 'install';
      $('pluginMarketDetailBody').innerHTML = `<div class="market-detail-grid">
        <div><p class="market-detail-description">${escapeHtml(item.description || '该插件没有提供说明。')}</p><dl class="entity-facts"><div><dt>插件 ID</dt><dd class="mono">${escapeHtml(item.id)}</dd></div><div><dt>作者</dt><dd>${escapeHtml(item.author || '未标注')}</dd></div><div><dt>兼容性</dt><dd>${escapeHtml(item.compatibility || '由 AstrBot 安装器校验')}</dd></div><div><dt>来源</dt><dd>${escapeHtml(item.source_label || 'AstrBot 官方市场')}</dd></div></dl>${item.repo ? `<a class="text-link" href="${escapeHtml(item.repo)}" target="_blank" rel="noopener noreferrer">查看源代码仓库</a>` : ''}</div>
        <div class="market-consent"><span class="market-runtime">${escapeHtml(item.runtime_label || 'AstrBot 兼容')}</span><h3>安装会授予什么</h3><ul>${permissions}</ul><p>这是社区第三方代码。平台不能从目录元数据证明它只使用上述资源；安装后 AstrBot 会重启，QQ 会短暂重连。</p>${action ? `<button class="primary" type="button" data-market-action="${action}" data-market-plugin="${escapeHtml(item.id)}">${action === 'update' ? '备份并更新' : '确认风险并安装'}</button>` : '<button class="secondary" type="button" disabled>已是最新版本</button>'}${item.installed && !item.protected ? `<button class="danger" type="button" data-market-action="uninstall" data-market-plugin="${escapeHtml(item.id)}">备份并卸载</button>` : ''}</div>
      </div>`;
      $('pluginMarketDetail').classList.remove('hidden');
      if (focus) $('pluginMarketDetail').focus();
    }

    async function loadPluginMarketplace({ force = false } = {}) {
      try {
        const result = await bridge(`/capabilities/marketplace${force ? '?force_refresh=1' : ''}`);
        state.pluginMarketplace = result.plugins || [];
        state.pluginMarketSources = result.sources || [];
        state.pluginMarketOperations = result.operations || [];
        state.pluginMarketMeta = result;
      } catch (error) {
        state.pluginMarketMeta = { ok: false, error: error.message || String(error) };
      }
      renderPluginMarketplace();
    }

    async function operateMarketPlugin(action, pluginId, button) {
      const item = state.pluginMarketplace.find((entry) => entry.id === pluginId);
      if (!item) return;
      const verb = pluginOperationLabel(action);
      const warning = action === 'uninstall'
        ? `确认卸载 ${item.display_name || pluginId}？系统会先备份插件代码，再重启 AstrBot。插件数据不会在这里删除。`
        : `确认${verb} ${item.display_name || pluginId}？这是第三方代码，将在 AstrBot 容器内运行，并会让 QQ 短暂重连。`;
      if (!window.confirm(warning)) return;
      const original = button?.textContent || verb;
      if (button) { button.disabled = true; button.textContent = `${verb}中…`; }
      setText('pluginMarketStatus', `${item.display_name || pluginId} ${verb}中，请勿重复操作…`);
      try {
        const result = await bridge('/capabilities/marketplace/operate', { method: 'POST', headers: { 'Idempotency-Key': `plugin-market-${action}-${pluginId}-${crypto.randomUUID()}` }, body: JSON.stringify({ action, plugin_id: pluginId, confirm_risk: true }) });
        state.capabilityPlugins = result.plugins || state.capabilityPlugins;
        state.pluginMarketOperations = result.operations || state.pluginMarketOperations;
        state.selectedMarketPlugin = '';
        $('pluginMarketDetail').classList.add('hidden');
        await loadPluginMarketplace({ force: true });
        renderCapabilities();
        setConnection(`${item.display_name || pluginId} ${verb}完成，AstrBot 已重启。`, 'ok');
      } catch (error) {
        await loadPluginMarketplace();
        setConnection(`${verb}失败：${error.message || String(error)}`, 'error');
      } finally {
        if (button && button.isConnected) { button.disabled = false; button.textContent = original; }
      }
    }

    function renderCapabilities() {
      const healthyPlugins = state.capabilityPlugins.filter((item) => item.healthy).length;
      const enabledSkills = state.capabilitySkills.filter((item) => Number(item.enabled)).length;
      $('capabilitySummary').innerHTML = [
        ['可执行能力', state.capabilityManifests.length, '代码注册的稳定契约', state.capabilityManifests.length ? 'blue' : 'red'],
        ['健康插件', `${healthyPlugins}/${state.capabilityPlugins.length}`, 'AstrBot 扩展运行状态', healthyPlugins === state.capabilityPlugins.length ? 'green' : 'amber'],
        ['启用 Skill', `${enabledSkills}/${state.capabilitySkills.length}`, '方法、约束与上下文', enabledSkills ? 'blue' : 'red'],
        ['Skill 使用', state.capabilitySkills.reduce((sum, item) => sum + Number(item.use_count || 0), 0), '仅代表被选择注入', 'blue'],
      ].map(([label, value, detail, tone], index) => `<article class="insight-card ${tone}"><span class="insight-index">0${index + 1}</span><p>${escapeHtml(label)}</p><strong>${escapeHtml(value)}</strong><small>${escapeHtml(detail)}</small></article>`).join('');
      $('capabilityManifestGrid').innerHTML = state.capabilityManifests.length ? state.capabilityManifests.map((item) => {
        const healthStatus = item.health?.status || (item.health?.ok === false ? 'unhealthy' : 'unknown');
        const healthy = item.enabled !== false && !['unhealthy', 'disabled'].includes(healthStatus);
        const permissions = Array.isArray(item.permissions) ? item.permissions : [];
        const sideEffects = Array.isArray(item.side_effects) ? item.side_effects : item.side_effects ? [item.side_effects] : [];
        const copy = capabilityCopy[item.id] || [item.label || item.id, item.description || ''];
        return `<article class="entity-card capability-card">
          <header><div><span class="entity-type">${escapeHtml(item.category || 'capability')}</span><h3>${escapeHtml(copy[0])}</h3></div><span class="status-chip ${healthy ? healthStatus === 'unknown' ? 'amber' : 'green' : 'red'}">${escapeHtml(healthStatus === 'healthy' ? '健康' : healthStatus === 'unknown' ? '未检查' : healthStatus)}</span></header>
          <p class="entity-description">${escapeHtml(copy[1])}</p>
          <p class="entity-note mono">${escapeHtml(item.id)}</p>
          <div class="tag-row"><span class="tag">${escapeHtml(item.risk_level || 'low')} risk</span><span class="tag">${escapeHtml(item.cost_class || 'local')} cost</span><span class="tag">${sideEffects.length ? `副作用：${escapeHtml(sideEffects.join(', '))}` : '只读'}</span></div>
          <dl class="entity-facts"><div><dt>权限</dt><dd>${escapeHtml(permissions.join(', ') || 'none')}</dd></div><div><dt>超时</dt><dd>${item.timeout_seconds || item.timeout ? `${escapeHtml(item.timeout_seconds || item.timeout)}s` : '—'}</dd></div><div><dt>版本</dt><dd>${escapeHtml(item.version || '1')}</dd></div></dl>
        </article>`;
      }).join('') : '<div class="empty-state">Capability Manifest 尚未由服务端返回。</div>';
      $('capabilityPluginRows').innerHTML = state.capabilityPlugins.length ? state.capabilityPlugins.map((item) => `<article class="entity-card compact-card">
        <header><div><span class="entity-type">plugin · ${escapeHtml(item.version || '—')}</span><h3>${escapeHtml(item.display_name || item.id)}</h3></div><span class="status-chip ${item.healthy ? 'green' : 'red'}">${item.healthy ? '正常' : '需检查'}</span></header>
        <p class="entity-description">${escapeHtml(item.description || item.validation_error || '')}</p>
        <footer class="entity-actions"><span class="mono">${escapeHtml(item.id)}</span><button class="secondary" type="button" data-plugin-toggle="${escapeHtml(item.id)}" ${item.protected ? 'disabled' : ''}>${item.protected ? '核心插件' : item.enabled ? '停用插件' : '启用插件'}</button></footer>
      </article>`).join('') : '<div class="empty-state">暂无插件。</div>';
      $('capabilitySkillRows').innerHTML = state.capabilitySkills.length ? state.capabilitySkills.map((item) => `<article class="entity-card skill-card">
        <header><div><span class="entity-type">${escapeHtml(item.scope || 'all')}</span><h3>${escapeHtml(item.name || item.id)}</h3></div><span class="status-chip ${item.healthy && Number(item.enabled) ? 'green' : item.healthy ? 'amber' : 'red'}">${item.healthy ? Number(item.enabled) ? '启用' : '停用' : '异常'}</span></header>
        <p class="entity-description">${escapeHtml(item.description || item.validation_error || '')}</p>
        <dl class="entity-facts"><div><dt>使用</dt><dd>${escapeHtml(item.use_count || 0)}</dd></div><div><dt>最近</dt><dd>${escapeHtml(item.last_used_at || '尚未使用')}</dd></div></dl>
        <footer class="entity-actions"><button class="secondary" type="button" data-skill-edit="${escapeHtml(item.id)}">编辑 Skill</button><button class="link" type="button" data-skill-toggle="${escapeHtml(item.id)}">${Number(item.enabled) ? '停用' : '启用'}</button></footer>
      </article>`).join('') : '<div class="empty-state">暂无 Skill。</div>';
      window.renderNetworkPolicy?.();
    }

    async function loadCapabilities({ forceMarketplace = false, waitForMarketplace = false } = {}) {
      setText('pluginMarketStatus', state.pluginMarketplace.length
        ? '已显示上次目录 · 后台检查更新'
        : '能力概览先显示，插件目录正在后台同步…');
      const marketplaceJob = loadPluginMarketplace({ force: forceMarketplace });
      const result = await bridge('/capabilities/summary');
      state.capabilityPlugins = result.plugins || [];
      state.capabilitySkills = result.skills || [];
      state.capabilityManifests = result.capabilities || result.catalog || [];
      state.networkPolicy = result.network_policy || state.networkPolicy;
      renderCapabilities();
      setCapabilityWorkspace(state.capabilityWorkspace);
      if (waitForMarketplace) await marketplaceJob;
    }

    function editSkill(id) {
      const item = state.capabilitySkills.find((entry) => entry.id === id);
      if (!item) return;
      $('skillId').value = item.id || ''; $('skillName').value = item.name || ''; $('skillDescription').value = item.description || '';
      $('skillScope').value = item.scope || 'all'; $('skillTriggers').value = item.triggers || ''; $('skillInstructions').value = item.instructions || '';
      $('skillEnabled').checked = Boolean(Number(item.enabled));
      $('skillEditor').open = true;
      requestAnimationFrame(() => $('skillName').focus());
    }

    function editNewSkill() {
      $('skillId').value = '';
      $('skillName').value = '';
      $('skillDescription').value = '';
      $('skillScope').value = 'all';
      $('skillTriggers').value = '';
      $('skillInstructions').value = '';
      $('skillEnabled').checked = true;
      $('skillEditor').open = true;
      requestAnimationFrame(() => $('skillName').focus());
    }

    async function saveSkill() {
      const result = await bridge('/capabilities/skills', { method: 'POST', body: JSON.stringify({
        id: $('skillId').value.trim(), name: $('skillName').value.trim(), description: $('skillDescription').value.trim(),
        scope: $('skillScope').value, triggers: $('skillTriggers').value.trim(), instructions: $('skillInstructions').value,
        enabled: $('skillEnabled').checked ? '1' : '0',
      }) });
      state.capabilitySkills = result.skills || []; renderCapabilities(); $('skillEditor').open = false; setConnection('Skill 已保存并可用于任务选择。', 'ok');
    }

    async function toggleSkill(id) {
      const item = state.capabilitySkills.find((entry) => entry.id === id);
      if (!item) return;
      const result = await bridge('/capabilities/skills/toggle', { method: 'POST', body: JSON.stringify({ id, enabled: Number(item.enabled) ? '0' : '1' }) });
      state.capabilitySkills = result.skills || []; renderCapabilities();
    }

    async function togglePlugin(id) {
      const item = state.capabilityPlugins.find((entry) => entry.id === id);
      if (!item || item.protected) return;
      if (!window.confirm(`确认${item.enabled ? '停用' : '启用'}插件 ${item.display_name || id}？AstrBot 会短暂重启。`)) return;
      const result = await bridge('/capabilities/plugins/toggle', { method: 'POST', headers: { 'Idempotency-Key': `plugin-toggle-${id}-${crypto.randomUUID()}` }, body: JSON.stringify({ id, enabled: item.enabled ? '0' : '1' }) });
      state.capabilityPlugins = result.plugins || []; renderCapabilities(); setConnection('插件状态已更新，AstrBot 已重启。', 'ok');
    }

    async function reloadPlugins() {
      if (!window.confirm('确认重载 AstrBot 插件？QQ 连接会短暂重连。')) return;
      const result = await bridge('/capabilities/plugins/reload', { method: 'POST', headers: { 'Idempotency-Key': `plugin-reload-${crypto.randomUUID()}` }, body: '{}' });
      state.capabilityPlugins = result.plugins || []; renderCapabilities(); setConnection(result.ok ? 'AstrBot 插件已重载。' : result.error || '插件重载失败。', result.ok ? 'ok' : 'error');
    }
