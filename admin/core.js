    (() => {
      const initial = document.querySelector('link[rel="stylesheet"][href*="/admin/static/admin.css"]');
      if (!initial) return;
      let attempts = 0;
      const ensureLoaded = () => {
        if (initial.sheet || document.querySelector('link[data-admin-css-ready="1"]')?.sheet || attempts >= 2) return;
        attempts += 1;
        const replacement = document.createElement('link');
        replacement.rel = 'stylesheet';
        replacement.dataset.adminCssReady = '1';
        const url = new URL(initial.href, window.location.href);
        url.searchParams.set('retry', `${Date.now()}-${attempts}`);
        replacement.href = url.toString();
        replacement.addEventListener('error', () => window.setTimeout(ensureLoaded, 250 * attempts), { once: true });
        document.head.appendChild(replacement);
      };
      initial.addEventListener('error', ensureLoaded, { once: true });
      window.setTimeout(ensureLoaded, 1200);
    })();

    const statusOrder = ['waiting_approval', 'queued', 'running', 'done', 'failed', 'timeout', 'cancelled'];
    const statusLabels = {
      waiting_approval: '等待确认',
      queued: '排队中',
      running: '运行中',
      done: '已完成',
      failed: '失败',
      timeout: '超时',
      cancelled: '已取消',
    };
    const metricLabels = {
      total: '任务总数',
      active: '活跃任务',
      queued: '排队中',
      running: '运行中',
      done: '已完成',
      failed: '失败',
      timeout: '超时',
      cancelled: '已取消',
    };
    const errorMessages = {
      invalid_token: '访问凭证不正确。',
      too_many_login_attempts: '登录失败次数过多，请稍后再试。',
      forbidden: '未登录或会话已过期。',
      invalid_background_url: '背景 URL 必须是 http 或 https 地址。',
      background_url_too_long: '背景 URL 过长。',
      token_whitespace_not_allowed: 'Token 首尾不能有空格。',
      token_confirmation_mismatch: '两次输入的 Token 不一致。',
      token_length_invalid: 'Token 长度必须为 8–256 位。',
      token_characters_invalid: 'Token 只能包含英文字母、数字或 . _ ~ -。',
      token_unchanged: '新 Token 与当前 Token 相同，无需保存。',
      token_matches_channel_secret: '管理 Token 不能与 QQ 渠道 Token 相同。',
      'args.new_token_invalid': 'Token 必须为 8–256 位，且只能包含英文字母、数字或 . _ ~ -。',
      admin_token_matches_channel_token: '管理 Token 不能与 QQ 渠道 Token 相同。',
      token_logout_confirmation_required: '请先确认保存后会退出登录。',
      token_path_symlink_not_allowed: 'Token 文件路径不安全，已拒绝写入。',
      token_write_failed: 'Token 写入失败，请检查服务器文件权限。',
      console_update_required: '控制台已更新，请重新载入页面后登录。',
      plugin_market_unavailable: '插件市场暂时不可用，请稍后重试。',
      plugin_market_risk_confirmation_required: '请先确认第三方插件的运行风险。',
      plugin_market_operation_failed: '插件操作失败；如有备份，系统会优先回滚。',
      plugin_not_in_trusted_market: '该插件不在当前可信市场目录中。',
      approval_not_pending: '这项操作已经处理，页面将刷新最新状态。',
      approval_version_conflict: '审批内容已更新，请审阅最新版本。',
      approval_expired: '这项确认已经过期，任务未执行。',
      approval_action_changed: '待执行动作已经变化，原批准已失效。',
      approval_task_state_changed: '任务状态已经变化，原批准已失效。',
      approval_timeout_out_of_range: '超时秒数必须在 30 到 900 之间。',
      formal_approval_disabled: '正式审批尚未启用。',
      artifact_preview_disabled: '成品中心尚未启用。',
      artifact_not_found: '没有找到这项成品，列表将刷新。',
      artifact_version_not_available: '这个版本目前不可下载或修改。',
      artifact_revision_instruction_invalid: '请输入 1–8000 字的修改要求。',
      artifact_current_version_conflict: '成品已产生新版本，请刷新后重试。',
      preview_publication_version_conflict: '预览状态已变化，请刷新后重试。',
      preview_publication_not_active: '预览当前未开放，请刷新后查看最新状态。',
      artifact_preview_base_url_missing: '尚未配置独立预览来源。',
      relationship_proactive_feature_disabled: '关系与主动行为尚未完成 Gate 8 切换，当前保持只读。',
      idempotency_key_required: '请求缺少幂等键，请刷新页面后重试。',
      idempotency_key_payload_conflict: '同一请求键对应了不同内容，请刷新页面后重试。',
      stale_relationship_version: '互动关系已经被其他页面更新，请重新载入。',
      stale_notification_policy_version: '通知规则已经被其他页面更新，请重新载入。',
      stale_social_proactive_policy_version: '社交主动规则已经被其他页面更新，请重新载入。',
      relationship_topic_conflict: '同一话题不能同时出现在允许和禁止列表。',
      notification_category_required: '至少保留一类任务或安全通知。',
      explicit_authorization_required: '启用社交主动前必须取得用户明确授权。',
      proactive_intent_required: '至少选择一种有真实理由的主动意图。',
      stale_routing_preset_preview: '模型连接或路由已变化，请重新生成预览。',
      routing_preset_unavailable: '当前连接和模型不能满足这套路由预设。',
      unknown_routing_preset: '没有找到这套路由预设。',
      protected_plugin_cannot_be_uninstalled: '核心插件不能从市场卸载。',
      pet_pack_not_found: '没有找到这个 PetPack。',
      invalid_pet_scale: '桌宠缩放必须在 0.5 到 1.8 之间。',
      unsupported_pet_asset_type: '只支持 PNG、WebP 或 GIF 桌宠资源。',
      invalid_pet_asset_size: '桌宠资源为空或超过 5 MB。',
      pet_asset_signature_mismatch: '文件内容与声明的图片格式不一致。',
      invalid_pet_manifest: '动画 manifest 格式或状态定义无效。',
      pet_manifest_dimensions_mismatch: '动画 manifest 的图集尺寸与图片实际尺寸不一致。',
      pet_delete_confirmation_required: '删除自定义 PetPack 前必须明确确认。',
      builtin_pet_pack_protected: '内建 PetPack 不能删除。',
      project_already_exists: '同名项目已经存在；原项目没有被修改。',
      project_name_already_exists: '这个项目名称已经被使用，请换一个名称。',
      project_path_already_registered: '这个工作目录已经属于另一个项目。',
      project_current_archive_forbidden: '当前项目不能直接归档，请先切换到其他项目。',
      project_archive_confirmation_required: '归档前必须明确确认。',
      project_expected_updated_at_required: '项目版本信息缺失，请刷新后重试。',
      project_stale: '项目已被其他页面更新，请刷新后重新操作。',
      project_not_found: '没有找到该项目，项目列表将刷新。',
      project_already_archived: '项目已经归档。',
      project_already_active: '项目已经恢复。',
      project_path_outside_allowed_roots: '工作目录不在服务器允许的项目根目录内。',
      assistant_version_required: '当前人格版本信息缺失，请重新载入后再保存。',
      assistant_version_conflict: '人格版本已在其他页面更新。当前草稿没有被覆盖，请重新载入最新版本后再确认。',
      active_assistant_missing: '当前没有可编辑的 Assistant Instance。',
      unsupported_voice_contract_schema: 'Voice Contract 版本不受支持，运行时已回退到安全中性配置。',
    };
    const SAMPLE_BACKGROUND_URL = '/admin/assets/sample-background.jpg';
    const viewTitles = {
      overview: '日常空间',
      tasks: '任务中心',
      artifacts: '成品中心',
      automations: '自动化',
      projects: '项目空间',
      assistant: '知识与记忆',
      brain: '身份与表达',
      growth: '学习',
      relationship: '关系与主动',
      social: '社交体验',
      models: '模型与 Provider',
      capabilities: '工具与 Skill',
      proxy: '网络代理',
      services: '运行环境',
      qq: 'QQ 渠道',
      logs: '运行日志',
      settings: '系统设置',
    };
    const navigationGroups = {
      workspace: {
        label: '工作',
        primaryViews: ['tasks', 'artifacts'],
        secondaryViews: ['projects', 'automations'],
        secondaryLabel: '管理',
      },
      agent: {
        label: '助手',
        primaryViews: ['assistant', 'relationship', 'brain', 'growth'],
        secondaryViews: ['models', 'capabilities'],
        secondaryLabel: '执行配置',
      },
      system: {
        label: '系统与连接',
        primaryViews: ['qq', 'proxy', 'services'],
        secondaryViews: ['settings', 'social', 'logs'],
        secondaryLabel: '高级设置',
      },
    };
    const viewGroups = Object.fromEntries(
      Object.entries(navigationGroups).flatMap(([group, config]) => (
        [...config.primaryViews, ...(config.secondaryViews || [])].map((view) => [view, group])
      )),
    );
    const ADMIN_BUILD = document.querySelector('meta[name="admin-build"]')?.content || '';
    const viewFreshnessMs = window.AdminViewConfig?.freshness || {};
    const state = {
      authenticated: false,
      activeView: 'overview',
      lastTaskId: '',
      lastTasks: [],
      projects: [],
      currentProject: null,
      assistantSettings: {},
      personaWorkspace: null,
      pendingPersonaWorkspace: null,
      providerPresets: [],
      modelProfiles: [],
      memeAssets: [], memeHealth: null,
      memeDiscovery: { jobs: [], candidates: [], providers: [], counts: {} },
      memeDiscoveryLoaded: false,
      socialWorkspace: 'groups',
      proactivePlans: [],
      automationJobs: [],
      automationRuns: [],
      proactivePolicies: [],
      proactiveMessagingPolicies: [],
      proactiveEvents: [],
      proactiveReviews: [],
      automationOverview: null,
      expressionHabits: [],
      groupPolicies: [],
      qqGroupAccess: null,
      modelProviders: [],
      modelConnectionTemplates: [],
      modelCatalog: [],
      modelRoles: [],
      modelRuntimeInventories: [],
      modelMigrationPreview: null,
      modelCatalogView: 'providers',
      modelWorkspace: 'connections',
      modelUsage: { summary: {}, by_model: [], events: [] },
      codexOperations: {},
      selectedModelProvider: '',
      capabilityPlugins: [],
      capabilitySkills: [],
      capabilityManifests: [],
      networkPolicy: null,
      networkPolicyEvents: [],
      pluginMarketplace: [],
      pluginMarketSources: [],
      pluginMarketOperations: [],
      pluginMarketMeta: {},
      selectedMarketPlugin: '',
      capabilityWorkspace: 'market',
      pluginMarketQuery: '',
      pluginMarketCategory: '',
      pluginMarketPage: 1,
      pluginMarketPageSize: 9,
      executionOverview: null,
      appearance: {},
      pet: { enabled: false, packs: [] },
      memories: [],
      memoryCandidates: [],
      knowledgeItems: [],
      knowledgeWorkspace: null,
      conversationThreads: [],
      qualityEvents: [],
      modeSessions: [],
      proxyDiagnostics: null,
      proxyGroups: [],
      proxyActiveGroup: 'Proxies',
      proxyDelays: {},
      proxyAiResults: {},
      proxyConfig: {},
      proxySubscriptions: {},
      proxySort: 'default',
      qqPollTimer: null,
      authEpoch: 0,
      refreshInFlight: false,
      viewRefreshPromises: new Map(),
      viewRefreshTimer: null,
      activeViewRefreshTimer: null,
      qqDiagnosticsPromise: null,
      versionCheckTimer: null,
      versionCheckStartTimer: null,
      petRuntimeTimer: null,
      viewLoadedAt: {},
      collectionBrowsers: new Map(),
    };
    const featureAssetsByView = window.AdminViewConfig?.assets || {};
    const featureAssetPromises = new Map();
    const initializedFeatureAssets = new Set();
    const bridgeGetRequests = new Map();

    function featureAssetUrl(name) {
      return `/admin/static/${name}?v=${encodeURIComponent(ADMIN_BUILD)}`;
    }

    function initializeFeatureAsset(name) {
      if (initializedFeatureAssets.has(name)) return;
      initializedFeatureAssets.add(name);
      if (name === 'views-models.js') {
        bindModelControlEvents();
        setModelCatalogView(state.modelCatalogView || 'providers');
      } else if (name === 'views-workspace.js') {
        window.bindWorkspaceEvents?.();
      } else if (name === 'views-persona.js') {
        window.bindPersonaEvents?.();
      } else if (name === 'views-knowledge.js') {
        window.bindKnowledgeEvents?.();
      } else if (name === 'views-gate8.js') {
        bindGate8Events();
      } else if (name === 'components/qq-access-editor.js') {
        window.bindQqAccessEvents?.();
      } else if (name === 'views-projects.js') {
        window.bindProjectLifecycleEvents?.();
      } else if (name === 'views-pets.js' && !state.petEventsBound) {
        bindPetEvents();
        state.petEventsBound = true;
      }
    }

    function loadFeatureAsset(name) {
      if (featureAssetPromises.has(name)) return featureAssetPromises.get(name);
      const existing = document.querySelector(`script[data-admin-feature="${name}"]`);
      if (existing?.dataset.loaded === 'true') {
        initializeFeatureAsset(name);
        return Promise.resolve();
      }
      const promise = new Promise((resolve, reject) => {
        const isStyle = name.endsWith('.css');
        const asset = existing || document.createElement(isStyle ? 'link' : 'script');
        asset.dataset.adminFeature = name;
        if (isStyle) {
          asset.rel = 'stylesheet';
          asset.href = featureAssetUrl(name);
        } else {
          // Parallel fetch, manifest-ordered execution.
          asset.async = false;
          asset.src = featureAssetUrl(name);
        }
        asset.addEventListener('load', () => {
          asset.dataset.loaded = 'true';
          if (!isStyle) initializeFeatureAsset(name);
          resolve();
        }, { once: true });
        asset.addEventListener('error', () => reject(new Error(`功能资源加载失败：${name}`)), { once: true });
        if (!existing) document.head.appendChild(asset);
      });
      featureAssetPromises.set(name, promise);
      return promise;
    }

    async function ensureViewFeatures(view) {
      const assets = featureAssetsByView[view] || [];
      await Promise.all(assets.map((asset) => loadFeatureAsset(asset)));
    }

    async function ensurePetRuntime() {
      await loadFeatureAsset('views-pets.js');
    }

    function cancelPetRuntimeSchedule() {
      window.clearTimeout(state.petRuntimeTimer);
      state.petRuntimeTimer = null;
    }

    function schedulePetRuntime() {
      if (state.petRuntimeTimer || typeof loadPetState === 'function') return;
      state.petRuntimeTimer = window.setTimeout(async () => {
        state.petRuntimeTimer = null;
        if (!state.authenticated || typeof loadPetState === 'function') return;
        try {
          await ensurePetRuntime();
          await window.loadPetState?.();
        } catch (_) {}
      }, 8000);
    }

    const $ = (id) => document.getElementById(id);
    const escapeHtml = (value) => String(value ?? '')
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');

    function setText(id, value) {
      const node = $(id);
      if (node) {
        node.textContent = value;
      }
    }

    function setBadge(id, value, tone = '') {
      const node = $(id);
      if (node) {
        node.textContent = value;
        node.className = 'badge' + (tone ? ' ' + tone : '');
      }
    }

    function initializeAccessibility() {
      ['connection', 'loginMessage', 'appearanceStatus', 'proxyStatus', 'proxyIpStatus', 'proxySubStatus'].forEach((id) => {
        const node = $(id);
        if (node) {
          node.setAttribute('role', 'status');
          node.setAttribute('aria-live', 'polite');
        }
      });
    }

    function setConnection(text, kind = '') {
      const node = $('connection');
      const loginNode = $('loginMessage');
      if (node) {
        node.textContent = text;
        node.className = 'connection' + (kind ? ' ' + kind : '');
      }
      if (loginNode && !state.authenticated) {
        loginNode.textContent = text;
        loginNode.className = 'connection' + (kind ? ' ' + kind : '');
      }
      window.setPetFeedback?.(kind, text);
    }

    function clampNumber(value, fallback, min, max) {
      const number = Number(value);
      if (!Number.isFinite(number)) {
        return fallback;
      }
      return Math.max(min, Math.min(number, max));
    }

    function cssImageUrl(value) {
      const raw = String(value || '').trim();
      if (!raw) {
        return 'none';
      }
      return `url("${raw.replaceAll('\\\\', '\\\\\\\\').replaceAll('"', '\\\\"')}")`;
    }

    function updateAppearanceRangeLabels() {
      $('appearanceDimValue').textContent = Number($('appearanceDimInput').value || 0.12).toFixed(2);
      $('appearancePanelOpacityValue').textContent = Number($('appearancePanelOpacityInput').value || 0.88).toFixed(2);
    }

    function appearanceFromForm() {
      return {
        admin_background_enabled: $('appearanceBackgroundEnabled').checked ? '1' : '0',
        admin_background_url: $('appearanceBackgroundUrl').value.trim(),
        admin_background_dim: String(clampNumber($('appearanceDimInput').value, 0.12, 0, 0.96)),
        admin_panel_opacity: String(clampNumber($('appearancePanelOpacityInput').value, 0.88, 0.72, 1)),
      };
    }

    function applyAppearance(appearance = {}) {
      const enabled = String(appearance.admin_background_enabled || '0') === '1';
      const url = String(appearance.admin_background_url || '').trim();
      const dim = clampNumber(appearance.admin_background_dim, 0.12, 0, 0.96);
      const panelOpacity = clampNumber(appearance.admin_panel_opacity, 0.88, 0.72, 1);
      document.body.classList.toggle('has-custom-background', enabled && Boolean(url));
      document.documentElement.style.setProperty('--custom-bg-image', enabled && url ? cssImageUrl(url) : 'none');
      document.documentElement.style.setProperty('--custom-bg-dim', String(dim));
      document.documentElement.style.setProperty('--panel-opacity', String(panelOpacity));
      $('appearancePreview')?.style.setProperty('--appearance-preview-image', url ? cssImageUrl(url) : '');
    }

    function renderAppearance(appearance = {}, message = '外观设置已加载。', kind = 'ok') {
      const merged = Object.assign({
        admin_background_enabled: '0',
        admin_background_url: '',
        admin_background_dim: '0.12',
        admin_panel_opacity: '0.88',
        sample_background_url: SAMPLE_BACKGROUND_URL,
      }, appearance || {});
      state.appearance = merged;
      $('appearanceBackgroundEnabled').checked = String(merged.admin_background_enabled || '0') === '1';
      $('appearanceBackgroundUrl').value = merged.admin_background_url || '';
      $('appearanceDimInput').value = String(clampNumber(merged.admin_background_dim, 0.12, 0, 0.96));
      $('appearancePanelOpacityInput').value = String(clampNumber(merged.admin_panel_opacity, 0.88, 0.72, 1));
      updateAppearanceRangeLabels();
      applyAppearance(merged);
      const status = $('appearanceStatus');
      if (status) {
        status.className = 'provider-status' + (kind ? ' ' + kind : '');
        status.textContent = message;
      }
      const enabled = String(merged.admin_background_enabled || '0') === '1';
      const url = String(merged.admin_background_url || '').trim();
      setText('overviewBackgroundStatus', enabled && url ? '自定义背景已启用' : '默认背景');
    }

    async function loadAppearance() {
      try {
        const response = await fetch('/admin/appearance', { credentials: 'same-origin' });
        const payload = await response.json();
        if (!response.ok || !payload.ok) {
          throw new Error(errorMessages[payload.error] || payload.error || `HTTP ${response.status}`);
        }
        renderAppearance(payload.appearance || {}, '外观设置已加载。', 'ok');
      } catch (error) {
        renderAppearance(state.appearance, error.message || String(error), 'error');
      }
    }

    async function saveAppearance() {
      const payload = appearanceFromForm();
      try {
        $('saveAppearanceBtn').disabled = true;
        renderAppearance(payload, '正在保存外观设置。', 'pending');
        const result = await bridge('/admin/appearance', {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        renderAppearance(result.appearance || payload, '外观设置已保存。', 'ok');
        setConnection('外观设置已保存。', 'ok');
      } catch (error) {
        renderAppearance(payload, error.message || String(error), 'error');
        setConnection(error.message || String(error), 'error');
      } finally {
        $('saveAppearanceBtn').disabled = false;
      }
    }

    function previewAppearance() {
      renderAppearance(appearanceFromForm(), '正在预览，保存后才会持久生效。', 'pending');
    }

    function resetAppearance() {
      renderAppearance({
        admin_background_enabled: '0',
        admin_background_url: '',
        admin_background_dim: '0.12',
        admin_panel_opacity: '0.88',
      }, '已恢复默认预览，保存后生效。', 'pending');
    }

    function useSampleBackground() {
      $('appearanceBackgroundEnabled').checked = true;
      $('appearanceBackgroundUrl').value = state.appearance.sample_background_url || SAMPLE_BACKGROUND_URL;
      $('appearanceDimInput').value = '0.12';
      $('appearancePanelOpacityInput').value = '0.88';
      previewAppearance();
    }

    function showAuthenticated(authenticated) {
      state.authenticated = authenticated;
      $('loginShell').classList.toggle('hidden', authenticated);
      $('appShell').classList.toggle('hidden', !authenticated);
      $('sessionState').textContent = authenticated ? '已登录' : '未登录';
      if (!authenticated) {
        cancelPetRuntimeSchedule();
        $('desktopPet').hidden = true;
      }
      if (authenticated && typeof loadPetState === 'function') {
        loadPetState().catch((error) => setConnection(error.message || String(error), 'error'));
      }
    }

    function renderFixedTokenStatus(token = {}, message = '', kind = '') {
      const status = $('fixedTokenStatus');
      const meta = $('fixedTokenMeta');
      if (meta) {
        meta.textContent = token.configured ? '固定文件已配置' : '尚未配置';
      }
      if (status) {
        status.className = 'provider-status' + (kind ? ' ' + kind : '');
        const updated = token.updated_at ? ` · 最近修改 ${compactTimestamp(token.updated_at)}` : '';
        status.textContent = message || `${token.configured ? 'Token 已固定保存' : 'Token 尚未配置'}${updated}`;
      }
    }

    async function loadFixedTokenStatus() {
      try {
        const result = await bridge('/admin/security/token');
        renderFixedTokenStatus(result.token || {}, '', 'ok');
      } catch (error) {
        renderFixedTokenStatus({}, error.message || String(error), 'error');
      }
    }

    async function saveFixedToken() {
      const token = $('fixedTokenInput').value;
      const confirmation = $('fixedTokenConfirmInput').value;
      if (token !== confirmation) {
        renderFixedTokenStatus({}, errorMessages.token_confirmation_mismatch, 'error');
        $('fixedTokenConfirmInput').focus();
        return;
      }
      const tokenError = token.length < 8 || token.length > 256 ? errorMessages.token_length_invalid : (!/^[A-Za-z0-9._~-]+$/.test(token) ? errorMessages.token_characters_invalid : '');
      if (tokenError) {
        renderFixedTokenStatus({}, tokenError, 'error');
        $('fixedTokenInput').focus();
        return;
      }
      if (!$('fixedTokenLogoutConfirm').checked) {
        renderFixedTokenStatus({}, errorMessages.token_logout_confirmation_required, 'error');
        $('fixedTokenLogoutConfirm').focus();
        return;
      }
      const button = $('saveFixedTokenBtn');
      try {
        button.disabled = true;
        renderFixedTokenStatus({}, '正在安全写入固定 Token…', 'pending');
        const result = await bridge('/admin/security/token', {
          method: 'POST',
          body: JSON.stringify({
            new_token: token,
            confirm_token: confirmation,
            confirm_logout: true,
          }),
        });
        if (!result.changed) throw new Error('Token 未发生变更。');
        $('fixedTokenInput').value = '';
        $('fixedTokenConfirmInput').value = '';
        $('fixedTokenLogoutConfirm').checked = false;
        state.authEpoch += 1;
        showAuthenticated(false);
        setConnection('固定 Token 已保存。请使用新 Token 重新登录。', 'ok');
        $('tokenInput').focus();
      } catch (error) {
        renderFixedTokenStatus({}, error.message || String(error), 'error');
        setConnection(error.message || String(error), 'error');
      } finally {
        button.disabled = false;
      }
    }

    function renderViewTabs(view) {
      const tabs = $('viewTabs'), group = viewGroups[view];
      tabs.replaceChildren();
      tabs.classList.toggle('hidden', !group);
      if (!group) return;
      const config = navigationGroups[group];
      tabs.setAttribute('aria-label', `${config.label}页面`);
      const add = (v, p) => {
        const b = document.createElement('button'); b.type = 'button';
        b.dataset.view = v;
        b.textContent = viewTitles[v];
        if (v === view) b.className = 'active', b.setAttribute('aria-current', 'page');
        b.onclick = () => switchView(v, { focusHeading: true }); p.append(b);
      };
      (config.primaryViews || config.views).forEach((v) => add(v, tabs));
      const extra = config.secondaryViews || [];
      if (!extra.length) return;
      const disclosure = document.createElement('details'); disclosure.className = 'tab-disclosure';
      disclosure.open = extra.includes(view);
      const summary = document.createElement('summary'); summary.textContent = config.secondaryLabel || '更多';
      disclosure.append(summary);
      const secondary = document.createElement('div'); secondary.className = 'tab-disclosure-items';
      extra.forEach((v) => add(v, secondary));
      disclosure.append(secondary);
      tabs.append(disclosure);
    }

    function viewAgeMs(view) {
      const loadedAt = Number(state.viewLoadedAt[view] || 0);
      return loadedAt ? Math.max(0, Date.now() - loadedAt) : Number.POSITIVE_INFINITY;
    }

    function isViewFresh(view) {
      return viewAgeMs(view) < Number(viewFreshnessMs[view] || 30000);
    }

    function stopActiveViewRefresh() {
      if (state.activeViewRefreshTimer) window.clearTimeout(state.activeViewRefreshTimer);
      state.activeViewRefreshTimer = null;
    }

    function scheduleActiveViewRefresh(view = state.activeView) {
      stopActiveViewRefresh();
      if (!state.authenticated || view !== state.activeView) return;
      const interval = Number(viewFreshnessMs[view] || 30000);
      state.activeViewRefreshTimer = window.setTimeout(async () => {
        state.activeViewRefreshTimer = null;
        if (!state.authenticated || state.activeView !== view) return;
        await refreshActiveView({ force: false, background: true });
        scheduleActiveViewRefresh(view);
      }, Math.max(3000, interval));
    }

    async function checkConsoleVersion() {
      try {
        const response = await fetch('/admin/version', { cache: 'no-store', credentials: 'same-origin' });
        const payload = await response.json();
        const changed = Boolean(ADMIN_BUILD && payload.version && payload.version !== ADMIN_BUILD);
        $('updateBanner')?.classList.toggle('hidden', !changed);
      } catch (_) {
        // Advisory only: a version check must never block normal work.
      }
    }

    function startVersionChecks() {
      if (state.versionCheckTimer) window.clearInterval(state.versionCheckTimer);
      if (state.versionCheckStartTimer) window.clearTimeout(state.versionCheckStartTimer);
      // Delay advisory version discovery until after cold start.
      state.versionCheckStartTimer = window.setTimeout(() => {
        state.versionCheckStartTimer = null;
        checkConsoleVersion();
      }, 12000);
      state.versionCheckTimer = window.setInterval(checkConsoleVersion, 60000);
    }

    function switchView(view, { focusHeading = false, load = true } = {}) {
      state.activeView = view;
      state.refreshInFlight = false;
      $('refreshBtn').disabled = false;
      $('appShell').removeAttribute('aria-busy');
      stopActiveViewRefresh();
      document.querySelectorAll('.view').forEach((node) => {
        node.classList.toggle('hidden', node.id !== `view-${view}`);
      });
      window.AdminMotion?.enterView(document.getElementById(`view-${view}`));
      window.applyDesktopPet?.();
      document.querySelectorAll('.nav button').forEach((button) => {
        const active = button.dataset.view === view || button.dataset.navGroup === viewGroups[view];
        button.classList.toggle('active', active);
        if (active) {
          button.setAttribute('aria-current', button.dataset.view ? 'page' : 'true');
        } else {
          button.removeAttribute('aria-current');
        }
      });
      renderViewTabs(view);
      const featureReady = ensureViewFeatures(view);
      const title = viewTitles[view] || view;
      $('viewTitle').textContent = title;
      document.title = `${title} · 私人助手控制台`;
      if (focusHeading) {
        // 视图切换相当于客户端导航：把焦点移到新标题，让键盘和屏幕阅读器
        // 用户立即获得上下文。WCAG 2.2 - 2.4.3 Focus Order。
        $('contentViewport')?.scrollTo({ top: 0, left: 0, behavior: 'instant' });
        requestAnimationFrame(() => $('viewTitle').focus({ preventScroll: true }));
      }
      if (view === 'qq') {
        startQqPolling();
      } else {
        stopQqPolling();
      }
      if (load && state.authenticated && typeof refreshActiveView === 'function') {
        if (state.viewRefreshTimer) window.clearTimeout(state.viewRefreshTimer);
        const hasData = Number.isFinite(viewAgeMs(view));
        setConnection(hasData ? `${title}已立即显示 · 后台检查更新` : `正在载入${title}首次数据……`, 'ok');
        // Debounce parent-to-child navigation.
        state.viewRefreshTimer = window.setTimeout(async () => {
          state.viewRefreshTimer = null;
          if (state.activeView === view) {
            try {
              await featureReady;
            } catch (_) {
              return;
            }
            refreshActiveView({ force: false, background: hasData });
            scheduleActiveViewRefresh(view);
          }
        }, 40);
      }
      featureReady.catch((error) => setConnection(error.message || String(error), 'error'));
    }

    async function bridge(path, options = {}) {
      const headers = Object.assign({}, options.headers || {});
      if (options.body && !headers['Content-Type']) {
        headers['Content-Type'] = 'application/json; charset=utf-8';
      }
      const requestOptions = Object.assign({}, options, { headers, credentials: 'same-origin' });
      const mergeableGet = String(options.method || 'GET').toUpperCase() === 'GET'
        && options.body == null
        && !options.signal
        && Object.keys(options).every((name) => ['method', 'headers'].includes(name));
      const requestKey = mergeableGet ? JSON.stringify([String(path), headers]) : '';
      if (requestKey && bridgeGetRequests.has(requestKey)) return bridgeGetRequests.get(requestKey);
      const request = (async () => {
        const animatePet = Boolean(window.shouldAnimatePetRequest?.(path, requestOptions));
        if (animatePet) window.setPetRequestActivity?.('start');
        let response;
        try {
          response = await fetch(path, requestOptions);
        } catch (error) {
          if (animatePet) window.setPetRequestActivity?.('failed');
          throw error;
        }
        let payload = {};
        try {
          payload = await response.json();
        } catch (error) {
          payload = { ok: false, error: `HTTP ${response.status}` };
        }
        if (!response.ok) {
          if (animatePet) window.setPetRequestActivity?.('failed');
          if (response.status === 403) {
            showAuthenticated(false);
            const authError = new Error('未登录或会话已过期。');
            authError.payload = payload;
            authError.status = response.status;
            throw authError;
          }
          const requestError = new Error(errorMessages[payload.error] || payload.error || `HTTP ${response.status}`);
          requestError.payload = payload;
          requestError.status = response.status;
          throw requestError;
        }
        if (animatePet) window.setPetRequestActivity?.('success');
        return payload;
      })();
      if (requestKey) {
        bridgeGetRequests.set(requestKey, request);
        const clearRequest = () => bridgeGetRequests.delete(requestKey);
        request.then(clearRequest, clearRequest);
      }
      return request;
    }

    async function checkSession() {
      const authEpoch = state.authEpoch;
      try {
        const response = await fetch('/admin/bootstrap', { credentials: 'same-origin' });
        const payload = await response.json();
        // A fast login can complete while the initial anonymous session check is
        // still in flight. Never let that stale response hide a newer session.
        if (authEpoch !== state.authEpoch) return;
        renderAppearance(payload.appearance || {}, '外观设置已加载。', 'ok');
        showAuthenticated(Boolean(payload.authenticated));
        if (state.authenticated) {
          switchView(state.activeView, { load: false });
          prepareScrollableRegions();
          refreshActiveView({ force: true, background: true })
            .catch((error) => setConnection(error.message || String(error), 'error'))
            .finally(() => {
              scheduleActiveViewRefresh(state.activeView);
              schedulePetRuntime();
            });
          startVersionChecks();
          setConnection(`日常空间已更新 · ${new Date().toLocaleTimeString()}`, 'ok');
        } else {
          setConnection('未登录。请输入访问凭证。');
        }
      } catch (error) {
        if (authEpoch !== state.authEpoch) return;
        showAuthenticated(false);
        setConnection(error.message || String(error), 'error');
      }
    }

