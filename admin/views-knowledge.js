    const knowledgeKindLabels = {
      fact: '事实', preference: '偏好', procedure: '流程', reference: '参考资料',
      decision: '决策', lesson: '经验', current_state: '当前状态',
    };
    const knowledgeStatusLabels = { draft: '待审核', published: '已发布', archived: '已归档', rejected: '已拒绝' };
    const knowledgeFreshnessLabels = { fresh: '新鲜', stale: '待复核', expired: '已失效', unverified: '未验证' };
    const knowledgeLintLabels = {
      knowledge_stale: '有已过复核时间的知识',
      knowledge_expired: '有已失效的知识',
      knowledge_unverified: '有尚未验证来源或时效的知识',
      knowledge_orphan_source: '有缺少来源证据的知识',
      knowledge_unrelated: '有尚未建立关联的孤立知识',
      knowledge_possible_conflict: '发现同名但内容不同的潜在冲突',
    };
    let knowledgeWorkspaceInFlight = null;
    let knowledgeWorkspaceLoadedAt = 0;

    function renderKnowledgeWorkspace(workspace = {}, { cached = false } = {}) {
      state.knowledgeWorkspace = workspace;
      const counts = workspace.counts || {};
      const freshness = workspace.freshness || {};
      const metrics = [
        ['已发布 / 草稿', `${Number(counts.published || 0)} / ${Number(counts.draft || 0)}`],
        ['新鲜 / 待复核', `${Number(freshness.fresh || 0)} / ${Number(freshness.stale || 0)}`],
        ['不可变版本', Number(workspace.revision_count || 0)],
        ['检索留痕', Number(workspace.retrieval_audit_count || 0)],
      ];
      $('knowledgeWorkspaceSummary').innerHTML = metrics.map(([label, value]) => (
        `<div class="wiki-health-item"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`
      )).join('');
      $('knowledgeWorkspaceSummary').setAttribute('aria-busy', 'false');
      const warnings = (workspace.lints || []).filter((item) => item.severity === 'warning');
      const lintNode = $('knowledgeLintSummary');
      lintNode.hidden = warnings.length === 0;
      lintNode.textContent = warnings.length
        ? `需处理 ${warnings.length} 项：${[...new Set(warnings.slice(0, 4).map((item) => knowledgeLintLabels[item.code] || item.code))].join('；')}。`
        : '';
      const backend = workspace.search_backend === 'fts5_trigram' ? '本地全文索引' : '本地兼容检索';
      $('knowledgeWorkspaceStatus').textContent = `${cached ? '已显示缓存；' : ''}${backend} · 数据库作用域与人工审核仍是唯一权限事实源。`;
    }

    function applyKnowledgeWorkspace(workspace, { cached = false } = {}) {
      renderKnowledgeWorkspace(workspace, { cached });
      renderKnowledge(workspace.items || []);
      renderMemoryCandidates(workspace.memory_candidates || []);
      renderMemories(workspace.memories || []);
      state.conversationThreads = workspace.conversation_threads || [];
      const selector = $('conversationThreadSelect');
      selector.innerHTML = state.conversationThreads.length
        ? state.conversationThreads.map((thread, index) => (
          `<option value="${escapeHtml(thread.id || '')}">${escapeHtml(thread.channel_label || thread.channel_type || '对话')} ${index + 1}</option>`
        )).join('')
        : '<option value="">暂无对话线程</option>';
      renderConversation(workspace.recent_messages || []);
      state.currentProject = workspace.current_project || null;
      renderAssistantSummary();
    }

    async function loadKnowledgeWorkspace({ force = false } = {}) {
      if (!force && state.knowledgeWorkspace && Date.now() - knowledgeWorkspaceLoadedAt < 300000) {
        applyKnowledgeWorkspace(state.knowledgeWorkspace, { cached: true });
        return state.knowledgeWorkspace;
      }
      if (knowledgeWorkspaceInFlight) return knowledgeWorkspaceInFlight;
      if (state.knowledgeWorkspace) applyKnowledgeWorkspace(state.knowledgeWorkspace, { cached: true });
      else $('knowledgeWorkspaceStatus').textContent = '正在载入知识、作用域记忆和最近对话。';
      knowledgeWorkspaceInFlight = bridge('/assistant/knowledge/workspace')
        .then((payload) => payload.result || payload)
        .then((workspace) => {
          knowledgeWorkspaceLoadedAt = Date.now();
          applyKnowledgeWorkspace(workspace);
          return workspace;
        })
        .finally(() => { knowledgeWorkspaceInFlight = null; });
      return knowledgeWorkspaceInFlight;
    }

    function renderMemoryCandidates(items) {
      state.memoryCandidates = items || [];
      $('memoryCandidateRows').innerHTML = state.memoryCandidates.length
        ? state.memoryCandidates.map((item) => (
          `<article class="memory-candidate-card">
            <div class="knowledge-card-meta">
              <span class="knowledge-kind">${escapeHtml(knowledgeKindLabels[item.kind] || item.kind || '事实')}</span>
              <span class="badge amber">待确认</span>
            </div>
            <p>${escapeHtml(item.content || '')}</p>
            <div class="knowledge-provenance">
              <span>${escapeHtml(item.scope_type === 'qq_group' ? '仅当前 QQ 群' : '仅来源对话')}</span>
              <span>置信度 ${Math.round(Number(item.confidence || 0) * 100)}%</span>
            </div>
            <div class="button-row">
              <button class="primary" type="button" data-memory-candidate-action="accepted" data-memory-candidate-id="${escapeHtml(item.id || '')}">确认记住</button>
              <button class="secondary" type="button" data-memory-candidate-action="rejected" data-memory-candidate-id="${escapeHtml(item.id || '')}">忽略</button>
            </div>
          </article>`
        )).join('')
        : '<p class="empty">暂无待确认的记忆候选。</p>';
      renderAssistantSummary();
      window.AdminMotion?.enterView($('memoryCandidateRows'));
    }

    async function loadMemoryCandidates() {
      const result = await bridge('/assistant/memory-candidates?status=pending&limit=100');
      renderMemoryCandidates(result.result || []);
      return result.result || [];
    }

    async function reviewMemoryCandidate(button) {
      const decision = button.dataset.memoryCandidateAction;
      if (decision === 'accepted' && !window.confirm('确认把这条候选保存到它的原始作用域吗？')) return;
      button.disabled = true;
      try {
        await bridge(`/assistant/memory-candidates/${encodeURIComponent(button.dataset.memoryCandidateId)}/review`, {
          method: 'POST',
          body: JSON.stringify({ status: decision }),
        });
        await Promise.all([loadMemoryCandidates(), decision === 'accepted' ? loadMemories() : Promise.resolve()]);
        setConnection(decision === 'accepted' ? '候选已保存为作用域记忆。' : '候选已忽略。', 'ok');
      } finally {
        button.disabled = false;
      }
    }

    function filteredKnowledgeItems() {
      const query = String($('knowledgeSearchInput')?.value || '').trim().toLocaleLowerCase('zh-CN');
      const status = $('knowledgeStatusFilter')?.value || '';
      const kind = $('knowledgeKindFilter')?.value || '';
      return (state.knowledgeItems || []).filter((item) => {
        const text = [item.title, item.summary, item.content, ...(item.tags || [])].join(' ').toLocaleLowerCase('zh-CN');
        return (!query || text.includes(query)) && (!status || item.status === status) && (!kind || item.kind === kind);
      });
    }

    function knowledgeEvidenceRefs(value) {
      const refs = [];
      const seen = new Set();
      String(value || '').split(/\r?\n/).forEach((item) => {
        const ref = item.trim();
        if (ref && !seen.has(ref)) {
          seen.add(ref);
          refs.push(ref);
        }
      });
      if (refs.length > 16) throw new Error('证据引用最多允许 16 条。');
      if (refs.some((item) => item.length > 300)) throw new Error('单条证据引用不能超过 300 个字符。');
      return refs;
    }

    function renderKnowledgeEvidence(refs = []) {
      if (!Array.isArray(refs) || !refs.length) return '';
      const links = refs.map((raw, index) => {
        const ref = String(raw || '').trim();
        try {
          const parsed = new URL(ref);
          if (parsed.protocol === 'https:' || parsed.protocol === 'http:') {
            return `<a class="text-link" href="${escapeHtml(parsed.href)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(ref)}">公开来源 ${index + 1}</a>`;
          }
        } catch (_) {
          // Non-URL evidence references remain visible text, never href values.
        }
        return `<span title="${escapeHtml(ref)}">证据 ${index + 1} · ${escapeHtml(ref)}</span>`;
      });
      return `<div class="knowledge-evidence" aria-label="知识证据引用">${links.join('')}</div>`;
    }

    function renderKnowledge(items, { replace = true } = {}) {
      if (replace) state.knowledgeItems = items || [];
      const visible = filteredKnowledgeItems();
      $('knowledgeRows').innerHTML = visible.length
        ? visible.map((item) => {
          const statusTone = item.status === 'published' ? 'green' : item.status === 'draft' ? 'amber' : '';
          const freshness = item.effective_freshness || item.freshness_status || 'unverified';
          const freshnessTone = freshness === 'fresh' ? 'green' : freshness === 'expired' ? 'red' : 'amber';
          const source = item.source_type === 'memory'
            ? `来源记忆 · ${item.source_scope_type || '原作用域未知'}`
            : item.source_type === 'admin' ? '管理员维护' : (item.source_type || '来源未知');
          const evidence = Array.isArray(item.evidence_refs) ? item.evidence_refs : [];
          return `<article class="knowledge-card" data-status="${escapeHtml(item.status || '')}" data-kind="${escapeHtml(item.kind || 'fact')}">
            <div class="knowledge-card-meta"><span class="knowledge-kind">${escapeHtml(knowledgeKindLabels[item.kind] || item.kind || '事实')}</span><span><span class="badge ${freshnessTone}">${escapeHtml(knowledgeFreshnessLabels[freshness] || freshness)}</span> <span class="badge ${statusTone}">${escapeHtml(knowledgeStatusLabels[item.status] || item.status || '')}</span></span></div>
            <h3>${escapeHtml(item.title || '')}</h3>
            ${item.summary ? `<p class="knowledge-summary">${escapeHtml(item.summary)}</p>` : ''}
            <p class="knowledge-content">${escapeHtml(item.content || '')}</p>
            ${(item.tags || []).length ? `<div class="knowledge-tags">${item.tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join('')}</div>` : ''}
            ${renderKnowledgeEvidence(evidence)}
            <div class="knowledge-provenance"><span>${escapeHtml(source)}${evidence.length ? ` · ${evidence.length} 条证据` : ''}</span><span>v${Number(item.version || 0)}</span></div>
            <div class="button-row knowledge-actions">
              <button class="secondary" type="button" data-knowledge-action="revisions" data-knowledge-id="${escapeHtml(item.id)}" data-knowledge-title="${escapeHtml(item.title || '')}">版本记录</button>
              ${item.status === 'draft' ? `<button class="secondary" type="button" data-knowledge-action="edit" data-knowledge-id="${escapeHtml(item.id)}">编辑</button><button class="primary" type="button" data-knowledge-action="published" data-knowledge-id="${escapeHtml(item.id)}" data-knowledge-version="${Number(item.version || 0)}">审核发布</button>` : ''}
              ${item.status === 'published' ? `<button class="secondary" type="button" data-knowledge-action="archived" data-knowledge-id="${escapeHtml(item.id)}" data-knowledge-version="${Number(item.version || 0)}">归档</button>` : ''}
            </div>
          </article>`;
        }).join('')
        : '<p class="empty">没有符合当前筛选条件的知识。</p>';
      const published = (state.knowledgeItems || []).filter((item) => item.status === 'published').length;
      const drafts = (state.knowledgeItems || []).filter((item) => item.status === 'draft').length;
      $('knowledgeStatus').textContent = `共 ${state.knowledgeItems.length} 条 · 已发布 ${published} · 待审核 ${drafts} · 当前显示 ${visible.length}`;
      $('knowledgeStatus').className = 'provider-status ok';
      renderAssistantSummary();
      window.AdminMotion?.enterView($('knowledgeRows'));
    }

    async function loadKnowledge() {
      return loadKnowledgeWorkspace({ force: true });
    }

    async function showKnowledgeRevisions(itemId, title) {
      const dialog = $('knowledgeRevisionDialog');
      const rows = $('knowledgeRevisionRows');
      $('knowledgeRevisionTitle').textContent = `知识版本记录 · ${title || itemId}`;
      rows.innerHTML = '<p class="empty">正在载入版本记录。</p>';
      if (typeof dialog.showModal === 'function') dialog.showModal();
      else dialog.setAttribute('open', '');
      try {
        const payload = await bridge(`/assistant/knowledge/revisions?item_id=${encodeURIComponent(itemId)}&limit=50`);
        const revisions = payload.result || [];
        rows.innerHTML = revisions.length ? revisions.map((item) => (
          `<article class="wiki-revision-item">
            <div class="knowledge-card-meta"><strong>v${Number(item.version || 0)}</strong><span class="badge">${escapeHtml(item.operation || '')}</span></div>
            <p>${escapeHtml(item.created_at || '')} · ${escapeHtml(item.created_by || '未知操作者')}</p>
            <p>内容指纹 ${escapeHtml(String(item.content_hash || '').slice(0, 12))} · 来源指纹 ${escapeHtml(String(item.source_hash || '').slice(0, 12))}</p>
          </article>`
        )).join('') : '<p class="empty">暂无版本记录。</p>';
      } catch (error) {
        rows.textContent = error.message || String(error);
      }
    }

    async function createKnowledge() {
      const title = $('knowledgeTitleInput').value.trim();
      const content = $('knowledgeContentInput').value.trim();
      if (!title || !content) {
        setConnection('请输入共享知识标题和内容。', 'error');
        return;
      }
      const button = $('createKnowledgeBtn');
      button.disabled = true;
      try {
        const editing = state.knowledgeEditingId;
        const payload = {
          title,
          content,
          audience: $('knowledgeAudienceInput').value,
          kind: $('knowledgeKindInput').value,
          summary: $('knowledgeSummaryInput').value.trim(),
          tags: $('knowledgeTagsInput').value.split(/[,，]/).map((item) => item.trim()).filter(Boolean),
          evidence_refs: knowledgeEvidenceRefs($('knowledgeEvidenceRefsInput').value),
        };
        if (editing) payload.expected_version = state.knowledgeEditingVersion;
        await bridge(editing ? `/assistant/knowledge/${encodeURIComponent(editing)}/edit` : '/assistant/knowledge', {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        resetKnowledgeEditor();
        await loadKnowledge();
        setConnection(editing ? '知识草稿已更新。' : '知识草稿已保存，发布前仍需明确审核。', 'ok');
      } finally {
        button.disabled = false;
      }
    }

    function resetKnowledgeEditor() {
      state.knowledgeEditingId = '';
      state.knowledgeEditingVersion = 0;
      ['knowledgeTitleInput', 'knowledgeSummaryInput', 'knowledgeTagsInput', 'knowledgeEvidenceRefsInput', 'knowledgeContentInput'].forEach((id) => { $(id).value = ''; });
      $('knowledgeKindInput').value = 'fact';
      $('knowledgeAudienceInput').value = 'all_channels';
      $('createKnowledgeBtn').textContent = '保存为待审核草稿';
      $('cancelKnowledgeEditBtn').classList.add('hidden');
    }

    function editKnowledge(itemId) {
      const item = (state.knowledgeItems || []).find((entry) => entry.id === itemId);
      if (!item || item.status !== 'draft') return;
      state.knowledgeEditingId = item.id;
      state.knowledgeEditingVersion = Number(item.version || 0);
      $('knowledgeTitleInput').value = item.title || '';
      $('knowledgeKindInput').value = item.kind || 'fact';
      $('knowledgeAudienceInput').value = item.audience || 'all_channels';
      $('knowledgeSummaryInput').value = item.summary || '';
      $('knowledgeTagsInput').value = (item.tags || []).join(', ');
      $('knowledgeEvidenceRefsInput').value = (item.evidence_refs || []).join('\n');
      $('knowledgeContentInput').value = item.content || '';
      $('createKnowledgeBtn').textContent = '保存草稿修改';
      $('cancelKnowledgeEditBtn').classList.remove('hidden');
      $('knowledgeTitleInput').focus();
    }

    async function reviewKnowledge(button) {
      const action = button.dataset.knowledgeAction;
      if (!window.confirm(action === 'published' ? '确认发布这条共享知识？发布后会进入助手上下文。' : '确认归档这条共享知识？')) return;
      button.disabled = true;
      try {
        await bridge(`/assistant/knowledge/${encodeURIComponent(button.dataset.knowledgeId)}/review`, {
          method: 'POST',
          body: JSON.stringify({ status: action, expected_version: Number(button.dataset.knowledgeVersion || 0) }),
        });
        await loadKnowledge();
        setConnection(action === 'published' ? '共享知识已审核发布。' : '共享知识已归档。', 'ok');
      } finally {
        button.disabled = false;
      }
    }

    async function promoteMemory(memoryId) {
      const memory = (state.memories || []).find((item) => String(item.id) === String(memoryId));
      if (!memory) return;
      const expandsScope = ['thread', 'qq_group', 'project'].includes(memory.scope_type);
      if (expandsScope && !window.confirm(`这条记忆当前只用于“${memory.scope_label || memory.scope_type}”。确认把它整理为可审核的共享知识草稿？`)) return;
      await bridge(`/assistant/memories/${encodeURIComponent(memoryId)}/promote`, {
        method: 'POST',
        body: JSON.stringify({
          title: `由记忆整理：${String(memory.content || '').slice(0, 28)}`,
          kind: memory.kind === 'preference' ? 'preference' : 'fact',
          audience: memory.scope_type === 'qq_group' ? 'group_all' : 'all_channels',
          confirm_scope_expansion: expandsScope,
        }),
      });
      await loadKnowledge();
      setConnection('已生成知识草稿；审核发布前不会进入共享上下文。', 'ok');
    }

    (() => {
      let knowledgeEventsBound = false;
      window.bindKnowledgeEvents = () => {
        if (knowledgeEventsBound) return;
        knowledgeEventsBound = true;
        $('loadKnowledgeBtn')?.addEventListener('click', () => loadKnowledge().catch((error) => setConnection(error.message || String(error), 'error')));
        $('loadMemoryCandidatesBtn')?.addEventListener('click', () => loadMemoryCandidates().catch((error) => setConnection(error.message || String(error), 'error')));
        $('memoryCandidateRows')?.addEventListener('click', (event) => {
          const button = event.target.closest('button[data-memory-candidate-action]');
          if (!button || button.disabled) return;
          reviewMemoryCandidate(button).catch((error) => setConnection(error.message || String(error), 'error'));
        });
        $('createKnowledgeBtn')?.addEventListener('click', () => createKnowledge().catch((error) => setConnection(error.message || String(error), 'error')));
        $('cancelKnowledgeEditBtn')?.addEventListener('click', resetKnowledgeEditor);
        $('knowledgeSearchInput')?.addEventListener('input', () => renderKnowledge(state.knowledgeItems || [], { replace: false }));
        $('knowledgeStatusFilter')?.addEventListener('change', () => renderKnowledge(state.knowledgeItems || [], { replace: false }));
        $('knowledgeKindFilter')?.addEventListener('change', () => renderKnowledge(state.knowledgeItems || [], { replace: false }));
        $('knowledgeRows')?.addEventListener('click', (event) => {
          const button = event.target.closest('button[data-knowledge-action]');
          if (!button || button.disabled) return;
          if (button.dataset.knowledgeAction === 'edit') editKnowledge(button.dataset.knowledgeId);
          else if (button.dataset.knowledgeAction === 'revisions') showKnowledgeRevisions(button.dataset.knowledgeId, button.dataset.knowledgeTitle);
          else reviewKnowledge(button).catch((error) => setConnection(error.message || String(error), 'error'));
        });
        $('memoryRows')?.addEventListener('click', (event) => {
          const button = event.target.closest('button[data-memory-action="promote"]');
          if (!button || button.disabled) return;
          promoteMemory(button.dataset.memoryId).catch((error) => setConnection(error.message || String(error), 'error'));
        });
      };
    })();
