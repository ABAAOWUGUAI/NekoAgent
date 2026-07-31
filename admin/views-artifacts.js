/* Gate 7 Artifact Center: immutable versions, protected preview and audit evidence. */
(() => {
  const artifactCenterState = {
    items: [],
    query: '',
    selectedId: '',
    selected: null,
    versions: [],
    events: [],
    pendingAction: '',
    bound: false,
    detailRequest: 0,
  };

  const kindLabels = {
    file: '文件',
    report: '报告',
    presentation: '演示文稿',
    image: '图片',
    archive: '压缩包',
    static_site: '静态网站',
  };
  const publicationLabels = {
    active: '预览开放',
    stopped: '预览已停止',
    expired: '预览已过期',
    deleted: '预览已删除',
  };
  const versionLabels = {
    preparing: '正在生成',
    available: '可下载',
    failed: '生成失败',
  };
  const eventLabels = {
    'artifact.created': '已登记成品',
    'artifact.version_preparing': '开始生成版本',
    'artifact.version_published': '版本已发布',
    'artifact.version_failed': '版本生成失败',
    'artifact.deleted': '成品已删除',
    'preview.published': '静态预览已发布',
    'preview.grant_created': '已创建一次性预览链接',
    'preview.stopped': '预览已停止',
    'preview.restored': '预览已恢复',
    'preview.extended': '预览有效期已延长',
    'preview.deleted': '预览已删除',
    'preview.access_revoked': '预览访问已撤销',
  };
  const prerequisiteLabels = {
    identity_enabled: '助手身份边界',
    memory_scope_enabled: '记忆作用域',
    daily_shell_enabled: '日常空间',
    interaction_plan_enabled: '多意图交互',
    formal_approval_enabled: '正式确认',
    storage_reconciled: '成品存储',
    broker_ready: '预览授权服务',
    preview_origin_isolated: '独立预览来源',
    admin_cookie_secure: '安全管理会话',
    tailscale_service_verified: '预览访问入口',
  };

  function artifactStatus(message, tone = '') {
    const node = $('artifactCenterStatus');
    if (!node) return;
    node.textContent = message;
    if (tone) node.dataset.tone = tone;
    else delete node.dataset.tone;
  }

  function formatArtifactDate(value) {
    if (!value) return '未设置';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString('zh-CN', { hour12: false });
  }

  function formatArtifactBytes(value) {
    const bytes = Math.max(0, Number(value) || 0);
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`;
    return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  }

  function artifactSummaryLine(item) {
    const version = item.current_version || {};
    const parts = [kindLabels[item.kind] || item.kind || '成品'];
    if (version.version_number) parts.push(`版本 ${version.version_number}`);
    if (item.updated_at) parts.push(formatArtifactDate(item.updated_at));
    return parts.join(' · ');
  }

  function isCurrentArtifact(item) {
    const version = item?.current_version || {};
    return !item?.deleted_at
      && String(item?.state || '').toLowerCase() !== 'deleted'
      && String(version.state || '').toLowerCase() !== 'deleted';
  }

  function filteredArtifacts() {
    const query = artifactCenterState.query;
    if (!query) return artifactCenterState.items;
    return artifactCenterState.items.filter((item) => (
      `${item.title || ''} ${item.summary || ''} ${kindLabels[item.kind] || item.kind || ''}`
        .normalize('NFKC').toLocaleLowerCase('zh-CN').includes(query)
    ));
  }

  function renderArtifactList() {
    const list = $('artifactList');
    if (!list) return;
    const filtered = filteredArtifacts();
    list.innerHTML = filtered.map((item) => `
      <li class="artifact-list-item">
        <button class="artifact-list-button" type="button" data-artifact-id="${escapeHtml(item.id)}"
          ${item.id === artifactCenterState.selectedId ? 'aria-current="true"' : ''}>
          <strong>${escapeHtml(item.title || '未命名成品')}</strong>
          <span>${escapeHtml(artifactSummaryLine(item))}</span>
        </button>
      </li>`).join('');
    $('artifactEmptyState').hidden = artifactCenterState.items.length !== 0;
    $('artifactNoResults').hidden = !(artifactCenterState.items.length && filtered.length === 0);
    const archived = Number(artifactCenterState.archivedCount || 0);
    $('artifactLibraryMeta').textContent = artifactCenterState.items.length
      ? `${filtered.length} / ${artifactCenterState.items.length} 项可用${archived ? ` · ${archived} 项历史记录已收起` : ''}`
      : archived ? `${archived} 项历史记录已收起` : '没有已登记成品。';
  }

  function renderPrerequisites(plan) {
    const target = $('artifactPrerequisiteList');
    const prerequisites = plan?.prerequisites || {};
    const rows = Object.entries(prerequisiteLabels).map(([key, label]) => {
      const ready = Boolean(prerequisites[key]);
      return `<div><dt>${escapeHtml(label)}</dt><dd data-ready="${ready}">${ready ? '已就绪' : '待完成'}</dd></div>`;
    });
    target.innerHTML = rows.join('');
  }

  async function showArtifactDisabledState(plan = null) {
    $('artifactCenterWorkspace').hidden = true;
    $('artifactDisabledState').hidden = false;
    $('artifactCount').textContent = '0';
    artifactStatus('成品中心未启用；未进行任何发布或访问操作。');
    try {
      if (plan) {
        renderPrerequisites(plan);
        return;
      }
      const payload = await bridge('/assistant/artifact/cutover-plan');
      renderPrerequisites(payload.result || {});
    } catch (error) {
      $('artifactPrerequisiteList').innerHTML = '';
      artifactStatus(`成品中心未启用；就绪检查暂不可用：${error.message || String(error)}`, 'error');
    }
  }

  function artifactFacts(item) {
    const version = item.current_version || {};
    const facts = [
      ['当前版本', version.version_number ? `版本 ${version.version_number}` : '尚无可用版本'],
      ['文件数量', version.file_count === undefined ? '未提供' : `${version.file_count} 个`],
      ['总大小', version.total_bytes === undefined ? '未提供' : formatArtifactBytes(version.total_bytes)],
      ['最后更新', formatArtifactDate(item.updated_at)],
      ['来源任务', item.source_goal_id || '未关联'],
      ['保留至', formatArtifactDate(version.retention_expires_at)],
    ];
    return facts.map(([term, value]) => (
      `<div><dt>${escapeHtml(term)}</dt><dd>${escapeHtml(value)}</dd></div>`
    )).join('');
  }

  function renderVersions(items, currentVersionId) {
    const target = $('artifactVersionList');
    if (!items.length) {
      target.innerHTML = '<li class="artifact-version-item"><strong>尚无版本</strong><p>任务还没有登记可下载内容。</p></li>';
      return;
    }
    target.innerHTML = items.map((version) => {
      const current = version.id === currentVersionId ? ' · 当前' : '';
      const checksum = version.manifest_sha256 ? `校验 ${version.manifest_sha256}` : '尚无校验值';
      const failure = version.failure_reason ? `<p>失败原因：${escapeHtml(version.failure_reason)}</p>` : '';
      const download = version.state === 'available'
        ? `<p class="artifact-version-actions"><a href="/assistant/artifacts/versions/${encodeURIComponent(version.id)}/download">下载此版本</a></p>`
        : '';
      return `<li class="artifact-version-item">
        <strong>版本 ${escapeHtml(version.version_number)}${escapeHtml(current)} · ${escapeHtml(versionLabels[version.state] || version.state)}</strong>
        <p>${escapeHtml(formatArtifactDate(version.created_at))} · ${escapeHtml(version.file_count)} 个文件 · ${escapeHtml(formatArtifactBytes(version.total_bytes))}</p>
        <p>${escapeHtml(checksum)}</p>${failure}${download}
      </li>`;
    }).join('');
  }

  function eventDetail(event) {
    const detail = event.detail || {};
    const parts = [];
    if (detail.file_count !== undefined) parts.push(`${detail.file_count} 个文件`);
    if (detail.total_bytes !== undefined) parts.push(formatArtifactBytes(detail.total_bytes));
    if (detail.reason) parts.push(String(detail.reason));
    return parts.join(' · ');
  }

  function renderEvents(items) {
    const target = $('artifactEventList');
    if (!items.length) {
      target.innerHTML = '<li class="artifact-event-item"><strong>暂无事件</strong><p>服务端尚未返回事件记录。</p></li>';
      return;
    }
    target.innerHTML = items.map((event) => {
      const detail = eventDetail(event);
      return `<li class="artifact-event-item">
        <strong>${escapeHtml(eventLabels[event.event_type] || event.event_type || '状态变化')}</strong>
        <p>${escapeHtml(formatArtifactDate(event.created_at))}${detail ? ` · ${escapeHtml(detail)}` : ''}</p>
      </li>`;
    }).join('');
  }

  function publicationButtons(publication) {
    if (!publication || publication.status === 'deleted') return '';
    const actions = [];
    if (publication.status === 'active') {
      actions.push(['grant', '创建预览链接', 'primary']);
      actions.push(['stop', '停止预览', 'secondary']);
      actions.push(['extend', '延长有效期', 'secondary']);
    } else {
      actions.push(['restore', '恢复预览', 'primary']);
    }
    actions.push(['delete-publication', '删除预览', 'danger']);
    return actions.map(([action, label, style]) => (
      `<button type="button" class="${style}" data-artifact-action="${action}">${label}</button>`
    )).join('');
  }

  function renderArtifactDetail(item, versions, events) {
    const version = item.current_version || {};
    const publication = item.publication || null;
    artifactCenterState.selected = item;
    artifactCenterState.versions = versions;
    artifactCenterState.events = events;
    $('artifactDetailEmpty').hidden = true;
    $('artifactDetail').hidden = false;
    $('artifactKindBadge').textContent = kindLabels[item.kind] || item.kind || '成品';
    $('artifactDetailTitle').textContent = item.title || '未命名成品';
    $('artifactDetailSummary').textContent = item.summary || '未提供摘要。';
    $('artifactFacts').innerHTML = artifactFacts(item);
    $('reviseArtifactBtn').disabled = version.state !== 'available';
    const badge = $('artifactPublicationBadge');
    badge.textContent = publication ? (publicationLabels[publication.status] || publication.status) : '没有静态预览';
    badge.dataset.status = publication?.status || 'none';
    $('artifactPublicationActions').innerHTML = publicationButtons(publication);
    $('artifactPublicationState').textContent = publication
      ? `访问状态：${publicationLabels[publication.status] || publication.status}；有效至 ${formatArtifactDate(publication.preview_expires_at)}。`
      : item.kind === 'static_site' ? '当前版本没有可管理的预览发布。' : '这项成品不包含静态网站预览。';
    renderVersions(versions, item.current_version_id);
    renderEvents(events);
  }

  async function selectArtifact(identifier, { focus = false } = {}) {
    if (!identifier) return;
    artifactCenterState.selectedId = identifier;
    renderArtifactList();
    const request = ++artifactCenterState.detailRequest;
    artifactStatus('正在读取成品详情。');
    try {
      const [detail, versions, events] = await Promise.all([
        bridge(`/assistant/artifacts/${encodeURIComponent(identifier)}`),
        bridge(`/assistant/artifacts/${encodeURIComponent(identifier)}/versions`),
        bridge(`/assistant/artifacts/${encodeURIComponent(identifier)}/events?limit=200`),
      ]);
      if (request !== artifactCenterState.detailRequest) return;
      renderArtifactDetail(detail.artifact || {}, versions.items || [], events.items || []);
      artifactStatus('成品详情已更新。', 'ok');
      if (focus) $('artifactDetailTitle').focus?.({ preventScroll: true });
    } catch (error) {
      if (request !== artifactCenterState.detailRequest) return;
      artifactStatus(error.message || String(error), 'error');
    }
  }

  async function loadArtifactCenter() {
    bindArtifactCenter();
    artifactStatus('正在刷新成品列表。');
    try {
      const cutover = await bridge('/assistant/artifact/cutover-plan');
      if (!cutover.result?.feature_enabled) {
        await showArtifactDisabledState(cutover.result || {});
        return;
      }
      const payload = await bridge('/assistant/artifacts?limit=100&offset=0');
      const allItems = Array.isArray(payload.items) ? payload.items : [];
      artifactCenterState.items = allItems.filter(isCurrentArtifact);
      artifactCenterState.archivedCount = allItems.length - artifactCenterState.items.length;
      $('artifactDisabledState').hidden = true;
      $('artifactCenterWorkspace').hidden = false;
      $('artifactCount').textContent = String(artifactCenterState.items.length);
      if (!artifactCenterState.items.some((item) => item.id === artifactCenterState.selectedId)) {
        artifactCenterState.selectedId = artifactCenterState.items[0]?.id || '';
      }
      renderArtifactList();
      if (artifactCenterState.selectedId) await selectArtifact(artifactCenterState.selectedId);
      else {
        artifactCenterState.selected = null;
        $('artifactDetail').hidden = true;
        $('artifactDetailEmpty').hidden = false;
        artifactStatus('成品中心已更新，目前没有成品。', 'ok');
      }
    } catch (error) {
      if (error.payload?.error === 'artifact_preview_disabled') {
        await showArtifactDisabledState();
        return;
      }
      $('artifactCenterWorkspace').hidden = true;
      $('artifactDisabledState').hidden = true;
      artifactStatus(error.message || String(error), 'error');
      throw error;
    }
  }

  function closeArtifactDialog(dialog) {
    if (dialog?.open) dialog.close();
  }

  function openArtifactAction(action) {
    const item = artifactCenterState.selected;
    if (!item) return;
    const publication = item.publication || {};
    const config = {
      stop: ['停止静态预览', '现有预览会立即失效；成品文件和版本仍会保留。', false, '停止预览'],
      restore: ['恢复静态预览', '恢复会创建新的访问代次，旧链接不会重新生效。', true, '恢复预览'],
      extend: ['延长预览有效期', '选择新的可访问时长；最长不超过版本保留期。', true, '延长有效期'],
      'delete-publication': ['删除静态预览', '预览访问会立即失效，且不能通过恢复按钮重新开放。成品版本仍保留。', false, '删除预览'],
      'delete-artifact': ['删除成品', '所有版本将停止访问并移出可服务目录。此操作不能在控制台撤销。', false, '删除成品'],
    }[action];
    if (!config) return;
    artifactCenterState.pendingAction = action;
    $('artifactActionHeading').textContent = config[0];
    $('artifactActionDescription').textContent = config[1];
    $('artifactTtlField').hidden = !config[2];
    $('confirmArtifactActionBtn').textContent = config[3];
    $('confirmArtifactActionBtn').className = action.includes('delete') ? 'danger' : 'primary';
    $('artifactActionStatus').textContent = '';
    $('artifactActionDialog').dataset.publicationId = publication.id || '';
    $('artifactActionDialog').showModal();
  }

  async function submitArtifactAction(event) {
    event.preventDefault();
    const item = artifactCenterState.selected;
    if (!item) return;
    const action = artifactCenterState.pendingAction;
    const button = $('confirmArtifactActionBtn');
    button.disabled = true;
    $('artifactActionStatus').textContent = '正在提交。';
    try {
      if (action === 'delete-artifact') {
        await bridge(`/assistant/artifacts/${encodeURIComponent(item.id)}/delete`, {
          method: 'POST', body: JSON.stringify({ expected_version: item.version }),
        });
        artifactCenterState.selectedId = '';
      } else {
        const publication = item.publication || {};
        const apiAction = action === 'delete-publication' ? 'delete' : action;
        await bridge(`/assistant/preview-publications/${encodeURIComponent(publication.id)}/${apiAction}`, {
          method: 'POST',
          body: JSON.stringify({
            expected_version: publication.version,
            expected_generation: publication.generation,
            ttl_seconds: Number($('artifactTtlInput').value) || 86400,
          }),
        });
      }
      closeArtifactDialog($('artifactActionDialog'));
      await loadArtifactCenter();
    } catch (error) {
      $('artifactActionStatus').textContent = error.message || String(error);
      $('artifactActionStatus').dataset.tone = 'error';
      if (error.status === 409) await selectArtifact(artifactCenterState.selectedId);
    } finally {
      button.disabled = false;
    }
  }

  async function createArtifactGrant() {
    const publication = artifactCenterState.selected?.publication;
    if (!publication) return;
    artifactStatus('正在创建一次性预览链接。');
    try {
      const payload = await bridge(`/assistant/preview-publications/${encodeURIComponent(publication.id)}/grant`, {
        method: 'POST', body: '{}',
      });
      const url = new URL(payload.grant?.activation_url || '');
      if (url.protocol !== 'https:' || url.origin === window.location.origin) throw new Error('预览链接未使用隔离的 HTTPS 来源。');
      const link = $('artifactGrantLink');
      link.href = url.href;
      $('artifactGrantExpiry').textContent = `激活链接有效至 ${formatArtifactDate(payload.grant?.expires_at)}。`;
      $('artifactGrantDialog').showModal();
      artifactStatus('一次性预览链接已创建。', 'ok');
      await selectArtifact(artifactCenterState.selectedId);
    } catch (error) {
      artifactStatus(error.message || String(error), 'error');
    }
  }

  async function submitArtifactRevision(event) {
    event.preventDefault();
    const item = artifactCenterState.selected;
    if (!item) return;
    const instruction = $('artifactRevisionInstruction').value.trim();
    const submit = event.submitter;
    submit.disabled = true;
    $('artifactReviseStatus').textContent = '正在创建修改任务。';
    try {
      const payload = await bridge(`/assistant/artifacts/${encodeURIComponent(item.id)}/revise`, {
        method: 'POST',
        body: JSON.stringify({ instruction, timeout: Number($('artifactRevisionTimeout').value) || 600 }),
      });
      closeArtifactDialog($('artifactReviseDialog'));
      $('artifactRevisionInstruction').value = '';
      artifactStatus(`修改任务已创建${payload.task?.id ? `：${payload.task.id}` : ''}。`, 'ok');
    } catch (error) {
      $('artifactReviseStatus').textContent = error.message || String(error);
      $('artifactReviseStatus').dataset.tone = 'error';
    } finally {
      submit.disabled = false;
    }
  }

  function bindArtifactCenter() {
    if (artifactCenterState.bound) return;
    artifactCenterState.bound = true;
    $('reloadArtifactsBtn').addEventListener('click', () => loadArtifactCenter());
    $('artifactSearchInput').addEventListener('input', (event) => {
      artifactCenterState.query = event.target.value.normalize('NFKC').toLocaleLowerCase('zh-CN').trim();
      renderArtifactList();
    });
    $('artifactList').addEventListener('click', (event) => {
      const button = event.target.closest('[data-artifact-id]');
      if (button) selectArtifact(button.dataset.artifactId, { focus: false });
    });
    $('artifactPublicationActions').addEventListener('click', (event) => {
      const action = event.target.closest('[data-artifact-action]')?.dataset.artifactAction;
      if (action === 'grant') createArtifactGrant();
      else if (action) openArtifactAction(action);
    });
    $('reviseArtifactBtn').addEventListener('click', () => {
      $('artifactReviseStatus').textContent = '';
      $('artifactReviseDialog').showModal();
    });
    $('deleteArtifactBtn').addEventListener('click', () => openArtifactAction('delete-artifact'));
    $('artifactReviseForm').addEventListener('submit', submitArtifactRevision);
    $('artifactActionForm').addEventListener('submit', submitArtifactAction);
    document.querySelectorAll('[data-close-artifact-dialog]').forEach((button) => {
      button.addEventListener('click', () => closeArtifactDialog(button.closest('dialog')));
    });
    $('artifactGrantDialog').addEventListener('close', () => {
      $('artifactGrantLink').removeAttribute('href');
      $('artifactGrantExpiry').textContent = '';
    });
    document.querySelectorAll('[data-artifact-go-tasks]').forEach((button) => {
      button.addEventListener('click', () => switchView('tasks', { focusHeading: true }));
    });
  }

  window.loadArtifactCenter = loadArtifactCenter;
})();
