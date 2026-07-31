(() => {
  const editorState = {
    bound: false,
    loading: false,
    featureEnabled: false,
    version: 0,
    administrators: [],
    privateAllowlist: [],
    groupAllowlist: [],
    runtimeState: 'offline',
  };

  const byId = (id) => document.getElementById(id);
  const escapeValue = (value) => String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');

  function mountEditor() {
    const mount = byId('qqAccessMount');
    if (!mount || byId('qqAccessForm')) return;
    mount.innerHTML = `
      <div class="qq-access-heading">
        <div><h2 id="qqAccessTitle">QQ 身份与访问控制</h2><p class="compact-note">管理员角色与普通使用白名单相互独立；配置保存在 Bridge 数据库中。</p></div>
        <span id="qqAccessVersion" class="meta">配置未加载</span>
      </div>
      <form id="qqAccessForm" class="qq-access-form">
        <fieldset class="qq-access-fieldset">
          <legend>渠道策略</legend>
          <div class="qq-access-policy-grid">
            <label class="checkbox-line"><input id="qqChannelEnabled" type="checkbox">启用 QQ 渠道</label>
            <label>访问模式<select id="qqAccessMode" aria-describedby="qqAccessModeHelp"><option value="disabled">关闭</option><option value="admin_only">仅管理员</option><option value="allowlist">管理员与白名单</option></select></label>
            <label class="checkbox-line"><input id="qqPrivateChatEnabled" type="checkbox">允许私聊</label>
            <label class="checkbox-line"><input id="qqGroupChatEnabled" type="checkbox">允许已授权群聊</label>
          </div>
          <p id="qqAccessModeHelp" class="compact-note">渠道启用前必须至少配置一名超级管理员。未启用 Gate 时，所有 QQ 业务消息默认拒绝。</p>
        </fieldset>
        <fieldset class="qq-access-fieldset qq-runtime-fieldset">
          <legend>机器人身份与运行配置</legend>
          <div class="qq-runtime-status" aria-labelledby="qqRuntimeStateLabel">
            <div><span id="qqRuntimeStateLabel" class="meta">应用状态</span><strong id="qqRuntimeState">离线</strong></div>
            <div><span class="meta">实际机器人 QQ</span><strong id="qqActualBotId" class="mono">未上报</strong></div>
            <div><span class="meta">插件版本</span><strong id="qqAppliedVersion">0</strong></div>
            <div><span class="meta">最后心跳</span><strong id="qqRuntimeHeartbeat">无</strong></div>
          </div>
          <div class="qq-runtime-grid">
            <label>预期机器人 QQ<input id="qqExpectedBotId" type="text" inputmode="numeric" pattern="[1-9][0-9]{4,19}" maxlength="20" aria-describedby="qqExpectedBotHelp"></label>
            <label>命令入口<input id="qqCommandPrefixes" type="text" maxlength="160" required aria-describedby="qqCommandPrefixesHelp"></label>
            <label>单条回复上限<input id="qqReplyMaxChars" type="number" min="500" max="10000" step="100" required></label>
            <label>Delivery 轮询秒数<input id="qqDeliveryPollSeconds" type="number" min="5" max="300" required></label>
            <label>配置同步/心跳秒数<input id="qqNotificationIntervalSeconds" type="number" min="10" max="3600" required></label>
            <label class="checkbox-line"><input id="qqAutoPrivateChat" type="checkbox">自动接管普通私聊</label>
          </div>
          <p id="qqExpectedBotHelp" class="compact-note">预期账号与插件自报的实际账号不同会标记为不匹配；留空只允许保存，不会显示“已生效”。</p>
          <p id="qqCommandPrefixesHelp" class="compact-note">使用逗号分隔，固定规范入口 /codex 不可删除；其他别名可增删。</p>
        </fieldset>
        <fieldset class="qq-access-fieldset">
          <legend>管理员账号</legend><p class="compact-note">普通白名单不会自动获得管理权限。</p>
          <div class="table-wrap" tabindex="0" role="region" aria-label="QQ 管理员账号表格"><table class="qq-access-table"><caption class="visually-hidden">QQ 管理员账号列表</caption><thead><tr><th>QQ 号</th><th>显示名称</th><th>角色</th><th>启用</th><th>操作</th></tr></thead><tbody id="qqAdministratorRows"></tbody></table></div>
          <button class="secondary" type="button" data-qq-add="administrator">添加管理员</button>
        </fieldset>
        <div class="qq-access-lists">
          <fieldset class="qq-access-fieldset">
            <legend>普通私聊白名单</legend>
            <div class="table-wrap" tabindex="0" role="region" aria-label="普通私聊白名单表格"><table class="qq-access-table"><caption class="visually-hidden">普通私聊白名单</caption><thead><tr><th>QQ 号</th><th>备注</th><th>启用</th><th>操作</th></tr></thead><tbody id="qqPrivateAllowlistRows"></tbody></table></div>
            <button class="secondary" type="button" data-qq-add="private">添加 QQ</button>
          </fieldset>
          <fieldset class="qq-access-fieldset">
            <legend>群聊白名单</legend>
            <div class="table-wrap" tabindex="0" role="region" aria-label="QQ群白名单表格"><table class="qq-access-table"><caption class="visually-hidden">QQ群白名单</caption><thead><tr><th>群号</th><th>备注</th><th>启用</th><th>操作</th></tr></thead><tbody id="qqGroupAllowlistRows"></tbody></table></div>
            <button class="secondary" type="button" data-qq-add="group">添加群</button>
          </fieldset>
        </div>
        <fieldset class="qq-access-fieldset">
          <legend>已授权群的参与方式</legend>
          <div class="qq-access-policy-grid">
            <label>保存白名单时同步为<select id="qqAuthorizedGroupParticipationMode" aria-describedby="qqAuthorizedGroupParticipationHelp"><option value="mentions_only">仅明确 @</option><option value="directed_context">@、回复或受控续接</option><option value="natural_participation">自然参与</option></select></label>
            <label>自然参与强度<input id="qqAuthorizedGroupReplyProbability" type="number" min="0" max="1" step="0.05" value="0.75" required aria-describedby="qqAuthorizedGroupParticipationHelp"></label>
          </div>
          <p id="qqAuthorizedGroupParticipationHelp" class="compact-note">本次保存会在同一事务中同步所有已启用群的参与策略；这不会扩大群准入范围。自然参与仍受冷却、突发、日预算、模型判断和发送 Gate 约束。</p>
          <p id="qqAuthorizedGroupParticipationState" class="provider-status" role="status" aria-live="polite">群参与策略尚未读取。</p>
        </fieldset>
        <div id="qqAccessStatus" class="provider-status" role="status" aria-live="polite">QQ 访问配置尚未加载。</div>
        <div class="button-row"><button id="saveQqAccessBtn" class="primary" type="submit">保存配置</button><button id="enableQqAccessGateBtn" class="secondary" type="button">启用访问控制 Gate</button><button id="disableQqAccessGateBtn" class="danger" type="button">关闭访问控制 Gate</button></div>
      </form>`;
    mount.removeAttribute('aria-busy');
  }

  function setStatus(message, tone = '') {
    const status = byId('qqAccessStatus');
    if (!status) return;
    status.textContent = message;
    status.classList.toggle('error', tone === 'error');
    status.classList.toggle('ok', tone === 'ok');
  }

  function setBusy(busy) {
    editorState.loading = busy;
    const form = byId('qqAccessForm');
    if (form) form.setAttribute('aria-busy', String(Boolean(busy)));
    ['saveQqAccessBtn', 'enableQqAccessGateBtn', 'disableQqAccessGateBtn'].forEach((id) => {
      const button = byId(id);
      if (button) button.disabled = Boolean(busy);
    });
  }

  function roleOptions(selected) {
    const labels = {
      super_admin: '超级管理员',
      admin: '管理员',
      operator: '运维员',
    };
    return Object.entries(labels).map(([value, label]) => (
      `<option value="${value}"${selected === value ? ' selected' : ''}>${label}</option>`
    )).join('');
  }

  function renderAdministrators() {
    const body = byId('qqAdministratorRows');
    if (!body) return;
    if (!editorState.administrators.length) {
      body.innerHTML = '<tr><td class="qq-access-empty" colspan="5">尚未配置管理员。</td></tr>';
      return;
    }
    body.innerHTML = editorState.administrators.map((item, index) => `<tr data-qq-row="administrator" data-index="${index}">
      <td data-label="QQ 号"><label class="visually-hidden" for="qqAdminId${index}">管理员 QQ 号</label><input id="qqAdminId${index}" type="text" inputmode="numeric" pattern="[1-9][0-9]{4,19}" maxlength="20" required value="${escapeValue(item.qq_id)}"></td>
      <td data-label="显示名称"><label class="visually-hidden" for="qqAdminName${index}">显示名称</label><input id="qqAdminName${index}" type="text" maxlength="80" value="${escapeValue(item.display_name)}"></td>
      <td data-label="角色"><label class="visually-hidden" for="qqAdminRole${index}">管理员角色</label><select id="qqAdminRole${index}">${roleOptions(item.role)}</select></td>
      <td data-label="启用"><label class="visually-hidden" for="qqAdminEnabled${index}">启用管理员</label><input id="qqAdminEnabled${index}" type="checkbox"${item.enabled !== false ? ' checked' : ''}></td>
      <td data-label="操作"><button class="secondary" type="button" data-qq-remove="administrator" data-index="${index}" aria-label="删除管理员 ${escapeValue(item.qq_id || index + 1)}">删除</button></td>
    </tr>`).join('');
  }

  function renderAccessRows(kind) {
    const isPrivate = kind === 'private';
    const items = isPrivate ? editorState.privateAllowlist : editorState.groupAllowlist;
    const body = byId(isPrivate ? 'qqPrivateAllowlistRows' : 'qqGroupAllowlistRows');
    if (!body) return;
    if (!items.length) {
      body.innerHTML = `<tr><td class="qq-access-empty" colspan="4">尚未配置${isPrivate ? '普通 QQ' : 'QQ群'}。</td></tr>`;
      return;
    }
    body.innerHTML = items.map((item, index) => {
      const subject = isPrivate ? item.qq_id : item.group_id;
      const prefix = isPrivate ? 'qqPrivate' : 'qqGroup';
      const subjectLabel = isPrivate ? '普通用户 QQ 号' : 'QQ群号';
      return `<tr data-qq-row="${kind}" data-index="${index}">
        <td data-label="${isPrivate ? 'QQ 号' : '群号'}"><label class="visually-hidden" for="${prefix}Id${index}">${subjectLabel}</label><input id="${prefix}Id${index}" type="text" inputmode="numeric" pattern="[1-9][0-9]{4,19}" maxlength="20" required value="${escapeValue(subject)}"></td>
        <td data-label="备注"><label class="visually-hidden" for="${prefix}Remark${index}">备注</label><input id="${prefix}Remark${index}" type="text" maxlength="160" value="${escapeValue(item.remark)}"></td>
        <td data-label="启用"><label class="visually-hidden" for="${prefix}Enabled${index}">启用</label><input id="${prefix}Enabled${index}" type="checkbox"${item.enabled !== false ? ' checked' : ''}></td>
        <td data-label="操作"><button class="secondary" type="button" data-qq-remove="${kind}" data-index="${index}" aria-label="删除${subjectLabel} ${escapeValue(subject || index + 1)}">删除</button></td>
      </tr>`;
    }).join('');
  }

  function renderEditor(payload, { animate = false } = {}) {
    const settings = payload.settings || {};
    const runtime = payload.runtime || {};
    const groupParticipation = payload.group_participation || {};
    editorState.featureEnabled = Boolean(payload.feature_enabled);
    editorState.version = Number(settings.config_version || 0);
    editorState.administrators = (payload.administrators || []).filter((item) => item.enabled !== false);
    editorState.privateAllowlist = (payload.private_allowlist || []).filter((item) => item.enabled !== false);
    editorState.groupAllowlist = (payload.group_allowlist || []).filter((item) => item.enabled !== false);

    byId('qqChannelEnabled').checked = Boolean(settings.channel_enabled);
    byId('qqAccessMode').value = settings.access_mode || 'disabled';
    byId('qqPrivateChatEnabled').checked = Boolean(settings.private_chat_enabled);
    byId('qqGroupChatEnabled').checked = Boolean(settings.group_chat_enabled);
    byId('qqExpectedBotId').value = settings.expected_bot_id || '';
    byId('qqCommandPrefixes').value = (settings.command_prefixes || ['/codex']).join(', ');
    byId('qqAutoPrivateChat').checked = Boolean(settings.auto_private_chat);
    byId('qqReplyMaxChars').value = Number(settings.reply_max_chars || 3600);
    byId('qqDeliveryPollSeconds').value = Number(settings.delivery_poll_seconds || 12);
    byId('qqNotificationIntervalSeconds').value = Number(settings.notification_interval_seconds || 90);
    const mode = groupParticipation.participation_mode || 'natural_participation';
    byId('qqAuthorizedGroupParticipationMode').value = mode === 'mixed' || mode === 'unconfigured'
      ? 'natural_participation' : mode;
    const probability = Number(groupParticipation.reply_probability);
    byId('qqAuthorizedGroupReplyProbability').value = Number.isFinite(probability) ? probability : 0.75;
    const groupPolicyState = {
      uniform: `已授权群当前统一为：${byId('qqAuthorizedGroupParticipationMode').selectedOptions[0].text}。`,
      mixed: '已授权群当前参与方式不一致；保存会按上方值统一。',
      unconfigured: '存在已授权但未配置参与方式的群；保存会按上方值补齐。',
      unavailable: '群参与策略暂不可读取；保存前请稍后刷新。',
    }[groupParticipation.state] || '群参与策略尚未读取。';
    byId('qqAuthorizedGroupParticipationState').textContent = groupPolicyState;
    byId('qqAuthorizedGroupParticipationState').className = `provider-status ${groupParticipation.state === 'uniform' ? 'ok' : groupParticipation.state === 'mixed' || groupParticipation.state === 'unconfigured' ? 'error' : ''}`;
    editorState.runtimeState = runtime.state || 'offline';
    const runtimeLabels = { applied: '已生效', pending: '等待应用', mismatch: '账号不匹配', offline: '离线', degraded: '同步异常' };
    byId('qqRuntimeState').textContent = runtimeLabels[editorState.runtimeState] || editorState.runtimeState;
    byId('qqRuntimeState').dataset.state = editorState.runtimeState;
    byId('qqActualBotId').textContent = runtime.actual_bot_id || '未上报';
    byId('qqAppliedVersion').textContent = `${Number(runtime.applied_version || 0)} / ${editorState.version}`;
    byId('qqRuntimeHeartbeat').textContent = runtime.last_heartbeat_at || '无';
    byId('qqAccessVersion').textContent = `配置版本 ${editorState.version} · Gate ${editorState.featureEnabled ? '已启用' : '未启用'}`;
    byId('qqAccessPanel').classList.toggle('qq-access-gate-on', editorState.featureEnabled);
    byId('enableQqAccessGateBtn').hidden = editorState.featureEnabled;
    byId('disableQqAccessGateBtn').hidden = !editorState.featureEnabled;
    renderAdministrators();
    renderAccessRows('private');
    renderAccessRows('group');
    const applied = editorState.runtimeState === 'applied';
    setStatus(editorState.featureEnabled
      ? applied
        ? `访问控制 Gate 已启用，插件已应用配置版本 ${editorState.version}。`
        : `访问控制 Gate 已启用；配置版本 ${editorState.version} 尚未得到插件一致回执。`
      : '配置已保存但 Gate 尚未启用；QQ 业务消息会默认拒绝。', applied ? 'ok' : '');
    window.dispatchEvent(new CustomEvent('qq-access-updated', { detail: payload }));
    if (animate) window.AdminMotion?.enterView(byId('qqAccessPanel'));
  }

  function syncRowsFromDom() {
    editorState.administrators = [...document.querySelectorAll('[data-qq-row="administrator"]')].map((row) => ({
      qq_id: row.querySelector('input[type="text"]').value.trim(),
      display_name: row.querySelectorAll('input[type="text"]')[1].value.trim(),
      role: row.querySelector('select').value,
      enabled: row.querySelector('input[type="checkbox"]').checked,
    }));
    const collect = (kind) => [...document.querySelectorAll(`[data-qq-row="${kind}"]`)].map((row) => {
      const values = row.querySelectorAll('input[type="text"]');
      return {
        [kind === 'private' ? 'qq_id' : 'group_id']: values[0].value.trim(),
        remark: values[1].value.trim(),
        enabled: row.querySelector('input[type="checkbox"]').checked,
      };
    });
    editorState.privateAllowlist = collect('private');
    editorState.groupAllowlist = collect('group');
  }

  function snapshotPayload() {
    syncRowsFromDom();
    return {
      expected_version: editorState.version,
      settings: {
        channel_enabled: byId('qqChannelEnabled').checked,
        access_mode: byId('qqAccessMode').value,
        private_chat_enabled: byId('qqPrivateChatEnabled').checked,
        group_chat_enabled: byId('qqGroupChatEnabled').checked,
        expected_bot_id: byId('qqExpectedBotId').value.trim(),
        command_prefixes: byId('qqCommandPrefixes').value.split(',').map((item) => item.trim()).filter(Boolean),
        auto_private_chat: byId('qqAutoPrivateChat').checked,
        reply_max_chars: Number(byId('qqReplyMaxChars').value),
        delivery_poll_seconds: Number(byId('qqDeliveryPollSeconds').value),
        notification_interval_seconds: Number(byId('qqNotificationIntervalSeconds').value),
      },
      administrators: editorState.administrators,
      private_allowlist: editorState.privateAllowlist,
      group_allowlist: editorState.groupAllowlist,
      group_participation: {
        apply_to_enabled_groups: true,
        participation_mode: byId('qqAuthorizedGroupParticipationMode').value,
        reply_probability: Number(byId('qqAuthorizedGroupReplyProbability').value),
      },
    };
  }

  function idempotencyKey() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    return `qq-settings-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  async function loadQqAccessSettings() {
    mountEditor();
    if (!byId('qqAccessPanel')) return;
    setBusy(true);
    try {
      const result = await bridge('/qq/settings');
      renderEditor(result, { animate: editorState.version === 0 });
    } catch (error) {
      setStatus(error.message || String(error), 'error');
      throw error;
    } finally {
      setBusy(false);
    }
  }

  async function saveSettings(event) {
    event.preventDefault();
    const form = byId('qqAccessForm');
    const prefixInput = byId('qqCommandPrefixes');
    const prefixes = prefixInput.value.split(',').map((item) => item.trim().toLowerCase()).filter(Boolean);
    prefixInput.setCustomValidity(prefixes.includes('/codex') ? '' : '固定规范入口 /codex 不可删除。');
    if (!form.reportValidity()) return;
    setBusy(true);
    setStatus('正在保存 QQ 访问配置…');
    try {
      const result = await bridge('/qq/settings', {
        method: 'POST',
        headers: { 'Idempotency-Key': idempotencyKey() },
        body: JSON.stringify(snapshotPayload()),
      });
      renderEditor(result);
      const applied = result.runtime?.state === 'applied';
      setStatus(applied
        ? `配置版本 ${result.settings.config_version} 已保存并由插件应用。`
        : `配置版本 ${result.settings.config_version} 已保存，正在等待插件应用。`, applied ? 'ok' : '');
    } catch (error) {
      setStatus(error.message || String(error), 'error');
      if (error.status === 409) await loadQqAccessSettings();
    } finally {
      setBusy(false);
    }
  }

  async function changeGate(enabled) {
    if (!window.confirm(enabled
      ? '启用后，QQ 插件将只接受当前角色与白名单配置。确认启用吗？'
      : '关闭后，所有 QQ 业务消息将被拒绝。确认关闭吗？')) return;
    setBusy(true);
    try {
      const plan = await bridge('/qq/access/cutover');
      if (enabled && !plan.ready) throw new Error('当前配置未满足启用条件，请先保存渠道模式和超级管理员。');
      await bridge('/qq/access/cutover', {
        method: 'POST',
        body: JSON.stringify({ enabled, plan_checksum: plan.plan_checksum }),
      });
      await loadQqAccessSettings();
    } catch (error) {
      setStatus(error.message || String(error), 'error');
    } finally {
      setBusy(false);
    }
  }

  function addRow(kind) {
    syncRowsFromDom();
    if (kind === 'administrator') {
      editorState.administrators.push({ qq_id: '', display_name: '', role: 'admin', enabled: true });
      renderAdministrators();
    } else if (kind === 'private') {
      editorState.privateAllowlist.push({ qq_id: '', remark: '', enabled: true });
      renderAccessRows('private');
    } else {
      editorState.groupAllowlist.push({ group_id: '', remark: '', enabled: true });
      renderAccessRows('group');
    }
    const rows = document.querySelectorAll(`[data-qq-row="${kind}"]`);
    rows[rows.length - 1]?.querySelector('input[type="text"]')?.focus();
  }

  function removeRow(kind, index) {
    syncRowsFromDom();
    const target = kind === 'administrator'
      ? editorState.administrators
      : kind === 'private' ? editorState.privateAllowlist : editorState.groupAllowlist;
    target.splice(index, 1);
    if (kind === 'administrator') renderAdministrators();
    else renderAccessRows(kind);
    byId('qqAccessStatus')?.focus?.();
  }

  function bindQqAccessEvents() {
    mountEditor();
    if (editorState.bound || !byId('qqAccessForm')) return;
    editorState.bound = true;
    byId('qqAccessForm').addEventListener('submit', saveSettings);
    byId('qqCommandPrefixes').addEventListener('input', (event) => {
      const values = event.target.value.split(',').map((item) => item.trim().toLowerCase());
      event.target.setCustomValidity(values.includes('/codex') ? '' : '固定规范入口 /codex 不可删除。');
    });
    byId('qqAccessPanel').addEventListener('click', (event) => {
      const add = event.target.closest('[data-qq-add]');
      if (add) {
        addRow(add.dataset.qqAdd);
        return;
      }
      const remove = event.target.closest('[data-qq-remove]');
      if (remove) removeRow(remove.dataset.qqRemove, Number(remove.dataset.index));
    });
    byId('enableQqAccessGateBtn').addEventListener('click', () => changeGate(true));
    byId('disableQqAccessGateBtn').addEventListener('click', () => changeGate(false));
  }

  window.loadQqAccessSettings = loadQqAccessSettings;
  window.bindQqAccessEvents = bindQqAccessEvents;
})();
