    // Product Gate C8: local, lazy-loaded Project lifecycle component.
    function renderProjects(projects, current) {
      state.projects = projects || [];
      state.currentProject = current || null;
      const showArchived = Boolean($('showArchivedProjects')?.checked);
      const archivedCount = state.projects.filter((project) => project.status === 'archived' || project.active === false).length;
      const visible = state.projects.filter((project) => showArchived || (project.status !== 'archived' && project.active !== false));
      const metaText = `${state.projects.length - archivedCount} 个使用中 · ${archivedCount} 个已归档`;
      $('projectListMeta').textContent = metaText;
      const progressiveHint = $('projectListMeta').closest('details.progressive-panel')?.querySelector(':scope > summary span');
      if (progressiveHint) progressiveHint.textContent = metaText;
      if (!visible.length) {
        $('projectRows').innerHTML = '<tr><td colspan="5" class="empty">暂无项目。</td></tr>';
        return;
      }
      const currentId = current?.id || '';
      $('projectRows').innerHTML = visible.map((project) => {
        const id = String(project.id || '');
        const isCurrent = id === currentId;
        const archived = project.status === 'archived' || project.active === false;
        const tasks = project.task_summary || {};
        const taskText = tasks.available === false ? '暂不可用' : `${Number(tasks.total || 0)} 项`;
        const taskHint = tasks.available === false ? '任务数据库暂不可用' : `${Number(tasks.active || 0)} 项进行中`;
        return `<tr data-motion-item>
          <td><strong>${escapeHtml(project.name || '')}</strong><small class="mono">${escapeHtml(id)}</small></td>
          <td class="mono project-path">${escapeHtml(project.path || '')}</td>
          <td><span class="badge ${isCurrent ? 'blue' : ''}">${isCurrent ? '当前' : (archived ? '已归档' : '使用中')}</span></td>
          <td><span class="project-task-count"><strong>${escapeHtml(taskText)}</strong><small>${escapeHtml(taskHint)}</small></span></td>
          <td><div class="project-actions">
            ${archived ? '' : `<button class="secondary" data-project-action="switch" data-project-id="${escapeHtml(id)}" type="button" aria-label="切换到 ${escapeHtml(project.name || id)}" ${isCurrent ? 'disabled' : ''}>切换使用</button>`}
            <button class="secondary" data-project-action="tasks" data-project-id="${escapeHtml(id)}" type="button" aria-label="查看 ${escapeHtml(project.name || id)} 的关联任务">关联任务</button>
            ${archived
              ? `<button class="secondary" data-project-action="restore" data-project-id="${escapeHtml(id)}" type="button" aria-label="恢复 ${escapeHtml(project.name || id)}">恢复</button>`
              : `<button class="secondary" data-project-action="edit" data-project-id="${escapeHtml(id)}" type="button" aria-label="编辑 ${escapeHtml(project.name || id)}">编辑</button><button class="danger" data-project-action="archive" data-project-id="${escapeHtml(id)}" type="button" aria-label="归档 ${escapeHtml(project.name || id)}" ${isCurrent ? 'disabled' : ''}>归档</button>`}
          </div></td>
        </tr>`;
      }).join('');
      window.AdminMotion?.enterView($('projectRows'));
    }

    async function createProject() {
      const name = $('projectNameInput').value.trim();
      if (!name) return setConnection('请输入项目名称。', 'error');
      const payload = { name, path: $('projectPathInput').value.trim(), description: $('projectDescInput').value.trim() };
      try {
        $('createProjectBtn').disabled = true;
        const result = await bridge('/projects', { method: 'POST', body: JSON.stringify(payload) });
        $('projectNameInput').value = '';
        $('projectPathInput').value = '';
        $('projectDescInput').value = '';
        setConnection(`项目 ${result.project?.name || name} 已创建并切换。`, 'ok');
        await Promise.all([loadProjectsPanel(), loadCodegraphStatus()]);
      } catch (error) {
        setConnection(error.message || String(error), 'error');
      } finally {
        $('createProjectBtn').disabled = false;
      }
    }

    async function switchProject(projectId) {
      try {
        await bridge('/projects/current', { method: 'POST', body: JSON.stringify({ id: projectId }) });
        setConnection('当前项目已切换。', 'ok');
        await Promise.all([loadProjectsPanel(), loadCodegraphStatus()]);
      } catch (error) {
        setConnection(error.message || String(error), 'error');
      }
    }

    function projectById(projectId) {
      return state.projects.find((item) => String(item.id || '') === String(projectId || '')) || null;
    }

    function closeProjectDialog(dialog, restoreFocus = true) {
      if (dialog?.open) dialog.close();
      if (restoreFocus && state.projectDialogReturnFocus?.isConnected) {
        state.projectDialogReturnFocus.focus();
      }
      state.projectDialogReturnFocus = null;
    }

    function focusProjectAction(projectId, action) {
      const selector = `button[data-project-id="${CSS.escape(projectId)}"][data-project-action="${action}"]`;
      requestAnimationFrame(() => (document.querySelector(selector) || $('showArchivedProjects') || $('createProjectBtn'))?.focus());
    }

    function openProjectEdit(projectId) {
      const project = projectById(projectId);
      if (!project) {
        setConnection('没有找到该项目，项目列表将刷新。', 'error');
        loadProjectsPanel();
        return;
      }
      const dialog = $('projectEditDialog');
      dialog.dataset.projectId = project.id;
      dialog.dataset.updatedAt = project.updated_at || '';
      $('projectEditName').value = project.name || '';
      $('projectEditDescription').value = project.description || '';
      $('projectEditStatus').textContent = '';
      dialog.showModal();
      $('projectEditName').focus();
    }

    async function submitProjectEdit(event) {
      event.preventDefault();
      const dialog = $('projectEditDialog');
      const projectId = dialog.dataset.projectId || '';
      const submit = dialog.querySelector('button[type="submit"]');
      try {
        submit.disabled = true;
        $('projectEditStatus').className = 'provider-status pending';
        $('projectEditStatus').textContent = '正在保存项目资料。';
        const result = await bridge(`/projects/${encodeURIComponent(projectId)}/rename`, {
          method: 'POST',
          body: JSON.stringify({ name: $('projectEditName').value.trim(), description: $('projectEditDescription').value.trim(), expected_updated_at: dialog.dataset.updatedAt || '' }),
        });
        closeProjectDialog(dialog, false);
        setConnection(`项目 ${result.project?.name || projectId} 已更新；ID 和工作目录保持不变。`, 'ok');
        await loadProjectsPanel();
        focusProjectAction(projectId, 'edit');
      } catch (error) {
        $('projectEditStatus').className = 'provider-status error';
        $('projectEditStatus').textContent = error.message || String(error);
      } finally {
        submit.disabled = false;
      }
    }

    function openProjectArchive(projectId) {
      const project = projectById(projectId);
      if (!project) {
        setConnection('没有找到该项目，项目列表将刷新。', 'error');
        loadProjectsPanel();
        return;
      }
      const dialog = $('projectArchiveDialog');
      dialog.dataset.projectId = project.id;
      dialog.dataset.updatedAt = project.updated_at || '';
      $('projectArchiveDescription').textContent = `将“${project.name || project.id}”移出使用中项目列表。`;
      $('confirmProjectArchiveBtn').textContent = `确认归档 ${project.name || project.id}`;
      $('projectArchiveStatus').textContent = '';
      dialog.showModal();
      $('confirmProjectArchiveBtn').focus();
    }

    async function submitProjectArchive(event) {
      event.preventDefault();
      const dialog = $('projectArchiveDialog');
      const projectId = dialog.dataset.projectId || '';
      try {
        $('confirmProjectArchiveBtn').disabled = true;
        $('projectArchiveStatus').className = 'provider-status pending';
        $('projectArchiveStatus').textContent = '正在归档项目记录。';
        const result = await bridge(`/projects/${encodeURIComponent(projectId)}/archive`, {
          method: 'POST', body: JSON.stringify({ confirm_archive: true, expected_updated_at: dialog.dataset.updatedAt || '' }),
        });
        closeProjectDialog(dialog, false);
        setConnection(`项目 ${result.project?.name || projectId} 已归档，工作目录和历史数据均已保留。`, 'ok');
        await loadProjectsPanel();
        $('showArchivedProjects')?.focus();
      } catch (error) {
        $('projectArchiveStatus').className = 'provider-status error';
        $('projectArchiveStatus').textContent = error.message || String(error);
      } finally {
        $('confirmProjectArchiveBtn').disabled = false;
      }
    }

    async function restoreProject(projectId) {
      const project = projectById(projectId);
      if (!project) return;
      try {
        await bridge(`/projects/${encodeURIComponent(projectId)}/restore`, {
          method: 'POST', body: JSON.stringify({ expected_updated_at: project.updated_at || '' }),
        });
        setConnection(`项目 ${project.name || projectId} 已恢复，可重新切换使用。`, 'ok');
        await loadProjectsPanel();
        focusProjectAction(projectId, 'edit');
      } catch (error) {
        setConnection(error.message || String(error), 'error');
      }
    }

    async function openProjectTasks(projectId) {
      const project = projectById(projectId);
      const dialog = $('projectTasksDialog');
      $('projectTasksHeading').textContent = `${project?.name || projectId} · 关联任务`;
      $('projectTaskList').innerHTML = '<p class="empty">正在读取关联任务。</p>';
      dialog.showModal();
      try {
        const result = await bridge(`/projects/${encodeURIComponent(projectId)}/tasks?limit=10`);
        const tasks = Array.isArray(result.tasks) ? result.tasks : [];
        $('projectTaskList').innerHTML = tasks.length
          ? tasks.map((task) => `<article class="project-task-item"><div><strong>${escapeHtml(task.summary || task.id || '未命名任务')}</strong><p class="mono">${escapeHtml(task.id || '')}</p></div><div><span class="badge">${escapeHtml(statusLabels[task.status] || task.status || '未知')}</span><small>${escapeHtml(compactTimestamp(task.created_at))}</small></div></article>`).join('')
          : '<p class="empty">这个项目还没有关联任务。</p>';
      } catch (error) {
        $('projectTaskList').innerHTML = `<p class="empty">${escapeHtml(error.message || String(error))}</p>`;
      }
    }

    (() => {
      let bound = false;

      function mountProjectDialogs() {
        if ($('projectEditDialog')) return;
        const mount = document.createElement('div');
        mount.id = 'projectDialogMount';
        mount.innerHTML = `
          <dialog id="projectEditDialog" class="project-dialog" aria-labelledby="projectEditHeading">
            <form id="projectEditForm">
              <div class="project-dialog-heading">
                <h3 id="projectEditHeading">编辑项目资料</h3>
                <button class="dialog-close" type="button" data-close-project-dialog aria-label="关闭项目编辑对话框">关闭</button>
              </div>
              <p class="compact-note">项目 ID 和工作目录保持不变，历史任务仍关联原项目。</p>
              <label>项目名称<input id="projectEditName" type="text" maxlength="80" required></label>
              <label>项目说明<textarea id="projectEditDescription" maxlength="2000" spellcheck="false"></textarea></label>
              <p id="projectEditStatus" class="provider-status" role="status" aria-live="polite"></p>
              <div class="button-row">
                <button class="primary" type="submit">保存项目资料</button>
                <button class="secondary" type="button" data-close-project-dialog>取消</button>
              </div>
            </form>
          </dialog>
          <dialog id="projectArchiveDialog" class="project-dialog" aria-labelledby="projectArchiveHeading">
            <form id="projectArchiveForm">
              <div class="project-dialog-heading">
                <h3 id="projectArchiveHeading">确认归档项目</h3>
                <button class="dialog-close" type="button" data-close-project-dialog aria-label="关闭项目归档确认对话框">关闭</button>
              </div>
              <p id="projectArchiveDescription"></p>
              <p class="compact-note">归档只停止新选择，不删除工作目录、历史任务、记忆或成品。当前项目必须先切换后才能归档。</p>
              <p id="projectArchiveStatus" class="provider-status" role="status" aria-live="polite"></p>
              <div class="button-row">
                <button id="confirmProjectArchiveBtn" class="danger" type="submit">确认归档项目</button>
                <button class="secondary" type="button" data-close-project-dialog>取消</button>
              </div>
            </form>
          </dialog>
          <dialog id="projectTasksDialog" class="project-dialog" aria-labelledby="projectTasksHeading">
            <div class="project-dialog-heading">
              <h3 id="projectTasksHeading">项目关联任务</h3>
              <button class="dialog-close" type="button" data-close-project-dialog aria-label="关闭项目关联任务对话框">关闭</button>
            </div>
            <div id="projectTaskList" class="project-task-list"><p class="empty">正在读取关联任务。</p></div>
            <div class="button-row"><button class="secondary" type="button" data-close-project-dialog>关闭</button></div>
          </dialog>`;
        document.body.appendChild(mount);
      }

      window.bindProjectLifecycleEvents = () => {
        mountProjectDialogs();
        if (bound) return;
        bound = true;
        $('createProjectBtn')?.addEventListener('click', () => createProject());
        $('projectRows')?.addEventListener('click', (event) => {
          const button = event.target.closest('button[data-project-action]');
          if (!button || button.disabled) return;
          const action = button.dataset.projectAction;
          if (action === 'switch') switchProject(button.dataset.projectId);
          else if (action === 'edit') {
            state.projectDialogReturnFocus = button;
            openProjectEdit(button.dataset.projectId);
          } else if (action === 'archive') {
            state.projectDialogReturnFocus = button;
            openProjectArchive(button.dataset.projectId);
          }
          else if (action === 'restore') restoreProject(button.dataset.projectId);
          else if (action === 'tasks') {
            state.projectDialogReturnFocus = button;
            openProjectTasks(button.dataset.projectId);
          }
        });
        $('showArchivedProjects')?.addEventListener('change', () => renderProjects(state.projects, state.currentProject));
        $('projectEditForm')?.addEventListener('submit', submitProjectEdit);
        $('projectArchiveForm')?.addEventListener('submit', submitProjectArchive);
        document.querySelectorAll('[data-close-project-dialog]').forEach((button) => {
          button.addEventListener('click', () => closeProjectDialog(button.closest('dialog')));
        });
      };
    })();
