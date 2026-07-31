    function prepareScrollableRegions() {
      document.querySelectorAll('.table-wrap').forEach((node) => {
        const scrollable = node.classList.contains('group-policy-table')
          || node.scrollWidth > node.clientWidth + 1;
        if (!scrollable) {
          node.removeAttribute('tabindex');
          if (node.dataset.autoRegion === 'true') {
            node.removeAttribute('role');
            node.removeAttribute('aria-label');
            delete node.dataset.autoRegion;
          }
          return;
        }
        node.tabIndex = 0;
        if (!node.hasAttribute('role')) {
          node.setAttribute('role', 'region');
          node.dataset.autoRegion = 'true';
        }
        if (!node.hasAttribute('aria-label')) {
          const heading = node.closest('.panel, section, article')?.querySelector('h2, h3');
          node.setAttribute('aria-label', `${heading?.textContent?.trim() || '数据表格'}，可横向滚动`);
          node.dataset.autoRegion = 'true';
        }
      });
    }

    const collectionBrowserConfigs = [
      {
        id: 'goalRunGrid', label: '任务', pageSize: 4, pageSizes: [4, 8, 12],
        filterLabel: '状态', filterOptions: [
          ['draft', '草稿'], ['active', '推进中'], ['waiting_user', '等待确认'], ['completed', '已完成'],
          ['failed', '失败'], ['cancelled', '已取消'], ['superseded', '已替代'],
        ],
      },
      {
        id: 'automationPlanList', label: '自动化计划', pageSize: 6, pageSizes: [6, 12, 24],
        filterLabel: '类型', filterAttribute: 'collectionType', filterOptions: [['job', '定时任务']],
      },
      { id: 'automationActivityList', label: '自动化活动', pageSize: 8, pageSizes: [8, 16, 30] },
      { id: 'projectRows', label: '项目', pageSize: 10, pageSizes: [10, 20, 50] },
      { id: 'memoryRows', label: '记忆', pageSize: 10, pageSizes: [10, 20, 50] },
      { id: 'groupPolicyRows', label: '群策略', pageSize: 10, pageSizes: [10, 20, 50] },
      { id: 'expressionRows', label: '表达习惯', pageSize: 10, pageSizes: [10, 20, 50] },
      {
        id: 'memeCandidateGrid', label: '表情候选', pageSize: 6, pageSizes: [6, 12, 24],
        filterLabel: '审核状态', filterOptions: [
          ['pending', '待审核'], ['approved', '已批准'], ['rejected', '已拒绝'],
          ['duplicate', '重复'], ['failed', '失败'],
        ],
      },
      { id: 'socialMemeGrid', label: '表情资产', pageSize: 8, pageSizes: [8, 16, 32] },
      { id: 'modelProviderRows', label: 'Provider', pageSize: 8, pageSizes: [8, 16, 32] },
      { id: 'modelCatalogRows', label: '模型', pageSize: 8, pageSizes: [8, 16, 32] },
      { id: 'modelUsageModels', label: '模型用量', pageSize: 8, pageSizes: [8, 16, 32] },
      { id: 'modelUsageEvents', label: '调用记录', pageSize: 10, pageSizes: [10, 20, 50] },
      { id: 'capabilityManifestGrid', label: 'Capability', pageSize: 8, pageSizes: [8, 16, 32] },
      { id: 'capabilitySkillRows', label: 'Skill', pageSize: 8, pageSizes: [8, 16, 32] },
      { id: 'capabilityPluginRows', label: '插件', pageSize: 8, pageSizes: [8, 16, 32] },
    ];

    function collectionItems(target) {
      return [...target.children].filter((item) => {
        if (item.matches('.empty, .empty-state')) return false;
        return !item.querySelector(':scope > .empty, :scope > .empty-state');
      });
    }

    function normalizeCollectionText(value) {
      return String(value || '').normalize('NFKC').toLocaleLowerCase('zh-CN').trim();
    }

    function initializeCollectionBrowsers() {
      collectionBrowserConfigs.forEach((config) => {
        const target = $(config.id);
        if (!target || state.collectionBrowsers.has(config.id)) return;

        const browserState = {
          page: 1,
          pageSize: config.pageSize,
          query: '',
          filter: '',
          applying: false,
        };
        const toolbar = document.createElement('div');
        toolbar.className = 'collection-browser';
        toolbar.dataset.collectionOwner = config.id;
        toolbar.setAttribute('role', 'search');
        toolbar.setAttribute('aria-label', `${config.label}浏览工具`);
        toolbar.innerHTML = `<label class="collection-search"><span>搜索</span><input type="search" autocomplete="off" placeholder="搜索 ${escapeHtml(config.label)}" aria-label="搜索${escapeHtml(config.label)}"></label>
          ${config.filterOptions ? `<label class="collection-filter"><span>${escapeHtml(config.filterLabel || '筛选')}</span><select aria-label="${escapeHtml(config.label)}${escapeHtml(config.filterLabel || '筛选')}"><option value="">全部</option>${config.filterOptions.map(([value, label]) => `<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`).join('')}</select></label>` : ''}
          <label class="collection-page-size"><span>每页</span><select aria-label="${escapeHtml(config.label)}每页数量">${config.pageSizes.map((size) => `<option value="${size}" ${size === config.pageSize ? 'selected' : ''}>${size}</option>`).join('')}</select></label>
          <div class="collection-pagination"><span class="collection-count"></span><button class="secondary" type="button" data-collection-page="previous">上一页</button><span class="collection-page-indicator"></span><button class="secondary" type="button" data-collection-page="next">下一页</button></div>
          <p class="collection-no-results" hidden>没有匹配的数据，请调整搜索或筛选条件。</p>
          <span class="visually-hidden collection-announcement" role="status" aria-live="polite" aria-atomic="true"></span>`;

        const host = target.tagName === 'TBODY' ? target.closest('.table-wrap') : target;
        host.parentNode.insertBefore(toolbar, host);
        target.classList.add('collection-managed');

        const search = toolbar.querySelector('input[type="search"]');
        const filter = toolbar.querySelector('.collection-filter select');
        const pageSize = toolbar.querySelector('.collection-page-size select');
        const previous = toolbar.querySelector('[data-collection-page="previous"]');
        const next = toolbar.querySelector('[data-collection-page="next"]');
        const count = toolbar.querySelector('.collection-count');
        const indicator = toolbar.querySelector('.collection-page-indicator');
        const noResults = toolbar.querySelector('.collection-no-results');
        const announcement = toolbar.querySelector('.collection-announcement');

        const apply = ({ announce = false } = {}) => {
          if (browserState.applying) return;
          browserState.applying = true;
          const items = collectionItems(target);
          const attribute = config.filterAttribute || 'collectionStatus';
          const matches = items.filter((item) => {
            const textMatches = !browserState.query || normalizeCollectionText(item.textContent).includes(browserState.query);
            const filterMatches = !browserState.filter || String(item.dataset[attribute] || '') === browserState.filter;
            return textMatches && filterMatches;
          });
          const pages = Math.max(1, Math.ceil(matches.length / browserState.pageSize));
          browserState.page = Math.min(browserState.page, pages);
          const start = (browserState.page - 1) * browserState.pageSize;
          const visible = new Set(matches.slice(start, start + browserState.pageSize));
          items.forEach((item) => { item.hidden = !visible.has(item); });
          const hasCriteria = Boolean(browserState.query || browserState.filter);
          count.textContent = hasCriteria ? `${matches.length} / ${items.length} 条` : `共 ${items.length} 条`;
          indicator.textContent = `${browserState.page} / ${pages} 页`;
          previous.disabled = browserState.page <= 1 || matches.length === 0;
          next.disabled = browserState.page >= pages || matches.length === 0;
          noResults.hidden = !(items.length > 0 && matches.length === 0);
          if (announce) {
            announcement.textContent = matches.length
              ? `${config.label}显示第 ${browserState.page} 页，共 ${matches.length} 条匹配数据。`
              : `${config.label}没有匹配数据。`;
          }
          browserState.applying = false;
        };

        search.addEventListener('input', () => {
          browserState.query = normalizeCollectionText(search.value);
          browserState.page = 1;
          apply({ announce: true });
        });
        filter?.addEventListener('change', () => {
          browserState.filter = filter.value;
          browserState.page = 1;
          apply({ announce: true });
        });
        pageSize.addEventListener('change', () => {
          browserState.pageSize = Number(pageSize.value) || config.pageSize;
          browserState.page = 1;
          apply({ announce: true });
        });
        previous.addEventListener('click', () => {
          browserState.page = Math.max(1, browserState.page - 1);
          apply({ announce: true });
        });
        next.addEventListener('click', () => {
          browserState.page += 1;
          apply({ announce: true });
        });

        // Background SWR refreshes replace collection children. Reapply the active
        // view without resetting search, page size, keyboard focus, or announcing
        // every refresh. WCAG 2.2 - 2.4.3 Focus Order, 4.1.3 Status Messages.
        const observer = new MutationObserver(() => apply());
        observer.observe(target, { childList: true });
        browserState.apply = apply;
        browserState.observer = observer;
        state.collectionBrowsers.set(config.id, browserState);
        apply();
      });
    }

    function organizeProductSurfaces() {
      const brainView = $('view-brain');
      const personaPanel = $('assistantDisplayName')?.closest('section.panel');
      if (brainView && personaPanel) {
        personaPanel.classList.add('persona-panel');
        const firstPanel = brainView.querySelector('section.panel');
        brainView.insertBefore(personaPanel, firstPanel);
      }

      const socialView = $('view-social');
      const expressionPanel = $('memeAssetList')?.closest('section.panel');
      if (socialView && expressionPanel) {
        // 新社交页已有统一的表情资产区。保留旧节点只为兼容历史渲染函数，
        // 不再向用户展示第二套重复管理界面。
        expressionPanel.hidden = true;
        expressionPanel.setAttribute('aria-hidden', 'true');
      }

      const channelPolicyPanel = $('assistantModelRouteSummary')?.closest('section.panel');
      const masterControls = $('socialMasterControls');
      const masterActions = $('socialMasterActions');
      if (channelPolicyPanel && masterControls && masterActions) {
        channelPolicyPanel.querySelectorAll('.policy-switches .checkbox-line').forEach((label) => {
          if (label.querySelector('#proactiveEnabled')) return;
          masterControls.appendChild(label);
        });
        const saveButton = $('saveAssistantProviderBtn');
        if (saveButton) {
          saveButton.textContent = '保存渠道表达开关';
          masterActions.appendChild(saveButton);
        }
        channelPolicyPanel.hidden = true;
        channelPolicyPanel.setAttribute('aria-hidden', 'true');
      }

      document.querySelectorAll('.assistant-grid').forEach((grid) => {
        if (grid.children.length === 1) {
          grid.classList.add('single-column');
        }
      });
    }

    function initializeProgressiveDisclosure() {
      // 身份与表达是一个有先后关系的版本化工作区，不能被拆成互斥手风琴。
      const progressiveViews = ['social', 'services', 'qq', 'logs', 'settings'];
      progressiveViews.forEach((viewName) => {
        const view = $(`view-${viewName}`);
        if (!view) return;
        view.classList.add('progressive-view');
        const candidates = [];
        [...view.children].forEach((child) => {
          if (child.classList.contains('panel')) {
            candidates.push(child);
            return;
          }
          if (child.matches('.assistant-grid, .split-grid, .overview-grid, .task-grid')) {
            [...child.children].forEach((nested) => {
              if (nested.classList.contains('panel')) candidates.push(nested);
            });
          }
        });
        candidates.forEach((panel, index) => {
          if (panel.dataset.progressive === 'true') return;
          const header = panel.querySelector(':scope > .panel-header');
          const heading = header?.querySelector('h2')?.textContent?.trim() || `配置分区 ${index + 1}`;
          const meta = header?.querySelector('.meta')?.textContent?.trim() || '按需展开';
          const actions = header ? header.querySelectorAll('button, input, select').length : 0;
          const details = document.createElement('details');
          details.className = 'progressive-panel';
          details.open = index === 0;
          const summary = document.createElement('summary');
          const title = document.createElement('strong');
          const hint = document.createElement('span');
          title.textContent = heading;
          hint.textContent = meta;
          summary.append(title, hint);
          panel.replaceWith(details);
          details.append(summary, panel);
          details.addEventListener('toggle', () => {
            if (!details.open) return;
            view.querySelectorAll(':scope > .progressive-panel[open]').forEach((other) => {
              if (other !== details) other.open = false;
            });
          });
          panel.dataset.progressive = 'true';
          if (header) {
            header.querySelector('h2')?.setAttribute('aria-hidden', 'true');
            if (!actions) header.hidden = true;
            else header.classList.add('progressive-actions');
          }
        });
      });
    }

