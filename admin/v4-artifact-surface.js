'use strict';

/* V4.1 Artifact daily layer. The existing Artifact Center remains the owner of
 * revisions, deletion, publication grants, and immutable-version operations. */
(() => {
  const routeEvent = 'nekoagent:v4-route-change';
  const artifactEndpoint = '/assistant/artifacts?limit=6&offset=0';
  const versionStateLabels = Object.freeze({
    preparing: '准备中',
    available: '已就绪',
    failed: '生成失败',
  });
  let root = null;
  let requestVersion = 0;
  let active = false;

  function currentItems(payload) {
    return (Array.isArray(payload?.items) ? payload.items : []).filter((item) => {
      const version = item?.current_version || {};
      return !item?.deleted_at
        && String(item?.state || '').toLowerCase() !== 'deleted'
        && String(version.state || '').toLowerCase() !== 'deleted';
    });
  }

  function formatUpdatedAt(value) {
    if (!value) return '未记录更新时间';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', { hour12: false });
  }

  function dailyState(item) {
    const state = String(item?.current_version?.state || '').toLowerCase();
    return { code: state || 'unavailable', label: versionStateLabels[state] || '暂无当前版本' };
  }

  function sourceWorkLabel(item) {
    return item?.source_goal_id ? '已关联来源任务' : '未关联来源任务';
  }

  function makeNode(tagName, className, text) {
    const node = document.createElement(tagName);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function openFullManagement() {
    if (!root) return;
    document.body.setAttribute('data-v4-artifact-legacy-mode', 'true');
    root.hidden = true;
    root.inert = true;
    active = false;
    requestVersion += 1;
    document.getElementById('view-artifacts')?.scrollIntoView({ block: 'start' });
  }

  function render(items, { loading = false, failed = false } = {}) {
    if (!root) return;
    root.replaceChildren();
    const header = makeNode('header', 'v4-artifact-daily-header');
    const eyebrow = makeNode('p', 'v4-surface-eyebrow', 'Artifact · 成品');
    const title = makeNode('h2', '', loading ? '正在读取最近成品' : '最近成品');
    title.id = 'v4ArtifactDailyTitle';
    const description = makeNode('p', 'v4-artifact-daily-copy', failed
      ? '暂时无法读取摘要。完整库管理仍可用于查看现有成品与受保护操作。'
      : '这里显示真实的近期成品。修改、删除、版本和预览授权仍在完整库管理中完成。');
    header.append(eyebrow, title, description);
    const manage = makeNode('button', 'v4-artifact-manage', '打开完整成品库');
    manage.type = 'button';
    manage.addEventListener('click', openFullManagement);
    header.append(manage);
    root.append(header);

    const status = makeNode('p', 'v4-artifact-daily-status', loading
      ? '正在同步现有成品记录…'
      : failed ? '读取摘要失败；没有尝试任何写入操作。' : `${items.length} 项当前成品`);
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    root.append(status);

    if (loading || failed) return;
    if (!items.length) {
      root.append(makeNode('p', 'v4-artifact-empty', '没有可展示的当前成品。'));
      return;
    }
    const list = makeNode('ul', 'v4-artifact-daily-list');
    items.forEach((item) => {
      const version = item.current_version || {};
      const state = dailyState(item);
      const entry = makeNode('li', 'v4-artifact-daily-item');
      const copy = makeNode('div', 'v4-artifact-daily-item-copy');
      const stateBadge = makeNode('span', 'v4-artifact-item-state', state.label);
      stateBadge.dataset.status = state.code;
      copy.append(
        makeNode('strong', '', item.title || '未命名成品'),
        stateBadge,
        makeNode('span', '', item.summary || '未提供摘要。'),
      );
      const meta = makeNode('span', 'v4-artifact-daily-meta', [
        item.kind || 'artifact',
        sourceWorkLabel(item),
        version.version_number ? `版本 ${version.version_number}` : '尚无版本',
        formatUpdatedAt(item.updated_at),
      ].join(' · '));
      const inspect = makeNode('button', 'v4-artifact-inspect', '打开完整成品库');
      inspect.type = 'button';
      inspect.addEventListener('click', openFullManagement);
      entry.append(copy, meta, inspect);
      list.append(entry);
    });
    root.append(list);
  }

  async function loadArtifacts() {
    const version = ++requestVersion;
    render([], { loading: true });
    try {
      if (typeof window.bridge !== 'function') throw new Error('artifact_bridge_unavailable');
      const payload = await window.bridge(artifactEndpoint);
      if (version !== requestVersion) return;
      render(currentItems(payload));
    } catch (_) {
      if (version === requestVersion) render([], { failed: true });
    }
  }

  function activate() {
    if (!root) return;
    const shouldLoad = !active || root.hidden || document.body.hasAttribute('data-v4-artifact-legacy-mode');
    document.body.removeAttribute('data-v4-artifact-legacy-mode');
    root.hidden = false;
    root.inert = false;
    active = true;
    if (shouldLoad) loadArtifacts();
  }

  function deactivate() {
    if (!root) return;
    requestVersion += 1;
    active = false;
    root.hidden = true;
    root.inert = true;
    document.body.removeAttribute('data-v4-artifact-legacy-mode');
  }

  function mount() {
    const viewport = document.getElementById('contentViewport');
    if (!viewport || root) return;
    root = makeNode('section', 'v4-artifact-daily');
    root.id = 'v4ArtifactDaily';
    root.hidden = true;
    root.inert = true;
    root.setAttribute('aria-labelledby', 'v4ArtifactDailyTitle');
    viewport.prepend(root);
    document.addEventListener(routeEvent, (event) => {
      if (event.detail?.routeId === 'artifact' && document.body.dataset.v4Experience === 'active') activate();
      else deactivate();
    });
    document.addEventListener('nekoagent:v4-experience-disable', deactivate);
    if (document.body.dataset.v4Experience === 'active' && document.body.dataset.v4ActiveView === 'artifact') activate();
  }

  document.addEventListener('DOMContentLoaded', mount, { once: true });
})();
