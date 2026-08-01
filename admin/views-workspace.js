    function renderAssistantSummary() {
      const knowledge = state.knowledgeItems || [];
      const items = [
        ['作用域记忆', state.memories.length, 'blue'],
        ['待确认候选', (state.memoryCandidates || []).length, 'amber'],
        ['已发布知识', knowledge.filter((item) => item.status === 'published').length, 'green'],
        ['待审核草稿', knowledge.filter((item) => item.status === 'draft').length, 'amber'],
        ['对话线程', (state.conversationThreads || []).length, 'blue'],
      ];
      $('assistantSummary').innerHTML = items.map(([label, value, tone]) => (
        `<div class="summary-item"><span>${escapeHtml(label)}</span><strong class="${tone}">${escapeHtml(value)}</strong></div>`
      )).join('');
    }

    function renderCurrentProject(project) {
      const current = project || {};
      $('currentProjectMeta').textContent = current.id ? '已选择' : '未设置';
      $('currentProjectName').textContent = current.name || '(未设置)';
      $('currentProjectPath').textContent = current.path || '(未设置)';
      $('currentProjectDescription').textContent = current.description || '暂无说明。';
    }

    function renderAssistantChannelSettings(settings) {
      assistantControl('memeEnabled').checked = boolSetting(settings.meme_enabled ?? '1');
      assistantControl('memeDailyEnabled').checked = boolSetting(settings.meme_daily_enabled ?? '1');
      assistantControl('memeWorkEnabled').checked = boolSetting(settings.meme_work_enabled ?? '0');
    }

    function renderAssistantSettings(settings) {
      state.assistantSettings = Object.assign({}, state.assistantSettings, settings || {});
      // The assistant workspace can load before the optional desktop-pet
      // feature. Its accessibility labels are enhanced when that feature is
      // available; rendering settings must not fail while it is lazy-loaded.
      window.updatePetAccessibleName?.(state.assistantSettings.display_name);
      renderAssistantRoleSettings(state.assistantSettings);
      renderAssistantChannelSettings(state.assistantSettings);
      const mirrorProvider = state.assistantSettings.chat_provider === 'openai-compatible'
        ? (state.assistantSettings.chat_provider_preset || 'OpenAI-compatible API')
        : 'Codex CLI';
      const mirrorModel = state.assistantSettings.chat_model || state.assistantSettings.codex_model || 'Provider 默认模型';
      $('assistantModelRouteSummary').textContent = `只读兼容镜像：${mirrorProvider} / ${mirrorModel}。实际分类、聊天、规划和执行绑定以“模型与路由”页为唯一准则。`;
      const keyText = state.assistantSettings.chat_api_key_set
        ? `已配置 ${state.assistantSettings.chat_api_key_preview || ''}`.trim()
        : '未配置';
      renderChatProviderState(
        state.assistantSettings,
        state.assistantSettings.chat_provider === 'openai-compatible' && !state.assistantSettings.chat_api_key_set ? 'pending' : 'ok',
        `当前通道：${state.assistantSettings.chat_provider === 'openai-compatible' ? 'OpenAI-compatible' : 'Codex CLI'}；Key：${keyText}`,
      );
      renderAgentPolicy(state.assistantSettings);
    }

    function renderSocialPanel() {
      if (!$('memeAssetList')) {
        return;
      }
      const enabledCount = state.memeAssets.filter((item) => Number(item.enabled)).length;
      $('socialStatus').className = `provider-status ${enabledCount ? 'ok' : 'pending'}`;
      $('socialStatus').textContent = `表情包 ${enabledCount}/${state.memeAssets.length} 个启用。主动聊天已迁移到“工作 → 自动化”。`;
      $('memeAssetList').innerHTML = state.memeAssets.length
        ? state.memeAssets.map((item) => (
          `<div class="settings-row">
            <strong>${escapeHtml(item.name || item.id)} ${Number(item.enabled) ? '' : '（待审核）'}</strong>
            <p>${escapeHtml(item.pack || '')} / ${escapeHtml(item.emotion || '')} / ${escapeHtml(item.tags || '')}</p>
            <p>${escapeHtml(item.license_note || item.source || '')}</p>
            <button class="secondary" type="button" data-meme-toggle="${escapeHtml(item.id || '')}">${Number(item.enabled) ? '停用' : '启用'}</button>
          </div>`
        )).join('')
        : '<div class="empty">暂无表情包资产。</div>';
    }

    function renderChatProviderState(settings, kind = '', message = '') {
      const node = $('chatProviderState');
      if (!node) {
        return;
      }
      const current = settings || state.assistantSettings || {};
      const preset = state.providerPresets.find((item) => item.key === current.chat_provider_preset);
      const provider = preset?.label || (current.chat_provider === 'openai-compatible' ? 'OpenAI-compatible' : 'Codex CLI');
      const model = current.chat_model || (provider === 'Codex CLI' ? 'ChatGPT 登录态' : '未设置模型');
      node.className = 'provider-status' + (kind ? ' ' + kind : '');
      node.textContent = message || `当前通道：${provider}；模型：${model}`;
    }

    function boolSetting(value) {
      return ['1', 'true', 'yes', 'on'].includes(String(value || '').toLowerCase());
    }

    function renderAgentPolicy(settings = {}) {
      if (!$('agentLanguage')) {
        return;
      }
      $('agentLanguage').value = settings.agent_language || 'zh-CN';
      $('agentDetailLevel').value = settings.agent_detail_level || 'standard';
      $('agentPersonaLevel').value = settings.agent_persona_level || 'light';
      $('agentTechnicalMode').value = settings.agent_technical_mode || 'professional';
      $('agentSummarizeTools').checked = boolSetting(settings.agent_summarize_tools ?? '1');
      $('agentDiscloseFallback').checked = boolSetting(settings.agent_disclose_fallback ?? '1');
      $('agentSelfCheck').checked = boolSetting(settings.agent_self_check ?? '1');
      $('agentClarifyWhenUncertain').checked = boolSetting(settings.agent_clarify_when_uncertain ?? '1');
      $('agentConfirmRiskyOps').checked = boolSetting(settings.agent_confirm_risky_ops ?? '1');
      $('agentQualityLogEnabled').checked = boolSetting(settings.agent_quality_log_enabled ?? '1');
      $('agentModeAutodetect').checked = boolSetting(settings.agent_mode_autodetect ?? '1');
      $('agentDefaultMode').value = settings.agent_default_mode || 'auto';
      $('agentLowConfidenceBehavior').value = settings.agent_low_confidence_behavior || 'previous';
      $('agentWorkExitPolicy').value = settings.agent_work_exit_policy || 'auto';
      $('agentWorkTtlMinutes').value = settings.agent_work_ttl_minutes || '30';
      $('agentWorkMaxTurns').value = settings.agent_work_max_turns || '6';
      $('agentDailyExpressionLevel').value = settings.agent_daily_expression_level || 'light';
      $('agentDailyEmojiMode').value = settings.agent_daily_emoji_mode || 'manual';
      $('agentWorkEmojiEnabled').checked = boolSetting(settings.agent_work_emoji_enabled ?? '0');
      $('agentMixedModeEnabled').checked = boolSetting(settings.agent_mixed_mode_enabled ?? '1');
      renderBrainSummary();
    }

    function agentPolicyPayload() {
      return {
        agent_language: $('agentLanguage').value,
        agent_detail_level: $('agentDetailLevel').value,
        agent_persona_level: $('agentPersonaLevel').value,
        agent_technical_mode: $('agentTechnicalMode').value,
        agent_summarize_tools: $('agentSummarizeTools').checked ? '1' : '0',
        agent_disclose_fallback: $('agentDiscloseFallback').checked ? '1' : '0',
        agent_self_check: $('agentSelfCheck').checked ? '1' : '0',
        agent_clarify_when_uncertain: $('agentClarifyWhenUncertain').checked ? '1' : '0',
        agent_confirm_risky_ops: $('agentConfirmRiskyOps').checked ? '1' : '0',
        agent_quality_log_enabled: $('agentQualityLogEnabled').checked ? '1' : '0',
        agent_mode_autodetect: $('agentModeAutodetect').checked ? '1' : '0',
        agent_default_mode: $('agentDefaultMode').value,
        agent_low_confidence_behavior: $('agentLowConfidenceBehavior').value,
        agent_work_exit_policy: $('agentWorkExitPolicy').value,
        agent_work_ttl_minutes: $('agentWorkTtlMinutes').value,
        agent_work_max_turns: $('agentWorkMaxTurns').value,
        agent_daily_expression_level: $('agentDailyExpressionLevel').value,
        agent_daily_emoji_mode: $('agentDailyEmojiMode').value,
        agent_work_emoji_enabled: $('agentWorkEmojiEnabled').checked ? '1' : '0',
        agent_mixed_mode_enabled: $('agentMixedModeEnabled').checked ? '1' : '0',
      };
    }

    function renderBrainSummary() {
      const settings = state.assistantSettings || {};
      const events = state.qualityEvents || [];
      const sessions = state.modeSessions || [];
      const warnCount = events.filter((item) => item.status === 'warn').length;
      const failedCount = events.filter((item) => item.status === 'failed').length;
      const activeWork = sessions.filter((item) => item.mode === 'work').length;
      const items = [
        ['模式判断', boolSetting(settings.agent_mode_autodetect ?? '1') ? '模型自动' : '固定策略', boolSetting(settings.agent_mode_autodetect ?? '1') ? 'blue' : 'amber'],
        ['默认语言', settings.agent_language === 'auto' ? '跟随用户' : '简体中文', 'blue'],
        ['自检', boolSetting(settings.agent_self_check ?? '1') ? '开启' : '关闭', boolSetting(settings.agent_self_check ?? '1') ? 'blue' : 'red'],
        ['降级说明', boolSetting(settings.agent_disclose_fallback ?? '1') ? '开启' : '关闭', boolSetting(settings.agent_disclose_fallback ?? '1') ? 'blue' : 'red'],
        ['工作会话', `${activeWork} 个活跃 / ${sessions.length} 条`, activeWork ? 'amber' : 'blue'],
        ['最近质量', `${events.length} 条 / ${warnCount + failedCount} 条需关注`, warnCount + failedCount ? 'red' : 'blue'],
      ];
      const target = $('agentPolicySummary');
      if (!target) return;
      target.innerHTML = items.map(([label, value, tone]) => (
        `<div class="summary-item"><span>${escapeHtml(label)}</span><strong class="${tone}">${escapeHtml(value)}</strong></div>`
      )).join('');
    }

    function issueLabel(issue) {
      return {
        empty_response: '空回复',
        language_mismatch: '语言不符',
        too_brief_for_requested_detail: '不够详细',
        fallback_not_disclosed: '降级未说明',
        weak_actionability: '可执行性弱',
        raw_tool_output_leaked: '原始输出泄露',
        work_mode_social_leak: '工作模式社交泄露',
      }[issue] || issue;
    }

    function modeBadge(mode) {
      if (mode === 'work') {
        return ['工作', 'blue'];
      }
      if (mode === 'mixed') {
        return ['混合', 'amber'];
      }
      if (mode === 'daily') {
        return ['日常', ''];
      }
      return [mode || '未知', ''];
    }

    function renderModeSessions(sessions) {
      state.modeSessions = sessions || [];
      renderBrainSummary();
      if (!state.modeSessions.length) {
        $('modeSessionRows').innerHTML = '<tr><td colspan="8" class="empty">暂无模式会话。</td></tr>';
        return;
      }
      $('modeSessionRows').innerHTML = state.modeSessions.map((session) => {
        const [modeText, modeTone] = modeBadge(session.mode);
        return `<tr>
          <td class="mono">${escapeHtml(session.user_id || '')}</td>
          <td><span class="badge ${modeTone}">${escapeHtml(modeText)}</span></td>
          <td>${escapeHtml(session.intent || '')}</td>
          <td class="mono">${escapeHtml(Number(session.confidence || 0).toFixed(2))}</td>
          <td>${escapeHtml(session.work_lifecycle || '')}</td>
          <td>${escapeHtml(session.ended_reason || '')}</td>
          <td class="mono">${escapeHtml(session.updated_at || '')}</td>
          <td class="quality-cell">${escapeHtml(session.reason || '')}</td>
        </tr>`;
      }).join('');
    }

    function qualityBadge(status) {
      if (status === 'failed') {
        return ['失败', 'red'];
      }
      if (status === 'warn') {
        return ['需关注', 'amber'];
      }
      if (status === 'passed') {
        return ['通过', 'blue'];
      }
      return [status || '未知', ''];
    }

    function renderQualityEvents(events) {
      state.qualityEvents = events || [];
      renderBrainSummary();
      if (!state.qualityEvents.length) {
        $('qualityRows').innerHTML = '<tr><td colspan="8" class="empty">暂无质量事件。</td></tr>';
        return;
      }
      $('qualityRows').innerHTML = state.qualityEvents.map((event) => {
        const [badgeText, badgeTone] = qualityBadge(event.status);
        const [modeText, modeTone] = modeBadge(event.checks?.mode || event.checks?.mode_decision?.mode || '');
        const issues = event.issues && event.issues.length
          ? `<div class="issue-list">${event.issues.map((issue) => `<span class="badge ${event.status === 'failed' ? 'red' : 'amber'}">${escapeHtml(issueLabel(issue))}</span>`).join('')}</div>`
          : '<span class="badge blue">无</span>';
        const intent = event.checks?.intent_label || event.intent || '';
        return `<tr>
          <td class="mono">${escapeHtml(event.id || '')}</td>
          <td class="mono">${escapeHtml(event.created_at || '')}</td>
          <td><span class="badge ${modeTone}">${escapeHtml(modeText)}</span></td>
          <td>${escapeHtml(intent)}</td>
          <td><span class="badge ${badgeTone}">${escapeHtml(badgeText)}</span></td>
          <td>${issues}</td>
          <td class="quality-cell">${escapeHtml(event.request || '')}</td>
          <td class="quality-cell">${escapeHtml(event.response || '')}</td>
        </tr>`;
      }).join('');
    }

    async function loadQualityEvents() {
      const query = new URLSearchParams({
        limit: String(Math.max(1, Math.min(Number($('qualityLimitInput').value || 20), 100))),
      });
      const userId = $('qualityUserIdInput').value.trim();
      const status = $('qualityStatusFilter').value;
      if (userId) {
        query.set('user_id', userId);
      }
      if (status) {
        query.set('status', status);
      }
      const result = await bridge('/assistant/quality?' + query.toString());
      renderQualityEvents(result.events || []);
      return result.events || [];
    }

    async function loadModeSessions() {
      const query = new URLSearchParams({ limit: '20' });
      const userId = $('qualityUserIdInput').value.trim();
      if (userId) {
        query.set('user_id', userId);
      }
      const result = await bridge('/assistant/mode-sessions?' + query.toString());
      renderModeSessions(result.sessions || []);
      return result.sessions || [];
    }

    async function loadBrainPanel({ force = false } = {}) {
      if (!state.authenticated) {
        return;
      }
      try {
        const [, settingsResult] = await Promise.all([
          loadPersonaWorkspace({ force }),
          bridge('/assistant/settings'),
          loadVoiceResponsePolicy({ force }),
        ]);
        state.assistantSettings = settingsResult.settings || {};
        renderAgentPolicy(state.assistantSettings);
        setConnection('身份、表达与助手策略已更新。', 'ok');
        // Quality tables are observability, not first meaningful content. Defer
        // them so the Persona workspace keeps a two-request cold first screen.
        window.setTimeout(() => {
          // A quick navigation must not make a hidden Persona view keep issuing
          // observability requests. The next visit will load them when the user
          // actually remains on this page.
          if (!state.authenticated || state.activeView !== 'brain') return;
          Promise.allSettled([loadQualityEvents(), loadModeSessions()]).then((results) => {
            const failures = results.filter((item) => item.status === 'rejected');
            if (failures.length) console.warn('身份与表达质量观察部分载入失败。', failures.map((item) => item.reason));
          });
        }, 240);
      } catch (error) {
        $('personaWorkspacePanel')?.setAttribute('aria-busy', 'false');
        $('assistantRoleStatus').className = 'provider-status error';
        $('assistantRoleStatus').textContent = error.message || String(error);
        setConnection(error.message || String(error), 'error');
      }
    }

    async function saveAgentPolicy() {
      const payload = agentPolicyPayload();
      try {
        $('saveAgentPolicyBtn').disabled = true;
        const result = await bridge('/assistant/settings', {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        state.assistantSettings = result.settings || Object.assign({}, state.assistantSettings, payload);
        renderAgentPolicy(state.assistantSettings);
        renderOverviewProvider(state.assistantSettings);
        setConnection('助手策略已保存。', 'ok');
      } catch (error) {
        setConnection(error.message || String(error), 'error');
      } finally {
        $('saveAgentPolicyBtn').disabled = false;
      }
    }

    function renderMemories(memories) {
      state.memories = memories || [];
      if (!state.memories.length) {
        $('memoryRows').innerHTML = '<tr><td colspan="7" class="empty">暂无记忆。</td></tr>';
        return;
      }
      $('memoryRows').innerHTML = state.memories.map((memory) => {
        const id = String(memory.id || '');
        const promotable = memory.sensitivity !== 'sensitive' && memory.scope_type !== 'sensitive_private';
        return `<tr>
          <td class="mono">${escapeHtml(id)}</td>
          <td>${escapeHtml(memory.kind || '')}</td>
          <td>${escapeHtml(memory.content || '')}</td>
          <td>${escapeHtml(memory.scope_label || memory.scope_type || '旧版范围')}</td>
          <td><span class="badge ${memory.sensitivity === 'sensitive' ? 'amber' : ''}">${escapeHtml(memory.sensitivity || '未标记')}</span></td>
          <td class="mono">${escapeHtml(memory.updated_at || memory.created_at || '')}</td>
          <td><div class="table-actions">
            ${promotable ? `<button class="secondary" data-memory-action="promote" data-memory-id="${escapeHtml(id)}" type="button">整理为知识草稿</button>` : ''}
            <button class="danger" data-memory-action="delete" data-memory-id="${escapeHtml(id)}" type="button">删除</button>
          </div></td>
        </tr>`;
      }).join('');
    }

    function renderConversation(items) {
      const conversations = items || [];
      $('conversationMeta').textContent = `${conversations.length} 条`;
      if (!conversations.length) {
        $('conversationPreview').textContent = '(暂无对话记录)';
        return;
      }
      $('conversationPreview').textContent = conversations.map((item) => (
        `[${item.created_at || '?'}] ${item.role || '?'}\n${item.content || ''}`
      )).join('\n\n');
    }

    async function loadMemories() {
      const query = new URLSearchParams({
        limit: String(Math.max(1, Math.min(Number($('memoryLimitInput').value || 20), 50))),
      });
      const keyword = $('memorySearchInput').value.trim();
      if (keyword) {
        query.set('q', keyword);
      }
      const result = await bridge('/assistant/memories?' + query.toString());
      renderMemories(result.memories || []);
      renderAssistantSummary();
      return result.memories || [];
    }

    async function loadConversation(threadId = '') {
      if (!threadId) {
        const threadsResult = await bridge('/assistant/conversations?limit=50');
        state.conversationThreads = threadsResult.result || [];
        const selector = $('conversationThreadSelect');
        selector.innerHTML = state.conversationThreads.length
          ? state.conversationThreads.map((thread, index) => (
            `<option value="${escapeHtml(thread.id || '')}">${escapeHtml(thread.channel_label || thread.channel_type || '对话')} ${index + 1}</option>`
          )).join('')
          : '<option value="">暂无对话线程</option>';
        threadId = selector.value;
      }
      if (!threadId) {
        renderConversation([]);
        return;
      }
      const result = await bridge(`/assistant/conversations/${encodeURIComponent(threadId)}/messages?limit=20`);
      renderConversation(result.result || []);
    }

    async function loadProjectsPanel() {
      if (!state.authenticated) {
        return;
      }
      try {
        const [projectsResult, currentResult] = await Promise.all([
          bridge('/projects?include_archived=true'),
          bridge('/projects/current'),
        ]);
        const projects = projectsResult.projects || [];
        const current = currentResult.project || projectsResult.current || projectsResult.current_project || null;
        renderProjects(projects, current);
        renderCurrentProject(current);
        setConnection('项目空间已更新。', 'ok');
      } catch (error) {
        setConnection(error.message || String(error), 'error');
      }
    }
    window.loadProjectsPanel = loadProjectsPanel;

    async function loadAssistantPanel({ force = false } = {}) {
      if (!state.authenticated) return;
      try {
        await loadKnowledgeWorkspace({ force });
        setConnection('Living Wiki、作用域记忆与最近对话已从同一快照更新。', 'ok');
      } catch (error) {
        setConnection(error.message || String(error), 'error');
      }
    }

    (() => {
      let workspaceEventsBound = false;
      window.bindWorkspaceEvents = () => {
        if (workspaceEventsBound) return;
        workspaceEventsBound = true;
        $('saveAssistantProviderBtn')?.addEventListener('click', () => saveAssistantProvider());
        $('addMemoryBtn')?.addEventListener('click', () => addMemory());
        $('loadMemoriesBtn')?.addEventListener('click', async () => {
          try {
            await Promise.all([loadMemories(), loadConversation()]);
            setConnection('记忆已更新。', 'ok');
          } catch (error) {
            setConnection(error.message || String(error), 'error');
          }
        });
        $('conversationThreadSelect')?.addEventListener('change', async (event) => {
          try {
            await loadConversation(event.target.value);
          } catch (error) {
            setConnection(error.message || String(error), 'error');
          }
        });
        $('memorySearchInput')?.addEventListener('keydown', async (event) => {
          if (event.key !== 'Enter') return;
          event.preventDefault();
          try {
            await loadMemories();
            setConnection('记忆已更新。', 'ok');
          } catch (error) {
            setConnection(error.message || String(error), 'error');
          }
        });
        $('memoryRows')?.addEventListener('click', (event) => {
          const button = event.target.closest('button[data-memory-action]');
          if (!button || button.disabled) return;
          if (button.dataset.memoryAction === 'delete') deleteMemory(button.dataset.memoryId);
        });
      };
    })();

    async function saveAssistantProvider() {
      const payload = {
        meme_enabled: $('memeEnabled').checked ? '1' : '0',
        meme_daily_enabled: $('memeDailyEnabled').checked ? '1' : '0',
        meme_work_enabled: $('memeWorkEnabled').checked ? '1' : '0',
      };
      try {
        $('saveAssistantProviderBtn').disabled = true;
        renderChatProviderState(state.assistantSettings, 'pending', '正在保存聊天策略。');
        const result = await bridge('/assistant/settings', {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        state.assistantSettings = Object.assign({}, state.assistantSettings, result.settings || payload);
        renderAssistantChannelSettings(state.assistantSettings);
        renderAgentPolicy(state.assistantSettings);
        renderAssistantSummary();
        setConnection('聊天策略已保存；模型路由未被修改。', 'ok');
      } catch (error) {
        renderChatProviderState(state.assistantSettings, 'error', error.message || String(error));
        setConnection(error.message || String(error), 'error');
      } finally {
        $('saveAssistantProviderBtn').disabled = false;
      }
    }

    async function testAssistantProvider() {
      try {
        $('testAssistantProviderBtn').disabled = true;
        renderChatProviderState(state.assistantSettings, 'pending', '正在测试 Provider 连通性。');
        const result = await bridge('/assistant/provider/test', {
          method: 'POST',
          body: JSON.stringify({}),
        });
        if (result.settings) {
          state.assistantSettings = result.settings;
        }
        const message = result.ok
          ? `测试通过：${result.provider_label || result.provider || 'provider'} ${result.model || ''} ${result.reply || result.message || ''}`.trim()
          : `测试失败：${result.error_kind || 'error'} ${result.error || ''}`.trim();
        renderChatProviderState(state.assistantSettings, result.ok ? 'ok' : 'error', message);
        renderAssistantSummary();
        setConnection(message, result.ok ? 'ok' : 'error');
      } catch (error) {
        renderChatProviderState(state.assistantSettings, 'error', error.message || String(error));
        setConnection(error.message || String(error), 'error');
      } finally {
        $('testAssistantProviderBtn').disabled = false;
      }
    }

    async function loadSocialPanel() {
      const memesResult = await bridge('/assistant/memes');
      state.memeAssets = memesResult.memes || [];
      renderSocialPanel();
    }

    async function toggleMemeAsset(id) {
      const item = state.memeAssets.find((entry) => entry.id === id);
      if (!item) {
        return;
      }
      const result = await bridge('/assistant/memes', {
        method: 'POST',
        body: JSON.stringify(Object.assign({}, item, { enabled: Number(item.enabled) ? '0' : '1' })),
      });
      state.memeAssets = result.memes || [];
      renderSocialPanel();
    }

    async function toggleAutomationJob(id) {
      const item = state.automationJobs.find((entry) => entry.id === id);
      if (!item) {
        return;
      }
      await bridge('/automations/jobs', {
        method: 'POST',
        body: JSON.stringify(Object.assign({}, item, { enabled: Number(item.enabled) ? '0' : '1', next_due_at: '' })),
      });
      await loadAutomationView();
    }

    async function toggleProactivePolicy(userId) {
      const item = state.proactivePolicies.find((entry) => entry.user_id === userId);
      if (!item) return;
      await bridge('/assistant/proactive/policies', {
        method: 'POST',
        body: JSON.stringify(Object.assign({}, item, {
          authorized: item.authorized ? '1' : '0',
          enabled: Number(item.enabled) ? '0' : '1',
          next_check_at: '',
        })),
      });
      await loadAutomationView();
    }

    async function addMemory() {
      const content = $('memoryContentInput').value.trim();
      if (!content) {
        setConnection('请输入记忆内容。', 'error');
        return;
      }
      const payload = {
        kind: $('memoryKindInput').value,
        content,
        source: 'admin',
        scope_type: $('memoryScopeInput').value,
        sensitivity: $('memorySensitivityInput').value,
      };
      if (payload.scope_type === 'project' && state.currentProject?.id) {
        payload.project_id = state.currentProject.id;
      }
      try {
        $('addMemoryBtn').disabled = true;
        await bridge('/assistant/memories', {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        $('memoryContentInput').value = '';
        await loadMemories();
        setConnection('记忆已保存。', 'ok');
      } catch (error) {
        setConnection(error.message || String(error), 'error');
      } finally {
        $('addMemoryBtn').disabled = false;
      }
    }

    async function deleteMemory(memoryId) {
      if (!window.confirm(`确定删除记忆 #${memoryId}？`)) {
        return;
      }
      try {
        await bridge('/assistant/memories/delete', {
          method: 'POST',
          body: JSON.stringify({ id: memoryId }),
        });
        await loadMemories();
        setConnection('记忆已删除。', 'ok');
      } catch (error) {
        setConnection(error.message || String(error), 'error');
      }
    }
