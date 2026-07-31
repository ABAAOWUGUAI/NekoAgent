function renderNetworkPolicy() {
  const policy = state.networkPolicy || {};
  const capabilityEnabled = policy.base_mode !== 'off';
  const searchActive = Boolean(policy.owner_web_search_active);
  const remainingMinutes = Math.max(0, Math.ceil(Number(policy.owner_web_search_remaining_seconds || 0) / 60));
  $('networkPolicySummary').innerHTML = [
    ['受控 Capability', capabilityEnabled ? '允许' : '关闭', capabilityEnabled ? '仅代码注册的固定来源' : '外部来源读取已禁用', capabilityEnabled ? 'green' : 'red'],
    ['Owner Web Search', searchActive ? '临时开启' : '未开启', searchActive ? `约 ${remainingMinutes} 分钟后失效` : '不会传给后台任务', searchActive ? 'amber' : 'blue'],
    ['Shell 原始网络', '关闭', '不会授予 CAP_NET_ADMIN 或完整网络', 'green'],
    ['运行要求', '仅 Codex', '需 codex_login 执行器', 'blue'],
  ].map(([label, value, detail, tone], index) => `<article class="insight-card ${tone}"><span class="insight-index">0${index + 1}</span><p>${escapeHtml(label)}</p><strong>${escapeHtml(value)}</strong><small>${escapeHtml(detail)}</small></article>`).join('');
  $('networkBaseMode').value = policy.base_mode || 'capability_only';
  $('ownerWebSearchEnabled').checked = searchActive;
  $('ownerWebSearchEnabled').disabled = !capabilityEnabled;
  $('ownerWebSearchTtl').disabled = !capabilityEnabled || !$('ownerWebSearchEnabled').checked;
  $('networkPolicyEvents').innerHTML = state.networkPolicyEvents.length
    ? state.networkPolicyEvents.map((item) => `<li><strong>${item.action === 'owner_web_search_enabled' ? '已开启 Owner Web Search' : '已更新网络策略'}</strong><span> · ${item.channel === 'qq' ? 'QQ' : '控制台'} · ${escapeHtml(new Date(item.created_at).toLocaleString('zh-CN', { hour12: false }))}</span></li>`).join('')
    : '<li class="empty-state">暂无策略变更。</li>';
}

async function loadNetworkPolicy() {
  const result = await bridge('/assistant/network-policy');
  state.networkPolicy = result.policy || null;
  state.networkPolicyEvents = result.events || [];
  renderNetworkPolicy();
  return state.networkPolicy;
}

async function saveNetworkPolicy(event) {
  event.preventDefault();
  const button = $('saveNetworkPolicyBtn');
  button.disabled = true;
  $('networkPolicyStatus').textContent = '正在保存网络策略……';
  try {
    const result = await bridge('/assistant/network-policy', {
      method: 'POST',
      body: JSON.stringify({
        base_mode: $('networkBaseMode').value,
        owner_web_search_enabled: $('ownerWebSearchEnabled').checked,
        ttl_minutes: Number($('ownerWebSearchTtl').value || 240),
        version: state.networkPolicy?.version,
      }),
    });
    state.networkPolicy = result.policy || null;
    state.networkPolicyEvents = result.events || [];
    renderNetworkPolicy();
    $('networkPolicyStatus').textContent = '网络策略已保存并立即生效。';
    setConnection('网络策略已保存。', 'ok');
  } catch (error) {
    $('networkPolicyStatus').textContent = `保存失败：${error.message || String(error)}`;
    throw error;
  } finally {
    button.disabled = false;
  }
}

$('networkPolicyForm').addEventListener('submit', (event) => saveNetworkPolicy(event).catch((error) => setConnection(error.message || String(error), 'error')));
$('refreshNetworkPolicyBtn').addEventListener('click', () => loadNetworkPolicy().then(() => {
  $('networkPolicyStatus').textContent = '网络策略已刷新。';
}).catch((error) => setConnection(error.message || String(error), 'error')));
$('networkBaseMode').addEventListener('change', () => {
  const enabled = $('networkBaseMode').value !== 'off';
  if (!enabled) $('ownerWebSearchEnabled').checked = false;
  $('ownerWebSearchEnabled').disabled = !enabled;
  $('ownerWebSearchTtl').disabled = !enabled || !$('ownerWebSearchEnabled').checked;
});
$('ownerWebSearchEnabled').addEventListener('change', () => {
  $('ownerWebSearchTtl').disabled = !$('ownerWebSearchEnabled').checked;
});

window.renderNetworkPolicy = renderNetworkPolicy;
window.loadNetworkPolicy = loadNetworkPolicy;
