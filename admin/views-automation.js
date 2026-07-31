    function automationScheduleLabel(item) {
      const type = item.schedule_type || '';
      if (type === 'once') return `一次 · ${compactTimestamp(item.run_at)}`;
      if (type === 'daily') return `每天 ${item.time_of_day || '09:00'}`;
      if (type === 'weekly') {
        const names = ['一', '二', '三', '四', '五', '六', '日'];
        const days = String(item.weekdays || '0').split(',').map((value) => names[Number(value)]).filter(Boolean);
        return `每周${days.join('、')} ${item.time_of_day || '09:00'}`;
      }
      return `每 ${item.interval_minutes || 0} 分钟`;
    }

    function renderAutomationView() {
      const overview = state.automationOverview || {};
      const jobs = state.automationJobs || [];
      const runs = state.automationRuns || [];
      const visibleJobs = jobs.filter((item) => (
        Number(item.enabled)
        || ['running', 'dispatched'].includes(String(item.state || ''))
      ));
      const archivedJobCount = Math.max(0, jobs.length - visibleJobs.length);
      const failedRuns = runs.filter((item) => ['failed', 'error', 'timeout'].includes(String(item.status || '').toLowerCase()));
      $('automationSummary').innerHTML = [
        ['有效计划', visibleJobs.length, '已启用或正在执行', 'blue'],
        ['最近运行', runs.length, '当前回读窗口', 'blue'],
        ['运行失败', failedRuns.length, '需要查看证据', failedRuns.length ? 'amber' : 'green'],
        ['历史计划', archivedJobCount, '已停用并从主列表收起', 'blue'],
      ].map(([label, value, detail, tone], index) => `<article class="insight-card ${tone}"><span class="insight-index">0${index + 1}</span><p>${escapeHtml(label)}</p><strong>${escapeHtml(value)}</strong><small>${escapeHtml(detail)}</small></article>`).join('');

      const nextItems = (overview.next_items || []).filter((item) => item.kind !== 'proactive');
      $('automationRail').innerHTML = nextItems.length ? nextItems.map((item) => (
        `<li><time datetime="${escapeHtml(item.due_at || '')}">${escapeHtml(compactTimestamp(item.due_at))}</time><span class="rail-marker" aria-hidden="true"></span><div><strong>${escapeHtml(item.title || item.id)}</strong><small>定时任务 · ${escapeHtml(item.state || '')}</small></div></li>`
      )).join('') : '<li class="empty">暂无已启用的定时计划。</li>';

      const planRows = visibleJobs
        .map((item) => ({ sort: item.next_due_at || 'z', item }))
        .sort((left, right) => left.sort.localeCompare(right.sort));
      $('automationPlanList').innerHTML = planRows.length ? planRows.map(({ item }) => {
        const completedOnce = item.schedule_type === 'once' && item.state === 'completed';
        return `<article class="timeline-item" data-collection-type="job"><div class="timeline-head"><strong>${escapeHtml(item.title || item.id)}</strong><span class="badge ${Number(item.enabled) ? 'green' : 'amber'}">${completedOnce ? '已完成' : (Number(item.enabled) ? '启用' : '停用')}</span></div><p>${escapeHtml(automationScheduleLabel(item))} · ${escapeHtml(item.action_type === 'agent' ? '助手工作' : '提醒')}</p><small>下次 ${escapeHtml(compactTimestamp(item.next_due_at))} · 成功 ${escapeHtml(item.run_count || 0)} · 失败 ${escapeHtml(item.failed_count || 0)}</small><button class="secondary" type="button" data-automation-job-toggle="${escapeHtml(item.id)}" ${completedOnce ? 'disabled' : ''}>${completedOnce ? '已完成，请新建计划' : (Number(item.enabled) ? '暂停定时任务' : '启用定时任务')}</button></article>`;
      }).join('') : '<div class="empty">尚未创建有效计划。</div>';

      const activities = runs
        .map((item) => ({ at: item.started_at, title: item.title || item.job_id, meta: `定时任务 · ${item.status}`, detail: item.error || item.dispatch || '' }))
        .sort((left, right) => String(right.at || '').localeCompare(String(left.at || '')))
        .slice(0, 20);
      $('automationActivityList').innerHTML = activities.length ? activities.map((item) => `<article class="timeline-item"><div class="timeline-head"><strong>${escapeHtml(item.title)}</strong><time datetime="${escapeHtml(item.at || '')}">${escapeHtml(compactTimestamp(item.at))}</time></div><p>${escapeHtml(item.meta)}</p>${item.detail ? `<small>${escapeHtml(item.detail)}</small>` : ''}</article>`).join('') : '<div class="empty">暂无定时任务运行记录。</div>';
      $('automationStatus').className = 'provider-status ok';
      $('automationStatus').textContent = [
        `当前 ${visibleJobs.length} 个有效定时任务`,
        archivedJobCount ? `${archivedJobCount} 个已停用计划已从主列表收起` : '',
      ].filter(Boolean).join('；') + '。';
    }

    async function loadAutomationView() {
      const result = await bridge('/automations/overview');
      state.automationOverview = result;
      state.automationJobs = result.jobs || [];
      state.automationRuns = result.runs || [];
      renderAutomationView();
    }

    function updateAutomationScheduleFields() {
      const type = $('automationScheduleType').value;
      $('automationOnceFields').classList.toggle('hidden', type !== 'once');
      $('automationCalendarFields').classList.toggle('hidden', !['daily', 'weekly'].includes(type));
      $('automationWeekdayField').classList.toggle('hidden', type !== 'weekly');
      $('automationIntervalFields').classList.toggle('hidden', type !== 'interval');
    }

    async function saveAutomationJob(event) {
      event.preventDefault();
      const action = document.querySelector('input[name="automationActionType"]:checked')?.value || 'reminder';
      const scheduleType = $('automationScheduleType').value;
      const localRunAt = $('automationRunAtInput').value;
      const userId = $('automationUserIdInput').value.trim();
      if (!userId) throw new Error('请输入接收用户。');
      const payload = {
        user_id: userId,
        title: $('automationTitleInput').value.trim(),
        instruction: $('automationInstructionInput').value.trim(),
        action_type: action,
        schedule_type: scheduleType,
        run_at: localRunAt,
        time_of_day: $('automationTimeOfDayInput').value || '09:00',
        weekdays: [...$('automationWeekdayInput').selectedOptions].map((item) => item.value),
        interval_minutes: $('automationIntervalInput').value || '1440',
        timezone: $('automationTimezoneInput').value.trim() || 'Asia/Shanghai',
        enabled: $('automationEnabledInput').checked ? '1' : '0',
      };
      $('saveAutomationJobBtn').disabled = true;
      try {
        await bridge('/automations/jobs', { method: 'POST', body: JSON.stringify(payload) });
        $('automationJobForm').reset();
        $('automationTimezoneInput').value = 'Asia/Shanghai';
        $('automationTimeOfDayInput').value = '09:00';
        $('automationIntervalInput').value = '1440';
        updateAutomationScheduleFields();
        await loadAutomationView();
        setConnection('定时任务已保存。', 'ok');
      } finally {
        $('saveAutomationJobBtn').disabled = false;
      }
    }

