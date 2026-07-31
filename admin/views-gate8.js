    const gate8StatusLabels = {
      healthy: '健康',
      unknown: '未知',
      degraded: '部分可用',
      unavailable: '不可用',
    };
    const gate8StatusTones = {
      healthy: 'green',
      unknown: '',
      degraded: 'amber',
      unavailable: 'red',
    };
    const gate8RoleLabels = {
      interaction_classifier: '交互意图判断',
      conversation_engagement: '群参与判断',
      conversation_reply: '对话回复',
      vision_caption: '识图理解',
      work_planner: '工作规划',
      work_executor: '工作执行',
    };
    const gate8WarningLabels = {
      'work_executor:kept_existing_remote_trusted_executor': '本地优先不会把工作执行交给未受信任的本地模型；当前受信任执行器保持不变。',
      'work_executor:trusted_current_executor_missing': '当前没有可保留的受信任工作执行器。',
    };
    const gate8ReasonLabels = {
      chatgpt_subscription_codex: 'ChatGPT 订阅登录态',
      codex_compatible: 'Codex 兼容路由',
      local_provider: '本地模型服务',
      registered_price: '已登记价格',
      declared_context_and_capabilities: '声明能力与上下文',
      verified_latency_and_role_compatibility: '验证结果、延迟与角色兼容性',
      no_compatible_model: '没有兼容模型',
    };

    function gate8State() {
      state.gate8 = state.gate8 || {
        relationship: null,
        notificationPolicy: null,
        socialPolicy: null,
        cutover: null,
        routingPresets: [],
        routingPreview: null,
        businessHealth: null,
        eventsBound: false,
      };
      return state.gate8;
    }

    function gate8RequestKey(prefix) {
      const suffix = globalThis.crypto?.randomUUID
        ? globalThis.crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
      return `${prefix}-${suffix}`;
    }

    function gate8Lines(value) {
      return String(value || '')
        .split(/\r?\n|,/)
        .map((item) => item.trim())
        .filter(Boolean);
    }

    function gate8SetStatus(id, message, tone = '') {
      const node = $(id);
      if (!node) return;
      node.className = `provider-status${tone ? ` ${tone}` : ''}`;
      node.textContent = message;
    }

    function gate8Target() {
      return {
        userId: $('relationshipUserId').value.trim() || 'admin',
        scopeType: $('relationshipScopeType').value || 'private_user',
        scopeId: $('relationshipScopeId').value.trim(),
      };
    }

    function renderRelationshipCutover(plan) {
      const gate = gate8State();
      gate.cutover = plan || {};
      const ready = Boolean(plan?.feature_enabled);
      const writable = ready
        && gate.relationship !== null
        && gate.notificationPolicy !== null
        && gate.socialPolicy !== null;
      const node = $('relationshipFeatureStatus');
      node.className = `gate8-feature-status ${ready ? 'ready' : 'blocked'}`;
      if (ready) {
        node.textContent = '关系状态、社交主动和任务通知已分离启用。所有写入都带版本检查和幂等保护。';
      } else if (plan?.ok) {
        node.textContent = '迁移与前置 Gate 已就绪，但 Gate 8 功能开关尚未切换；当前页面保持只读。';
      } else {
        node.textContent = 'Gate 8 前置条件尚未通过；当前页面保持只读，不能把未验证能力写成已上线。';
      }
      document.querySelectorAll(
        '#relationshipStateForm button[type="submit"], #notificationPolicyForm button[type="submit"], #socialProactiveForm button[type="submit"]',
      ).forEach((button) => {
        button.disabled = !writable;
      });
    }

    function renderRelationshipState(item) {
      const current = item || {};
      gate8State().relationship = current;
      $('relationshipPreferredAddress').value = current.preferred_address || '';
      $('relationshipInteractionStyle').value = current.interaction_style || 'natural';
      $('relationshipFamiliarity').value = current.familiarity_context || 'new';
      $('relationshipAllowedTopics').value = (current.allowed_topics || []).join('\n');
      $('relationshipBlockedTopics').value = (current.blocked_topics || []).join('\n');
      $('relationshipVersion').textContent = `版本 ${Number(current.version || 0)}`;
      gate8SetStatus('relationshipStateStatus', '互动边界已载入。', 'ok');
    }

    function renderNotificationPolicy(item) {
      const current = item || {};
      gate8State().notificationPolicy = current;
      const enabled = new Set(current.enabled_categories || []);
      document.querySelectorAll('input[name="notificationCategory"]').forEach((input) => {
        input.checked = enabled.has(input.value);
      });
      $('notificationQuietStart').value = current.quiet_start || '23:30';
      $('notificationQuietEnd').value = current.quiet_end || '09:00';
      $('notificationGroupWindow').value = Number(current.group_window_minutes ?? 10);
      $('notificationCriticalBypass').checked = Boolean(current.critical_bypass_quiet);
      $('notificationPolicyVersion').textContent = `版本 ${Number(current.version || 0)}`;
      gate8SetStatus('notificationPolicyStatus', '任务与安全通知规则已载入。', 'ok');
    }

    function renderSocialPolicy(item) {
      const current = item || {};
      gate8State().socialPolicy = current;
      $('socialTimezone').value = current.timezone || 'Asia/Shanghai';
      $('socialInitiativeMode').value = current.initiative_mode || 'balanced';
      $('socialQuietStart').value = current.quiet_start || '23:30';
      $('socialQuietEnd').value = current.quiet_end || '09:00';
      $('socialMinSilence').value = Number(current.min_silence_minutes ?? 180);
      $('socialMinGap').value = Number(current.min_gap_minutes ?? 360);
      $('socialDailyLimit').value = Number(current.daily_limit ?? 2);
      $('socialWeeklyLimit').value = Number(current.weekly_limit ?? 5);
      $('socialUnansweredLimit').value = Number(current.unanswered_limit ?? 2);
      $('socialEvaluationInterval').value = Number(current.evaluation_interval_minutes ?? 60);
      $('socialTopicNotes').value = current.topic_notes || '';
      $('socialScheduleJitter').value = Number(current.schedule_jitter_minutes ?? 20);
      $('socialTopicCooldown').value = Number(current.topic_cooldown_minutes ?? 1440);
      $('socialAuthorized').checked = Boolean(current.authorized);
      $('socialPolicyEnabled').checked = Boolean(current.enabled);
      const intents = new Set(current.allowed_intents || []);
      document.querySelectorAll('input[name="socialIntent"]').forEach((input) => {
        input.checked = intents.has(input.value);
      });
      $('socialPolicyVersion').textContent = `版本 ${Number(current.policy_version || 0)}`;
      gate8SetStatus(
        'socialPolicyStatus',
        current.enabled ? '社交主动已启用；每次发送仍需满足真实触发原因和节律边界。' : '社交主动当前关闭。',
        current.enabled ? 'ok' : '',
      );
    }

    const proactiveDecisionLabels = {
      delivered: '已送达',
      pending: '已决定发送，等待送达',
      blocked: '未发送（策略拦截）',
      skipped: '本次保持安静',
    };
    const proactiveReasonLabels = {
      global_social_proactive_disabled: '全局社交主动开关已关闭',
      social_policy_disabled: '该用户的社交主动规则已关闭',
      social_policy_not_authorized: '该用户尚未明确授权社交主动',
      social_quiet_hours: '当前处于静默时间',
      social_unanswered_limit: '连续未回复已达到上限',
      user_became_active: '用户在发送前已经主动发言',
      proactive_event_not_sendable: '主动事件已被标记为不可发送',
      notification_quiet_hours: '当前处于通知静默时间',
    };

    function proactiveReasonLabel(value) {
      const reason = String(value || '').trim();
      if (proactiveReasonLabels[reason]) return proactiveReasonLabels[reason];
      if (reason.startsWith('notification_category_disabled:')) {
        return `通知类别未启用：${reason.slice('notification_category_disabled:'.length)}`;
      }
      return reason || '未记录原因';
    }

    function renderRecentProactiveEvent(items) {
      const latest = Array.isArray(items) && items.length ? items[0] : null;
      const node = $('socialPolicyStatus');
      if (!node) return;
      if (!latest) {
        node.className = 'provider-status';
        node.textContent = '最近一次决策尚未载入。';
        return;
      }
      const reason = latest.blocked_reason || latest.error || latest.reason || '';
      let status = 'pending';
      let label = proactiveDecisionLabels.pending;
      if (latest.delivered_at) {
        status = 'ok';
        label = proactiveDecisionLabels.delivered;
      } else if (latest.error || latest.blocked_reason) {
        status = 'error';
        label = proactiveDecisionLabels.blocked;
      } else if (latest.action === 'skip' || latest.intent === 'silence') {
        label = proactiveDecisionLabels.skipped;
      }
      node.className = `provider-status ${status}`;
      node.textContent = `最近一次：${label} · 原因：${proactiveReasonLabel(reason)} · 决策时间：${latest.decision_at || '未知'}`;
    }

    async function loadRelationshipManagement() {
      const target = gate8Target();
      const gate = gate8State();
      gate.relationship = null;
      gate.notificationPolicy = null;
      gate.socialPolicy = null;
      gate.recentProactiveEvent = null;
      const relationshipQuery = new URLSearchParams({
        user_id: target.userId,
        scope_type: target.scopeType,
        scope_id: target.scopeId,
      });
      const ownerQuery = new URLSearchParams({
        user_id: target.userId,
        channel_scope: 'owner',
      });
      const userQuery = new URLSearchParams({ user_id: target.userId });
      gate8SetStatus('relationshipStateStatus', '正在读取互动边界。', 'pending');
      gate8SetStatus('notificationPolicyStatus', '正在读取通知规则。', 'pending');
      gate8SetStatus('socialPolicyStatus', '正在读取社交主动规则。', 'pending');
      const plan = await bridge('/assistant/relationship/cutover');
      renderRelationshipCutover(plan);
      const [relationship, notification, social, settings, proactiveEvents] = await Promise.all([
        bridge(`/assistant/relationship?${relationshipQuery.toString()}`),
        bridge(`/assistant/notification-policy?${ownerQuery.toString()}`),
        bridge(`/assistant/proactive/social-policy?${userQuery.toString()}`),
        bridge('/assistant/settings'),
        bridge(`/assistant/proactive/events?${userQuery.toString()}`),
      ]);
      renderRelationshipState(relationship);
      renderNotificationPolicy(notification);
      renderSocialPolicy(social);
      renderRecentProactiveEvent(proactiveEvents.events || []);
      $('globalProactiveEnabled').checked = ['1', 'true', 'yes', 'on'].includes(
        String(settings.settings?.proactive_enabled || '0').toLowerCase(),
      );
      gate8SetStatus('globalProactiveStatus', '全局社交主动总开关已载入。', 'ok');
      renderRelationshipCutover(plan);
    }

    async function saveRelationshipState(event) {
      event.preventDefault();
      const current = gate8State().relationship || { version: 0 };
      const target = gate8Target();
      const payload = {
        user_id: target.userId,
        scope_type: target.scopeType,
        scope_id: target.scopeId,
        preferred_address: $('relationshipPreferredAddress').value.trim(),
        interaction_style: $('relationshipInteractionStyle').value,
        familiarity_context: $('relationshipFamiliarity').value,
        allowed_topics: gate8Lines($('relationshipAllowedTopics').value),
        blocked_topics: gate8Lines($('relationshipBlockedTopics').value),
        social_proactive_enabled: false,
        expected_version: Number(current.version || 0),
      };
      const button = $('saveRelationshipBtn');
      button.disabled = true;
      gate8SetStatus('relationshipStateStatus', '正在保存互动边界。', 'pending');
      try {
        const result = await bridge('/assistant/relationship', {
          method: 'POST',
          headers: { 'Idempotency-Key': gate8RequestKey('relationship') },
          body: JSON.stringify(payload),
        });
        renderRelationshipState(result);
        setConnection('互动关系已保存。', 'ok');
      } catch (error) {
        gate8SetStatus('relationshipStateStatus', error.message || String(error), 'error');
        throw error;
      } finally {
        button.disabled = !Boolean(gate8State().cutover?.feature_enabled);
      }
    }

    async function saveGlobalProactive(event) {
      event.preventDefault();
      const button = $('saveGlobalProactiveBtn');
      button.disabled = true;
      gate8SetStatus('globalProactiveStatus', '正在保存全局总开关。', 'pending');
      try {
        await bridge('/assistant/settings', {
          method: 'POST',
          body: JSON.stringify({ proactive_enabled: $('globalProactiveEnabled').checked ? '1' : '0' }),
        });
        gate8SetStatus('globalProactiveStatus', '全局社交主动总开关已保存。', 'ok');
        setConnection('全局社交主动总开关已保存。', 'ok');
      } catch (error) {
        gate8SetStatus('globalProactiveStatus', error.message || String(error), 'error');
        throw error;
      } finally {
        button.disabled = false;
      }
    }

    async function saveNotificationPolicy(event) {
      event.preventDefault();
      const current = gate8State().notificationPolicy || { version: 0 };
      const target = gate8Target();
      const payload = {
        user_id: target.userId,
        channel_scope: 'owner',
        enabled_categories: Array.from(
          document.querySelectorAll('input[name="notificationCategory"]:checked'),
        ).map((input) => input.value),
        quiet_start: $('notificationQuietStart').value || '23:30',
        quiet_end: $('notificationQuietEnd').value || '09:00',
        critical_bypass_quiet: $('notificationCriticalBypass').checked,
        group_window_minutes: Number($('notificationGroupWindow').value || 10),
        expected_version: Number(current.version || 0),
      };
      const button = $('saveNotificationPolicyBtn');
      button.disabled = true;
      gate8SetStatus('notificationPolicyStatus', '正在保存任务与安全通知规则。', 'pending');
      try {
        const result = await bridge('/assistant/notification-policy', {
          method: 'POST',
          headers: { 'Idempotency-Key': gate8RequestKey('notification') },
          body: JSON.stringify(payload),
        });
        renderNotificationPolicy(result);
        setConnection('任务与安全通知规则已保存。', 'ok');
      } catch (error) {
        gate8SetStatus('notificationPolicyStatus', error.message || String(error), 'error');
        throw error;
      } finally {
        button.disabled = !Boolean(gate8State().cutover?.feature_enabled);
      }
    }

    async function saveSocialPolicy(event) {
      event.preventDefault();
      const current = gate8State().socialPolicy || { policy_version: 0 };
      const enabled = $('socialPolicyEnabled').checked;
      const authorized = $('socialAuthorized').checked;
      if (enabled && !authorized) {
        gate8SetStatus('socialPolicyStatus', '启用社交主动前，必须确认用户已经明确授权。', 'error');
        $('socialAuthorized').focus();
        return;
      }
      const payload = {
        user_id: gate8Target().userId,
        timezone: $('socialTimezone').value.trim() || 'Asia/Shanghai',
        quiet_start: $('socialQuietStart').value || '23:30',
        quiet_end: $('socialQuietEnd').value || '09:00',
        min_silence_minutes: Number($('socialMinSilence').value || 180),
        min_gap_minutes: Number($('socialMinGap').value || 360),
        daily_limit: Number($('socialDailyLimit').value || 2),
        weekly_limit: Number($('socialWeeklyLimit').value || 5),
        unanswered_limit: Number($('socialUnansweredLimit').value || 2),
        evaluation_interval_minutes: Number($('socialEvaluationInterval').value || 60),
        topic_notes: $('socialTopicNotes').value.trim(),
        initiative_mode: $('socialInitiativeMode').value || 'balanced',
        schedule_jitter_minutes: Number($('socialScheduleJitter').value || 20),
        topic_cooldown_minutes: Number($('socialTopicCooldown').value || 1440),
        allowed_intents: Array.from(
          document.querySelectorAll('input[name="socialIntent"]:checked'),
        ).map((input) => input.value),
        condition_contract: { trigger_reason_required: true },
        authorized,
        enabled,
        expected_version: Number(current.policy_version || 0),
      };
      const button = $('saveSocialPolicyBtn');
      button.disabled = true;
      gate8SetStatus('socialPolicyStatus', '正在保存社交主动边界。', 'pending');
      try {
        const result = await bridge('/assistant/proactive/social-policy', {
          method: 'POST',
          headers: { 'Idempotency-Key': gate8RequestKey('social-proactive') },
          body: JSON.stringify(payload),
        });
        renderSocialPolicy(result);
        setConnection('社交主动边界已保存。', 'ok');
      } catch (error) {
        gate8SetStatus('socialPolicyStatus', error.message || String(error), 'error');
        throw error;
      } finally {
        button.disabled = !Boolean(gate8State().cutover?.feature_enabled);
      }
    }

    function gate8WarningLabel(value) {
      const warning = String(value || '');
      if (gate8WarningLabels[warning]) return gate8WarningLabels[warning];
      const [role, detail] = warning.split(':', 2);
      const roleLabel = gate8RoleLabels[role] || role;
      const details = {
        no_compatible_model: '没有兼容模型',
        local_model_unavailable: '没有已登记的本地模型',
        price_unknown: '价格未登记，不能声称这是最低成本',
        selected_model_test_status_unknown: '最近验证状态未知',
        codex_compatible_model_unavailable: '没有可用的 Codex 路由，保留兼容模型',
      };
      return `${roleLabel}：${details[detail] || detail || warning}`;
    }

    function renderModelRoutingPresets(result) {
      const gate = gate8State();
      gate.routingPresets = result.presets || [];
      const activeId = result.active?.preset || '';
      const active = gate.routingPresets.find((item) => item.id === activeId);
      $('modelPresetActive').textContent = active ? `当前：${active.label}` : '未应用预设';
      $('modelPresetActive').className = `badge${active ? ' green' : ''}`;
      $('modelPresetGrid').innerHTML = gate.routingPresets.length
        ? gate.routingPresets.map((item) => `
          <button class="gate8-preset-card" type="button" data-model-preset-preview="${escapeHtml(item.id)}"
            aria-pressed="${item.id === gate.routingPreview?.preset ? 'true' : 'false'}" ${item.available ? '' : 'disabled'}>
            <span>
              <strong>${escapeHtml(item.label)}</strong>
              <small>${escapeHtml(item.description)}</small>
            </span>
            <span class="gate8-preset-meta">
              <span>${escapeHtml(item.changed_roles || 0)} 个角色变更</span>
              <span>${item.available ? `${escapeHtml((item.warnings || []).length)} 条提醒` : '当前不可用'}</span>
            </span>
          </button>
        `).join('')
        : '<div class="empty-state">没有可用预设。请先创建连接并登记模型。</div>';
    }

    async function loadModelRoutingPresets() {
      const result = await bridge('/assistant/models/presets');
      renderModelRoutingPresets(result);
    }

    function renderModelPresetPreview(result) {
      const gate = gate8State();
      gate.routingPreview = result;
      $('modelPresetPreview').classList.remove('hidden');
      $('confirmModelPresetApply').checked = false;
      $('applyModelPresetBtn').disabled = true;
      document.querySelectorAll('[data-model-preset-preview]').forEach((button) => {
        button.setAttribute('aria-pressed', button.dataset.modelPresetPreview === result.preset ? 'true' : 'false');
      });
      const mappings = result.mappings || [];
      $('modelPresetMappingList').innerHTML = mappings.map((item) => `
        <article class="gate8-mapping-card">
          <strong>${escapeHtml(gate8RoleLabels[item.role] || item.role)}</strong>
          <span>${escapeHtml(item.model_label || '无可用模型')}</span>
          <small>${escapeHtml(item.provider_name || '未绑定连接')}</small>
          <small>${escapeHtml(gate8ReasonLabels[item.selection_reason] || item.selection_reason || '')}${item.changed ? ' · 将变更' : ' · 保持不变'}</small>
        </article>
      `).join('');
      const warnings = [
        ...(result.warnings || []).map((message) => ({ message, blocker: false })),
        ...(result.blockers || []).map((message) => ({ message, blocker: true })),
      ];
      $('modelPresetWarningList').innerHTML = warnings.length
        ? warnings.map((item) => `<div class="gate8-warning${item.blocker ? ' blocker' : ''}">${escapeHtml(gate8WarningLabel(item.message))}</div>`).join('')
        : '<div class="provider-status ok">没有未说明的风险或未知值。</div>';
      gate8SetStatus(
        'modelPresetPreviewStatus',
        result.ok
          ? `已生成“${result.definition?.label || result.preset}”预览；应用前请逐项检查。`
          : '当前连接和模型无法满足这套预设，不能应用。',
        result.ok ? 'ok' : 'error',
      );
    }

    async function previewModelRoutingPreset(preset) {
      gate8SetStatus('modelPresetPreviewStatus', '正在根据当前真实连接生成预览。', 'pending');
      const result = await bridge(`/assistant/models/presets/preview?preset=${encodeURIComponent(preset)}`);
      renderModelPresetPreview(result);
    }

    async function applyModelRoutingPreset() {
      const preview = gate8State().routingPreview;
      if (!preview?.preset || !preview?.fingerprint || !preview.ok) return;
      const button = $('applyModelPresetBtn');
      button.disabled = true;
      gate8SetStatus('modelPresetPreviewStatus', '正在重新校验并原子应用路由。', 'pending');
      try {
        const result = await bridge('/assistant/models/presets/apply', {
          method: 'POST',
          body: JSON.stringify({
            preset: preview.preset,
            fingerprint: preview.fingerprint,
          }),
        });
        renderModelPresetPreview(result);
        await loadModelRegistry();
        setConnection(`已应用“${result.definition?.label || result.preset}”路由预设。`, 'ok');
      } catch (error) {
        gate8SetStatus('modelPresetPreviewStatus', error.message || String(error), 'error');
        throw error;
      }
    }

    function renderBusinessHealth(result) {
      const gate = gate8State();
      gate.businessHealth = result;
      const counts = result.counts || {};
      const summaryItems = [
        ['健康', counts.healthy || 0, '有当前业务证据', 'green'],
        ['未知', counts.unknown || 0, '不能算作健康', ''],
        ['部分可用', counts.degraded || 0, '需要处理', 'amber'],
        ['不可用', counts.unavailable || 0, '业务链路中断', 'red'],
      ];
      $('businessHealthSummary').innerHTML = summaryItems.map(([label, value, detail, tone], index) => (
        `<article class="insight-card ${tone}"><span class="insight-index">0${index + 1}</span><p>${label}</p><strong>${escapeHtml(value)}</strong><small>${detail}</small></article>`
      )).join('');
      $('businessHealthGrid').innerHTML = (result.checks || []).map((item) => `
        <article class="gate8-health-card" data-health-status="${escapeHtml(item.status)}">
          <div class="gate8-health-head">
            <h3>${escapeHtml(item.label)}</h3>
            <span class="badge ${escapeHtml(gate8StatusTones[item.status] || '')}">${escapeHtml(gate8StatusLabels[item.status] || item.status)}</span>
          </div>
          <p>${escapeHtml(item.summary)}</p>
          <small>证据：${escapeHtml(item.evidence_type || '未说明')}</small>
          ${item.next_action ? `<div class="next-action">${escapeHtml(item.next_action)}</div>` : ''}
        </article>
      `).join('') || '<div class="empty-state">没有业务检查结果。</div>';
      const tone = result.status === 'healthy' ? 'ok' : result.status === 'unknown' ? '' : 'error';
      gate8SetStatus(
        'businessHealthStatus',
        result.live_probe
          ? `完整业务诊断完成 · ${new Date().toLocaleTimeString()}`
          : '已加载数据库与状态证据；QQ、Codex 和预览仍显示未知，直到手动运行完整诊断。',
        tone,
      );
    }

    async function loadBusinessHealth({ live = false } = {}) {
      const button = $('runBusinessHealthBtn');
      if (live && button) {
        button.disabled = true;
        button.textContent = '诊断中…';
        gate8SetStatus('businessHealthStatus', '正在运行 QQ、Codex 和成品预览业务探测。', 'pending');
      }
      try {
        const result = await bridge(`/assistant/health/business?live=${live ? '1' : '0'}`);
        renderBusinessHealth(result);
      } finally {
        if (button) {
          button.disabled = false;
          button.textContent = '运行完整诊断';
        }
      }
    }

    function bindGate8Events() {
      const gate = gate8State();
      if (gate.eventsBound) return;
      gate.eventsBound = true;
      const bind = (id, eventName, handler) => {
        const node = $(id);
        if (node) node.addEventListener(eventName, handler);
      };
      bind('relationshipTargetForm', 'submit', (event) => {
        event.preventDefault();
        loadRelationshipManagement().catch((error) => setConnection(error.message || String(error), 'error'));
      });
      bind('reloadRelationshipBtn', 'click', () => (
        loadRelationshipManagement().catch((error) => setConnection(error.message || String(error), 'error'))
      ));
      bind('relationshipStateForm', 'submit', (event) => (
        saveRelationshipState(event).catch((error) => setConnection(error.message || String(error), 'error'))
      ));
      bind('notificationPolicyForm', 'submit', (event) => (
        saveNotificationPolicy(event).catch((error) => setConnection(error.message || String(error), 'error'))
      ));
      bind('socialProactiveForm', 'submit', (event) => (
        saveSocialPolicy(event).catch((error) => setConnection(error.message || String(error), 'error'))
      ));
      bind('globalProactiveForm', 'submit', (event) => (
        saveGlobalProactive(event).catch((error) => setConnection(error.message || String(error), 'error'))
      ));
      bind('modelPresetGrid', 'click', (event) => {
        const button = event.target.closest('[data-model-preset-preview]');
        if (button && !button.disabled) {
          previewModelRoutingPreset(button.dataset.modelPresetPreview)
            .catch((error) => setConnection(error.message || String(error), 'error'));
        }
      });
      bind('closeModelPresetPreviewBtn', 'click', () => {
        $('modelPresetPreview').classList.add('hidden');
        gate.routingPreview = null;
        document.querySelectorAll('[data-model-preset-preview]').forEach((button) => button.setAttribute('aria-pressed', 'false'));
      });
      bind('confirmModelPresetApply', 'change', () => {
        $('applyModelPresetBtn').disabled = !(
          $('confirmModelPresetApply').checked && gate.routingPreview?.ok
        );
      });
      bind('applyModelPresetBtn', 'click', () => (
        applyModelRoutingPreset().catch((error) => setConnection(error.message || String(error), 'error'))
      ));
      bind('runBusinessHealthBtn', 'click', () => (
        loadBusinessHealth({ live: true }).catch((error) => setConnection(error.message || String(error), 'error'))
      ));
    }

    window.loadRelationshipManagement = loadRelationshipManagement;
    window.loadModelRoutingPresets = loadModelRoutingPresets;
    window.loadBusinessHealth = loadBusinessHealth;
