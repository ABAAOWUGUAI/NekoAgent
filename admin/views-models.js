    // Model control plane is kept separate from the capability catalog.

    function modelOptions(selectedId, predicate = () => true, emptyLabel = '不设置', { executor = false } = {}) {
      const options = state.modelCatalog.filter(predicate).map((item) => {
        const elig = executor ? (item.executor_eligibility || {}) : null;
        const suffix = elig ? (elig.can_bind ? '（已验证）' : `（${elig.reason_zh || '不可选'}）`) : '';
        const attr = elig && !elig.can_bind ? ' disabled' : '';
        return `<option value="${escapeHtml(item.id)}" ${item.id === selectedId ? 'selected' : ''}${attr}>${escapeHtml(item.label || item.model || item.id)} · ${escapeHtml(item.provider_name || '')}${escapeHtml(suffix)}</option>`;
      });
      return `<option value="">${escapeHtml(emptyLabel)}</option>` + options.join('');
    }

    function isCustomCodexTransport(value = $('modelProviderTransport')?.value) {
      return value === 'codex_cli_custom_provider';
    }

    function renderExecutorRuntimeSummary(profile) {
      const target = $('modelExecutorRuntimeSummary');
      if (!target) return;
      const runtime = profile?.runtime || {};
      const version = Number(profile?.config_version || 0);
      const applied = Number(profile?.applied_version || 0);
      const facts = profile ? [
        ['配置', version && version === applied ? `v${version} 已应用` : `v${version || '-'} 待应用`, version && version === applied],
        ['Profile', runtime.profile_available ? '已安装' : '缺失', runtime.profile_available],
        ['沙箱', runtime.sandbox_available ? '可用' : '缺少 bubblewrap', runtime.sandbox_available],
        ['工作目录', runtime.workspace_available ? '可用' : '缺失', runtime.workspace_available],
      ] : [['配置', '待保存', false], ['Profile', '待检测', false], ['沙箱', '待检测', false], ['工作目录', '待检测', false]];
      target.innerHTML = facts.map(([label, value, ok]) => `<div><dt>${escapeHtml(label)}</dt><dd class="${ok ? 'ready' : ''}">${escapeHtml(value)}</dd></div>`).join('');
    }
    async function verifyExecutorWorkMode() {
      const status = $('modelExecutorVerificationStatus'), button = $('verifyExecutorWorkModeBtn');
      if (!status || !button) return;
      button.disabled = true;
      try {
        const r = await bridge('/assistant/models/executor/verify', { provider_id: $('modelProviderId').value.trim(), timeout: 120 });
        status.textContent = r.ok ? '隔离工作模式验证已通过（文件/命令/哈希/最终正文均完成）。' : `验证失败：${r.error || 'executor_verify_failed'}`;
      } finally { button.disabled = false; }
    }

    function syncExecutorUpstreamModels(preferredModelId = '') {
      const providerId = $('modelExecutorUpstreamProvider').value;
      const models = state.modelCatalog.filter((item) => (
        item.provider_id === providerId && Number(item.enabled) && Number(item.provider_enabled)
      ));
      $('modelExecutorUpstreamModel').innerHTML = models.length
        ? models.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label || item.model || item.id)} · ${escapeHtml(item.model || '')}</option>`).join('')
        : '<option value="">该连接没有可用模型</option>';
      if (models.some((item) => item.id === preferredModelId)) $('modelExecutorUpstreamModel').value = preferredModelId;
    }

    function syncExecutorUpstreamOptions(profile = null) {
      const currentId = $('modelProviderId').value.trim();
      const candidates = state.modelProviders.filter((item) => (
        item.id !== currentId && item.transport === 'openai_chat_completions' && Number(item.enabled)
      ));
      const preferredProvider = profile?.upstream_provider_id || $('modelExecutorUpstreamProvider').value;
      $('modelExecutorUpstreamProvider').innerHTML = candidates.length
        ? candidates.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · OpenAI-compatible</option>`).join('')
        : '<option value="">请先添加并验证一个 API 连接</option>';
      if (candidates.some((item) => item.id === preferredProvider)) $('modelExecutorUpstreamProvider').value = preferredProvider;
      syncExecutorUpstreamModels(profile?.upstream_model_id || $('modelExecutorUpstreamModel').value);
    }

    function syncExecutorProfileFields(profile = null) {
      const visible = isCustomCodexTransport();
      $('modelExecutorFields').classList.toggle('hidden', !visible);
      $('modelProviderTrusted').closest('label').classList.toggle('hidden', !visible && $('modelProviderTransport').value !== 'codex_cli_chatgpt');
      if (visible && !$('modelExecutorProfileName').value.trim()) {
        $('modelExecutorProfileName').value = profile?.profile_name || $('modelProviderId').value.trim();
      }
      if (visible) syncExecutorUpstreamOptions(profile);
      renderExecutorRuntimeSummary(profile);
    }

    function renderModelRegistry() {
      const transportLabels = {
        codex_cli_chatgpt: 'Codex CLI · ChatGPT 登录',
        openai_chat_completions: 'OpenAI-compatible API',
        azure_openai_chat_completions: 'Azure OpenAI API',
        anthropic_messages: 'Anthropic Messages',
        google_gemini_generate_content: 'Gemini generateContent',
        codex_cli_custom_provider: 'Codex CLI · 自定义 Provider',
      };
      const billingLabels = {
        chatgpt_subscription: 'ChatGPT 订阅',
        api_key: 'API Key 按量',
        local_proxy: '本地代理 / 上游自管',
      };
      const healthyProviders = state.modelProviders.filter((item) => Number(item.enabled) && item.last_test_status === 'passed').length;
      const untestedProviders = state.modelProviders.filter((item) => Number(item.enabled) && !item.last_test_status).length;
      const boundRoles = state.modelRoles.filter((item) => item.primary_model_id).length;
      const items = [
        ['已验证连接', `${healthyProviders}/${state.modelProviders.length}`, '通过最近一次模型调用验证', healthyProviders ? 'blue' : 'red'],
        ['待验证连接', untestedProviders, '未测试不会计入健康', untestedProviders ? 'amber' : 'blue'],
        ['可用模型', state.modelCatalog.filter((item) => Number(item.enabled) && Number(item.provider_enabled)).length, `目录共 ${state.modelCatalog.length} 个`, 'blue'],
        ['已绑定角色', `${boundRoles}/${state.modelRoles.length}`, 'Runtime Role 路由', boundRoles === state.modelRoles.length ? 'green' : 'amber'],
      ];
      $('modelRegistrySummary').innerHTML = items.map(([label, value, detail, tone], index) => `<article class="insight-card ${tone}">
        <span class="insight-index">0${index + 1}</span>
        <p>${escapeHtml(label)}</p>
        <strong>${escapeHtml(value)}</strong>
        <small>${escapeHtml(detail)}</small>
      </article>`).join('');
      $('modelCatalogProvider').innerHTML = state.modelProviders.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join('');
      $('modelTemplateGallery').innerHTML = state.modelConnectionTemplates.map((item) => `<button class="template-card" type="button" data-model-template-start="${escapeHtml(item.key)}">
        <span class="template-card-icon" aria-hidden="true">${escapeHtml((item.label || '?').slice(0, 1))}</span>
        <span class="template-card-copy"><strong>${escapeHtml(item.label || item.key)}</strong><small>${escapeHtml(item.description || '可编辑的用户连接模板')}</small></span>
        <span class="template-card-cta">添加此类型</span>
      </button>`).join('');
      const providerIds = new Set(state.modelProviders.map((item) => item.id));
      if (!providerIds.has(state.selectedModelProvider)) {
        state.selectedModelProvider = state.modelProviders[0]?.id || '';
      }
      $('modelProviderRows').innerHTML = state.modelProviders.length ? state.modelProviders.map((item) => {
        const models = state.modelCatalog.filter((model) => model.provider_id === item.id);
        const status = !Number(item.enabled) ? '停用' : item.last_test_status === 'passed' ? '已验证' : item.last_test_status === 'failed' ? '测试失败' : '未测试';
        const tone = status === '已验证' ? 'green' : status === '测试失败' ? 'red' : status === '未测试' ? 'amber' : '';
        const selected = item.id === state.selectedModelProvider;
        const firstModel = models.find((model) => Number(model.enabled) && Number(model.provider_enabled));
        const executor = item.executor_profile;
        const executorRuntime = item.executor_runtime || executor?.runtime || {};
        const executorCopy = item.transport === 'codex_cli_custom_provider'
          ? `<small class="executor-state ${executorRuntime.ready ? 'ready' : ''}">执行器 · ${escapeHtml(executor?.profile_name || '未配置 Profile')} · ${executorRuntime.ready ? '预检通过' : escapeHtml(executorRuntime.error || '待预检')}</small>`
          : '';
        return `<article class="resource-record connection-record ${selected ? 'selected' : ''}">
          <button class="resource-record-main" type="button" data-model-provider-select="${escapeHtml(item.id)}" aria-pressed="${selected ? 'true' : 'false'}">
            <span><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(transportLabels[item.transport] || item.transport || item.kind)} · ${escapeHtml(billingLabels[item.billing_scope] || item.billing_scope || '费用未知')} · ${models.length} 个模型</small>${executorCopy}</span>
            <span class="status-chip ${tone}">${escapeHtml(status)}</span>
          </button>
          <div class="connection-actions"><button class="secondary" type="button" data-model-provider-models="${escapeHtml(item.id)}">管理模型</button><button class="link" type="button" data-model-provider-test="${escapeHtml(firstModel?.id || '')}" ${firstModel ? '' : 'disabled'}>验证</button><button class="link" type="button" data-model-provider-edit="${escapeHtml(item.id)}">编辑</button></div>
        </article>`;
      }).join('') : '<div class="empty-state"><strong>还没有模型连接</strong><span>点击“添加连接”，从 Codex、OpenAI、Anthropic、Gemini 或本地模型模板开始。</span></div>';
      const visibleModels = state.selectedModelProvider
        ? state.modelCatalog.filter((item) => item.provider_id === state.selectedModelProvider)
        : state.modelCatalog;
      const selectedProvider = state.modelProviders.find((item) => item.id === state.selectedModelProvider);
      $('modelCatalogContext').textContent = selectedProvider
        ? `“${selectedProvider.name}”下的模型；这些记录可单独编辑或删除。`
        : '先选择一个连接，再维护它的模型。';
      $('modelCatalogRows').innerHTML = visibleModels.length ? visibleModels.map((item) => {
        const available = Number(item.enabled) && Number(item.provider_enabled);
        const roles = state.modelRoles.filter((role) => role.primary_model_id === item.id || role.fallback_model_id === item.id);
        return `<article class="resource-model-row">
          <div class="resource-model-copy">
            <span class="entity-type">${escapeHtml(item.provider_name || item.provider_id)}</span>
            <h3>${escapeHtml(item.label || item.id)}</h3>
            <p class="mono">${escapeHtml(item.model || '(Provider 默认模型)')}</p>
          </div>
          <div class="resource-model-meta">
            <span class="status-chip ${available ? 'green' : 'red'}">${available ? '可用' : '停用'}</span>
            <span>${escapeHtml((item.capabilities || []).join(' / ') || '能力未声明')}</span>
            <span>${roles.length ? `${roles.length} 个角色` : '未绑定角色'}</span>
          </div>
          <footer class="entity-actions"><button class="secondary" type="button" data-model-edit="${escapeHtml(item.id)}">编辑</button><button class="link" type="button" data-model-test="${escapeHtml(item.id)}" ${available ? '' : 'disabled'}>测试</button></footer>
        </article>`;
      }).join('') : '<div class="empty-state"><strong>此连接还没有模型</strong><span>添加服务端实际可调用的模型名，再分配给使用场景。</span></div>';
      $('modelRoleRows').innerHTML = state.modelRoles.length ? state.modelRoles.map((item) => {
        const executor = item.role === 'work_executor';
        const predicate = (model) => Number(model.enabled) && Number(model.provider_enabled);
        const primary = state.modelCatalog.find((model) => model.id === item.primary_model_id);
        return `<article class="route-card">
          <div class="route-marker" aria-hidden="true"></div>
          <header><div><span class="entity-type mono">${escapeHtml(item.role)}</span><h3>${escapeHtml(item.label)}</h3></div><span class="status-chip ${primary ? 'green' : 'amber'}">${primary ? '已路由' : '待配置'}</span></header>
          <p>${escapeHtml(item.description || '')}</p>
          ${item.role === 'vision_caption' ? '<p class="route-capability-note">独立识图路由：必须绑定同时声明 text + vision 的模型；不会替换对话回复。</p>' : ''}
          <div class="route-selects">
            <label>主模型<select data-role-primary="${escapeHtml(item.role)}">${modelOptions(item.primary_model_id, predicate, '选择主模型', { executor })}</select></label>
            ${executor ? '' : `<label>备用模型<select data-role-fallback="${escapeHtml(item.role)}">${modelOptions(item.fallback_model_id, predicate)}</select></label>`}
          </div>
          <div class="route-footer"><span>${executor ? '仅受信任执行器 · 无自动切换' : '文本或对话模型'}</span><button class="primary" type="button" data-role-save="${escapeHtml(item.role)}">保存路由</button></div>
        </article>`;
      }).join('') : '<div class="empty-state">暂无角色绑定。</div>';
      $('modelRuntimeRows').innerHTML = state.modelRuntimeInventories.length ? state.modelRuntimeInventories.map((item) => {
        const managed = item.config_mode === 'managed';
        const roles = item.role_bindings || [];
        return `<article class="runtime-boundary-card">
          <header><div><span class="entity-type">${escapeHtml(item.runtime_owner || '')}</span><h3>${escapeHtml(item.label || item.runtime_owner || '')}</h3></div><span class="status-chip ${item.status === 'ready' ? 'green' : 'amber'}">${escapeHtml(item.status === 'ready' ? '已读取' : item.status || '未知')}</span></header>
          <p class="runtime-source">${escapeHtml(item.source_label || '未提供配置来源')}</p>
          <dl class="runtime-facts"><div><dt>配置权</dt><dd>${managed ? '平台可写' : 'Runtime 只读'}</dd></div><div><dt>Provider</dt><dd>${(item.providers || []).length}</dd></div><div><dt>模型</dt><dd>${(item.models || []).length}</dd></div><div><dt>路由</dt><dd>${roles.length}</dd></div></dl>
          <p class="runtime-boundary-note">${managed ? '在本页管理平台连接、模型与场景路由。' : '请前往该 Runtime 自己的控制台修改；平台只读取清单，不会覆盖其配置。'}</p>
        </article>`;
      }).join('') : '<div class="empty-state">尚未读取 Runtime 清单。</div>';
      renderModelPlaygroundOptions();
    }
    function renderModelUsage() {
      const report = state.modelUsage || {};
      const summary = report.summary || {};
      const sloStatus = { passed: ['green', '通过'], failed: ['red', '未通过'], insufficient_evidence: ['amber', '证据不足'], observed: ['blue', '仅观测'], no_evidence: ['amber', '暂无证据'] };
      const metrics = [
        ['调用次数', summary.calls ?? 0, `${report.range_days || 7} 天范围`],
        ['成功率', summary.success_rate == null ? '暂无' : `${summary.success_rate}%`, '按实际返回状态统计'],
        ['Token', `${Number(summary.input_tokens || 0).toLocaleString()} / ${Number(summary.output_tokens || 0).toLocaleString()}`, `输入 / 输出 · ${summary.unknown_token_calls || 0} 次未知`],
        ['P95 延迟', summary.p95_duration == null ? '暂无' : `${summary.p95_duration}s`, `平均 ${summary.average_duration == null ? '暂无' : `${summary.average_duration}s`}`],
        ['估算费用', summary.estimated_cost ? `$${Number(summary.estimated_cost).toFixed(6)}` : '暂无', '只使用已配置价格'],
      ];
      $('modelUsageSummary').innerHTML = metrics.map(([label, value, detail], index) => `<article class="insight-card blue"><span class="insight-index">0${index + 1}</span><p>${escapeHtml(label)}</p><strong>${escapeHtml(value)}</strong><small>${escapeHtml(detail)}</small></article>`).join('');
      const cacheSloRows = (report.cache_slos || []).length ? report.cache_slos.map((item) => {
        const [tone, status] = sloStatus[item.status] || ['amber', '未知'];
        const rate = item.warm_hit_rate == null ? '暂无' : `${item.warm_hit_rate}%`;
        const [target, evidence] = item.target_percent == null ? ['不设百分比门槛', `${item.cache_known_calls || 0} 次上报缓存字段 · ${item.unknown_calls || 0} 次未知`] : [`目标 ≥${item.target_percent}%`, `warm ${item.warm_calls || 0}/${item.minimum_warm_calls || 0} 次 · ${Number(item.warm_hit_tokens || 0).toLocaleString()}/${Number(item.minimum_warm_tokens || 0).toLocaleString()} token`];
        return `<article class="resource-record cache-slo-record"><div class="resource-record-main"><span><strong>${escapeHtml(item.label || item.id)}</strong><small>${escapeHtml(item.description || '')}</small><small>${escapeHtml(evidence)}</small></span><span class="resource-model-meta"><strong>${escapeHtml(rate)}</strong><small>${escapeHtml(target)}</small><span class="status-chip ${tone}">${escapeHtml(status)}</span></span></div></article>`;
      }).join('') : '<div class="empty-state">当前范围内尚无可用于缓存分层验收的 Provider 用量。</div>';
      const modelRows = (report.by_model || []).length ? report.by_model.map((item) => `<article class="resource-model-row"><div class="resource-model-copy"><span class="entity-type">MODEL</span><h3>${escapeHtml(item.key)}</h3><p>${escapeHtml(item.calls)} 次调用 · ${escapeHtml(item.known_token_calls)} 次上报 Token</p></div><div class="resource-model-meta"><span>${Number(item.input_tokens || 0).toLocaleString()} 输入</span><span>${Number(item.output_tokens || 0).toLocaleString()} 输出</span><span>${item.estimated_cost ? `${escapeHtml(item.currency)} ${Number(item.estimated_cost).toFixed(6)}` : '费用未知'}</span></div></article>`).join('') : '<div class="empty-state">当前范围内还没有模型调用记录。</div>';
      $('modelUsageModels').innerHTML = `<section class="cache-slo-panel" aria-label="缓存分层验收"><div class="section-heading compact-heading"><div><h3>缓存分层验收</h3><p>长可复用上下文以 Provider warm-hit ≥95% 验收；短新对话与多模态只报告真实事实。</p></div></div><div class="entity-grid compact-entities" aria-live="polite">${cacheSloRows}</div></section>${modelRows}`;
      $('modelUsageEvents').innerHTML = (report.events || []).length ? report.events.map((item) => `<article class="resource-record"><div class="resource-record-main"><span><strong>${escapeHtml(item.source || item.role || 'model call')}</strong><small>${escapeHtml(item.model_name || item.model_id || item.provider_kind || 'unknown')} · ${escapeHtml(compactTimestamp(item.created_at))}</small></span><span class="status-chip ${item.status === 'success' ? 'green' : 'red'}">${item.usage_reported ? `${escapeHtml(item.total_tokens || 0)} token` : 'Token 未知'}</span></div></article>`).join('') : '<div class="empty-state">暂无调用记录。</div>';
    }
    function renderCodexOperations() {
      const item = state.codexOperations || {};
      const rows = [['版本', item.version || '未检测'], ['安装方式', item.install_method || 'unknown'], ['运行账号', item.service_user || 'unknown'], ['登录状态', item.login_state === 'authenticated' ? '已登录' : '需要登录']];
      $('codexOperationsStatus').innerHTML = rows.map(([label, value]) => `<article class="resource-record"><div class="resource-record-main"><span><strong>${escapeHtml(label)}</strong><small>${escapeHtml(value)}</small></span></div></article>`).join('');
      $('codexLoginSteps').innerHTML = (item.login_steps || []).map((step) => `<li>${escapeHtml(step)}</li>`).join('');
      $('codexUpgradeSteps').innerHTML = (item.upgrade_steps || []).map((step) => `<li>${escapeHtml(step)}</li>`).join('');
      renderProxyStatus();
    }
    function renderProxyStatus() {
      const item = state.proxyStatus || {};
      const tcp = item.tcp || {};
      const hz = item.healthz || {};
      const proxyRunning = tcp.ok && hz.ok;
      const rows = [
        ['代理类型', 'codex_deepseek_proxy'],
        ['进程状态', proxyRunning ? '● 运行中' : '○ 未运行'],
        ['端口', '127.0.0.1:5000 · ' + (tcp.ok ? `TCP ${tcp.latency_ms}ms` : (tcp.error || '不可达'))],
        ['本地健康', hz.ok ? `${hz.latency_ms}ms` : (hz.error || '未检测')],
        ['上游模型', hz.model || 'deepseek-v4-pro'],
        ['能力模式', '兼容模式（已禁用 thinking）'],
        ['最近检测', item.probed_at ? compactTimestamp(item.probed_at) : '未检测'],
      ];
      $('proxyStatusGrid').innerHTML = rows.map(([label, value]) => `<article class="resource-record"><div class="resource-record-main"><span><strong>${escapeHtml(label)}</strong><small>${escapeHtml(value)}</small></span></div></article>`).join('');
      const probeLocal = $('probeProxyLocalBtn');
      const probeUpstream = $('probeProxyUpstreamBtn');
      if (probeLocal) probeLocal.onclick = () => probeProxy(false);
      if (probeUpstream) probeUpstream.onclick = () => probeProxy(true);
    }
    async function loadProxyStatus() {
      try {
        state.proxyStatus = await bridge('/system/proxy/status');
      } catch (e) {
        state.proxyStatus = { ok: false, tcp: {}, healthz: {} };
      }
      renderProxyStatus();
    }
    async function probeProxy(upstream) {
      if (upstream && !window.confirm('上游探测会向 DeepSeek 发送最少请求，产生少量 API 费用。是否继续？')) return;
      const btn = upstream ? $('probeProxyUpstreamBtn') : $('probeProxyLocalBtn');
      const originalLabel = btn.textContent;
      btn.disabled = true;
      btn.textContent = '正在检测……';
      try {
        const result = await bridge('/system/proxy/probe', {
          method: 'POST',
          body: JSON.stringify({ upstream: !!upstream, confirm_cost: !!upstream }),
        });
        state.proxyStatus = result;
        renderProxyStatus();
        setConnection(upstream ? 'DeepSeek 上游检测通过。' : '代理本地检测通过。', 'ok');
      } catch (error) {
        setConnection(`检测失败：${error.message || String(error)}`, 'error');
      } finally {
        btn.disabled = false;
        btn.textContent = originalLabel;
      }
    }
    async function loadModelWorkspaceDetails(view) {
      if (view === 'routing' && window.loadModelRoutingPresets) {
        await window.loadModelRoutingPresets();
      } else if (view === 'usage') {
        await loadModelUsage();
      } else if (view === 'advanced') {
        await Promise.all([loadCodexOperations(), loadProxyStatus()]);
      }
    }
    function setModelWorkspace(view, { load = true } = {}) {
      const allowed = new Set(['connections', 'catalog', 'routing', 'playground', 'usage', 'advanced']);
      state.modelWorkspace = allowed.has(view) ? view : 'connections';
      document.querySelectorAll('[data-model-workspace]').forEach((button) => {
        const active = button.dataset.modelWorkspace === state.modelWorkspace;
        button.classList.toggle('active', active);
        button.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      document.querySelectorAll('[data-model-journey]').forEach((item) => {
        item.classList.toggle('active', item.dataset.modelJourney === state.modelWorkspace);
      });
      document.querySelectorAll('[data-model-pane]').forEach((node) => node.classList.toggle('hidden', node.dataset.modelPane !== state.modelWorkspace));
      $('modelRegistryWorkspace').classList.toggle('hidden', !['connections', 'catalog'].includes(state.modelWorkspace));
      $('modelRegistryWorkspace').classList.toggle('single-pane', ['connections', 'catalog'].includes(state.modelWorkspace));
      $('modelRegistrySummary').hidden = ['usage', 'advanced'].includes(state.modelWorkspace);
      const copy = {
        connections: ['连接账户', '连接你自己的登录态、API 账户或本地模型服务；所有连接都能编辑和删除。'],
        catalog: ['账户模型', '维护所选连接下真实可调用的模型；模型能力和凭证保持分离。'],
        routing: ['场景路由', '为模式判断、日常聊天、工作规划和工具执行分别选择模型。'],
        playground: ['模型验证台', '独立验证模型、提示词和参数，不写入 QQ、长期记忆或正式任务。'],
        usage: ['用量', '查看实际调用、Token、延迟、成功率与已配置价格下的估算费用。'],
        advanced: ['高级', '查看 Runtime 所有权、Codex 登录与第三方执行代理；新用户通常无需修改。'],
      }[state.modelWorkspace];
      const intro = document.querySelector('#view-models .page-intro');
      const heading = intro?.querySelector('h2');
      const description = intro?.querySelector('p:not(.page-kicker)');
      if (heading) heading.textContent = copy[0];
      if (description) description.textContent = copy[1];
      $('modelProviderNewBtn').classList.toggle('hidden', state.modelWorkspace !== 'connections');
      if (load && state.authenticated && ['routing', 'usage', 'advanced'].includes(state.modelWorkspace)) {
        setConnection(`正在读取${copy[0]}……`, 'ok');
        loadModelWorkspaceDetails(state.modelWorkspace)
          .then(() => setConnection(`${copy[0]}已更新。`, 'ok'))
          .catch((error) => setConnection(error.message || String(error), 'error'));
      }
    }
    window.setModelWorkspace = setModelWorkspace;
    function setModelCatalogView(view) {
      state.modelCatalogView = view === 'models' ? 'models' : 'providers';
      setModelWorkspace(state.modelCatalogView === 'models' ? 'catalog' : 'connections');
      document.querySelectorAll('[data-model-catalog-view]').forEach((button) => {
        const active = button.dataset.modelCatalogView === state.modelCatalogView;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
    }
    function editModelProvider(id = '') {
      const item = state.modelProviders.find((entry) => entry.id === id) || {};
      const template = state.modelConnectionTemplates.find((entry) => (
        entry.kind === item.kind && entry.transport === item.transport && entry.billing_scope === item.billing_scope
      ));
      $('modelProviderTemplate').value = template?.key || '';
      $('modelProviderId').value = item.id || '';
      $('modelProviderId').disabled = Boolean(item.id);
      $('modelProviderName').value = item.name || '';
      $('modelProviderKind').value = item.kind || 'openai-compatible';
      $('modelProviderTransport').value = item.transport || (item.kind === 'codex' ? 'codex_cli_custom_provider' : 'openai_chat_completions');
      $('modelProviderBilling').value = item.billing_scope || (item.kind === 'codex' ? 'local_proxy' : 'api_key');
      $('modelProviderBaseUrl').value = item.base_url || '';
      $('modelProviderApiKey').value = '';
      $('modelProviderClearKey').checked = false;
      $('modelProviderClearKeyLine').classList.toggle('hidden', !item.api_key_set);
      $('modelProviderTimeout').value = item.timeout_seconds || 60;
      $('modelProviderEnabled').checked = item.enabled == null ? true : Boolean(Number(item.enabled));
      $('modelProviderTrusted').checked = Boolean(Number(item.trusted_for_executor));
      const executor = item.executor_profile || null;
      $('modelExecutorProfileName').value = executor?.profile_name || item.id || '';
      $('modelExecutorEnabled').checked = executor == null ? true : Boolean(Number(executor.enabled));
      syncExecutorProfileFields(executor);
      $('modelTemplateHint').textContent = template?.description || '这是一条用户拥有的可编辑连接，不会被平台自动恢复。';
      $('modelProviderEditorStatus').className = 'provider-status';
      $('modelProviderEditorStatus').textContent = item.id ? '已载入连接，修改后保存。' : '填写连接信息后保存。';
      $('deleteModelProviderBtn').classList.toggle('hidden', !item.id);
      $('modelProviderEditor').open = true;
      requestAnimationFrame(() => $('modelProviderName').focus());
    }
    function editCatalogModel(id = '') {
      const item = state.modelCatalog.find((entry) => entry.id === id) || {};
      $('modelCatalogProvider').value = item.provider_id || state.modelProviders[0]?.id || '';
      $('modelCatalogProvider').disabled = Boolean(item.id);
      if (item.id) $('modelCatalogProvider').setAttribute('aria-describedby', 'modelCatalogProviderLockHint');
      else $('modelCatalogProvider').removeAttribute('aria-describedby');
      $('modelCatalogProviderLockHint').classList.toggle('hidden', !item.id);
      $('modelCatalogId').value = item.id || '';
      $('modelCatalogId').disabled = Boolean(item.id);
      $('modelCatalogLabel').value = item.label || '';
      $('modelCatalogName').value = item.model || '';
      $('modelContextWindow').value = item.context_window || 0;
      $('modelMaxOutput').value = item.max_output_tokens || 900;
      $('modelCatalogEnabled').checked = item.enabled == null ? true : Boolean(Number(item.enabled));
      $('modelSupportsTools').checked = Boolean(Number(item.supports_tools));
      const capabilities = new Set(item.capabilities || ['text']);
      $('modelCapabilityText').checked = capabilities.has('text');
      $('modelCapabilityVision').checked = capabilities.has('vision');
      $('modelCapabilityEmbedding').checked = capabilities.has('embedding');
      $('modelCapabilityStructured').checked = capabilities.has('structured_output');
      $('modelCatalogNotes').value = item.notes || '';
      $('modelInputPrice').value = item.input_price_per_million ?? '';
      $('modelOutputPrice').value = item.output_price_per_million ?? '';
      $('modelPriceCurrency').value = item.price_currency || 'USD';
      $('modelPriceSource').value = item.price_source || '';
      resetModelDiscovery({ editable: !item.id });
      $('modelCatalogEditor').open = true;
      $('deleteModelCatalogBtn').classList.toggle('hidden', !item.id);
      $('modelPricingEditor').open = Boolean(item.id);
      requestAnimationFrame(() => $('modelCatalogLabel').focus());
    }
    let discoveredProviderModelItems = [];
    function ensureModelDiscoveryControls() {
      const target = $('modelCatalogDiscovery');
      if (target.dataset.ready) return;
      target.dataset.ready = 'true';
      target.innerHTML = `<fieldset><legend>读取此连接上报的候选模型</legend><p id="modelCatalogDiscoveryHelp" class="provider-status">目录是 Provider 当前上报的候选项，不等于此令牌已经可用。选择后会发起一次实时、隔离的验证请求。</p><button id="discoverProviderModelsBtn" class="secondary" type="button" aria-describedby="modelCatalogDiscoveryHelp">实时读取候选目录</button><div id="discoveredProviderModelsLine" class="hidden"><label>搜索已读取的候选模型<input id="discoveredProviderModelSearch" type="search" autocomplete="off" spellcheck="false" placeholder="按模型 ID 或名称筛选" aria-describedby="discoveredProviderModelSearchSummary" disabled></label><p id="discoveredProviderModelSearchSummary" class="provider-status" aria-live="polite">先读取候选目录。</p><label>候选模型（须实时验证）<select id="discoveredProviderModels" aria-describedby="discoveredProviderValidationSummary" disabled><option value="">先读取</option></select></label><p id="discoveredProviderValidationSummary" class="provider-status">选择候选模型后，才能查看本页实时验证状态。</p></div><button id="validateDiscoveredModelBtn" class="secondary" type="button" disabled>实时验证所选模型</button><button id="applyDiscoveredModelBtn" class="primary" type="button" disabled>填入</button><p id="modelCatalogDiscoveryStatus" class="provider-status" role="status" aria-live="polite">等待读取。</p></fieldset>`;
    }
    function setModelDiscoveryStatus(message, tone = '') {
      ensureModelDiscoveryControls();
      const target = $('modelCatalogDiscoveryStatus'); target.className = `provider-status${tone ? ` ${tone}` : ''}`;
      target.textContent = message;
    }
    function updateDiscoveredValidationSummary() {
      const summary = $('discoveredProviderValidationSummary'); const result = window.modelDiscoveryValidationState.describe(selectedDiscoveredModel());
      $('applyDiscoveredModelBtn').disabled = !result.ok;
      summary.className = `provider-status${result.tone ? ` ${result.tone}` : ''}`;
      summary.textContent = result.text;
    }
    function renderDiscoveredProviderModels({ preserveSelection = false } = {}) {
      const search = $('discoveredProviderModelSearch');
      const select = $('discoveredProviderModels');
      const previous = preserveSelection ? select.value : '';
      const query = search.value.trim().toLocaleLowerCase();
      const models = discoveredProviderModelItems.filter((item) => {
        const haystack = `${item.id || ''} ${item.label || ''}`.toLocaleLowerCase();
        return !query || haystack.includes(query);
      });
      select.innerHTML = models.length
        ? '<option value="">选择一个候选模型</option>' + models.map((item) => {
          const suffix = window.modelDiscoveryValidationState.optionSuffix(item.id);
          return `<option value="${escapeHtml(item.id)}">${escapeHtml(`${item.label || item.id}${suffix}`)}</option>`;
        }).join('')
        : '<option value="">没有匹配的模型</option>';
      if (models.some((item) => item.id === previous)) select.value = previous;
      select.disabled = !models.length;
      $('discoveredProviderModelSearchSummary').textContent = query
        ? `筛选到 ${models.length} 个模型。`
        : `已读取 ${discoveredProviderModelItems.length} 个模型。`;
      $('validateDiscoveredModelBtn').disabled = !selectedDiscoveredModel();
      updateDiscoveredValidationSummary();
    }
    function resetModelDiscovery({ editable = true } = {}) {
      ensureModelDiscoveryControls();
      $('modelCatalogDiscovery').classList.toggle('hidden', !editable);
      $('discoveredProviderModelsLine').classList.add('hidden');
      discoveredProviderModelItems = [];
      window.modelDiscoveryValidationState.clear();
      $('discoveredProviderModelSearch').value = '';
      $('discoveredProviderModelSearch').disabled = true;
      $('discoveredProviderModelSearchSummary').textContent = '先读取模型目录。';
      $('discoveredProviderModels').innerHTML = '<option value="">先读取目录</option>';
      $('discoveredProviderModels').disabled = true;
      $('validateDiscoveredModelBtn').disabled = true;
      $('applyDiscoveredModelBtn').disabled = true;
      $('discoveredProviderValidationSummary').textContent = '选择候选模型后，才能查看本页实时验证状态。';
      setModelDiscoveryStatus(editable ? '先实时读取连接上报的候选目录。目录项不能直接当作可用模型。' : '编辑已有模型不会重写其接口模型名。');
    }
    function selectedDiscoveredModel() {
      return $('discoveredProviderModels').value.trim();
    }
    async function discoverProviderModels() {
      const providerId = $('modelCatalogProvider').value;
      if (!providerId) throw new Error('请先选择已保存的连接。');
      const button = $('discoverProviderModelsBtn');
      button.disabled = true;
      button.textContent = '读取中…';
      setModelDiscoveryStatus('正在实时读取该连接上报的候选目录…');
      try {
        const result = await bridge('/assistant/models/discover', { method: 'POST', body: JSON.stringify({ provider_id: providerId }) });
        if (!result.ok) {
          setModelDiscoveryStatus(`读取失败：${window.modelDiscoveryFailureMessage(result)}`, 'error');
          return;
        }
        const models = result.models || [];
        discoveredProviderModelItems = models;
        window.modelDiscoveryValidationState.clear();
        $('discoveredProviderModelSearch').value = '';
        $('discoveredProviderModelSearch').disabled = !models.length;
        $('discoveredProviderModelsLine').classList.remove('hidden');
        renderDiscoveredProviderModels();
        setModelDiscoveryStatus(models.length ? `已实时读取 ${models.length} 个候选模型。目录不等于可用；选择一个后进行实时验证。` : '连接响应正常，但没有返回可选择的候选模型。', models.length ? 'ok' : 'error');
      } finally {
        button.disabled = false;
        button.textContent = '实时读取候选目录';
      }
    }
    async function validateDiscoveredModel() {
      const providerId = $('modelCatalogProvider').value;
      const model = selectedDiscoveredModel();
      if (!providerId || !model) throw new Error('请先从读取结果中选择一个模型。');
      const button = $('validateDiscoveredModelBtn');
      button.disabled = true;
      button.textContent = '实时验证中…';
      setModelDiscoveryStatus(`正在对 ${model} 发起实时、隔离验证请求…`);
      try {
        const result = await bridge('/assistant/models/discover', { method: 'POST', body: JSON.stringify({ action: 'validate', provider_id: providerId, model, user_prompt: '请只回复 OK', max_tokens: 256 }) });
        const record = { ok: Boolean(result.ok), validatedAt: result.validated_at || new Date().toISOString(), message: result.ok ? '' : window.modelValidationFailureMessage(result) };
        window.modelDiscoveryValidationState.record(model, record);
        renderDiscoveredProviderModels({ preserveSelection: true });
        if (!record.ok) { setModelDiscoveryStatus(`验证失败：${window.modelValidationFailureMessage(result)}`, 'error'); return; }
        setModelDiscoveryStatus(`实时验证通过：${model}（服务器时间 ${window.modelDiscoveryValidationState.formatTime(record.validatedAt)}）。现在可以填入表单并保存；路由仍需单独确认。`, 'ok');
      } catch (_error) {
        window.modelDiscoveryValidationState.record(model, { ok: false, validatedAt: new Date().toISOString(), message: '验证请求未完成，请检查连接后重试。' });
        renderDiscoveredProviderModels({ preserveSelection: true });
        setModelDiscoveryStatus('验证请求未完成，请检查连接后重试。', 'error');
      } finally {
        button.disabled = false;
        button.textContent = '实时验证所选模型';
      }
    }
    function applyDiscoveredModel() {
      const model = selectedDiscoveredModel();
      if (!model) return;
      $('modelCatalogName').value = model;
      if (!$('modelCatalogLabel').value.trim()) $('modelCatalogLabel').value = model;
      if (!$('modelCatalogId').value.trim()) $('modelCatalogId').value = `${$('modelCatalogProvider').value}-${model}`.toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 64);
      $('saveModelCatalogBtn').textContent = '保存模型（有修改）';
      setModelDiscoveryStatus(`已填入 ${model}；保存后才会写入目录，路由不会自动改变。`, 'ok');
      // 填写后把焦点交给可编辑名称，键盘用户无需回到顶部寻找下一步。WCAG 2.2 - 2.4.3 Focus Order.
      $('modelCatalogLabel').focus();
    }

    async function loadModelRegistry() {
      const providerEditorOpen = $('modelProviderEditor').open;
      const providerDraft = providerEditorOpen ? {
        template: $('modelProviderTemplate').value,
        id: $('modelProviderId').value,
        name: $('modelProviderName').value,
        kind: $('modelProviderKind').value,
        transport: $('modelProviderTransport').value,
        billing: $('modelProviderBilling').value,
        baseUrl: $('modelProviderBaseUrl').value,
        apiKey: $('modelProviderApiKey').value,
        clearApiKey: $('modelProviderClearKey').checked,
        timeout: $('modelProviderTimeout').value,
        enabled: $('modelProviderEnabled').checked,
        trusted: $('modelProviderTrusted').checked,
        executorProfileName: $('modelExecutorProfileName').value,
        executorEnabled: $('modelExecutorEnabled').checked,
        executorUpstreamProvider: $('modelExecutorUpstreamProvider').value,
        executorUpstreamModel: $('modelExecutorUpstreamModel').value,
      } : null;
      const result = await bridge('/assistant/models');
      state.modelProviders = result.providers || [];
      state.modelCatalog = result.models || [];
      state.modelRoles = result.roles || [];
      state.modelRuntimeInventories = result.runtime_inventories || [];
      state.modelConnectionTemplates = result.connection_templates || [];
      const selectedTemplate = $('modelProviderTemplate').value;
      $('modelProviderTemplate').innerHTML = '<option value="">自定义连接</option>' + state.modelConnectionTemplates.map((item) => (
        `<option value="${escapeHtml(item.key)}">${escapeHtml(item.label)}</option>`
      )).join('');
      if (state.modelConnectionTemplates.some((item) => item.key === selectedTemplate)) {
        $('modelProviderTemplate').value = selectedTemplate;
      }
      renderModelRegistry();
      if (providerDraft) {
        $('modelProviderTemplate').value = providerDraft.template;
        $('modelProviderId').value = providerDraft.id;
        $('modelProviderName').value = providerDraft.name;
        $('modelProviderKind').value = providerDraft.kind;
        $('modelProviderTransport').value = providerDraft.transport;
        $('modelProviderBilling').value = providerDraft.billing;
        $('modelProviderBaseUrl').value = providerDraft.baseUrl;
        $('modelProviderApiKey').value = providerDraft.apiKey;
        $('modelProviderClearKey').checked = providerDraft.clearApiKey;
        $('modelProviderTimeout').value = providerDraft.timeout;
        $('modelProviderEnabled').checked = providerDraft.enabled;
        $('modelProviderTrusted').checked = providerDraft.trusted;
        $('modelExecutorProfileName').value = providerDraft.executorProfileName;
        $('modelExecutorEnabled').checked = providerDraft.executorEnabled;
        syncExecutorProfileFields(state.modelProviders.find((item) => item.id === providerDraft.id)?.executor_profile || null);
        $('modelExecutorUpstreamProvider').value = providerDraft.executorUpstreamProvider;
        syncExecutorUpstreamModels(providerDraft.executorUpstreamModel);
        $('modelProviderEditor').open = true;
      }
      setModelWorkspace(state.modelWorkspace, { load: false });
      await loadModelWorkspaceDetails(state.modelWorkspace);
    }

    async function loadModelUsage() {
      state.modelUsage = await bridge(`/assistant/models/usage?days=${encodeURIComponent($('modelUsageDays').value || '7')}`);
      renderModelUsage();
    }

    async function loadCodexOperations() {
      state.codexOperations = await bridge('/system/codex');
      renderCodexOperations();
    }

    async function saveModelProvider() {
      const result = await bridge('/assistant/models/provider', { method: 'POST', body: JSON.stringify({
        id: $('modelProviderId').value.trim(), name: $('modelProviderName').value.trim(), kind: $('modelProviderKind').value,
        transport: $('modelProviderTransport').value, billing_scope: $('modelProviderBilling').value,
        base_url: $('modelProviderBaseUrl').value.trim(), api_key: $('modelProviderApiKey').value.trim(),
        clear_api_key: $('modelProviderClearKey').checked,
        timeout_seconds: Number($('modelProviderTimeout').value || 60), enabled: $('modelProviderEnabled').checked ? '1' : '0',
        trusted_for_executor: $('modelProviderTrusted').checked ? '1' : '0',
        executor_profile_name: $('modelExecutorProfileName').value.trim(),
        executor_enabled: $('modelExecutorEnabled').checked ? '1' : '0',
        executor_adapter_type: 'codex_cli_profile',
        executor_credential_source: 'proxy_access_key',
        executor_upstream_provider_id: $('modelExecutorUpstreamProvider').value,
        executor_upstream_model_id: $('modelExecutorUpstreamModel').value,
      }) });
      $('modelProviderApiKey').value = '';
      state.modelProviders = result.providers || []; state.modelCatalog = result.models || []; state.modelRoles = result.roles || [];
      renderModelRegistry();
      if (!result.ok) {
        const failure = (result.executor_apply || []).find((item) => !item.ok);
        throw new Error(`连接已保存，但执行器应用失败：${failure?.error || '请检查运行状态'}`);
      }
      $('modelProviderEditorStatus').className = 'provider-status ok';
      $('modelProviderEditorStatus').textContent = (result.executor_apply || []).length ? '连接已保存，执行器配置已应用。' : '连接已保存。';
      $('modelProviderEditor').open = false;
      setConnection('连接已保存。', 'ok');
    }

    async function saveModelCatalog() {
      const result = await bridge('/assistant/models/model', { method: 'POST', body: JSON.stringify({
        id: $('modelCatalogId').value.trim(), provider_id: $('modelCatalogProvider').value, label: $('modelCatalogLabel').value.trim(),
        model: $('modelCatalogName').value.trim(), context_window: Number($('modelContextWindow').value || 0),
        max_output_tokens: Number($('modelMaxOutput').value || 900), supports_tools: $('modelSupportsTools').checked ? '1' : '0',
        capabilities: [
          $('modelCapabilityText').checked && 'text', $('modelSupportsTools').checked && 'tools',
          $('modelCapabilityVision').checked && 'vision', $('modelCapabilityEmbedding').checked && 'embedding',
          $('modelCapabilityStructured').checked && 'structured_output',
        ].filter(Boolean),
        enabled: $('modelCatalogEnabled').checked ? '1' : '0', notes: $('modelCatalogNotes').value.trim(),
        input_price_per_million: $('modelInputPrice').value, output_price_per_million: $('modelOutputPrice').value,
        price_currency: $('modelPriceCurrency').value.trim() || 'USD', price_source: $('modelPriceSource').value.trim(),
      }) });
      state.modelProviders = result.providers || []; state.modelCatalog = result.models || []; state.modelRoles = result.roles || [];
      renderModelRegistry(); $('modelCatalogEditor').open = false; setConnection('模型目录已保存。', 'ok');
    }

    async function bindRuntimeRole(role) {
      const primary = document.querySelector(`[data-role-primary="${CSS.escape(role)}"]`);
      const fallback = document.querySelector(`[data-role-fallback="${CSS.escape(role)}"]`);
      const result = await bridge('/assistant/models/bind', { method: 'POST', body: JSON.stringify({ role, primary_model_id: primary?.value || '', fallback_model_id: fallback?.value || '' }) });
      state.modelProviders = result.providers || []; state.modelCatalog = result.models || []; state.modelRoles = result.roles || [];
      renderModelRegistry(); setConnection(`${role} 角色已绑定。`, 'ok');
    }

    async function testRegistryModel(modelId) {
      const result = await bridge('/assistant/models/test', { method: 'POST', body: JSON.stringify({ model_id: modelId }) });
      setConnection(result.ok ? `模型测试通过：${result.provider_label || result.provider} ${result.model || ''}` : `模型测试失败：${result.error || result.error_kind}`, result.ok ? 'ok' : 'error');
      await loadModelRegistry();
    }

    function openModelPlaygroundForModel(modelId) {
      const item = state.modelCatalog.find((entry) => entry.id === modelId);
      if (!item) return;
      state.selectedModelProvider = item.provider_id;
      setModelWorkspace('playground', { load: false });
      renderModelPlaygroundOptions(modelId);
      $('modelPlaygroundModel').value = modelId;
      $('modelPlaygroundTitle').focus?.();
      setConnection(`验证台已锁定：${item.label || item.model || item.id} · ${item.provider_name || item.provider_id}`, 'ok');
    }

    function applyModelProviderTemplate(key) {
      const item = state.modelConnectionTemplates.find((entry) => entry.key === key);
      if (!item) {
        $('modelTemplateHint').textContent = '自定义连接不会绑定任何平台固定资产。';
        return;
      }
      $('modelProviderKind').value = item.kind;
      $('modelProviderTransport').value = item.transport;
      $('modelProviderBilling').value = item.billing_scope;
      $('modelProviderTrusted').checked = Boolean(item.trusted_for_executor);
      if (!$('modelProviderId').value.trim() || !$('modelProviderBaseUrl').value.trim()) $('modelProviderBaseUrl').value = item.base_url || '';
      if (!$('modelProviderName').value.trim()) $('modelProviderName').value = item.label;
      $('modelTemplateHint').textContent = item.description || '模板只预填字段，保存后仍可自由编辑或删除。';
      syncExecutorProfileFields();
    }

    function dependencyMessage(error, noun) {
      const dependencies = error?.payload?.dependencies || {};
      const roles = dependencies.roles || [];
      const models = dependencies.models || [];
      if (roles.length) return `${noun}仍被角色 ${roles.join('、')} 使用，请先修改角色路由。`;
      if (models.length) return `${noun}下仍有模型 ${models.join('、')}，请先删除这些模型。`;
      return error.message || String(error);
    }

    async function deleteCurrentModelProvider() {
      const id = $('modelProviderId').value.trim();
      if (!id) return;
      const item = state.modelProviders.find((entry) => entry.id === id);
      if (!window.confirm(`删除连接“${item?.name || id}”？其凭证会一并删除，且无法撤销。`)) return;
      try {
        await bridge('/assistant/models/provider/delete', { method: 'POST', body: JSON.stringify({ id }) });
        $('modelProviderEditor').open = false;
        await loadModelRegistry();
        setConnection('连接已删除。', 'ok');
      } catch (error) {
        setConnection(dependencyMessage(error, '连接'), 'error');
      }
    }

    async function deleteCurrentCatalogModel() {
      const id = $('modelCatalogId').value.trim();
      if (!id) return;
      const item = state.modelCatalog.find((entry) => entry.id === id);
      if (!window.confirm(`删除模型“${item?.label || id}”？此操作无法撤销。`)) return;
      try {
        await bridge('/assistant/models/model/delete', { method: 'POST', body: JSON.stringify({ id }) });
        $('modelCatalogEditor').open = false;
        await loadModelRegistry();
        setConnection('模型已删除。', 'ok');
      } catch (error) {
        setConnection(dependencyMessage(error, '模型'), 'error');
      }
    }

    function bindModelControlEvents() {
      bindModelPlaygroundEvents();
      ensureModelDiscoveryControls();
      $('reloadModelRegistryBtn').addEventListener('click', loadModelRegistry);
      $('modelProviderNewBtn').addEventListener('click', () => { setModelWorkspace('connections'); editModelProvider(); });
      $('modelCatalogNewBtn').addEventListener('click', () => { setModelWorkspace('catalog'); editCatalogModel(); });
      $('cancelModelProviderBtn').addEventListener('click', () => { $('modelProviderEditor').open = false; });
      $('cancelModelCatalogBtn').addEventListener('click', () => { $('modelCatalogEditor').open = false; $('modelPricingEditor').open = false; });
      document.querySelectorAll('[data-model-workspace]').forEach((button) => {
        button.addEventListener('click', () => setModelWorkspace(button.dataset.modelWorkspace));
      });
      document.querySelectorAll('[data-model-catalog-view]').forEach((button) => {
        button.addEventListener('click', () => setModelCatalogView(button.dataset.modelCatalogView));
      });
      $('saveModelProviderBtn').textContent = '保存连接';
      $('saveModelCatalogBtn').textContent = '保存模型';
      $('modelProviderEditor').addEventListener('input', () => { $('saveModelProviderBtn').textContent = '保存连接（有修改）'; });
      $('modelCatalogEditor').addEventListener('input', () => { $('saveModelCatalogBtn').textContent = '保存模型（有修改）'; });
      $('modelPricingEditor').addEventListener('input', () => { $('saveModelCatalogBtn').textContent = '保存模型（有修改）'; });
      $('saveModelProviderBtn').addEventListener('click', async () => {
        const button = $('saveModelProviderBtn'); button.disabled = true; button.textContent = '保存中…';
        try { await saveModelProvider(); button.textContent = '已保存'; setConnection(`API 连接已保存 · ${new Date().toLocaleTimeString()}`, 'ok'); }
        catch (error) { button.textContent = '保存失败，重试'; setConnection(error.message || String(error), 'error'); }
        finally { button.disabled = false; }
      });
      $('saveModelCatalogBtn').addEventListener('click', async () => {
        const button = $('saveModelCatalogBtn'); button.disabled = true; button.textContent = '保存中…';
        try { await saveModelCatalog(); button.textContent = '已保存'; setConnection(`模型已保存 · ${new Date().toLocaleTimeString()}`, 'ok'); }
        catch (error) { button.textContent = '保存失败，重试'; setConnection(error.message || String(error), 'error'); }
        finally { button.disabled = false; }
      });
      $('reloadModelUsageBtn').addEventListener('click', () => loadModelUsage().catch((error) => setConnection(error.message || String(error), 'error')));
      $('modelUsageDays').addEventListener('change', () => loadModelUsage().catch((error) => setConnection(error.message || String(error), 'error')));
      $('reloadCodexOperationsBtn').addEventListener('click', () => loadCodexOperations().catch((error) => setConnection(error.message || String(error), 'error')));
      $('modelRoleRows').addEventListener('click', (event) => {
        const button = event.target.closest('[data-role-save]');
        if (button) bindRuntimeRole(button.dataset.roleSave).catch((error) => setConnection(error.message || String(error), 'error'));
      });
      $('modelProviderRows').addEventListener('click', (event) => {
        const select = event.target.closest('[data-model-provider-select]');
        const edit = event.target.closest('[data-model-provider-edit]');
        const manage = event.target.closest('[data-model-provider-models]');
        const test = event.target.closest('[data-model-provider-test]');
        if (select) {
          state.selectedModelProvider = select.dataset.modelProviderSelect;
          renderModelRegistry();
        }
        if (manage) {
          state.selectedModelProvider = manage.dataset.modelProviderModels;
          setModelCatalogView('models');
          renderModelRegistry();
        }
        if (edit) editModelProvider(edit.dataset.modelProviderEdit);
        if (test && !test.disabled) testRegistryModel(test.dataset.modelProviderTest).catch((error) => setConnection(error.message || String(error), 'error'));
      });
      $('modelTemplateGallery').addEventListener('click', (event) => {
        const button = event.target.closest('[data-model-template-start]');
        if (!button) return;
        editModelProvider();
        $('modelProviderTemplate').value = button.dataset.modelTemplateStart;
        applyModelProviderTemplate(button.dataset.modelTemplateStart);
      });
      $('modelCatalogRows').addEventListener('click', (event) => {
        const edit = event.target.closest('[data-model-edit]');
        const test = event.target.closest('[data-model-test]');
        if (edit) editCatalogModel(edit.dataset.modelEdit);
        if (test && !test.disabled) openModelPlaygroundForModel(test.dataset.modelTest);
      });
      $('openModelRoutingBtn').addEventListener('click', () => { switchView('models', { focusHeading: true }); setModelWorkspace('routing'); });
      $('modelProviderTemplate').addEventListener('change', () => applyModelProviderTemplate($('modelProviderTemplate').value));
      $('deleteModelProviderBtn').addEventListener('click', deleteCurrentModelProvider);
      $('deleteModelCatalogBtn').addEventListener('click', deleteCurrentCatalogModel);
      $('discoverProviderModelsBtn').addEventListener('click', () => discoverProviderModels().catch((error) => setModelDiscoveryStatus(error.message || String(error), 'error')));
      $('validateDiscoveredModelBtn').addEventListener('click', () => validateDiscoveredModel().catch((error) => setModelDiscoveryStatus(error.message || String(error), 'error')));
      $('applyDiscoveredModelBtn').addEventListener('click', applyDiscoveredModel);
      $('discoveredProviderModelSearch').addEventListener('input', () => renderDiscoveredProviderModels({ preserveSelection: true }));
      $('discoveredProviderModels').addEventListener('change', () => {
        const selected = Boolean(selectedDiscoveredModel());
        $('validateDiscoveredModelBtn').disabled = !selected;
        updateDiscoveredValidationSummary();
        if (selected) setModelDiscoveryStatus('候选目录不等于可用模型；请对所选项发起实时验证。');
      });
      $('modelCatalogProvider').addEventListener('change', () => resetModelDiscovery());
      $('modelProviderKind').addEventListener('change', () => {
        const kind = $('modelProviderKind').value;
        const template = state.modelConnectionTemplates.find((item) => item.kind === kind);
        if (template) {
          $('modelProviderTransport').value = template.transport;
          $('modelProviderBilling').value = template.billing_scope;
        }
        syncExecutorProfileFields();
      });
      $('modelProviderTransport').addEventListener('change', () => syncExecutorProfileFields());
      $('modelExecutorUpstreamProvider').addEventListener('change', () => syncExecutorUpstreamModels());
      $('verifyExecutorWorkModeBtn').addEventListener('click', () => verifyExecutorWorkMode().catch((error) => setConnection(error.message || String(error), 'error')));
      $('modelProviderId').addEventListener('input', () => {
        if (isCustomCodexTransport() && !$('modelExecutorProfileName').value.trim()) {
          $('modelExecutorProfileName').value = $('modelProviderId').value.trim();
        }
      });
    }
