    async function loadStatus() {
      const [server, codex] = await Promise.all([
        // Routine navigation only needs host metrics. Runtime, CodeGraph,
        // services and containers are rendered by their dedicated requests
        // below, so repeating those probes here only delays the page.
        bridge('/server/status?depth=quick'),
        bridge('/status'),
      ]);
      $('serverStatus').textContent = [
        server.output || server.error || '(空)',
        '',
        'Codex 登录状态:',
        codex.output || codex.error || '(空)',
      ].join('\n');
      $('statusStamp').textContent = new Date().toLocaleString();
      renderOverviewCodex(codex);
    }

    async function loadCodegraphStatus() {
      const result = await bridge('/codegraph/status');
      renderCodegraphStatus(result);
    }

    async function loadStats() {
      const result = await bridge('/tasks/stats');
      renderMetrics(result);
    }

    async function loadServicesAndContainers() {
      const serviceJob = bridge('/services').then((result) => {
        renderServices(result.services || []);
        return result;
      });
      const containerJob = bridge('/docker/containers').then((result) => {
        renderContainers(result.containers || []);
        return result;
      });
      const [services, containers] = await Promise.all([serviceJob, containerJob]);
      const serviceItems = services.services || [];
      const containerItems = containers.containers || [];
      renderHealthSummary(serviceItems, containerItems);
    }

    async function loadServicesView() {
      // Deep diagnostics enrich the page but must not hold the service list
      // hostage. They finish independently and keep their own visible status.
      Promise.allSettled([
        loadStatus(),
        loadCodegraphStatus(),
        window.loadBusinessHealth(),
      ]).then((results) => {
        const failures = results.filter((result) => result.status === 'rejected');
        if (failures.length) console.warn('部分深度诊断暂时不可用。', failures.map((item) => item.reason));
      });
      await loadServicesAndContainers();
    }

    async function loadTasks() {
      const status = $('statusFilter').value;
      const limit = Math.max(1, Math.min(Number($('limitInput').value || 10), 50));
      const query = new URLSearchParams({ limit: String(limit) });
      if (status) {
        query.set('status', status);
      }
      const result = await bridge('/tasks?' + query.toString());
      renderTasks(result.tasks || []);
    }

    async function loadTask(id) {
      const result = await bridge('/tasks/' + encodeURIComponent(id));
      const task = result.task || {};
      state.lastTaskId = task.id || id;
      $('detailTitle').textContent = task.id ? `#${task.id}` : id;
      $('taskDetail').textContent = formatTask(task);
      switchView('tasks', { load: false });
    }

    async function loadQqDiagnostics() {
      if (!state.authenticated) {
        return;
      }
      if (state.qqDiagnosticsPromise) return state.qqDiagnosticsPromise;
      state.qqDiagnosticsPromise = (async () => {
        try {
          const result = await bridge('/qq/diagnostics');
          renderQqDiagnostics(result);
          setConnection('QQ 链路诊断已更新。', 'ok');
          return result;
        } catch (error) {
          setConnection(error.message || String(error), 'error');
          return null;
        } finally {
          state.qqDiagnosticsPromise = null;
        }
      })();
      return state.qqDiagnosticsPromise;
    }

    function startQqPolling() {
      stopQqPolling();
      state.qqPollTimer = window.setInterval(() => {
        if (state.authenticated && state.activeView === 'qq') {
          loadQqDiagnostics();
        }
      }, 30000);
    }

    function stopQqPolling() {
      if (state.qqPollTimer) {
        window.clearInterval(state.qqPollTimer);
        state.qqPollTimer = null;
      }
    }

    async function refreshQrcode() {
      if (!state.authenticated) {
        return;
      }
      const diagnostics = state.qqDiagnostics || {};
      if (diagnostics.qrcode_supported === false) {
        setConnection(diagnostics.login_management_hint || '请通过安全 SSH 隧道访问 LLBot WebUI 完成扫码登录。', 'error');
        return;
      }
      if (!window.confirm('此操作会重启当前 QQ Adapter 并中断现有连接。仅在登录失效时继续。')) {
        return;
      }
      try {
        $('refreshQrcodeBtn').disabled = true;
        setConnection('正在重启 QQ Adapter 并等待二维码生成。', 'ok');
        const result = await bridge('/qq/qrcode/refresh', {
          method: 'POST',
          body: JSON.stringify({ wait_seconds: 25, confirm_restart: true }),
        });
        renderQqDiagnostics(result.diagnostics || result);
        const failureMessage = result.error === 'qrcode_not_refreshed'
          ? 'QQ Adapter 未生成新二维码，旧二维码已隐藏；请稍后重试。'
          : '二维码暂未生成，请稍后再刷新。';
        setConnection(result.ok ? '二维码已刷新，请扫码登录。' : failureMessage, result.ok ? 'ok' : 'error');
      } catch (error) {
        setConnection(error.message || String(error), 'error');
        await loadQqDiagnostics();
      }
    }

    async function loadLogs() {
      if (!state.authenticated) {
        return;
      }
      try {
        const target = $('logTarget').value || 'bridge';
        const lines = Math.max(20, Math.min(Number($('logLines').value || 120), 300));
        const query = new URLSearchParams({ target, lines: String(lines) });
        const result = await bridge('/logs?' + query.toString());
        $('logOutput').textContent = result.output || result.error || '(空)';
        setConnection(result.ok ? '日志已更新。' : '日志读取失败。', result.ok ? 'ok' : 'error');
      } catch (error) {
        setConnection(error.message || String(error), 'error');
      }
    }

    async function refreshActiveView({ force = true, background = false } = {}) {
      if (!state.authenticated) {
        setConnection('未登录。请输入访问凭证。');
        return;
      }
      const view = state.activeView;
      await ensureViewFeatures(view);
      const existingRefresh = state.viewRefreshPromises.get(view);
      if (existingRefresh) {
        if (!background && state.activeView === view) {
          state.refreshInFlight = true;
          $('refreshBtn').disabled = true;
          $('appShell').setAttribute('aria-busy', 'true');
          setConnection(`正在完成${viewTitles[view] || '当前页面'}的数据同步……`, 'ok');
        }
        try {
          await existingRefresh;
          if (state.activeView === view) {
            setConnection(`${viewTitles[view] || '当前页面'}数据已同步 · ${new Date().toLocaleTimeString()}`, 'ok');
          }
        } catch (error) {
          if (state.activeView === view) setConnection(error.message || String(error), 'error');
        } finally {
          if (state.activeView === view && !background) {
            state.refreshInFlight = false;
            $('refreshBtn').disabled = false;
            $('appShell').removeAttribute('aria-busy');
          }
        }
        return false;
      }
      if (!force && isViewFresh(view)) {
        setConnection(`${viewTitles[view] || '当前页面'}已立即显示 · 数据为最新`, 'ok');
        return false;
      }
      if (state.viewRefreshTimer) {
        window.clearTimeout(state.viewRefreshTimer);
        state.viewRefreshTimer = null;
      }
      let refreshPromise;
      try {
        state.refreshInFlight = true;
        if (!background) {
          $('refreshBtn').disabled = true;
          $('appShell').setAttribute('aria-busy', 'true');
          setConnection(`正在刷新${viewTitles[view] || '当前页面'}……`, 'ok');
        } else {
          setConnection(`${viewTitles[view] || '当前页面'}已立即显示 · 后台检查更新`, 'ok');
        }
        let loaders = [];
        if (view === 'overview') loaders = [loadAssistantHome];
        else if (view === 'tasks') loaders = [loadStats, loadTasks, loadExecutionOverview, loadFormalApprovals];
        else if (view === 'artifacts') loaders = [window.loadArtifactCenter];
        else if (view === 'automations') loaders = [loadAutomationView];
        else if (view === 'projects') loaders = [loadProjectsPanel];
        else if (view === 'assistant') loaders = [() => loadAssistantPanel({ force })];
        else if (view === 'brain') loaders = [() => loadBrainPanel({ force })];
      if(view==='growth')loaders=[loadLearningPanel];
        else if (view === 'relationship') loaders = [window.loadRelationshipManagement];
        else if (view === 'social') loaders = [loadSocialExperience];
        else if (view === 'models') loaders = [loadModelRegistry];
        else if (view === 'capabilities') loaders = [loadCapabilities];
        else if (view === 'proxy') loaders = [loadProxyGroups];
        else if (view === 'services') loaders = [loadServicesView];
        else if (view === 'qq') loaders = [loadQqDiagnostics, window.loadQqAccessSettings];
        else if (view === 'logs') loaders = [loadLogs];
        else if (view === 'settings') loaders = [loadAppearance, loadFixedTokenStatus, loadPetState];
        const jobs = loaders.map((load) => Promise.resolve().then(() => load()));
        refreshPromise = Promise.allSettled(jobs).then((results) => {
          const failures = results.filter((result) => result.status === 'rejected');
          if (failures.length === results.length && failures.length) {
            throw failures[0].reason;
          }
          state.viewLoadedAt[view] = Date.now();
          prepareScrollableRegions();
          if (failures.length) {
            console.warn(`${view} 有 ${failures.length} 个非关键数据源暂时不可用。`, failures.map((item) => item.reason));
          }
        });
        state.viewRefreshPromises.set(view, refreshPromise);
        await refreshPromise;
        if (state.activeView === view) {
          setConnection(`${viewTitles[view] || '当前页面'}数据已同步 · ${new Date().toLocaleTimeString()}`, 'ok');
        }
      } catch (error) {
        if (state.activeView === view) {
          setConnection(error.message || String(error), 'error');
        }
      } finally {
        if (state.viewRefreshPromises.get(view) === refreshPromise) {
          state.viewRefreshPromises.delete(view);
        }
        if (state.activeView === view) {
          state.refreshInFlight = false;
          if (!background) {
            $('refreshBtn').disabled = false;
            $('appShell').removeAttribute('aria-busy');
          }
        }
      }
      return true;
    }

    async function ensureCodegraph() {
      try {
        $('codegraphEnsureBtn').disabled = true;
        const result = await bridge('/codegraph/ensure', {
          method: 'POST',
          body: JSON.stringify({}),
        });
        renderCodegraphStatus(result.codegraph || result);
        setConnection(result.ok ? '代码图谱已同步。' : '代码图谱同步失败。', result.ok ? 'ok' : 'error');
      } catch (error) {
        setConnection(error.message || String(error), 'error');
      } finally {
        $('codegraphEnsureBtn').disabled = false;
      }
    }

    async function createTask() {
      const prompt = $('promptInput').value.trim();
      if (!prompt) {
        setConnection('请输入任务内容。', 'error');
        return;
      }
      const sandbox = $('sandboxInput').value;
      const networkMode = $('taskNetworkMode').value;
      const timeout = Math.max(30, Math.min(Number($('timeoutInput').value || 180), 900));
      try {
        const result = await bridge('/tasks', {
          method: 'POST',
          body: JSON.stringify({ prompt, sandbox, timeout, network_mode: networkMode }),
        });
        const task = result.task || {};
        $('promptInput').value = '';
        setConnection(`任务 #${task.id || '?'} 已创建。`, 'ok');
        await Promise.all([loadStats(), loadTasks(), loadExecutionOverview()]);
        if (task.id) {
          await loadTask(task.id);
        }
      } catch (error) {
        setConnection(error.message || String(error), 'error');
      }
    }

    async function taskAction(action, id) {
      try {
        if (action === 'detail') {
          await loadTask(id);
        } else if (action === 'cancel') {
          await bridge('/tasks/' + encodeURIComponent(id) + '/cancel', { method: 'POST' });
          await Promise.all([loadStats(), loadTasks(), loadTask(id)]);
        } else if (action === 'retry') {
          const result = await bridge('/tasks/' + encodeURIComponent(id) + '/retry', { method: 'POST' });
          const task = result.task || {};
          await Promise.all([loadStats(), loadTasks()]);
          if (task.id) {
            await loadTask(task.id);
          }
        }
        setConnection('操作完成。', 'ok');
      } catch (error) {
        setConnection(error.message || String(error), 'error');
      }
    }

    async function login() {
      const token = $('tokenInput').value.trim();
      if (!token) {
        setConnection('请输入访问凭证。', 'error');
        return;
      }
      state.authEpoch += 1;
      try {
        const response = await fetch('/admin/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json; charset=utf-8' },
          credentials: 'same-origin',
          body: JSON.stringify({ token, build: ADMIN_BUILD }),
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) {
          throw new Error(errorMessages[payload.error] || payload.error || '登录失败。');
        }
        $('tokenInput').value = '';
        showAuthenticated(true);
        renderAppearance(payload.appearance || {}, '外观设置已加载。', 'ok');
        switchView('overview', { load: false });
        await refreshActiveView({ force: true, background: true });
        prepareScrollableRegions();
        scheduleActiveViewRefresh('overview');
        schedulePetRuntime();
        startVersionChecks();
        setConnection(`日常空间已更新 · ${new Date().toLocaleTimeString()}`, 'ok');
      } catch (error) {
        showAuthenticated(false);
        setConnection(error.message || String(error), 'error');
      }
    }

    async function logout() {
      state.authEpoch += 1;
      try {
        await fetch('/admin/logout', {
          method: 'POST',
          credentials: 'same-origin',
        });
      } catch (error) {
        // 退出登录时仍然清理本地页面状态。
      }
      state.lastTaskId = '';
      stopActiveViewRefresh();
      if (state.versionCheckTimer) window.clearInterval(state.versionCheckTimer);
      if (state.versionCheckStartTimer) window.clearTimeout(state.versionCheckStartTimer);
      $('tokenInput').value = '';
      $('taskDetail').textContent = '(空)';
      $('detailTitle').textContent = '未选择任务';
      showAuthenticated(false);
      setConnection('已退出登录。');
    }

    organizeProductSurfaces();
    initializeProgressiveDisclosure();
    initializeCollectionBrowsers();
    initializeAccessibility();

    document.querySelectorAll('.nav button').forEach((button) => {
      button.addEventListener('click', () => {
        switchView(button.dataset.view || button.dataset.defaultView, { focusHeading: true });
      });
    });
    document.querySelectorAll('[data-jump]').forEach((button) => {
      button.addEventListener('click', () => switchView(button.dataset.jump, { focusHeading: true }));
    });
    $('view-models').addEventListener('click', async (event) => {
      const button = event.target.closest('[data-model-workspace]');
      if (!button || typeof window.setModelWorkspace === 'function') return;
      event.preventDefault();
      event.stopImmediatePropagation();
      const target = button.dataset.modelWorkspace;
      button.setAttribute('aria-busy', 'true');
      setConnection('正在载入模型管理功能……', 'ok');
      try {
        await ensureViewFeatures('models');
        window.setModelWorkspace?.(target);
      } catch (error) {
        setConnection(error.message || String(error), 'error');
      } finally {
        button.removeAttribute('aria-busy');
      }
    }, true);
    $('loginBtn').addEventListener('click', login);
    $('logoutBtn').addEventListener('click', logout);
    $('refreshBtn').addEventListener('click', () => refreshActiveView({ force: true, background: false }));
    $('reloadConsoleBtn').addEventListener('click', () => window.location.reload());
    $('quickTaskBtn').addEventListener('click', () => {
      switchView('tasks', { focusHeading: true });
      requestAnimationFrame(() => $('promptInput').focus());
    });
    $('newGoalBtn').addEventListener('click', () => {
      $('promptInput').scrollIntoView({ behavior: 'smooth', block: 'center' });
      window.setTimeout(() => $('promptInput').focus(), 180);
    });
    $('systemAuditBtn').addEventListener('click', async () => {
      try {
        await loadSystemAudit();
        setConnection('系统体检已完成。', 'ok');
      } catch (error) {
        setConnection(error.message || String(error), 'error');
      }
    });
    $('codegraphEnsureBtn').addEventListener('click', ensureCodegraph);
    $('createTaskBtn').addEventListener('click', createTask);
    $('refreshQrcodeBtn').addEventListener('click', refreshQrcode);
    $('proxyReloadBtn').addEventListener('click', () => loadProxyGroups());
    $('proxyDelayBtn').addEventListener('click', () => runProxyDelay());
    $('proxyAiCheckBtn').addEventListener('click', () => runProxyDiagnostics(false));
    $('proxyAiSwitchBtn').addEventListener('click', () => runProxyDiagnostics(true));
    $('proxyModeSaveBtn').addEventListener('click', () => saveProxyMode());
    $('proxyIpBtn').addEventListener('click', () => checkProxyIp());
    $('proxySubNewBtn').addEventListener('click', () => editProxySubscription());
    $('proxySubCancelBtn').addEventListener('click', () => closeProxySubscriptionEditor());
    $('proxySubscriptionEditor').addEventListener('submit', (event) => {
      event.preventDefault();
      saveProxySubscription();
    });
    $('systemFrameworkBtn')?.addEventListener('click', async () => {
      try {
        await loadSystemFramework();
        setConnection('系统架构说明已读取。', 'ok');
      } catch (error) {
        setConnection(error.message || String(error), 'error');
      }
    });
    $('proxySortSelect').addEventListener('change', () => {
      state.proxySort = $('proxySortSelect').value || 'default';
      renderProxyCards();
    });
    $('proxySearchInput').addEventListener('input', () => renderProxyCards());
    $('proxyCards').addEventListener('click', (event) => {
      const card = event.target.closest('button[data-proxy-node]');
      if (!card || card.disabled) {
        return;
      }
      selectProxyNode(card.dataset.proxyNode);
    });
    $('proxyAlpha').addEventListener('click', (event) => {
      const button = event.target.closest('button[data-proxy-filter]');
      if (!button) {
        return;
      }
      $('proxySearchInput').value = button.dataset.proxyFilter || '';
      renderProxyCards();
    });
    $('reloadSocialBtn').addEventListener('click', async () => {
      try {
        await loadSocialPanel();
        setConnection('表情包已刷新。', 'ok');
      } catch (error) {
        setConnection(error.message || String(error), 'error');
      }
    });
    $('memeAssetList').addEventListener('click', async (event) => {
      const button = event.target.closest('[data-meme-toggle]');
      if (!button) {
        return;
      }
      try {
        await toggleMemeAsset(button.dataset.memeToggle);
        setConnection('表情包状态已更新。', 'ok');
      } catch (error) {
        setConnection(error.message || String(error), 'error');
      }
    });
    $('reloadAutomationBtn').addEventListener('click', () => loadAutomationView().catch((error) => setConnection(error.message || String(error), 'error')));
    $('automationScheduleType').addEventListener('change', () => updateAutomationScheduleFields());
    $('automationJobForm').addEventListener('submit', (event) => saveAutomationJob(event).catch((error) => setConnection(error.message || String(error), 'error')));
    $('automationPlanList').addEventListener('click', (event) => {
      const jobButton = event.target.closest('[data-automation-job-toggle]');
      if (jobButton) {
        toggleAutomationJob(jobButton.dataset.automationJobToggle).catch((error) => setConnection(error.message || String(error), 'error'));
      }
    });
    $('saveAgentPolicyBtn').addEventListener('click', () => saveAgentPolicy());
    $('reloadSocialExperienceBtn').addEventListener('click', () => loadSocialExperience());
    $('saveGroupPolicyBtn').addEventListener('click', () => saveGroupPolicy().catch((error) => setConnection(error.message || String(error), 'error')));
    $('clearGroupPolicyBtn').addEventListener('click', () => clearGroupPolicyForm());
    $('groupNaturalGuardFields').addEventListener('click', (event) => {
      const button = event.target.closest('[data-group-natural-cutover]');
      if (button) setNaturalGroupParticipation().catch((error) => setConnection(error.message || String(error), 'error'));
    });
    $('groupPolicyId').addEventListener('input', () => renderGroupPolicyAccessStatus());
    $('openQqAccessFromGroupBtn').addEventListener('click', () => switchView('qq', { focusHeading: true }));
    window.addEventListener('qq-access-updated', (event) => {
      state.qqGroupAccess = event.detail || null;
      state.viewLoadedAt.social = 0;
      if (state.activeView === 'social') {
        renderSocialExperience();
        renderGroupPolicyAccessStatus();
      }
    });
    $('saveExpressionBtn').addEventListener('click', () => saveExpression().catch((error) => setConnection(error.message || String(error), 'error')));
    $('uploadMemeBtn').addEventListener('click', () => uploadMeme().catch((error) => {
      $('memeUploadStatus').textContent = error.message || String(error);
      setConnection(error.message || String(error), 'error');
    }));
    $('groupPolicyRows').addEventListener('click', (event) => {
      const button = event.target.closest('[data-group-edit]');
      if (button) editGroupPolicy(button.dataset.groupEdit);
    });
    $('expressionRows').addEventListener('click', (event) => {
      const edit = event.target.closest('[data-expression-edit]');
      const toggle = event.target.closest('[data-expression-toggle]');
      if (edit) editExpression(edit.dataset.expressionEdit);
      if (toggle) toggleExpression(toggle.dataset.expressionToggle).catch((error) => setConnection(error.message || String(error), 'error'));
    });
    $('socialMemeGrid').addEventListener('click', (event) => {
      const button = event.target.closest('[data-social-meme-toggle]');
      if (button && !button.disabled) toggleSocialMeme(button.dataset.socialMemeToggle).catch((error) => setConnection(error.message || String(error), 'error'));
    });
    document.querySelectorAll('[data-social-workspace]').forEach((button) => {
      button.addEventListener('click', () => setSocialWorkspace(button.dataset.socialWorkspace));
    });
    $('memeDiscoveryForm').addEventListener('submit', (event) => searchMemeCandidates(event).catch((error) => {
      $('memeDiscoveryStatus').textContent = error.message || String(error);
      $('memeCandidateGrid').setAttribute('aria-busy', 'false');
      $('memeDiscoverySubmitBtn').disabled = false;
      $('memeDiscoverySubmitBtn').textContent = '查找候选';
      setConnection(error.message || String(error), 'error');
    }));
    $('memeCandidateGrid').addEventListener('click', (event) => {
      const button = event.target.closest('[data-candidate-review]');
      if (!button || button.disabled) return;
      reviewMemeCandidate(button.dataset.candidateId, button.dataset.candidateReview, button)
        .catch((error) => {
          $('memeDiscoveryStatus').textContent = error.message || String(error);
          setConnection(error.message || String(error), 'error');
        });
    });
    $('reloadCapabilitiesBtn').addEventListener('click', () => loadCapabilities({ forceMarketplace: true, waitForMarketplace: true }));
    document.querySelectorAll('[data-capability-workspace]').forEach((button) => {
      button.addEventListener('click', () => setCapabilityWorkspace(button.dataset.capabilityWorkspace));
      button.addEventListener('keydown', (event) => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const tabs = [...document.querySelectorAll('[data-capability-workspace]')];
        const current = tabs.indexOf(button);
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (current + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
        tabs[next].focus();
        setCapabilityWorkspace(tabs[next].dataset.capabilityWorkspace);
      });
    });
    $('refreshPluginMarketBtn').addEventListener('click', async () => {
      const button = $('refreshPluginMarketBtn'); button.disabled = true; button.textContent = '同步中…';
      try { await loadPluginMarketplace({ force: true }); setConnection('插件市场目录已同步。', 'ok'); }
      catch (error) { setConnection(error.message || String(error), 'error'); }
      finally { button.disabled = false; button.textContent = '同步市场'; }
    });
    $('pluginMarketGrid').addEventListener('click', (event) => {
      const button = event.target.closest('[data-market-detail]');
      if (button) renderPluginMarketDetail(button.dataset.marketDetail);
    });
    $('pluginMarketSearch').addEventListener('input', () => {
      state.pluginMarketQuery = $('pluginMarketSearch').value;
      state.pluginMarketPage = 1;
      renderPluginMarketplace();
      setText('pluginMarketAnnouncement', `找到 ${$('pluginMarketCount').textContent}，当前第 1 页。`);
    });
    $('pluginMarketCategory').addEventListener('change', () => {
      state.pluginMarketCategory = $('pluginMarketCategory').value;
      state.pluginMarketPage = 1;
      renderPluginMarketplace();
      setText('pluginMarketAnnouncement', `筛选已更新，${$('pluginMarketCount').textContent}。`);
    });
    $('pluginMarketPageSize').addEventListener('change', () => {
      state.pluginMarketPageSize = Number($('pluginMarketPageSize').value) || 9;
      state.pluginMarketPage = 1;
      renderPluginMarketplace();
    });
    $('pluginMarketPreviousBtn').addEventListener('click', () => {
      state.pluginMarketPage = Math.max(1, state.pluginMarketPage - 1);
      renderPluginMarketplace();
      setText('pluginMarketAnnouncement', `当前第 ${state.pluginMarketPage} 页。`);
    });
    $('pluginMarketNextBtn').addEventListener('click', () => {
      state.pluginMarketPage += 1;
      renderPluginMarketplace();
      setText('pluginMarketAnnouncement', `当前第 ${state.pluginMarketPage} 页。`);
    });
    $('pluginMarketDetailBody').addEventListener('click', (event) => {
      const button = event.target.closest('[data-market-action]');
      if (button && !button.disabled) operateMarketPlugin(button.dataset.marketAction, button.dataset.marketPlugin, button).catch((error) => setConnection(error.message || String(error), 'error'));
    });
    $('closePluginMarketDetailBtn').addEventListener('click', () => {
      state.selectedMarketPlugin = '';
      $('pluginMarketDetail').classList.add('hidden');
      document.querySelector('[data-market-detail]')?.focus();
    });
    $('skillNewBtn').addEventListener('click', () => editNewSkill());
    $('cancelSkillBtn').addEventListener('click', () => { $('skillEditor').open = false; });
    $('reloadPluginsBtn').addEventListener('click', () => reloadPlugins().catch((error) => setConnection(error.message || String(error), 'error')));
    $('saveSkillBtn').addEventListener('click', () => saveSkill().catch((error) => setConnection(error.message || String(error), 'error')));
    $('capabilityPluginRows').addEventListener('click', (event) => {
      const button = event.target.closest('[data-plugin-toggle]');
      if (button && !button.disabled) togglePlugin(button.dataset.pluginToggle).catch((error) => setConnection(error.message || String(error), 'error'));
    });
    $('capabilitySkillRows').addEventListener('click', (event) => {
      const edit = event.target.closest('[data-skill-edit]');
      const toggle = event.target.closest('[data-skill-toggle]');
      if (edit) editSkill(edit.dataset.skillEdit);
      if (toggle) toggleSkill(toggle.dataset.skillToggle).catch((error) => setConnection(error.message || String(error), 'error'));
    });
    $('loadQualityBtn').addEventListener('click', async () => {
      try {
        await Promise.all([loadQualityEvents(), loadModeSessions()]);
        setConnection('质量观察与模式会话已更新。', 'ok');
      } catch (error) {
        setConnection(error.message || String(error), 'error');
      }
    });
    $('saveAppearanceBtn').addEventListener('click', saveAppearance);
    $('saveFixedTokenBtn').addEventListener('click', saveFixedToken);
    $('previewAppearanceBtn').addEventListener('click', previewAppearance);
    $('resetAppearanceBtn').addEventListener('click', resetAppearance);
    $('useSampleBackgroundBtn').addEventListener('click', useSampleBackground);
    $('appearanceBackgroundEnabled').addEventListener('change', previewAppearance);
    $('appearanceBackgroundUrl').addEventListener('change', previewAppearance);
    $('appearanceDimInput').addEventListener('input', () => {
      updateAppearanceRangeLabels();
      previewAppearance();
    });
    $('appearancePanelOpacityInput').addEventListener('input', () => {
      updateAppearanceRangeLabels();
      previewAppearance();
    });
    $('loadLogsBtn').addEventListener('click', loadLogs);
    $('statusFilter').addEventListener('change', loadTasks);
    $('limitInput').addEventListener('change', loadTasks);
    $('qualityStatusFilter').addEventListener('change', async () => {
      try {
        await loadQualityEvents();
      } catch (error) {
        setConnection(error.message || String(error), 'error');
      }
    });
    $('qualityLimitInput').addEventListener('change', async () => {
      try {
        await loadQualityEvents();
      } catch (error) {
        setConnection(error.message || String(error), 'error');
      }
    });
    $('qualityUserIdInput').addEventListener('change', async () => {
      try {
        await loadQualityEvents();
      } catch (error) {
        setConnection(error.message || String(error), 'error');
      }
    });
    $('tokenInput').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        login();
      }
    });
    $('taskRows').addEventListener('click', (event) => {
      const button = event.target.closest('button[data-action]');
      if (!button || button.disabled) {
        return;
      }
      taskAction(button.dataset.action, button.dataset.id);
    });
    bindHomeWorkbench();
    bindAssistantHome();
    $('goalRunGrid').addEventListener('click', (event) => {
      const timelineButton = event.target.closest('[data-task-timeline]');
      if (timelineButton) {
        state.taskTimelineReturnFocus = timelineButton;
        state.taskTimelineReturnGoalId = timelineButton.dataset.taskTimeline || '';
        loadTaskTimeline(timelineButton.dataset.taskTimeline).catch((error) => setConnection(error.message || String(error), 'error'));
        return;
      }
      const detailButton = event.target.closest('[data-execution-task]');
      if (detailButton) loadTask(detailButton.dataset.executionTask).catch((error) => setConnection(error.message || String(error), 'error'));
    });
    $('formalApprovalList').addEventListener('click', (event) => {
      const button = event.target.closest('button[data-approval-action]');
      if (button && !button.disabled) decideFormalApproval(button);
    });
    $('view-tasks').addEventListener('click', (event) => {
      const button = event.target.closest('button[data-delivery-requeue]');
      if (button && !button.disabled) requeueDeadLetter(button);
    });
    $('closeTaskTimelineBtn').addEventListener('click', () => {
      $('taskTimelinePanel').classList.add('hidden');
      const currentTimelineButton = Array.from(document.querySelectorAll('[data-task-timeline]')).find(
        (button) => button.dataset.taskTimeline === state.taskTimelineReturnGoalId,
      );
      if (currentTimelineButton) {
        currentTimelineButton.focus();
      } else if (state.taskTimelineReturnFocus?.isConnected) {
        state.taskTimelineReturnFocus.focus();
      }
      state.taskTimelineReturnFocus = null;
      state.taskTimelineReturnGoalId = '';
    });

    let scrollRegionResizeTimer = null;
    window.addEventListener('resize', () => {
      window.clearTimeout(scrollRegionResizeTimer);
      scrollRegionResizeTimer = window.setTimeout(prepareScrollableRegions, 120);
    });

    renderMetrics({ total: 0, active: 0, counts: {} });
    renderExecutionOverview({ overview: { goals: { total: 0, counts: {} }, runs: { total: 0, counts: {} }, evidence: 0 }, goals: [], runs: [], evidence: [] });
    renderSystemAudit(null);
    checkSession();


