    // Home workbench: one real entrypoint for chat, light capabilities, and tracked work.

    const homeModeHints = {
      auto: '自动判断会优先使用轻量能力，复杂请求进入工作队列。',
      chat: '即时回答保留网页工作会话，不创建后台工作任务。',
      task: '创建可追踪工作，并在当前工作区生成任务记录与 Evidence。',
    };
    let homeDispatchPending = false;

    function newHomeDispatchRequestId() {
      const suffix = window.crypto?.randomUUID?.()
        || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
      return `web-workbench-${suffix}`;
    }

    function renderHomeDispatchResult(result) {
      const container = $('homeDispatchResult');
      const dispatchLabels = {
        chat: '即时回答',
        light: '轻量能力',
        task: '已创建工作',
        task_append: '已补充工作',
        approval_required: '等待确认',
      };
      const ok = Boolean(result.ok);
      const dispatch = result.dispatch || (ok ? 'chat' : 'error');
      container.hidden = false;
      $('homeDispatchStatus').className = `status-chip ${ok ? 'green' : 'red'}`;
      $('homeDispatchStatus').textContent = ok ? (dispatchLabels[dispatch] || '已处理') : '失败';
      $('homeDispatchResultTitle').textContent = dispatchLabels[dispatch] || (ok ? '处理结果' : '处理失败');
      $('homeDispatchReply').textContent = result.reply || result.error || '没有返回可显示的内容。';
      const taskId = result.task?.id || '';
      const openButton = $('homeDispatchOpenTaskBtn');
      openButton.dataset.taskId = taskId;
      openButton.classList.toggle('hidden', !taskId);
      container.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'nearest' });
    }

    async function submitHomeDispatch(event) {
      event.preventDefault();
      const prompt = $('homeDispatchPrompt').value.trim();
      if (!prompt || homeDispatchPending) return;
      homeDispatchPending = true;
      // This value is created once per deliberate form submission, never at
      // render time.  The current Workbench has no automatic resend path.
      const request = { id: newHomeDispatchRequestId(), message: prompt };
      const button = $('homeDispatchBtn');
      button.disabled = true;
      button.textContent = '助手正在判断…';
      $('homeDispatchResult').hidden = false;
      $('homeDispatchStatus').className = 'status-chip amber';
      $('homeDispatchStatus').textContent = '处理中';
      $('homeDispatchResultTitle').textContent = '当前助手正在处理';
      $('homeDispatchReply').textContent = '请求已送入网页工作会话，请稍候。';
      try {
        const result = await bridge('/assistant/dispatch', {
          method: 'POST',
          headers: {
            'X-QQ-Message-ID': request.id,
          },
          body: JSON.stringify({
            user_id: 'web-console',
            source: 'web-console',
            message: request.message,
            trace_id: request.id,
            force: $('homeDispatchMode').value,
            timeout: 180,
          }),
        });
        renderHomeDispatchResult(result);
        await loadAssistantHome();
        state.viewLoadedAt.overview = Date.now();
        state.viewLoadedAt.tasks = 0;
        setConnection('日常空间已更新。', 'ok');
      } catch (error) {
        renderHomeDispatchResult({ ok: false, error: error.message || String(error) });
        setConnection(error.message || String(error), 'error');
      } finally {
        homeDispatchPending = false;
        button.disabled = false;
        button.textContent = '开始';
      }
    }

    function bindHomeWorkbench() {
      $('homeDispatchForm')?.addEventListener('submit', submitHomeDispatch);
      $('homeDispatchMode')?.addEventListener('change', () => {
        $('homeDispatchHint').textContent = homeModeHints[$('homeDispatchMode').value] || homeModeHints.auto;
      });
      $('homeDispatchAgainBtn')?.addEventListener('click', () => {
        $('homeDispatchPrompt').focus();
        $('homeDispatchPrompt').select();
      });
      $('homeDispatchOpenTaskBtn')?.addEventListener('click', () => {
        const id = $('homeDispatchOpenTaskBtn').dataset.taskId;
        if (id) loadTask(id).catch((error) => setConnection(error.message || String(error), 'error'));
      });
    }
