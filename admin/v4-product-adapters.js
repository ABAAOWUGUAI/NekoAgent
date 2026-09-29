(() => {
  'use strict';

  const pendingReadControllers = new Set();
  const gatewayAuth = document.querySelector('meta[name="admin-auth-mode"]')?.content === 'gateway';
  let csrfToken = '';

  const request = async (path, options = {}) => {
    const { signal: suppliedSignal, timeoutMs, allowPartialOk = false, ...fetchOptions } = options;
    const headers = { ...(fetchOptions.headers || {}) };
    if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json; charset=utf-8';
    const controller = new AbortController();
    const method = String(fetchOptions.method || 'GET').toUpperCase();
    if (gatewayAuth && ['POST', 'PATCH'].includes(method) && path !== '/admin/login' && csrfToken) {
      headers['X-CSRF-Token'] = csrfToken;
    }
    const requestedTimeout = Number(timeoutMs ?? (method === 'GET' ? 10_000 : 90_000));
    const effectiveTimeout = Number.isFinite(requestedTimeout)
      ? Math.max(1_000, Math.min(requestedTimeout, 610_000))
      : (method === 'GET' ? 10_000 : 90_000);
    let timedOut = false;
    const forwardAbort = () => controller.abort();
    if (suppliedSignal) {
      if (suppliedSignal.aborted) forwardAbort();
      else suppliedSignal.addEventListener('abort', forwardAbort, { once: true });
    }
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, effectiveTimeout);
    try {
      const response = await fetch(path, { ...fetchOptions, headers, credentials: 'same-origin', signal: controller.signal });
      const payload = await response.json().catch(() => ({ ok: false, error: `HTTP ${response.status}` }));
      if (!response.ok || (payload.ok === false && !allowPartialOk)) {
        const error = new Error(payload.error || `HTTP ${response.status}`);
        error.status = response.status;
        error.payload = payload;
        throw error;
      }
      return payload;
    } catch (error) {
      if (timedOut && error?.name === 'AbortError') {
        const timeoutError = new Error('请求超时，请稍后重试。');
        timeoutError.name = 'RequestTimeoutError';
        throw timeoutError;
      }
      throw error;
    } finally {
      clearTimeout(timer);
      suppliedSignal?.removeEventListener('abort', forwardAbort);
    }
  };

  const requestId = (prefix) => `${prefix}-${crypto.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
  const get = (path, options = {}) => {
    const controller = new AbortController();
    pendingReadControllers.add(controller);
    return request(path, { ...options, signal: controller.signal }).finally(() => pendingReadControllers.delete(controller));
  };
  const cancelPendingReads = () => {
    for (const controller of pendingReadControllers) controller.abort();
    pendingReadControllers.clear();
    // A rejected request remains in ``inflight`` until its fetch promise
    // settles.  A new route may arrive in that small interval, so it must not
    // inherit the old request's AbortError.
    inflight.clear();
  };
  const inflight = new Map();
  const readOnce = (key, operation) => {
    if (!inflight.has(key)) {
      const promise = Promise.resolve().then(operation).finally(() => {
        if (inflight.get(key) === promise) inflight.delete(key);
      });
      inflight.set(key, promise);
    }
    return inflight.get(key);
  };
  const post = (path, body, prefix = 'v4', options = {}) => {
    const { requestId: fixedRequestId, ...requestOptions } = options;
    if (fixedRequestId && !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/.test(fixedRequestId)) throw new Error('invalid_request_id');
    const clientRequestId = fixedRequestId || requestId(prefix);
    return request(path, {
      method: 'POST',
      headers: {
        'Idempotency-Key': clientRequestId,
        'X-Request-ID': clientRequestId,
        'X-Admin-Build': renderedConsoleBuild(),
      },
      body: JSON.stringify(body),
      ...requestOptions,
    }).catch((error) => {
      error.requestId = error?.payload?.request_id || clientRequestId;
      throw error;
    });
  };
  const patch = (path, body, prefix = 'v4') => {
    const clientRequestId = requestId(prefix);
    return request(path, {
      method: 'PATCH',
      headers: {
        'X-Request-ID': clientRequestId,
        'X-Admin-Build': renderedConsoleBuild(),
      },
      body: JSON.stringify(body),
    }).catch((error) => {
      error.requestId = error?.payload?.request_id || clientRequestId;
      throw error;
    });
  };
  const renderedConsoleBuild = () => String(document.querySelector('meta[name="admin-build"]')?.content || '').trim();
  const retainGatewaySession = (payload) => {
    if (!gatewayAuth) return payload;
    csrfToken = payload?.authenticated && typeof payload.csrf_token === 'string' ? payload.csrf_token : '';
    return payload;
  };

  window.V4CapabilityAdapters = Object.freeze({
    cancelPendingReads,
    bootstrap: () => get('/admin/bootstrap').then(retainGatewaySession),
    login: (credential) => post(
      '/admin/login',
      { [gatewayAuth ? 'password' : 'token']: credential, build: renderedConsoleBuild() },
      'v4-login',
    ).then(retainGatewaySession),
    logout: () => post('/admin/logout', {}, 'v4-logout').finally(() => { if (gatewayAuth) csrfToken = ''; }),
    saveAppearance: (appearance) => post('/admin/appearance', appearance, 'v4-appearance'),

    ownerBrief: () => readOnce('owner-brief', () => get('/assistant/owner-brief?limit=6')),
    webConversations: (limit = 20, cursor = '') => readOnce(`web-conversations:${limit}:${cursor}`, () => get(`/assistant/conversations?channel_type=web&limit=${Math.min(80, Math.max(1, Number(limit) || 20))}&cursor=${encodeURIComponent(cursor)}`)),
    webConversationMessages: (threadId, limit = 40, cursor = '') => readOnce(`web-conversation:${threadId}:${limit}:${cursor}`, () => get(`/assistant/conversations/${encodeURIComponent(threadId)}/messages?channel_type=web&limit=${Math.min(80, Math.max(1, Number(limit) || 40))}&cursor=${encodeURIComponent(cursor)}`)),
    qqConversations: (kind = 'group', limit = 20, cursor = '') => readOnce(`qq-conversations:${kind}:${limit}:${cursor}`, () => get(`/assistant/qq/conversations?kind=${encodeURIComponent(kind)}&limit=${Math.min(80, Math.max(1, Number(limit) || 20))}&cursor=${encodeURIComponent(cursor)}`)),
    qqTimeline: (conversationRef, limit = 40, cursor = '') => readOnce(`qq-timeline:${conversationRef}:${limit}:${cursor}`, () => get(`/assistant/qq/conversations/${encodeURIComponent(conversationRef)}/timeline?limit=${Math.min(80, Math.max(1, Number(limit) || 40))}&cursor=${encodeURIComponent(cursor)}`)),
    qqConversationSummary: (conversationRef, windowHours = 168) => readOnce(`qq-summary:${conversationRef}:${windowHours}`, () => get(`/assistant/qq/conversations/${encodeURIComponent(conversationRef)}/summary?window_hours=${Math.min(168, Math.max(1, Number(windowHours) || 168))}`)),
    qqEventInspector: (eventRef) => readOnce(`qq-event:${eventRef}`, () => get(`/assistant/qq/events/${encodeURIComponent(eventRef)}/inspector`)),
    qqMemories: (channel, subjectId) => get(`/assistant/qq-memories?channel=${encodeURIComponent(channel)}&subject_id=${encodeURIComponent(subjectId)}`),
    mutateQqMemory: (payload) => post('/assistant/qq-memories', payload, 'v4-qq-memory'),

    qqSettings: () => get('/qq/settings'),
    qqGroupStates: () => get('/qq/groups/state'),
    saveQqSettings: (snapshot) => post('/qq/settings', snapshot, 'v4-qq-access'),
    groupParticipationWindows: () => get('/qq/group-participation/windows'),
    saveGroupParticipationWindows: (payload) => post('/qq/group-participation/windows', payload, 'v4-qq-ambient-windows'),
    groups: () => get('/assistant/groups'),
    groupMessages: (groupId, limit = 50) => get(`/assistant/groups/messages?group_id=${encodeURIComponent(groupId)}&limit=${Math.min(80, Math.max(1, Number(limit) || 50))}`),
    saveGroup: (group) => post('/assistant/groups', group, 'v4-qq-group'),
    groupResearch: () => get('/assistant/group-research?limit=30'),
    saveGroupResearch: (policy) => post('/assistant/group-research/policy', policy, 'v4-group-research'),
    deliveries: () => get('/deliveries?state=all&channel=qq&limit=60'),

    tasks: () => get('/tasks?limit=60'),
    task: (taskId) => get(`/tasks/${encodeURIComponent(taskId)}`),
    execution: () => get('/execution/overview?limit=30'),
    approvals: () => get('/assistant/approvals?status=pending&limit=50'),
    decideApproval: (id, decision) => post(`/assistant/approvals/${encodeURIComponent(id)}/decision`, decision, 'v4-approval'),
    automations: () => get('/automations/overview'),
    automationJobs: () => get('/automations/jobs'),
    saveAutomation: (job) => post('/automations/jobs', job, 'v4-automation'),
    projects: () => get('/projects?include_archived=true'),
    currentProject: () => get('/projects/current'),

    artifacts: () => get('/assistant/artifacts?limit=100&offset=0'),
    artifact: (id) => get(`/assistant/artifacts/${encodeURIComponent(id)}`),
    artifactVersions: (id) => get(`/assistant/artifacts/${encodeURIComponent(id)}/versions`),
    artifactEvents: (id) => get(`/assistant/artifacts/${encodeURIComponent(id)}/events?limit=80`),
    reviseArtifact: (id, requestBody) => post(
      `/assistant/artifacts/${encodeURIComponent(id)}/revise`,
      requestBody,
      'v4-artifact-revise',
      { timeoutMs: 610_000 },
    ),

    memory: () => get('/assistant/memories?limit=100'),
    addMemory: (memory) => post('/assistant/memories', memory, 'v4-memory'),
    correctMemory: (id, correction) => patch(`/assistant/memories/${encodeURIComponent(id)}`, correction, 'v4-memory-correct'),
    deleteMemory: (id) => post('/assistant/memories/delete', { id }, 'v4-memory-delete'),
    promoteMemory: (id, draft) => post(`/assistant/memories/${encodeURIComponent(id)}/promote`, draft, 'v4-memory-promote'),
    knowledge: () => get('/assistant/knowledge/workspace'),
    learning: () => get('/assistant/learning'),
    learningTrace: () => get('/assistant/learning/trace?limit=8'),
    behaviorGrowth: () => get('/assistant/behavior-growth'),
    behaviorGrowthCases: () => get('/assistant/behavior-growth/cases?limit=6'),
    behaviorEvidenceCollectionPlan: () => get('/assistant/behavior-growth/evidence-collection/cutover'),
    setBehaviorEvidenceCollection: (enabled, planChecksum) => post(
      '/assistant/behavior-growth/evidence-collection/cutover',
      { enabled, plan_checksum: planChecksum },
      'v4-behavior-evidence-collection',
    ),
    behaviorOptimizerPlan: () => get('/assistant/behavior-growth/optimizer/cutover'),
    setBehaviorOptimizer: (enabled, planChecksum) => post(
      '/assistant/behavior-growth/optimizer/cutover',
      { enabled, plan_checksum: planChecksum },
      'v4-behavior-optimizer',
    ),
    behaviorAuthorizations: () => get('/assistant/behavior-growth/authorizations'),
    planBehaviorAuthorization: (purpose, binding) => post(
      '/assistant/behavior-growth/authorizations/plan',
      { purpose, binding },
      'v4-behavior-authorization-plan',
    ),
    createBehaviorAuthorization: (purpose, binding, planChecksum, expiresAt) => post(
      '/assistant/behavior-growth/authorizations/create',
      { purpose, binding, plan_checksum: planChecksum, expires_at: expiresAt },
      'v4-behavior-authorization-create',
    ),
    revokeBehaviorAuthorization: (authorizationRef, planChecksum) => post(
      '/assistant/behavior-growth/authorizations/revoke',
      { authorization_ref: authorizationRef, plan_checksum: planChecksum },
      `v4-behavior-authorization-revoke-${authorizationRef}`,
    ),
    behaviorPairedShadowPlan: () => get('/assistant/behavior-growth/paired-shadow/cutover'),
    setBehaviorPairedShadow: (authorizationRef, planChecksum) => post(
      '/assistant/behavior-growth/paired-shadow/cutover/set',
      { authorization_ref: authorizationRef, plan_checksum: planChecksum },
      'v4-behavior-paired-shadow-set',
    ),
    revokeBehaviorPairedShadow: (planChecksum) => post(
      '/assistant/behavior-growth/paired-shadow/cutover/revoke',
      { plan_checksum: planChecksum },
      'v4-behavior-paired-shadow-revoke',
    ),
    learningFeedback: (candidateId, feedback) => post('/assistant/learning/feedback', { candidate_id: candidateId, feedback }, 'v4-learning'),

    persona: () => get('/assistant/persona-workspace'),
    previewPersona: (draft) => post('/assistant/persona-workspace/preview', draft, 'v4-persona-preview'),
    savePersona: (draft) => post('/assistant/persona-workspace', draft, 'v4-persona'),
    personaPresets: () => get('/assistant/persona-presets'),
    createPersonaPreset: (draft) => post('/assistant/persona-presets', draft, 'v4-persona-preset-create'),
    updatePersonaPreset: (draft) => post('/assistant/persona-presets/update', draft, `v4-persona-preset-update-${draft.id}`),
    archivePersonaPreset: (id, expectedUpdatedAt) => post(
      '/assistant/persona-presets/archive', { id, expected_updated_at: expectedUpdatedAt }, `v4-persona-preset-archive-${id}`,
    ),
    applyPersonaPreset: (id) => post('/assistant/persona-presets/apply', { id }, `v4-persona-preset-apply-${id}`),
    relationshipFor: (userId) => get(`/assistant/relationship?user_id=${encodeURIComponent(userId)}&scope_type=private_user&scope_id=`),
    saveRelationship: (draft) => post('/assistant/relationship', draft, `v4-private-relationship-${draft.user_id}`),
    socialPolicy: (userId) => get(`/assistant/proactive/social-policy?user_id=${encodeURIComponent(userId)}`),
    saveSocialPolicy: (draft) => post('/assistant/proactive/social-policy', draft, `v4-private-social-${draft.user_id}`),
    voice: () => get('/assistant/voice-response-policy'),
    pets: () => get('/assistant/pets'),

    models: () => get('/assistant/models'),
    // Provider secrets are write-only.  The server never returns them in the
    // registry projection, and this adapter deliberately has no read helper
    // for a secret value.
    // A Provider/Model write can succeed while the dependent executor apply is
    // still rejected.  Preserve that canonical server result for an immediate
    // GET readback; do not relabel a persisted configuration as a failed POST.
    saveModelProvider: (draft) => post('/assistant/models/provider', draft, 'v4-model-provider', { allowPartialOk: true }),
    saveModelCatalog: (draft) => post('/assistant/models/model', draft, 'v4-model-catalog', { allowPartialOk: true }),
    // Removal is deliberately a separate explicit action.  The server refuses
    // to delete a model that is still routed or used by an Executor Profile,
    // and refuses to delete a connection that still owns catalog entries.
    deleteModelCatalog: (id) => post('/assistant/models/model/delete', { id }, 'v4-model-delete', { allowPartialOk: true }),
    deleteModelProvider: (id) => post('/assistant/models/provider/delete', { id }, 'v4-model-provider-delete', { allowPartialOk: true }),
    // Discovery consults only an already saved connection.  It returns a
    // bounded candidate list; selecting a name does not persist or route it.
    discoverProviderModels: (providerId) => post(
      '/assistant/models/discover', { provider_id: providerId }, 'v4-model-discover', { allowPartialOk: true, timeoutMs: 130_000 },
    ),
    validateDiscoveredModel: (providerId, model) => post(
      '/assistant/models/discover', { action: 'validate', provider_id: providerId, model, user_prompt: '请只回复 OK', max_tokens: 256 }, 'v4-model-discover-validate', { allowPartialOk: true, timeoutMs: 130_000 },
    ),
    bindModelRole: (role, primaryModelId, fallbackModelId) => post('/assistant/models/bind', {
      role,
      primary_model_id: primaryModelId,
      // A work executor is a single trusted executor, never a generic model
      // fallback.  Preserve this boundary even for non-UI callers.
      fallback_model_id: role === 'work_executor' ? '' : fallbackModelId,
    }, 'v4-model-role', { allowPartialOk: true }),
    // A custom work executor owns the singleton local proxy.  It therefore
    // has a dedicated, explicit switch endpoint rather than piggybacking on
    // the generic role-binding write.
    activateWorkExecutor: (modelId) => post(
      '/assistant/models/work-executor/activate',
      { model_id: modelId },
      'v4-work-executor-activate',
      { timeoutMs: 45_000, allowPartialOk: true },
    ),
    testModel: (modelId) => post(
      '/assistant/models/test',
      { model_id: modelId },
      'v4-model-test',
      { allowPartialOk: true },
    ),
    verifyExecutor: (providerId) => post(
      '/assistant/models/executor/verify',
      { provider_id: providerId, timeout: 120 },
      'v4-executor-verify',
      { timeoutMs: 130_000, allowPartialOk: true },
    ),
    capabilities: () => get('/capabilities/summary'),
    plugins: () => get('/capabilities/plugins'),
    togglePlugin: (id, enabled) => post('/capabilities/plugins/toggle', { id, enabled }, 'v4-plugin'),
    skills: () => get('/capabilities/skills'),
    network: () => get('/assistant/network-policy'),
    proxySubscriptions: () => get('/proxy/subscriptions'),
    proxyGroups: () => get('/proxy/groups'),
    createProxySubscription: (draft) => post('/proxy/subscriptions/create', draft, 'v4-proxy-subscription-create', { timeoutMs: 45_000, allowPartialOk: true }),
    updateProxySubscription: (draft) => post('/proxy/subscriptions/update', draft, 'v4-proxy-subscription-update', { timeoutMs: 45_000, allowPartialOk: true }),
    operateProxySubscription: (action, key, expectedRevision) => {
      const payload = { key, expected_revision: Number(expectedRevision) };
      const options = { timeoutMs: 45_000, allowPartialOk: true };
      if (action === 'refresh') return post('/proxy/subscriptions/refresh', payload, 'v4-proxy-subscription-refresh', options);
      if (action === 'switch') return post('/proxy/subscriptions/switch', payload, 'v4-proxy-subscription-switch', options);
      if (action === 'enable') return post('/proxy/subscriptions/enable', payload, 'v4-proxy-subscription-enable', options);
      if (action === 'disable') return post('/proxy/subscriptions/disable', payload, 'v4-proxy-subscription-disable', options);
      if (action === 'delete') return post('/proxy/subscriptions/delete', payload, 'v4-proxy-subscription-delete', options);
      throw new Error('proxy_subscription_action_forbidden');
    },
    delayProxyNodes: (group, names) => post('/proxy/delay', { group, names, timeout_ms: 8000 }, 'v4-proxy-node-delay', { timeoutMs: 20_000, allowPartialOk: true }),
    delayProxyNode: (group, node) => post('/proxy/delay', { group, names: [node], timeout_ms: 8000 }, 'v4-proxy-node-delay', { timeoutMs: 20_000, allowPartialOk: true }),
    selectProxyNode: (subscriptionKey, node, expectedRevision) => post('/proxy/select', { subscription_key: subscriptionKey, node, expected_revision: Number(expectedRevision) }, 'v4-proxy-node-select', { allowPartialOk: true }),
    proxyConsumer: () => {
      const clientRequestId = requestId('v4-proxy-consumer-read');
      return get('/proxy/consumers/aiclient2api').catch((error) => {
        error.requestId = error?.payload?.request_id || clientRequestId;
        throw error;
      });
    },
    proxyModelObservation: () => get('/proxy/consumers/aiclient2api/model-observation'),
    saveProxyConsumer: (draft) => post('/proxy/consumers/aiclient2api/save', draft, 'v4-proxy-consumer-save', { allowPartialOk: true }),
    testProxyConsumer: (saved) => post('/proxy/consumers/aiclient2api/test', { expected_revision: Number(saved.expected_revision), timeout_seconds: 120 }, 'v4-proxy-consumer-test', { timeoutMs: 130_000, allowPartialOk: true }),
    applyProxyConsumer: (draft) => post('/proxy/consumers/aiclient2api/apply', draft, 'v4-proxy-consumer-apply', { timeoutMs: 90_000, allowPartialOk: true }),
    rollbackProxyConsumer: (draft) => post('/proxy/consumers/aiclient2api/rollback', draft, 'v4-proxy-consumer-rollback', { timeoutMs: 90_000, allowPartialOk: true }),
    reliability: () => get('/reliability/dead-letters'),
    requeueDelivery: (id, { confirmDuplicateRisk = false } = {}) => post(`/reliability/dead-letters/${encodeURIComponent(id)}/requeue`, {
      confirm_requeue: true,
      ...(confirmDuplicateRisk ? { confirm_duplicate_risk: true } : {}),
    }, 'v4-delivery-retry'),
    diagnostics: () => get('/qq/diagnostics'),
    qqLoginQrcodeUrl: () => '/qq/qrcode',
    refreshQqLogin: () => post('/qq/qrcode/refresh', { confirm_restart: true, wait_seconds: 30 }, 'v4-qq-login-refresh', { timeoutMs: 45_000, allowPartialOk: true }),
    services: () => get('/services'),
    // The server grants this bounded log query up to 12 seconds.  Keep a small
    // client margin so the browser does not abort a still-valid diagnostic read.
    logs: () => get('/logs?target=bridge&lines=40', { timeoutMs: 15_000 }),

    newDispatchRequestId: () => requestId('v4-chat'),
    dispatch: (prompt, fixedRequestId = '') => post('/assistant/dispatch', {
      message: prompt,
      source: 'web-console',
      force: 'auto',
    }, 'v4-chat', fixedRequestId ? { requestId: fixedRequestId } : {}),
  });
})();
