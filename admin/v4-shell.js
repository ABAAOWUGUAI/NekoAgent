'use strict';

const V4_IMPLEMENTATION_STATES = Object.freeze({
  legacy: Object.freeze({ label: '保留旧路径', description: '完整功能仍由旧控制台提供。' }),
  partial: Object.freeze({ label: '新版日用层', description: '日常入口在新版；受保护或管理操作仍回到旧控制台。' }),
});
const V4_OWNER_SURFACES = Object.freeze([
  { id: 'overview', label: '概览', section: '小菲', legacyViews: ['overview'], implementationState: 'partial', implementationNote: '新版概览层；现有读模型保持原路径' },
  { id: 'qq', label: 'QQ', section: '对话', legacyViews: ['qq'], implementationState: 'legacy' },
  { id: 'chat', label: 'AI Chat', section: '对话', legacyViews: [], fallbackLegacyView: 'overview', fallbackLabel: '日常空间', implementationState: 'partial', implementationNote: '新版对话前台；连续上下文与受保护操作仍复用既有路径' },
  { id: 'work', label: '工作', section: '工作', legacyViews: ['tasks', 'projects', 'automations'], implementationState: 'legacy' },
  { id: 'artifact', label: '成品', section: '工作', legacyViews: ['artifacts'], implementationState: 'partial', implementationNote: '新版日用层；完整成品库仍使用旧控制台' },
  { id: 'memory', label: '记忆', section: '小菲', legacyViews: ['brain'], implementationState: 'legacy' },
  { id: 'assistant', label: '小菲', section: '小菲', legacyViews: ['assistant', 'relationship', 'social', 'growth'], implementationState: 'legacy' },
  { id: 'console', label: 'Console', section: '后台', legacyViews: ['models', 'capabilities', 'proxy', 'services', 'logs'], console: true, implementationState: 'legacy' },
  { id: 'settings', label: '设置', section: '后台', legacyViews: ['settings'], implementationState: 'legacy' },
]);
const V4_LEGACY_VIEW_OWNERS = Object.freeze(Object.fromEntries(
  V4_OWNER_SURFACES.flatMap((surface) => surface.legacyViews.map((view) => [view, surface.id])),
));
const V4_SHELL_CONTRACT = Object.freeze({
  ownerSurfaces: V4_OWNER_SURFACES,
  legacyViewOwners: V4_LEGACY_VIEW_OWNERS,
  implementationStates: V4_IMPLEMENTATION_STATES,
});

if (typeof module !== 'undefined' && module.exports) module.exports = V4_SHELL_CONTRACT;

if (typeof window !== 'undefined' && typeof document !== 'undefined') (() => {
  const EXPERIENCE_PARAM = 'experience';
  const V4_VALUE = 'v4';
  const V4_SESSION_KEY = 'nekoagent.v4-shell';
  const routes = V4_OWNER_SURFACES;
  const routeById = new Map(routes.map((route) => [route.id, route]));
  const $ = (selector, root = document) => root.querySelector(selector);
  let mounted = false;
  let selectedRouteId = 'overview';
  let explicitTransitionRouteId = '';
  let commandNavigationInProgress = false;

  function readSessionFlag() { try { return window.sessionStorage.getItem(V4_SESSION_KEY) === V4_VALUE; } catch (_) { return false; } }
  function writeSessionFlag(enabled) { try { if (enabled) window.sessionStorage.setItem(V4_SESSION_KEY, V4_VALUE); else window.sessionStorage.removeItem(V4_SESSION_KEY); } catch (_) { /* Query opt-in remains sufficient. */ } }
  function isRequested() { const requested = new URLSearchParams(window.location.search).get(EXPERIENCE_PARAM); return requested === null ? readSessionFlag() : requested === V4_VALUE; }
  function updateUrl(enabled) { const url = new URL(window.location.href); if (enabled) url.searchParams.set(EXPERIENCE_PARAM, V4_VALUE); else url.searchParams.delete(EXPERIENCE_PARAM); window.history.replaceState({}, '', url); }
  function currentLegacyView() { return $('.view:not(.hidden)')?.id.replace(/^view-/, '') || 'overview'; }
  function legacyViewFor(route) { return route.fallbackLegacyView || route.legacyViews[0] || 'overview'; }
  function ownerForLegacyView(view) { return routeById.get(V4_LEGACY_VIEW_OWNERS[view]) || null; }

  function showFallback(message) {
    const note = $('#v4FallbackNote');
    if (!note) return;
    note.textContent = message;
    note.hidden = false;
    window.clearTimeout(showFallback.timer);
    showFallback.timer = window.setTimeout(() => { note.hidden = true; }, 4800);
  }

  function renderSelection({ explicitRouteId = '', legacyView = currentLegacyView() } = {}) {
    const activeLegacyView = legacyView;
    const observedOwner = ownerForLegacyView(activeLegacyView);
    const route = routeById.get(explicitRouteId) || observedOwner;
    selectedRouteId = route?.id || '';
    const routeId = route?.id || 'legacy-unknown';
    document.body.dataset.v4ActiveView = routeId;
    document.querySelectorAll('[data-v4-route]').forEach((button) => {
      if (route && button.dataset.v4Route === route.id) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
    if (!route) showFallback(`未识别的旧控制台视图“${activeLegacyView}”仍保持原样，未被归入概览。`);
    document.dispatchEvent(new CustomEvent('nekoagent:v4-route-change', {
      detail: { routeId, legacyView: activeLegacyView, implementationState: route?.implementationState || 'legacy-unknown' },
    }));
  }

  function switchTo(route, { announceFallback = true, onBeforeNavigate = null } = {}) {
    if (!route || typeof window.switchView !== 'function') return;
    const legacyView = legacyViewFor(route);
    onBeforeNavigate?.();
    explicitTransitionRouteId = route.id;
    try { window.switchView(legacyView, { focusHeading: true }); }
    finally { explicitTransitionRouteId = ''; }
    if (route.implementationState === 'legacy' && announceFallback) showFallback(`${route.label} 当前通过既有“${route.fallbackLabel || route.label}”路径打开；旧页面仍保留全部操作。`);
  }

  function createRouteButton(route, className = 'v4-nav-item', { onBeforeNavigate = null, command = false } = {}) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = className;
    button.dataset.v4Route = route.id;
    button.dataset.v4Console = route.console ? 'true' : 'false';
    button.dataset.v4ImplementationState = route.implementationState;
    const label = document.createElement('span');
    label.className = command ? '' : 'v4-nav-label';
    label.textContent = route.label;
    button.append(label);
    const detail = document.createElement(command ? 'small' : 'em');
    detail.textContent = route.implementationNote || (route.fallbackLabel ? `旧${route.fallbackLabel}` : V4_IMPLEMENTATION_STATES[route.implementationState].label);
    button.append(detail);
    button.addEventListener('click', () => {
      if (command) commandNavigationInProgress = true;
      switchTo(route, { onBeforeNavigate });
    });
    return button;
  }

  function buildSidebar() {
    const sidebar = document.createElement('aside');
    sidebar.id = 'v4ShellSidebar';
    sidebar.className = 'v4-shell-sidebar';
    sidebar.setAttribute('aria-label', '新版工作区导航');
    sidebar.innerHTML = '<div class="v4-brand"><span class="v4-brand-avatar" aria-hidden="true"></span><span><strong>当前 Assistant</strong><small>私人 AI 工作区</small></span></div><div class="v4-presence"><span class="v4-presence-mark" aria-hidden="true"></span><span><strong>从概览继续</strong><small>对话、工作与记忆在同一工作区</small></span></div><nav class="v4-primary-nav" aria-label="V4.1 主导航"></nav><div class="v4-sidebar-footer"><button id="v4ReturnLegacyBtn" type="button">返回旧版界面</button><button id="v4LogoutBtn" type="button">退出登录</button></div>';
    const nav = $('.v4-primary-nav', sidebar);
    for (const sectionName of ['小菲', '对话', '工作', '后台']) {
      const section = document.createElement('section');
      section.className = 'v4-nav-section';
      const heading = document.createElement('p');
      heading.textContent = sectionName;
      section.append(heading);
      routes.filter((route) => route.section === sectionName).forEach((route) => section.append(createRouteButton(route)));
      nav.append(section);
    }
    $('#v4ReturnLegacyBtn', sidebar).addEventListener('click', () => disable({ restoreFocus: true }));
    $('#v4LogoutBtn', sidebar).addEventListener('click', () => $('#logoutBtn')?.click());
    return sidebar;
  }

  function buildCommandDialog() {
    const dialog = document.createElement('dialog');
    dialog.id = 'v4CommandPalette';
    dialog.className = 'v4-command-dialog';
    dialog.setAttribute('aria-labelledby', 'v4CommandTitle');
    dialog.innerHTML = '<form class="v4-command-form" method="dialog"><div class="v4-command-heading"><strong id="v4CommandTitle">前往工作区</strong><button value="cancel" type="submit" aria-label="关闭工作区导航">关闭</button></div><label><span class="visually-hidden">筛选工作区</span><input id="v4CommandQuery" type="search" autocomplete="off" placeholder="搜索工作区"></label><div id="v4CommandList" class="v4-command-list" aria-label="工作区结果"></div></form>';
    const list = $('#v4CommandList', dialog);
    const render = () => {
      const query = $('#v4CommandQuery', dialog).value.trim().toLocaleLowerCase();
      list.replaceChildren();
      routes.filter((route) => !query || `${route.label} ${route.section}`.toLocaleLowerCase().includes(query)).forEach((route) => list.append(createRouteButton(route, 'v4-command-route', { onBeforeNavigate: () => dialog.close(), command: true })));
    };
    $('#v4CommandQuery', dialog).addEventListener('input', render);
    dialog.addEventListener('close', () => {
      const restoreCommandTrigger = !commandNavigationInProgress;
      commandNavigationInProgress = false;
      if (restoreCommandTrigger) window.requestAnimationFrame(() => $('#v4CommandTrigger')?.focus({ preventScroll: true }));
    });
    render();
    return dialog;
  }

  function buildOverviewLens() {
    const lens = document.createElement('section');
    lens.id = 'v4OverviewLens';
    lens.className = 'v4-overview-lens';
    lens.setAttribute('aria-labelledby', 'v4OverviewLensTitle');
    lens.innerHTML = '<span class="v4-overview-mark" aria-hidden="true"></span><div><h2 id="v4OverviewLensTitle">从这里继续</h2><p>概览保留现有真实读模型；对话、工作和记忆通过各自的对象路径继续。</p></div><div class="v4-overview-actions"><button type="button" data-v4-lens="qq">查看 QQ</button><button type="button" data-v4-lens="work">查看工作</button><button type="button" data-v4-lens="memory">查看记忆</button></div>';
    lens.addEventListener('click', (event) => { const target = event.target.closest('button[data-v4-lens]'); if (target) switchTo(routeById.get(target.dataset.v4Lens)); });
    return lens;
  }

  function refreshToggle() {
    const enabled = document.body.dataset.v4Experience === 'active';
    const toggle = $('#v4ShellToggle');
    const command = $('#v4CommandTrigger');
    if (toggle) { toggle.textContent = enabled ? '返回旧版界面' : '使用新版界面'; toggle.setAttribute('aria-pressed', String(enabled)); }
    if (command) command.hidden = !enabled;
  }
  function openCommand() { const dialog = $('#v4CommandPalette'); if (!dialog || typeof dialog.showModal !== 'function' || dialog.open) return; dialog.showModal(); const query = $('#v4CommandQuery', dialog); query.value = ''; query.dispatchEvent(new Event('input')); query.focus(); }
  function addToolbarControls() {
    const toolbar = $('.toolbar');
    if (!toolbar) return;
    const command = document.createElement('button');
    command.id = 'v4CommandTrigger'; command.type = 'button'; command.className = 'v4-command-trigger'; command.setAttribute('aria-label', '前往工作区');
    command.innerHTML = '<span class="v4-command-trigger-symbol" aria-hidden="true">导航</span><span>前往工作区</span><kbd>Ctrl K</kbd>';
    command.addEventListener('click', openCommand);
    const toggle = document.createElement('button');
    toggle.id = 'v4ShellToggle'; toggle.type = 'button'; toggle.className = 'v4-shell-toggle';
    toggle.addEventListener('click', () => (document.body.dataset.v4Experience === 'active' ? disable() : enable()));
    toolbar.prepend(toggle); toolbar.prepend(command); refreshToggle();
  }
  function enable() {
    writeSessionFlag(true); updateUrl(true); document.body.dataset.v4Experience = 'active';
    document.dispatchEvent(new CustomEvent('nekoagent:v4-experience-enable'));
    refreshToggle(); renderSelection();
  }
  function restoreLegacyFocus() {
    window.requestAnimationFrame(() => $('#viewTitle')?.focus({ preventScroll: true }));
  }
  function disable({ restoreFocus = false } = {}) {
    writeSessionFlag(false); updateUrl(false); explicitTransitionRouteId = '';
    delete document.body.dataset.v4Experience; delete document.body.dataset.v4ActiveView;
    document.dispatchEvent(new CustomEvent('nekoagent:v4-experience-disable'));
    const dialog = $('#v4CommandPalette'); if (dialog?.open) dialog.close();
    $('#v4FallbackNote')?.setAttribute('hidden', '');
    refreshToggle();
    if (restoreFocus) restoreLegacyFocus();
  }
  function observeLegacyNavigation() {
    const existing = window.switchView;
    if (typeof existing !== 'function' || existing.__v4OwnerTracking) return;
    const tracked = function trackedSwitchView(view, options) {
      // A view transition, not an arbitrary DOM mutation, is the only source
      // of V4 navigation truth.  The explicit Owner survives a compatibility
      // fallback such as AI Chat -> legacy Overview.
      const explicitRouteId = explicitTransitionRouteId;
      const result = existing.call(this, view, options);
      renderSelection({ explicitRouteId, legacyView: currentLegacyView() });
      return result;
    };
    tracked.__v4OwnerTracking = true;
    window.switchView = tracked;
  }
  function mount() {
    if (mounted || !$('#appShell') || !$('.main')) return;
    mounted = true;
    observeLegacyNavigation();
    $('#appShell').prepend(buildSidebar());
    $('#contentViewport').prepend(buildOverviewLens());
    document.body.append(buildCommandDialog());
    const fallback = document.createElement('p');
    fallback.id = 'v4FallbackNote'; fallback.className = 'v4-fallback-note'; fallback.setAttribute('role', 'status'); fallback.setAttribute('aria-live', 'polite'); fallback.hidden = true;
    document.body.append(fallback);
    addToolbarControls();
    document.addEventListener('keydown', (event) => {
      const dialog = $('#v4CommandPalette');
      if (event.key === 'Escape' && dialog?.open) { event.preventDefault(); dialog.close(); return; }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k' && document.body.dataset.v4Experience === 'active') { event.preventDefault(); openCommand(); }
    });
    if (isRequested()) enable(); else renderSelection();
  }
  document.addEventListener('DOMContentLoaded', mount, { once: true });
})();
