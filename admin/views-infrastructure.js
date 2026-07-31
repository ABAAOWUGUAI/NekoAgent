    function stateText(ok, goodText = '正常', badText = '需要检查') {
      return ok ? goodText : badText;
    }

    function renderQqDiagnostics(result) {
      state.qqDiagnostics = result;
      const qqOnline = result.qq_status === 'online' && !result.needs_login;
      const qqLoginRequired = result.needs_login || result.qq_status === 'login_required';
      const adapterLabel = result.adapter_label || result.adapter_id || 'QQ Adapter';
      const qrcodeSupported = result.qrcode_supported !== false;
      const qqLoginLabel = qqOnline ? '在线' : qqLoginRequired ? '需登录' : '未确认';
      const items = [
        ['QQ 登录', qqLoginLabel, qqOnline ? 'blue' : 'red'],
        ['OneBot', stateText(result.onebot_connected, '已连接', '未连接'), result.onebot_connected ? 'blue' : 'red'],
        ['插件', stateText(result.plugin_loaded, '已加载', '未加载'), result.plugin_loaded ? 'blue' : 'red'],
        ['Bridge', stateText(result.bridge_reachable_from_astrbot, '可访问', '不可访问'), result.bridge_reachable_from_astrbot ? 'blue' : 'red'],
      ];
      $('qqSummary').innerHTML = items.map(([label, value, tone]) => (
        `<div class="summary-item"><span>${escapeHtml(label)}</span><strong class="${tone}">${escapeHtml(value)}</strong></div>`
      )).join('');

      $('qqMeta').textContent = `耗时 ${result.duration ?? '?'}s`;
      $('qqAd').textContent = adapterLabel;
      $('qqRecommendation').textContent = result.recommendation || '(无建议)';
      $('qqLoginState').textContent = qqOnline
        ? 'QQ 机器人账号在线。'
        : qqLoginRequired
          ? 'QQ 机器人账号需要重新登录。'
          : '未确认 QQ 机器人登录状态。';
      $('qqAllowedIds').textContent = (result.allowed_qq_ids || []).join(', ') || '(未配置)';
      $('qqOnebotState').textContent = result.onebot_connected ? `${adapterLabel} 到 AstrBot 的 OneBot 连接正常。` : '未确认 OneBot 连接。';
      $('qqPluginState').textContent = result.plugin_loaded ? 'codex_agent 插件已加载。' : '未看到 codex_agent 插件加载记录。';
      $('qqBridgeState').textContent = result.bridge_reachable_from_astrbot
        ? `AstrBot 可访问 ${result.bridge_url || 'bridge'}。`
        : `AstrBot 暂时无法访问 ${result.bridge_url || 'bridge'}。${result.bridge_probe_output ? '\n' + result.bridge_probe_output : ''}`;

      const showQr = Boolean(result.needs_login && result.qrcode_available && result.qrcode_url);
      $('qqQrBox').classList.toggle('hidden', !showQr);
      $('refreshQrcodeBtn').hidden = !qrcodeSupported;
      $('refreshQrcodeBtn').disabled = qqOnline || !qrcodeSupported;
      $('refreshQrcodeBtn').textContent = qrcodeSupported ? '刷新 QQ 登录二维码' : '请通过 LLBot WebUI 登录';
      $('refreshQrcodeBtn').title = qrcodeSupported
        ? (qqOnline ? 'QQ 当前在线，无需刷新二维码。' : '重新生成 QQ 登录二维码。')
        : (result.login_management_hint || 'LLBot 登录由服务器本地 WebUI 管理。');
      if (showQr) {
        const qrTime = result.qrcode_mtime
          ? new Date(Number(result.qrcode_mtime) * 1000).toLocaleString()
          : '刚刚';
        $('qqQrHint').textContent = `登录态失效。请使用手机 QQ 扫描下方二维码授权。二维码更新时间：${qrTime}`;
      } else if (!qrcodeSupported) {
        $('qqQrHint').textContent = qqLoginRequired
          ? (result.login_management_hint || '请通过安全 SSH 隧道访问 LLBot WebUI 完成扫码登录。')
          : '当前无需扫码。LLBot 登录凭据仅保留在服务器，控制台不会读取或保存 WebUI 密码。';
      } else if (qqLoginRequired) {
        $('qqQrHint').textContent = result.qrcode_stale
          ? '登录态失效，现有二维码已经过期。请点击“刷新二维码”，并等待新的更新时间出现后再扫码。'
          : result.qrcode_decode_url
          ? `二维码图片暂未生成。可先复制日志中的解码地址，或点击“刷新二维码”重启登录流程。`
          : '登录态失效，但暂未发现二维码图片。请点击“刷新二维码”重启登录流程。';
      } else {
        $('qqQrHint').textContent = '当前不需要扫码；如果后续登录失效，这里会显示二维码。';
      }
      if (showQr) {
        const stamp = result.qrcode_mtime || Date.now();
        $('qqQrImage').src = `${result.qrcode_url}?t=${stamp}`;
      }

      const receives = result.recent_allowed_receives || [];
      const sends = result.recent_allowed_sends || [];
      $('qqMessageEvents').textContent = [
        '最近接收:',
        ...(receives.length ? receives : ['(暂无白名单私聊接收记录)']),
        '',
        '最近发送:',
        ...(sends.length ? sends : ['(暂无白名单私聊发送记录)']),
      ].join('\n');
      $('qqRuntimeEvents').textContent = [
        '连接事件:',
        ...((result.connection_events || []).length ? result.connection_events : ['(暂无连接事件)']),
        '',
        '插件事件:',
        ...((result.plugin_events || []).length ? result.plugin_events : ['(暂无插件事件)']),
      ].join('\n');
      renderQqAuditEvents(result.audit_events || []);
      renderQqParticipation(result.participation || {});
    }

    function renderQqParticipation(participation) {
      const decisions = participation.decisions || [];
      const mode = participation.deterministic_enabled
        ? '确定性参与已启用'
        : participation.shadow_enabled ? '仅影子观察' : '未启用';
      $('qqParticipationMeta').textContent = `${mode} · ${decisions.length} 条`;
      if (!decisions.length) {
        $('qqParticipationRows').innerHTML = '<tr><td colspan="6" class="empty">暂无参与决策。</td></tr>';
        return;
      }
      $('qqParticipationRows').innerHTML = decisions.map((item) => `<tr>
        <td class="mono">${escapeHtml(item.created_at || '')}</td>
        <td>${escapeHtml(`${item.channel_type || '-'} / ${item.conversation_scope || '-'}`)}</td>
        <td><span class="badge ${item.action === 'silent' ? '' : 'blue'}">${escapeHtml(item.action || '-')}</span></td>
        <td>${escapeHtml(item.reason_code || '-')}</td>
        <td>${escapeHtml(item.model_role || '确定性规则')}</td>
        <td>${escapeHtml(item.policy_version || '-')}</td>
      </tr>`).join('');
    }

    function renderQqAuditEvents(events) {
      $('qqAuditMeta').textContent = `${events.length} 条`;
      if (!events.length) {
        $('qqAuditRows').innerHTML = '<tr><td colspan="7" class="empty">暂无审计事件。</td></tr>';
        return;
      }
      $('qqAuditRows').innerHTML = events.map((item) => {
        const detail = item.message || item.detail || '';
        const taskId = item.task_id || '';
        return `<tr>
          <td class="mono">${escapeHtml(item.created_at || '')}</td>
          <td class="mono">${escapeHtml(item.trace_id || '')}</td>
          <td>${escapeHtml(item.stage || '')}</td>
          <td>${escapeHtml(item.action || '')}</td>
          <td><span class="badge ${item.status === 'ok' ? 'blue' : item.status ? 'red' : ''}">${escapeHtml(item.status || '-')}</span></td>
          <td class="mono">${escapeHtml(taskId)}</td>
          <td>${escapeHtml(detail)}</td>
        </tr>`;
      }).join('');
    }

    function renderServices(services) {
      if (!services.length) {
        $('serviceRows').innerHTML = '<tr><td colspan="4" class="empty">暂无服务。</td></tr>';
        return;
      }
      $('serviceRows').innerHTML = services.map((item) => {
        const tone = item.ok ? 'blue' : 'red';
        return `<tr>
          <td>${escapeHtml(item.name || '')}</td>
          <td>${escapeHtml(item.type || '')}</td>
          <td class="mono">${escapeHtml(item.target || '')}</td>
          <td><span class="badge ${tone}">${escapeHtml(item.status || 'unknown')}</span></td>
        </tr>`;
      }).join('');
    }

    function renderContainers(containers) {
      if (!containers.length) {
        $('containerRows').innerHTML = '<tr><td colspan="4" class="empty">暂无容器。</td></tr>';
        return;
      }
      $('containerRows').innerHTML = containers.map((item) => {
        const running = String(item.state || '').toLowerCase() === 'running';
        return `<tr>
          <td>${escapeHtml(item.name || '')}</td>
          <td class="mono">${escapeHtml(item.image || '')}</td>
          <td><span class="badge ${running ? 'blue' : 'red'}">${escapeHtml(item.status || item.state || '')}</span></td>
          <td>${escapeHtml(item.ports || '')}</td>
        </tr>`;
      }).join('');
    }

    function selectedProxyGroup() {
      const active = (state.proxySubscriptions?.managed || []).find((item) => item.active);
      return active ? (state.proxyGroups.find((item) => item.name === 'Proxies') || null) : null;
    }

    function proxyDelayFor(name, node) {
      const measured = state.proxyDelays[name];
      if (measured) {
        return measured;
      }
      const history = node?.history || [];
      const latest = history.length ? history[history.length - 1] : null;
      if (latest && typeof latest.delay === 'number') {
        return { ok: latest.delay >= 0, delay: latest.delay, error: latest.delay >= 0 ? '' : 'history failed' };
      }
      return null;
    }

    function renderProxyCards() {
      const group = selectedProxyGroup();
      const query = ($('proxySearchInput')?.value || '').trim().toLowerCase();
      if (!group) {
        $('proxyCards').innerHTML = '<div class="empty">请先新增并启用一个订阅。旧运行配置不会作为可管理节点显示。</div>';
        return;
      }
      const nodes = (group.nodes || []).filter((node) => {
        if (!query) {
          return true;
        }
        return String(node.name || '').toLowerCase().includes(query);
      });
      const originalIndex = new Map((group.nodes || []).map((node, index) => [node.name, index]));
      const delayRank = (node) => {
        const delay = proxyDelayFor(node.name, node);
        if (!delay || !delay.ok || typeof delay.delay !== 'number') {
          return Number.POSITIVE_INFINITY;
        }
        return delay.delay;
      };
      nodes.sort((a, b) => {
        if (state.proxySort === 'delay') {
          const left = delayRank(a);
          const right = delayRank(b);
          if (left !== right) {
            return left - right;
          }
        }
        if (state.proxySort === 'name') {
          return String(a.name || '').localeCompare(String(b.name || ''), 'zh-Hans-CN');
        }
        if (state.proxySort === 'ai') {
          const left = state.proxyAiResults[a.name]?.ok ? 0 : 1;
          const right = state.proxyAiResults[b.name]?.ok ? 0 : 1;
          if (left !== right) {
            return left - right;
          }
        }
        return (originalIndex.get(a.name) ?? 0) - (originalIndex.get(b.name) ?? 0);
      });
      if (!nodes.length) {
        $('proxyCards').innerHTML = '<div class="empty">没有匹配的节点。</div>';
        return;
      }
      $('proxyCards').innerHTML = nodes.map((node) => {
        const name = node.name || '';
        const active = name === group.now;
        const delay = proxyDelayFor(name, node);
        const ai = state.proxyAiResults[name];
        const aiFail = ai && !ai.ok;
        const cardClass = ['proxy-card', active ? 'active' : '', aiFail ? 'ai-fail' : ''].filter(Boolean).join(' ');
        const delayText = delay ? (delay.ok ? `${delay.delay} ms` : 'timeout') : '-';
        const delayClass = delay && !delay.ok ? 'proxy-delay bad' : 'proxy-delay';
        const aiBadge = ai
          ? `<span class="badge ${ai.ok ? 'green' : 'red'}">${ai.ok ? 'AI OK' : 'AI FAIL'}</span>`
          : '<span class="badge">AI 未测</span>';
        return `<button class="${cardClass}" type="button" data-proxy-node="${escapeHtml(name)}" title="${escapeHtml(name)}">
          <div>
            <strong>${escapeHtml(name)}</strong>
            <div class="proxy-card-meta">
              <span class="badge">${escapeHtml(node.type || (node.is_group ? 'Selector' : 'Proxy'))}</span>
              ${node.udp ? '<span class="badge">UDP</span>' : ''}
              ${node.is_group ? '<span class="badge blue">Group</span>' : ''}
              ${active ? '<span class="badge blue">当前</span>' : ''}
            </div>
          </div>
          <div class="proxy-card-state">
            <span class="${delayClass}">${escapeHtml(delayText)}</span>
            ${aiBadge}
          </div>
        </button>`;
      }).join('');
    }

    function renderProxyAlpha(group) {
      const firstChars = Array.from(new Set((group?.nodes || []).map((node) => String(node.name || '').trim().slice(0, 2)).filter(Boolean))).slice(0, 18);
      $('proxyAlpha').innerHTML = firstChars.map((prefix) => (
        `<button type="button" data-proxy-filter="${escapeHtml(prefix)}">${escapeHtml(prefix)}</button>`
      )).join('');
    }

    function renderProxyPage(message = '') {
      const group = selectedProxyGroup();
      const total = group?.nodes?.length || 0;
      const measured = Object.keys(state.proxyDelays).length;
      const aiOk = Object.values(state.proxyAiResults).filter((item) => item.ok).length;
      const summary = [
        ['当前订阅', (state.proxySubscriptions?.managed || []).find((item) => item.active)?.name || '未设置', group ? 'blue' : 'red'],
        ['当前节点', group?.now || '-', group?.now ? 'blue' : 'red'],
        ['代理方式', state.proxyConfig.mode || '-', state.proxyConfig.mode === 'direct' ? 'amber' : 'blue'],
        ['节点数量', total, ''],
        ['已测速', measured, measured ? 'green' : ''],
        ['AI 可用', aiOk, aiOk ? 'green' : 'red'],
      ];
      $('proxySummary').innerHTML = summary.map(([label, value, tone]) => (
        `<div class="summary-item"><span>${escapeHtml(label)}</span><strong class="${tone}">${escapeHtml(value)}</strong></div>`
      )).join('');
      $('proxyMeta').textContent = group ? `${total} 个订阅节点` : '订阅是唯一配置来源';
      $('proxyStatus').className = 'provider-status' + (aiOk ? ' ok' : '');
      $('proxyStatus').textContent = message || (group ? '只读体检不会切换节点；“检测并切换”才会改变活动路由并验证 AI 站点。' : '尚未启用订阅，旧内联节点不会作为资产展示。');
      renderProxyAlpha(group);
      renderProxyCards();
    }

    function renderProxySubscriptions(result = {}) {
      const managed = result.managed || [];
      const active = managed.find((item) => item.active);
      $('proxySubStatus').className = `provider-status ${active ? 'ok' : managed.length ? 'pending' : ''}`;
      $('proxySubStatus').textContent = active
        ? `正在使用“${active.name || active.key}”；节点仅来自该订阅。`
        : managed.length ? '已有订阅但尚未启用，请选择一个订阅。' : '尚未添加订阅。旧内联节点不属于“我的订阅”。';
      if (!managed.length) {
        $('proxySubscriptions').innerHTML = '<div class="empty">没有订阅。点击“新增订阅”开始。</div>';
        return;
      }
      $('proxySubscriptions').innerHTML = managed.map((item) => (
        `<article class="proxy-subscription-card ${item.active ? 'active' : ''}">
          <div><strong>${escapeHtml(item.name || item.key)}</strong><p>${escapeHtml(item.format || 'unknown')} · ${escapeHtml(item.node_count ?? '?')} 节点</p></div>
          <div class="proxy-subscription-state"><span class="badge ${item.active ? 'green' : item.last_status === 'refresh_failed' ? 'red' : ''}">${item.active ? '当前使用' : escapeHtml(item.last_status || '未启用')}</span><small>${escapeHtml(item.url || '')}</small></div>
          <div class="actions">
            ${item.active ? '' : `<button class="primary" type="button" data-proxy-sub-action="switch" data-proxy-sub-key="${escapeHtml(item.key)}">使用</button>`}
            <button class="secondary" type="button" data-proxy-sub-action="refresh" data-proxy-sub-key="${escapeHtml(item.key)}">更新</button>
            <button class="secondary" type="button" data-proxy-sub-edit="${escapeHtml(item.key)}">编辑</button>
            <button class="danger" type="button" data-proxy-sub-action="delete" data-proxy-sub-key="${escapeHtml(item.key)}">删除</button>
          </div>
        </article>`
      )).join('');
    }

    function editProxySubscription(key = '') {
      const item = (state.proxySubscriptions?.managed || []).find((entry) => entry.key === key) || {};
      $('proxySubKeyInput').value = item.key || '';
      $('proxySubNameInput').value = item.name || '';
      $('proxySubUrlInput').value = '';
      $('proxySubscriptionEditor').hidden = false;
      $('proxySubCreateBtn').textContent = item.key ? '保存修改' : '创建并验证';
      requestAnimationFrame(() => $('proxySubNameInput').focus());
    }

    function closeProxySubscriptionEditor() {
      $('proxySubscriptionEditor').hidden = true;
      $('proxySubKeyInput').value = '';
      $('proxySubNameInput').value = '';
      $('proxySubUrlInput').value = '';
    }

    async function operateProxySubscription(action, key) {
      if (action === 'delete' && !window.confirm('删除该托管订阅、代理组和本地 Provider 文件？配置会先集中备份。')) {
        return;
      }
      const button = document.querySelector(`[data-proxy-sub-action="${action}"][data-proxy-sub-key="${CSS.escape(key)}"]`);
      try {
        if (button) button.disabled = true;
        $('proxySubStatus').className = 'provider-status pending';
        $('proxySubStatus').textContent = action === 'refresh' ? '正在重新下载订阅并验证配置。' : action === 'switch' ? '正在切换订阅并重建活动节点。' : '正在备份并删除订阅及其派生节点。';
        const result = await bridge(`/proxy/subscriptions/${action}`, {
          method: 'POST',
          body: JSON.stringify({ key }),
        });
        await loadProxyGroups({
          config: state.proxyConfig,
          subscriptions: result,
        });
        setConnection(action === 'refresh' ? '订阅已更新。' : action === 'switch' ? '订阅已切换。' : '订阅已删除。', 'ok');
      } catch (error) {
        $('proxySubStatus').className = 'provider-status error';
        $('proxySubStatus').textContent = error.message || String(error);
        setConnection(error.message || String(error), 'error');
      } finally {
        if (button && button.isConnected) button.disabled = false;
      }
    }

    async function saveProxyMode() {
      const mode = $('proxyModeSelect').value;
      try {
        $('proxyModeSaveBtn').disabled = true;
        const result = await bridge('/proxy/config', {
          method: 'POST',
          body: JSON.stringify({ mode }),
        });
        state.proxyConfig.mode = result.mode || mode;
        setConnection(`代理方式已切换为 ${state.proxyConfig.mode}。`, 'ok');
        renderProxyPage(`代理方式已切换为 ${state.proxyConfig.mode}。`);
      } catch (error) {
        setConnection(error.message || String(error), 'error');
      } finally {
        $('proxyModeSaveBtn').disabled = false;
      }
    }

    function formatIpResult(item) {
      if (!item || !item.ok) {
        return `${item?.name || '-'}：失败，${item?.error || 'unknown'}`;
      }
      return `${item.name}：${item.ip} / ${item.region || '-'} / ${item.org || '-'} (${item.service})`;
    }

    async function checkProxyIp() {
      try {
        $('proxyIpBtn').disabled = true;
        $('proxyIpStatus').className = 'provider-status pending';
        $('proxyIpStatus').textContent = '正在检测直连与代理出口 IP。';
        const result = await bridge('/proxy/ip');
        $('proxyIpStatus').className = `provider-status ${result.ok ? 'ok' : 'error'}`;
        $('proxyIpStatus').innerHTML = [
          escapeHtml(formatIpResult(result.direct)),
          escapeHtml(formatIpResult(result.proxy)),
        ].join('<br>');
      } catch (error) {
        $('proxyIpStatus').className = 'provider-status error';
        $('proxyIpStatus').textContent = error.message || String(error);
        setConnection(error.message || String(error), 'error');
      } finally {
        $('proxyIpBtn').disabled = false;
      }
    }

    async function saveProxySubscription() {
      const key = $('proxySubKeyInput').value.trim();
      const name = $('proxySubNameInput').value.trim();
      const url = $('proxySubUrlInput').value.trim();
      if (!name || (!key && !url)) {
        $('proxySubStatus').className = 'provider-status error';
        $('proxySubStatus').textContent = key ? '请输入订阅名称。' : '请输入订阅名称和订阅 URL。';
        return;
      }
      try {
        $('proxySubCreateBtn').disabled = true;
        $('proxySubStatus').className = 'provider-status pending';
        $('proxySubStatus').textContent = '正在识别订阅、测试配置并热重载 mihomo。';
        const result = await bridge('/proxy/subscriptions', {
          method: 'POST',
          body: JSON.stringify({ key, name, url }),
        });
        closeProxySubscriptionEditor();
        await loadProxyGroups({
          config: state.proxyConfig,
          subscriptions: result,
        });
        setConnection(`订阅 ${name} 已保存。`, 'ok');
      } catch (error) {
        $('proxySubStatus').className = 'provider-status error';
        $('proxySubStatus').textContent = error.message || String(error);
        setConnection(error.message || String(error), 'error');
      } finally {
        $('proxySubCreateBtn').disabled = false;
      }
    }

    async function loadProxyGroups({ groups = null, config = null, subscriptions = null } = {}) {
      try {
        const [groupResult, configResult, subscriptionResult] = await Promise.all([
          groups ? Promise.resolve(groups) : bridge('/proxy/groups'),
          config ? Promise.resolve(config) : bridge('/proxy/config'),
          subscriptions ? Promise.resolve(subscriptions) : bridge('/proxy/subscriptions'),
        ]);
        state.proxyGroups = groupResult.groups || [];
        state.proxyConfig = configResult || {};
        state.proxySubscriptions = subscriptionResult || {};
        const active = (subscriptionResult.managed || []).find((item) => item.active);
        state.proxyActiveGroup = active ? 'Proxies' : '';
        if (configResult.mode) {
          $('proxyModeSelect').value = configResult.mode;
        }
        renderProxySubscriptions(subscriptionResult);
        renderProxyPage(active ? '当前订阅节点已加载。' : '请新增并启用一个订阅。');
      } catch (error) {
        $('proxyStatus').className = 'provider-status error';
        $('proxyStatus').textContent = error.message || String(error);
        setConnection(error.message || String(error), 'error');
      }
    }

    async function selectProxyNode(name) {
      const group = selectedProxyGroup();
      if (!group || !name) {
        return;
      }
      try {
        $('proxyStatus').className = 'provider-status pending';
        $('proxyStatus').textContent = `正在切换到 ${name}。`;
        await bridge('/proxy/select', {
          method: 'POST',
          body: JSON.stringify({ group: group.name, node: name }),
        });
        group.now = name;
        renderProxyPage(`已切换到 ${name}。`);
        setConnection(`代理已切换到 ${name}。`, 'ok');
      } catch (error) {
        $('proxyStatus').className = 'provider-status error';
        $('proxyStatus').textContent = error.message || String(error);
        setConnection(error.message || String(error), 'error');
      }
    }

    async function runProxyDelay() {
      const group = selectedProxyGroup();
      if (!group) {
        return;
      }
      const limit = Math.max(1, Math.min(Number($('proxyLimitInput').value || 24), 80));
      const names = (group.nodes || []).slice(0, limit).map((node) => node.name);
      try {
        $('proxyDelayBtn').disabled = true;
        $('proxyStatus').className = 'provider-status pending';
        $('proxyStatus').textContent = `正在测试 ${names.length} 个节点延迟。`;
        const result = await bridge('/proxy/delay', {
          method: 'POST',
          body: JSON.stringify({ group: group.name, names, timeout_ms: 6000 }),
        });
        (result.results || []).forEach((item) => {
          state.proxyDelays[item.name] = { ok: item.ok, delay: item.delay, error: item.error || '' };
        });
        state.proxySort = 'delay';
        $('proxySortSelect').value = 'delay';
        renderProxyPage(`延迟测试完成：${result.results?.length || 0} 个节点。`);
      } catch (error) {
        $('proxyStatus').className = 'provider-status error';
        $('proxyStatus').textContent = error.message || String(error);
        setConnection(error.message || String(error), 'error');
      } finally {
        $('proxyDelayBtn').disabled = false;
      }
    }

    async function runProxyDiagnostics(autoSwitch = false) {
      const group = selectedProxyGroup();
      const limit = Math.max(1, Math.min(Number($('proxyLimitInput').value || 24), 80));
      if (!group) {
        return;
      }
      $('proxyAiCheckBtn').disabled = true;
      $('proxyAiSwitchBtn').disabled = true;
      $('proxyStatus').className = 'provider-status pending';
      $('proxyStatus').textContent = autoSwitch ? '正在检测 AI 可用性并准备切换。' : '正在并发测试节点延迟；当前活动节点不会改变。';
      try {
        const result = await bridge('/proxy/diagnostics', {
          method: 'POST',
          body: JSON.stringify({ limit, group: group.name, auto_switch: autoSwitch }),
        });
        state.proxyDiagnostics = result;
        (result.results || []).forEach((item) => {
          if (autoSwitch) state.proxyAiResults[item.name] = { ok: Boolean(item.ok), tests: item.tests || [] };
          else state.proxyDelays[item.name] = { ok: Boolean(item.ok), delay: item.delay, error: item.error || '' };
        });
        if (result.switched && result.usable_node) {
          group.now = result.usable_node;
        }
        state.proxySort = autoSwitch && result.usable ? 'ai' : autoSwitch ? state.proxySort : 'delay';
        $('proxySortSelect').value = state.proxySort;
        renderProxyPage(result.recommendation || (autoSwitch ? 'AI 可用性检测完成。' : '只读节点体检完成。'));
        setConnection(result.recommendation || '代理节点检测完成。', result.usable ? 'ok' : 'error');
      } catch (error) {
        $('proxyStatus').className = 'provider-status error';
        $('proxyStatus').textContent = error.message || String(error);
        setConnection(error.message || String(error), 'error');
      } finally {
        $('proxyAiCheckBtn').disabled = false;
        $('proxyAiSwitchBtn').disabled = false;
      }
    }

    $('proxySubscriptions').addEventListener('click', (event) => {
      const button = event.target.closest('button[data-proxy-sub-action]');
      const edit = event.target.closest('button[data-proxy-sub-edit]');
      if (edit) editProxySubscription(edit.dataset.proxySubEdit);
      if (button && !button.disabled) operateProxySubscription(button.dataset.proxySubAction, button.dataset.proxySubKey);
    });
