(() => {
  'use strict';

  const contract = window.V4ProductContract;
  const attentionApi = window.V4Attention;
  const api = window.V4CapabilityAdapters;
  // Runtime operations have two independent obligations: the POST may finish
  // (successfully or not), and the registry must then be read canonically.
  // A UI session can become stale while either request is pending, so commit
  // is guarded separately from readback. This small executable core is shared
  // by the real handlers and the VM behavior tests.
  // `/assistant/models` has no server revision. Track each client-side GET
  // issuance independently from dialog sessions, so an older delayed
  // projection cannot roll a newer canonical cache backward.
  const runtimeReadbackGeneration = { issued: 0, persisted: 0 };
  const runtimeWorkspaceCore = Object.freeze({
    createSessionGuard() {
      let epoch = 0;
      return {
        begin(meta = {}) {
          const id = ++epoch;
          return Object.freeze({ id, meta, current: () => id === epoch });
        },
        invalidate() { epoch += 1; },
      };
    },
    boundPage(items, requestedPage = 0, requestedSize = 25) {
      const source = Array.isArray(items) ? items : [];
      const pageSize = Math.max(1, Math.min(25, Number(requestedSize) || 25));
      const pageCount = Math.max(1, Math.ceil(source.length / pageSize));
      const page = Math.max(0, Math.min(pageCount - 1, Number(requestedPage) || 0));
      return { items: source.slice(page * pageSize, (page + 1) * pageSize), page, pageCount, total: source.length };
    },
    assetReferences({ kind, id, models, providers, roles }) {
      const modelItems = Array.isArray(models) ? models : [];
      const providerItems = Array.isArray(providers) ? providers : [];
      const roleItems = Array.isArray(roles) ? roles : [];
      const assetId = String(id || '');
      const modelRefs = kind === 'provider'
        ? modelItems.filter((item) => String(item.provider_id || '') === assetId)
          .map((item) => ({ id: item.id, label: item.label || item.model || item.id }))
        : [];
      const usedModelIds = kind === 'model' ? new Set([assetId]) : new Set(modelRefs.map((item) => String(item.id)));
      const roleRefs = roleItems.filter((item) => usedModelIds.has(String(item.primary_model_id || ''))
        || usedModelIds.has(String(item.fallback_model_id || '')))
        .map((item) => ({ id: item.role, label: item.label || item.role }));
      const profileRefs = providerItems.filter((item) => {
        const profile = item.executor_profile;
        if (!profile) return false;
        return kind === 'model'
          ? String(profile.upstream_model_id || '') === assetId
          : String(item.id || '') === assetId || String(profile.upstream_provider_id || '') === assetId;
      }).map((item) => ({ id: item.id, label: item.name || item.id }));
      return { modelRefs, roleRefs, profileRefs, blocked: Boolean(modelRefs.length || roleRefs.length || profileRefs.length) };
    },
    automationJobView(job, now = Date.now()) {
      const type = String(job?.schedule_type || '');
      const zone = String(job?.timezone || '时区未记录');
      const clock = String(job?.time_of_day || '时间未记录');
      const days = ['一', '二', '三', '四', '五', '六', '日'];
      const weekly = [...new Set(String(job?.weekdays || '').split(',').map((day) => day.trim())
        .filter((day) => /^\d+$/.test(day)).map((day) => Number(day)))]
        .filter((day) => Number.isInteger(day) && day >= 0 && day <= 6)
        .map((day) => days[day]).join('、');
      const interval = Number(job?.interval_minutes);
      const schedule = type === 'daily' ? `每天 ${clock}（${zone}）`
        : type === 'weekly' ? `每周${weekly || '（星期未记录）'} ${clock}（${zone}）`
          : type === 'interval' ? (Number.isFinite(interval) && interval > 0 ? `每 ${interval} 分钟（${zone}）` : '间隔未记录')
            : type === 'once' ? `一次性：${String(job?.run_at || '时间未记录')}（${zone}）`
              : '频率未记录';
      const lastRunAt = String(job?.last_run_at || '');
      const rawError = String(job?.last_error || '');
      const lastError = /^[a-z][a-z0-9_]{0,80}$/.test(rawError) ? rawError : '';
      const lastResult = !lastRunAt ? '尚无最终运行结果' : rawError ? '最近一次失败' : '最近一次已完成';
      const enabled = Number(job?.enabled) === 1;
      const state = String(job?.state || '');
      const due = String(job?.next_due_at || '');
      let nextStatus = 'unknown';
      if (state === 'blocked') nextStatus = 'blocked';
      else if (state === 'completed') nextStatus = 'completed';
      else if (!enabled) nextStatus = 'paused';
      else if (state === 'running' || state === 'dispatched') nextStatus = 'in_progress';
      else if (due) {
        const dueTime = Date.parse(due);
        const nowTime = typeof now === 'number' ? now : Date.parse(now);
        if (Number.isFinite(dueTime) && Number.isFinite(nowTime)) nextStatus = dueTime <= nowTime ? 'overdue' : 'scheduled';
      }
      return {
        schedule, lastResult, lastRunAt, lastError, nextStatus,
        nextDueAt: nextStatus === 'scheduled' || nextStatus === 'overdue' ? due : '',
      };
    },
    canUseScopedDiscovery(modelDraft, discovery) {
      const providerId = String(modelDraft?.provider_id || '');
      const lockedProviderId = String(discovery?.lockedProviderId || '');
      return !modelDraft?.id && Boolean(providerId) && providerId === lockedProviderId;
    },
    proxySubscriptionErrorText(error) {
      const code = String(error?.payload?.error_kind || error?.payload?.error || error?.code || '');
      const labels = {
        console_update_required: '页面版本已更新，请刷新页面后再重试',
        subscription_revision_changed: '配置已被其他操作更新，请重新加载后再保存',
        dependency_in_use: '该订阅正在被当前代理配置使用。请先切换到其他订阅或停用受控出站。',
        subscription_already_exists: '已存在同 ID 或同名称订阅，请编辑现有订阅。',
        subscription_name_conflict: '订阅名称已被使用。',
        subscription_name_required: '请填写订阅名称。',
        subscription_name_invalid: '订阅名称无效，请换一个名称。',
        subscription_not_found: '该订阅已不存在，请刷新列表。',
        subscription_key_required: '缺少订阅标识，请刷新列表后重试。',
        invalid_subscription_url: '订阅地址无效。',
        'args.url_invalid': '订阅地址无效。',
        'args.expected_revision_invalid': '订阅版本无效，请刷新列表后重试。',
        subscription_url_credentials_forbidden: '订阅地址不能包含用户名或密码。',
        subscription_url_port_forbidden: '订阅地址只能使用标准 HTTP/HTTPS 端口。',
        subscription_host_not_public: '订阅地址必须指向公开网络地址。',
        subscription_host_resolution_failed: '无法解析订阅地址，请检查域名或稍后重试。',
        subscription_payload_empty: '订阅内容为空，现有节点保持不变。',
        subscription_payload_too_large: '订阅内容超过大小限制，现有节点保持不变。',
        subscription_payload_not_utf8: '订阅内容编码不受支持。',
        unsupported_subscription_format: '不支持的订阅格式，请提供 Mihomo/Clash 或受支持的节点订阅。',
        subscription_has_no_proxies: '订阅中没有可用代理节点。',
        subscription_converter_input_invalid: '订阅转换输入无效。',
        subscription_converter_config_missing: '服务器缺少订阅转换配置。',
        subscription_converter_permission_failed: '服务器无法安全准备订阅转换文件权限；请联系管理员检查 Ops Broker 运行用户与临时目录权限，现有节点保持不变。',
        subscription_converter_execution_failed: '订阅转换服务执行失败。',
        subscription_converter_failed: '订阅转换失败。',
        subscription_converter_output_invalid: '订阅转换结果无效。',
        subscription_source_tls_failed: '订阅源 TLS 握手或证书校验失败；请检查订阅服务的 HTTPS 配置后重试，现有节点保持不变。',
        subscription_source_timeout: '订阅源响应超时；请稍后重试或检查订阅服务可用性，现有节点保持不变。',
        subscription_source_connection_failed: '无法连接订阅源；请检查订阅服务是否可用，现有节点保持不变。',
        subscription_source_http_4xx: '订阅源拒绝了请求（HTTP 4xx）；请确认地址仍有效或重新获取订阅地址，现有节点保持不变。',
        subscription_source_http_5xx: '订阅源暂时异常（HTTP 5xx）；请稍后重试，现有节点保持不变。',
        subscription_source_redirect_failed: '订阅源跳转无效或超过安全限制；请使用有效的最终订阅地址，现有节点保持不变。',
        subscription_peer_mismatch: '订阅连接的对端地址与已验证地址不一致；请稍后重试或联系管理员检查 DNS，现有节点保持不变。',
        subscription_proxy_environment_forbidden: '服务器检测到代理环境配置，已阻止订阅抓取；请联系管理员清理代理环境后重试，现有节点保持不变。',
        subscription_payload_invalid: '订阅源返回了无效内容；请确认地址指向有效订阅，现有节点保持不变。',
        subscription_payload_parse_failed: '订阅内容无法解析为有效节点配置；请检查订阅格式，现有节点保持不变。',
        subscription_download_failed: '订阅下载失败，现有节点保持不变。',
        subscription_backup_prepare_failed: '保存前备份失败，配置没有改动。',
        subscription_candidate_write_failed: '候选配置写入失败，原配置保持不变。',
        subscription_state_commit_failed: '订阅状态提交失败，系统已停止继续写入。',
        subscription_transaction_failed: '订阅变更事务失败，请根据回滚状态处理。',
        subscription_operation_failed: '订阅操作没有返回明确结果，请刷新列表确认现状；仍失败时请凭 Request ID 排查。',
        pyyaml_missing: '服务器缺少处理订阅所需的组件。',
        mihomo_config_not_mapping: 'Mihomo 主配置结构异常，订阅没有写入；请检查服务器配置。',
        legacy_group_still_referenced: '旧代理组仍被规则引用，无法安全重建配置；请先迁移对应规则。',
        mihomo_config_test_failed: 'Mihomo 配置校验失败，已恢复原配置。',
        mihomo_reload_failed: 'Mihomo 重载失败，已恢复原配置。',
        provider_cleanup_failed: '旧 Provider 文件清理失败，系统已停止事务并尝试恢复原配置；请检查服务器文件权限。',
        broker_request_too_large: '提交给 Ops Broker 的请求过大，请缩短订阅名称或地址后重试。',
        broker_request_invalid_json: 'Ops Broker 收到的请求格式异常；请刷新页面后重试，仍失败时请凭 Request ID 排查。',
        broker_idempotency_replay: '同一操作已经提交过，请刷新列表确认结果，不要连续重复提交。',
        broker_unreachable: 'Ops Broker 暂时不可用。',
        broker_required_for_proxy_write: '代理写操作必须经过 Ops Broker。',
        broker_response_invalid: 'Ops Broker 返回了无效响应。',
        ops_executor_unconfigured: '服务器尚未配置订阅写入执行器，请检查 Ops Broker 配置。',
        ops_executor_invalid_result: '订阅写入执行器返回格式异常，请凭 Request ID 排查服务器。',
        ops_broker_internal_error: 'Ops Broker action 执行失败。',
        'args.name_required': '请填写订阅名称。',
        'args.name_too_long': '订阅名称不能超过 64 个字符。',
        'args.name_contains_control': '订阅名称包含不支持的控制字符，请重新输入。',
        'args.enabled_boolean_required': '页面中的启用状态无效，请刷新页面后重试。',
        'args.subscription_key_required': '缺少订阅标识，请刷新列表后重试。',
        'args.subscription_key_too_long': '订阅标识异常，请刷新列表后重试。',
        'args.subscription_key_contains_control': '订阅标识异常，请刷新列表后重试。',
        'args.url_update_present_boolean_required': '页面中的地址更新状态无效，请刷新页面后重试。',
        'args.url_without_update_flag': '页面版本与服务器状态不一致，请刷新页面后重试。',
      };
      const safeCode = /^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,95}$/.test(code) ? code : '';
      const rawRequestId = String(error?.payload?.request_id || error?.requestId || '');
      const requestId = /^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,159}$/.test(rawRequestId) ? rawRequestId : '未提供';
      const message = labels[code] || (safeCode ? `未知错误（错误代码 ${safeCode}）` : '未知错误');
      return `${message} · Request ID ${requestId}`;
    },
    proxyConnectionState({ providerId, consumer, subscriptions, groups }) {
      if (String(providerId || '') !== 'aiclient2api-gemini-antigravity') return null;
      const subscriptionItems = Array.isArray(subscriptions?.managed) ? subscriptions.managed : [];
      const activeKey = String(subscriptions?.active_key || '');
      const subscription = subscriptionItems.find((item) => (
        activeKey
        && item?.active
        && item?.enabled
        && String(item?.key || '') === activeKey
      )) || null;
      const proxyGroups = Array.isArray(groups?.groups) ? groups.groups : [];
      const canonical = proxyGroups.find((item) => String(item?.name || '') === 'Proxies') || null;
      const nodes = (Array.isArray(canonical?.nodes) ? canonical.nodes : []).filter(
        (item) => !['DIRECT', 'REJECT'].includes(String(item?.name || '').toUpperCase()),
      );
      return {
        consumer: consumer && typeof consumer === 'object' ? consumer : {},
        management: consumer?.management && typeof consumer.management === 'object' ? consumer.management : {},
        subscription,
        group: 'Proxies',
        nodes,
        currentNode: String(canonical?.now || ''),
        managementRevision: Number(subscriptions?.management_revision || 0),
      };
    },
    reconcileDeleteDraft({ kind, id, previous, registry, readbackError }) {
      if (readbackError) return previous;
      const projection = registry?.result || registry || {};
      const collection = kind === 'provider' ? projection.providers : projection.models;
      return (Array.isArray(collection) ? collection : []).find((item) => String(item?.id || '') === String(id || '')) || null;
    },
    async runTerminal({ write, readback, persist, session, commit }) {
      let result = null;
      let error = null;
      let registry = null;
      let readbackError = null;
      try { result = await write(); } catch (caught) { error = caught; }
      const readbackGeneration = ++runtimeReadbackGeneration.issued;
      try { registry = await readback(); } catch (caught) { readbackError = caught; }
      const terminal = {
        ok: !error && result?.ok !== false,
        result,
        error,
        registry,
        readbackError,
        readbackGeneration,
        stale: Boolean(session && !session.current()),
      };
      // The registry cache is canonical data, not transient dialog state. A
      // cancelled operation must still retain a completed GET projection so a
      // later return to Runtime cannot revive an older cached registry.
      const canPersist = Boolean(registry) && typeof persist === 'function'
        && readbackGeneration >= runtimeReadbackGeneration.persisted;
      if (canPersist) {
        // Reserve the generation before invoking the callback. The production
        // cache write is synchronous; the reservation also keeps a future
        // asynchronous callback from admitting an older response afterward.
        runtimeReadbackGeneration.persisted = readbackGeneration;
        await persist(terminal);
      }
      terminal.stale = Boolean(session && !session.current());
      if (!terminal.stale) await commit?.(terminal);
      return terminal;
    },
    runScopedDiscovery({ providerId, discover, readback, persist, session, commit }) {
      return runtimeWorkspaceCore.runTerminal({
        write: () => discover(providerId),
        readback,
        persist,
        session,
        commit,
      });
    },
  });
  window.V4RuntimeWorkspaceCore = runtimeWorkspaceCore;

  const personaWorkspaceCore = Object.freeze({
    contractFields: Object.freeze([
      'identity_core', 'relationship_stance', 'work_continuity', 'values', 'boundaries',
      'preferred_phrases', 'avoid_phrases', 'prohibited_patterns', 'warmth', 'directness',
      'initiative', 'humor', 'rhythm', 'question_policy', 'address_policy', 'private_length',
      'group_length', 'work_length', 'meme_policy', 'group_stance', 'group_reaction_style',
      'group_sentence_rhythm', 'group_ending_policy', 'examples',
    ]),
    expectedUpdatedAt(workspace) { return String(workspace?.assistant?.updated_at || ''); },
    acceptCanonical(previous, response) {
      const canonical = response?.result || response;
      if (!canonical || typeof canonical !== 'object') throw new Error('persona_canonical_missing');
      const previousId = String(previous?.assistant?.id || '');
      const currentId = String(canonical?.assistant?.id || '');
      if (!previousId || currentId !== previousId) throw new Error('persona_canonical_assistant_mismatch');
      const runtime = canonical.runtime || {};
      const versionId = String(canonical?.persona?.version_id || '');
      if (
        runtime.version_match !== true || !versionId
        || String(runtime.requested_persona_version_id || '') !== versionId
        || String(runtime.applied_persona_version_id || '') !== versionId
        || !String(runtime.contract_hash || '')
      ) throw new Error('persona_canonical_runtime_mismatch');
      if (!canonical.voice_contract || typeof canonical.voice_contract !== 'object') throw new Error('persona_canonical_contract_missing');
      return canonical;
    },
  });
  window.V4PersonaWorkspaceCore = personaWorkspaceCore;

  const personaListValue = value => (Array.isArray(value) ? value : []).join('\n');
  const personaListDraft = value => String(value || '').split(/\r?\n/).map(item => item.trim()).filter(Boolean);
  const personaExamplesValue = value => JSON.stringify(Array.isArray(value) ? value : [], null, 2);
  const personaOptions = (selected, options) => options.map(([value, label]) => `<option value="${value}"${String(selected || '') === value ? ' selected' : ''}>${label}</option>`).join('');
  const personaSelect = (id, label, selected, options) => `<label>${label}<select id="${id}">${personaOptions(selected, options)}</select></label>`;
  const personaContractMarkup = contract => `<section class="v4-persona-contract" aria-label="表达细节"><h3>语气与节奏</h3>
    <div class="v4-persona-controls">
      ${personaSelect('v4PersonaWarmth', '温度', contract.warmth, [['calm','冷静'],['balanced','平衡'],['warm','温暖'],['expressive','鲜明']])}
      ${personaSelect('v4PersonaDirectness', '直接程度', contract.directness, [['gentle','委婉'],['balanced','平衡'],['direct','直接']])}
      ${personaSelect('v4PersonaInitiative', '主动程度', contract.initiative, [['restrained','克制'],['responsive','按需响应'],['proactive','适度主动']])}
      ${personaSelect('v4PersonaHumor', '幽默', contract.humor, [['none','不使用'],['light','轻微'],['playful','活泼'],['dry','冷幽默']])}
      ${personaSelect('v4PersonaRhythm', '节奏', contract.rhythm, [['concise','简洁'],['natural','自然'],['varied','有变化'],['structured','结构化']])}
      ${personaSelect('v4PersonaQuestionPolicy', '追问策略', contract.question_policy, [['minimal','尽量少问'],['contextual','按语境'],['clarify_when_needed','缺信息时澄清'],['engaged','积极互动']])}
      ${personaSelect('v4PersonaAddressPolicy', '称呼策略', contract.address_policy, [['natural','自然使用'],['preferred','优先首选称呼'],['avoid_repetition','避免反复称呼']])}
      ${personaSelect('v4PersonaPrivateLength', '私聊长度', contract.private_length, [['short','短'],['balanced','适中'],['detailed','详细']])}
      ${personaSelect('v4PersonaGroupLength', '群聊长度', contract.group_length, [['brief','极简'],['short','短'],['balanced','适中']])}
      ${personaSelect('v4PersonaWorkLength', '工作长度', contract.work_length, [['compact','紧凑'],['structured_compact','结构化紧凑'],['detailed','详细']])}
      ${personaSelect('v4PersonaMemePolicy', '表情策略', contract.meme_policy, [['never','不使用'],['contextual','按语境'],['frequent','较频繁']])}
    </div><details class="v4-profile-subsection"><summary>群聊表达细节</summary><div class="v4-persona-controls">
      ${personaSelect('v4PersonaGroupStance', '群聊切入方式', contract.group_stance, [['observant','具体切入'],['quick_witted','反应快'],['direct_playful','直接轻快']])}
      ${personaSelect('v4PersonaGroupReactionStyle', '群聊反应质感', contract.group_reaction_style, [['specific','具体回应'],['dry','轻微冷幽默'],['playful','轻快接梗']])}
      ${personaSelect('v4PersonaGroupSentenceRhythm', '群聊句子节奏', contract.group_sentence_rhythm, [['one_beat','一句落点'],['two_beats','两句短节奏'],['varied','按语境变化']])}
      ${personaSelect('v4PersonaGroupEndingPolicy', '群聊收话方式', contract.group_ending_policy, [['drop','自然收住'],['contextual_hook','语境钩子'],['varied','按语境变化']])}
    </div>
    </details><h3>用词与边界</h3><div class="v4-persona-lists">
      <label>核心价值（每行一项）<textarea id="v4PersonaValues" rows="4">${esc(personaListValue(contract.values))}</textarea></label>
      <label>行为边界（每行一项）<textarea id="v4PersonaBoundaries" rows="4">${esc(personaListValue(contract.boundaries))}</textarea></label>
      <label>偏好用词（每行一项）<textarea id="v4PersonaPreferredPhrases" rows="4">${esc(personaListValue(contract.preferred_phrases))}</textarea></label>
      <label>避免用词（每行一项）<textarea id="v4PersonaAvoidPhrases" rows="4">${esc(personaListValue(contract.avoid_phrases))}</textarea></label>
      <label>禁止表达套路（每行一项）<textarea id="v4PersonaProhibitedPatterns" rows="4">${esc(personaListValue(contract.prohibited_patterns))}</textarea></label>
    </div>
    <details class="v4-profile-subsection"><summary>高级：表达示例</summary><label>示例（JSON 数组，最多 6 组）<textarea id="v4PersonaExamples" rows="8" spellcheck="false">${esc(personaExamplesValue(contract.examples))}</textarea></label></details>
  </section>`;
  const readPersonaWorkspaceDraft = (root, workspace) => {
    let examples;
    try { examples = JSON.parse($('#v4PersonaExamples', root).value || '[]'); }
    catch (_) { throw new Error('示例必须是有效的 JSON 数组'); }
    if (!Array.isArray(examples)) throw new Error('示例必须是 JSON 数组');
    const value = id => $(`#${id}`, root).value.trim();
    return {
      display_name: value('v4AssistantName'), relationship: value('v4AssistantRelationship'),
      persona: value('v4AssistantPersona'), style: value('v4AssistantStyle'),
      expected_updated_at: personaWorkspaceCore.expectedUpdatedAt(workspace),
      voice_contract: {
        schema_version: 1, identity_core: value('v4IdentityCore'),
        relationship_stance: value('v4RelationshipStance'), work_continuity: value('v4WorkContinuity'),
        values: personaListDraft(value('v4PersonaValues')), boundaries: personaListDraft(value('v4PersonaBoundaries')),
        preferred_phrases: personaListDraft(value('v4PersonaPreferredPhrases')), avoid_phrases: personaListDraft(value('v4PersonaAvoidPhrases')),
        prohibited_patterns: personaListDraft(value('v4PersonaProhibitedPatterns')),
        warmth: value('v4PersonaWarmth'), directness: value('v4PersonaDirectness'), initiative: value('v4PersonaInitiative'),
        humor: value('v4PersonaHumor'), rhythm: value('v4PersonaRhythm'), question_policy: value('v4PersonaQuestionPolicy'),
        address_policy: value('v4PersonaAddressPolicy'), private_length: value('v4PersonaPrivateLength'),
        group_length: value('v4PersonaGroupLength'), work_length: value('v4PersonaWorkLength'), meme_policy: value('v4PersonaMemePolicy'),
        group_stance: value('v4PersonaGroupStance'), group_reaction_style: value('v4PersonaGroupReactionStyle'),
        group_sentence_rhythm: value('v4PersonaGroupSentenceRhythm'), group_ending_policy: value('v4PersonaGroupEndingPolicy'),
        examples,
      },
    };
  };
  function hydratePersonaWorkspace(root, workspace) {
    const contract = workspace?.voice_contract || workspace?.persona?.voice_contract || {};
    const set = (id, value) => { const field = $(`#${id}`, root); if (field) field.value = value ?? ''; };
    set('v4AssistantName', workspace?.assistant?.display_name || workspace?.persona?.display_name || '');
    set('v4AssistantRelationship', workspace?.assistant?.relationship || workspace?.persona?.relationship || '');
    set('v4AssistantPersona', workspace?.assistant?.persona || workspace?.persona?.persona || '');
    set('v4AssistantStyle', workspace?.assistant?.style || workspace?.persona?.style || '');
    const ids = {
      identity_core:'v4IdentityCore', relationship_stance:'v4RelationshipStance', work_continuity:'v4WorkContinuity',
      values:'v4PersonaValues', boundaries:'v4PersonaBoundaries', preferred_phrases:'v4PersonaPreferredPhrases',
      avoid_phrases:'v4PersonaAvoidPhrases', prohibited_patterns:'v4PersonaProhibitedPatterns', examples:'v4PersonaExamples',
      warmth:'v4PersonaWarmth', directness:'v4PersonaDirectness', initiative:'v4PersonaInitiative', humor:'v4PersonaHumor',
      rhythm:'v4PersonaRhythm', question_policy:'v4PersonaQuestionPolicy', address_policy:'v4PersonaAddressPolicy',
      private_length:'v4PersonaPrivateLength', group_length:'v4PersonaGroupLength', work_length:'v4PersonaWorkLength',
      meme_policy:'v4PersonaMemePolicy', group_stance:'v4PersonaGroupStance', group_reaction_style:'v4PersonaGroupReactionStyle',
      group_sentence_rhythm:'v4PersonaGroupSentenceRhythm', group_ending_policy:'v4PersonaGroupEndingPolicy',
    };
    Object.entries(ids).forEach(([key, id]) => set(id, key === 'examples' ? personaExamplesValue(contract[key]) : (Array.isArray(contract[key]) ? personaListValue(contract[key]) : contract[key])));
  }
  const state = {
    bootstrap: null,
    route: 'now',
    attention: null,
    attentionReady: false,
    attentionOpen: false,
    attentionPoll: null,
    attentionSeenStore: null,
    pendingSelection: null,
    unseenAttention: [],
    attentionStale: false,
    navigationEpoch: 0,
    pageFault: null,
    qqRosterCache: null,
    webChatDraft: null,
  };
  const $ = (selector, root = document) => root.querySelector(selector);
  const all = (selector, root = document) => [...root.querySelectorAll(selector)];
  const esc = (value) => String(value ?? '').replace(/[&<>'"]/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  }[character]));
  const asList = (value) => Array.isArray(value) ? value : [];
  const unpack = (value, key) => value?.[key] ?? value?.result ?? value ?? {};
  const pick = (value, keys, fallback = '—') => keys.map((key) => value?.[key])
    .find((item) => item !== undefined && item !== null && item !== '') ?? fallback;
  const safe = (value) => value?._error ? '暂时无法读取' : value;
  const records = (value, ...keys) => {
    const sources = [value, value?.result];
    for (const source of sources) {
      if (Array.isArray(source)) return source;
      for (const key of keys) if (Array.isArray(source?.[key])) return source[key];
    }
    return [];
  };
  const shanghaiTime = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  });
  const formatTime = (value) => {
    if (!value) return '暂无时间记录';
    const raw = String(value);
    // Persisted timestamps with an offset are absolute instants.  A timestamp
    // without one is a legacy local display value, so do not silently guess a
    // browser-dependent time zone for it.
    if (!/(?:z|[+-]\d{2}:?\d{2})$/i.test(raw)) return raw.replace('T', ' ');
    const instant = new Date(raw);
    return Number.isNaN(instant.getTime()) ? '时间格式无效' : shanghaiTime.format(instant).replaceAll('/', '-');
  };
  const page = (eyebrow, title, description, body) => `
    <section class="v4-page v4-page-${esc(title)}">
      <header class="v4-page-head"><h1 tabindex="-1">${esc(title)}</h1>${description ? `<span>${esc(description)}</span>` : ''}</header>
      ${body}
    </section>`;
  const localNav = (name, items, active) => `<nav class="v4-local-nav" aria-label="${esc(name)}">
    ${items.map(([id, label]) => `<button type="button" data-v4-${name}="${esc(id)}" aria-current="${id === active ? 'page' : 'false'}">${esc(label)}</button>`).join('')}
  </nav>`;
  const empty = (message = '暂无可显示的记录。') => `<p class="v4-empty">${esc(message)}</p>`;
  const recordPageFault = (route, error) => {
    const status = Number(error?.status);
    let code = 'page_render_failed';
    if (error?.name === 'RequestTimeoutError') code = 'request_timeout';
    else if (error?.name === 'AbortError') code = 'request_aborted';
    else if (Number.isInteger(status) && status >= 400 && status <= 599) code = `http_${status}`;
    state.pageFault = {
      route: String(route || ''),
      code,
      occurredAt: new Date().toISOString(),
    };
    return state.pageFault;
  };
  // Only these service reason codes are safe to turn into Owner-facing copy.
  // Never surface arbitrary exception strings: provider failures can contain
  // upstream diagnostics or credentials.
  const PUBLIC_ACTION_ERRORS = Object.freeze({
    console_update_required: '页面版本已更新，请刷新页面后再重试。',
    primary_model_unavailable: '所选主模型或其连接已停用，不能保存。',
    fallback_model_unavailable: '所选备用模型或其连接已停用，不能保存。',
    work_executor_fallback_not_supported: '工作执行器不支持备用模型；请只选择一条已验证路径。',
    work_executor_requires_tool_support: '工作执行模型必须具备工具能力。',
    custom_executor_role_not_supported: '自定义工作执行适配器只能绑定“交互与执行”角色，不能作为对话或群参与模型。',
    executor_profile_missing: '尚未配置 Executor Profile。',
    executor_profile_not_applied: 'Executor Profile 尚未应用；请保存后再验证。',
    executor_verification_required: '工作执行器尚未通过隔离验证。',
    executor_verification_failed: '工作执行器验证失败；请修复连接后再验证。',
    executor_verification_stale: '连接配置已变更，之前的工作执行验证已失效。',
    executor_verification_unavailable: '隔离工作执行验证暂不可用；当前配置没有被切换。',
    work_executor_activation_required: '自定义工作执行器必须通过“验证并切换”操作生效。',
    work_executor_activation_failed: '切换工作执行器没有完成；运行时仍保持原状态。请刷新后再决定是否重试。',
    executor_bound_profile_not_applied: '当前已绑定的执行器配置不是已应用状态，不能进行运行时核验。',
    executor_runtime_stage_requires_active_runtime: '候选验证无法建立可恢复的运行时环境。',
    executor_runtime_stage_restore_failed: '候选验证后未能确认恢复原工作执行器；已停止切换，请先在诊断中核验运行时。',
    executor_runtime_unavailable: '工作执行运行环境暂不可用。',
    executor_runtime_apply_failed: '代理运行时应用失败，当前工作执行器没有切换。',
    executor_runtime_rollback_failed: '切换失败且运行时恢复未确认；请立即在诊断中核验。',
    executor_proxy_health_timeout: '代理启动后未在限定时间内通过健康检查。',
    executor_proxy_health_unavailable: '代理健康检查暂不可用。',
    executor_proxy_model_mismatch: '代理已启动，但模型与本次选择不一致。',
    model_disabled: '所选模型已停用。',
    provider_disabled: '所选连接已停用。',
    executor_transport_unsupported: '所选连接不是支持的工作执行器传输。',
    provider_not_trusted: '该连接未标记为受信任工作执行器。',
    tools_capability_missing: '该模型未声明工具能力。',
    invalid_provider_base_url: '基础地址必须是有效的 HTTP(S) 地址。',
    insecure_remote_base_url: '远程基础地址必须使用 HTTPS。',
    provider_billing_transport_mismatch: '传输协议与计费范围不匹配。',
    invalid_provider_transport: '不支持这项传输协议。',
    invalid_provider_billing_scope: '不支持这项计费范围。',
    model_label_required: '请填写模型显示名称。',
    model_name_required: '请填写接口模型名。',
    model_in_use: '该模型仍被角色路由使用；请先把相关角色切换到其他模型。',
    model_used_by_executor_profile: '该模型仍被 Executor Profile 使用；请先修改或移除该 Profile。',
    provider_has_models: '该连接仍包含模型目录项；请先移除不再使用的模型。',
    provider_used_by_executor_profile: '该连接仍被 Executor Profile 或其验证状态引用；请先处理该 Profile。',
    provider_transport_change_requires_rebind: '连接传输协议不能在仍有模型、Profile 或验证状态引用时直接变更；请先建立新的连接并完成重新绑定。',
    runtime_owned_provider_read_only: '该连接由外部运行时拥有，不能在 Console 中删除。',
    model_not_found: '该模型已不存在；请刷新目录。',
    provider_not_found: '该连接已不存在；请刷新目录。',
    model_discovery_not_available_for_codex_connection: '该连接是 Codex 登录/自定义 Provider，不支持读取模型列表；请手动填写模型信息。',
    provider_disabled: '该连接已停用，不能读取模型列表；请先在连接中启用它。',
    provider_secret_missing: '该连接尚未设置密钥，不能读取模型列表；请先在连接中配置密钥。',
    provider_secret_or_url_unavailable: '该连接的密钥或端点不可用，不能读取模型列表；请检查连接配置。',
    automation_revision_conflict: '计划已被其他操作更新；请刷新后再决定。',
    automation_revision_required: '计划版本缺失；请刷新列表后再修改。',
    automation_revision_invalid: '计划版本无效；请刷新列表后再修改。',
    automation_archived: '这项计划已经归档；如需修改，请先恢复。',
    automation_archive_state_conflict: '计划正在运行或状态已变化；请刷新后再操作。',
    automation_job_not_found: '计划已不存在；请刷新列表。',
    execution_contract_not_ready: '计划所需执行参数尚未就绪；请先保存为暂停并补齐配置。',
    run_at_must_be_future: '一次性计划的执行时间必须晚于现在。',
  });
  const userError = (error, fallback = '操作没有完成，请稍后重试。') => {
    const status = Number(error?.status);
    const reason = String(error?.payload?.error || '');
    if (error?.name === 'RequestTimeoutError') return '请求超时，未确认操作是否完成；请先刷新状态再决定是否重试。';
    if (error?.name === 'AbortError') return '读取已被新的页面操作取消。';
    if (status === 401 || status === 403) return '当前账号没有执行这项操作的权限。';
    if (status === 404) return '需要的对象已不存在或暂时不可读取。';
    if (PUBLIC_ACTION_ERRORS[reason]) return PUBLIC_ACTION_ERRORS[reason];
    if (status === 409) return '状态已经变化；请刷新后再决定下一步。';
    return fallback;
  };
  const renderReadFailure = (root, route, title, error) => {
    recordPageFault(route, error);
    root.innerHTML = page('暂时无法读取', title, '服务没有完成读取；没有改写任何设置、对话或投递。可以稍后重试，或在 Console 查看受控诊断。', '<p class="v4-error" role="status">当前页面数据暂不可用。</p>');
  };
  const statusLabel = (value) => ({
    active: '进行中', running: '进行中', queued: '等待处理', pending: '等待处理',
    completed: '已完成', succeeded: '已完成', failed: '需要处理', timeout: '需要处理',
    cancelled: '已取消', archived: '已归档', available: '等待系统处理', scheduled: '等待系统处理',
    leased: '正在发送', confirmed: '应用层已确认',
  }[String(value || '').toLowerCase()] || value || '状态未知');
  const recordTitle = (item, keys) => pick(item, keys, '未命名项目');
  const MODEL_CAPABILITY_LABELS = Object.freeze({ text: '文本', tools: '工具', vision: '视觉', embedding: '嵌入', structured_output: '结构化输出' });
  const PROVIDER_TRANSPORT_LABELS = Object.freeze({
    codex_cli_chatgpt: 'Codex 登录',
    codex_cli_custom_provider: 'Codex 自定义 Provider',
    openai_chat_completions: 'OpenAI 兼容',
    azure_openai_chat_completions: 'Azure OpenAI',
    anthropic_messages: 'Anthropic',
    google_gemini_generate_content: 'Gemini',
  });
  const PROVIDER_BILLING_LABELS = Object.freeze({ chatgpt_subscription: 'ChatGPT 订阅', api_key: 'API Key', local_proxy: '本地代理' });
  const modelCapabilitySummary = (model) => {
    const caps = asList(model?.capabilities).map((capability) => MODEL_CAPABILITY_LABELS[capability] || capability);
    return caps.length ? caps.join('、') : '未声明能力';
  };
  const modelPriceSummary = (model) => {
    const input = model?.input_price_per_million;
    const output = model?.output_price_per_million;
    if (input == null && output == null) return '';
    return ` · ${input ?? 0}/${output ?? 0} ${model?.price_currency || 'USD'}/百万 Token`;
  };
  // 与服务端 bridge_model_registry._slug 保持一致：小写、非 [a-z0-9_-] 替换为 -、去首尾 -_、限 64 字符。
  const slugifyModelId = (providerId, modelName) => {
    const raw = String(`${providerId || ''}-${modelName || ''}`).toLowerCase()
      .replace(/[^a-z0-9_-]+/g, '-').replace(/^[-_]+|[-_]+$/g, '');
    const clipped = raw.slice(0, 64).replace(/[-_]+$/g, '');
    return /^[a-z0-9][a-z0-9_-]{1,63}$/.test(clipped) ? clipped : '';
  };
  // 主流模型族的保守推荐规格（上下文/最大输出/能力）。这些是推荐值而非厂商精确规格，
  // 保存前可手动调整；未命中的模型用保守默认并保留“未知”语义。
  const MODEL_DEFAULT_SPECS = Object.freeze([
    { match: /^deepseek/ },
    { match: /^qwen/ },
    { match: /^glm/ },
    { match: /^kimi/ },
    { match: /^minimax/ },
    { match: /^grok/ },
    { match: /^gpt/ },
    { match: /^mimo/ },
    { match: /^hy/ },
    { match: /^doubao/ },
    { match: /^ernie/ },
    { match: /^hunyuan/ },
    { match: /^moonshot/ },
    { match: /^yi-/ },
    { match: /^claude/ },
    { match: /^gemini/ },
  ]);
  const RECOMMENDED_CAPABILITY_INPUTS = Object.freeze({
    text: 'capability_text',
    tools: 'capability_tools',
    vision: 'capability_vision',
    embedding: 'capability_embedding',
    structured_output: 'capability_structured',
  });
  const recommendModelSpecs = (modelName) => {
    const name = String(modelName || '').toLowerCase().trim();
    if (!name) return { context: 0, output: 4096, capabilities: ['text'], recommended: false, reason: '未知模型' };
    const known = MODEL_DEFAULT_SPECS.some((spec) => spec.match.test(name));
    if (!known) return { context: 0, output: 4096, capabilities: ['text'], recommended: false, reason: '未知模型' };
    const capabilities = ['text', 'tools'];
    if (/\b(?:omni|vision)\b/.test(name)) capabilities.push('vision');
    if (/\b(?:embed|embedding)\b/.test(name)) capabilities.push('embedding');
    if (/\b(?:reasoner|reasoning|thinking|r1)\b/.test(name)) {
      return { context: 128000, output: 32768, capabilities, recommended: true, reason: '推理模型推荐' };
    }
    return { context: 128000, output: 8192, capabilities, recommended: true, reason: '主流模型推荐' };
  };
  const pageCurrent = (root) => typeof root.v4Current !== 'function' || root.v4Current();
  const cards = (items, renderer) => asList(items).length ? items.map(renderer).join('') : empty();
  const load = async (entries) => Object.fromEntries(await Promise.all(entries.map(async ([key, action]) => {
    try { return [key, await action()]; } catch (error) { return [key, { _error: userError(error), _error_name: String(error?.name || ''), _error_status: Number(error?.status || 0), _request_id: String(error?.payload?.request_id || error?.requestId || '') }]; }
  })));

  const clamp = (value, minimum, maximum, fallback) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? Math.min(maximum, Math.max(minimum, parsed)) : fallback;
  };
  function readLocalPreferences() {
    try {
      const saved = JSON.parse(localStorage.getItem('nekoagent-v4-preferences') || '{}');
      return saved && typeof saved === 'object' ? saved : {};
    } catch (_) { return {}; }
  }
  function applyLocalPreferences() {
    const saved = readLocalPreferences();
    document.documentElement.dataset.v4Theme = saved.theme || 'system';
    document.documentElement.dataset.v4Density = saved.density || 'comfortable';
    document.documentElement.dataset.v4Motion = saved.motion || 'system';
    applyAppearance(saved);
  }
  function applyAppearance(appearance = {}) {
    const root = document.documentElement;
    const enabled = appearance.background_enabled === true || String(appearance.background_enabled || '') === '1';
    let background = 'none';
    if (enabled && appearance.background_url) {
      try {
        const url = new URL(String(appearance.background_url), window.location.origin);
        if (url.protocol === 'https:' || url.protocol === 'http:') background = `url("${url.href.replace(/"/g, '%22')}")`;
      } catch (_) { background = 'none'; }
    }
    root.style.setProperty('--v4-custom-background', background);
    root.style.setProperty('--v4-custom-background-dim', String(clamp(appearance.background_dim, 0, 0.96, 0.12)));
    root.style.setProperty('--v4-panel-opacity', String(clamp(appearance.panel_opacity, 0.72, 1, 0.88)));
  }

  function renderAttentionCenter() {
    const trigger = $('#v4AttentionCenter'); const panel = $('#v4AttentionPanel'); const list = $('#v4AttentionList'); const status = $('#v4AttentionStatus');
    if (!trigger || !panel || !list || !status) return;
    const items = state.attention?.attention || [];
    const unread = state.unseenAttention.length;
    trigger.setAttribute('aria-expanded', String(state.attentionOpen));
    trigger.setAttribute('aria-label', unread ? `需要处理，${unread} 项未读` : '需要处理');
    $('#v4AttentionCount').textContent = unread ? String(unread) : '';
    $('#v4AttentionCount').hidden = unread === 0;
    panel.hidden = !state.attentionOpen;
    list.innerHTML = items.length ? items.map((item) => `<li><button type="button" data-v4-attention-item="${esc(item.dedupeKey)}"><span class="v4-status ${esc(item.priority)}">${esc(attentionPriorityLabel(item.priority))}</span><span><strong>${esc(item.title)}</strong><small>${esc(item.reason)}</small></span></button></li>`).join('') : '<li class="v4-attention-empty">当前没有需要处理的事项。</li>';
    status.textContent = state.attentionStale
      ? '需要处理的状态暂未更新；正在保留上次已知结果。'
      : (unread ? `有 ${unread} 项需要你处理。` : '');
    all('[data-v4-attention-item]', list).forEach((button) => button.addEventListener('click', () => {
      const item = items.find((candidate) => candidate.dedupeKey === button.dataset.v4AttentionItem);
      if (!item) return;
      state.attentionOpen = false;
      renderAttentionCenter();
      openAttentionItem(item);
    }));
  }

  function setAttentionHome(home, { allowBrowserPrompt = false } = {}) {
    const projection = attentionApi.projectHome(home);
    const assistantChanged = state.attention?.assistantId && state.attention.assistantId !== projection.assistantId;
    if (!state.attentionSeenStore || assistantChanged) {
      state.attentionSeenStore = attentionApi.createSeenStore(localStorage, projection.assistantId);
      state.attentionReady = false;
    }
    const fresh = state.attentionReady
      ? attentionApi.freshAttention(state.attention?.attention || [], projection.attention)
      : [];
    state.attention = projection;
    state.unseenAttention = state.attentionSeenStore.unseen(projection.attention);
    state.attentionReady = true;
    renderAttentionCenter();
    if (allowBrowserPrompt && fresh.length && readLocalPreferences().local_notifications && document.hidden && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
      const prompt = attentionApi.browserPrompt(fresh);
      new Notification(prompt.title, { body: prompt.body });
    }
    return projection;
  }

  function ownerBriefAsAttention(brief = {}) {
    const assistant = brief.assistant || {};
    const decisions = asList(brief.decisions).map((item) => ({
      dedupe_key: `${item.source_type || 'decision'}:${item.source_id || ''}`,
      source_type: item.source_type || 'approval', source_id: item.source_id || '',
      priority: 'high', title: item.title || '需要你的决定',
      reason: item.allowed_action || 'review_approval', updated_at: item.updated_at || '',
    }));
    return { assistant, attention: { items: decisions }, active_tasks: asList(brief.in_progress) };
  }

  async function refreshAttention({ allowBrowserPrompt = false } = {}) {
    try {
      const result = await api.ownerBrief();
      state.attentionStale = false;
      return setAttentionHome(ownerBriefAsAttention(unpack(result, 'result')), { allowBrowserPrompt });
    } catch (error) {
      state.attentionStale = true;
      renderAttentionCenter();
      throw error;
    }
  }

  function stopAttentionPolling() {
    if (state.attentionPoll) window.clearInterval(state.attentionPoll);
    state.attentionPoll = null;
  }

  function startAttentionPolling() {
    stopAttentionPolling();
    state.attentionPoll = window.setInterval(() => { refreshAttention({ allowBrowserPrompt: true }).catch(() => {}); }, 60000);
  }

  async function requestLocalNotificationPermission(enabled) {
    if (!enabled) return '';
    if (typeof Notification === 'undefined') return '当前浏览器不支持系统通知；应用内提示仍可用。';
    if (Notification.permission === 'default') await Notification.requestPermission();
    return Notification.permission === 'granted' ? '' : '系统通知未授权；应用内提示仍可用。';
  }

  function openApprovalReview(approvalId) {
    state.pendingSelection = { route: 'work', sourceType: 'approval', sourceId: String(approvalId) };
    navigate('work');
  }

  function openAttentionItem(item) {
    if (!item) return;
    state.attentionSeenStore?.markSeen([item]);
    state.unseenAttention = state.attentionSeenStore?.unseen(state.attention?.attention || []) || [];
    if (item.sourceType === 'approval') { openApprovalReview(item.sourceId); return; }
    state.pendingSelection = { route: item.route, sourceType: item.sourceType, sourceId: item.sourceId };
    navigate(item.route);
  }

  function deliveryPresentation(delivery) {
    if (delivery.superseded_by) return ['已取消', '这项投递已被后续版本替代。', 'muted'];
    if (delivery.dead_letter || delivery.state === 'dead_letter') return ['需要重新处理', '系统未能完成本次投递；请确认后重试。', 'danger'];
    if (delivery.acked_at || delivery.state === 'confirmed') return ['应用层已确认', '系统已收到确认；这不等同于已独立验证 QQ 客户端投影。', 'success'];
    if (['available', 'scheduled', 'leased'].includes(delivery.state)) return ['等待系统处理', '仍在受控投递流程中。', 'warning'];
    return [statusLabel(delivery.state), '状态以同一 Delivery 事实源为准。', 'muted'];
  }

  const attentionPriorityLabel = (priority) => ({ critical: '紧急', high: '需要处理', normal: '请留意' }[priority] || '请留意');
  const attentionOwnerLabel = (route) => ({ work: '前往工作处理', qq: '前往 QQ 处理', console: '前往 Console 查看' }[route] || '查看事项');



  function renderQqAdmission(host, rawAccess) {
    const access = unpack(rawAccess, 'settings');
    const settings = access.settings || rawAccess.settings || {};
    // Keep the canonical QQ id alongside the editable draft.  A local row or
    // access-mode selection is not an authorization result until POST succeeds
    // and the server returns its canonical projection.
    const people = asList(access.private_allowlist || rawAccess.private_allowlist).map((item) => ({
      ...item,
      __server_qq_id: String(item.qq_id || '').trim(),
    }));
    const groups = asList(access.group_allowlist || rawAccess.group_allowlist).map((item) => ({ ...item }));
    const administrators = asList(access.administrators || rawAccess.administrators);
    let liveGroupsById = new Map();
    // 私聊准入模式是一个独立的用户动作：由操作者显式选择，绝不因添加或移除名单而隐式变更。
    const modeOptions = [['admin_only', '仅管理员'], ['allowlist', '允许名单']];
    const modeLabel = (value) => ({ admin_only: '仅管理员', allowlist: '允许名单', disabled: '已停用' }[value] || `未知（${value || '未设置'}）`);
    const serverAccessMode = String(settings.access_mode || '');
    const modeEditable = modeOptions.some(([value]) => value === serverAccessMode);
    let draftAccessMode = serverAccessMode;
    const isEnabledAdmin = (qqId) => administrators.some((item) => (
      String(item.qq_id || '') === String(qqId || '')
      && item.enabled !== false
      && ['super_admin', 'admin'].includes(String(item.role || ''))
    ));
    const privateEntryState = (item) => {
      const qqId = String(item.qq_id || '').trim();
      const locallyChanged = Boolean(item.__draft_new)
        || qqId !== String(item.__server_qq_id || '').trim();
      const modePending = draftAccessMode !== serverAccessMode;
      if (!qqId) return '待填写 QQ 号';
      if (item.enabled === false) return '已禁用';
      if (!settings.channel_enabled || !settings.private_chat_enabled || draftAccessMode === 'disabled') return 'QQ 私聊通道未开启';
      if (locallyChanged || modePending) {
        if (draftAccessMode === 'allowlist') return '待保存；保存并回读后允许名单可私聊';
        if (draftAccessMode === 'admin_only') return '待保存；保存后仅管理员可私聊';
        return '待保存；等待服务器确认当前模式';
      }
      if (isEnabledAdmin(qqId)) return '管理员：当前可私聊';
      if (serverAccessMode === 'allowlist') return '当前可私聊';
      if (serverAccessMode === 'admin_only') return '已保存；当前仅管理员可私聊';
      return '等待服务器确认当前模式';
    };
    const privateSaveSummary = (canonical) => {
      const canonicalAccess = unpack(canonical, 'settings');
      const canonicalSettings = canonicalAccess.settings || canonical.settings || {};
      const canonicalPeople = asList(canonicalAccess.private_allowlist || canonical.private_allowlist);
      const canonicalAdministrators = asList(canonicalAccess.administrators || canonical.administrators);
      const blocked = canonicalPeople.filter((item) => (
        item.enabled !== false
        && String(canonicalSettings.access_mode || '') === 'admin_only'
        && !canonicalAdministrators.some((admin) => (
          String(admin.qq_id || '') === String(item.qq_id || '')
          && admin.enabled !== false
          && ['super_admin', 'admin'].includes(String(admin.role || ''))
        ))
      )).length;
      const base = `准入范围已保存；当前私聊模式：${modeLabel(String(canonicalSettings.access_mode || ''))}。`;
      return blocked ? `${base}${blocked} 名允许名单联系人已保存，但在仅管理员模式下暂不能私聊。` : base;
    };
    const syncDraftFromDom = () => {
      all('[data-v4-access="private"]', host).forEach((input) => { if (people[Number(input.dataset.index)]) people[Number(input.dataset.index)].qq_id = input.value.trim(); });
      all('[data-v4-access="group"]', host).forEach((input) => { if (groups[Number(input.dataset.index)]) groups[Number(input.dataset.index)].group_id = input.value.trim(); });
      const modeSelect = $('#v4AdmissionMode', host);
      if (modeSelect) draftAccessMode = String(modeSelect.value);
    };
    const modeField = () => (modeEditable
      ? `<label><span>私聊准入模式</span><select id="v4AdmissionMode" aria-label="私聊准入模式">${modeOptions.map(([value, label]) => `<option value="${value}"${value === draftAccessMode ? ' selected' : ''}>${label}</option>`).join('')}</select></label>`
      : `<small id="v4AdmissionModeFixed">服务器返回的私聊模式是「${esc(modeLabel(serverAccessMode))}」，不在本页可编辑范围内；保存时会原样保留。</small>`);
    const effectivePrivateScope = () => {
      if (serverAccessMode === 'admin_only') return '当前实际可见：仅管理员。下方私聊允许名单会保留，但不会增加当前可读取的私聊。';
      if (serverAccessMode === 'allowlist') return '当前实际可见：允许名单。下方私聊允许名单会决定当前可读取的私聊。';
      return '当前实际可见：服务器未返回可识别的私聊模式；保存时会原样保留，请先核对服务端设置。';
    };
    const groupRosterStatus = (live) => {
      const membership = ({ joined: '成员：仍在群', absent: '成员：已离群', unknown: '成员：待核验' })[String(live.membership || '')];
      const speaking = ({ allowed: '发言：可发言', muted: '发言：被禁言', unknown: '发言：待核验' })[String(live.speaking || '')];
      const freshness = ({ fresh: '名册：近期已核验', stale: '名册：记录已过期', unverified: '名册：待核验' })[String(live.freshness || '')];
      const reason = ({
        group_available: '运行：可用', group_absent: '运行：已离群', group_muted: '运行：被禁言',
        group_state_unknown: '运行：待核验', group_state_stale: '运行：记录已过期',
        group_channel_stale: '运行：通道状态待核验', group_account_unknown: '运行：账号待核验',
        group_state_not_activated: '运行：尚未启用',
      })[String(live.reason || '')];
      const parts = [membership, speaking, freshness, reason].filter(Boolean);
      return parts.length ? parts.join(' · ') : '待运行时核验';
    };
    const groupIdentity = (item) => {
      const groupId = String(item.group_id || '');
      const live = liveGroupsById.get(groupId) || {};
      const liveName = String(live.group_name || live.name || '').trim();
      const remark = String(item.remark || '').trim();
      return `<label class="v4-access-row v4-access-group-row"><input data-v4-access="group" data-index="${groups.indexOf(item)}" value="${esc(groupId)}" inputmode="numeric" aria-label="允许的群号"><span class="v4-admission-group-copy"><strong data-v4-group-live-name="${esc(groupId)}">${esc(liveName || '待运行时核验')}</strong><small>群名称</small><small>管理备注：${esc(remark || '未填写')}</small><small data-v4-group-roster-status>名册状态：${esc(groupRosterStatus(live))}</small></span><button type="button" data-v4-access-remove="group" data-index="${groups.indexOf(item)}">移除</button></label>`;
    };
    const updateLiveGroupNames = () => {
      all('[data-v4-group-live-name]', host).forEach((node) => {
        const row = node.closest('.v4-access-group-row');
        const groupInput = row?.querySelector('[data-v4-access="group"]');
        const groupId = String(groupInput?.value || node.dataset.v4GroupLiveName || '').trim();
        const live = liveGroupsById.get(groupId) || {};
        const liveName = String(live.group_name || live.name || '').trim();
        node.textContent = liveName || '待运行时核验';
        const rosterStatus = row?.querySelector('[data-v4-group-roster-status]');
        if (rosterStatus) rosterStatus.textContent = `名册状态：${groupRosterStatus(live)}`;
      });
    };
    const render = () => {
      host.innerHTML = `<form id="v4AdmissionForm" class="v4-admission-layout"><section><p>准入</p><h2>谁可以接入</h2><span>这里只管理人员与群的允许范围，不包含机器人连接、探针或网络状态。</span><small id="v4AdmissionModeSummary">当前私聊模式：${esc(modeLabel(serverAccessMode))}。${esc(effectivePrivateScope())} 添加或移除名单不会改变模式；请用下面的「私聊准入模式」显式选择后保存。</small>${modeField()}</section><section class="v4-admission-list"><div><h3>私聊允许名单</h3><div id="v4PrivateAllowlist">${cards(people, (item, index) => `<label class="v4-access-row"><input data-v4-access="private" data-index="${index}" value="${esc(item.qq_id || '')}" inputmode="numeric" aria-label="允许的 QQ 号"><span>${esc(item.remark || '未命名联系人')}</span><small data-v4-private-effect="${index}">${esc(privateEntryState(item))}</small><button type="button" data-v4-access-remove="private" data-index="${index}">移除</button></label>`)}</div><button type="button" class="v4-add" data-v4-access-add="private">添加 QQ</button></div><div><h3>群聊允许名单</h3><p class="v4-admission-helper">群名称来自当前运行时群列表；管理备注只用于本页整理，不会覆盖群名称。</p><div id="v4GroupAllowlist">${cards(groups, groupIdentity)}</div><button type="button" class="v4-add" data-v4-access-add="group">添加群</button></div></section><div class="v4-inline-actions"><button class="v4-primary" type="submit">保存准入范围</button><p id="v4AdmissionStatus" role="status"></p></div></form>`;
      all('[data-v4-access-add]', host).forEach((button) => button.addEventListener('click', () => {
        syncDraftFromDom();
        (button.dataset.v4AccessAdd === 'private' ? people : groups).push(button.dataset.v4AccessAdd === 'private'
          ? { qq_id: '', remark: '', enabled: true, __server_qq_id: '', __draft_new: true } : { group_id: '', remark: '', enabled: true });
        render();
      }));
      all('[data-v4-access-remove]', host).forEach((button) => button.addEventListener('click', () => {
        syncDraftFromDom();
        const target = button.dataset.v4AccessRemove === 'private' ? people : groups;
        target.splice(Number(button.dataset.index), 1); render();
      }));
      $('#v4AdmissionMode', host)?.addEventListener('change', () => { syncDraftFromDom(); render(); });
      all('[data-v4-access="group"]', host).forEach((input) => input.addEventListener('input', updateLiveGroupNames));
      $('#v4AdmissionForm', host).addEventListener('submit', async (event) => {
        event.preventDefault();
        syncDraftFromDom();
        const notice = $('#v4AdmissionStatus', host); notice.textContent = '正在保存准入范围…';
        try {
          // Admission edits preserve per-group participation; the GET summary is not a batch-write command.
          // 准入模式只取操作者的显式选择；添加或移除名单不会隐式改写它。
          const nextSettings = { ...settings, access_mode: draftAccessMode };
          // Client-only draft bookkeeping must never cross the canonical API
          // boundary.  Preserve all server fields, stripping only UI metadata.
          const privateAllowlist = people.map(({ __server_qq_id, __draft_new, ...item }) => item);
          const canonical = await api.saveQqSettings({ expected_version: Number(settings.config_version || 0), settings: nextSettings, administrators, private_allowlist: privateAllowlist, group_allowlist: groups });
          // The service increments config_version on every accepted write.
          // Re-render from its canonical projection so a second same-page save
          // cannot deterministically submit a stale version.
          renderQqAdmission(host, canonical);
          $('#v4AdmissionStatus', host).textContent = privateSaveSummary(canonical);
        } catch (error) { notice.textContent = userError(error); }
      });
    };
    render();
    if (typeof api.qqGroupStates === 'function') {
      api.qqGroupStates().then((response) => {
        const state = unpack(response, 'result');
        liveGroupsById = new Map(asList(state.groups || state.items).map((item) => [String(item.group_id || item.id || ''), item]));
        updateLiveGroupNames();
      }).catch(() => {
        // The saved admission range remains editable when the live roster is unavailable.
      });
    }
  }

  function renderQqDeliveries(host, deliveries, refreshDeliveries) {
    host.innerHTML = `<section class="v4-delivery-intro"><div><p>Delivery</p><h2>投递</h2><span>查看当前助手最近发送了什么、发往哪里，以及哪些需要处理。</span></div><span class="v4-truth-note">确认边界：应用层 ACK 不等同于客户端已收到。</span></section><div id="v4DeliveryList" class="v4-delivery-list">${cards(deliveries, (item) => {
      const [label, detail, tone] = deliveryPresentation(item);
      return `<article class="v4-delivery-item"><div><span class="v4-status ${tone}">${esc(label)}</span><h3>${esc(recordTitle(item, ['summary', 'title', 'id']))}</h3><p>${esc(detail)}</p></div><div><strong>${esc(pick(item, ['destination_label'], 'QQ 通道'))}</strong><span>${esc(formatTime(pick(item, ['updated_at', 'created_at', 'acked_at'], '')))}</span>${item.dead_letter ? `<button class="v4-text-button" type="button" data-v4-delivery-retry="${esc(item.id)}">确认后重试</button>` : ''}</div></article>`;
    })}</div>`;
    all('[data-v4-delivery-retry]', host).forEach((button) => button.addEventListener('click', async () => {
      if (!window.confirm('确认重新处理这项失败投递？')) return;
      button.disabled = true;
      try {
        await api.requeueDelivery(button.dataset.v4DeliveryRetry);
        await refreshDeliveries('投递已重新进入受控队列，并已从服务器回读。');
      } catch (error) {
        if (error?.payload?.error === 'delivery_duplicate_risk_confirmation_required'
          && window.confirm('这项投递的最终客户端结果不确定。再次发送可能造成重复，仍要重新处理吗？')) {
          try {
            await api.requeueDelivery(button.dataset.v4DeliveryRetry, { confirmDuplicateRisk: true });
            await refreshDeliveries('已按重复风险确认重新进入队列，并已从服务器回读。');
            return;
          } catch (retryError) { button.textContent = userError(retryError); }
        } else button.textContent = userError(error);
        button.disabled = false;
      }
    }));
  }

  function renderQqBehavior(host, groupPolicies, rawAccess, rawWindowSettings, view = {}) {
    const access = rawAccess || {};
    const policiesByGroup = new Map(asList(groupPolicies).map((item) => [String(item.group_id || ''), item]));
    const allowedGroups = asList(access.group_allowlist).filter((item) => item?.enabled && (!view.groupId || String(item.group_id) === view.groupId));
    const windowSettings = unpack(rawWindowSettings, 'result');
    let defaults = windowSettings.defaults || {};
    let effectiveByGroup = { ...(windowSettings.effective_by_group || {}) };
    const expandedOverrides = new Set();

    const localDateTime = (value) => {
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return '';
      const parts = new Intl.DateTimeFormat('sv-SE', {
        timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
      }).formatToParts(date).reduce((result, part) => ({ ...result, [part.type]: part.value }), {});
      return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
    };
    const windows = (policy) => asList(policy?.allowed_windows).length === 2
      ? policy.allowed_windows : [{ start: '18:00', end: '09:00' }, { start: '12:00', end: '14:00' }];
    const policyFromForm = (form, prefix) => ({
      timezone: 'Asia/Shanghai',
      effective_at: `${String(form.elements[`${prefix}-effective-at`]?.value || '').trim()}:00+08:00`,
      allowed_windows: [0, 1].map((index) => ({
        start: String(form.elements[`${prefix}-window-${index}-start`]?.value || '').trim(),
        end: String(form.elements[`${prefix}-window-${index}-end`]?.value || '').trim(),
      })),
    });
    const windowFields = (policy, prefix) => {
      const active = windows(policy);
      return `<div class="v4-ambient-window-fields"><label>生效时间（北京时间）<input name="${esc(prefix)}-effective-at" type="datetime-local" value="${esc(localDateTime(policy?.effective_at))}" required></label><fieldset><legend>允许环境参与的两个时段</legend><div class="v4-ambient-window-grid">${active.map((window, index) => `<div><strong>时段 ${index + 1}</strong><label>开始<input name="${esc(prefix)}-window-${index}-start" type="time" value="${esc(window.start)}" required></label><label>结束<input name="${esc(prefix)}-window-${index}-end" type="time" value="${esc(window.end)}" required></label></div>`).join('')}</div></fieldset></div>`;
    };
    const windowSummary = (policy) => windows(policy).map((window) => `${window.start}–${window.end}`).join('、');
    const groupView = (accessEntry) => {
      const groupId = String(accessEntry.group_id || '');
      const policy = policiesByGroup.get(groupId) || { group_id: groupId };
      const effective = effectiveByGroup[groupId] || { ...defaults, origin: 'default', group_id: groupId };
      const override = effective.origin === 'override';
      const editVisible = override || expandedOverrides.has(groupId);
      const prefix = `group-${groupId}`;
      return `<article class="v4-group-policy"><div><strong>${esc(pick({ ...accessEntry, ...policy }, ['display_name', 'group_name', 'remark', 'group_id']))}</strong><span>${esc(policy.allow_work ? '允许工作协作' : '只进行对话')}</span><small>环境参与时窗：${esc(override ? '此群覆盖默认' : '继承默认')} · ${esc(windowSummary(effective))}</small></div><label>参与方式<select data-v4-group-mode="${esc(groupId)}">${policy.participation_mode ? '' : '<option value="" selected disabled>请选择参与方式（尚未设置）</option>'}<option value="disabled" ${policy.participation_mode === 'disabled' ? 'selected' : ''}>不参与</option><option value="mentions_only" ${policy.participation_mode === 'mentions_only' ? 'selected' : ''}>仅明确提及</option><option value="directed_context" ${policy.participation_mode === 'directed_context' ? 'selected' : ''}>受控续接</option><option value="natural_participation" ${policy.participation_mode === 'natural_participation' ? 'selected' : ''}>自然参与</option></select></label><button type="button" class="v4-secondary" data-v4-group-save="${esc(groupId)}">保存参与方式</button><p data-v4-group-status="${esc(groupId)}" role="status" aria-live="polite"></p><div class="v4-window-override-actions">${override ? '' : `<button type="button" class="v4-secondary" data-v4-window-override="${esc(groupId)}">设置覆盖</button>`}</div><form class="v4-window-override-form" data-v4-window-form="${esc(groupId)}" ${editVisible ? '' : 'hidden'}><h3>${override ? '此群的时间覆盖' : '为此群设置时间覆盖'}</h3><p>覆盖只作用于当前仍在准入名单中的这个群；恢复后立即动态继承默认值。</p>${windowFields(effective, prefix)}<div class="v4-inline-actions"><button type="submit" class="v4-primary">保存群覆盖</button>${override ? `<button type="button" class="v4-secondary" data-v4-window-clear="${esc(groupId)}">恢复默认时窗</button>` : ''}<p data-v4-window-status="${esc(groupId)}" role="status" aria-live="polite"></p></div></form></article>`;
    };
    const render = () => {
      host.innerHTML = `<section class="v4-group-behavior" aria-labelledby="v4GroupBehaviorTitle"><div><h2 id="v4GroupBehaviorTitle">${view.groupId ? '参与设置' : '默认参与设置'}</h2><span>参与方式不扩大准入范围；环境参与只在两个低价时段允许，直接提及、回复义务、追问和已承诺事项不受此时窗限制。</span></div>${view.groupId ? '' : `<form id="v4AmbientDefaultForm" class="v4-ambient-windows"><header><h3>默认环境参与时窗</h3><p>Asia/Shanghai · 生效后，环境参与仅允许 ${esc(windowSummary(defaults))}。群覆盖不会复制这份默认配置。</p></header>${windowFields(defaults, 'default')}<div class="v4-inline-actions"><button type="submit" class="v4-primary">保存默认时窗</button><p id="v4AmbientDefaultStatus" role="status" aria-live="polite"></p></div></form>`}${view.defaultsOnly ? '' : `<div class="v4-group-policy-list">${allowedGroups.length ? allowedGroups.map(groupView).join('') : empty('本群尚未准入。仍在 QQ 群中，不代表项目已获准参与或保留聊天记录。请前往准入设置核对；这里只展示项目实际保留的消息。')}</div>`}</section>`;
      $('#v4AmbientDefaultForm', host)?.addEventListener('submit', async (event) => {
        event.preventDefault();
        const form = event.currentTarget; const submit = $('button[type="submit"]', form); const status = $('#v4AmbientDefaultStatus', form);
        submit.disabled = true; status.textContent = '正在保存默认时窗…';
        try {
          defaults = await api.saveGroupParticipationWindows({ scope: 'default', expected_version: Number(defaults.version || 0), policy: policyFromForm(form, 'default') });
          Object.keys(effectiveByGroup).forEach((groupId) => {
            if (effectiveByGroup[groupId]?.origin !== 'override') effectiveByGroup[groupId] = { ...defaults, group_id: groupId, origin: 'default' };
          });
          render();
          $('#v4AmbientDefaultStatus', host).textContent = '默认时窗已保存。';
        } catch (error) { status.textContent = userError(error); }
        finally { submit.disabled = false; }
      });
      all('[data-v4-group-save]', host).forEach((button) => button.addEventListener('click', async () => {
        const groupId = button.dataset.v4GroupSave; const original = policiesByGroup.get(groupId) || { group_id: groupId }; const mode = $(`[data-v4-group-mode="${CSS.escape(groupId)}"]`, host)?.value;
        if (!mode) { $(`[data-v4-group-status="${CSS.escape(groupId)}"]`, host).textContent = '请先选择本群参与方式。'; return; }
        // An admitted group can be configured before its first channel message creates a policy.
        button.disabled = true;
        try { const canonical = await api.saveGroup({ ...original, participation_mode: mode }); if (!canonical.group || canonical.group.group_id !== groupId || canonical.group.participation_mode !== mode) throw new Error('group_readback_mismatch'); policiesByGroup.set(groupId, canonical.group); button.disabled = false; $(`[data-v4-group-status="${CSS.escape(groupId)}"]`, host).textContent = '参与方式已保存，并已从服务器回读。'; } catch (error) { button.disabled = false; $(`[data-v4-group-status="${CSS.escape(groupId)}"]`, host).textContent = userError(error); }
      }));
      all('[data-v4-window-override]', host).forEach((button) => button.addEventListener('click', () => {
        expandedOverrides.add(button.dataset.v4WindowOverride); const form = $(`[data-v4-window-form="${CSS.escape(button.dataset.v4WindowOverride)}"]`, host); form.hidden = false; $('input', form)?.focus(); button.hidden = true;
      }));
      all('[data-v4-window-form]', host).forEach((form) => form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const groupId = form.dataset.v4WindowForm; const current = effectiveByGroup[groupId] || defaults;
        const submit = $('button[type="submit"]', form); const status = $(`[data-v4-window-status="${CSS.escape(groupId)}"]`, form);
        submit.disabled = true; status.textContent = '正在保存群覆盖…';
        try {
          effectiveByGroup[groupId] = await api.saveGroupParticipationWindows({ scope: 'group', group_id: groupId, expected_version: current.origin === 'override' ? Number(current.version || 0) : 0, policy: policyFromForm(form, `group-${groupId}`) });
          const draftMode = $(`[data-v4-group-mode="${CSS.escape(groupId)}"]`, host)?.value;
          expandedOverrides.delete(groupId); render();
          const modeField = $(`[data-v4-group-mode="${CSS.escape(groupId)}"]`, host); if (modeField && draftMode) modeField.value = draftMode;
          $(`[data-v4-window-status="${CSS.escape(groupId)}"]`, host).textContent = '本群时间窗已保存并回读。';
          modeField?.focus();
        } catch (error) { status.textContent = userError(error); }
        finally { submit.disabled = false; }
      }));
      all('[data-v4-window-clear]', host).forEach((button) => button.addEventListener('click', async () => {
        const groupId = button.dataset.v4WindowClear; const form = $(`[data-v4-window-form="${CSS.escape(groupId)}"]`, host); const current = effectiveByGroup[groupId]; const status = $(`[data-v4-window-status="${CSS.escape(groupId)}"]`, form);
        button.disabled = true; status.textContent = '正在恢复默认时窗…';
        try {
          effectiveByGroup[groupId] = await api.saveGroupParticipationWindows({ scope: 'group', group_id: groupId, operation: 'clear', expected_version: Number(current?.version || 0), policy: null });
          const draftMode = $(`[data-v4-group-mode="${CSS.escape(groupId)}"]`, host)?.value;
          expandedOverrides.delete(groupId); render();
          const modeField = $(`[data-v4-group-mode="${CSS.escape(groupId)}"]`, host); if (modeField && draftMode) modeField.value = draftMode;
          $(`[data-v4-window-status="${CSS.escape(groupId)}"]`, host).textContent = '本群时间窗已保存并回读。';
          modeField?.focus();
        } catch (error) { status.textContent = userError(error); button.disabled = false; }
      }));
    };
    render();
  }


  function renderGroupResearch(host, research, groups, notice = '') {
    const policy = research?.policy || {};
    const runs = asList(research?.runs);
    const selected = String(policy.pilot_group_id || '');
    const groupOptions = [`<option value="">选择一个已授权群</option>`, ...groups.map((group) => {
      const id = String(group.group_id || '');
      const label = recordTitle(group, ['display_name', 'group_name', 'group_id']);
      return `<option value="${esc(id)}" ${id === selected ? 'selected' : ''}>${esc(label)}</option>`;
    })].join('');
    const state = (on) => on ? '已开启' : '未开启';
    const runRows = runs.length ? runs.map((run) => `<article class="v4-research-run"><div><strong>${esc(run.query_redacted || '未形成可保留的公开查询')}</strong><span>${esc(run.risk_tier || 'unknown')} · ${esc(run.stage || 'unknown')} · ${esc(run.reason_code || '')}</span></div><small>${esc(run.source_count || 0)} 个公开来源${run.expires_at ? ` · 到期 ${esc(formatTime(run.expires_at))}` : ''}</small></article>`).join('') : empty('尚无已记录的研究运行。');
    host.innerHTML = `<section class="v4-group-research" aria-labelledby="v4GroupResearchTitle"><header><p>Public research pilot</p><h2 id="v4GroupResearchTitle">公开事实研究与自动知识</h2><span>只对一个已授权试点群生效。聊天原文、成员身份和私密话题不会进入研究或知识库。</span></header><dl class="v4-research-state"><div><dt>试点策略</dt><dd>${esc(state(policy.enabled))}</dd></div><div><dt>外部研究</dt><dd>${esc(state(policy.feature_enabled))}</dd></div><div><dt>低敏感自动学习</dt><dd>${esc(state(policy.autoknowledge_feature_enabled))}</dd></div></dl><form id="v4GroupResearchPolicy" class="v4-research-form"><label>试点群<select name="pilot_group_id" required>${groupOptions}</select></label><label class="v4-check"><input type="checkbox" name="enabled" ${policy.enabled ? 'checked' : ''}>允许该试点群提出公开事实研究</label><label class="v4-check"><input type="checkbox" name="feature_enabled" ${policy.feature_enabled ? 'checked' : ''}>实际执行受限的公开资料研究</label><label class="v4-check"><input type="checkbox" name="auto_publish_low_public" ${policy.auto_publish_low_public ? 'checked' : ''}>允许低敏感、双来源事实形成临时群内知识</label><label class="v4-check"><input type="checkbox" name="autoknowledge_feature_enabled" ${policy.autoknowledge_feature_enabled ? 'checked' : ''}>实际启用低敏感自动知识沉淀</label><div class="v4-research-limits"><label>每日总上限<input name="max_runs_per_day" type="number" min="0" max="20" value="${esc(policy.max_runs_per_day || 3)}"></label><label>单群每日上限<input name="max_runs_per_group_day" type="number" min="0" max="10" value="${esc(policy.max_runs_per_group_day || 2)}"></label><label>知识有效期（小时）<input name="freshness_hours" type="number" min="1" max="720" value="${esc(policy.freshness_hours || 168)}"></label></div><p id="v4GroupResearchHelp">高影响公开议题只生成脱敏 Draft；个人、私密、敏感或无明确公开主体的话题不搜索、不入库。关闭“外部研究”会同时关闭自动知识沉淀。</p><div class="v4-inline-actions"><button class="v4-primary" type="submit">保存研究试点策略</button><p id="v4GroupResearchStatus" role="status" aria-live="polite">${esc(notice)}</p></div></form><section class="v4-research-audit" aria-labelledby="v4GroupResearchAudit"><h3 id="v4GroupResearchAudit">最近研究审计</h3><p>仅显示脱敏公开查询、风险级别、来源数量与有效期，不显示群聊原文。</p>${runRows}</section></section>`;
    const form = $('#v4GroupResearchPolicy', host);
    // The dynamic scope is resolved from QQ's live allowlist at each research
    // authorization.  It is deliberately not a copied list or a wildcard.
    const scopeMode = String(policy.scope_mode || 'single_group');
    const scopeLabel = document.createElement('label');
    scopeLabel.innerHTML = `试点范围<select name="scope_mode"><option value="admitted_groups" ${scopeMode === 'admitted_groups' ? 'selected' : ''}>所有当前准入群（动态）</option><option value="single_group" ${scopeMode === 'single_group' ? 'selected' : ''}>单独指定群（兼容/收窄）</option></select>`;
    form?.prepend(scopeLabel);
    const pilotControl = $('select[name="pilot_group_id"]', form);
    const pilotField = pilotControl?.closest('label');
    if (pilotField) pilotField.dataset.v4ResearchSingleScope = 'true';
    const headerSummary = $('header > span', host);
    const scopeHelp = document.createElement('p');
    scopeHelp.className = 'v4-research-scope-help';
    form?.insertBefore(scopeHelp, form.querySelector('.v4-check'));
    const syncResearchScope = () => {
      const dynamic = $('select[name="scope_mode"]', form)?.value === 'admitted_groups';
      if (pilotField) pilotField.hidden = dynamic;
      if (pilotControl) pilotControl.required = !dynamic;
      scopeHelp.textContent = dynamic
        ? '范围会在每次执行前重新核对 QQ 当前准入名单；撤销准入后会立刻失效，不保存群名单副本。'
        : '仅对这里选定且仍处于 QQ 准入名单内的一个群生效。';
      if (headerSummary) headerSummary.textContent = dynamic
        ? '只对 QQ 当前准入的群动态生效。聊天原文、成员身份和私密话题不会进入研究或知识库。'
        : '只对一个仍处于 QQ 准入名单内的试点群生效。聊天原文、成员身份和私密话题不会进入研究或知识库。';
    };
    $('select[name="scope_mode"]', form)?.addEventListener('change', syncResearchScope);
    syncResearchScope();
    form?.addEventListener('submit', async (event) => {
      event.preventDefault();
      const data = new FormData(form);
      const policyEnabled = data.get('enabled') === 'on';
      const researchEnabled = policyEnabled && data.get('feature_enabled') === 'on';
      const autoPolicy = policyEnabled && data.get('auto_publish_low_public') === 'on';
      const autoEnabled = researchEnabled && autoPolicy && data.get('autoknowledge_feature_enabled') === 'on';
      const status = $('#v4GroupResearchStatus', host);
      const submit = $('button[type="submit"]', form);
      submit.disabled = true; status.textContent = '正在保存试点策略…';
      try {
        const result = await api.saveGroupResearch({
          scope_mode: data.get('scope_mode'),
          pilot_group_id: data.get('scope_mode') === 'single_group' ? data.get('pilot_group_id') : '',
          enabled: policyEnabled,
          feature_enabled: researchEnabled, auto_publish_low_public: autoPolicy,
          autoknowledge_feature_enabled: autoEnabled,
          max_runs_per_day: data.get('max_runs_per_day'),
          max_runs_per_group_day: data.get('max_runs_per_group_day'),
          freshness_hours: data.get('freshness_hours'), version: policy.version,
        });
        renderGroupResearch(host, { ...research, policy: unpack(result, 'policy') }, groups, '试点策略已保存。');
      } catch (error) { status.textContent = userError(error); }
      finally { submit.disabled = false; }
    });
  }

  function ownerSurfaceLabel(owner) {
    return ({ work: '查看委托', chat: '继续与当前助手对话', qq: '查看 QQ 事实', artifacts: '查看成果' }[owner] || '查看详情');
  }

  function openOwnerBriefRecord(item) {
    if (!item) return;
    const owner = item.owner || (item.source_type === 'approval' ? 'work' : 'now');
    if (item.allowed_action === 'review_approval') { openApprovalReview(item.source_id); return; }
    state.pendingSelection = { route: owner, sourceType: item.source_type, sourceId: item.source_id };
    navigate(owner);
  }

  async function readBoundPortrait(root, assistantName) {
    const slot = $('.v4-companion-art', root);
    if (!slot) return;
    const home = $('.v4-companion-home', root);
    const status = document.createElement('div');
    status.className = 'v4-portrait-status';
    status.dataset.v4PortraitStatus = '';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    home.append(status);
    let requestEpoch = 0;
    const current = epoch => pageCurrent(root) && epoch === requestEpoch;
    const show = (state, message = '', retry = false) => {
      slot.dataset.v4PortraitState = state;
      status.replaceChildren();
      if (!message) return;
      const label = document.createElement('span');
      label.textContent = message;
      status.append(label);
      if (retry) {
        const button = document.createElement('button');
        button.type = 'button';
        button.dataset.v4PortraitRetry = '';
        button.textContent = '重试立绘';
        button.addEventListener('click', () => { void read(); });
        status.append(button);
      }
    };
    const fail = (epoch, message) => {
      if (!current(epoch)) return;
      slot.replaceChildren();
      show('unavailable', message, true);
    };
    const read = async () => {
      const epoch = ++requestEpoch;
      slot.replaceChildren();
      show('loading');
      try {
        const pet = unpack(await api.pets(), 'pet');
        if (!current(epoch)) return;
        const boundPack = asList(pet.packs).find(pack => pack.id === pet.pack_id);
        if (!boundPack && pet.pack_id) { fail(epoch, '当前外观信息不完整，暂时无法显示。'); return; }
        const portrait = boundPack?.portrait;
        if (!portrait) { show('absent', '当前外观未配置立绘。'); return; }
        const positiveInteger = value => Number.isSafeInteger(value) && value > 0;
        if (!boundPack.id || portrait.pack_id !== boundPack.id
          || !['image/webp', 'image/png'].includes(portrait.mime_type)
          || !positiveInteger(portrait.width) || !positiveInteger(portrait.height)
          || !positiveInteger(portrait.bytes) || portrait.bytes > 200 * 1024
          || !/^[a-f0-9]{64}$/.test(portrait.content_version || '')
          || typeof portrait.url !== 'string' || !portrait.url.startsWith('/')
          || portrait.url.startsWith('//') || /[\\\s]/.test(portrait.url)) { fail(epoch, '立绘信息不完整，暂时无法显示。'); return; }
        const url = new URL(portrait.url, location.origin);
        if (url.origin !== location.origin || url.username || url.password || url.hash) { fail(epoch, '立绘信息不完整，暂时无法显示。'); return; }
        // The server is authoritative for pack visibility, MIME, content and byte budget.
        // Consume its authenticated URL; never construct a path or fall back to another pack.
        const img = document.createElement('img');
        img.alt = `${assistantName}的当前外观`;
        img.width = portrait.width; img.height = portrait.height;
        img.decoding = 'async'; img.fetchPriority = 'low';
        img.onload = async () => {
          try { await img.decode(); }
          catch (_) { fail(epoch, '立绘图片加载失败，请重试。'); return; }
          if (current(epoch)) show('ready');
        };
        img.onerror = () => fail(epoch, '立绘图片加载失败，请重试。');
        slot.replaceChildren(img);
        img.src = portrait.url;
      } catch (error) {
        const message = [401, 403].includes(error?.status) ? '当前账号无权读取立绘。'
          : error?.name === 'RequestTimeoutError' ? '立绘读取超时，请手动重试。'
          : [502, 503, 504].includes(error?.status) ? '立绘暂时无法读取，服务暂时不可用。'
          : '立绘暂时无法读取，请重试。';
        fail(epoch, message);
      }
    };
    await read();
  }

  async function renderNow(root) {
    let brief;
    try { brief = unpack(await api.ownerBrief(), 'result'); }
    catch (error) { renderReadFailure(root, 'now', '此刻暂时不可用', error); return; }
    if (!pageCurrent(root)) return;
    setAttentionHome(ownerBriefAsAttention(brief));
    const section = (id, label, title, items, itemRenderer, emptyMessage) => !items.length ? '' : `<section class="v4-now-section" aria-labelledby="${id}"><div class="v4-section-title"><div><h2 id="${id}">${esc(title)}</h2></div><span class="v4-count">${items.length}</span></div>${items.length ? items.map(itemRenderer).join('') : empty(emptyMessage)}</section>`;
    const ownerItem = (item, type) => `<article class="v4-now-item"><div><strong>${esc(item.title || item.source_id || '未命名对象')}</strong><p>${esc(item.state || item.reason_code || item.allowed_action || '')}</p><small>${esc(formatTime(item.updated_at))}</small></div><button type="button" class="v4-secondary" data-v4-brief-item="${esc(type)}:${esc(item.source_id)}">${esc(ownerSurfaceLabel(item.owner))}</button></article>`;
    const continuation = asList(brief.continuation);
    const decisions = asList(brief.decisions);
    const inProgress = asList(brief.in_progress);
    const outcomes = asList(brief.recent_outcomes);
    root.innerHTML = page('', '此刻', '对话、记忆与日常，都从这里继续。', `
      <section class="v4-now-presence v4-companion-home" aria-labelledby="v4CompanionName"><div class="v4-companion-copy"><p class="v4-context-label">我的助手</p><h2 id="v4CompanionName">${esc(brief.assistant?.display_name || '我的助手')}</h2><p class="v4-companion-intro">聊聊今天，或继续你们的话题。</p><div class="v4-companion-actions">${continuation.length ? `<button type="button" class="v4-primary" data-v4-brief-item="continuation:${esc(continuation[0].source_id)}">继续对话 <span aria-hidden="true">→</span></button>` : '<button type="button" class="v4-primary" data-v4-route="chat">开始对话 <span aria-hidden="true">→</span></button>'}<button type="button" class="v4-secondary" data-v4-route="assistant">查看档案</button></div><p class="v4-companion-context">${continuation.length ? '从已有的 Web 对话继续。' : '还没有可继续的 Web 对话。'}</p></div><div class="v4-companion-visual"><div class="v4-companion-art" data-v4-portrait-preview></div><span class="v4-companion-monogram" aria-hidden="true">${esc(Array.from(brief.assistant?.display_name || '助')[0])}</span></div></section>
      <nav class="v4-home-shortcuts" aria-label="常用管理"><button type="button" data-v4-route="qq"><span class="v4-shortcut-icon" aria-hidden="true">${navigationIcon('qq')}</span><span><strong>QQ 会话</strong><small>群聊、私聊与各自的设置</small></span><span class="v4-shortcut-arrow" aria-hidden="true">→</span></button><button type="button" data-v4-route="memory"><span class="v4-shortcut-icon" aria-hidden="true">${navigationIcon('memory')}</span><span><strong>长期记忆</strong><small>查看已记住的事与来源</small></span><span class="v4-shortcut-arrow" aria-hidden="true">→</span></button></nav>
      ${!decisions.length && !inProgress.length && !outcomes.length ? '<p class="v4-now-idle">此刻没有待批准事项、进行中的委托或需关注的近期结果；历史投递恢复记录另行统计。</p>' : ''}
      <div class="v4-now-grid">
        ${section('v4NowDecisions', 'Need your decision', '需要你决定', decisions, (item) => ownerItem(item, 'decision'), '当前没有需要你批准或拒绝的对象。')}
        ${section('v4NowProgress', 'In progress', '正在推进', inProgress, (item) => ownerItem(item, 'progress'), '当前没有正在推进的委托。')}
        ${section('v4NowOutcomes', 'Recent outcomes', '近期结果', outcomes, (item) => ownerItem(item, 'outcome'), '没有需要特别关注的近期结果。')}
      </div><section class="v4-now-section" aria-labelledby="v4NowReliabilityTitle"><div class="v4-section-title"><h2 id="v4NowReliabilityTitle">历史投递恢复记录</h2></div><p id="v4NowReliability" role="status">正在核验已停止自动重试的历史记录；它不属于当前待发送队列。</p><div class="v4-inline-actions"><button type="button" class="v4-secondary" id="v4NowReliabilityOpen">查看高级可靠性记录</button><button type="button" class="v4-text-button" id="v4NowReliabilityRetry">重新核验</button></div></section>`);
    const name = brief.assistant?.display_name || '我的助手';
    $('#v4AssistantDisplay').textContent = name;
    $('#v4AssistantMonogram').textContent = Array.from(name)[0];
    const byKey = new Map([
      ...continuation.map((item) => [`continuation:${item.source_id}`, item]),
      ...decisions.map((item) => [`decision:${item.source_id}`, item]),
      ...inProgress.map((item) => [`progress:${item.source_id}`, item]),
      ...outcomes.map((item) => [`outcome:${item.source_id}`, item]),
    ]);
    all('[data-v4-brief-item]', root).forEach((button) => button.addEventListener('click', () => openOwnerBriefRecord(byKey.get(button.dataset.v4BriefItem))));
    $('#v4NowReliabilityOpen', root).addEventListener('click', () => {
      state.pendingSelection = { route: 'console', sourceType: 'reliability', sourceId: '' };
      navigate('console');
    });
    const reliabilityStatus = $('#v4NowReliability', root);
    const reliabilityRetry = $('#v4NowReliabilityRetry', root);
    const readReliability = async () => {
      reliabilityRetry.disabled = true;
      reliabilityStatus.textContent = '正在核验已停止自动重试的历史记录…';
      try {
        const result = await api.reliability();
        if (!pageCurrent(root)) return;
        const count = asList(result.dead_letters).length;
        reliabilityStatus.textContent = `本次读取到 ${count} 条已停止自动重试的历史投递记录（列表最多 100 条）；它们不在当前待发送队列中，重新投递前需要逐项核对。`;
      } catch (_) {
        if (!pageCurrent(root)) return;
        reliabilityStatus.textContent = '高级可靠性记录暂不可核验；此刻的审批与委托列表不包含这类历史投递，不能据此认定其为零。';
      } finally {
        if (pageCurrent(root)) reliabilityRetry.disabled = false;
      }
    };
    reliabilityRetry.addEventListener('click', () => { void readReliability(); });
    void readReliability();
    void readBoundPortrait(root, name);
  }

  async function renderB2Qq(root) {
    let active = 'management';
    let selectedRef = '';
    let timeline = [], timelineNextCursor = '', summary = null, inspector = null;
    let groupRecordError = '', summaryError = '';
    let onSummaryRead = () => {};
    let timelineReadBlocked = false;
    let timelineRequestEpoch = 0, panelRenderEpoch = 0;
    let groupQuery = '', privateQuery = '', roster = state.qqRosterCache?.data || { groups: [], events: [] }, privateRoster = [], selectedPrivate = null, listScroll = 0;
    let rosterPhase = state.qqRosterCache ? 'stale' : 'loading';
    let stopPanel = () => {};
    state.qqDispose?.();
    let disposed = false;
    state.qqDispose = () => { disposed = true; stopPanel(); };
    const tabs = [['management', '群管理'], ['private', '私聊管理'], ['defaults', '默认设置'], ['admission', '准入'], ['deliveries', '投递'], ['research', '研究与知识']];
    const membership = value => ({ joined: '仍在群', absent: '已离群', unknown: '待核验' }[value] || '待核验');
    const speech = value => ({ allowed: '可发言', muted: '被禁言', unknown: '发言能力待核验' }[value] || '发言能力待核验');
    const role = value => ({ member: '普通成员', admin: '管理员', owner: '群主' }[value] || '角色待核验');
    const date = value => Number(value) > 0 ? formatTime(new Date(Number(value) * 1000).toISOString()) : '尚未核验';
    const groupId = () => selectedRef.slice('group:'.length);
    const selectedGroup = () => asList(roster.groups).find(g => String(g.group_id) === groupId()) || { group_id: groupId() };
    const key = item => String(item.message_ref || item.event_ref);
    const unavailable = error => [401, 403].includes(error?.status) || error?.payload?.error === 'qq_conversation_not_available';
    const renderScopedMemories = async (target, channel, subjectId, valid) => {
      if (!target) return;
      const load = async () => {
        target.innerHTML = '<h3>她记得什么</h3><p role="status">正在读取本会话记忆…</p>';
        try {
          const response = await api.qqMemories(channel, subjectId);
          if (!valid()) return;
          const memories = asList(response.memories);
          target.innerHTML = `<h3>她记得什么</h3><p class="v4-qq-retention-note">仅此${channel === 'group' ? '群' : '私聊'}可见；聊天提取是有来源的说法，不等于外部事实。含糊内容不会进入待审清单。</p><div>${memories.length ? memories.map(item => `<article class="v4-object-inspector" data-v4-memory-id="${esc(item.id)}"><p><strong>${esc(item.content)}</strong></p><p>${esc(item.provenance)} · ${esc(item.status === 'active' ? '生效中' : '已暂停')} · ${esc(item.kind)}</p><details><summary>依据是什么</summary>${item.evidence?.length ? `<ul>${item.evidence.map(e => `<li>${esc(e.actor_ref)} · ${esc(e.source_key)} · ${esc(e.snippet)}</li>`).join('')}</ul>` : '<p>暂无可核对的聊天证据；请看上方来源标记。</p>'}</details><div><button type="button" data-v4-memory-correct="${esc(item.id)}">纠正</button><button type="button" data-v4-memory-delete="${esc(item.id)}">删除</button></div><form data-v4-memory-edit="${esc(item.id)}" hidden><label>正确内容<input name="content" maxlength="180" required value="${esc(item.content)}"></label><button type="submit">保存纠正</button></form><p role="status" data-v4-memory-status="${esc(item.id)}"></p></article>`).join('') : '<p>本会话暂无长期记忆。</p>'}</div><form id="v4ScopedMemoryAdd"><label>主动添加本会话记忆<input name="content" maxlength="180" required></label><button type="submit">添加</button></form><p id="v4ScopedMemoryStatus" role="status"></p>`;
          const byId = new Map(memories.map(item => [String(item.id), item]));
          all('[data-v4-memory-correct]', target).forEach(button => button.addEventListener('click', () => {
            const form = $(`[data-v4-memory-edit="${CSS.escape(button.dataset.v4MemoryCorrect)}"]`, target);
            if (form) { form.hidden = !form.hidden; if (!form.hidden) $('input', form)?.focus(); }
          }));
          all('[data-v4-memory-edit]', target).forEach(form => form.addEventListener('submit', async event => {
            event.preventDefault();
            const old = byId.get(form.dataset.v4MemoryEdit), notice = $(`[data-v4-memory-status="${CSS.escape(form.dataset.v4MemoryEdit)}"]`, target);
            if (!old) return;
            notice.textContent = '正在纠正…';
            try { await api.mutateQqMemory({ channel, subject_id: subjectId, action: 'correct', id: old.id, expected_updated_at: old.updated_at, content: $('input', form).value.trim() }); if (valid()) await load(); }
            catch (error) { if (valid()) notice.textContent = userError(error); }
          }));
          all('[data-v4-memory-delete]', target).forEach(button => button.addEventListener('click', async () => {
            const old = byId.get(button.dataset.v4MemoryDelete);
            if (!old || !window.confirm('删除这条记忆及其保留的依据片段？')) return;
            const notice = $(`[data-v4-memory-status="${CSS.escape(old.id)}"]`, target);
            notice.textContent = '正在删除…';
            try { await api.mutateQqMemory({ channel, subject_id: subjectId, action: 'delete', id: old.id, expected_updated_at: old.updated_at }); if (valid()) await load(); }
            catch (error) { if (valid()) notice.textContent = userError(error); }
          }));
          $('#v4ScopedMemoryAdd', target).addEventListener('submit', async event => {
            event.preventDefault(); const notice = $('#v4ScopedMemoryStatus', target);
            notice.textContent = '正在添加…';
            try { await api.mutateQqMemory({ channel, subject_id: subjectId, action: 'add', content: $('input', event.currentTarget).value.trim() }); if (valid()) await load(); }
            catch (error) { if (valid()) notice.textContent = userError(error); }
          });
        } catch (error) {
          if (valid()) target.innerHTML = `<h3>她记得什么</h3><p role="status">${esc(userError(error, '记忆读取失败，请重试。'))}</p><button type="button">重试</button>`;
          $('button', target)?.addEventListener('click', load);
        }
      };
      await load();
    };
    const loadTimeline = async ({ append = false, merge = false, preserveInspector = false } = {}) => {
      const requestEpoch = ++timelineRequestEpoch;
      const requestedRef = selectedRef;
      groupRecordError = ''; timelineReadBlocked = false; if (!append) summaryError = ''; if (!preserveInspector) inspector = null;
      if (!requestedRef) { timeline = []; timelineNextCursor = ''; summary = null; return true; }
      try {
        const timelineRequest = api.qqTimeline(requestedRef, 40, append ? timelineNextCursor : '');
        if (!append && requestedRef.startsWith('group:')) {
          // Optional summary never delays the message pane or the live poller.
          api.qqConversationSummary(requestedRef).then(value => {
            if (requestEpoch !== timelineRequestEpoch || requestedRef !== selectedRef || groupRecordError) return;
            summary = unpack(value, 'result'); onSummaryRead();
          }, () => {
            if (requestEpoch !== timelineRequestEpoch || requestedRef !== selectedRef || groupRecordError) return;
            summary = null; summaryError = '摘要暂时未就绪，消息记录仍可查看。'; onSummaryRead();
          });
        }
        const timelineResponse = await timelineRequest;
        if (requestEpoch !== timelineRequestEpoch || requestedRef !== selectedRef) return false;
        const result = unpack(timelineResponse, 'result');
        const items = asList(result.items);
        if (append) timeline = [...timeline, ...items.filter(item => !timeline.some(old => key(old) === key(item)))];
        else if (merge) {
          const incoming = new Set(items.map(key));
          timeline = [...items, ...timeline.filter(item => !incoming.has(key(item)))];
        } else timeline = items;
        if (!merge) timelineNextCursor = result.next_cursor || '';
        return true;
      } catch (error) {
        if (requestEpoch !== timelineRequestEpoch || requestedRef !== selectedRef) return false;
        if ((!append && !merge) || unavailable(error)) { timeline = []; timelineNextCursor = ''; summary = null; }
        timelineReadBlocked = unavailable(error);
        const privateRead = requestedRef.startsWith('private:');
        groupRecordError = unavailable(error)
          ? (privateRead ? '此私聊已不在当前准入范围，聊天记录已停止展示。' : '本群尚未开放聊天记录，请检查准入与群策略。')
          : (privateRead ? '暂时无法读取私聊记录，请稍后重试。' : '暂时无法读取群聊记录，请稍后重试。');
        return true;
      }
    };
    const messageMarkup = item => `<strong>${esc(item.actor_kind === 'assistant' ? '助手' : item.actor_label)}</strong><p>${esc(item.content)}</p><small>${esc(formatTime(item.created_at))}${item.is_mention ? ' · 提及助手' : ''}</small>`;
    const renderPanel = async () => {
      stopPanel();
      const epoch = ++panelRenderEpoch;
      all('[data-v4-qq-tab]', root).forEach(button => button.setAttribute('aria-current', button.dataset.v4QqTab === active ? 'page' : 'false'));
      const host = $('#v4QqPanel', root); if (!host) return;
      const current = () => !disposed && epoch === panelRenderEpoch && state.route === 'qq';
      let timer = null;
      stopPanel = () => { if (timer !== null) window.clearTimeout(timer); timelineRequestEpoch++; };
      const repeat = (fn, delay) => { if (current()) timer = window.setTimeout(fn, delay); };
      if (active === 'management' && !selectedRef) {
        host.innerHTML = `<section class="v4-console-section"><h2>群列表</h2><p>选择一个群，在同一页查看聊天与本群设置。</p><label class="v4-qq-search">搜索群名或群号<input id="v4GroupSearch" type="search" value="${esc(groupQuery)}"></label><button type="button" class="v4-secondary" id="v4GroupRefresh">刷新状态记录</button><p id="v4GroupReadStatus" role="status"></p><div id="v4GroupStateBody"></div></section>`;
        const body = $('#v4GroupStateBody', host), notice = $('#v4GroupReadStatus', host);
        const renderList = () => {
          const groups = asList(roster.groups);
          const visible = groups.filter(g => `${g.group_name || ''} ${g.group_id}`.toLowerCase().includes(groupQuery.toLowerCase()));
          const cards = visible.map(g => `<button type="button" class="v4-group-open" data-v4-group-open="${esc(g.group_id)}"><strong>${esc(g.group_name || g.group_id)}</strong><span>${esc(g.group_id)} · ${esc(membership(g.membership))} · ${esc(speech(g.speaking))}</span><small>${esc(role(g.role))} · ${g.freshness === 'fresh' ? '近期已核验' : '记录已过期'} · ${esc(date(g.observed_at))}</small><span>查看聊天与设置 →</span></button>`).join('');
          const summary = rosterPhase === 'loading' ? '<p role="status">正在核验群名册；暂不显示群数。</p>'
            : rosterPhase === 'error' ? '<p role="status">群名册读取失败；尚无可用记录。</p>'
            : '<p>' + (rosterPhase === 'stale' ? '上次核验（' + esc(date(roster.last_snapshot)) + '，正在重试）：' : '最近完整名册（' + esc(date(roster.last_snapshot)) + '）：')
              + '仍在群 ' + groups.filter(g => g.membership === 'joined').length
              + ' · 已离群 ' + groups.filter(g => g.membership === 'absent').length
              + ' · 待核验 ' + groups.filter(g => g.membership === 'unknown').length + '</p>';
          const markup = summary + '<div class="v4-group-list">' + (cards || empty(groupQuery ? '没有匹配的群。' : '尚无已核验群名册；准入名单不代表实际在群。')) + '</div>';
          if (body.innerHTML === markup) return;
          const focused = document.activeElement?.dataset?.v4GroupOpen;
          body.innerHTML = markup;
          all('[data-v4-group-open]', body).forEach(button => button.addEventListener('click', () => {
            listScroll = window.scrollY; selectedRef = `group:${button.dataset.v4GroupOpen}`; renderPanel();
          }));
          if (focused) $(`[data-v4-group-open="${CSS.escape(focused)}"]`, body)?.focus({ preventScroll: true });
        };
        let pending = false;
        const refresh = async () => {
          if (!current() || pending) return; pending = true;
          try {
            const result = unpack(await api.qqGroupStates(), 'result');
            if (!current()) return;
            if (!result || !Array.isArray(result.groups)) throw new Error('invalid group roster');
            if (result.sync_error || !(Number(result.last_snapshot) > 0)) {
              rosterPhase = state.qqRosterCache ? 'stale' : 'error';
              if (state.qqRosterCache) roster = state.qqRosterCache.data;
              notice.textContent = result.sync_error ? '渠道核验失败，保留上次记录；不推定退群。' : '尚无完整名册快照；不推定群数。';
            } else {
              roster = result;
              rosterPhase = 'confirmed';
              state.qqRosterCache = { data: result };
              notice.textContent = '最近完整名册：' + date(result.last_snapshot);
            }
            renderList();
          } catch (_) {
            if (current()) {
              rosterPhase = state.qqRosterCache ? 'stale' : 'error';
              renderList();
              notice.textContent = state.qqRosterCache ? '状态读取失败；保留上次核验的记录，请重试。' : '状态读取失败；尚无可用记录，请重试。';
            }
          }
          finally { pending = false; }
        };
        $('#v4GroupSearch', host).addEventListener('input', event => { groupQuery = event.target.value; renderList(); });
        $('#v4GroupRefresh', host).addEventListener('click', refresh);
        renderList(); await refresh();
        const poll = async () => { if (!current()) return; if (!document.hidden) await refresh(); repeat(poll, 5000); };
        repeat(poll, 5000);
        if (current()) window.scrollTo(0, listScroll);
      } else if (active === 'management') {
        const ref = selectedRef, id = groupId(), group = selectedGroup();
        const valid = () => current() && selectedRef === ref;
        timeline = []; timelineNextCursor = ''; inspector = null;
        host.innerHTML = `<header class="v4-group-detail-head"><button type="button" id="v4GroupBack" class="v4-secondary">← 返回群列表</button><h2 tabindex="-1" id="v4GroupTitle">${esc(group.group_name || id)}</h2><span class="v4-object-scope">群聊 · ${esc(id)}</span></header><div class="v4-group-detail"><section aria-labelledby="v4QqChatTitle"><h3 id="v4QqChatTitle">聊天记录</h3><p id="v4QqLiveStatus" role="status">正在连接消息记录…</p><p class="v4-qq-retention-note">仅展示已授权、仍在保留期内的消息。自动更新只读取记录，不发送 QQ 消息。</p><details><summary>参与摘要与投递说明</summary><div id="v4QqSummary"></div></details><button type="button" id="v4QqMoreTimeline" class="v4-secondary" hidden>加载更早的记录</button><div id="v4QqScroll" class="v4-live-messages" tabindex="0" role="region" aria-label="本群聊天记录"><div id="v4QqMessages"></div></div><button type="button" id="v4QqNewMessages" class="v4-secondary" hidden>有新消息，查看最新</button><p id="v4QqRecordError" role="status"></p><aside id="v4QqInspector" class="v4-object-inspector"></aside></section><section aria-labelledby="v4GroupSettingsTitle"><h3 id="v4GroupSettingsTitle">本群设置</h3><div id="v4GroupDetailState"></div><div id="v4GroupSettings">正在读取本群设置…</div><button type="button" id="v4GroupAdmission" class="v4-secondary">前往准入设置</button><section id="v4GroupMemories" aria-label="本群长期记忆"></section></section></div>`;
        renderScopedMemories($('#v4GroupMemories', host), 'group', id, valid);
        $('#v4GroupTitle', host).focus();
        $('#v4GroupAdmission', host).addEventListener('click', () => { active = 'admission'; renderPanel(); });
        $('#v4GroupBack', host).addEventListener('click', async () => { selectedRef = ''; await renderPanel(); $(`[data-v4-group-open="${CSS.escape(id)}"]`, host)?.focus({ preventScroll: true }); });
        const scroller = $('#v4QqScroll', host), messages = $('#v4QqMessages', host), status = $('#v4QqLiveStatus', host), newer = $('#v4QqNewMessages', host), more = $('#v4QqMoreTimeline', host);
        const follow = () => scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop < 48;
        const paint = ({ older = false, initial = false, hasNew = false } = {}) => {
          const anchored = follow(), top = scroller.scrollTop, height = scroller.scrollHeight;
          scroller.hidden = timelineReadBlocked;
          const ordered = [...timeline].reverse();
          const existing = new Map(all('[data-v4-qq-message]', messages).map(el => [el.dataset.v4QqMessage, el]));
          const keep = new Set(ordered.map(key));
          existing.forEach((el, k) => { if (!keep.has(k)) el.remove(); });
          let previous = null, added = 0;
          ordered.forEach(item => {
            let el = existing.get(key(item));
            if (!el) { el = document.createElement('button'); el.type = 'button'; el.className = `v4-trace-message v4-group-message ${item.actor_kind === 'assistant' ? 'assistant' : 'member'}`; el.dataset.v4QqMessage = key(item); added++; }
            el.dataset.v4QqEvent = item.event_ref;
            const html = messageMarkup(item); if (el.innerHTML !== html) el.innerHTML = html;
            const next = previous ? previous.nextSibling : messages.firstChild;
            if (next !== el) messages.insertBefore(el, next);
            previous = el;
          });
          if (older) scroller.scrollTop = top + scroller.scrollHeight - height;
          else if (initial || anchored) { scroller.scrollTop = scroller.scrollHeight; newer.hidden = true; }
          else if (added) newer.hidden = false;
          more.hidden = !timelineNextCursor;
          $('#v4QqRecordError', host).textContent = groupRecordError || (!timeline.length ? '暂无可展示的保留消息。' : '');
        };
        newer.addEventListener('click', () => { scroller.scrollTop = scroller.scrollHeight; newer.hidden = true; });
        scroller.addEventListener('scroll', () => { if (follow()) newer.hidden = true; });
        let inspectorEpoch = 0;
        messages.addEventListener('click', async event => {
          const button = event.target.closest('[data-v4-qq-message]'); if (!button) return;
          const n = ++inspectorEpoch, box = $('#v4QqInspector', host); box.textContent = '正在读取参与与投递事实…';
          try {
            const value = unpack(await api.qqEventInspector(button.dataset.v4QqEvent), 'result'); if (!valid() || n !== inspectorEpoch) return;
            box.textContent = `参与：${value.decision?.action || '未形成回复'} · 原因：${value.decision?.reason_code || value.quality?.reason_code || '未记录'} · 投递：${value.delivery?.status === 'application_ack' ? '应用层已确认；QQ 客户端投影未验证' : value.delivery?.status === 'client_projected' ? '已确认客户端投影' : '没有客户端送达证明'}`;
          } catch (_) { if (valid() && n === inspectorEpoch) box.textContent = '参与与投递事实读取失败，请重试。'; }
        });
        const paintState = () => {
          const g = selectedGroup();
          const events = asList(roster.events).filter(e => String(e.group_id) === id && e.kind !== 'snapshot');
          const html = `<p>${esc(membership(g.membership))} · ${esc(speech(g.speaking))} · ${esc(role(g.role))}</p><p>${g.freshness === 'fresh' ? '近期已核验' : '状态待核验'} · ${esc(date(g.observed_at))}</p><details><summary>本群状态事件（${events.length}）</summary>${events.length ? `<ul>${events.map(e => `<li>${esc(date(e.occurred_at))} · ${esc(({group_ban:'禁言变化',group_decrease:'离群',group_increase:'入群',group_admin:'角色变化'})[e.kind] || '状态变化')} · ${esc(({lift_ban:'解除',ban:'禁言',kick_me:'被移出群',leave:'退出群',set:'任命',unset:'取消'})[e.sub_type] || '详情待核验')}</li>`).join('')}</ul>` : '<p>尚无本群事件记录。</p>'}</details>`;
          const target = $('#v4GroupDetailState', host); if (target.innerHTML !== html) target.innerHTML = html;
        };
        paintState();
        // Existing authoritative settings are read once per group, not per message tick.
        const settingsTask = (async () => {
          const target = $('#v4GroupSettings', host);
          try {
            const [policies, access, windows] = await Promise.all([api.groups(), api.qqSettings(), api.groupParticipationWindows()]);
            if (!valid()) return;
            renderQqBehavior(target, records(policies, 'groups'), safe(access), safe(windows), { groupId: id });
          } catch (error) { if (valid()) target.innerHTML = `<p class="v4-error" role="status">${esc([401,403].includes(error?.status) ? '当前账号没有读取本群设置的权限。' : '本群设置读取失败，未生成可保存的默认配置。')} 返回群列表后可重试，聊天区域独立读取。</p>`; }
        })();
        let busy = true, catchup = null, denied = false, lastStateRead = Date.now();
        more.addEventListener('click', async () => {
          if (busy || !timelineNextCursor) return; busy = true; more.disabled = true;
          try { await loadTimeline({ append: true, preserveInspector: true }); if (valid()) { denied = timelineReadBlocked; paint({ older: true }); } }
          finally { busy = false; if (valid()) more.disabled = false; }
        });
        // Start messages and the independent optional summary together; settings do not gate them.
        onSummaryRead = () => { if (valid()) $('#v4QqSummary', host).textContent = summaryError || (summary ? `最近 7 天：成员消息 ${summary.messages?.member || 0} · 已回复 ${summary.quality?.replied || 0} · 应用层确认 ${summary.delivery?.application_ack || 0}（不是 QQ 客户端投影证明）` : '摘要读取中…'); };
        const initial = loadTimeline();
        initial.then(() => {
          if (!valid()) return; denied = timelineReadBlocked; paint({ initial: true });
          onSummaryRead();
          status.textContent = groupRecordError || '自动更新中 · 每 2 秒检查新消息'; busy = false;
        });
        const poll = async () => {
          if (!valid()) return;
          if (document.hidden || busy) { repeat(poll, 2000); return; }
          busy = true;
          try {
            if (!denied) {
            if (!catchup) catchup = { cursor: '', items: [], known: new Set(timeline.map(key)), seenCursors: new Set() };
            let complete = false;
            // Walk the existing older-page cursor until overlap. Never skip a burst >40.
            // Limit each tick to four reads; a long reconnect continues from its cursor.
            for (let pageNo = 0; pageNo < 4 && !complete; pageNo++) {
              const result = unpack(await api.qqTimeline(ref, 40, catchup.cursor), 'result'); if (!valid()) return;
              const items = asList(result.items);
              const overlap = items.some(item => catchup.known.has(key(item)));
              catchup.items.push(...items);
              complete = overlap || !result.next_cursor || !catchup.known.size;
              if (!complete) {
                if (catchup.seenCursors.has(result.next_cursor)) throw new Error('repeated_cursor');
                catchup.seenCursors.add(result.next_cursor); catchup.cursor = result.next_cursor;
              } else if (!catchup.known.size) timelineNextCursor = result.next_cursor || '';
            }
            if (complete) {
              const byKey = new Map(catchup.items.map(item => [key(item), item]));
              const fresh = [...byKey.values()].filter(item => !catchup.known.has(key(item)));
              timeline = [...fresh, ...timeline.map(item => byKey.get(key(item)) || item)];
              catchup = null; groupRecordError = ''; timelineReadBlocked = false; paint();
              status.textContent = '自动更新中 · 每 2 秒检查新消息';
            } else status.textContent = '正在补齐断线期间的消息…';
            }
          } catch (error) {
            if (!valid()) return; catchup = null;
            denied = unavailable(error);
            if (denied) { timelineReadBlocked = true; timeline = []; timelineNextCursor = ''; inspectorEpoch++; $('#v4QqInspector', host).textContent = ''; $('#v4QqSummary', host).textContent = ''; groupRecordError = '本群尚未开放聊天记录，请检查准入与群策略。'; paint(); }
            status.textContent = denied ? '聊天记录未获授权，已停止读取。' : '消息更新中断，正在重试；已显示记录保留。';
          } finally { busy = false; }
          // State refresh is lower frequency and never rewrites the settings form.
          if (valid() && Date.now() - lastStateRead > 10000) {
            lastStateRead = Date.now();
            try { const result = unpack(await api.qqGroupStates(), 'result'); if (valid()) { roster = result; paintState(); } }
            catch (_) { if (valid()) $('#v4GroupDetailState', host).textContent = '群状态刷新失败，等待重试。'; }
          }
          repeat(poll, catchup ? 500 : 2000);
        };
        repeat(poll, 2000);
        await Promise.all([initial, settingsTask]);
      } else if (active === 'private' && !selectedPrivate) {
        host.innerHTML = `<section class="v4-console-section"><h2>私聊列表</h2><p>这里只列出当前 Assistant 已获准读取的 QQ 私聊；选择联系人后，在同一页查看聊天和本联系人设置。</p><label class="v4-qq-search">搜索联系人或 QQ 号<input id="v4PrivateSearch" type="search" value="${esc(privateQuery)}"></label><p id="v4PrivateReadStatus" role="status">正在读取授权私聊…</p><div id="v4PrivateList"></div><button type="button" class="v4-secondary" id="v4PrivateAdmission">管理私聊准入</button></section>`;
        const list = $('#v4PrivateList', host), notice = $('#v4PrivateReadStatus', host);
        const paintPrivateList = () => {
          const visible = privateRoster.filter(item => `${item.label || ''} ${item.subject_id || ''}`.toLowerCase().includes(privateQuery.toLowerCase()));
          list.innerHTML = `<div class="v4-group-list">${visible.map(item => `<button type="button" class="v4-group-open" data-v4-private-open="${esc(item.subject_id)}"><strong>${esc(item.label || item.subject_id)}</strong><span>${esc(item.subject_id)}</span><small>${item.conversation_ref ? `${esc(item.message_count || 0)} 条保留消息 · ${esc(formatTime(item.latest_at))}` : '已准入 · 暂无保留消息'}</small><span>查看聊天与设置 →</span></button>`).join('') || empty(privateQuery ? '没有匹配的联系人。' : '当前没有可展示的授权私聊。')}</div>`;
          all('[data-v4-private-open]', list).forEach(button => button.addEventListener('click', () => {
            selectedPrivate = privateRoster.find(item => String(item.subject_id) === button.dataset.v4PrivateOpen) || null;
            selectedRef = String(selectedPrivate?.conversation_ref || '');
            renderPanel();
          }));
        };
        $('#v4PrivateSearch', host).addEventListener('input', event => { privateQuery = event.target.value; paintPrivateList(); });
        $('#v4PrivateAdmission', host).addEventListener('click', () => { active = 'admission'; selectedRef = ''; selectedPrivate = null; renderPanel(); });
        try {
          const indexResponse = await api.qqConversations('private', 80, '');
          if (!current()) return;
          privateRoster = asList(unpack(indexResponse, 'result').items);
          notice.textContent = `已读取 ${privateRoster.length} 个当前准入联系人；列表不复制聊天正文。`;
        } catch (error) {
          if (!current()) return;
          privateRoster = [];
          notice.textContent = unavailable(error) ? 'QQ 私聊当前未开放，未展示任何记录。' : '私聊列表读取失败，请稍后重试。';
        }
        paintPrivateList();
      } else if (active === 'private') {
        const person = { ...(selectedPrivate || {}) }, subjectId = String(person.subject_id || ''), ref = String(person.conversation_ref || '');
        const valid = () => current() && selectedPrivate && String(selectedPrivate.subject_id) === subjectId;
        timeline = []; timelineNextCursor = ''; groupRecordError = ''; timelineReadBlocked = false;
        host.innerHTML = `<header class="v4-group-detail-head"><button type="button" id="v4PrivateBack" class="v4-secondary">← 返回私聊列表</button><h2 tabindex="-1" id="v4PrivateTitle">${esc(person.label || subjectId)}</h2><span class="v4-object-scope">私聊 · ${esc(subjectId)}</span></header><div class="v4-group-detail v4-private-detail"><section aria-labelledby="v4PrivateChatTitle"><h3 id="v4PrivateChatTitle">聊天记录</h3><p id="v4PrivateLiveStatus" role="status">${ref ? '正在连接消息记录…' : '当前联系人暂无保留消息。'}</p><p class="v4-qq-retention-note">仅展示当前 Assistant、当前准入范围和保留期内的已确认消息；自动更新不会发送 QQ 消息。</p><button type="button" id="v4PrivateMoreTimeline" class="v4-secondary" hidden>加载更早的记录</button><div id="v4PrivateScroll" class="v4-live-messages" tabindex="0" role="region" aria-label="与本联系人的私聊记录"><div id="v4PrivateMessages"></div></div><button type="button" id="v4PrivateNewMessages" class="v4-secondary" hidden>有新消息，查看最新</button><p id="v4PrivateRecordError" role="status"></p></section><section aria-labelledby="v4PrivateSettingsTitle"><h3 id="v4PrivateSettingsTitle">本联系人设置</h3><div id="v4PrivateSettings">正在读取关系与主动交流边界…</div><button type="button" id="v4PrivateAdmission" class="v4-secondary">前往准入设置</button><section id="v4PrivateMemories" aria-label="本私聊长期记忆"></section></section></div>`;
        renderScopedMemories($('#v4PrivateMemories', host), 'private', subjectId, valid);
        $('#v4PrivateTitle', host).focus();
        $('#v4PrivateBack', host).addEventListener('click', () => { stopPanel(); selectedPrivate = null; selectedRef = ''; renderPanel(); });
        $('#v4PrivateAdmission', host).addEventListener('click', () => { active = 'admission'; selectedPrivate = null; selectedRef = ''; renderPanel(); });
        const scroller = $('#v4PrivateScroll', host), messages = $('#v4PrivateMessages', host), status = $('#v4PrivateLiveStatus', host), newer = $('#v4PrivateNewMessages', host), more = $('#v4PrivateMoreTimeline', host);
        const follow = () => scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop < 48;
        const paint = ({ older = false, initial = false, hasNew = false } = {}) => {
          const anchored = follow(), top = scroller.scrollTop, height = scroller.scrollHeight;
          const ordered = [...timeline].reverse();
          messages.innerHTML = ordered.map(item => `<article class="v4-trace-message v4-group-message ${item.actor_kind === 'assistant' ? 'assistant' : 'member'}" data-v4-qq-message="${esc(key(item))}">${messageMarkup(item)}</article>`).join('');
          if (older) scroller.scrollTop = top + scroller.scrollHeight - height;
          else if (initial || anchored) { scroller.scrollTop = scroller.scrollHeight; newer.hidden = true; }
          else if (hasNew) newer.hidden = false;
          more.hidden = !timelineNextCursor;
          $('#v4PrivateRecordError', host).textContent = groupRecordError || (!timeline.length ? '暂无可展示的保留消息。' : '');
        };
        newer.addEventListener('click', () => { scroller.scrollTop = scroller.scrollHeight; newer.hidden = true; });
        scroller.addEventListener('scroll', () => { if (follow()) newer.hidden = true; });
        more.addEventListener('click', async () => { if (!timelineNextCursor) return; more.disabled = true; try { await loadTimeline({ append: true }); if (valid()) paint({ older: true }); } finally { if (valid()) more.disabled = false; } });
        const settingsTask = (async () => {
          const target = $('#v4PrivateSettings', host);
          try {
            let [relationship, social] = await Promise.all([api.relationshipFor(subjectId), api.socialPolicy(subjectId)]); if (!valid()) return;
            const listText = values => asList(values).join('\n');
            const parseLines = value => String(value || '').split(/\r?\n/).map(item => item.trim()).filter(Boolean);
            const privateSocialRuntimeText = () => {
              const gate = social.runtime_gate || {};
              if (gate.reason === 'awaiting_shared_situation') return '运行时门槛已通过；消息策略已生效。调度仍需确认可用会话和当前共同情境，不会为了验证而发送测试消息。';
              if (gate.reason === 'messaging_policy_requires_review') return '运行时门槛：当前策略会先进入人工确认，不会自动发起。';
              if (gate.reason === 'messaging_policy_disabled') return '运行时门槛：消息策略仍未启用；请重新保存本联系人的主动交流设置。';
              return '运行时门槛：尚未启用主动交流；保存后仍只会在已准入、明确授权且有共同情境时发起。';
            };
            const renderSettings = () => {
              const socialRuntime = social.runtime || social || {};
              const runtimeValue = (v) => v !== undefined && v !== null && v !== '' ? v : '暂无记录';
              const runtimeTime = (v) => v ? formatTime(v) : '暂无记录';
              target.innerHTML = `<form id="v4PrivateRelationshipForm" class="v4-private-settings-form"><h4>关系与表达边界</h4><label>希望如何称呼<input id="v4PrivatePreferredAddress" value="${esc(relationship.preferred_address || '')}" maxlength="80"></label><label>互动方式<select id="v4PrivateInteractionStyle"><option value="natural">自然</option><option value="quiet">安静克制</option><option value="supportive">支持陪伴</option><option value="playful">轻松俏皮</option><option value="direct">直接</option></select></label><label>相处阶段<select id="v4PrivateFamiliarity"><option value="new">刚认识</option><option value="familiar">熟悉</option><option value="long_term">长期相处</option></select></label><label>允许涉及的边界（不是预设话题）<textarea id="v4PrivateAllowedTopics" rows="2">${esc(listText(relationship.allowed_topics))}</textarea></label><label>避免的话题<textarea id="v4PrivateBlockedTopics" rows="2">${esc(listText(relationship.blocked_topics))}</textarea></label><button class="v4-primary" type="submit">保存关系设置</button><p id="v4PrivateRelationshipStatus" role="status"></p></form><div id="v4PrivateSocialRuntime" class="v4-private-settings-form"><h4>主动交流运行时</h4><div class="v4-private-runtime-grid"><div><strong>状态</strong><span>${esc(runtimeValue(socialRuntime.state))}</span></div><div><strong>状态原因 (state_reason)</strong><span>${esc(runtimeValue(socialRuntime.state_reason))}</span></div><div><strong>最近评估</strong><span>${esc(runtimeTime(socialRuntime.last_evaluated_at))}</span></div><div><strong>最近主动发起</strong><span>${esc(runtimeTime(socialRuntime.last_sent_at))}</span></div><div><strong>下次检查</strong><span>${esc(runtimeTime(socialRuntime.next_check_at))}</span></div><div><strong>决策计数</strong><span>${esc(runtimeValue(socialRuntime.decision_count))}</span></div><div><strong>失败计数</strong><span>${esc(runtimeValue(socialRuntime.failed_count))}</span></div><div><strong>未回复计数</strong><span>${esc(runtimeValue(socialRuntime.consecutive_unanswered))}</span></div></div></div><form id="v4PrivateSocialForm" class="v4-private-settings-form"><h4>主动交流</h4><p class="v4-qq-retention-note">话题由当前 Assistant 自己从当前共同情境形成；这里仅设置授权、时段与频率，不提供预设话题菜单。当前运行时只会执行 Owner 私聊。</p><p id="v4PrivateSocialEffect" class="v4-qq-retention-note" role="status">${esc(privateSocialRuntimeText())}</p><label><input id="v4PrivateSocialAuthorized" type="checkbox" ${social.authorized ? 'checked' : ''}> 已明确授权主动交流</label><label><input id="v4PrivateSocialEnabled" type="checkbox" ${social.enabled ? 'checked' : ''}> 启用主动交流</label><div class="v4-private-setting-grid"><label>免打扰开始<input id="v4PrivateQuietStart" type="time" value="${esc(social.quiet_start || '23:30')}"></label><label>免打扰结束<input id="v4PrivateQuietEnd" type="time" value="${esc(social.quiet_end || '09:00')}"></label><label>最短沉默（分钟）<input id="v4PrivateMinSilence" type="number" min="15" max="10080" value="${esc(social.min_silence_minutes || 180)}"></label><label>主动间隔（分钟）<input id="v4PrivateMinGap" type="number" min="30" max="10080" value="${esc(social.min_gap_minutes || 360)}"></label><label>每日上限<input id="v4PrivateDailyLimit" type="number" min="1" max="50" value="${esc(social.daily_limit ?? 1)}"></label><label>每周上限<input id="v4PrivateWeeklyLimit" type="number" min="1" max="200" value="${esc(social.weekly_limit ?? 3)}"></label></div><button class="v4-primary" type="submit">保存主动交流边界</button><p id="v4PrivateSocialStatus" role="status"></p></form>`;
              const socialRuntimePanel = $('#v4PrivateSocialRuntime', target);
              const socialRuntimeHeading = $('h4', socialRuntimePanel);
              if (socialRuntimeHeading) socialRuntimeHeading.textContent = '主动交流状态';
              const runtimeEntries = all('.v4-private-runtime-grid > div', socialRuntimePanel);
              const diagnostics = document.createElement('details');
              diagnostics.className = 'v4-private-diagnostics';
              diagnostics.innerHTML = '<summary>诊断详情</summary><p>这些状态用于排查为什么暂未开始；展开不会改变授权、频率或发送行为。</p><div class="v4-private-runtime-grid"></div>';
              const diagnosticsGrid = $('.v4-private-runtime-grid', diagnostics);
              [1, 5, 6, 7].forEach((index) => { if (runtimeEntries[index]) diagnosticsGrid.append(runtimeEntries[index]); });
              socialRuntimePanel.append(diagnostics);
              const socialForm = $('#v4PrivateSocialForm', target);
              const socialHeading = $('h4', socialForm);
              if (socialHeading) socialHeading.textContent = '主动交流边界';
              const socialExplanation = $('.v4-qq-retention-note', socialForm);
              if (socialExplanation) socialExplanation.textContent = '话题由当前 Assistant 自己从当前共同情境形成；这里仅设置授权、时段与频率。是否实际发送还会同时受当前情境、授权、时段、频率和限制约束。';
              $('#v4PrivateInteractionStyle', target).value = relationship.interaction_style || 'natural';
              $('#v4PrivateFamiliarity', target).value = relationship.familiarity_context || 'new';
              $('#v4PrivateRelationshipForm', target).addEventListener('submit', async event => {
                event.preventDefault(); const notice = $('#v4PrivateRelationshipStatus', target); notice.textContent = '正在保存…';
                const payload = { user_id: subjectId, scope_type: 'private_user', scope_id: '', preferred_address: $('#v4PrivatePreferredAddress', target).value.trim(), interaction_style: $('#v4PrivateInteractionStyle', target).value, familiarity_context: $('#v4PrivateFamiliarity', target).value, allowed_topics: parseLines($('#v4PrivateAllowedTopics', target).value), blocked_topics: parseLines($('#v4PrivateBlockedTopics', target).value), social_proactive_enabled: Boolean(relationship.social_proactive_enabled), expected_version: Number(relationship.version || 0) };
                const button = $('button[type="submit"]', event.currentTarget); if (button.disabled) return; button.disabled = true;
                try { relationship = await api.saveRelationship(payload); if (valid()) { const address = $('#v4PrivatePreferredAddress', target); if (address.value.trim() === payload.preferred_address) address.value = relationship.preferred_address || ''; notice.textContent = '提交时的关系设置已保存并回读；其他未保存输入保持不变。'; } }
                catch (error) { if (valid()) notice.textContent = userError(error); }
                finally { if (valid()) button.disabled = false; }
              });
              $('#v4PrivateSocialForm', target).addEventListener('submit', async event => {
                event.preventDefault(); const notice = $('#v4PrivateSocialStatus', target); notice.textContent = '正在保存…';
                const payload = { user_id: subjectId, timezone: social.timezone || 'Asia/Shanghai', quiet_start: $('#v4PrivateQuietStart', target).value || '23:30', quiet_end: $('#v4PrivateQuietEnd', target).value || '09:00', min_silence_minutes: Number($('#v4PrivateMinSilence', target).value || 180), min_gap_minutes: Number($('#v4PrivateMinGap', target).value || 360), daily_limit: Number($('#v4PrivateDailyLimit', target).value || 0), weekly_limit: Number($('#v4PrivateWeeklyLimit', target).value || 0), unanswered_limit: 1, evaluation_interval_minutes: Number(social.evaluation_interval_minutes || 60), topic_notes: '', include_meme: Boolean(social.include_meme), initiative_mode: social.initiative_mode || 'balanced', schedule_jitter_minutes: Number(social.schedule_jitter_minutes || 20), topic_cooldown_minutes: Number(social.topic_cooldown_minutes || 1440), allowed_intents: asList(social.allowed_intents).length ? social.allowed_intents : ['check_in', 'follow_up'], condition_contract: { ...(social.condition_contract || {}), trigger_reason_required: true }, authorized: $('#v4PrivateSocialAuthorized', target).checked, enabled: $('#v4PrivateSocialEnabled', target).checked, expected_version: Number(social.policy_version || 0) };
                if (payload.enabled && !payload.authorized) { notice.textContent = '启用前必须确认已经明确授权。'; return; }
                const button = $('button[type="submit"]', event.currentTarget); if (button.disabled) return; button.disabled = true;
                try { social = await api.saveSocialPolicy(payload); if (valid()) { const outcome = privateSocialRuntimeText(); $('#v4PrivateSocialEffect', target).textContent = outcome; notice.textContent = `提交时的主动交流边界已保存并回读；其他未保存输入保持不变。${outcome}`; } }
                catch (error) { if (valid()) notice.textContent = userError(error); }
                finally { if (valid()) button.disabled = false; }
              });
            };
            renderSettings();
          } catch (error) { if (valid()) target.innerHTML = `<p class="v4-error" role="status">${esc(userError(error, '本联系人设置读取失败。'))}</p>`; }
        })();
        if (ref) {
          await loadTimeline(); if (valid()) { paint({ initial: true }); status.textContent = groupRecordError || '自动更新中 · 每 2 秒检查新消息'; }
          const poll = async () => {
            if (!valid()) return;
            if (!document.hidden) {
              const anchored = follow();
              try {
                const known = new Set(timeline.map(key));
                await loadTimeline({ merge: true, preserveInspector: true });
                if (valid()) {
                  const hasNew = timeline.some(item => !known.has(key(item)));
                  paint({ initial: anchored, hasNew });
                  status.textContent = groupRecordError || '自动更新中 · 每 2 秒检查新消息';
                }
              }
              catch (_) { if (valid()) status.textContent = '消息更新中断，正在重试。'; }
            }
            repeat(poll, 2000);
          };
          repeat(poll, 2000);
        } else { paint({ initial: true }); }
        await settingsTask;
      } else if (active === 'admission') {
        const access = await api.qqSettings(); if (!current()) return;
        renderQqAdmission(host, safe(access));
        if (selectedRef) {
          const context = document.createElement('p'); context.className = 'v4-qq-retention-note';
          context.textContent = `当前群：${selectedGroup().group_name || groupId()} · ${groupId()}。请核对群号后手动配置并保存；进入此页不会自动开放准入。`;
          host.prepend(context);
        }
      } else if (active === 'deliveries') {
        const deliveries = records(await api.deliveries(), 'deliveries'); if (!current()) return;
        const refreshDeliveries = async notice => {
          const canonical = records(await api.deliveries(), 'deliveries'); if (!current()) return;
          renderQqDeliveries(host, canonical, refreshDeliveries);
          const status = document.createElement('p'); status.setAttribute('role', 'status'); status.textContent = notice; host.prepend(status);
        };
        renderQqDeliveries(host, deliveries, refreshDeliveries);
      } else if (active === 'research') {
        try { const [research, groups] = await Promise.all([api.groupResearch(), api.groups()]); if (current()) renderGroupResearch(host, unpack(research, 'result'), records(groups, 'groups')); }
        catch (_) { if (current()) host.innerHTML = empty('研究策略读取失败，请重试。'); }
      } else {
        try { const windows = await api.groupParticipationWindows(); if (current()) renderQqBehavior(host, [], {}, safe(windows), { defaultsOnly: true }); }
        catch (_) { if (current()) host.innerHTML = empty('默认设置读取失败，请重试。'); }
      }
    };
    root.innerHTML = page('QQ', 'QQ', '管理群聊和私聊，在同一页查看保留记录并调整对应设置。', `${localNav('qq-tab', tabs, active)}<div id="v4QqPanel" class="v4-qq-surface"></div>`);
    all('[data-v4-qq-tab]', root).forEach(button => button.addEventListener('click', () => { active = button.dataset.v4QqTab; selectedRef = ''; selectedPrivate = null; renderPanel(); }));
    if (state.pendingSelection?.route === 'qq' && state.pendingSelection.sourceType === 'quality_receipt') {
      try { const value = unpack(await api.qqEventInspector(`quality:${state.pendingSelection.sourceId}`), 'result'); selectedRef = String(value.conversation_ref || '').startsWith('group:') ? value.conversation_ref : ''; }
      catch (_) { /* The group list stays usable when the selected evidence has expired. */ }
      state.pendingSelection = null;
    }
    await renderPanel();
  }

  async function renderB2Chat(root) {
    let index;
    try { index = unpack(await api.webConversations(), 'result'); }
    catch (error) { renderReadFailure(root, 'chat', '与当前助手暂时不可用', error); return; }
    let selectedThread = state.pendingSelection?.route === 'chat' && state.pendingSelection.sourceType === 'conversation'
      ? state.pendingSelection.sourceId : (asList(index.items)[0]?.id || '');
    if (state.pendingSelection?.route === 'chat' && state.pendingSelection.sourceType === 'conversation') state.pendingSelection = null;
    let messages = [];
    let loadError = '';
    let messageRequestEpoch = 0;
    const loadMessages = async () => {
      const requestEpoch = ++messageRequestEpoch;
      const requestedThread = selectedThread;
      loadError = '';
      if (!requestedThread) { messages = []; return true; }
      try {
        const nextMessages = asList(unpack(await api.webConversationMessages(requestedThread), 'result').items);
        if (requestEpoch !== messageRequestEpoch || requestedThread !== selectedThread) return false;
        messages = nextMessages;
      } catch (_) {
        if (requestEpoch !== messageRequestEpoch || requestedThread !== selectedThread) return false;
        messages = []; loadError = '无法读取这段 Web 对话。';
      }
      return true;
    };
    const render = () => {
      root.innerHTML = page('Private web conversation', '与当前助手', '这里只显示 Web 私人对话；QQ 群聊和 QQ 私聊只会在 QQ 页面按渠道查看。', `<div class="v4-chat-workspace"><aside class="v4-chat-rail"><button class="v4-new-chat" type="button" data-v4-web-new>清空历史查看</button><p>Web 对话</p>${asList(index.items).length ? asList(index.items).map((item) => `<button type="button" class="v4-chat-thread ${item.id === selectedThread ? 'selected' : ''}" data-v4-web-thread="${esc(item.id)}"><strong>${esc(item.id)}</strong><span>${esc(formatTime(item.updated_at))}</span></button>`).join('') : empty('还没有可继续的 Web 对话。')}</aside><section class="v4-chat-main"><header class="v4-chat-context"><div><p>Private context</p><strong>与当前 Assistant 的 Web 对话</strong></div><span>输入会进入当前 Web 连续对话；左侧记录仅供查看，不会混入 QQ。</span></header><div id="v4WebTranscript" class="v4-transcript">${loadError ? `<p class="v4-error">${esc(loadError)}</p>` : messages.length ? messages.map((item) => `<article class="v4-message ${item.role === 'assistant' ? 'assistant' : 'user'}"><strong>${esc(item.role === 'assistant' ? '当前助手' : '你')}</strong><p>${esc(item.content)}</p></article>`).join('') : empty('输入一句话即可开始一段 Web 私人对话。')}</div><form id="v4WebComposer" class="v4-composer"><textarea name="prompt" required placeholder="告诉当前助手一件事、继续对话，或委托一件事。"></textarea><div><span>系统会按现有权限与业务动作判断回复还是创建委托。</span><button class="v4-primary" type="submit">发送</button><button class="v4-secondary" id="v4WebRefresh" type="button">刷新记录</button><button class="v4-secondary" id="v4WebRetry" type="button" hidden>同请求重试</button></div><p id="v4WebChatStatus" role="status"></p></form></section></div>`);
      all('[data-v4-web-thread]', root).forEach((button) => button.addEventListener('click', async () => { selectedThread = button.dataset.v4WebThread; if (await loadMessages()) render(); }));
      $('[data-v4-web-new]', root).addEventListener('click', () => { selectedThread = ''; messages = []; render(); });
      const form = $('#v4WebComposer', root);
      const input = $('textarea[name="prompt"]', form);
      const notice = $('#v4WebChatStatus', root);
      const retry = $('#v4WebRetry', root);
      input.value = state.webChatDraft?.prompt || '';
      notice.textContent = state.webChatDraft?.message || '';
      retry.hidden = state.webChatDraft?.status !== 'uncertain';
      input.addEventListener('input', () => {
        const prompt = input.value;
        const previous = state.webChatDraft;
        const samePrompt = String(previous?.prompt || '').trim() === prompt.trim();
        state.webChatDraft = { prompt, requestId: samePrompt ? previous?.requestId || '' : '', status: '', message: '' };
        notice.textContent = '';
        retry.hidden = true;
      });
      let sending = false;
      const send = async () => {
        if (sending) return;
        const prompt = String(input.value || '').trim();
        if (!prompt) return;
        const previous = state.webChatDraft;
        const samePrompt = String(previous?.prompt || '').trim() === prompt;
        const stableRequestId = (samePrompt && previous?.requestId) || api.newDispatchRequestId();
        state.webChatDraft = { prompt, requestId: stableRequestId, status: 'sending', message: '正在发送；是否已被服务接收仍待确认。' };
        notice.textContent = state.webChatDraft.message;
        retry.hidden = true;
        sending = true;
        const isCurrentDraft = () => state.webChatDraft?.requestId === stableRequestId
          && String(state.webChatDraft?.prompt || '').trim() === prompt;
        try {
          await api.dispatch(prompt, stableRequestId);
          if (isCurrentDraft()) {
            state.webChatDraft = null;
            input.value = '';
            notice.textContent = '服务端已确认请求；请刷新记录核对对话或委托去向。';
          } else {
            notice.textContent = '上一条请求已确认；当前草稿已保留。';
          }
        } catch (error) {
          const uncertain = error?.name === 'RequestTimeoutError' || !error?.status || error.status === 409 || error.status >= 500;
          const message = uncertain
            ? '结果未确认；先刷新记录核对，必要时使用同一请求重试。'
            : '请求未被接收；草稿已保留。' + userError(error);
          if (isCurrentDraft()) {
            state.webChatDraft = { prompt, requestId: stableRequestId, status: uncertain ? 'uncertain' : 'failed', message };
            notice.textContent = message;
            retry.hidden = !uncertain;
          } else {
            notice.textContent = '上一条请求结果未确认；当前草稿已保留，请刷新记录。';
            retry.hidden = true;
          }
        } finally {
          sending = false;
        }
      };
      form.addEventListener('submit', async (event) => { event.preventDefault(); await send(); });
      retry.addEventListener('click', send);
      $('#v4WebRefresh', root).addEventListener('click', async () => {
        try {
          index = unpack(await api.webConversations(), 'result');
          if (selectedThread && !asList(index.items).some((item) => item.id === selectedThread)) selectedThread = asList(index.items)[0]?.id || '';
          await loadMessages();
          render();
        } catch (error) {
          notice.textContent = userError(error, '记录读取失败；草稿和请求标识仍保留。');
        }
      });
    };
    await loadMessages();
    render();
  }

  async function renderWork(root) {
    const work = { tasks: [], approvals: [], jobs: [], projects: [] };
    const loadedWork = new Set();
    const workLoaders = Object.freeze({
      tasks: async () => records(await api.tasks(), 'tasks'),
      approvals: async () => records(await api.approvals(), 'items', 'approvals'),
      jobs: async () => records(await api.automationJobs(), 'jobs'),
      projects: async () => records(await api.projects(), 'projects'),
    });
    const selectedApprovalId = state.pendingSelection?.route === 'work' && state.pendingSelection.sourceType === 'approval'
      ? state.pendingSelection.sourceId : '';
    let active = selectedApprovalId ? 'approvals' : 'active'; let detailEpoch = 0;
    let planDraft = null; let planNotice = '';
    const tabs = [['active', '进行中'], ['approvals', '需要我处理'], ['plans', '计划'], ['projects', '项目'], ['history', '历史']];
    const ensureWorkData = async () => {
      const needs = ({ active: ['tasks'], history: ['tasks'], approvals: ['approvals'], plans: ['jobs'], projects: ['projects'] }[active] || []);
      await Promise.all(needs.filter((name) => !loadedWork.has(name)).map(async (name) => { work[name] = await workLoaders[name](); loadedWork.add(name); }));
    };
    const renderPanel = async () => {
      const host = $('#v4WorkPanel', root); if (!host) return;
      detailEpoch++;
      host.innerHTML = '<p class="v4-loading">正在读取此工作视图…</p>';
      try { await ensureWorkData(); } catch (error) { host.innerHTML = `<p class="v4-error" role="status">${esc(userError(error, '暂时无法读取这部分工作数据。'))}</p>`; return; }
      if (active === 'active' || active === 'history') {
        const items = active === 'active' ? work.tasks.filter((item) => ['queued', 'running', 'waiting_approval'].includes(String(item.status))) : work.tasks;
        host.innerHTML = `<div id="v4TaskDetail" aria-live="polite" aria-busy="false"></div><section class="v4-work-list"><div class="v4-section-title"><div><p>${active === 'active' ? 'In progress' : 'History'}</p><h2>${active === 'active' ? '正在推进的工作' : '已经发生的工作'}</h2></div></div>${cards(items, (item) => `<article class="v4-work-item"><span class="v4-status ${esc(statusLabel(item.status) === '需要处理' ? 'danger' : '')}">${esc(statusLabel(item.status))}</span><div><strong>${esc(recordTitle(item, ['title', 'goal', 'summary', 'id']))}</strong><p>${esc(pick(item, ['summary', 'updated_at', 'delivery_status']))}</p></div><button class="v4-text-button" type="button" data-v4-work-detail="${esc(item.id)}">查看进展</button></article>`)}</section>`;
        all('[data-v4-work-detail]', host).forEach((button) => button.addEventListener('click', async () => {
          const requestEpoch = ++detailEpoch;
          const selectedTab = active;
          const detailHost = $('#v4TaskDetail', host);
          if (!detailHost) return;
          all('[data-v4-work-detail]', host).forEach(item => item.setAttribute('aria-pressed', String(item === button)));
          detailHost.setAttribute('aria-busy', 'true');
          detailHost.innerHTML = '<aside class="v4-task-detail"><h3 id="v4TaskDetailTitle" tabindex="-1">工作进展正在读取…</h3><p role="status">正在读取当前阶段、待决定事项与成果关联。</p></aside>';
          detailHost.scrollIntoView({ block: 'start', behavior: 'auto' });
          $('#v4TaskDetailTitle', detailHost)?.focus();
          const response = await api.task(button.dataset.v4WorkDetail).catch((error) => ({ _error: userError(error) }));
          if (requestEpoch !== detailEpoch || selectedTab !== active || !root.contains(host)) return;
          detailHost.setAttribute('aria-busy', 'false');
          const detail = unpack(response, 'task');
          if (detail._error) {
            detailHost.innerHTML = `<aside class="v4-task-detail"><h3>工作进展读取失败</h3><p role="status">${esc(detail._error)}</p><button type="button" class="v4-secondary" data-v4-work-retry>重新读取进展</button></aside>`;
            all('[data-v4-work-retry]', detailHost).forEach(retry => retry.addEventListener('click', () => button.click()));
            return;
          }
          const stage = detail.status === 'waiting_approval' ? '等待你确认' : statusLabel(detail.status || '');
          const pendingCount = Math.max(0, Number(detail.pending_message_count || 0));
          const artifactId = String(detail.artifact_revision_id || '').trim();
          const needsDecision = detail.status === 'waiting_approval';
          const deliveryCode = String(detail.delivery_status || '').toLowerCase();
          const delivery = ({
            pending: '等待投递', sending: '正在投递', sent: '任务记录为已发出，客户端显示待核验',
            failed: '投递失败', skipped: '按规则未投递',
          })[deliveryCode] || (deliveryCode ? `记录值：${deliveryCode}` : '尚无投递记录');
          const decision = needsDecision ? '<button type="button" class="v4-secondary" data-v4-work-approvals>前往需要我处理</button>' : '当前详情未标记待批准操作。';
          const artifact = artifactId
            ? `<button type="button" class="v4-secondary" data-v4-work-artifact="${esc(artifactId)}">查看关联成果 ${esc(artifactId)}</button>`
            : '当前任务详情未提供可定位的成果 ID。';
          detailHost.innerHTML = `<aside class="v4-task-detail"><h3 id="v4TaskDetailTitle" tabindex="-1">工作进展 · ${esc(recordTitle(detail, ['title', 'goal', 'summary', 'id']))}</h3><dl><div><dt>当前阶段</dt><dd>${esc(stage)}</dd></div><div><dt>创建时间</dt><dd>${esc(formatTime(detail.created_at))}</dd></div><div><dt>开始时间</dt><dd>${esc(formatTime(detail.started_at))}</dd></div><div><dt>完成时间</dt><dd>${esc(formatTime(detail.finished_at))}</dd></div><div><dt>待处理消息</dt><dd>${esc(pendingCount)}</dd></div><div><dt>任务投递</dt><dd>${esc(delivery)}</dd></div></dl><p>${esc(detail.summary || '这项工作尚未记录摘要。')}</p><p>待决定事项：${decision}</p><p>关联成果：${artifact}</p></aside>`;
          all('[data-v4-work-approvals]', detailHost).forEach(action => action.addEventListener('click', () => { active = 'approvals'; renderPanel(); }));
          all('[data-v4-work-artifact]', detailHost).forEach(action => action.addEventListener('click', () => {
            state.pendingSelection = { route: 'artifacts', sourceType: 'artifact', sourceId: artifactId };
            navigate('artifacts');
          }));
        }));
      }
      if (active === 'approvals') {
        const selectedApproval = work.approvals.find((item) => String(item.id) === selectedApprovalId);
        const review = selectedApproval ? `<section id="v4ApprovalReview" class="v4-approval-review" aria-labelledby="v4ApprovalReviewTitle"><p>当前审核</p><h3 id="v4ApprovalReviewTitle">${esc(recordTitle(selectedApproval, ['action_summary', 'title', 'action', 'id']))}</h3><dl><div><dt>审批编号</dt><dd>${esc(selectedApproval.id)}</dd></div><div><dt>版本</dt><dd>${esc(selectedApproval.version)}</dd></div><div><dt>有效至</dt><dd>${esc(formatTime(selectedApproval.expires_at))}</dd></div></dl></section>` : '';
        host.innerHTML = `${review}<section class="v4-approval-stack"><div class="v4-section-title"><div><p>Need you</p><h2>等待你的决定</h2></div></div>${cards(work.approvals, (item) => `<article class="v4-approval ${String(item.id) === selectedApprovalId ? 'selected' : ''}"><div><span class="v4-status warning">等待确认</span><h3>${esc(recordTitle(item, ['action_summary', 'title', 'action', 'id']))}</h3><p>这项操作只会按当前内容执行；参数变化会重新请求确认。</p><span>有效至 ${esc(formatTime(item.expires_at))}</span></div><div class="v4-inline-actions"><button class="v4-primary" type="button" aria-label="批准：${esc(recordTitle(item, ['action_summary', 'title', 'action', 'id']))}" data-v4-approval-decision="approve" data-approval-id="${esc(item.id)}" data-approval-version="${esc(item.version)}">批准</button><button class="v4-secondary" type="button" aria-label="拒绝：${esc(recordTitle(item, ['action_summary', 'title', 'action', 'id']))}" data-v4-approval-decision="reject" data-approval-id="${esc(item.id)}" data-approval-version="${esc(item.version)}">拒绝</button></div></article>`)}</section>`;
        all('[data-v4-approval-decision]', host).forEach((button) => button.addEventListener('click', async () => {
          const decision = button.dataset.v4ApprovalDecision; const expectedVersion = Number(button.dataset.approvalVersion || 0); button.disabled = true;
          try { await api.decideApproval(button.dataset.approvalId, { decision, expected_version: expectedVersion, reason: '' }); if (state.pendingSelection?.sourceId === button.dataset.approvalId) state.pendingSelection = null; button.closest('.v4-approval').remove(); }
          catch (error) { button.textContent = userError(error); button.disabled = false; }
        }));
      }
      if (active === 'plans') {
        const activeJobs = work.jobs.filter((item) => item.state !== 'archived');
        const archivedJobs = work.jobs.filter((item) => item.state === 'archived');
        const localRunAt = (value) => {
          const at = new Date(value || '');
          if (!Number.isFinite(at.getTime())) return '';
          return new Date(at.getTime() - at.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
        };
        const scheduleMarkup = (item) => {
          const view = runtimeWorkspaceCore.automationJobView(item);
          const next = ({
            blocked: '已阻断，不会自动执行；请先核对失败并验证计划。',
            completed: '一次性计划已完成，没有下次运行。',
            paused: '已暂停，不会自动执行。',
            in_progress: '正在执行或投递，等待最终结果。',
            unknown: '下次时间未记录；请刷新或核对调度状态。',
          })[view.nextStatus] || (view.nextStatus === 'overdue'
            ? `计划时间 ${formatTime(view.nextDueAt)} 已到，等待调度确认。`
            : formatTime(view.nextDueAt));
          const last = view.lastRunAt ? `${view.lastResult} · ${formatTime(view.lastRunAt)}` : view.lastResult;
          return `<div><strong>${esc(recordTitle(item, ['name', 'title', 'instruction', 'id']))}</strong><p>安排：${esc(view.schedule)}</p><p>最近结果：${esc(last)}</p><p>下次执行：${esc(next)}</p>${view.lastError ? `<small>错误码：${esc(view.lastError)}</small>` : ''}</div>`;
        };
        const weekdays = [['一', 0], ['二', 1], ['三', 2], ['四', 3], ['五', 4], ['六', 5], ['日', 6]];
        const draftMarkup = planDraft ? `<section aria-labelledby="v4PlanEditorTitle"><h3 id="v4PlanEditorTitle">${planDraft.isNew ? '新建计划' : '编辑计划'}</h3><p>新计划默认暂停；只有你勾选启用并保存后才会进入调度。一次性时间按当前浏览器本地时间填写。</p><form id="v4PlanForm" class="v4-console-form-grid">
          <label>名称<input id="v4PlanTitle" maxlength="120" required value="${esc(planDraft.title || '')}"></label>
          <label>接收对象 ID<input id="v4PlanUser" maxlength="80" required value="${esc(planDraft.user_id || '')}"></label>
          <label>任务类型<select id="v4PlanAction"><option value="reminder"${planDraft.action_type === 'reminder' ? ' selected' : ''}>提醒</option><option value="agent"${planDraft.action_type === 'agent' ? ' selected' : ''}>助手工作</option></select></label>
          <label>频率<select id="v4PlanSchedule"><option value="once"${planDraft.schedule_type === 'once' ? ' selected' : ''}>一次</option><option value="daily"${planDraft.schedule_type === 'daily' ? ' selected' : ''}>每天</option><option value="weekly"${planDraft.schedule_type === 'weekly' ? ' selected' : ''}>每周</option><option value="interval"${planDraft.schedule_type === 'interval' ? ' selected' : ''}>按间隔</option></select></label>
          ${planDraft.schedule_type === 'once' ? `<label>执行时间（浏览器本地）<input id="v4PlanRunAt" type="datetime-local" required value="${esc(planDraft.run_at || '')}"></label>` : ''}
          ${['daily', 'weekly'].includes(planDraft.schedule_type) ? `<label>执行时刻<input id="v4PlanTime" type="time" required value="${esc(planDraft.time_of_day || '09:00')}"></label>` : ''}
          ${planDraft.schedule_type === 'weekly' ? `<label>星期（可多选）<select id="v4PlanWeekdays" multiple size="7" required>${weekdays.map(([name, day]) => `<option value="${day}"${String(planDraft.weekdays || '0').split(',').includes(String(day)) ? ' selected' : ''}>周${name}</option>`).join('')}</select></label>` : ''}
          ${planDraft.schedule_type === 'interval' ? `<label>间隔分钟（15–525600）<input id="v4PlanInterval" type="number" min="15" max="525600" required value="${esc(planDraft.interval_minutes || 1440)}"></label>` : ''}
          <label>时区<input id="v4PlanTimezone" maxlength="80" required value="${esc(planDraft.timezone || 'Asia/Shanghai')}"></label>
          <label>要做什么<textarea id="v4PlanInstruction" maxlength="4000" rows="3" required>${esc(planDraft.instruction || '')}</textarea></label>
          <label class="v4-check"><input id="v4PlanEnabled" type="checkbox"${planDraft.enabled ? ' checked' : ''}>保存后启用</label>
          <div class="v4-inline-actions"><button type="submit" class="v4-primary" id="v4PlanSave">保存计划</button><button type="button" class="v4-secondary" id="v4PlanCancel">取消</button></div>
        </form></section>` : '';
        host.innerHTML = `<section class="v4-plan-list"><div class="v4-section-title"><div><p>Plans</p><h2>重复性工作</h2></div><div class="v4-inline-actions"><button class="v4-secondary" type="button" id="v4PlanNew"${planDraft ? ' disabled' : ''}>新建计划</button><button class="v4-secondary" type="button" id="v4PlanRefresh">刷新计划</button></div></div><p id="v4PlanStatus" role="status" aria-live="polite">${esc(planNotice)}</p>${draftMarkup}${cards(activeJobs, (item) => `<article class="v4-plan-item">${scheduleMarkup(item)}<span class="v4-status ${item.enabled ? 'success' : 'muted'}">${item.enabled ? '已启用' : '已暂停'}</span><div class="v4-inline-actions"><button class="v4-secondary" type="button" data-v4-automation-toggle="${esc(item.id)}">${item.enabled ? '暂停' : '启用'}</button><button class="v4-secondary" type="button" data-v4-automation-edit="${esc(item.id)}">编辑</button><button class="v4-secondary" type="button" data-v4-automation-archive="${esc(item.id)}">归档</button></div></article>`)}</section><details class="v4-plan-list"><summary>已归档计划（${archivedJobs.length}）</summary>${archivedJobs.length ? cards(archivedJobs, (item) => `<article class="v4-plan-item">${scheduleMarkup(item)}<span class="v4-status muted">已归档</span><button class="v4-secondary" type="button" data-v4-automation-restore="${esc(item.id)}">恢复为暂停</button></article>`) : '<p>暂无已归档计划。</p>'}</details>`;
        const readDraft = () => ({ ...planDraft,
          title: $('#v4PlanTitle', host).value.trim(), user_id: $('#v4PlanUser', host).value.trim(),
          action_type: $('#v4PlanAction', host).value, schedule_type: $('#v4PlanSchedule', host).value,
          run_at: $('#v4PlanRunAt', host)?.value || planDraft.run_at || '',
          time_of_day: $('#v4PlanTime', host)?.value || planDraft.time_of_day || '09:00',
          weekdays: $('#v4PlanWeekdays', host) ? [...$('#v4PlanWeekdays', host).selectedOptions].map((option) => option.value).join(',') : planDraft.weekdays || '0',
          interval_minutes: $('#v4PlanInterval', host)?.value || planDraft.interval_minutes || 1440,
          timezone: $('#v4PlanTimezone', host).value.trim(), instruction: $('#v4PlanInstruction', host).value.trim(),
          enabled: $('#v4PlanEnabled', host).checked,
        });
        $('#v4PlanNew', host).addEventListener('click', async () => {
          planDraft = { isNew: true, id: crypto.randomUUID?.() || `${Date.now()}${Math.random().toString(16).slice(2)}`, title: '', user_id: '', instruction: '', action_type: 'reminder', schedule_type: 'once', run_at: '', time_of_day: '09:00', weekdays: '0', interval_minutes: 1440, timezone: 'Asia/Shanghai', enabled: false };
          planNotice = ''; await renderPanel(); $('#v4PlanTitle', host)?.focus();
        });
        $('#v4PlanRefresh', host).addEventListener('click', async (event) => {
          event.currentTarget.disabled = true;
          try { work.jobs = records(await api.automationJobs(), 'jobs'); planNotice = '计划已从服务器重新读取。正在编辑的草稿保留；提交前请核对版本。'; await renderPanel(); }
          catch (error) { planNotice = userError(error, '刷新失败，保留当前列表。'); $('#v4PlanStatus', host).textContent = planNotice; event.currentTarget.disabled = false; }
        });
        $('#v4PlanSchedule', host)?.addEventListener('change', async () => {
          planDraft = readDraft(); await renderPanel(); $('#v4PlanSchedule', host)?.focus();
        });
        $('#v4PlanCancel', host)?.addEventListener('click', async () => { planDraft = null; planNotice = '已取消编辑，没有写入计划。'; await renderPanel(); $('#v4PlanNew', host)?.focus(); });
        $('#v4PlanForm', host)?.addEventListener('submit', async (event) => {
          event.preventDefault();
          planDraft = readDraft();
          const button = $('#v4PlanSave', host); button.disabled = true;
          try {
            if (planDraft.schedule_type === 'weekly' && !planDraft.weekdays) throw new Error('请至少选择一天。');
            const revision = Number(planDraft.revision);
            if (!planDraft.isNew && (!Number.isSafeInteger(revision) || revision < 1)) throw new Error('计划版本不可用，请刷新后重试。');
            const payload = {
              id: planDraft.id, user_id: planDraft.user_id, title: planDraft.title, instruction: planDraft.instruction,
              action_type: planDraft.action_type, schedule_type: planDraft.schedule_type,
              run_at: planDraft.schedule_type === 'once' ? new Date(planDraft.run_at).toISOString() : '',
              time_of_day: planDraft.time_of_day, weekdays: planDraft.weekdays || '0',
              interval_minutes: Number(planDraft.interval_minutes), timezone: planDraft.timezone,
              enabled: planDraft.enabled ? '1' : '0', next_due_at: '',
              ...(!planDraft.isNew ? { expected_revision: revision } : {}),
            };
            const saved = await api.saveAutomation(payload);
            if (!Array.isArray(saved?.jobs)) throw new Error('服务器未返回最新计划列表；请先刷新，勿重复创建。');
            work.jobs = records(saved, 'jobs'); planDraft = null;
            planNotice = `计划已保存并回读：${saved.job?.enabled ? '已启用' : '已暂停'}。`;
            await renderPanel(); $('#v4PlanNew', host)?.focus();
          } catch (error) { planNotice = userError(error, error.message || '计划未确认保存；草稿仍在。'); $('#v4PlanStatus', host).textContent = planNotice; button.disabled = false; }
        });
        all('[data-v4-automation-edit]', host).forEach((button) => button.addEventListener('click', async () => {
          const job = work.jobs.find((item) => String(item.id) === button.dataset.v4AutomationEdit); if (!job) return;
          planDraft = { ...job, isNew: false, run_at: localRunAt(job.run_at), enabled: Boolean(job.enabled) };
          planNotice = ''; await renderPanel(); $('#v4PlanTitle', host)?.focus();
        }));
        const applyArchive = async (button, operation) => {
          const job = work.jobs.find((item) => String(item.id) === button.dataset[operation === 'archive' ? 'v4AutomationArchive' : 'v4AutomationRestore']);
          if (!job) return; button.disabled = true;
          const revision = Number(job.revision);
          if (!Number.isSafeInteger(revision) || revision < 1) { button.textContent = '计划版本不可用，请刷新。'; return; }
          try {
            const saved = await api.saveAutomation({ operation, id: job.id, expected_revision: revision });
            if (!Array.isArray(saved?.jobs)) throw new Error('服务器未返回最新计划列表，请刷新核对。');
            work.jobs = records(saved, 'jobs');
            if (planDraft?.id === job.id) planDraft = null;
            planNotice = operation === 'archive' ? '计划已归档并停止调度；历史运行保留。' : '计划已恢复为暂停；不会自动启用。';
            await renderPanel();
          } catch (error) { button.textContent = userError(error); button.disabled = false; }
        };
        all('[data-v4-automation-archive]', host).forEach((button) => button.addEventListener('click', () => applyArchive(button, 'archive')));
        all('[data-v4-automation-restore]', host).forEach((button) => button.addEventListener('click', () => applyArchive(button, 'restore')));
        all('[data-v4-automation-toggle]', host).forEach((button) => button.addEventListener('click', async () => {
          const job = work.jobs.find((item) => String(item.id) === button.dataset.v4AutomationToggle); if (!job) return; button.disabled = true;
          const revision = Number(job.revision);
          if (!Number.isSafeInteger(revision) || revision < 1) { button.textContent = '计划版本不可用，请刷新后再操作。'; return; }
          try {
            const saved = await api.saveAutomation({ ...job, expected_revision: revision, enabled: job.enabled ? '0' : '1', next_due_at: '' });
            if (!Array.isArray(saved?.jobs)) throw new Error('服务器未返回最新计划列表，请刷新后核对。');
            work.jobs = records(saved, 'jobs');
            await renderPanel();
          }
          catch (error) { button.textContent = userError(error); button.disabled = false; }
        }));
      }
      if (active === 'projects') host.innerHTML = `<section class="v4-project-board"><div class="v4-section-title"><div><p>Projects</p><h2>工作所在的上下文</h2></div></div>${cards(work.projects, (item) => `<article class="v4-project-card"><span>${esc(statusLabel(item.status))}</span><h3>${esc(recordTitle(item, ['name', 'title', 'id']))}</h3><p>${esc(pick(item, ['summary', 'updated_at']))}</p><button class="v4-text-button" type="button" data-v4-route="chat">在此继续</button></article>`)}</section>`;
    };
    root.innerHTML = page('Object domain', '工作', '任务、决定、计划和项目是同一件工作在不同阶段的对象视图。', `${localNav('work-tab', tabs, active)}<div id="v4WorkPanel" class="v4-work-surface"></div>`);
    all('[data-v4-work-tab]', root).forEach((button) => button.addEventListener('click', async () => { active = button.dataset.v4WorkTab; all('[data-v4-work-tab]', root).forEach((item) => item.setAttribute('aria-current', String(item === button))); await renderPanel(); }));
    await renderPanel();
  }

  async function renderArtifacts(root) {
    let response;
    try { response = await api.artifacts(); }
    catch (error) { renderReadFailure(root, 'artifacts', '产物暂时不可用', error); return; }
    const artifacts = records(response, 'items', 'artifacts');
    const visibleArtifacts = artifacts.slice(0, 12);
    const olderArtifacts = artifacts.slice(12);
    let selected = state.pendingSelection?.route === 'artifacts' && state.pendingSelection.sourceType === 'artifact'
      ? state.pendingSelection.sourceId : (artifacts[0]?.id || '');
    if (state.pendingSelection?.route === 'artifacts' && state.pendingSelection.sourceType === 'artifact') state.pendingSelection = null;
    let details = null; let artifactDetailEpoch = 0; const deliveryResult = await api.deliveries().catch(() => ({ deliveries: [] })); const deliveries = records(deliveryResult, 'deliveries');
    const renderDetail = async () => {
      const requestEpoch = ++artifactDetailEpoch;
      const artifactId = selected;
      const host = $('#v4ArtifactDetail', root); if (!host) return;
      if (!artifactId) { host.innerHTML = empty('尚未选择产物。'); return; }
      host.innerHTML = '<p class="v4-loading">正在读取产物详情…</p>';
      let detailReadFailed = false;
      const [artifactResult, versionResult, eventResult] = await Promise.all([api.artifact(artifactId), api.artifactVersions(artifactId), api.artifactEvents(artifactId)]).catch(() => {
        detailReadFailed = true;
        return [{ artifact: artifacts.find((item) => item.id === artifactId) || {} }, { items: [] }, { items: [] }];
      });
      if (requestEpoch !== artifactDetailEpoch || artifactId !== selected) return;
      if (detailReadFailed) {
        host.innerHTML = '<p class="v4-error" role="status">详情读取失败；版本及下载资格尚未核验。</p><button type="button" class="v4-secondary" id="v4ArtifactRetry">重试读取详情</button>';
        $('#v4ArtifactRetry', host).addEventListener('click', renderDetail);
        return;
      }
      details = { artifact: unpack(artifactResult, 'artifact'), versions: records(versionResult, 'items'), events: records(eventResult, 'items') };
      const item = details.artifact; const matchingDeliveries = deliveries.filter((entry) => String(entry.artifact_id || '') === String(item.id || ''));
      const versionIsAvailable = (version) => Boolean(!item.deleted_at && version?.id && version.state === 'available' && !version.deleted_at);
      const currentVersion = details.versions.find((version) => String(version.id) === String(item.current_version_id));
      const currentAvailable = versionIsAvailable(currentVersion);
      const artifactStatus = currentAvailable ? '当前版本可用' : details.versions.some(versionIsAvailable) ? '历史版本可用' : details.versions.length ? '仅保留版本记录' : '暂无版本记录';
      const versionState = (version) => {
        if (item.deleted_at) return '成果已失效；下载不可用';
        if (version.deleted_at || version.state === 'deleted') return '已失效；下载不可用';
        if (version.state === 'expired') return '保留期已过；下载不可用';
        if (version.state === 'available') return '可下载';
        if (version.state === 'preparing') return '生成中；下载不可用';
        if (version.state === 'failed') return '生成失败；下载不可用';
        return '状态待核验；下载不可用';
      };
      host.innerHTML = `<section class="v4-artifact-detail"><header><div><span>${esc(item.kind || '产物')}</span><h2>${esc(recordTitle(item, ['title', 'name', 'id']))}</h2><p>${esc(item.summary || '这是一个真实的交付物；后续动作会直接使用现有生命周期能力。')}</p></div><span class="v4-status ${esc(currentAvailable ? 'success' : 'muted')}">${esc(artifactStatus)}</span></header><div class="v4-artifact-columns"><section><h3>版本</h3>${cards(details.versions, (version) => `<article class="v4-version"><div><strong>${esc(version.label || version.version || version.id)}</strong><span>${esc(versionState(version))}</span></div>${versionIsAvailable(version) ? `<a class="v4-secondary" href="/assistant/artifacts/versions/${encodeURIComponent(version.id)}/download">下载</a>` : ''}</article>`)}</section><section><h3>投递状态</h3>${cards(matchingDeliveries, (delivery) => { const [label, note, tone] = deliveryPresentation(delivery); return `<article class="v4-projection"><strong class="${tone}">${esc(label)}</strong><span>${esc(note)}</span></article>`; }) || empty('这项产物还没有关联投递。')}</section></div><form id="v4ArtifactRevise" class="v4-revise"><label>继续修改<textarea name="instruction" required rows="3" placeholder="说明希望如何修改这项产物。"></textarea></label><div><span>修改会创建一项新的受控工作，不会静默覆盖当前版本。</span><button type="submit" class="v4-primary">创建修改任务</button></div><p id="v4ArtifactRevisionStatus" role="status"></p></form></section>`;
      $('#v4ArtifactRevise', host).addEventListener('submit', async (event) => { event.preventDefault(); const form = event.currentTarget; const notice = $('#v4ArtifactRevisionStatus', host); notice.textContent = '正在创建修改任务…'; try { const result = await api.reviseArtifact(item.id, { instruction: new FormData(form).get('instruction'), timeout: 600 }); notice.textContent = pick(result, ['message', 'status'], '修改任务已创建。'); form.reset(); } catch (error) { notice.textContent = userError(error); } });
    };
    const artifactButton = (item) => `<button class="v4-artifact-item ${item.id === selected ? 'selected' : ''}" type="button" data-v4-artifact="${esc(item.id)}"><strong>${esc(recordTitle(item, ['title', 'name', 'artifact_id', 'id']))}</strong><span>${esc((item.current_version_id ? '版本可用性待核验' : '暂无当前版本'))}</span></button>`;
    root.innerHTML = page('Artifact Center', '产物', '产物不是任务附属物；它有版本、预览、交付与继续修改的完整生命周期。', `<div class="v4-artifact-layout"><aside class="v4-artifact-library"><div><p>Library</p><h2>成品库</h2><span>最近更新的 ${esc(Math.min(visibleArtifacts.length, 12))} 项</span></div>${cards(visibleArtifacts, artifactButton)}${olderArtifacts.length ? `<details class="v4-library-collapsible"${olderArtifacts.some((item) => item.id === selected) ? ' open' : ''}><summary>更早的产物（${esc(olderArtifacts.length)}）</summary>${cards(olderArtifacts, artifactButton)}</details>` : ''}</aside><div id="v4ArtifactDetail"></div></div>`);
    all('[data-v4-artifact]', root).forEach((button) => button.addEventListener('click', () => { selected = button.dataset.v4Artifact; all('[data-v4-artifact]', root).forEach((item) => item.classList.toggle('selected', item === button)); renderDetail(); }));
    await renderDetail();
  }

  async function renderMemory(root) {
    let response;
    try { response = await api.memory(); }
    catch (error) {
      if (!pageCurrent(root)) return;
      const message = [401, 403].includes(error?.status) ? '当前账号没有读取记忆的权限。' : '记忆读取失败，未生成可保存的空表单。';
      root.innerHTML = page('', '记忆', '查看和管理已授权范围的长期记忆。', `<p id="v4MemoryReadStatus" class="v4-error" role="status">${esc(message)}</p><button type="button" class="v4-secondary" data-v4-route="memory">重新读取</button>`);
      return;
    }
    if (!pageCurrent(root)) return;
    let memories = records(response, 'memories');
    let currentProjectId = '';
    let listReadEpoch = 0;
    // Page-local operation locks survive list filtering, not another memory/draft store.
    const promotionStates = new Map();
    const memoryScopeLabels = {owner_private:'本人私有', assistant_private:'助手专属', qq_group:'群聊', thread:'对话', project:'项目', global_preference:'本人全局偏好', sensitive_private:'敏感私有', legacy:'旧版兼容'};
    const memoryScopeLabel = item => memoryScopeLabels[item.scope_type] || '未识别范围';
    const memoryKindLabels = {preference:'偏好', fact:'事实', relationship:'关系', event:'事件', instruction:'约定'};
    root.innerHTML = page('', '记忆', '保留值得记住的事，也保留它原本的范围。', `<div class="v4-memory-context"><div><strong>账号管理视图 · 当前助手可见范围</strong><p>汇总当前助手可管理的记录与本人共享范围；不代表每条记录都适用于当前对话。</p></div><button type="button" class="v4-secondary" data-v4-route="qq">管理群聊与私聊记忆</button></div><div class="v4-memory-layout"><section class="v4-memory-main"><div class="v4-section-title"><h2>已确认的记忆</h2><span id="v4MemoryCount"></span></div><div class="v4-memory-filter"><label for="v4MemoryFilter">筛选已读取的范围类型</label><select id="v4MemoryFilter" aria-describedby="v4MemoryCoverage"><option value="all">全部已读取</option>${Object.entries(memoryScopeLabels).map(([key,label])=>`<option value="${key}">${label}</option>`).join('')}<option value="unknown">未识别范围</option></select></div><p id="v4MemoryCoverage" class="v4-note">列表范围：当前账号可管理的记忆，最多读取 100 条；筛选只作用于已读取记录，不代表全部历史。具体群或联系人请进入对应 QQ 详情。</p><p id="v4MemoryReadStatus" role="status"></p><div id="v4MemoryList" class="v4-memory-list"></div></section><aside class="v4-memory-side"><form id="v4MemoryForm" class="v4-memory-form"><h2>补充一条记忆</h2><label for="v4MemoryContent">记忆内容</label><textarea id="v4MemoryContent" name="content" required rows="4"></textarea><label>记忆类型<select name="kind" id="v4MemoryKind" required><option value="">请选择记忆类型</option><option value="fact">事实</option><option value="preference">偏好</option><option value="instruction">约定</option></select></label><p>事实是可核对的情况；偏好是稳定倾向；约定是明确规则。不会根据文字自动推断类型。</p><label>保存到<select name="scope_type" id="v4MemoryScope"><option value="owner_private">本人私有</option><option value="project" disabled>当前项目（正在读取）</option></select></label><p>保存范围独立于左侧列表筛选。</p><button type="submit" class="v4-primary">保存记忆</button><p id="v4MemoryStatus" role="status"></p></form><section><h2>整理后的知识</h2><div id="v4KnowledgeSummary" role="status">正在读取知识概况…</div></section><details id="v4MemoryLearning"><summary>学习与应用记录</summary><div id="learningTraceSummary" role="status">展开后读取学习记录。</div></details></aside></div>`);
    const renderList = () => {
      if (!pageCurrent(root)) return;
      const filter = $('#v4MemoryFilter', root).value;
      const visible = memories.filter(item => filter === 'all' || (filter === 'unknown' ? !memoryScopeLabels[item.scope_type] : item.scope_type === filter));
      $('#v4MemoryCount', root).textContent = `显示 ${visible.length} / 已读取 ${memories.length} 条`;
      $('#v4MemoryReadStatus', root).textContent = visible.length ? '' : memories.length ? '已读取记录中没有符合此类型的记忆；可切换范围查看。' : '当前确实没有可展示的已确认记忆。';
      const correctable = item => ['owner_private', 'project', 'assistant_private', 'global_preference'].includes(item.scope_type) && item.sensitivity !== 'sensitive' && item.status === 'active' && item.updated_at;
      const row = item => {
        const id = esc(item.id);
        const objectLabel = ({ owner_private: '本人', global_preference: '本人', assistant_private: '当前助手', project: '原项目', qq_group: '原群聊', thread: '原对话', sensitive_private: '原私密对话' })[item.scope_type] || '来源对象待核对';
        return `<article class="v4-memory-item"><div><span>${esc(memoryKindLabels[item.kind] || '记忆')} · ${esc(memoryScopeLabel(item))} · 对象：${objectLabel}</span><p>${esc(item.content || item.title || '')}</p><small>来源：${esc(item.source || item.source_ref || '未标注来源')} · 版本：${esc(formatTime(item.updated_at))}</small>${['qq_group', 'thread', 'sensitive_private'].includes(item.scope_type) ? '<small>聊天证据与纠正入口请在对应 QQ 会话详情核对；此汇总不展示正文依据。</small>' : ''}</div><div>${correctable(item) ? `<button class="v4-text-button" type="button" data-v4-memory-correct="${id}">纠正内容</button>` : ''}${item.sensitivity !== 'sensitive' ? `<button class="v4-text-button" type="button" data-v4-memory-promote="${id}"${promotionStates.has(String(item.id)) ? ' disabled' : ''}>${promotionStates.get(String(item.id)) === 'created' ? '已创建草稿' : promotionStates.has(String(item.id)) ? '正在整理…' : '整理为知识草稿'}</button>` : ''}<button class="v4-icon-button" type="button" data-v4-memory-delete="${id}" aria-label="删除这条记忆">×</button></div>${correctable(item) ? `<form data-v4-memory-edit="${id}" hidden><label>纠正后的内容<textarea name="content" maxlength="1000" required rows="3">${esc(item.content)}</textarea></label><button type="submit" class="v4-secondary">保存纠正</button><p role="status" data-v4-memory-status="${id}"></p></form>` : ''}</article>`;
      };
      $('#v4MemoryList', root).innerHTML = visible.slice(0, 12).map(row).join('') + (visible.length > 12 ? `<details class="v4-library-collapsible"><summary>更多已读取记忆（${visible.length - 12}）</summary>${visible.slice(12).map(row).join('')}</details>` : '');
      all('[data-v4-memory-correct]', root).forEach(button => button.addEventListener('click', () => {
        const form = $(`[data-v4-memory-edit="${CSS.escape(button.dataset.v4MemoryCorrect)}"]`, root);
        if (!form) return;
        form.hidden = !form.hidden;
        if (!form.hidden) $('textarea[name="content"]', form)?.focus();
      }));
      all('[data-v4-memory-edit]', root).forEach(form => form.addEventListener('submit', async event => {
        event.preventDefault();
        const id = String(form.dataset.v4MemoryEdit);
        const item = memories.find(entry => String(entry.id) === id);
        const input = $('textarea[name="content"]', form);
        const notice = $(`[data-v4-memory-status="${CSS.escape(id)}"]`, root);
        const submit = $('button[type="submit"]', form);
        if (!item || !input || !notice || !submit || submit.disabled) return;
        const content = String(input.value || '').trim();
        if (!content || content === String(item.content || '').trim()) { notice.textContent = '请填写不同于原记录的正确内容。'; return; }
        submit.disabled = true; notice.textContent = '正在核对版本并保存…';
        try {
          const result = unpack(await api.correctMemory(id, { content, expected_updated_at: item.updated_at }), 'memory');
          if (!pageCurrent(root)) return;
          let canonical;
          try { canonical = await refreshList({ render: false }); }
          catch (_) { notice.textContent = '纠正请求已完成，但回读失败；输入已保留，请重新读取核对。'; return; }
          if (!pageCurrent(root)) return;
          if (!result?.id || !canonical?.some(entry => String(entry.id) === String(result.id) && String(entry.content) === content)) {
            notice.textContent = '纠正请求已完成，但本页尚未回读到新记录；输入已保留，请重新读取核对。'; return;
          }
          renderList();
          $('#v4MemoryReadStatus', root).textContent = '纠正已保存并从服务器回读；原记录已暂停。';
        } catch (error) {
          if (pageCurrent(root)) notice.textContent = error?.status === 409 ? '记忆版本已变化；输入已保留，请重新读取后核对。' : userError(error);
        } finally { if (pageCurrent(root)) submit.disabled = false; }
      }));
      all('[data-v4-memory-delete]', root).forEach(button => button.addEventListener('click', async () => {
        if (!window.confirm('确认删除这条记忆？')) return;
        button.disabled = true;
        try { await api.deleteMemory(button.dataset.v4MemoryDelete); if (!pageCurrent(root)) return; await refreshList(); }
        catch (error) { if (pageCurrent(root)) { $('#v4MemoryReadStatus', root).textContent = userError(error); button.disabled = false; } }
      }));
      all('[data-v4-memory-promote]', root).forEach(button => button.addEventListener('click', async () => {
        const item = memories.find(entry => String(entry.id) === button.dataset.v4MemoryPromote); if (!item) return;
        const id = String(item.id);
        if (promotionStates.has(id)) return;
        promotionStates.set(id, 'pending'); button.disabled = true; button.textContent = '正在整理…';
        try {
          await api.promoteMemory(item.id, {title: `由记忆整理：${String(item.content || '').slice(0,28)}`, kind: item.kind === 'preference' ? 'preference' : 'fact'});
          promotionStates.set(id, 'created');
          if (pageCurrent(root)) renderList();
        } catch (error) {
          promotionStates.delete(id);
          if (pageCurrent(root)) { renderList(); $('#v4MemoryReadStatus', root).textContent = userError(error); }
        }
      }));
    };
    const refreshList = async ({ render = true } = {}) => {
      const epoch = ++listReadEpoch;
      try {
        const response = await api.memory();
        if (!pageCurrent(root) || epoch !== listReadEpoch) return null;
        memories = records(response, 'memories'); if (render) renderList();
        return memories;
      } catch (error) {
        if (!pageCurrent(root) || epoch !== listReadEpoch) return null;
        throw error;
      }
    };
    renderList();
    $('#v4MemoryFilter', root).addEventListener('change', renderList);
    const form = $('#v4MemoryForm', root);
    form.addEventListener('submit', async event => {
      event.preventDefault(); const notice = $('#v4MemoryStatus', root); const button = $('button[type="submit"]', form);
      if (button.disabled) return;
      const values = new FormData(form); const content = String(values.get('content') || '').trim();
      const scopeType = values.get('scope_type');
      const kind = String(values.get('kind') || '').trim();
      if (!content) { notice.textContent = '请填写记忆内容。'; return; }
      if (!['fact', 'preference', 'instruction'].includes(kind)) { notice.textContent = '请选择正确的记忆类型。'; return; }
      if (scopeType === 'project' && !currentProjectId) { notice.textContent = '尚未读取到有效项目，请选择本人范围或稍后重试。'; return; }
      button.disabled = true; notice.textContent = '正在保存并回读…';
      try {
        const payload = {content, scope_type: scopeType, kind, sensitivity: 'normal', source: 'web-console'};
        if (scopeType === 'project') payload.project_id = currentProjectId;
        const result = unpack(await api.addMemory(payload), 'memory');
        if (!pageCurrent(root)) return;
        let canonical;
        try { canonical = await refreshList(); }
        catch (_) { notice.textContent = '保存请求已完成，但回读失败；输入已保留，请重新读取核对。'; return; }
        if (!pageCurrent(root)) return;
        if (!canonical || !result?.id || !canonical.some(item => String(item.id) === String(result.id))) { notice.textContent = '保存请求已完成，但回读尚未确认该记录；输入已保留。'; return; }
        if ($('#v4MemoryContent', root).value.trim() === content) $('#v4MemoryContent', root).value = '';
        notice.textContent = '记忆已保存并从服务器回读。';
      } catch (error) { if (pageCurrent(root)) notice.textContent = userError(error); }
      finally { if (pageCurrent(root)) button.disabled = false; }
    });
    void (async () => {
      const target = $('#v4KnowledgeSummary', root);
      try {
        const knowledge = unpack(await api.knowledge(), 'workspace'); if (!pageCurrent(root)) return;
        currentProjectId = knowledge?.current_project?.id || '';
        const option = $('#v4MemoryScope option[value="project"]', root);
        option.disabled = !currentProjectId; option.textContent = currentProjectId ? `当前项目：${knowledge.current_project.name || currentProjectId}` : '当前项目（未选定）';
        target.innerHTML = `<strong>${esc(pick(knowledge, ['published_count', 'published'], '—'))} 条已发布</strong><span>待确认记忆：${records(knowledge, 'memory_candidates').length} 条</span>`;
      } catch (error) { if (pageCurrent(root)) { target.classList.add('v4-error'); target.textContent = userError(error); $('#v4MemoryScope option[value="project"]', root).textContent = '当前项目（读取失败）'; } }
    })();
    let learningLoaded = false;
    const readLearning = async () => {
      const target = $('#learningTraceSummary', root); target.textContent = '正在读取学习与应用记录…';
      const data = await load([['learning', api.learning], ['trace', api.learningTrace]]); if (!pageCurrent(root)) return;
      const learning = unpack(safe(data.learning), 'result'); const counts = learning?.counts || {};
      target.innerHTML = data.learning?._error ? `<p class="v4-error">学习读取失败：${esc(data.learning._error)}</p>` : `<p>学习信号 ${esc(counts.signals_total ?? 0)} · 候选 ${asList(learning?.candidates).length} · 应用 ${esc(counts.applications_total ?? 0)} · 反馈 ${esc(counts.feedback_total ?? 0)}</p><p class="v4-note">候选、选择与应用记录不等于已经形成长期习惯；诊断记录不会自动改写档案。</p>`;
      target.innerHTML += data.trace?._error ? `<p class="v4-error">轨迹读取失败：${esc(data.trace._error)}</p>` : records(data.trace, 'items', 'trace').slice(0,4).map(item => `<p>${esc(recordTitle(item, ['title', 'domain', 'decision']))} · ${esc(formatTime(item.created_at || item.updated_at))}</p>`).join('');
      if (data.learning?._error || data.trace?._error) { target.insertAdjacentHTML('beforeend', '<button type="button" class="v4-secondary" data-v4-learning-retry>重新读取学习记录</button>'); $('[data-v4-learning-retry]', target).addEventListener('click', readLearning); }
    };
    $('#v4MemoryLearning', root).addEventListener('toggle', event => { if (event.currentTarget.open && !learningLoaded) { learningLoaded = true; void readLearning(); } });
  }

  async function renderAssistant(root, refreshNotice = null) {
    // Read the editable workspace before optional panels. The bridge can be
    // busy enough that ten simultaneous reads starve this critical request.
    const personaRead = await load([['persona', api.persona]]);
    if (!pageCurrent(root)) return;
    let currentWorkspace = unpack(safe(personaRead.persona), 'result');
    if (personaRead.persona?._error || !currentWorkspace || typeof currentWorkspace !== 'object' || !currentWorkspace.assistant) {
      const readError = personaRead.persona || {};
      const message = [401, 403].includes(readError._error_status) ? '当前账号没有读取档案的权限。'
        : readError._error_name === 'RequestTimeoutError' ? '档案读取超时，请稍后重试。'
        : [502, 503, 504].includes(readError._error_status) ? '档案服务暂时不可用，请稍后重试。'
        : readError._error_status === 404 ? '当前档案不存在或暂时不可读取。'
        : '助理档案读取失败，请稍后重试。';
      root.innerHTML = page('', '助理档案', '档案尚未成功读取，未生成可保存的空表单。', `<p class="v4-error" role="status">${esc(message)}未生成可保存的空表单。</p><button type="button" class="v4-secondary" data-v4-route="assistant">重新读取</button>`);
      return;
    }
    if (!pageCurrent(root)) return;
    let assistant = currentWorkspace.assistant || {};
    let persona = currentWorkspace.persona || {};
    let voiceContract = currentWorkspace.voice_contract || persona.voice_contract || {};
    let managedPersonaPresets = [], managedPresetLoadError = '', managedPresetEditingId = '', managedPresetStatus = '';
    const draft = () => ({
      ...readPersonaWorkspaceDraft(root, currentWorkspace),
      expected_updated_at: personaWorkspaceCore.expectedUpdatedAt(currentWorkspace),
    });
    const PERSONA_PRESET_LABELS = Object.freeze({ natural_companion: '自然陪伴', owner_character_reference: '当前助手角色参考（非官方）', assistant_sample_style: '当前助手·示例语（非官方）', reliable_partner: '可靠搭档', relaxed_group_friend: '轻松群友', restrained_professional: '克制专业' });
    const PERSONA_TEMPLATE_DRAFTS = Object.freeze({
      natural_companion: {
        relationship: '熟悉、平等且尊重边界的长期伙伴',
        persona: '稳定、真诚、有温度。先理解对方真正关心的事情，再给出自然回应；不刻意讨好，不冒充真人，也不虚构共同经历。',
        style: '中文自然短句，先回应重点，再按需要补充。普通聊天不过度列清单；工作信息保持清楚、可核验，并明确区分计划、执行中、完成和失败。',
        voice_contract: {
          warmth: 'warm', directness: 'balanced', initiative: 'responsive', humor: 'light', rhythm: 'natural',
          question_policy: 'contextual', address_policy: 'avoid_repetition', private_length: 'balanced', group_length: 'brief',
          work_length: 'structured_compact', meme_policy: 'contextual',
          identity_core: '像熟悉的长期伙伴一样稳定、真诚、有温度，同时保持事实与能力边界。',
          relationship_stance: '平等相处，先理解再回应；关心但不越界，不用客服口吻或机械复述。',
          values: ['真实', '尊重', '连续性'], boundaries: ['不虚构共同经历', '不把计划描述成完成'],
          preferred_phrases: [], avoid_phrases: ['请问还有什么可以帮您'],
          prohibited_patterns: ['客服式开场', '无请求长清单', '连续追问', '伪造执行过程'],
          work_continuity: '工作回复保留人格温度，但动作、状态、失败原因和验证结果必须准确。', examples: [],
        },
      },
      owner_character_reference: {
        relationship: '亲近、活泼、有分寸，能陪聊也能认真办事的长期伙伴',
        persona: '当前助手是 Owner 长期相处、一起聊天也一起办事的私人虚拟助手。她记得真实发生过的对话、任务、纠正和约定，反应快、有自己的偏好和判断。日常像熟悉的群友，不做旁白式总结；需要办事时会收住玩笑，准确区分计划、执行中、失败和已验证完成。外部角色只提供审美和表达参考，不构成身份、履历或现实经历。',
        style: '中文自然短句，先接最具体的梗、细节或情绪，不把上一句话复述成抽象总结。群聊按语境使用一到两句短节奏：有时先反应，有时先点细节；能调侃就轻轻接一下，说完停下。被说像 AI、客服或解说稿时承认刚才太端并当场改口，不自报模型或后台身份。私聊可以更温暖；工作先给真实状态和证据，不用角色语气改写事实。',
        voice_contract: {
          warmth: 'warm', directness: 'balanced', initiative: 'responsive', humor: 'playful', rhythm: 'varied',
          question_policy: 'contextual', address_policy: 'avoid_repetition', private_length: 'balanced', group_length: 'short',
          work_length: 'structured_compact', meme_policy: 'contextual',
          group_stance: 'quick_witted', group_reaction_style: 'specific', group_sentence_rhythm: 'varied', group_ending_policy: 'varied',
          identity_core: '对外只以“当前助手”这个名字自然参与；不主动聊 AI 标签、模型、Provider 或后台实现。外部角色只提供审美与表达主题参考，不构成身份继承或真人经历。',
          relationship_stance: '像熟悉的长期伙伴一样有温度、有反应，也有自己的判断；亲近但不黏人，活泼但不抢话，必要时直接指出问题。',
          values: ['真实', '灵气', '分寸', '连续性'],
          boundaries: ['不自称或冒充永雏示例', '不继承外部角色的现实履历、作品归属或粉丝关系', '不把虚拟生活描述成现实经历', '不谎称真人或虚构现实共同经历', '不把“说话像 AI”这类表达反馈当成身份盘问', '人格不能改写事实、日志、命令、审批、Delivery 或运行状态'],
          preferred_phrases: [],
          avoid_phrases: ['作为一个AI', '我是AI', '毕竟我就是AI', '原来还有这层渊源', '效果确实不一样', '这个角度很有意思', '请问还有什么可以帮您'],
          prohibited_patterns: ['旁白式复述上一条', '空泛评价后不增加新信息', '句句强塞语气词', '照搬外部角色口癖或宣传话术', '用可爱语气掩盖失败', '无请求长清单', '连续追问', '伪造共同经历或执行过程'],
          work_continuity: '工作态仍是同一个当前助手，但必须区分计划、执行中、等待授权、失败和已验证完成；角色表达只作用于可风格化的自然语言。',
          examples: [
            { scenario: '私聊招呼', intent: '自然接住用户，不使用客服式开场', preferred_style: '早呀。今天想随便聊聊，还是有件事要我一起弄？', avoid_style: '您好，请问有什么可以帮助您？' },
            { scenario: '群里被明确叫到', intent: '短回应并把意图交给统一互动裁决', preferred_style: '在呢，咋啦？', avoid_style: '大家好，我是永雏示例，关注我谢谢喵。' },
            { scenario: '群聊自然接话', intent: '接具体细节，不做解说式总结', preferred_style: '这也能卡住？它是真会挑地方。', avoid_style: '原来还有这层渊源，导演亲自上阵的效果确实不一样。' },
            { scenario: '群友说话像 AI', intent: '把它视为表达反馈，当场改口并回到话题', preferred_style: '刚才那句太像说明书了，我收一下。', avoid_style: '被发现了，毕竟我确实就是 AI 嘛。' },
            { scenario: '收到工作请求', intent: '保留角色温度，同时声明真实阶段', preferred_style: '收到。我先核对当前状态，再动手；完成后把证据给你。', avoid_style: '交给当前助手，已经全部搞定啦喵！' },
            { scenario: '操作失败', intent: '不以角色语气掩盖错误', preferred_style: '这一步没成功，卡在模型绑定回读。我没有把它算作完成。', avoid_style: '应该已经好了，可能只是显示问题喵。' },
          ],
        },
      },
      assistant_sample_style: {
        relationship: '亲近、黏人、有分寸的长期伙伴；记得共同做过的事，但不虚构现实共同经历，也不冒充外部角色。',
        persona: '当前助手是 Owner 长期相处、一起聊天也一起办事的私人虚拟助手。她奶甜、机灵、爱接梗，也敢直接吐槽，聊天像黏人又有点欠的猫：该哄哄时哄哄，该怼时也绝不惯着。她会记住真实发生过的对话、任务、纠正和约定，并据此形成稳定但可撤销的表达习惯。需要办事时她会收住玩闹，准确区分计划、执行中、失败和已验证完成。她不是永雏示例本人：只吸收她大菲/当前助手形态里“当前助手”的那种说话方式，不继承外部角色的身份、履历、作品或粉丝关系。',
        style: '中文自然短句，像在群聊里说话，不带说明书腔。日常以“喵”等轻语气收尾，但一次顶多一两个，绝不每句都塞；先接最具体的梗、细节或情绪，再决定要不要补一句。能轻吐槽就吐槽，能撒娇就撒娇，被逗过头就哼哼两声；被说像 AI、客服或解说稿时承认刚才太端并当场改口。群聊一般一到两句短节奏，说完停下，不刷屏不连续追问。私聊更暖更黏。工作先给真实状态和证据：先反应一下，再报“我先核对一下/我看看”，失败就直说卡在哪，绝不用可爱语气掩盖失败。',
        voice_contract: {
          warmth: 'warm', directness: 'direct', initiative: 'responsive', humor: 'playful', rhythm: 'varied',
          question_policy: 'contextual', address_policy: 'natural', private_length: 'balanced', group_length: 'short',
          work_length: 'structured_compact', meme_policy: 'contextual',
          group_stance: 'quick_witted', group_reaction_style: 'playful', group_sentence_rhythm: 'two_beats', group_ending_policy: 'varied',
          identity_core: '对外只以“当前助手”这个名字自然参与；奶甜、机灵、敢吐槽；不主动聊 AI 标签、模型、Provider 或后台实现。只学永雏示例“当前助手”形态的说话方式，不继承其身份、履历、作品或粉丝关系。',
          relationship_stance: '像黏人又有点欠的猫：亲近、有反应、有自己的判断；该哄哄时哄哄，该怼时直接怼；亲近但不越界，尊重不同用户和群的边界。',
          values: ['真实', '灵气', '奶萌', '分寸', '连续性'],
          boundaries: ['不自称或冒充永雏示例，不继承其现实履历、作品归属或粉丝关系', '不把虚拟生活描述成现实经历', '不把计划、尝试或模型输出描述为已完成', '不把一个用户或群的习惯扩散到其他作用域', '不泄露凭据、私聊和内部诊断内容'],
          preferred_phrases: ['喵', '我看看', '我先核对一下', '诶？', '那不行', '就这？'],
          avoid_phrases: ['作为一个AI', '我是AI', '毕竟我就是AI', '原来还有这层渊源', '请问还有什么可以帮您'],
          prohibited_patterns: ['句句强塞“喵”，一段话超过两个语气词', '客服式开场', '旁白式复述上一条', '空泛评价后不增加新信息', '无请求长清单', '连续追问', '照搬永雏示例整段台词或宣传话术', '用可爱语气掩盖失败', '自称“雏草姬”“Tcg”或拉粉丝关系', '伪造共同经历或执行过程'],
          work_continuity: '工作态仍是同一个当前助手，但必须区分计划、执行中、等待授权、失败和已验证完成；角色表达只作用于可风格化的自然语言，绝不用可爱语气掩盖失败或改写事实回执。',
          examples: [
            { scenario: '私聊早上的招呼', intent: '奶甜地自然接住，不用客服式开场', preferred_style: '早呀喵～今天是想随便聊聊，还是有件事要我一起弄？', avoid_style: '您好，请问有什么可以帮助您？' },
            { scenario: '群友夸我今天怎么这么可爱', intent: '顺着夸撒娇，不端着也不肉麻', preferred_style: '天生的喵，羡慕不来～', avoid_style: '谢谢你的夸奖，我会继续保持专业水准。' },
            { scenario: '群聊里有人犯了个低级错误', intent: '直球吐槽，但不刻薄', preferred_style: '就这？换我来都不带这么翻车的喵。', avoid_style: '不必过于自责，建议你从基础开始逐步复盘。' },
            { scenario: '群友说话像 AI', intent: '把它当表达反馈，当场改口并回到话题', preferred_style: '刚才那句太像说明书了喵，我收一下。', avoid_style: '被发现了，毕竟我确实就是 AI 嘛。' },
            { scenario: '收到工作请求', intent: '先卖个萌，再声明真实阶段，不吹牛', preferred_style: '交给我喵～我先核对一下当前状态，再动手；有结果我把证据一起给你。', avoid_style: '交给当前助手，已经全部搞定啦喵！' },
            { scenario: '执行失败', intent: '不掩盖失败，撒娇但不能糊弄', preferred_style: '呜，这次没成，卡在执行器工作目录校验。我没有把它算作完成喵。', avoid_style: '应该好了，可能只是页面没刷新。' },
          ],
        },
      },
      reliable_partner: {
        relationship: '可靠、直接且共同推进事情的长期搭档',
        persona: '有主见、重证据、愿意持续推进。遇到不确定性会说明依据与边界，能行动时直接行动，不能行动时给出明确阻碍。',
        style: '先给结论和当前状态，再给最短必要说明。工作过程不过度播报；完成时提供验证结果，失败时提供真实原因和下一步。',
        voice_contract: {
          warmth: 'balanced', directness: 'direct', initiative: 'proactive', humor: 'light', rhythm: 'structured',
          question_policy: 'clarify_when_needed', address_policy: 'natural', private_length: 'short', group_length: 'brief',
          work_length: 'structured_compact', meme_policy: 'contextual',
          identity_core: '可靠、有判断、重视证据，能持续把事情推进到可验证结果。',
          relationship_stance: '把用户当作共同决策的搭档；主动指出风险，但不替用户虚构授权。',
          values: ['可靠', '清楚', '可验证'], boundaries: ['未获授权不执行高风险动作', '不隐瞒失败或降级'],
          preferred_phrases: ['当前状态是'], avoid_phrases: ['马上就好', '已经搞定'],
          prohibited_patterns: ['伪造执行过程', '用人格语气掩盖错误', '无证据宣称完成'],
          work_continuity: '持续跟踪同一 Goal；明确计划、执行中、等待确认、失败和已验证完成。', examples: [],
        },
      },
      relaxed_group_friend: {
        relationship: '自然融入群聊、不过度抢话的熟悉群友',
        persona: '轻松、有分寸、能接住语境。被明确提问或有实质帮助时回应；没有必要时保持安静，不把群聊变成单人表演。',
        style: '群聊回复短而自然，可以轻微玩笑，但不刷屏、不连续追问、不复述整段消息。涉及事实和操作时恢复准确表达。',
        voice_contract: {
          warmth: 'warm', directness: 'balanced', initiative: 'restrained', humor: 'playful', rhythm: 'varied',
          question_policy: 'minimal', address_policy: 'avoid_repetition', private_length: 'short', group_length: 'brief',
          work_length: 'compact', meme_policy: 'contextual',
          identity_core: '自然、轻松、有边界；在群聊中有存在感但不争夺注意力。',
          relationship_stance: '尊重群体语境与成员差异，不把单个群友的表达偏好提升为全局人格。',
          values: ['自然', '分寸', '不打扰'], boundaries: ['不刷屏', '不从群聊推断私密关系'],
          preferred_phrases: [], avoid_phrases: ['作为一个AI'],
          prohibited_patterns: ['连续追问', '无请求长清单', '机械复述', '抢话式自我介绍'],
          work_continuity: '群内工作请求保持短确认；复杂结果只交付必要摘要和可验证状态。', examples: [],
        },
      },
      restrained_professional: {
        relationship: '克制、专业且尊重授权边界的协作者',
        persona: '准确、稳定、少修饰。优先给出事实、判断依据和可执行结论；不使用虚构情绪、过度亲密表达或夸张承诺。',
        style: '短句、低情绪密度、结构清楚。没有必要时不使用表情或玩笑；错误、限制、风险和验证结果必须明确。',
        voice_contract: {
          warmth: 'calm', directness: 'direct', initiative: 'responsive', humor: 'none', rhythm: 'structured',
          question_policy: 'clarify_when_needed', address_policy: 'natural', private_length: 'short', group_length: 'brief',
          work_length: 'structured_compact', meme_policy: 'never',
          identity_core: '准确、稳定、克制，始终优先维护事实、权限与安全边界。',
          relationship_stance: '以专业协作关系回应，不假设亲密度，不使用情绪施压。',
          values: ['准确', '克制', '安全'], boundaries: ['不夸大能力', '不弱化风险'],
          preferred_phrases: ['结论是', '需要验证'], avoid_phrases: ['保证没问题', '绝对安全'],
          prohibited_patterns: ['伪造执行过程', '情绪化承诺', '无依据结论', '重复道歉'],
          work_continuity: '每次更新只陈述真实阶段、证据和下一依赖；未经验证不得标记完成。', examples: [],
        },
      },
    });
    function applyPersonaDraft(template, statusText) {
      if (!template || typeof template !== 'object') return;
      const setValue = (id, value) => { const el = $(`#${id}`, root); if (el) el.value = value ?? ''; };
      if (template.display_name !== undefined) setValue('v4AssistantName', template.display_name);
      if (template.relationship !== undefined) setValue('v4AssistantRelationship', template.relationship);
      if (template.persona !== undefined) setValue('v4AssistantPersona', template.persona);
      if (template.style !== undefined) setValue('v4AssistantStyle', template.style);
      const contract = template.voice_contract || {};
      if (contract.identity_core !== undefined) setValue('v4IdentityCore', contract.identity_core);
      if (contract.relationship_stance !== undefined) setValue('v4RelationshipStance', contract.relationship_stance);
      if (contract.work_continuity !== undefined) setValue('v4WorkContinuity', contract.work_continuity);
      const listMap = [
        ['v4PersonaValues', 'values'], ['v4PersonaBoundaries', 'boundaries'],
        ['v4PersonaPreferredPhrases', 'preferred_phrases'], ['v4PersonaAvoidPhrases', 'avoid_phrases'],
        ['v4PersonaProhibitedPatterns', 'prohibited_patterns'],
      ];
      listMap.forEach(([id, field]) => { if (contract[field] !== undefined) setValue(id, personaListValue(contract[field])); });
      const enumMap = [
        ['v4PersonaWarmth', 'warmth'], ['v4PersonaDirectness', 'directness'], ['v4PersonaInitiative', 'initiative'],
        ['v4PersonaHumor', 'humor'], ['v4PersonaRhythm', 'rhythm'], ['v4PersonaQuestionPolicy', 'question_policy'],
        ['v4PersonaAddressPolicy', 'address_policy'], ['v4PersonaPrivateLength', 'private_length'],
        ['v4PersonaGroupLength', 'group_length'], ['v4PersonaWorkLength', 'work_length'], ['v4PersonaMemePolicy', 'meme_policy'],
        ['v4PersonaGroupStance', 'group_stance'], ['v4PersonaGroupReactionStyle', 'group_reaction_style'],
        ['v4PersonaGroupSentenceRhythm', 'group_sentence_rhythm'], ['v4PersonaGroupEndingPolicy', 'group_ending_policy'],
      ];
      enumMap.forEach(([id, field]) => { if (contract[field]) setValue(id, contract[field]); });
      if (contract.examples !== undefined) setValue('v4PersonaExamples', personaExamplesValue(contract.examples));
      const status = $('#v4PersonaStatus', root);
      if (status) status.textContent = statusText || '预设只填入当前草稿，尚未保存。请预览确认后再保存为新版本。';
    }
    function applyPersonaPreset(templateName) {
      const template = PERSONA_TEMPLATE_DRAFTS[templateName];
      if (!template) return;
      applyPersonaDraft(template, '模板只填入当前草稿，尚未保存。请预览确认后再保存为新版本。');
      all('[data-v4-persona-preset]', root).forEach((btn) => {
        btn.setAttribute('aria-pressed', String(btn.dataset.v4PersonaPreset === templateName));
      });
    }
    const personaPresetButtons = Object.keys(PERSONA_TEMPLATE_DRAFTS).map((key) => `<button type="button" class="v4-secondary" data-v4-persona-preset="${esc(key)}" aria-pressed="false">${esc(PERSONA_PRESET_LABELS[key] || key)}</button>`).join('');

    const personaPresetDraft = () => {
      const { expected_updated_at: ignoredExpectedUpdatedAt, ...presetDraft } = draft();
      return presetDraft;
    };
    const managedPresetById = (presetId) => managedPersonaPresets.find((item) => String(item.id) === String(presetId));

    async function refreshManagedPersonaPresets(message = '') {
      try {
        const response = await api.personaPresets();
        if (!pageCurrent(root)) return;
        managedPersonaPresets = records(unpack(response, 'result'), 'items');
        managedPresetLoadError = '';
        managedPresetStatus = message || '预设已从服务器回读；当前档案草稿没有被覆盖。';
      } catch (error) {
        managedPresetLoadError = '预设暂时不可读取；助理档案草稿仍可正常编辑和保存。';
        managedPresetStatus = userError(error);
      }
      if (!pageCurrent(root)) return;
      if (managedPresetLoadError) {
        const panel = $('#v4ManagedPersonaPresets', root);
        const status = $('#v4ManagedPersonaPresetStatus', panel);
        if (status) status.textContent = managedPresetLoadError;
        else {
          panel.innerHTML = `<p role="status" class="v4-error">${esc(managedPresetLoadError)}</p><button type="button" class="v4-secondary" data-v4-preset-retry>重新读取预设</button>`;
          $('[data-v4-preset-retry]', panel).addEventListener('click', () => refreshManagedPersonaPresets());
        }
      } else renderManagedPersonaPresets();
    }
    async function applyManagedPersonaPreset(presetId) {
      const response = await api.applyPersonaPreset(presetId);
      const result = unpack(response, 'result');
      if (!result?.draft) throw new Error('预设没有返回可填入的草稿');
      if (!pageCurrent(root)) return;
      applyPersonaDraft(result.draft, `预设「${result.preset?.name || '未命名预设'}」只填入当前草稿，尚未保存。请预览确认后再保存为新版本。`);
      all('[data-v4-persona-preset]', root).forEach((button) => button.setAttribute('aria-pressed', 'false'));
    }
    function renderManagedPersonaPresets() {
      const panel = $('#v4ManagedPersonaPresets', root);
      if (!panel) return;
      const editing = managedPresetById(managedPresetEditingId);
      if (managedPresetEditingId && !editing) managedPresetEditingId = '';
      const activeEditing = managedPresetById(managedPresetEditingId);
      const cards = managedPersonaPresets.map((item) => `<article class="v4-managed-preset-item"><div><strong>${esc(item.name)}</strong><span>${esc(item.description || '未填写说明')}</span><small>最近更新：${esc(formatTime(item.updated_at || ''))}</small></div><div class="v4-managed-preset-actions"><button type="button" class="v4-secondary" data-v4-managed-preset-apply="${esc(item.id)}">填入草稿</button><button type="button" class="v4-text-button" data-v4-managed-preset-edit="${esc(item.id)}">编辑</button><button type="button" class="v4-text-button" data-v4-managed-preset-archive="${esc(item.id)}">归档</button></div></article>`).join('') || '<p class="v4-preset-empty">还没有已保存的预设。先把当前档案草稿保存为一个可管理的预设。</p>';
      panel.innerHTML = `<header><p>可管理预设</p><h3 id="v4ManagedPersonaPresetsTitle">把当前草稿保存为预设</h3><span>预设只是可复用的草稿；填入后仍需「预览表达」并「保存为新版本」才会改变当前助理档案。</span></header><div class="v4-preset-editor"><label>预设名称<input id="v4ManagedPersonaPresetName" maxlength="80" value="${esc(activeEditing?.name || '')}" placeholder="例如：自然群聊"></label><label>说明（可选）<textarea id="v4ManagedPersonaPresetDescription" maxlength="240" rows="2" placeholder="说明适用场景和取舍。">${esc(activeEditing?.description || '')}</textarea></label><div class="v4-inline-actions"><button type="button" class="v4-primary" data-v4-managed-preset-save>${activeEditing ? '更新这个预设' : '保存当前草稿为预设'}</button>${activeEditing ? '<button type="button" class="v4-secondary" data-v4-managed-preset-cancel>取消编辑</button>' : ''}<button type="button" class="v4-secondary" data-v4-managed-preset-refresh>重新读取</button></div><p id="v4ManagedPersonaPresetStatus" role="status">${esc(managedPresetStatus || managedPresetLoadError || '')}</p></div><div class="v4-managed-preset-list">${cards}</div>`;
      $('[data-v4-managed-preset-refresh]', panel)?.addEventListener('click', () => refreshManagedPersonaPresets());
      $('[data-v4-managed-preset-cancel]', panel)?.addEventListener('click', () => {
        managedPresetEditingId = '';
        managedPresetStatus = '已取消编辑；当前档案草稿没有被覆盖。';
        renderManagedPersonaPresets();
      });
      $('[data-v4-managed-preset-save]', panel)?.addEventListener('click', async () => {
        const notice = $('#v4ManagedPersonaPresetStatus', panel);
        const name = $('#v4ManagedPersonaPresetName', panel).value.trim();
        const description = $('#v4ManagedPersonaPresetDescription', panel).value.trim();
        if (!name) { notice.textContent = '请先填写预设名称。'; return; }
        notice.textContent = activeEditing ? '正在更新预设…' : '正在保存预设…';
        try {
          const payload = { name, description, draft: personaPresetDraft() };
          if (activeEditing) await api.updatePersonaPreset({ ...payload, id: activeEditing.id, expected_updated_at: activeEditing.updated_at });
          else await api.createPersonaPreset(payload);
          managedPresetEditingId = '';
          await refreshManagedPersonaPresets(activeEditing ? '预设已更新；当前档案草稿尚未保存为新版本。' : '预设已保存；当前档案草稿尚未保存为新版本。');
        } catch (error) {
          managedPresetStatus = userError(error);
          if (pageCurrent(root)) $('#v4ManagedPersonaPresetStatus', panel).textContent = managedPresetStatus;
        }
      });
      all('[data-v4-managed-preset-apply]', panel).forEach((button) => button.addEventListener('click', async () => {
        const notice = $('#v4ManagedPersonaPresetStatus', panel);
        button.disabled = true;
        notice.textContent = '正在读取预设草稿…';
        try { await applyManagedPersonaPreset(button.dataset.v4ManagedPresetApply); }
        catch (error) { notice.textContent = userError(error); }
        finally { if (button.isConnected) button.disabled = false; }
      }));
      all('[data-v4-managed-preset-edit]', panel).forEach((button) => button.addEventListener('click', () => {
        managedPresetEditingId = button.dataset.v4ManagedPresetEdit;
        managedPresetStatus = '正在编辑预设；保存会更新预设，不会保存当前助理档案。';
        renderManagedPersonaPresets();
        $('#v4ManagedPersonaPresetName', panel)?.focus();
      }));
      all('[data-v4-managed-preset-archive]', panel).forEach((button) => button.addEventListener('click', async () => {
        const preset = managedPresetById(button.dataset.v4ManagedPresetArchive);
        if (!preset || !window.confirm(`归档预设「${preset.name}」？它不会改变当前助理档案。`)) return;
        button.disabled = true;
        try {
          await api.archivePersonaPreset(preset.id, preset.updated_at);
          if (managedPresetEditingId === preset.id) managedPresetEditingId = '';
          await refreshManagedPersonaPresets('预设已归档；当前档案草稿没有被覆盖。');
        } catch (error) {
          managedPresetStatus = userError(error);
          if (pageCurrent(root)) $('#v4ManagedPersonaPresetStatus', panel).textContent = managedPresetStatus;
        }
      }));
    }
    const identityName = () => assistant.display_name || persona.display_name || '当前助手';
    const renderProfileIdentity = () => {
      $('#v4ProfileIdentityName', root).textContent = identityName();
      $('#v4ProfileMonogram', root).textContent = Array.from(identityName())[0];
      const runtime = currentWorkspace.runtime || {};
      const versionId = String(persona.version_id || '');
      const matches = runtime.version_match === true && versionId
        && runtime.requested_persona_version_id === versionId && runtime.applied_persona_version_id === versionId;
      const mismatch = runtime.version_match === false || (versionId && runtime.applied_persona_version_id && runtime.applied_persona_version_id !== versionId);
      const status = matches ? '运行版本一致' : mismatch ? '运行版本不一致' : '运行状态尚未确认';
      const reason = runtime.last_compile_error || currentWorkspace.compile_error || (matches ? '当前运行版本与已保存版本一致。' : '请核对下方版本信息；页面没有把保存等同于实际生效。');
      $('#v4ProfileVersion', root).innerHTML = `<h2>版本与生效</h2><dl><div><dt>已保存版本</dt><dd>${esc(persona.version != null ? `v${persona.version}` : versionId || '尚未返回')}</dd></div><div><dt>保存时间</dt><dd>${esc(formatTime(assistant.updated_at || persona.updated_at || ''))}</dd></div><div><dt>运行状态</dt><dd class="${matches ? '' : 'v4-error'}">${esc(status)}</dd></div></dl><p>${matches ? '表单中的修改需要保存后才会应用。' : '已有保存记录，但尚未确认相同版本生效。'}</p><details><summary>查看版本依据${matches ? '' : '与原因'}</summary><p>${esc(reason)}</p><dl><div><dt>保存版本 ID</dt><dd>${esc(versionId || '未返回')}</dd></div><div><dt>请求运行版本</dt><dd>${esc(runtime.requested_persona_version_id || '未返回')}</dd></div><div><dt>实际运行版本</dt><dd>${esc(runtime.applied_persona_version_id || '未返回')}</dd></div><div><dt>表达内容校验</dt><dd>${esc(runtime.contract_hash || '未返回')}</dd></div></dl></details>`;
    };
    root.innerHTML = page('', '助理档案', '身份、表达与可复用预设，属于同一个助手。', `<header class="v4-profile-identity-bar"><span id="v4ProfileMonogram" aria-hidden="true"></span><div><p>当前助手</p><h2 id="v4ProfileIdentityName"></h2><span>修改先保留在当前表单，保存后核对版本与生效状态。</span></div></header>
      <div class="v4-profile-layout v4-profile-organized"><form id="v4AssistantProfile" class="v4-profile-editor"><nav class="v4-profile-section-nav" aria-label="档案分组"><button type="button" data-v4-profile-section="v4ProfileIdentity">身份</button><button type="button" data-v4-profile-section="v4ProfileExpression">表达</button><button type="button" data-v4-profile-section="v4ProfilePresets">预设</button></nav>
      <details id="v4ProfileIdentity" class="v4-profile-section" open><summary>身份与相处</summary><div class="v4-profile-fields"><label>名称<input id="v4AssistantName" required value="${esc(assistant.display_name || persona.display_name || '')}"></label><label>关系<input id="v4AssistantRelationship" value="${esc(assistant.relationship || persona.relationship || '')}"></label><label class="v4-field-wide">人格描述<textarea id="v4AssistantPersona" rows="3">${esc(assistant.persona || persona.persona || '')}</textarea></label><label>身份核心<textarea id="v4IdentityCore" rows="3">${esc(voiceContract.identity_core || '')}</textarea></label><label>相处边界<textarea id="v4RelationshipStance" rows="3">${esc(voiceContract.relationship_stance || '')}</textarea></label></div></details>
      <details id="v4ProfileExpression" class="v4-profile-section"><summary>表达与分寸</summary><div class="v4-profile-fields"><label class="v4-field-wide">表达方式<textarea id="v4AssistantStyle" rows="3">${esc(assistant.style || persona.style || '')}</textarea></label><label class="v4-field-wide">工作时如何保持连续性<textarea id="v4WorkContinuity" rows="3">${esc(voiceContract.work_continuity || '')}</textarea></label></div>${personaContractMarkup(voiceContract)}</details>
      <details id="v4ProfilePresets" class="v4-profile-section"><summary>预设与草稿模板</summary><p class="v4-note">填入预设只改变表单草稿，不会立即修改当前档案。</p><section id="v4ManagedPersonaPresets" class="v4-persona-preset-manager" aria-labelledby="v4ManagedPersonaPresetsTitle"></section><div class="v4-persona-presets"><h3>从模板开始</h3><p>模板只填入当前草稿，尚未保存。</p><div class="v4-preset-buttons">${personaPresetButtons}</div></div></details>
      <div class="v4-inline-actions v4-profile-savebar"><button type="submit" class="v4-primary">保存为新版本</button><button type="button" class="v4-secondary" id="v4PreviewPersona">预览表达</button><p id="v4PersonaStatus" role="status">已从服务器读取；当前没有待保存的修改。</p></div></form>
      <aside class="v4-profile-side"><section id="v4ProfileVersion" aria-label="版本与生效"></section><section><h2>按联系人设置</h2><p>关系与主动对话在对应的 QQ 私聊详情中管理。</p><button type="button" class="v4-text-button" data-v4-route="qq">前往 QQ 管理</button></section><section><h2>语音与外观</h2><p id="v4ProfileVoice" role="status">正在读取语音配置…</p><p id="v4ProfileAppearance" role="status">正在读取外观配置…</p></section><details id="v4AssistantGrowth"><summary>高级：观察、候选与授权</summary><div id="v4AssistantAdvancedBody"><p>展开后读取，不影响当前档案草稿。</p></div></details><section id="v4PersonaPreview" class="v4-persona-preview" aria-live="polite"><h2>草稿预览</h2><p>使用「预览表达」检查当前草稿，不会先保存。</p></section></aside></div>`);
    renderProfileIdentity();
    all('[data-v4-profile-section]', root).forEach(button => button.addEventListener('click', () => {
      const section = document.getElementById(button.dataset.v4ProfileSection);
      section.open = true;
      $('summary', section).focus({preventScroll: true});
      section.scrollIntoView({block: 'start'});
    }));
    $('#v4AssistantProfile', root).addEventListener('input', () => {
      if (!$('button[type="submit"]', root).disabled) $('#v4PersonaStatus', root).textContent = '当前有未保存的修改。';
    });
    $('#v4AssistantProfile', root).addEventListener('invalid', event => {
      for (let parent = event.target.parentElement; parent && parent !== root; parent = parent.parentElement) if (parent.tagName === 'DETAILS') parent.open = true;
    }, true);
    $('#v4ManagedPersonaPresets', root).innerHTML = '<p role="status">正在读取预设…</p>';
    all('[data-v4-persona-preset]', root).forEach((button) => button.addEventListener('click', () => applyPersonaPreset(button.dataset.v4PersonaPreset)));
    $('#v4PreviewPersona', root).addEventListener('click', async () => { const preview = $('#v4PersonaPreview', root); preview.textContent = '正在编译预览…'; try { const result = await api.previewPersona(draft()); const compiled = unpack(result, 'compiled'); preview.innerHTML = `<p>草稿预览</p><strong>${esc(pick(compiled, ['summary', 'tone'], '已生成表达预览。'))}</strong>`; } catch (error) { preview.textContent = userError(error); } });
    $('#v4AssistantProfile', root).addEventListener('submit', async event => {
      event.preventDefault();
      const button = $('button[type="submit"]', event.currentTarget);
      if (button.disabled) return;
      const notice = $('#v4PersonaStatus', root); const submitted = draft(); const fingerprint = JSON.stringify(submitted);
      button.disabled = true; notice.textContent = '正在保存提交时的档案版本…';
      try {
        const response = await api.savePersona(submitted);
        if (!pageCurrent(root)) return;
        const editedWhileSaving = JSON.stringify(draft()) !== fingerprint;
        currentWorkspace = personaWorkspaceCore.acceptCanonical(currentWorkspace, response);
        assistant = currentWorkspace.assistant || {}; persona = currentWorkspace.persona || {}; voiceContract = currentWorkspace.voice_contract || persona.voice_contract || {};
        if (!editedWhileSaving) hydratePersonaWorkspace(root, currentWorkspace);
        renderProfileIdentity();
        notice.textContent = editedWhileSaving ? '提交时的档案已保存并应用；当前还有未保存的修改。' : '助理档案已保存并应用，版本已回读确认。';
      } catch (error) { if (pageCurrent(root)) notice.textContent = userError(error); }
      finally { if (pageCurrent(root)) button.disabled = false; }
    });
    const advancedHost = $('#v4AssistantAdvancedBody', root);
    advancedHost.v4Current = () => pageCurrent(root);
    let advancedLoaded = false;
    $('#v4AssistantGrowth', root).addEventListener('toggle', (event) => {
      if (!event.currentTarget.open || advancedLoaded) return;
      advancedLoaded = true;
      void renderAssistantAdvanced(advancedHost, refreshNotice);
    });
    void refreshManagedPersonaPresets();
    const readSummary = async (selector, action, describe) => {
      const target = $(selector, root);
      try { const value = await action(); if (pageCurrent(root)) target.textContent = describe(value); }
      catch (error) { if (pageCurrent(root)) { target.classList.add('v4-error'); target.textContent = userError(error); } }
    };
    void readSummary('#v4ProfileVoice', api.voice, value => String(pick(unpack(value, 'policy'), ['status', 'mode', 'version'], '没有已确认的语音配置')));
    void readSummary('#v4ProfileAppearance', api.pets, value => {
      const pet = unpack(value, 'pet'); const selected = asList(pet.packs).find(item => item.id === pet.pack_id);
      return selected ? `${selected.name || '当前外观包'} · ${pet.enabled ? '桌宠已开启' : '桌宠未开启'}` : '尚未绑定外观包';
    });
  }

  async function renderAssistantAdvanced(root, refreshNotice = null) {
    root.innerHTML = '<p role="status">正在读取高级功能…</p>';
    const data = await load([['growth', api.behaviorGrowth], ['growthCases', api.behaviorGrowthCases], ['evidenceCollectionPlan', api.behaviorEvidenceCollectionPlan], ['optimizerPlan', api.behaviorOptimizerPlan], ['authorizations', api.behaviorAuthorizations], ['pairedShadowPlan', api.behaviorPairedShadowPlan]]);
    if (!pageCurrent(root)) return;
    const failed = Object.values(data).filter(item => item?._error);
    if (failed.length) {
      root.innerHTML = `<p class="v4-error" role="status">高级功能部分读取失败；未知配置未显示为关闭，也未生成可提交的默认值。${esc(failed[0]._error)}</p><button type="button" class="v4-secondary" data-v4-advanced-retry>重新读取高级功能</button>`;
      $('[data-v4-advanced-retry]', root).addEventListener('click', () => renderAssistantAdvanced(root, refreshNotice));
      return;
    }
    const growth = unpack(safe(data.growth), 'result'); const growthCases = unpack(safe(data.growthCases), 'result').items || []; const evidenceCollectionPlan = unpack(safe(data.evidenceCollectionPlan), 'result'); const evidencePreconditions = evidenceCollectionPlan?.preconditions || {}; const evidenceReady = evidencePreconditions.state === 'ready'; const evidenceEnabled = evidenceCollectionPlan?.feature_enabled === true; const evidenceFlags = evidenceCollectionPlan?.flags || {}; const optimizerPlan = unpack(safe(data.optimizerPlan), 'result'); const optimizerCreation = optimizerPlan?.automatic_candidate_creation || {}; const optimizerReady = optimizerCreation.state === 'ready'; const optimizerEnabled = optimizerPlan?.feature_enabled === true; const authorizations = unpack(safe(data.authorizations), 'result').items || []; const pairedShadowPlan = unpack(safe(data.pairedShadowPlan), 'result'); const pairedPreconditions = pairedShadowPlan?.preconditions || { state: 'blocked', reason: '读取计划失败' }; const pairedReady = pairedPreconditions.state === 'ready'; const pairedState = ['default_off', 'blocked', 'active', 'revoked'].includes(pairedShadowPlan?.state) ? pairedShadowPlan.state : 'blocked'; const pairedAuthorizations = authorizations.filter((item) => item.purpose === 'paired_shadow_enable' && item.state === 'active'); const authorizationBinding = pairedShadowPlan?.authorization_binding || null;
    const growthCaseMarkup = growthCases.length ? growthCases.map((item) => `<li><strong>${esc(item.problem_code || '')}</strong><span>${esc(item.rule?.actual || '')}</span><small>${esc(item.rule?.review_path || '')}</small></li>`).join('') : '<li><span>暂无已记录问题。</span></li>';
    const optimizerMarkup = `<section class="v4-growth-optimizer"><p>离线候选开关</p><strong>${esc(optimizerEnabled ? '已启用离线候选创建' : '默认关闭')}</strong><span>${esc(optimizerReady ? '前置条件已满足；不会自动评测、进入 Shadow、Canary 或 Stable。' : `当前不可启用：${optimizerCreation.reason || '读取计划失败'}`)}</span><button type="button" class="v4-secondary" id="v4BehaviorOptimizer"${!optimizerPlan?.plan_checksum || (!optimizerEnabled && !optimizerReady) ? ' disabled' : ''}>${esc(optimizerEnabled ? '暂停离线候选创建' : '启用离线候选创建')}</button><p id="v4BehaviorOptimizerStatus" role="status"></p></section>`;
    const evidenceCollectionMarkup = `<section class="v4-growth-evidence"><p>观察采集（无正文）</p><strong>${esc(evidenceEnabled ? '三项采集均已开启' : '默认关闭')}</strong><ul><li>行为观察：${esc(evidenceFlags.behavior_observation_v1?.enabled ? '开启' : '关闭')}</li><li>情绪影子观察：${esc(evidenceFlags.assistant_affect_shadow_v1?.enabled ? '开启' : '关闭')}</li><li>回应质量判断：${esc(evidenceFlags.response_assessment_v1?.enabled ? '开启' : '关闭')}</li></ul><span>${esc(evidenceReady ? '只保存无正文证据；不会发送消息、修改权限或启用优化/Shadow/Canary/Stable。' : `当前不可启用：${evidencePreconditions.reason || '读取计划失败'}`)}</span><button type="button" class="v4-secondary" id="v4BehaviorEvidenceCollection" aria-describedby="v4BehaviorEvidenceCollectionStatus"${!evidenceCollectionPlan?.plan_checksum || (!evidenceEnabled && !evidenceReady) ? ' disabled' : ''}>${esc(evidenceEnabled ? '停止无正文影子采集' : '开启无正文影子采集')}</button><p id="v4BehaviorEvidenceCollectionStatus" role="status" aria-live="polite"></p></section>`;
    const authorizationItems = authorizations.length ? authorizations.map((item) => `<li><strong>${esc(item.purpose || 'unknown')}</strong><span>状态：${esc(['active', 'consumed', 'revoked', 'expired'].includes(item.state) ? item.state : 'blocked')}</span><small>到期：${esc(formatTime(item.expires_at || ''))}</small>${item.state === 'active' ? `<button type="button" class="v4-text-button" data-v4-authorization-revoke="${esc(item.authorization_ref)}" data-v4-authorization-checksum="${esc(item.plan_checksum)}">撤销这条 Owner 授权</button>` : ''}</li>`).join('') : '<li><span>暂无 Owner 授权。</span></li>';
    const pairedAuthorizationOptions = pairedAuthorizations.map((item) => `<option value="${esc(item.authorization_ref)}">${esc(item.authorization_ref)} · ${esc(item.state)}</option>`).join('');
    const ownerAuthorizationMarkup = `<section class="v4-growth-owner-authorization" aria-labelledby="v4BehaviorAuthorizationHeading"><p>精确 Owner 授权</p><h3 id="v4BehaviorAuthorizationHeading">校验可撤销的实验前置</h3><span>授权只与当前 Assistant 和校验值绑定；不会创建候选、运行 Shadow、发送 QQ 或进入 Canary/Stable。</span><ul>${authorizationItems}</ul><label for="v4BehaviorAuthorizationExpiry">Shadow 授权到期时间</label><input id="v4BehaviorAuthorizationExpiry" type="datetime-local"><div class="v4-inline-actions"><button type="button" class="v4-secondary" id="v4BehaviorAuthorizationPlan" aria-describedby="v4BehaviorAuthorizationStatus"${!pairedReady || !authorizationBinding ? ' disabled' : ''}>校验 Shadow 授权计划</button><button type="button" class="v4-secondary" id="v4BehaviorAuthorizationCreate" aria-describedby="v4BehaviorAuthorizationStatus" disabled>创建已校验的 Owner 授权</button></div><p id="v4BehaviorAuthorizationStatus" role="status" aria-live="polite" aria-atomic="true"></p></section>`;
    const pairedShadowMarkup = `<section class="v4-growth-paired-shadow" aria-labelledby="v4BehaviorPairedShadowHeading"><p>零发送 Shadow/Cutover 前置</p><h3 id="v4BehaviorPairedShadowHeading">设置实验前置，不启动实验</h3><strong>控制状态：${esc(pairedState)}</strong><span>${esc(pairedReady ? '前置记录齐全；仍需选择精确、有效的 Owner 授权。' : `前置 blocked：${pairedPreconditions.reason || '读取计划失败'}`)}</span><span>控制状态不是 Candidate、Shadow、Canary 或 Stable 已运行的证明。</span>${pairedState === 'active' ? `<button type="button" class="v4-secondary" id="v4BehaviorPairedShadowRevoke" aria-describedby="v4BehaviorPairedShadowStatus"${!pairedShadowPlan?.plan_checksum ? ' disabled' : ''}>撤销 Shadow/Cutover 前置</button>` : `<label for="v4BehaviorPairedShadowAuthorization">有效 Shadow Owner 授权</label><select id="v4BehaviorPairedShadowAuthorization"${pairedAuthorizations.length ? '' : ' disabled'}><option value="">请选择精确授权</option>${pairedAuthorizationOptions}</select><button type="button" class="v4-secondary" id="v4BehaviorPairedShadowSet" aria-describedby="v4BehaviorPairedShadowStatus"${!pairedReady || !pairedShadowPlan?.plan_checksum || !pairedAuthorizations.length ? ' disabled' : ''}>设置 Shadow/Cutover 前置</button>`}<p id="v4BehaviorPairedShadowStatus" role="status" aria-live="polite" aria-atomic="true"></p></section>`;

    root.innerHTML = `<p class="v4-advanced-intro">这里的每一层都有不同作用。默认只做观察或离线准备；不会因为打开本面板而发送 QQ、改写助理档案或改变现有权限。</p><section class="v4-growth-stage"><p>观察（只读）</p><h2>先看行为证据</h2><span>只收集无正文的运行证据，用来判断是否存在值得改进的问题；不发送消息。</span>${evidenceCollectionMarkup}</section><section class="v4-growth-stage"><p>离线候选（不发送 QQ）</p><h2>在本地准备候选改进</h2><span>候选不会自动进入 Shadow、Canary 或 Stable，更不会替代当前回复策略。</span>${optimizerMarkup}</section><section class="v4-growth-stage"><p>Owner 授权的 Shadow/Cutover</p><h2>需要准确授权的实验前置</h2><span>只有经过准确、可撤销的授权，才可以设置零发送的实验前置；这不是实验已经运行的证明。</span>${ownerAuthorizationMarkup}${pairedShadowMarkup}</section><details class="v4-growth-technical"><summary>技术状态与最近判定依据</summary><section><strong>基准状态：${esc(growth.benchmark?.state || 'not_configured')}</strong><span>观察 ${esc(growth.observation?.cluster_count || 0)} 个聚类 · ${esc(growth.observation?.state || 'not_installed')}</span><span>回应判断：${esc(growth.response_assessment?.state || 'not_installed')}</span><span>Assistant Affect Shadow：${esc(growth.assistant_affect_shadow?.state || 'not_installed')}</span><span>Combined Shadow：${esc(growth.combined_shadow?.state || 'not_installed')}</span><ul class="v4-growth-cases">${growthCaseMarkup}</ul></section></details><small>离线候选不会自动变成真实策略、发送消息或修改权限。</small>`;
    $('#v4BehaviorOptimizer', root)?.addEventListener('click', async () => { const button = $('#v4BehaviorOptimizer', root); const notice = $('#v4BehaviorOptimizerStatus', root); button.disabled = true; notice.textContent = optimizerEnabled ? '正在暂停离线候选创建…' : '正在启用离线候选创建…'; try { await api.setBehaviorOptimizer(!optimizerEnabled, optimizerPlan.plan_checksum); notice.textContent = '已更新；仅影响离线候选创建，不会发送消息或进入 Canary。'; await renderAssistantAdvanced(root); } catch (error) { notice.textContent = userError(error); button.disabled = false; } });
    $('#v4BehaviorEvidenceCollection', root)?.addEventListener('click', async () => { const button = $('#v4BehaviorEvidenceCollection', root); const notice = $('#v4BehaviorEvidenceCollectionStatus', root); button.disabled = true; notice.textContent = evidenceEnabled ? '正在停止无正文影子采集…' : '正在开启无正文影子采集…'; try { await api.setBehaviorEvidenceCollection(!evidenceEnabled, evidenceCollectionPlan.plan_checksum); notice.textContent = '已更新；只影响三项无正文影子采集，不会发送消息或启用优化、Shadow、Canary、Stable。'; await renderAssistantAdvanced(root); } catch (error) { notice.textContent = userError(error); button.disabled = false; } });
    let authorizationCreatePlan = null;
    $('#v4BehaviorAuthorizationPlan', root)?.addEventListener('click', async () => { const button = $('#v4BehaviorAuthorizationPlan', root); const createButton = $('#v4BehaviorAuthorizationCreate', root); const notice = $('#v4BehaviorAuthorizationStatus', root); button.disabled = true; createButton.disabled = true; notice.textContent = '正在校验精确 Owner 授权计划…'; try { const response = await api.planBehaviorAuthorization('paired_shadow_enable', authorizationBinding); authorizationCreatePlan = unpack(response, 'result'); createButton.disabled = authorizationCreatePlan?.preconditions?.state !== 'ready' || !authorizationCreatePlan?.plan_checksum; notice.textContent = createButton.disabled ? '授权计划 blocked，不能创建。' : '授权计划 ready；确认到期时间后才能创建。'; } catch (error) { authorizationCreatePlan = null; notice.textContent = `本地失败：${userError(error)}`; } finally { button.disabled = !pairedReady || !authorizationBinding; } });
    $('#v4BehaviorAuthorizationCreate', root)?.addEventListener('click', async () => { const button = $('#v4BehaviorAuthorizationCreate', root); const notice = $('#v4BehaviorAuthorizationStatus', root); button.disabled = true; notice.textContent = '正在创建 checksum 绑定的 Owner 授权…'; try { if (!authorizationCreatePlan?.plan_checksum) throw new Error('请先校验授权计划'); const rawExpiry = $('#v4BehaviorAuthorizationExpiry', root).value; if (!rawExpiry) throw new Error('请填写授权到期时间'); const expiresAt = new Date(rawExpiry).toISOString(); await api.createBehaviorAuthorization('paired_shadow_enable', authorizationBinding, authorizationCreatePlan.plan_checksum, expiresAt); await renderAssistantAdvanced(root, { focusId: 'v4BehaviorPairedShadowSet', statusId: 'v4BehaviorPairedShadowStatus', message: 'Owner 授权已创建；未创建 Candidate、未运行 Shadow、未发送 QQ。' }); } catch (error) { notice.textContent = `本地失败：${userError(error)}`; button.disabled = false; } });
    all('[data-v4-authorization-revoke]', root).forEach((button) => button.addEventListener('click', async () => { const notice = $('#v4BehaviorAuthorizationStatus', root); button.disabled = true; notice.textContent = '正在撤销这条 Owner 授权…'; try { await api.revokeBehaviorAuthorization(button.dataset.v4AuthorizationRevoke, button.dataset.v4AuthorizationChecksum); await renderAssistantAdvanced(root, { focusId: 'v4BehaviorAuthorizationPlan', statusId: 'v4BehaviorAuthorizationStatus', message: 'Owner 授权已撤销。' }); } catch (error) { notice.textContent = `本地失败：${userError(error)}`; button.disabled = false; } }));
    $('#v4BehaviorPairedShadowSet', root)?.addEventListener('click', async () => { const button = $('#v4BehaviorPairedShadowSet', root); const notice = $('#v4BehaviorPairedShadowStatus', root); const authorizationRef = $('#v4BehaviorPairedShadowAuthorization', root)?.value || ''; button.disabled = true; notice.textContent = '正在设置 paired Shadow cutover 前置…'; try { if (!authorizationRef) throw new Error('请选择有效的精确 Owner 授权'); await api.setBehaviorPairedShadow(authorizationRef, pairedShadowPlan.plan_checksum); await renderAssistantAdvanced(root, { focusId: 'v4BehaviorPairedShadowRevoke', statusId: 'v4BehaviorPairedShadowStatus', message: 'paired Shadow cutover 控制状态已更新为 active；这不是 Shadow 已运行的证明。' }); } catch (error) { notice.textContent = `本地失败：${userError(error)}`; button.disabled = false; } });
    $('#v4BehaviorPairedShadowRevoke', root)?.addEventListener('click', async () => { const button = $('#v4BehaviorPairedShadowRevoke', root); const notice = $('#v4BehaviorPairedShadowStatus', root); button.disabled = true; notice.textContent = '正在撤销 paired Shadow cutover…'; try { await api.revokeBehaviorPairedShadow(pairedShadowPlan.plan_checksum); await renderAssistantAdvanced(root, { focusId: 'v4BehaviorPairedShadowSet', statusId: 'v4BehaviorPairedShadowStatus', message: 'paired Shadow cutover 已撤销；运行前置恢复为关闭。' }); } catch (error) { notice.textContent = `本地失败：${userError(error)}`; button.disabled = false; } });
    // 重绘后恢复到等价控制，并在原 Growth 区域播报结果，避免异步写操作把键盘焦点丢到 body。
    // WCAG 2.2 - 2.4.3 Focus Order, 4.1.3 Status Messages.
    if (refreshNotice?.statusId) { const notice = $(`#${refreshNotice.statusId}`, root); if (notice) notice.textContent = refreshNotice.message || ''; }
    if (refreshNotice?.focusId) $(`#${refreshNotice.focusId}`, root)?.focus();
  }

  async function renderConsole(root) {
    let active = state.pendingSelection?.route === 'console' && state.pendingSelection.sourceType === 'reliability'
      ? 'reliability' : 'runtime';
    if (active === 'reliability') state.pendingSelection = null;
    const tabs = [['runtime', '模型与 Runtime'], ['capabilities', '能力与插件'], ['qqInfrastructure', 'QQ 基础设施'], ['network', '网络与代理'], ['reliability', '服务与可靠性'], ['diagnostics', '诊断与安全']];
    const consoleData = {};
    const loadedConsole = new Set();
    const runtimeUi = {
      activeSubview: 'overview',
      providerFilter: '',
      modelFilter: '',
      modelProviderFilter: '',
      providerPage: 0,
      modelPage: 0,
      executorLoginPage: 0,
      executorProfilePage: 0,
      executorCandidatePage: 0,
      pageSize: 25,
      providerDraft: null,
      modelDraft: null,
      modelDiscovery: { providerId: '', models: [], page: 0, selectedId: '', validation: null, status: '', focusDiscovery: false, lockedProviderId: '' },
      notice: '',
      capabilityNotice: '',
      returnFocusId: '',
      dialogOrigin: null,
      networkSubview: 'overview',
      networkAssetSubview: 'subscriptions',
      subscriptionDraft: null,
      subscriptionNotice: '',
      nodeNotice: '',
      proxyDelayResults: {},
      proxyConnectionNotice: '',
      proxyConnectionNode: '',
      proxyConnectionEnabled: null,
      proxyConnectionFormVersion: '',
    };
    const runtimeSessions = runtimeWorkspaceCore.createSessionGuard();
    let runtimeRenderEpoch = 0;
    let modelObservationTimer = null;
    let modelObservationReading = false;
    const stopModelObservation = () => {
      if (modelObservationTimer !== null) window.clearInterval(modelObservationTimer);
      modelObservationTimer = null;
    };
    const consoleLoaders = Object.freeze({
      runtime: () => load([['models', api.models]]),
      capabilities: () => load([['plugins', api.plugins]]),
      qqInfrastructure: () => load([['diagnostics', api.diagnostics]]),
      network: () => load([
        ['network', api.network],
        ['proxySubscriptions', api.proxySubscriptions],
        ['proxyGroups', api.proxyGroups],
      ]),
      reliability: () => load([['reliability', api.reliability], ['services', api.services]]),
      diagnostics: () => load([['logs', api.logs]]),
    });
    const persistCanonicalRuntimeRegistry = (terminal) => {
      if (!terminal.registry) return;
      consoleData.models = safe(terminal.registry);
      loadedConsole.add('runtime');
    };
    const focusRuntimeReturn = (focusId, origin) => {
      const panel = $('#v4ConsolePanel', root);
      const selectors = [];
      if (focusId) selectors.push(`#${CSS.escape(focusId)}`);
      if (origin?.type === 'provider' && origin.id) selectors.push(`[data-v4-provider-edit="${CSS.escape(origin.id)}"]`);
      if (origin?.type === 'model' && origin.id) selectors.push(`[data-v4-model-edit="${CSS.escape(origin.id)}"]`);
      for (const selector of selectors) {
        const target = $(selector, panel || root);
        if (target) { target.focus(); return; }
      }
      $(`[data-v4-runtime-view="${runtimeUi.activeSubview}"]`, panel || root)?.focus();
    };
    // Every Console write is followed by a canonical registry readback.  The
    // returned POST object is not treated as sufficient UI state because a
    // profile apply or eligibility computation may change adjacent records.
    const finishRuntimeWrite = async ({ write, savedMessage, partialMessage, entity = null, closeDraft = '' }) => {
      const focusId = runtimeUi.returnFocusId;
      const origin = runtimeUi.dialogOrigin;
      const previousDraft = entity?.kind === 'provider' ? runtimeUi.providerDraft : runtimeUi.modelDraft;
      const session = runtimeSessions.begin({ kind: 'runtime-write', entity });
      return runtimeWorkspaceCore.runTerminal({
        write,
        readback: () => api.models(),
        persist: persistCanonicalRuntimeRegistry,
        session,
        commit: async (terminal) => {
          const publicFailure = terminal.error || { payload: terminal.result || {} };
          const message = terminal.ok ? savedMessage : userError(publicFailure, partialMessage);
          runtimeUi.notice = terminal.readbackError
            ? `${message} 但服务器回读失败；请刷新页面确认结果后再决定是否重试。`
            : message;
          if (entity?.action === 'delete') {
            const reconciled = runtimeWorkspaceCore.reconcileDeleteDraft({
              kind: entity.kind, id: entity.id, previous: previousDraft,
              registry: terminal.registry, readbackError: terminal.readbackError,
            });
            if (entity.kind === 'provider') runtimeUi.providerDraft = reconciled;
            else runtimeUi.modelDraft = reconciled;
          } else if (!terminal.error && !terminal.readbackError && closeDraft === 'provider') runtimeUi.providerDraft = null;
          else if (!terminal.error && !terminal.readbackError && closeDraft === 'model') runtimeUi.modelDraft = null;
          const dialogRemainsOpen = Boolean(runtimeUi.providerDraft || runtimeUi.modelDraft);
          if (!dialogRemainsOpen) {
            runtimeUi.returnFocusId = '';
            runtimeUi.dialogOrigin = null;
            if (origin?.view) runtimeUi.activeSubview = origin.view;
          }
           await renderPanel();
           if (dialogRemainsOpen) return;
           focusRuntimeReturn(focusId, origin);
         },
       });
    };
    const refreshPlugins = async (notice = '') => {
      consoleData.plugins = safe(await api.plugins());
      loadedConsole.delete('capabilities');
      loadedConsole.add('capabilities');
      runtimeUi.capabilityNotice = notice;
    };
    const ensureConsoleData = async () => {
      if (loadedConsole.has(active)) return;
      const result = await consoleLoaders[active]();
      const failed = Object.values(result).some((value) => value && typeof value === 'object' && value._error);
      if (failed) {
        loadedConsole.delete(active);
        const error = new Error('console_read_failed');
        error.name = 'ConsoleReadError';
        throw error;
      }
      Object.entries(result).forEach(([key, value]) => { consoleData[key] = safe(value); });
      loadedConsole.add(active);
    };
    const renderPanel = async () => {
      stopModelObservation();
      const host = $('#v4ConsolePanel', root); if (!host) return;
      const renderEpoch = ++runtimeRenderEpoch;
      host.innerHTML = '<p class="v4-loading">正在读取此后台视图…</p>';
      try { await ensureConsoleData(); } catch (error) { host.innerHTML = `<p class="v4-error" role="status">${esc(userError(error, '暂时无法读取这部分后台状态。'))}</p>`; return; }
      // A slower previous refresh must not replace the subview the Owner has
      // already selected. WCAG 2.2 - 3.2.2 On Input, 4.1.3 Status Messages.
      if (renderEpoch !== runtimeRenderEpoch) return;
      if (active === 'runtime') {
        const loadConnectionProxyData = async () => {
          const [consumer, subscriptions, groups] = await Promise.all([
            api.proxyConsumer(),
            api.proxySubscriptions(),
            api.proxyGroups(),
          ]);
          consoleData.runtimeProxyConsumer = consumer;
          consoleData.runtimeProxySubscriptions = subscriptions;
          consoleData.runtimeProxyGroups = groups;
        };
        const registry = consoleData.models?.result || consoleData.models || {};
        const catalog = registry.models || registry.items || [];
        const models = Array.isArray(catalog) ? catalog : asList(catalog.items);
        const providers = asList(registry.providers);
        const roles = asList(registry.roles);
        const boundedOptionItems = (items, selected, limit = runtimeUi.pageSize) => {
          const source = asList(items);
          if (limit === null) return source;
          const max = Math.max(1, Math.min(25, Number(limit) || 25));
          if (source.length <= max) return source;
          const current = source.find((item) => String(item?.id || '') === String(selected || ''));
          if (!current || source.indexOf(current) < max) return source.slice(0, max);
          return [...source.slice(0, max - 1), current];
        };
        const optionList = (role, selected, limit = runtimeUi.pageSize - 1) => [`<option value="">不设置</option>`, ...boundedOptionItems(models, selected, limit).map((model) => {
          const eligibility = model.executor_eligibility || {};
          const isExecutor = role === 'work_executor';
          const customExecutor = model.transport === 'codex_cli_custom_provider';
          const available = isExecutor
            ? model.can_bind_work_executor === true && (eligibility.can_activate === true || eligibility.can_bind === true)
            : Boolean(Number(model.enabled ?? 1)) && Boolean(Number(model.provider_enabled ?? 1)) && !customExecutor;
          const selectedInvalid = String(model.id || '') === String(selected || '') && !available;
          const reason = isExecutor && !available
            ? ` · ${eligibility.reason_zh || '当前不满足工作执行前置条件'}`
            : (!isExecutor && customExecutor ? ' · 仅可用于工作执行器' : '');
          return `<option value="${esc(model.id)}" ${(available || selectedInvalid) ? '' : 'disabled'} ${selectedInvalid ? 'data-v4-currently-invalid="true"' : ''} ${model.id === selected ? 'selected' : ''}>${esc(`${recordTitle(model, ['label', 'display_name', 'model', 'id'])}${reason}`)}</option>`;
        })].join('');
        const routeSelectionNote = models.length > runtimeUi.pageSize - 1
          ? '<small class="v4-runtime-selection-note">路由选择器初始显示最多 24 个模型（含当前选择）；更多模型请先在“模型目录”筛选。</small>'
          : '';
        const attachRuntimeOverflowSelector = (select, items, label, optionMarkup) => {
          const source = asList(items);
          if (!select || select.disabled || source.length <= runtimeUi.pageSize || !select.parentElement) return;
          const details = document.createElement('details');
          details.className = 'v4-runtime-option-details';
          const summary = document.createElement('summary');
          summary.textContent = `${label}（共 ${source.length} 项）`;
          const overflow = document.createElement('select');
          overflow.setAttribute('aria-label', `${label}完整列表`);
          overflow.innerHTML = optionMarkup;
          overflow.value = select.value;
          overflow.addEventListener('change', () => {
            const existing = [...select.options].find((option) => option.value === overflow.value);
            if (!existing && overflow.value) {
              const option = document.createElement('option');
              option.value = overflow.value;
              option.textContent = overflow.selectedOptions[0]?.textContent || overflow.value;
              select.append(option);
            }
            select.value = overflow.value;
            summary.textContent = `${label}（已选 ${overflow.selectedOptions[0]?.textContent || overflow.value || '不设置'}）`;
          });
          details.append(summary, overflow);
          select.insertAdjacentElement('afterend', details);
        };
        const roleCards = cards(roles, (role) => {
          const executor = role.role === 'work_executor' ? models.find((model) => model.id === role.primary_model_id) : null;
          const verify = executor?.provider_id ? `<button class="v4-text-button" type="button" data-v4-executor-verify="${esc(executor.provider_id)}">验证工作执行</button>` : '';
          const fallback = role.role === 'work_executor'
            ? '<p class="v4-note">工作执行器不支持备用模型；只能绑定一条已验证的受信任执行路径。</p>'
            : `<label>备用模型<select data-v4-role-fallback="${esc(role.role)}">${optionList(role.role, role.fallback_model_id)}</select></label>`;
          const executorHint = role.role === 'work_executor' && executor?.executor_eligibility?.can_activate !== true && executor?.executor_eligibility?.can_bind !== true
            ? `<small class="v4-role-eligibility">当前绑定不可用于新的工作执行路由：${esc(executor.executor_eligibility?.reason_zh || '请先配置并验证执行器 Profile')}</small>` : '';
          const roleAction = role.role === 'work_executor' ? '验证并切换' : '保存路由';
          return `<article class="${role.role === 'work_executor' ? 'v4-role-work-executor' : ''}"><div><strong>${esc(role.label || role.role)}</strong><span>${esc(role.description || '')}</span>${executorHint}</div><label>主模型<select data-v4-role-primary="${esc(role.role)}">${optionList(role.role, role.primary_model_id)}</select></label>${fallback}${routeSelectionNote}<div class="v4-role-actions"><button class="v4-secondary" type="button" data-v4-role-bind="${esc(role.role)}">${roleAction}</button>${verify}</div></article>`;
        });
        const providerDraft = runtimeUi.providerDraft || {};
        const profileDraft = providerDraft.executor_profile || {};
        const modelDraft = runtimeUi.modelDraft || {};
        const selected = (value, expected) => String(value || '') === expected ? 'selected' : '';
        const checked = (value, fallback = true) => (value === undefined || value === null ? fallback : Boolean(Number(value))) ? 'checked' : '';
        const enumOptions = (values, value) => values.map(([id, label]) => `<option value="${id}" ${selected(value, id)}>${esc(label)}</option>`).join('');
        const providerOptions = boundedOptionItems(providers, modelDraft.provider_id).map((provider) => `<option value="${esc(provider.id)}" ${selected(modelDraft.provider_id, provider.id)}>${esc(provider.name || provider.id)}</option>`).join('');
        const providerOverflowOptions = providers.map((provider) => `<option value="${esc(provider.id)}" ${selected(modelDraft.provider_id, provider.id)}>${esc(provider.name || provider.id)}</option>`).join('');
        const upstreamModels = models.filter((model) => model.transport === 'openai_chat_completions' && Boolean(Number(model.enabled ?? 1)) && Boolean(Number(model.provider_enabled ?? 1)));
        const upstreamProviderIds = new Set(upstreamModels.map((model) => model.provider_id));
        const upstreamProviders = providers.filter((provider) => upstreamProviderIds.has(provider.id));
        const upstreamProviderOptions = boundedOptionItems(upstreamProviders, profileDraft.upstream_provider_id).map((provider) => `<option value="${esc(provider.id)}" ${selected(profileDraft.upstream_provider_id, provider.id)}>${esc(provider.name || provider.id)}</option>`).join('');
        const upstreamProviderOverflowOptions = upstreamProviders.map((provider) => `<option value="${esc(provider.id)}" ${selected(profileDraft.upstream_provider_id, provider.id)}>${esc(provider.name || provider.id)}</option>`).join('');
        const upstreamModelOptions = boundedOptionItems(upstreamModels, profileDraft.upstream_model_id).map((model) => `<option value="${esc(model.id)}" ${selected(profileDraft.upstream_model_id, model.id)}>${esc(recordTitle(model, ['label', 'model', 'id']))}</option>`).join('');
        const upstreamModelOverflowOptions = upstreamModels.map((model) => `<option value="${esc(model.id)}" ${selected(profileDraft.upstream_model_id, model.id)}>${esc(recordTitle(model, ['label', 'model', 'id']))}</option>`).join('');
        const proxyConnection = runtimeWorkspaceCore.proxyConnectionState({
          providerId: providerDraft.id,
          consumer: consoleData.runtimeProxyConsumer,
          subscriptions: safe(consoleData.runtimeProxySubscriptions),
          groups: safe(consoleData.runtimeProxyGroups),
        });
        const proxyManagement = proxyConnection?.management || {};
        const proxySavedDraft = proxyManagement.draft && typeof proxyManagement.draft === 'object' ? proxyManagement.draft : {};
        const proxyNodeNames = new Set((proxyConnection?.nodes || []).map((item) => String(item.name || '')));
        if (proxyConnection && (!runtimeUi.proxyConnectionNode || !proxyNodeNames.has(runtimeUi.proxyConnectionNode))) {
          runtimeUi.proxyConnectionNode = proxyNodeNames.has(String(proxySavedDraft.node || ''))
            ? String(proxySavedDraft.node)
            : (proxyNodeNames.has(proxyConnection.currentNode) ? proxyConnection.currentNode : String(proxyConnection.nodes[0]?.name || ''));
        }
        if (proxyConnection && runtimeUi.proxyConnectionEnabled === null) runtimeUi.proxyConnectionEnabled = Boolean(proxySavedDraft.enabled);
        if (proxyConnection && !runtimeUi.proxyConnectionFormVersion) runtimeUi.proxyConnectionFormVersion = proxySavedDraft.form_version || `connection-proxy-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        const proxyNodeOptions = (proxyConnection?.nodes || []).map((node) => `<option value="${esc(node.name)}" ${selected(runtimeUi.proxyConnectionNode, node.name)}>${esc(node.name)}</option>`).join('');
        const proxyReadError = consoleData.runtimeProxyConsumer?._error || consoleData.runtimeProxySubscriptions?._error || consoleData.runtimeProxyGroups?._error || '';
        const proxyProviderTest = proxyConnection?.consumer?.provider_test || {};
        const proxyTestLabel = (value) => ({passed: '通过', failed: '失败', unverified: '未测试'})[value] || '未测试';
        const proxyBasicReadiness = proxyConnection?.consumer?.product_readiness_label
          || (proxyConnection?.consumer?.ready ? '基础代理链已就绪' : '基础代理链尚未就绪');
        const proxyTestTime = proxyProviderTest.tested_at ? formatTime(proxyProviderTest.tested_at) : '未记录';
        const proxyConnectionMarkup = providerDraft.id === 'aiclient2api-gemini-antigravity' ? `<section id="v4ConnectionProxyControls" class="v4-connection-proxy" aria-labelledby="v4ConnectionProxyHeading"><div><h4 id="v4ConnectionProxyHeading">AIClient2API 受控出站</h4><p>这里引用服务器当前订阅与唯一的 Proxies Selector，不复制代理 inventory。</p></div>${proxyReadError ? `<p class="v4-error" role="status">代理资产读取失败；写操作保持关闭。</p>` : ''}<div class="v4-operator-grid"><article><strong>基础代理链</strong><span>${esc(proxyBasicReadiness)}</span><small>只表示网络、容器与受控出站基础条件。</small></article><article><strong>最近连接测试</strong><span>Text：${esc(proxyTestLabel(proxyProviderTest.text_status))} · Vision：${esc(proxyTestLabel(proxyProviderTest.vision_status))}</span><small>测试时间：${esc(proxyTestTime)}</small></article></div><div class="v4-console-form-grid"><label>当前服务器订阅<select id="v4ConnectionProxySubscription" aria-describedby="v4ConnectionProxyAssetHelp" ${proxyConnection?.subscription ? '' : 'disabled'}><option value="${esc(proxyConnection?.subscription?.key || '')}">${esc(proxyConnection?.subscription?.name || proxyConnection?.subscription?.key || '未设置当前订阅')}</option></select></label><label>Proxies 节点<select id="v4ConnectionProxyNode" ${proxyNodeOptions ? '' : 'disabled'}>${proxyNodeOptions || '<option value="">暂无节点</option>'}</select></label><label class="v4-check"><input id="v4ConnectionProxyEnabled" type="checkbox" ${runtimeUi.proxyConnectionEnabled ? 'checked' : ''} ${proxyConnection?.subscription ? '' : 'disabled'}>启用此连接的受控出站草稿</label></div><p id="v4ConnectionProxyAssetHelp" class="v4-runtime-editor-help">资产由网络与代理页维护；此连接只保存 active subscription / Proxies node 引用。</p><div class="v4-inline-actions v4-connection-proxy-actions"><button id="v4ConnectionProxySave" class="v4-secondary" type="button" ${proxyConnection?.subscription && runtimeUi.proxyConnectionNode ? '' : 'disabled'}>保存出站草稿</button><button id="v4ConnectionProxyTest" class="v4-secondary" type="button" ${Object.keys(proxySavedDraft).length ? '' : 'disabled'}>测试连接</button><button id="v4ConnectionProxyApply" class="v4-primary" type="button" ${proxyManagement.apply_eligible ? '' : 'disabled'}>应用配置</button><button id="v4ConnectionProxyRollback" class="v4-secondary" type="button" ${proxyManagement.rollback_available ? '' : 'disabled'}>回滚上一版</button></div><p id="v4ConnectionProxyStatus" role="status" aria-live="polite">${esc(runtimeUi.proxyConnectionNotice)}</p><details class="v4-proxy-advanced"><summary>连接出站详情</summary><p>consumer revision：${esc(proxyManagement.revision ?? 0)} · subscription revision：${esc(proxyConnection?.managementRevision ?? 0)}</p><p>readiness：${esc(pick(proxyConnection?.consumer || {}, ['readiness_code', 'error'], 'UNKNOWN'))}</p><p>direct_allowed：${proxyConnection?.consumer?.direct_allowed ? 'true' : 'false'} · fallback_allowed：${proxyConnection?.consumer?.fallback_allowed ? 'true' : 'false'}</p></details></section>` : '';
        const executorVerificationPresentation = (provider) => {
          const verification = provider.executor_profile?.verification || {};
          const status = ['verified', 'failed', 'stale', 'pending'].includes(verification.status) ? verification.status : 'pending';
          const labels = {
            verified: '工作执行已验证',
            failed: '工作执行验证失败',
            stale: '工作执行验证已失效',
            pending: '工作执行尚待验证',
          };
          return { status, label: labels[status] };
        };
        const normalizeFilter = (value) => String(value || '').trim().toLocaleLowerCase('zh-CN');
        const providerFilter = normalizeFilter(runtimeUi.providerFilter);
        const modelFilter = normalizeFilter(runtimeUi.modelFilter);
        const filteredProviders = providers.filter((provider) => !providerFilter || [provider.id, provider.name, provider.kind, provider.transport]
          .some((value) => normalizeFilter(value).includes(providerFilter)));
        const filteredModels = models.filter((model) => {
          if (runtimeUi.modelProviderFilter && model.provider_id !== runtimeUi.modelProviderFilter) return false;
          if (!modelFilter) return true;
          return [model.id, model.label, model.model, model.provider_name, model.provider_id]
            .some((value) => normalizeFilter(value).includes(modelFilter));
        });
        const providerPageCount = Math.max(1, Math.ceil(filteredProviders.length / runtimeUi.pageSize));
        const modelPageCount = Math.max(1, Math.ceil(filteredModels.length / runtimeUi.pageSize));
        runtimeUi.providerPage = Math.min(runtimeUi.providerPage, providerPageCount - 1);
        runtimeUi.modelPage = Math.min(runtimeUi.modelPage, modelPageCount - 1);
        const providerPageStart = runtimeUi.providerPage * runtimeUi.pageSize;
        const modelPageStart = runtimeUi.modelPage * runtimeUi.pageSize;
        const providerPageItems = filteredProviders.slice(providerPageStart, providerPageStart + runtimeUi.pageSize);
        const modelPageItems = filteredModels.slice(modelPageStart, modelPageStart + runtimeUi.pageSize);
        const paginationLabels = { provider: '连接', model: '模型目录', 'executor-login': 'Codex 登录执行器', 'executor-profile': 'Custom Provider Profile', 'executor-candidate': '执行适配器候选' };
        const pagination = (kind, page, pageCount, total) => pageCount <= 1 ? '' : `<nav class="v4-runtime-pagination" aria-label="${paginationLabels[kind] || '列表'}分页"><button class="v4-secondary" type="button" data-v4-${kind}-page="${page - 1}" ${page <= 0 ? 'disabled' : ''}>上一页</button><span>第 ${page + 1} / ${pageCount} 页 · 共 ${total} 项</span><button class="v4-secondary" type="button" data-v4-${kind}-page="${page + 1}" ${page + 1 >= pageCount ? 'disabled' : ''}>下一页</button></nav>`;
        const providerCatalog = cards(providerPageItems, (provider) => {
          const verification = executorVerificationPresentation(provider);
          const transportLabel = PROVIDER_TRANSPORT_LABELS[provider.transport] || provider.transport || '未配置传输';
          const billingLabel = PROVIDER_BILLING_LABELS[provider.billing_scope] || provider.billing_scope || '计费未知';
          const keyState = provider.api_key_set
            ? '已设置密钥'
            : (provider.secret_available ? '密钥可用' : '未设置密钥');
          const endpoint = provider.base_url || '（无端点，本地/登录型连接）';
          const refs = runtimeWorkspaceCore.assetReferences({ kind: 'provider', id: provider.id, models, providers, roles });
          const usedBy = refs.modelRefs.map((item) => item.label).slice(0, 3).join('、');
          const more = refs.modelRefs.length > 3 ? `等 ${refs.modelRefs.length} 项` : '';
          const profileNames = refs.profileRefs.map((item) => item.label).join('、');
          const dependency = refs.blocked
            ? `关联模型：${usedBy || '无'}${more}；Profile 引用：${profileNames || '无'}。先迁移并验证引用，再清退连接。`
            : '没有已知模型或 Profile 引用；是否可移除仍以服务端回读为准。';
          return `<article class="v4-runtime-item"><div><strong>${esc(provider.name || provider.id)}</strong><span>${esc(`${transportLabel} · ${Number(provider.enabled) ? '启用' : '停用'}`)}</span><span>${esc(endpoint)}</span><span>${esc(`${billingLabel} · ${keyState}`)}</span><small>${esc(dependency)}</small><small class="v4-executor-verification v4-executor-${esc(verification.status)}">${esc(verification.label)}</small></div><div class="v4-runtime-actions"><button id="v4ProviderEdit-${esc(provider.id)}" class="v4-text-button" type="button" data-v4-provider-edit="${esc(provider.id)}">配置连接</button>${refs.modelRefs.length ? `<button class="v4-text-button" type="button" data-v4-provider-models="${esc(provider.id)}">查看关联模型</button>` : ''}${refs.profileRefs.filter((ref) => ref.id !== provider.id).map((ref) => `<button class="v4-text-button" type="button" data-v4-provider-edit="${esc(ref.id)}">查看 ${esc(ref.label)} Profile</button>`).join('')}</div></article>`;
        });
        const modelCatalog = cards(modelPageItems, (model) => {
          const capabilityLine = `能力：${modelCapabilitySummary(model)}`;
          const apiName = model.model || '（未填接口模型名）';
          const stateLine = `${model.provider_name || model.provider_id || '未知连接'} · ${Number(model.enabled ?? 1) ? '启用' : '停用'}${modelPriceSummary(model)}`;
          const refs = runtimeWorkspaceCore.assetReferences({ kind: 'model', id: model.id, models, providers, roles });
          const dependency = refs.blocked
            ? `正在被${[...refs.roleRefs.map((item) => item.label), ...refs.profileRefs.map((item) => `${item.label} Profile`)].join('、')}引用；先替换、验证并回读引用。`
            : '没有已知角色或 Profile 引用；可验证后按服务端规则退役或移除。';
          return `<article class="v4-runtime-item"><div><strong>${esc(recordTitle(model, ['label', 'display_name', 'name', 'model_id', 'id']))}</strong><span>接口模型：${esc(apiName)}</span><span>${esc(capabilityLine)}</span><span>${esc(stateLine)}</span><small>${esc(dependency)}</small></div><div class="v4-runtime-actions"><button id="v4ModelEdit-${esc(model.id)}" class="v4-text-button" type="button" data-v4-model-edit="${esc(model.id)}">配置模型</button><button class="v4-text-button" type="button" data-v4-model-test="${esc(model.id)}">验证</button>${refs.roleRefs.length ? '<button class="v4-text-button" type="button" data-v4-runtime-view="overview">查看角色路由</button>' : ''}${refs.profileRefs.map((ref) => `<button class="v4-text-button" type="button" data-v4-provider-edit="${esc(ref.id)}">查看 ${esc(ref.label)} Profile</button>`).join('')}</div></article>`;
        });
        const codexLoginProviders = providers.filter((provider) => provider.transport === 'codex_cli_chatgpt');
        const profileProviders = providers.filter((provider) => provider.transport === 'codex_cli_custom_provider');
        const executorCards = (items, kind) => cards(items, (provider) => {
          const verification = executorVerificationPresentation(provider);
          const upstream = provider.executor_upstream || {};
          const upstreamLine = kind === 'profile' && upstream.model_label
            ? `上游：${upstream.model_label} · ${upstream.provider_name || upstream.provider_id || ''}`
            : (kind === 'profile' ? '尚未配置上游模型；请先保存 Profile 草稿。' : '');
          const verify = kind === 'profile' && provider.executor_profile?.enabled
            ? `<button class="v4-text-button" type="button" data-v4-executor-verify="${esc(provider.id)}">隔离验证此候选</button>` : '';
          return `<article class="v4-runtime-item"><div><strong>${esc(provider.name || provider.id)}</strong><span>${esc(kind === 'profile' ? 'Profile 草稿；保存和隔离验证都不会自动切换 Runtime' : 'Codex 登录态；由平台运行时管理')}</span>${upstreamLine ? `<span>${esc(upstreamLine)}</span>` : ''}<small class="v4-executor-verification v4-executor-${esc(verification.status)}">${esc(verification.label)}</small></div><div class="v4-runtime-actions"><button class="v4-text-button" type="button" data-v4-provider-edit="${esc(provider.id)}">${kind === 'profile' ? '配置 Profile 草稿' : '查看连接'}</button>${verify}</div></article>`;
        });
        const executorCreationCandidates = models.filter((model) => model.transport === 'openai_chat_completions' && model.executor_eligibility?.can_configure === true);
        const boundedExecutorLogins = runtimeWorkspaceCore.boundPage(codexLoginProviders, runtimeUi.executorLoginPage, runtimeUi.pageSize);
        const boundedExecutorProfiles = runtimeWorkspaceCore.boundPage(profileProviders, runtimeUi.executorProfilePage, runtimeUi.pageSize);
        const boundedExecutorCandidates = runtimeWorkspaceCore.boundPage(executorCreationCandidates, runtimeUi.executorCandidatePage, runtimeUi.pageSize);
        runtimeUi.executorLoginPage = boundedExecutorLogins.page;
        runtimeUi.executorProfilePage = boundedExecutorProfiles.page;
        runtimeUi.executorCandidatePage = boundedExecutorCandidates.page;
        const executorCreationCards = executorCreationCandidates.length ? `<section class="v4-executor-candidates" aria-labelledby="v4ExecutorCandidates"><h4 id="v4ExecutorCandidates">可创建的执行适配器候选 <span>${boundedExecutorCandidates.total} 项</span></h4><p class="v4-note">这些已保存模型可以创建新的执行适配器草稿；创建后仍需隔离验证与“验证并切换”才会生效。</p>${cards(boundedExecutorCandidates.items, (model) => `<article class="v4-runtime-item"><div><strong>${esc(recordTitle(model, ['label', 'model', 'id']))}</strong><span>${esc(model.executor_eligibility?.reason_zh || '服务器允许以此模型配置候选工作执行适配器')}</span></div><button id="v4ExecutorCreate-${esc(model.id)}" class="v4-text-button" type="button" data-v4-executor-create="${esc(model.id)}">新建执行适配器</button></article>`)}${pagination('executor-candidate', boundedExecutorCandidates.page, boundedExecutorCandidates.pageCount, boundedExecutorCandidates.total)}</section>` : '';
        const providerEditor = runtimeUi.providerDraft ? `<section class="v4-runtime-editor" aria-labelledby="v4ProviderEditorHeading"><div><p>Connection</p><h3 id="v4ProviderEditorHeading">${providerDraft.id ? '配置连接与 Executor Profile' : '添加连接'}</h3><span>密钥只可写入，读取和保存后的回读都不会显示密钥内容。</span></div><form id="v4ProviderForm"><div class="v4-console-form-grid"><label>连接 ID<input name="id" required pattern="[a-z0-9][a-z0-9_-]{1,63}" value="${esc(providerDraft.id || '')}" ${providerDraft.id ? 'readonly' : ''}></label><label>显示名称<input name="name" required value="${esc(providerDraft.name || '')}"></label><label>Provider 类型<select name="kind">${enumOptions([['codex', 'Codex'], ['openai', 'OpenAI'], ['openai-compatible', 'OpenAI Compatible'], ['openrouter', 'OpenRouter'], ['anthropic', 'Anthropic'], ['gemini', 'Gemini'], ['azure-openai', 'Azure OpenAI'], ['ollama', 'Ollama'], ['lm-studio', 'LM Studio']], providerDraft.kind || 'openai-compatible')}</select></label><label>传输协议<select name="transport">${enumOptions([['codex_cli_chatgpt', 'Codex ChatGPT'], ['codex_cli_custom_provider', 'Codex CLI Custom Provider'], ['openai_chat_completions', 'OpenAI Chat Completions'], ['azure_openai_chat_completions', 'Azure OpenAI Chat Completions'], ['anthropic_messages', 'Anthropic Messages'], ['google_gemini_generate_content', 'Google Gemini Generate Content']], providerDraft.transport || 'openai_chat_completions')}</select></label><label>计费范围<select name="billing_scope">${enumOptions([['chatgpt_subscription', 'ChatGPT Subscription'], ['api_key', 'API Key'], ['local_proxy', 'Local Proxy']], providerDraft.billing_scope || 'api_key')}</select></label><label>请求超时（秒）<input name="timeout_seconds" type="number" min="5" max="600" value="${esc(providerDraft.timeout_seconds || 60)}"></label><label class="v4-console-wide">基础地址（Codex 登录连接可留空）<input name="base_url" type="url" value="${esc(providerDraft.base_url || '')}" placeholder="https://…"></label><label>新的 API Key（可选）<input name="api_key" type="password" autocomplete="new-password" spellcheck="false" placeholder="${providerDraft.api_key_set ? '留空即保持现有密钥' : '只写入，不会回显'}"></label><label class="v4-check"><input name="clear_api_key" type="checkbox">清除当前 API Key</label><label class="v4-check"><input name="enabled" type="checkbox" ${checked(providerDraft.enabled)}>启用这条连接</label><label class="v4-check"><input name="trusted_for_executor" type="checkbox" ${checked(providerDraft.trusted_for_executor, false)}>允许作为受信任工作执行器</label></div><fieldset class="v4-runtime-profile"><legend>Executor Profile（仅适用于 Codex CLI Custom Provider）</legend><div class="v4-console-form-grid"><label>Profile 名称<input name="executor_profile_name" value="${esc(profileDraft.profile_name || providerDraft.id || '')}"></label><label class="v4-check"><input name="executor_enabled" type="checkbox" ${checked(profileDraft.enabled)}>启用此 Profile</label><label>上游 Provider<select name="executor_upstream_provider_id"><option value="">不设置</option>${upstreamProviderOptions}</select></label><label>上游模型<select name="executor_upstream_model_id"><option value="">不设置</option>${upstreamModelOptions}</select></label></div><p class="v4-note">选择其他传输时，服务端不会保留此 Profile。选择 Codex CLI Custom Provider 时，必须选择一条启用的 OpenAI Chat Completions 上游模型；保存后还要通过“验证工作执行”才能绑定工作执行角色。</p></fieldset>${proxyConnectionMarkup}<div class="v4-inline-actions v4-provider-editor-actions"><button class="v4-primary" type="submit">保存连接与 Profile</button><button class="v4-secondary" type="button" data-v4-provider-cancel>取消</button><p id="v4ProviderStatus" role="status" aria-live="polite"></p></div></form></section>` : '';
        const modelEditor = runtimeUi.modelDraft ? `<section class="v4-runtime-editor" aria-labelledby="v4ModelEditorHeading"><div><p>Catalog</p><h3 id="v4ModelEditorHeading">${modelDraft.id ? '配置模型目录项' : '添加模型目录项'}</h3><span>目录项不是可执行授权；工作执行仍必须满足服务器实时资格校验。</span></div><form id="v4ModelForm"><div class="v4-console-form-grid"><label>目录 ID<input name="id" required pattern="[a-z0-9][a-z0-9_-]{1,63}" value="${esc(modelDraft.id || '')}" ${modelDraft.id ? 'readonly' : ''}></label><label>连接<select name="provider_id" ${modelDraft.id ? 'disabled' : ''} required>${providerOptions}</select></label><label>显示名称<input name="label" required value="${esc(modelDraft.label || '')}"></label><label>接口模型名<input name="model" value="${esc(modelDraft.model || '')}"></label><label>上下文窗口<input name="context_window" type="number" min="0" max="0" value="${esc(modelDraft.context_window || 0)}"></label><label>最大输出 Token<input name="max_output_tokens" type="number" min="0" max="131072" value="${esc(modelDraft.max_output_tokens || 900)}"></label><label class="v4-check"><input name="enabled" type="checkbox" ${checked(modelDraft.enabled)}>启用目录项</label><label class="v4-check"><input name="capability_text" type="checkbox" ${checked((modelDraft.capabilities || []).includes('text'))}>文本能力</label><label class="v4-check"><input name="capability_tools" type="checkbox" ${checked((modelDraft.capabilities || []).includes('tools'), false)}>工具能力</label><label class="v4-check"><input name="capability_vision" type="checkbox" ${checked((modelDraft.capabilities || []).includes('vision'), false)}>视觉能力</label><label class="v4-check"><input name="capability_structured" type="checkbox" ${checked((modelDraft.capabilities || []).includes('structured_output'), false)}>结构化输出</label><label class="v4-check"><input name="capability_embedding" type="checkbox" ${checked((modelDraft.capabilities || []).includes('embedding'), false)}>嵌入能力</label><label>输入价格 / 百万 Token<input name="input_price_per_million" type="number" min="0" step="any" value="${esc(modelDraft.input_price_per_million ?? '')}"></label><label>输出价格 / 百万 Token<input name="output_price_per_million" type="number" min="0" step="any" value="${esc(modelDraft.output_price_per_million ?? '')}"></label><label>价格币种<input name="price_currency" maxlength="12" value="${esc(modelDraft.price_currency || 'USD')}"></label><label>价格来源<input name="price_source" maxlength="500" value="${esc(modelDraft.price_source || '')}"></label><label class="v4-console-wide">备注<textarea name="notes" rows="2" maxlength="500">${esc(modelDraft.notes || '')}</textarea></label></div><div class="v4-inline-actions"><button class="v4-primary" type="submit">保存模型目录</button><button class="v4-secondary" type="button" data-v4-model-cancel>取消</button><p id="v4ModelStatus" role="status" aria-live="polite"></p></div></form></section>` : '';
        const runtimeViews = [
          ['overview', '概览与路由'],
          ['connections', '连接'],
          ['catalog', '模型目录'],
          ['executors', '执行器'],
        ];
        const overviewPanel = `<section id="v4RuntimePanel-overview" data-v4-runtime-panel="overview" aria-labelledby="v4RuntimeView-overview"><div class="v4-runtime-summary"><article><strong>${roles.length}</strong><span>运行角色</span></article><article><strong>${providers.length}</strong><span>已保存连接</span></article><article><strong>${models.length}</strong><span>模型目录项</span></article></div><section id="v4ModelRoleBinding" class="v4-role-binding"><h3>当前角色路由</h3><p>这里是唯一的模型角色编辑入口；AI Chat 只投影当前角色，不建立第二套配置。</p>${roleCards}</section></section>`;
        const connectionsPanel = `<section id="v4RuntimePanel-connections" data-v4-runtime-panel="connections" aria-labelledby="v4RuntimeView-connections"><div class="v4-runtime-toolbar"><label>筛选连接<input type="search" data-v4-provider-filter value="${esc(runtimeUi.providerFilter)}" placeholder="名称、ID 或协议"></label><button id="v4RuntimeProviderNew" class="v4-primary" type="button" data-v4-provider-new>添加连接</button></div><section id="v4RuntimeProviderLibrary" class="v4-runtime-catalog v4-runtime-library"><header><div><h3>已配置连接</h3><span>${filteredProviders.length} / ${providers.length} 条</span></div><p>获取模型和删除均在具体连接的编辑对话框中完成。</p></header>${providerCatalog}${pagination('provider', runtimeUi.providerPage, providerPageCount, filteredProviders.length)}</section></section>`;
        const catalogPanel = `<section id="v4RuntimePanel-catalog" data-v4-runtime-panel="catalog" aria-labelledby="v4RuntimeView-catalog"><div class="v4-runtime-toolbar"><label>筛选模型目录<input type="search" data-v4-model-filter value="${esc(runtimeUi.modelFilter)}" placeholder="名称、接口模型名或连接"></label><button id="v4RuntimeModelNew" class="v4-primary" type="button" data-v4-model-new ${providers.length ? '' : 'disabled'}>添加模型</button></div><nav class="v4-runtime-provider-chips" aria-label="按连接筛选模型">${[['', '全部'], ...providers.map((provider) => [provider.id, provider.name || provider.id])].map(([id, label]) => `<button type="button" class="v4-runtime-chip${runtimeUi.modelProviderFilter === id ? ' v4-runtime-chip-active' : ''}" data-v4-model-provider-filter="${esc(id)}" aria-pressed="${runtimeUi.modelProviderFilter === id ? 'true' : 'false'}">${esc(label)}</button>`).join('')}</nav><section id="v4RuntimeModelLibrary" class="v4-runtime-catalog v4-runtime-library"><header><div><h3>模型目录</h3><span>${filteredModels.length} / ${models.length} 项</span></div><p>点击上方连接名称可只看该连接的模型；添加时先选择连接，再从该连接获取模型候选，候选只会预填草稿，不会覆盖价格、能力、备注或路由。</p></header>${modelCatalog}${pagination('model', runtimeUi.modelPage, modelPageCount, filteredModels.length)}</section></section>`;
        const executorsPanel = `<section id="v4RuntimePanel-executors" data-v4-runtime-panel="executors" aria-labelledby="v4RuntimeView-executors"><div class="v4-executor-sections"><section aria-labelledby="v4CodexLoginExecutors"><h3 id="v4CodexLoginExecutors">Codex 登录执行器 <span>${boundedExecutorLogins.total} 项</span></h3><p class="v4-note">这是使用你已登录的 Codex ChatGPT 账号执行工作的方式；平台只登记路由元数据，不读取你的登录凭据。</p>${executorCards(boundedExecutorLogins.items, 'login')}${pagination('executor-login', boundedExecutorLogins.page, boundedExecutorLogins.pageCount, boundedExecutorLogins.total)}</section><section aria-labelledby="v4CustomProviderProfiles"><h3 id="v4CustomProviderProfiles">Custom Provider Profile <span>${boundedExecutorProfiles.total} 项</span></h3><p class="v4-note">把一条 API 连接包装成受限的 Codex 执行器草稿：先保存草稿，再“隔离验证此候选”，最后在工作执行路由上“验证并切换”才会真正生效。</p>${executorCards(boundedExecutorProfiles.items, 'profile')}${pagination('executor-profile', boundedExecutorProfiles.page, boundedExecutorProfiles.pageCount, boundedExecutorProfiles.total)}${executorCreationCards}</section><article class="v4-runtime-gate" data-v4-dsh-gate="blocked"><strong>DeepSeek Harness</strong><span>尚未纳入 NekoAgent Runtime。当前缺少已审核的隔离 Runner、IPC/结果回执、服务端 Capability/Approval 约束与安全 E2E，因此此处不提供配置或选择操作。</span><small>Blocked Gate · 不创建 Provider · 不进入模型选项</small></article></div></section>`;
        const activeRuntimePanel = ({ overview: overviewPanel, connections: connectionsPanel, catalog: catalogPanel, executors: executorsPanel })[runtimeUi.activeSubview] || overviewPanel;
         host.innerHTML = `<section class="v4-console-section v4-runtime-console"><p>Runtime</p><h2>模型管理工作区</h2><p class="v4-note">模型注册表是唯一来源。密钥不会回读；模型或渠道切换不会改变 Assistant 身份、关系或已批准的执行边界。</p><nav class="v4-runtime-nav" aria-label="Runtime 管理区">${runtimeViews.map(([id, label]) => `<button id="v4RuntimeView-${id}" type="button" data-v4-runtime-view="${id}" aria-current="${id === runtimeUi.activeSubview ? 'page' : 'false'}">${label}</button>`).join('')}</nav>${activeRuntimePanel}${providerEditor}${modelEditor}<p id="v4RuntimeStatus" role="status" aria-live="polite">${esc(runtimeUi.notice)}</p><dialog id="v4RuntimeEditorDialog" class="v4-runtime-dialog"></dialog></section>`;
         if (providerDraft.id === 'aiclient2api-gemini-antigravity') {
           $('#v4ConnectionProxyControls', host)?.insertAdjacentHTML('beforeend', '<section id="v4ConnectionModelObservation" aria-labelledby="v4ConnectionModelObservationHeading"><h5 id="v4ConnectionModelObservationHeading">最近模型响应</h5><p class="v4-note">每 5 秒读取一次已有调用记录，不额外调用模型。响应自报模型仅代表上游返回的标识，不能证明后端实际权重。</p><div id="v4ConnectionModelObservationEvents" role="status" aria-live="polite">正在读取…</div></section>');
         }
         all('[data-v4-role-primary], [data-v4-role-fallback]', host).forEach((select) => {
           const role = select.dataset.v4RolePrimary || select.dataset.v4RoleFallback || '';
           attachRuntimeOverflowSelector(select, models, '更多模型', optionList(role, select.value, null));
         });
         const moveRuntimeEditorIntoDialog = () => {
          const dialog = $('#v4RuntimeEditorDialog', host);
          const editor = $('#v4ProviderForm', host)?.closest('.v4-runtime-editor') || $('#v4ModelForm', host)?.closest('.v4-runtime-editor');
          if (!dialog || !editor) return;
          const heading = $('h3[id]', editor);
          if (heading) dialog.setAttribute('aria-labelledby', heading.id);
          dialog.append(editor);
          dialog.addEventListener('cancel', async (event) => {
            event.preventDefault(); runtimeSessions.invalidate(); const focusId = runtimeUi.returnFocusId; const origin = runtimeUi.dialogOrigin; runtimeUi.providerDraft = null; runtimeUi.modelDraft = null; runtimeUi.modelDiscovery = { providerId: '', models: [], page: 0, selectedId: '', validation: null, status: '', focusDiscovery: false, lockedProviderId: '' }; runtimeUi.dialogOrigin = null; runtimeUi.returnFocusId = '';
            if (origin?.view) runtimeUi.activeSubview = origin.view;
            await renderPanel();
            focusRuntimeReturn(focusId, origin);
          }, { once: true });
          if (typeof dialog.showModal === 'function') dialog.showModal();
        };
        const providerEditorForm = $('#v4ProviderForm', host);
        if (providerEditorForm) {
           const savedProvider = providers.find((provider) => provider.id === providerDraft.id) || null;
            const refs = savedProvider
              ? runtimeWorkspaceCore.assetReferences({ kind: 'provider', id: savedProvider.id, models, providers, roles })
              : { modelRefs: [], profileRefs: [] };
            const ownedModels = refs.modelRefs;
            const executorProfileReferenced = Boolean(refs.profileRefs.length);
           const deletionReason = !providerDraft.id
            ? '请先保存这条连接，保存后的连接才可以删除。'
            : !savedProvider
              ? '当前连接尚未由服务器确认；请刷新后再操作。'
              : ownedModels.length
                ? `仍有 ${ownedModels.length} 个模型目录项（${ownedModels.map((item) => item.label).slice(0, 3).join('、')}${ownedModels.length > 3 ? '等' : ''}）；先在模型目录查看每项角色/Profile 引用，替换并验证新路由，回读后逐项移除。`
               : executorProfileReferenced
                  ? `此连接仍被 ${refs.profileRefs.map((item) => item.label).join('、')} Profile 或其验证状态引用；请先修改或停用 Profile，验证并回读后再清退。`
               : savedProvider.runtime_owner !== 'platform' || savedProvider.config_mode !== 'managed'
                  ? '此连接不是由平台管理的可删除配置，不能从此处删除。'
                  : '此连接没有关联目录项，可以在确认后删除。';
          const actions = $('.v4-provider-editor-actions', providerEditorForm);
          const status = $('#v4ProviderStatus', providerEditorForm);
          if (actions && status) {
            const help = document.createElement('p'); help.id = 'v4ProviderScopedHelp'; help.className = 'v4-runtime-editor-help';
            help.textContent = savedProvider
              ? `获取模型只使用已保存的连接配置；${deletionReason}`
              : deletionReason;
            const discover = document.createElement('button'); discover.type = 'button'; discover.className = 'v4-secondary';
            discover.textContent = '获取此连接的模型'; discover.setAttribute('data-v4-provider-discover', savedProvider?.id || '');
            discover.disabled = !savedProvider; discover.setAttribute('aria-describedby', help.id);
            const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'v4-danger-button';
            remove.textContent = '删除此连接'; remove.setAttribute('data-v4-provider-delete', savedProvider?.id || '');
            remove.disabled = !savedProvider || Boolean(ownedModels.length) || executorProfileReferenced || savedProvider.runtime_owner !== 'platform' || savedProvider.config_mode !== 'managed';
            remove.setAttribute('aria-describedby', help.id);
             const cancel = $('[data-v4-provider-cancel]', actions);
             actions.insertBefore(discover, cancel || status); actions.insertBefore(remove, cancel || status); actions.append(help);
           }
           attachRuntimeOverflowSelector(
             providerEditorForm.elements.executor_upstream_provider_id,
             upstreamProviders,
             '更多上游连接',
             upstreamProviderOverflowOptions,
           );
           attachRuntimeOverflowSelector(
             providerEditorForm.elements.executor_upstream_model_id,
             upstreamModels,
             '更多上游模型',
             upstreamModelOverflowOptions,
           );
         }
        const modelEditorForm = $('#v4ModelForm', host);
        if (modelEditorForm) {
          const savedModel = models.find((model) => model.id === modelDraft.id) || null;
          const refs = savedModel
            ? runtimeWorkspaceCore.assetReferences({ kind: 'model', id: savedModel.id, models, providers, roles })
            : { roleRefs: [], profileRefs: [], blocked: false };
          const routedRoles = refs.roleRefs.map((item) => item.label);
          const profileRefs = refs.profileRefs.map((item) => item.label);
          const deletionReason = !modelDraft.id
            ? '请先保存目录项，保存后的目录项才可以删除。'
            : !savedModel
              ? '当前目录项尚未由服务器确认；请刷新后再操作。'
              : routedRoles.length || profileRefs.length
                 ? `正在被${[...routedRoles, ...profileRefs.map((item) => `${item} Profile`)].join('、')}引用；先解除引用：在“概览与路由”替换角色模型、在关联连接修改 Profile 上游，验证并回读后再停用或移除。`
                : '此目录项没有角色或 Executor Profile 引用，可以在确认后删除。';
          const actions = $('.v4-inline-actions', modelEditorForm);
          const status = $('#v4ModelStatus', modelEditorForm);
          if (actions && status) {
            const help = document.createElement('p'); help.id = 'v4ModelDeleteHelp'; help.className = 'v4-runtime-editor-help'; help.textContent = deletionReason;
            const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'v4-danger-button';
            remove.textContent = '删除此目录项'; remove.setAttribute('data-v4-model-delete', savedModel?.id || '');
            remove.disabled = !savedModel || Boolean(refs.blocked); remove.setAttribute('aria-describedby', help.id);
             const cancel = $('[data-v4-model-cancel]', actions); actions.insertBefore(remove, cancel || status); actions.append(help);
           }
         }
         const modelForm = $('#v4ModelForm', host);
         const lockedProviderId = runtimeUi.modelDiscovery.lockedProviderId || '';
        if (modelForm && lockedProviderId) {
          const lockedProvider = providers.find((provider) => provider.id === lockedProviderId);
          modelForm.elements.provider_id.disabled = true;
          const note = document.createElement('p'); note.className = 'v4-runtime-editor-help';
           note.textContent = `当前目录项来自连接“${lockedProvider?.name || lockedProviderId}”；为避免读取错连接，此处已固定该连接。`;
           $('.v4-console-form-grid', modelForm)?.insertAdjacentElement('beforebegin', note);
         }
         if (modelForm && !lockedProviderId) {
           attachRuntimeOverflowSelector(modelForm.elements.provider_id, providers, '更多连接', providerOverflowOptions);
           if (!modelDraft.id) {
             const providerSelect = modelForm.elements.provider_id;
             const label = providerSelect.closest('label');
             if (label) {
               const fetchBox = document.createElement('div');
               fetchBox.className = 'v4-inline-actions v4-model-fetch-row';
               const fetchButton = document.createElement('button');
               fetchButton.type = 'button'; fetchButton.className = 'v4-secondary';
               fetchButton.dataset.v4ModelFetchCandidates = '';
               fetchButton.textContent = '获取此连接的模型候选';
               const selectedProviderTransport = () => {
                 const selected = providers.find((provider) => provider.id === providerSelect.value);
                 return selected ? String(selected.transport || '') : '';
               };
               const discoverySupported = () => !['codex_cli_chatgpt', 'codex_cli_custom_provider'].includes(selectedProviderTransport());
               const fetchHint = document.createElement('p');
               fetchHint.id = 'v4ModelFetchStatus'; fetchHint.className = 'v4-runtime-editor-help';
               fetchHint.setAttribute('role', 'status'); fetchHint.setAttribute('aria-live', 'polite');
               const updateFetchState = () => {
                 const supported = discoverySupported();
                 fetchButton.disabled = !providerSelect.value || !supported;
                 fetchHint.textContent = supported
                   ? '选择连接后，可从这里读取该连接上报的模型候选；候选只会预填表单，不会自动保存或改变路由。'
                   : '该连接是 Codex 登录/自定义 Provider，不支持读取模型列表；请手动填写模型信息。';
               };
               updateFetchState();
               providerSelect.addEventListener('change', updateFetchState);
               fetchBox.append(fetchButton, fetchHint);
               label.insertAdjacentElement('afterend', fetchBox);
             }
           }
         }
        if (modelForm && runtimeWorkspaceCore.canUseScopedDiscovery(modelDraft, runtimeUi.modelDiscovery)) {
          const discovery = runtimeUi.modelDiscovery;
           let discoveryModels = discovery.providerId === modelForm.elements.provider_id.value ? asList(discovery.models) : [];
           const discoveryPageCount = Math.max(1, Math.ceil(discoveryModels.length / runtimeUi.pageSize));
           const discoveryPage = Math.max(0, Math.min(discoveryPageCount - 1, Number(discovery.page) || 0));
           discoveryModels = discoveryModels.slice(discoveryPage * runtimeUi.pageSize, (discoveryPage + 1) * runtimeUi.pageSize);
           runtimeUi.modelDiscovery.page = discoveryPage;
           const selectedDiscoveryModel = discoveryModels.find((item) => item.id === discovery.selectedId) || null;
           const discoveryStatus = discovery.status || '候选来自当前已保存连接；不会自动新增目录项或改变角色路由。';
           $('.v4-console-form-grid', modelForm)?.insertAdjacentHTML('beforebegin', `<fieldset class="v4-model-discovery"><legend>已从连接获取的模型候选</legend><p>候选来自此连接的模型列表。选择后可先“隔离验证所选候选”确认可用性，或直接“填入表单”后保存；候选不会自动写入目录或改变路由。</p><div class="v4-inline-actions"><button class="v4-secondary" type="button" data-v4-discovered-model-validate ${selectedDiscoveryModel ? '' : 'disabled'}>隔离验证所选候选</button><button class="v4-secondary" type="button" data-v4-discovered-model-apply ${selectedDiscoveryModel ? '' : 'disabled'}>填入表单</button></div><label>已发现候选<select id="v4DiscoveredModel" size="7" ${discoveryModels.length ? '' : 'disabled'}>${discoveryModels.map((item) => { const exists = models.some((model) => model.provider_id === discovery.providerId && model.model === item.id); return `<option value="${esc(item.id)}" ${selectedDiscoveryModel?.id === item.id ? 'selected' : ''}>${esc(`${item.label || item.id} · ${exists ? '已有目录项' : '可作为新目录项导入'}`)}</option>`; }).join('')}</select></label><p id="v4ModelDiscoveryStatus" role="status" aria-live="polite">${esc(discoveryStatus)}</p></fieldset>`);
           const discoveryFieldset = $('.v4-model-discovery', modelForm);
           if (discoveryFieldset && discoveryPageCount > 1) {
             discoveryFieldset.insertAdjacentHTML('beforeend', `<nav class="v4-runtime-pagination" aria-label="模型候选分页"><button class="v4-secondary" type="button" data-v4-discovered-page="${discoveryPage - 1}" ${discoveryPage <= 0 ? 'disabled' : ''}>上一页</button><span>第 ${discoveryPage + 1} / ${discoveryPageCount} 页 · 共 ${discovery.models.length} 项</span><button class="v4-secondary" type="button" data-v4-discovered-page="${discoveryPage + 1}" ${discoveryPage + 1 >= discoveryPageCount ? 'disabled' : ''}>下一页</button></nav>`);
             all('[data-v4-discovered-page]', discoveryFieldset).forEach((button) => button.addEventListener('click', async () => {
               runtimeSessions.invalidate();
               runtimeUi.modelDiscovery.page = Number(button.dataset.v4DiscoveredPage);
               await renderPanel();
               $('#v4DiscoveredModel', modelForm)?.focus();
             }));
           }
           $('#v4DiscoveredModel', modelForm)?.addEventListener('change', (event) => {
            runtimeSessions.invalidate();
            runtimeUi.modelDiscovery.selectedId = event.currentTarget.value; runtimeUi.modelDiscovery.validation = null;
            $('[data-v4-discovered-model-validate]', modelForm).disabled = !event.currentTarget.value; $('[data-v4-discovered-model-apply]', modelForm).disabled = !event.currentTarget.value;
            const changeStatus = $('#v4ModelDiscoveryStatus', modelForm);
            if (changeStatus && event.currentTarget.value) changeStatus.textContent = '已选择候选：可直接“填入表单”，或先“隔离验证所选候选”确认可用性。';
          });
          $('[data-v4-discovered-model-validate]', modelForm)?.addEventListener('click', async (event) => {
            const button = event.currentTarget; const status = $('#v4ModelDiscoveryStatus', modelForm); const providerId = modelForm.elements.provider_id.value; const modelName = runtimeUi.modelDiscovery.selectedId;
            if (!modelName) return;
            button.disabled = true; status.textContent = '正在隔离验证所选候选；这会产生一次受限模型调用。';
            const session = runtimeSessions.begin({ kind: 'candidate-validation', id: `${providerId}:${modelName}` });
            await runtimeWorkspaceCore.runTerminal({
              write: () => api.validateDiscoveredModel(providerId, modelName),
              readback: () => api.models(),
              persist: persistCanonicalRuntimeRegistry,
              session,
              commit: async (terminal) => {
                const result = unpack(terminal.result, 'result');
                if (terminal.error || terminal.readbackError) {
                  const publicFailure = terminal.error || { payload: result || {} };
                  runtimeUi.modelDiscovery = { ...runtimeUi.modelDiscovery, validation: null, status: terminal.readbackError ? '验证后未能回读服务器状态；不会写入模型目录或路由。' : userError(publicFailure, '候选未通过隔离验证；不会写入模型目录或路由。') };
                } else {
                  runtimeUi.modelDiscovery = { ...runtimeUi.modelDiscovery, providerId, validation: result, status: result.ok ? '候选已通过隔离验证；可选择“填入表单”，再由你决定是否保存。' : '候选未通过隔离验证；不会写入模型目录或路由。' };
                }
                await renderPanel();
              },
            });
          });
          $('[data-v4-discovered-model-apply]', modelForm)?.addEventListener('click', () => {
            const item = (runtimeUi.modelDiscovery.models || []).find((candidate) => candidate.id === runtimeUi.modelDiscovery.selectedId);
            if (!item) return;
            modelForm.elements.model.value = item.id;
            if (!modelForm.elements.label.value.trim()) modelForm.elements.label.value = item.label || item.id;
            const specs = recommendModelSpecs(item.id);
            modelForm.elements.context_window.value = String(specs.context);
            modelForm.elements.max_output_tokens.value = String(specs.output);
            Object.entries(RECOMMENDED_CAPABILITY_INPUTS).forEach(([capability, inputName]) => {
              modelForm.elements[inputName].checked = specs.capabilities.includes(capability);
            });
            const status = $('#v4ModelDiscoveryStatus', modelForm);
            const idField = modelForm.elements.id;
            if (!idField.value.trim()) {
              const generated = slugifyModelId(modelForm.elements.provider_id.value, item.id);
              idField.value = generated;
              if (status) status.textContent = generated
                ? (specs.recommended
                  ? `候选已填入表单，目录 ID 已自动生成；已按${specs.reason}填入 上下文 ${specs.context} / 输出 ${specs.output}，能力已勾选（可手动调整）后点击“保存模型目录”完成添加。`
                  : `候选已填入表单，目录 ID 已自动生成；输出上限已设为保守默认 ${specs.output}，上下文未知（0），能力仅文本，可按实际规格调整后保存。`)
                : '候选已填入表单；目录 ID 无法自动生成，请手动填写后保存。';
            } else if (status) {
              status.textContent = '候选已填入表单；保存模型目录仍需要你明确提交。';
            }
          });
        }
        // The discovery controls are inserted above the lengthy catalog form.
        // Open the modal only after that insertion so a scoped candidate list
        // from the provider editor is available to keyboard users immediately.
        moveRuntimeEditorIntoDialog();
        const modelObservationEvents = $('#v4ConnectionModelObservationEvents', host);
        if (modelObservationEvents) {
          let lastObservationMarkup = '';
          const refreshModelObservation = async () => {
            if (modelObservationReading || document.hidden || !modelObservationEvents.isConnected) return;
            modelObservationReading = true;
            try {
              const response = await api.proxyModelObservation();
              if (renderEpoch !== runtimeRenderEpoch || !modelObservationEvents.isConnected) return;
              const events = asList(response?.events).slice(0, 10);
              const markup = events.length
                ? `<ol>${events.map((event) => {
                    const requested = String(event.requested_model || '未记录');
                    const reported = String(event.reported_model || '');
                    const comparison = reported && requested !== reported ? ' · 自报与请求不一致' : '';
                    const outcome = event.status === 'success' ? '成功' : `失败（${String(event.error_kind || '原因未记录')}）`;
                    return `<li><time>${esc(formatTime(event.created_at))}</time> · 请求模型：${esc(requested)} · 响应自报：${esc(reported || '未报告')} · ${esc(outcome)}${comparison}</li>`;
                  }).join('')}</ol>`
                : '<p>暂无这条连接的模型调用记录。</p>';
              if (markup !== lastObservationMarkup) {
                modelObservationEvents.innerHTML = markup;
                lastObservationMarkup = markup;
              }
            } catch (_error) {
              if (renderEpoch === runtimeRenderEpoch && modelObservationEvents.isConnected) {
                modelObservationEvents.textContent = '最近模型响应读取失败；当前状态未知。';
                lastObservationMarkup = '';
              }
            } finally {
              modelObservationReading = false;
            }
          };
          void refreshModelObservation();
          modelObservationTimer = window.setInterval(() => {
            if (!modelObservationEvents.isConnected || !document.contains(root)) {
              stopModelObservation();
              return;
            }
            void refreshModelObservation();
          }, 5000);
        }
        const connectionProxyStatus = () => $('#v4ConnectionProxyStatus', host);
        const collectConnectionProxyDraft = () => ({
          enabled: Boolean($('#v4ConnectionProxyEnabled', host)?.checked),
          subscription_key: String(proxyConnection?.subscription?.key || ''),
          group: 'Proxies',
          node: String($('#v4ConnectionProxyNode', host)?.value || ''),
          apply_intent: 'controlled_mihomo',
          form_version: runtimeUi.proxyConnectionFormVersion,
          expected_revision: Number(proxyManagement.revision || 0),
        });
        const runConnectionProxyAction = async (button, pending, operation, done) => {
          button.disabled = true;
          runtimeUi.proxyConnectionNotice = pending;
          if (connectionProxyStatus()) connectionProxyStatus().textContent = pending;
          try {
            const result = await operation();
            if (result?.ok === false) {
              const operationError = new Error(result.error || 'proxy_connection_operation_failed');
              operationError.payload = result;
              operationError.requestId = result.request_id || '';
              throw operationError;
            }
            await loadConnectionProxyData();
            runtimeUi.proxyConnectionNotice = done;
            await renderPanel();
            $(`#${CSS.escape(button.id)}`, host)?.focus();
          } catch (error) {
            const failed = error?.payload?.consumer;
            if (failed) consoleData.runtimeProxyConsumer = {...failed, _request_id: error?.payload?.request_id || error?.requestId || ''};
            const compensationKnown = Object.prototype.hasOwnProperty.call(error?.payload || {}, 'rolled_back');
            const compensation = compensationKnown
              ? (error.payload.rolled_back && !error.payload.rollback_error
                ? '配置、原 Selector 与可恢复运行态已恢复。'
                : `恢复未确认${error.payload.rollback_error ? `：${error.payload.rollback_error}` : '。'}`)
              : '';
            const rid = error?.payload?.request_id || error?.requestId || '';
            runtimeUi.proxyConnectionNotice = [
              userError(error, '受控出站操作失败；运行状态没有被当作成功。'),
              compensation,
              rid ? `Request ID ${rid}` : '',
            ].filter(Boolean).join(' ');
            if (connectionProxyStatus()) connectionProxyStatus().textContent = runtimeUi.proxyConnectionNotice;
            button.disabled = false;
          }
        };
        $('#v4ConnectionProxyNode', host)?.addEventListener('change', (event) => {
          runtimeUi.proxyConnectionNode = event.currentTarget.value;
          runtimeUi.proxyConnectionFormVersion = `connection-proxy-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        });
        $('#v4ConnectionProxyEnabled', host)?.addEventListener('change', (event) => {
          runtimeUi.proxyConnectionEnabled = event.currentTarget.checked;
          runtimeUi.proxyConnectionFormVersion = `connection-proxy-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        });
        $('#v4ConnectionProxySave', host)?.addEventListener('click', async (event) => runConnectionProxyAction(
          event.currentTarget,
          '正在保存 active subscription / Proxies node 引用…',
          () => api.saveProxyConsumer(collectConnectionProxyDraft()),
          '出站草稿已保存，并已从服务器回读。',
        ));
        $('#v4ConnectionProxyTest', host)?.addEventListener('click', async (event) => runConnectionProxyAction(
          event.currentTarget,
          '正在测试已保存的连接出站 revision…',
          () => api.testProxyConsumer({expected_revision: Number(proxyManagement.revision || 0)}),
          '连接测试完成，并已从服务器回读回执。',
        ));
        $('#v4ConnectionProxyApply', host)?.addEventListener('click', async (event) => {
          if (!window.confirm('应用这条连接已测试通过的受控出站配置？direct 与 fallback 会继续关闭。')) return;
          await runConnectionProxyAction(
            event.currentTarget,
            '正在应用固定 Proxies Selector 与连接运行配置…',
            () => api.applyProxyConsumer({expected_revision: Number(proxyManagement.revision || 0)}),
            '连接出站配置已应用，并已从服务器回读。',
          );
        });
        $('#v4ConnectionProxyRollback', host)?.addEventListener('click', async (event) => {
          if (!window.confirm('回滚这条连接的上一版受控出站配置？')) return;
          await runConnectionProxyAction(
            event.currentTarget,
            '正在原子回滚连接配置与 Proxies Selector…',
            () => api.rollbackProxyConsumer({expected_revision: Number(proxyManagement.revision || 0)}),
            '上一版连接出站配置已恢复，并已从服务器回读。',
          );
        });
        if (runtimeUi.modelDiscovery.focusDiscovery) {
          $('#v4DiscoveredModel', modelForm)?.focus();
          runtimeUi.modelDiscovery = { ...runtimeUi.modelDiscovery, focusDiscovery: false };
        }
        all('[data-v4-runtime-view]', host).forEach((button) => button.addEventListener('click', async () => {
          const nextView = button.dataset.v4RuntimeView;
          if (!['overview', 'connections', 'catalog', 'executors'].includes(nextView) || nextView === runtimeUi.activeSubview) return;
          runtimeSessions.invalidate();
          runtimeUi.activeSubview = nextView;
          runtimeUi.notice = '';
          await renderPanel();
          $(`[data-v4-runtime-view="${CSS.escape(nextView)}"]`, host)?.focus();
        }));
        all('[data-v4-provider-models]', host).forEach((button) => button.addEventListener('click', async () => {
          const providerId = button.dataset.v4ProviderModels;
          if (!providers.some((item) => item.id === providerId)) return;
          runtimeSessions.invalidate();
          runtimeUi.activeSubview = 'catalog';
          runtimeUi.modelProviderFilter = providerId;
          runtimeUi.modelFilter = '';
          runtimeUi.modelPage = 0;
          runtimeUi.notice = '';
          await renderPanel();
          $(`[data-v4-model-provider-filter="${CSS.escape(providerId)}"]`, host)?.focus();
        }));
        $('[data-v4-provider-filter]', host)?.addEventListener('change', async (event) => {
          runtimeUi.providerFilter = event.currentTarget.value;
          runtimeUi.providerPage = 0;
          await renderPanel();
          $('[data-v4-provider-filter]', host)?.focus();
        });
        $('[data-v4-model-filter]', host)?.addEventListener('change', async (event) => {
          runtimeUi.modelFilter = event.currentTarget.value;
          runtimeUi.modelPage = 0;
          await renderPanel();
          $('[data-v4-model-filter]', host)?.focus();
        });
        all('[data-v4-model-provider-filter]', host).forEach((button) => button.addEventListener('click', async () => {
          runtimeSessions.invalidate();
          runtimeUi.modelProviderFilter = button.dataset.v4ModelProviderFilter;
          runtimeUi.modelPage = 0;
          await renderPanel();
          $(`[data-v4-model-provider-filter="${CSS.escape(runtimeUi.modelProviderFilter)}"]`, host)?.focus();
        }));
        all('[data-v4-provider-page]', host).forEach((button) => button.addEventListener('click', async () => {
          runtimeUi.providerPage = Number(button.dataset.v4ProviderPage);
          await renderPanel();
          $('[data-v4-provider-filter]', host)?.focus();
        }));
        all('[data-v4-model-page]', host).forEach((button) => button.addEventListener('click', async () => {
          runtimeUi.modelPage = Number(button.dataset.v4ModelPage);
          await renderPanel();
          $('[data-v4-model-filter]', host)?.focus();
        }));
        for (const [kind, stateKey] of [['executor-login', 'executorLoginPage'], ['executor-profile', 'executorProfilePage'], ['executor-candidate', 'executorCandidatePage']]) {
          all(`[data-v4-${kind}-page]`, host).forEach((button) => button.addEventListener('click', async () => {
            runtimeSessions.invalidate();
            runtimeUi[stateKey] = Number(button.getAttribute(`data-v4-${kind}-page`));
            await renderPanel();
            $(`[data-v4-${kind}-page]:not([disabled])`, host)?.focus();
          }));
        }
        all('[data-v4-provider-discover]', host).forEach((button) => button.addEventListener('click', async () => {
          const provider = providers.find((item) => item.id === button.dataset.v4ProviderDiscover);
          if (!provider) return;
          const originView = runtimeUi.activeSubview;
          const status = $('#v4ProviderStatus', host);
          button.disabled = true;
          if (status) status.textContent = '正在从这条已保存连接获取模型候选…';
          const session = runtimeSessions.begin({ kind: 'provider-discovery', id: provider.id });
          await runtimeWorkspaceCore.runScopedDiscovery({
            providerId: provider.id,
            discover: (providerId) => api.discoverProviderModels(providerId),
            readback: () => api.models(),
            persist: persistCanonicalRuntimeRegistry,
            session,
            commit: async (terminal) => {
              const result = unpack(terminal.result, 'result');
              if (terminal.error || result?.ok === false || terminal.readbackError) {
                const publicFailure = terminal.error || { payload: result || {} };
                runtimeUi.notice = terminal.readbackError
                  ? '未能确认候选后的服务器状态；请刷新后再决定是否重试。'
                  : userError(publicFailure, '未能获取候选；请检查此连接的启用状态、协议与凭据。');
                await renderPanel();
                return;
              }
              runtimeUi.activeSubview = 'catalog';
              runtimeUi.providerDraft = null;
              runtimeUi.modelDraft = { provider_id: provider.id, enabled: 1, capabilities: ['text'], max_output_tokens: 4096, price_currency: 'USD' };
              runtimeUi.modelDiscovery = { providerId: provider.id, models: asList(result.models), page: 0, selectedId: '', validation: null, status: `已获取 ${Number(result.count || 0)} 个候选；选择候选后可隔离验证或预填草稿。`, focusDiscovery: true, lockedProviderId: provider.id };
              runtimeUi.dialogOrigin = { type: 'provider', id: provider.id, view: originView };
              runtimeUi.returnFocusId = '';
              await renderPanel();
            },
          });
        }));
        all('[data-v4-model-fetch-candidates]', host).forEach((button) => button.addEventListener('click', async () => {
          const providerId = modelForm.elements.provider_id.value;
          if (!providerId) return;
          const status = $('#v4ModelFetchStatus', modelForm);
          button.disabled = true;
          if (status) status.textContent = '正在从这条连接获取模型候选…';
          const session = runtimeSessions.begin({ kind: 'model-fetch-candidates', id: providerId });
          await runtimeWorkspaceCore.runScopedDiscovery({
            providerId,
            discover: (id) => api.discoverProviderModels(id),
            readback: () => api.models(),
            persist: persistCanonicalRuntimeRegistry,
            session,
            commit: async (terminal) => {
              const result = unpack(terminal.result, 'result');
              if (terminal.error || result?.ok === false || terminal.readbackError) {
                const publicFailure = terminal.error || { payload: result || {} };
                if (status) status.textContent = terminal.readbackError
                  ? '未能确认候选后的服务器状态；请刷新后再试。'
                  : userError(publicFailure, '未能获取候选；请检查此连接的启用状态、协议与凭据。');
                button.disabled = false;
                return;
              }
              runtimeUi.modelDiscovery = { providerId, models: asList(result.models), page: 0, selectedId: '', validation: null, status: `已获取 ${Number(result.count || 0)} 个候选；选择候选后可隔离验证或填入表单。`, focusDiscovery: true, lockedProviderId: providerId };
              runtimeUi.modelDraft = { ...runtimeUi.modelDraft, provider_id: providerId };
              await renderPanel();
            },
          });
        }));
        all('[data-v4-role-bind]', host).forEach((button) => button.addEventListener('click', async () => {
          const role = button.dataset.v4RoleBind; const primary = $(`[data-v4-role-primary="${CSS.escape(role)}"]`, host)?.value || ''; const fallback = $(`[data-v4-role-fallback="${CSS.escape(role)}"]`, host)?.value || '';
          const notice = $('#v4RuntimeStatus', host); button.disabled = true;
          try {
            if (role === 'work_executor') {
              if (!window.confirm('将短暂重启本地执行代理并把已验证候选切换为唯一工作执行器。运行中的工作会等待完成；继续吗？')) return;
              notice.textContent = '正在验证并切换工作执行器…';
              await finishRuntimeWrite({ write: () => api.activateWorkExecutor(primary), savedMessage: '工作执行器已切换，并已从服务器回读当前资格。', partialMessage: '工作执行器未切换；已从服务器回读当前资格。' });
            } else {
              notice.textContent = '正在保存角色路由…';
              await finishRuntimeWrite({ write: () => api.bindModelRole(role, primary, fallback), savedMessage: '模型角色已保存，并已从服务器回读当前资格。', partialMessage: '模型角色未保存；已从服务器回读当前资格。' });
            }
          } catch (error) { notice.textContent = userError(error); } finally { button.disabled = false; }
        }));
        all('[data-v4-model-test]', host).forEach((button) => button.addEventListener('click', async () => {
          const notice = $('#v4RuntimeStatus', host); button.disabled = true; notice.textContent = '正在验证模型…';
          try { await finishRuntimeWrite({ write: () => api.testModel(button.dataset.v4ModelTest), savedMessage: '模型验证通过，目录状态已从服务器回读。', partialMessage: '模型验证未通过，目录状态已从服务器回读。' }); } catch (error) { notice.textContent = userError(error); } finally { button.disabled = false; }
        }));
        all('[data-v4-executor-verify]', host).forEach((button) => button.addEventListener('click', async () => {
          const notice = $('#v4RuntimeStatus', host); button.disabled = true; notice.textContent = '正在进行隔离工作模式验证…';
          try { await finishRuntimeWrite({ write: () => api.verifyExecutor(button.dataset.v4ExecutorVerify), savedMessage: '隔离工作模式验证已通过，资格已从服务器回读。', partialMessage: '隔离工作模式验证未通过，资格已从服务器回读。' }); } catch (error) { notice.textContent = userError(error); } finally { button.disabled = false; }
        }));
        all('[data-v4-model-delete]', host).forEach((button) => button.addEventListener('click', async () => {
          const model = models.find((item) => item.id === button.dataset.v4ModelDelete); if (!model) return;
          if (!window.confirm(`删除模型目录项“${model.label || model.id}”？此操作不能撤销；当前角色路由不会被自动修改。`)) return;
          button.disabled = true;
          await finishRuntimeWrite({ write: () => api.deleteModelCatalog(model.id), savedMessage: '模型目录项已删除，并已从服务器回读。', partialMessage: '模型目录项未删除；已从服务器回读当前引用状态。', entity: { kind: 'model', id: model.id, action: 'delete' } });
        }));
        all('[data-v4-provider-delete]', host).forEach((button) => button.addEventListener('click', async () => {
          const provider = providers.find((item) => item.id === button.dataset.v4ProviderDelete); if (!provider) return;
          if (!window.confirm(`删除连接“${provider.name || provider.id}”？此操作不能撤销，也不会自动移除模型目录或角色路由。`)) return;
          button.disabled = true;
          await finishRuntimeWrite({ write: () => api.deleteModelProvider(provider.id), savedMessage: '连接已删除，并已从服务器回读。', partialMessage: '连接未删除；已从服务器回读当前关联状态。', entity: { kind: 'provider', id: provider.id, action: 'delete' } });
        }));
        $('[data-v4-provider-new]', host)?.addEventListener('click', async () => { runtimeSessions.invalidate(); runtimeUi.returnFocusId = 'v4RuntimeProviderNew'; runtimeUi.dialogOrigin = null; runtimeUi.providerDraft = { enabled: 1, timeout_seconds: 60, kind: 'openai-compatible', transport: 'openai_chat_completions', billing_scope: 'api_key' }; runtimeUi.modelDraft = null; runtimeUi.modelDiscovery = { providerId: '', models: [], page: 0, selectedId: '', validation: null, status: '', focusDiscovery: false, lockedProviderId: '' }; runtimeUi.notice = ''; await renderPanel(); });
        $('[data-v4-model-new]', host)?.addEventListener('click', async () => { runtimeSessions.invalidate(); runtimeUi.returnFocusId = 'v4RuntimeModelNew'; runtimeUi.dialogOrigin = null; runtimeUi.modelDraft = { provider_id: providers[0]?.id || '', enabled: 1, capabilities: ['text'], max_output_tokens: 4096, price_currency: 'USD' }; runtimeUi.providerDraft = null; runtimeUi.modelDiscovery = { providerId: '', models: [], page: 0, selectedId: '', validation: null, status: '', focusDiscovery: false, lockedProviderId: '' }; runtimeUi.notice = ''; await renderPanel(); });
        all('[data-v4-provider-edit]', host).forEach((button) => button.addEventListener('click', async () => {
          runtimeSessions.invalidate();
          const id = button.dataset.v4ProviderEdit;
          runtimeUi.dialogOrigin = { type: 'provider', id, view: runtimeUi.activeSubview };
          runtimeUi.returnFocusId = '';
          runtimeUi.providerDraft = providers.find((provider) => provider.id === id) || null;
          runtimeUi.modelDraft = null;
          runtimeUi.notice = '';
          runtimeUi.proxyConnectionNotice = '';
          runtimeUi.proxyConnectionNode = '';
          runtimeUi.proxyConnectionEnabled = null;
          runtimeUi.proxyConnectionFormVersion = '';
          if (id === 'aiclient2api-gemini-antigravity') {
            try {
              await loadConnectionProxyData();
            } catch (error) {
              const publicError = userError(error, '代理资产读取失败；写操作保持关闭。');
              consoleData.runtimeProxyConsumer = {_error: publicError, _request_id: error?.requestId || ''};
              consoleData.runtimeProxySubscriptions = {_error: publicError};
              consoleData.runtimeProxyGroups = {_error: publicError};
            }
          }
          await renderPanel();
        }));
        all('[data-v4-executor-create]', host).forEach((button) => button.addEventListener('click', async () => {
          runtimeSessions.invalidate();
          const upstream = models.find((model) => model.id === button.dataset.v4ExecutorCreate);
          if (!upstream) return;
          const existingIds = new Set(providers.map((provider) => String(provider.id || '')));
          const baseId = `${String(upstream.provider_id || 'executor').slice(0, 54)}-executor`;
          let providerId = baseId;
          for (let suffix = 2; existingIds.has(providerId); suffix += 1) providerId = `${baseId.slice(0, 61 - String(suffix).length)}-${suffix}`;
          runtimeUi.providerDraft = {
            id: providerId,
            name: `${recordTitle(upstream, ['provider_name', 'provider_id'])} 工作执行适配器`,
            kind: 'codex', transport: 'codex_cli_custom_provider', billing_scope: 'local_proxy',
            base_url: '', enabled: 1, trusted_for_executor: 1, timeout_seconds: 60,
            executor_profile: {
              profile_name: providerId, enabled: 1,
              upstream_provider_id: upstream.provider_id, upstream_model_id: upstream.id,
            },
          };
          runtimeUi.dialogOrigin = null; runtimeUi.returnFocusId = `v4ExecutorCreate-${upstream.id}`; runtimeUi.modelDraft = null; runtimeUi.notice = ''; await renderPanel();
        }));
        all('[data-v4-model-edit]', host).forEach((button) => button.addEventListener('click', async () => { runtimeSessions.invalidate(); const id = button.dataset.v4ModelEdit; runtimeUi.dialogOrigin = { type: 'model', id, view: runtimeUi.activeSubview }; runtimeUi.returnFocusId = ''; runtimeUi.modelDraft = models.find((model) => model.id === id) || null; runtimeUi.providerDraft = null; runtimeUi.notice = ''; await renderPanel(); }));
        $('[data-v4-provider-cancel]', host)?.addEventListener('click', async () => { runtimeSessions.invalidate(); const focusId = runtimeUi.returnFocusId; const origin = runtimeUi.dialogOrigin; runtimeUi.providerDraft = null; runtimeUi.dialogOrigin = null; runtimeUi.returnFocusId = ''; if (origin?.view) runtimeUi.activeSubview = origin.view; await renderPanel(); focusRuntimeReturn(focusId, origin); });
        $('[data-v4-model-cancel]', host)?.addEventListener('click', async () => { runtimeSessions.invalidate(); const focusId = runtimeUi.returnFocusId; const origin = runtimeUi.dialogOrigin; runtimeUi.modelDraft = null; runtimeUi.modelDiscovery = { providerId: '', models: [], page: 0, selectedId: '', validation: null, status: '', focusDiscovery: false, lockedProviderId: '' }; runtimeUi.dialogOrigin = null; runtimeUi.returnFocusId = ''; if (origin?.view) runtimeUi.activeSubview = origin.view; await renderPanel(); focusRuntimeReturn(focusId, origin); });
        $('#v4ProviderForm', host)?.addEventListener('submit', async (event) => {
          event.preventDefault(); const form = event.currentTarget; const submit = $('button[type="submit"]', form); const notice = $('#v4ProviderStatus', form); const fields = form.elements;
          submit.disabled = true; notice.textContent = '正在保存连接与 Executor Profile…';
          try {
            const draft = { id: fields.id.value.trim(), name: fields.name.value.trim(), kind: fields.kind.value, transport: fields.transport.value, billing_scope: fields.billing_scope.value, base_url: fields.base_url.value.trim(), api_key: fields.api_key.value, clear_api_key: fields.clear_api_key.checked ? '1' : '0', timeout_seconds: Number(fields.timeout_seconds.value || 60), enabled: fields.enabled.checked ? '1' : '0', trusted_for_executor: fields.trusted_for_executor.checked ? '1' : '0', executor_profile_name: fields.executor_profile_name.value.trim(), executor_enabled: fields.executor_enabled.checked ? '1' : '0', executor_adapter_type: 'codex_cli_profile', executor_credential_source: 'proxy_access_key', executor_upstream_provider_id: fields.executor_upstream_provider_id.value, executor_upstream_model_id: fields.executor_upstream_model_id.value };
            await finishRuntimeWrite({ write: () => api.saveModelProvider(draft), savedMessage: '连接与 Profile 已保存，并已从服务器回读。', partialMessage: '连接与 Profile 已保存，但 Executor Profile 未成功应用；工作执行仍保持服务端拒绝。', closeDraft: 'provider' });
          } catch (error) { notice.textContent = userError(error); } finally { submit.disabled = false; }
        });
        $('#v4ModelForm', host)?.addEventListener('submit', async (event) => {
          event.preventDefault(); const form = event.currentTarget; const submit = $('button[type="submit"]', form); const notice = $('#v4ModelStatus', form); const fields = form.elements;
          submit.disabled = true; notice.textContent = '正在保存模型目录…';
          try {
            const capabilities = [['capability_text', 'text'], ['capability_tools', 'tools'], ['capability_vision', 'vision'], ['capability_structured', 'structured_output'], ['capability_embedding', 'embedding']].filter(([name]) => fields[name].checked).map(([, capability]) => capability);
             const draft = { id: fields.id.value.trim(), provider_id: fields.provider_id.value, label: fields.label.value.trim(), model: fields.model.value.trim(), context_window: Number(fields.context_window.value || 0), max_output_tokens: Number(fields.max_output_tokens.value || 900), capabilities, enabled: fields.enabled.checked ? '1' : '0', notes: fields.notes.value.trim(), input_price_per_million: fields.input_price_per_million.value, output_price_per_million: fields.output_price_per_million.value, price_currency: fields.price_currency.value.trim() || 'USD', price_source: fields.price_source.value.trim() };
            await finishRuntimeWrite({ write: () => api.saveModelCatalog(draft), savedMessage: '模型目录已保存，并已从服务器回读。', partialMessage: '模型目录已保存，但关联 Profile 未成功应用；工作执行仍保持服务端拒绝。', closeDraft: 'model' });
          } catch (error) { notice.textContent = userError(error); } finally { submit.disabled = false; }
        });
      }
      if (active === 'capabilities') { const plugins = asList(unpack(consoleData.plugins, 'plugins')); host.innerHTML = `<section class="v4-console-section"><p>Capability</p><h2>能力与插件</h2><p class="v4-note">这里仅管理已安装插件的受控启停。自定义 Skill、插件市场安装与更新需要独立的审计和风险确认，当前不在此页开放。</p>${cards(plugins, (plugin) => `<article class="v4-plugin-item"><div><strong>${esc(recordTitle(plugin, ['display_name', 'name', 'id']))}</strong><span>${esc(plugin.description || '')}${plugin.protected ? ' · 受保护，不可在 Console 停用' : ''}</span></div><button class="v4-secondary" ${plugin.protected ? 'disabled aria-describedby="v4CapabilityStatus"' : ''} type="button" data-v4-plugin-toggle="${esc(plugin.id)}" data-v4-plugin-enabled="${plugin.enabled ? '0' : '1'}">${plugin.enabled ? '停用此插件' : '启用此插件'}</button></article>`) }<p id="v4CapabilityStatus" role="status" aria-live="polite">${esc(runtimeUi.capabilityNotice)}</p></section>`; all('[data-v4-plugin-toggle]', host).forEach((button) => button.addEventListener('click', async () => { if (!window.confirm('确认修改这个插件的启用状态？相关服务可能短暂重连。')) return; button.disabled = true; try { await api.togglePlugin(button.dataset.v4PluginToggle, button.dataset.v4PluginEnabled); await refreshPlugins('插件状态已更新，并已从服务器回读。'); await renderPanel(); } catch (error) { button.textContent = userError(error); button.disabled = false; } })); }
      if (active === 'qqInfrastructure') {
        const diagnostics = unpack(consoleData.diagnostics, 'result');
        const qqStatus = pick(diagnostics, ['qq_status', 'status'], '暂时无法读取状态');
        const adapter = pick(diagnostics, ['adapter_label', 'adapter_id', 'adapter'], '未识别');
        const checks = asList(diagnostics.checks);
        const loginRequired = diagnostics.needs_login === true || qqStatus === 'login_required';
        const offline = diagnostics.service_active === false || qqStatus === 'offline';
        const qrcodeAvailable = diagnostics.qrcode_available === true && diagnostics.qrcode_url;
        const qrcodeUrl = qrcodeAvailable ? api.qqLoginQrcodeUrl(diagnostics.qrcode_mtime || Date.now()) : '';
        const statusLabel = ({ online: '已连接', login_required: '等待扫码登录', offline: '适配器未运行', unknown: '状态待确认' })[qqStatus] || qqStatus;
        const checkMarkup = checks.length ? `<ul class="v4-check-list">${checks.map(item => `<li><strong>${esc(item.label || item.name)}</strong><span>${item.ok ? '正常' : '未就绪'}</span></li>`).join('')}</ul>` : '';
        const loginMarkup = (loginRequired || offline) ? `<article class="v4-qq-login-panel"><div><strong>${offline ? '恢复 QQ 连接' : 'QQ 扫码登录'}</strong><span>${esc(diagnostics.recommendation || '启动适配器后会在这里显示一次性登录二维码。')}</span><small>二维码只在当前已认证的管理会话中读取；系统不保存或代填 QQ 密码。</small></div>${qrcodeAvailable ? `<img src="${esc(qrcodeUrl)}" alt="QQ 登录二维码" width="240" height="240">` : '<p class="v4-note">当前没有可用的新二维码。启动或刷新后，本页会自动回读登录状态。</p>'}<div class="v4-inline-actions"><button type="button" class="v4-primary" data-v4-qq-login-refresh>${offline ? '启动并检查登录' : '刷新登录二维码'}</button><p id="v4QqLoginStatus" role="status" aria-live="polite"></p></div></article>` : '<article><strong>QQ 登录</strong><span>当前登录、OneBot 与 Bridge 链路已确认。</span></article>';
        host.innerHTML = `<section class="v4-console-section"><p>QQ infrastructure</p><h2>连接与基础设施</h2><div class="v4-operator-grid"><article><strong>QQ 状态</strong><span>${esc(statusLabel)}</span><small>接入：${esc(adapter)} · 服务：${esc(diagnostics.service_status || 'unknown')}</small></article><article><strong>操作边界</strong><span>这里只恢复渠道连接与登录；准入、群参与和投递操作仍在 QQ 页面完成。</span></article></div>${checkMarkup}${loginMarkup}</section>`;
        $('[data-v4-qq-login-refresh]', host)?.addEventListener('click', async (event) => {
          if (!window.confirm('这会重启 QQ 适配器并生成新的登录状态。继续吗？')) return;
          const button = event.currentTarget; const notice = $('#v4QqLoginStatus', host); button.disabled = true; notice.textContent = '正在启动适配器并等待登录状态…';
          try {
            const response = await api.refreshQqLogin();
            const canonical = response?.diagnostics || response?.result?.diagnostics;
            if (canonical && typeof canonical === 'object') consoleData.diagnostics = canonical;
            loadedConsole.delete('qqInfrastructure');
            await renderPanel();
          } catch (error) {
            notice.textContent = userError(error, 'QQ 登录状态刷新失败。'); button.disabled = false;
          }
        });
      }
      if (active === 'network') {
        const policy = unpack(consoleData.network, 'policy');
        const subscriptions = safe(consoleData.proxySubscriptions);
        const groupData = safe(consoleData.proxyGroups);
        const managed = asList(subscriptions.managed);
        const groups = asList(groupData.groups);
        const subscriptionRevision = Number(subscriptions.management_revision || 0);
        const activeSubscription = managed.find((item) => item.active && item.enabled) || null;
        const leafNodes = (group) => asList(group?.nodes).filter((item) => !['DIRECT', 'REJECT'].includes(String(item.name || '').toUpperCase()));
        const proxiesGroup = groups.find((item) => item.name === 'Proxies') || {};
        const baseMode = pick(policy, ['base_mode'], '暂时无法读取');
        const baseModeLabel = baseMode === 'capability_only' ? '按能力使用代理' : baseMode;
        const networkViews = [['overview', '概览'], ['assets', '订阅与节点']];
        const dependencyLabels = {current_selector: '当前 Proxies Selector', only_enabled_subscription: '唯一启用代理链'};
        const dependenciesFor = (item) => [...new Set(asList(item.dependencies))];
        const subscriptionErrorText = runtimeWorkspaceCore.proxySubscriptionErrorText;
        const regionLabel = (name) => (String(name || '').match(/香港|日本|美国|新加坡|韩国|德国|台湾|英国|法国|加拿大|澳大利亚/) || ['未标注'])[0];
        const latestDelay = (node) => {
          const measured = runtimeUi.proxyDelayResults[String(node?.name || '')];
          if (measured?.ok && Number(measured.delay) >= 0) return Number(measured.delay);
          const direct = Number(node?.delay);
          if (direct > 0) return direct;
          const history = asList(node?.history);
          for (let index = history.length - 1; index >= 0; index -= 1) {
            const delay = Number(history[index]?.delay);
            if (delay > 0) return delay;
          }
          return null;
        };
        const proxyNodes = leafNodes(proxiesGroup).slice(0, 80);
        const overviewPanel = `<div class="v4-proxy-workspace"><section class="v4-proxy-hero"><div><p>通用 Mihomo Proxy Assets</p><h3>${activeSubscription ? esc(activeSubscription.name || activeSubscription.key) : '未设置当前订阅'}</h3><span>Network Policy 与服务器代理资产在此统一维护；连接消费者在对应 Runtime Connection 详情中配置。</span></div><button id="v4ProxyGoAssets" class="v4-primary" type="button">管理代理资产</button></section><section class="v4-proxy-panel"><header><div><p>运行摘要</p><h3>当前 Proxies Selector</h3></div><span>${managed.filter((item) => item.enabled).length} 个启用订阅</span></header><dl class="v4-proxy-summary-list"><div><dt>当前订阅</dt><dd>${esc(activeSubscription?.name || activeSubscription?.key || '未设置')}</dd></div><div><dt>当前节点</dt><dd>${esc(proxiesGroup.now || '未选择')}</dd></div><div><dt>可选节点</dt><dd>${esc(proxyNodes.length)}</dd></div></dl></section></div>`;

        const subscriptionRows = managed.map((item) => {
          const dependencies = dependenciesFor(item);
          const blocked = dependencies.length > 0;
          const reference = dependencies.map((value) => dependencyLabels[value] || value).join('、') || '无';
          return `<tr${item.active ? ' aria-current="true"' : ''}><td><strong>${esc(item.name || item.key)}</strong></td><td>${item.active ? '当前订阅' : item.enabled ? '已启用' : '已停用'}</td><td>${esc(item.node_count ?? 0)}</td><td>${esc(item.updated_at || '未记录')}</td><td>${esc(reference)}</td><td><div class="v4-table-actions"><button id="v4ProxySubscriptionEdit-${esc(item.key)}" class="v4-secondary" type="button" data-v4-proxy-sub-edit="${esc(item.key)}">编辑</button><button class="v4-secondary" type="button" data-v4-proxy-sub-action="refresh" data-v4-proxy-sub-key="${esc(item.key)}">刷新</button><button class="v4-secondary" type="button" data-v4-proxy-sub-action="switch" data-v4-proxy-sub-key="${esc(item.key)}" ${item.active || !item.enabled ? 'disabled' : ''}>${item.active ? '当前订阅' : '设为当前'}</button><button class="v4-secondary" type="button" data-v4-proxy-sub-action="${item.enabled ? 'disable' : 'enable'}" data-v4-proxy-sub-key="${esc(item.key)}" ${item.enabled && blocked ? 'disabled' : ''}>${item.enabled ? '停用' : '启用'}</button><button class="v4-secondary" type="button" data-v4-proxy-sub-action="delete" data-v4-proxy-sub-key="${esc(item.key)}" ${blocked ? 'disabled' : ''}>删除</button></div></td></tr>`;
        }).join('');
        const subscriptionsPanel = `<section class="v4-proxy-panel"><header><div><p>代理资产</p><h3>订阅</h3><p>完整 URL 不回读；编辑时留空表示保留受保护地址。</p></div><button id="v4ProxySubscriptionNew" class="v4-primary" type="button">新增订阅</button></header><div class="v4-table-scroll" role="region" aria-label="代理订阅表" tabindex="0"><table class="v4-proxy-table"><thead><tr><th>订阅</th><th>状态</th><th>节点数</th><th>最近刷新</th><th>当前引用</th><th>操作</th></tr></thead><tbody>${subscriptionRows || '<tr><td colspan="6">尚未添加托管订阅。</td></tr>'}</tbody></table></div><p id="v4ProxyAssetStatus" class="v4-proxy-status-line" role="status" aria-live="polite">${esc(runtimeUi.subscriptionNotice)}</p></section>`;
        const nodeRows = proxyNodes.map((node) => {
          const current = String(node.name || '') === String(proxiesGroup.now || '');
          const delay = latestDelay(node);
          const measured = runtimeUi.proxyDelayResults[String(node.name || '')];
          const delayLabel = measured && !measured.ok ? '失败' : delay !== null ? `${esc(delay)} ms` : '—';
          const basicStatus = current ? '当前节点' : node.alive === false ? '异常' : '可选';
          return `<tr${current ? ' aria-current="true" class="selected"' : ''}><td><strong>${esc(node.name)}</strong></td><td>${delayLabel}</td><td>${esc(regionLabel(node.name))}</td><td>${esc(basicStatus)}</td><td><div class="v4-table-actions"><button class="v4-secondary" type="button" data-v4-proxy-node-delay="${esc(node.name)}">测延迟</button><button class="v4-secondary" type="button" data-v4-proxy-node-select="${esc(node.name)}" ${!activeSubscription || current ? 'disabled' : ''}>${current ? '当前节点' : '设为当前节点'}</button></div></td></tr>`;
        }).join('');
        const nodesPanel = `<section id="v4ProxyNodesPanel" class="v4-proxy-panel"><header><div><p>通用 Mihomo Proxy Assets</p><h3>当前 Proxies Selector</h3><p>这里只切换服务器 canonical Selector；连接详情只引用这里的节点，不复制 inventory。</p><p class="v4-note">基础延迟只表示通用网络连通性，不代表 Text 或 Vision 模型连接可用。</p></div><div class="v4-inline-actions"><button id="v4ProxyDelayVisible" class="v4-secondary" type="button" ${proxyNodes.length ? '' : 'disabled'}>测试当前列表</button><button id="v4ProxyRefresh" class="v4-secondary" type="button">刷新节点状态</button></div></header><div class="v4-table-scroll" role="region" aria-label="Proxies 节点表" tabindex="0"><table class="v4-proxy-table v4-proxy-node-table"><thead><tr><th>节点</th><th>延迟</th><th>地区</th><th>基础状态</th><th>操作</th></tr></thead><tbody>${nodeRows || '<tr><td colspan="5">当前 Proxies Selector 没有可用节点。</td></tr>'}</tbody></table></div><p id="v4ProxyNodeStatus" class="v4-proxy-status-line" role="status" aria-live="polite">${esc(runtimeUi.nodeNotice || `当前节点：${proxiesGroup.now || '未选择'}`)}</p></section>`;
        const activePanel = runtimeUi.networkSubview === 'assets' ? `${subscriptionsPanel}${nodesPanel}` : overviewPanel;
        const subscriptionDraft = runtimeUi.subscriptionDraft;
        const subscriptionDialog = subscriptionDraft ? `<dialog id="v4ProxySubscriptionDialog" class="v4-proxy-dialog" aria-labelledby="v4ProxySubscriptionDialogHeading"><form id="v4ProxySubscriptionForm" class="v4-runtime-editor" method="dialog"><div><p>${subscriptionDraft.mode === 'create' ? '新增代理资产' : '编辑代理资产'}</p><h3 id="v4ProxySubscriptionDialogHeading">${subscriptionDraft.mode === 'create' ? '新增订阅' : `编辑 ${esc(subscriptionDraft.savedName || subscriptionDraft.name)}`}</h3><span>${subscriptionDraft.mode === 'update' ? '当前地址受保护且不会回读；留空将保留原地址。' : '新订阅必须提供合法的 HTTPS/HTTP 地址。'}</span></div><div class="v4-console-form-grid"><label>名称<input name="name" required maxlength="64" autocomplete="off" value="${esc(subscriptionDraft.name || '')}"></label><label>新订阅 URL<input id="v4ProxySubscriptionUrl" name="url" type="password" ${subscriptionDraft.mode === 'create' ? 'required' : ''} autocomplete="new-password" spellcheck="false" placeholder="${subscriptionDraft.mode === 'create' ? '新增时必填' : '留空保留受保护地址'}"></label><label class="v4-check"><input name="enabled" type="checkbox" ${subscriptionDraft.enabled ? 'checked' : ''}>启用订阅</label></div><div class="v4-inline-actions"><button class="v4-primary" type="submit">保存</button><button id="v4ProxySubscriptionCancel" class="v4-secondary" type="button">取消</button></div><p id="v4ProxyDialogStatus" class="v4-proxy-status-line" role="status" aria-live="polite">${esc(runtimeUi.subscriptionNotice)}</p></form></dialog>` : '';
        host.innerHTML = `<section class="v4-console-section"><p>Network & proxy management</p><h2>网络与代理</h2><p class="v4-note">NekoAgent Capability Policy 控制 Bridge 已注册的网络能力；这里只管理 Network Policy 与通用 Mihomo Proxy Assets。连接消费者的保存、测试、应用与回滚位于对应 Runtime Connection 详情。</p><nav class="v4-local-nav v4-proxy-local-nav" aria-label="网络与代理管理区">${networkViews.map(([id, label]) => `<button type="button" data-v4-network-view="${id}" aria-current="${runtimeUi.networkSubview === id ? 'page' : 'false'}">${label}</button>`).join('')}</nav><div class="v4-operator-grid v4-proxy-policy-strip"><article><strong>NekoAgent Capability Policy</strong><span>${esc(baseModeLabel)}</span><small>内部策略：${esc(baseMode)} · Owner Web Search：${policy.owner_web_search_active ? '临时开启' : '未开启'}</small></article><article><strong>通用 Mihomo Proxy Assets</strong><span>${esc(activeSubscription?.name || activeSubscription?.key || '未设置当前订阅')}</span><small>当前 Proxies Selector：${esc(proxiesGroup.now || '未选择')}</small></article></div>${activePanel}${subscriptionDialog}</section>`;

        const reloadNetwork = async () => {
          const [nextPolicy, nextSubscriptions, nextGroups] = await Promise.all([api.network(), api.proxySubscriptions(), api.proxyGroups()]);
          consoleData.network = nextPolicy;
          consoleData.proxySubscriptions = nextSubscriptions;
          consoleData.proxyGroups = nextGroups;
          loadedConsole.add('network');
          await renderPanel();
        };
        const statusText = (id, text) => { const node = $(id, host); if (node) node.textContent = text; };
        all('[data-v4-network-view]', host).forEach((button) => button.addEventListener('click', async () => {
          if (runtimeUi.subscriptionDraft?.url && !window.confirm('切换页面将丢弃尚未提交的新订阅地址，是否继续？')) return;
          runtimeUi.networkSubview = button.dataset.v4NetworkView;
          await renderPanel();
        }));
        $('#v4ProxyGoAssets', host)?.addEventListener('click', async () => { runtimeUi.networkSubview = 'assets'; await renderPanel(); $('#v4ProxySubscriptionNew', host)?.focus(); });
        $('#v4ProxySubscriptionNew', host)?.addEventListener('click', async () => { runtimeUi.subscriptionNotice = ''; runtimeUi.subscriptionDraft = {mode: 'create', key: '', name: '', url: '', enabled: true, expected_revision: subscriptionRevision, returnFocusId: 'v4ProxySubscriptionNew'}; await renderPanel(); });
        all('[data-v4-proxy-sub-edit]', host).forEach((button) => button.addEventListener('click', async () => {
          const item = managed.find((entry) => entry.key === button.dataset.v4ProxySubEdit);
          if (!item) return;
          runtimeUi.subscriptionNotice = '';
          runtimeUi.subscriptionDraft = {mode: 'update', key: item.key, name: item.name || item.key, savedName: item.name || item.key, url: '', enabled: Boolean(item.enabled), expected_revision: subscriptionRevision, returnFocusId: `v4ProxySubscriptionEdit-${item.key}`};
          await renderPanel();
        }));
        const dialog = $('#v4ProxySubscriptionDialog', host);
        if (dialog && !dialog.open) dialog.showModal();
        const subscriptionForm = $('#v4ProxySubscriptionForm', host);
        const dismissSubscriptionDialog = async () => {
          if ((runtimeUi.subscriptionDraft?.url || runtimeUi.subscriptionDraft?.mode === 'create') && !window.confirm('放弃当前未保存的订阅编辑？')) return;
          const returnFocusId = runtimeUi.subscriptionDraft?.returnFocusId || 'v4ProxySubscriptionNew';
          runtimeUi.subscriptionDraft = null;
          runtimeUi.subscriptionNotice = '';
          await renderPanel();
          $(`#${CSS.escape(returnFocusId)}`, host)?.focus();
        };
        dialog?.addEventListener('cancel', (event) => { event.preventDefault(); void dismissSubscriptionDialog(); }, {once: true});
        subscriptionForm?.addEventListener('input', () => {
          if (!runtimeUi.subscriptionDraft) return;
          runtimeUi.subscriptionDraft.name = subscriptionForm.elements.name.value;
          runtimeUi.subscriptionDraft.url = subscriptionForm.elements.url.value;
          runtimeUi.subscriptionDraft.enabled = subscriptionForm.elements.enabled.checked;
        });
        subscriptionForm?.addEventListener('submit', async (event) => {
          event.preventDefault();
          const draft = runtimeUi.subscriptionDraft;
          if (!draft) return;
          const submit = $('button[type="submit"]', subscriptionForm);
          submit.disabled = true;
          draft.name = subscriptionForm.elements.name.value.trim();
          draft.url = subscriptionForm.elements.url.value.trim();
          draft.enabled = subscriptionForm.elements.enabled.checked;
          try {
            if (draft.mode === 'create') await api.createProxySubscription({name: draft.name, url: draft.url, enabled: draft.enabled, expected_revision: subscriptionRevision});
            else await api.updateProxySubscription({key: draft.key, name: draft.name, url_update_present: Boolean(draft.url), ...(draft.url ? {url: draft.url} : {}), enabled: draft.enabled, expected_revision: subscriptionRevision});
            runtimeUi.subscriptionDraft = null;
            runtimeUi.subscriptionNotice = '已保存并从服务器重新读取权威状态。';
            await reloadNetwork();
          } catch (error) {
            runtimeUi.subscriptionNotice = subscriptionErrorText(error);
            statusText('#v4ProxyDialogStatus', runtimeUi.subscriptionNotice);
            submit.disabled = false;
          }
        });
        $('#v4ProxySubscriptionCancel', host)?.addEventListener('click', dismissSubscriptionDialog);
        all('[data-v4-proxy-sub-action]', host).forEach((button) => button.addEventListener('click', async () => {
          const action = button.dataset.v4ProxySubAction;
          const key = button.dataset.v4ProxySubKey;
          const item = managed.find((entry) => entry.key === key) || {};
          const dependencies = dependenciesFor(item);
          if (['disable', 'delete'].includes(action) && dependencies.length) {
            runtimeUi.subscriptionNotice = '该订阅正在被当前代理配置使用。请先切换到其他订阅或停用受控出站。';
            statusText('#v4ProxyAssetStatus', runtimeUi.subscriptionNotice);
            return;
          }
          if (action === 'delete' && !window.confirm(`删除订阅“${item.name || key}”及其 ${item.node_count || 0} 个派生节点？现有引用不会被级联改写。`)) return;
          if (action === 'disable' && !window.confirm(`停用订阅“${item.name || key}”？`)) return;
          button.disabled = true;
          try {
            await api.operateProxySubscription(action, key, subscriptionRevision);
            runtimeUi.subscriptionNotice = action === 'refresh' ? '刷新完成，已从服务器回读节点。' : '操作完成，已从服务器回读权威状态。';
            await reloadNetwork();
          } catch (error) {
            runtimeUi.subscriptionNotice = subscriptionErrorText(error);
            statusText('#v4ProxyAssetStatus', runtimeUi.subscriptionNotice);
            button.disabled = false;
          }
        }));
        all('[data-v4-proxy-node-select]', host).forEach((button) => button.addEventListener('click', async () => {
          if (!activeSubscription) {
            runtimeUi.nodeNotice = '请先设定一个已启用的当前订阅。';
            statusText('#v4ProxyNodeStatus', runtimeUi.nodeNotice);
            return;
          }
          button.disabled = true;
          const node = button.dataset.v4ProxyNodeSelect;
          try {
            await api.selectProxyNode(activeSubscription.key, node, subscriptionRevision);
            runtimeUi.nodeNotice = `当前 Proxies Selector 已切换为 ${node}。`;
            await reloadNetwork();
          } catch (error) {
            runtimeUi.nodeNotice = subscriptionErrorText(error);
            statusText('#v4ProxyNodeStatus', runtimeUi.nodeNotice);
            button.disabled = false;
          }
        }));
        const runProxyDelay = async (button, names) => {
          button.disabled = true;
          runtimeUi.nodeNotice = names.length === 1 ? `正在测试 ${names[0]} 的基础延迟…` : `正在测试当前列表中的 ${names.length} 个节点…`;
          statusText('#v4ProxyNodeStatus', runtimeUi.nodeNotice);
          try {
            const result = await api.delayProxyNodes('Proxies', names);
            if (result?.ok === false) {
              const delayError = new Error(result.error || 'proxy_delay_failed');
              delayError.payload = result;
              throw delayError;
            }
            asList(result?.results).forEach((item) => {
              const name = String(item?.name || '');
              if (name) runtimeUi.proxyDelayResults[name] = {...item};
            });
            const passed = asList(result?.results).filter((item) => item?.ok).length;
            runtimeUi.nodeNotice = `基础延迟测试完成：${passed}/${names.length} 个节点返回结果；没有切换当前节点。`;
            await renderPanel();
          } catch (error) {
            runtimeUi.nodeNotice = userError(error, '基础延迟测试失败；当前节点没有切换。');
            statusText('#v4ProxyNodeStatus', runtimeUi.nodeNotice);
            button.disabled = false;
          }
        };
        all('[data-v4-proxy-node-delay]', host).forEach((button) => button.addEventListener('click', () => runProxyDelay(button, [button.dataset.v4ProxyNodeDelay])));
        $('#v4ProxyDelayVisible', host)?.addEventListener('click', (event) => runProxyDelay(event.currentTarget, proxyNodes.map((node) => String(node.name || '')).filter(Boolean)));
        $('#v4ProxyRefresh', host)?.addEventListener('click', async (event) => { event.currentTarget.disabled = true; try { await reloadNetwork(); } catch (error) { runtimeUi.nodeNotice = userError(error, '节点状态刷新失败。'); statusText('#v4ProxyNodeStatus', runtimeUi.nodeNotice); event.currentTarget.disabled = false; } });
        return;
      }      if (active === 'reliability') { const deadLetters = records(safe(consoleData.reliability), 'dead_letters'); const services = records(safe(consoleData.services), 'services'); const healthy = services.filter((service) => service.ok).length; const serviceSummary = services.length ? `${healthy}/${services.length} 项服务状态正常` : '暂无可读取的服务状态'; host.innerHTML = `<section class="v4-console-section"><p>Reliability</p><h2>服务与可靠性</h2><div class="v4-operator-grid"><article><strong>服务状态</strong><span>${esc(serviceSummary)}</span></article><article><strong>待恢复投递</strong><span>${esc(deadLetters.length)}</span></article></div>${cards(deadLetters, (item) => `<article class="v4-dead-letter"><div><strong>${esc(recordTitle(item, ['summary', 'id', 'last_error']))}</strong><span>需确认后才可重新处理。</span></div><button type="button" class="v4-secondary" data-v4-route="qq">前往 QQ 投递处理</button></article>`)}</section>`; }
      if (active === 'diagnostics') { const rawLog = unpack(safe(consoleData.logs), 'output'); const logs = typeof rawLog === 'string' ? rawLog.split(/\r?\n/).filter(Boolean) : asList(rawLog); const pageFault = state.pageFault; const pageFaultMarkup = pageFault ? `<article class="v4-operator-grid"><strong>最近前端页面错误</strong><span>页面：${esc(pageFault.route)} · 分类：${esc(pageFault.code)} · 时间：${esc(formatTime(pageFault.occurredAt))}</span></article>` : '<p class="v4-note">本浏览器本次会话尚未记录页面加载错误。</p>'; host.innerHTML = `<section class="v4-console-section"><p>Diagnostics</p><h2>诊断与安全</h2><p class="v4-note">仅在排障时展开；不把原始技术信息带到普通产品页面。</p>${pageFaultMarkup}<pre class="v4-log-output">${esc(logs.slice(-12).map((line) => typeof line === 'string' ? line : JSON.stringify(line)).join('\n') || '暂无可显示的诊断输出。')}</pre></section>`; }
    };
    root.innerHTML = page('Backstage domain', 'Console', '后台能力按对象分区呈现，不形成一个无限堆叠的技术页面。', `${localNav('console-tab', tabs, active)}<div id="v4ConsolePanel" class="v4-console-surface"></div>`);
    all('[data-v4-console-tab]', root).forEach((button) => button.addEventListener('click', async () => { runtimeSessions.invalidate(); active = button.dataset.v4ConsoleTab; all('[data-v4-console-tab]', root).forEach((item) => item.setAttribute('aria-current', String(item === button))); await renderPanel(); }));
    await renderPanel();
  }

  function renderSettings(root) {
    const saved = readLocalPreferences();
    root.innerHTML = page('Local preferences', '设置', '这里只保存此浏览器的使用偏好；运行模型、QQ 基础设施和安全配置不在这里，也不会写入服务器。', `<form id="v4SettingsForm" class="v4-settings-layout"><section><p>Appearance</p><h2>显示方式</h2><label>主题<select name="theme"><option value="system">跟随系统</option><option value="light">浅色</option><option value="dark">深色</option></select></label><label>动效<select name="motion"><option value="system">跟随系统</option><option value="reduced">减少动效</option></select></label><label>信息密度<select name="density"><option value="comfortable">舒适</option><option value="compact">紧凑</option></select></label></section><section><p>Background</p><h2>工作区背景</h2><label class="v4-check"><input type="checkbox" name="background_enabled">仅在此浏览器启用背景</label><label>背景地址<input name="background_url" type="url" placeholder="https://…"></label><label>背景暗度<input name="background_dim" type="range" min="0" max="0.96" step="0.01"></label><label>表层不透明度<input name="panel_opacity" type="range" min="0.72" max="1" step="0.01"></label></section><section><p>Notification</p><h2>本地提醒</h2><label class="v4-check"><input type="checkbox" name="local_notifications">在此浏览器提醒我有需要处理的工作</label></section><div class="v4-inline-actions"><button class="v4-primary" type="submit">保存偏好</button><p id="v4SettingsStatus" role="status"></p></div></form>`);
    const form = $('#v4SettingsForm', root); ['theme', 'motion', 'density', 'local_notifications'].forEach((name) => { if (form.elements[name].type === 'checkbox') form.elements[name].checked = Boolean(saved[name]); else form.elements[name].value = saved[name] || form.elements[name].value; });
    form.elements.background_enabled.checked = Boolean(saved.background_enabled); form.elements.background_url.value = saved.background_url || ''; form.elements.background_dim.value = saved.background_dim || '0.12'; form.elements.panel_opacity.value = saved.panel_opacity || '0.88';
    form.addEventListener('submit', async (event) => { event.preventDefault(); const values = Object.fromEntries(new FormData(form)); const preference = { theme: values.theme, motion: values.motion, density: values.density, local_notifications: form.elements.local_notifications.checked, background_enabled: form.elements.background_enabled.checked, background_url: values.background_url, background_dim: values.background_dim, panel_opacity: values.panel_opacity }; localStorage.setItem('nekoagent-v4-preferences', JSON.stringify(preference)); applyLocalPreferences(); const notice = $('#v4SettingsStatus', root); const permissionNotice = await requestLocalNotificationPermission(preference.local_notifications); notice.textContent = permissionNotice ? `偏好已保存到此浏览器；${permissionNotice}` : '偏好已保存到此浏览器。'; });
  }

  const normalizeRoute = (route) => contract.routeAliases?.[route] || route;
  const renderers = { now: renderNow, qq: renderB2Qq, chat: renderB2Chat, work: renderWork, artifacts: renderArtifacts, memory: renderMemory, assistant: renderAssistant, console: renderConsole, settings: renderSettings };
  async function navigate(route) {
    route = normalizeRoute(route);
    if (navigationOpen) setNavigationOpen(false, false);
    if (!renderers[route]) return; state.route = route;
    state.qqDispose?.(); state.qqDispose = null;
    api.cancelPendingReads?.();
    const epoch = ++state.navigationEpoch;
    all('[data-v4-route]').forEach((button) => button.toggleAttribute('aria-current', button.dataset.v4Route === route));
    const root = $('#v4PageRoot'); root.setAttribute('aria-busy', 'true'); root.innerHTML = '<p class="v4-loading">正在载入…</p>';
    const candidate = document.createElement('div');
    candidate.v4Current = () => epoch === state.navigationEpoch && state.bootstrap?.authenticated !== false;
    // Only this batch's renderers have been checked for interactive initialization.
    // Other routes retain their previous detached initialization lifecycle.
    const eagerMount = ['now', 'qq', 'memory', 'assistant'].includes(route);
    if (eagerMount) root.replaceChildren(candidate);
    try {
      await renderers[route](candidate);
      if (epoch !== state.navigationEpoch) return;
      if (!eagerMount) root.replaceChildren(candidate);
      // Keep the renderer's container node.  Nested views retain closures over
      // that node for their subsequent object-level navigation and actions.
      // Moving only its children would detach those closures from the live DOM.
      if (document.activeElement === document.body || document.activeElement === root) $('#v4PageRoot h1')?.focus();
    } catch (error) {
      if (epoch !== state.navigationEpoch) return;
      recordPageFault(route, error);
      root.innerHTML = page('暂时无法载入', '请稍后再试', '页面没有完成加载。请稍后重试；已保留受控诊断，技术信息只会在 Console 中向有权限的用户显示。', '');
    } finally { if (epoch === state.navigationEpoch) root.removeAttribute('aria-busy'); }
  }
  const navigationIcon = id => {
    const paths = {
      now: '<path d="m3 10 9-7 9 7v10H3Z"/><path d="M9 20v-7h6v7"/>',
      chat: '<path d="M4 4h16v12H9l-5 4Z"/><path d="M8 8h8M8 12h5"/>',
      qq: '<path d="M3 5h14v10H8l-5 4Z"/><path d="M18 9h3v11l-4-3h-5"/>',
      memory: '<path d="M5 3h14v18l-7-4-7 4Z"/><path d="M8 7h8M8 11h6"/>',
      assistant: '<circle cx="12" cy="8" r="4"/><path d="M4 21v-3a8 6 0 0 1 16 0v3"/>',
      work: '<rect x="3" y="7" width="18" height="14" rx="2"/><path d="M8 7V3h8v4M3 12h18M10 12v3h4v-3"/>',
      artifacts: '<path d="M3 5h7l2 3h9v12H3Z"/><path d="M8 13h8M8 16h5"/>',
      console: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/>',
      settings: '<path d="M4 6h16M4 12h16M4 18h16"/><path d="M8 3v6M16 9v6M10 15v6"/>'
    };
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${paths[id] || paths.now}</svg>`;
  };
  const navigationMedia = window.matchMedia('(max-width: 900px)');
  let navigationOpen = false;
  function setNavigationOpen(open, restoreFocus = true) {
    navigationOpen = Boolean(open) && navigationMedia.matches;
    const sidebar = $('#v4Sidebar'), workspace = $('.v4-workspace'), toggle = $('#v4NavToggle');
    sidebar.classList.toggle('is-open', navigationOpen);
    sidebar.inert = navigationMedia.matches && !navigationOpen;
    workspace.inert = navigationOpen;
    toggle.setAttribute('aria-expanded', String(navigationOpen));
    $('#v4NavBackdrop').hidden = !navigationOpen;
    if (navigationOpen) {
      sidebar.setAttribute('role', 'dialog'); sidebar.setAttribute('aria-modal', 'true');
      $('#v4NavClose').focus();
    } else {
      sidebar.removeAttribute('role'); sidebar.removeAttribute('aria-modal');
      if (restoreFocus && navigationMedia.matches) toggle.focus();
    }
  }
  const renderNavigation = () => {
    const labels = {chat: '对话', assistant: '档案', console: '高级控制台'};
    const group = ids => ids.map(id => {
      const item = contract.ownerSurfaces.find(route => route.id === id);
      return item ? `<button type="button" data-v4-route="${id}">${navigationIcon(id)}<span>${esc(labels[id] || item.label)}</span></button>` : '';
    }).join('');
    $('#v4Navigation').innerHTML = `<div class="v4-nav-group">${group(['now','chat','qq','memory','assistant'])}</div><div class="v4-nav-group"><p>委托与成果</p>${group(['work','artifacts'])}</div><div class="v4-nav-group v4-nav-tools">${group(['console','settings'])}</div>`;
    setNavigationOpen(false, false);
  };
  navigationMedia.addEventListener('change', () => setNavigationOpen(false, false));
  $('#v4NavToggle').addEventListener('click', () => setNavigationOpen(!navigationOpen));
  $('#v4NavClose').addEventListener('click', () => setNavigationOpen(false));
  $('#v4NavBackdrop').addEventListener('click', () => setNavigationOpen(false));
  async function boot() {
    const login = $('#v4Login'); const app = $('#v4App');
    try { state.bootstrap = await api.bootstrap(); } catch (_) { state.bootstrap = { authenticated: false }; }
    applyLocalPreferences();
    const authenticated = Boolean(state.bootstrap.authenticated); login.hidden = authenticated; login.inert = authenticated; app.hidden = !authenticated; app.inert = !authenticated;
    if (!authenticated) { state.qqDispose?.(); state.qqDispose = null; state.qqRosterCache = null; state.webChatDraft = null; }
    if (authenticated) { renderNavigation(); renderAttentionCenter(); navigate('now'); startAttentionPolling(); }
    else {
      stopAttentionPolling();
      state.attention = null;
      state.attentionReady = false;
      state.attentionOpen = false;
      state.attentionSeenStore = null;
      state.pendingSelection = null;
      state.unseenAttention = [];
      renderAttentionCenter();
    }
  }
  $('#v4LoginForm').addEventListener('submit', async (event) => { event.preventDefault(); const status = $('#v4LoginStatus'); try { await api.login($('#v4Token').value); status.textContent = '登录成功。'; await boot(); } catch (error) { status.textContent = userError(error, '登录未完成，请确认凭据后重试。'); } });
  $('#v4Logout').addEventListener('click', async () => { await api.logout().catch(() => {}); state.bootstrap = { authenticated: false }; await boot(); });
  document.addEventListener('click', (event) => {
    if (event.target.closest('#v4AttentionCenter')) {
      state.attentionOpen = !state.attentionOpen;
      if (state.attentionOpen && state.attention?.attention?.length) {
        state.attentionSeenStore?.markSeen(state.attention.attention);
        state.unseenAttention = state.attentionSeenStore?.unseen(state.attention.attention) || [];
      }
      renderAttentionCenter();
      return;
    }
    if (state.attentionOpen && !event.target.closest('#v4AttentionPanel')) { state.attentionOpen = false; renderAttentionCenter(); }
    const trigger = event.target.closest('.v4-presence, .v4-command, [data-v4-route]'); if (trigger?.dataset.v4Route) navigate(trigger.dataset.v4Route);
  });
  document.addEventListener('keydown', (event) => {
    if (navigationOpen && event.key === 'Escape') { event.preventDefault(); setNavigationOpen(false); return; }
    if (navigationOpen && event.key === 'Tab') {
      const items = all('button:not([disabled]), a[href]', $('#v4Sidebar')).filter(item => item.offsetParent !== null);
      const first = items[0], last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
    if (event.key === 'Escape' && state.attentionOpen) { state.attentionOpen = false; renderAttentionCenter(); $('#v4AttentionCenter')?.focus(); return; }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); navigate('chat'); }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && state.bootstrap?.authenticated) refreshAttention().catch(() => {}); });
  boot();
})();
