'use strict';

/* V4.1 AI Chat first slice. Existing dispatch, Work and approval paths remain
 * the behavioral owners; this layer only presents one current-page frontstage. */
(() => {
  const routeEvent = 'nekoagent:v4-route-change';
  const dispatchEndpoint = '/assistant/dispatch';
  const webConsoleUserId = 'web-console';
  const dispatchLabels = Object.freeze({
    chat: '即时回答',
    light: '轻量处理',
    task: '已创建工作',
    task_append: '已补充工作',
    approval_required: '等待确认',
    goal_followup_clarification: '需要补充信息',
    goal_feedback: '已记录反馈',
  });
  let root = null;
  let active = false;
  let submitting = false;
  let requestVersion = 0;
  let retryableRequest = null;
  let draft = '';
  let turns = [];

  function makeNode(tagName, className, text) {
    const node = document.createElement(tagName);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function newRequestId() {
    const suffix = window.crypto?.randomUUID?.()
      || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    return `web-v4-${suffix}`;
  }

  function statusLabel(result) {
    return dispatchLabels[String(result?.dispatch || '')] || '已处理';
  }

  function assistantTurn(result) {
    const dispatch = String(result?.dispatch || 'chat');
    return {
      kind: 'assistant',
      dispatch,
      text: String(result?.reply || '').trim() || '已收到处理结果；请在既有工作流继续查看。',
      taskId: String(result?.task?.id || '').trim(),
    };
  }

  function currentRequest() {
    if (retryableRequest) return retryableRequest;
    return { id: newRequestId(), message: draft.trim() };
  }

  function hasVisibleRequest(request) {
    return turns.some((turn) => turn.kind === 'user' && turn.requestId === request.id);
  }

  function showLegacyWorkbench() {
    if (!root) return;
    requestVersion += 1;
    active = false;
    submitting = false;
    document.body.setAttribute('data-v4-chat-legacy-mode', 'true');
    root.hidden = true;
    root.inert = true;
    window.switchView?.('overview', { focusHeading: true });
  }

  async function openTask(taskId) {
    if (!taskId) return;
    if (typeof window.loadTask !== 'function') {
      showLegacyWorkbench();
      return;
    }
    try {
      await window.loadTask(taskId);
    } catch (_) {
      showLegacyWorkbench();
    }
  }

  function appendTurnCard(turn) {
    const card = makeNode('article', `v4-ai-chat-turn v4-ai-chat-turn-${turn.kind}`);
    const heading = makeNode('p', 'v4-ai-chat-turn-label', turn.kind === 'user' ? '你' : '当前 Assistant');
    card.append(heading);
    if (turn.kind === 'assistant') {
      const badge = makeNode('span', 'v4-ai-chat-dispatch', statusLabel(turn));
      badge.dataset.dispatch = turn.dispatch || 'chat';
      heading.append(' ', badge);
    }
    card.append(makeNode('p', 'v4-ai-chat-turn-copy', turn.text));
    if (turn.taskId) {
      const taskButton = makeNode('button', 'v4-ai-chat-turn-action', '在工作中继续');
      taskButton.type = 'button';
      taskButton.dataset.v4ChatTask = turn.taskId;
      card.append(taskButton);
    } else if (turn.dispatch === 'approval_required') {
      const workbenchButton = makeNode('button', 'v4-ai-chat-turn-action', '使用现有工作台处理');
      workbenchButton.type = 'button';
      workbenchButton.dataset.v4ChatLegacy = 'approval';
      card.append(workbenchButton);
    }
    return card;
  }

  function render() {
    if (!root) return;
    root.replaceChildren();
    const header = makeNode('header', 'v4-ai-chat-header');
    const copy = makeNode('div', 'v4-ai-chat-heading');
    const title = makeNode('h2', '', '从一段对话开始完成工作');
    title.id = 'v4AiChatTitle';
    copy.append(
      makeNode('p', 'v4-surface-eyebrow', 'AI Chat · 深度协作'),
      title,
      makeNode('p', 'v4-ai-chat-intro', '当前 Assistant 会沿用既有受控路由：直接回答、创建或补充工作，敏感操作仍会要求确认。'),
    );
    const legacy = makeNode('button', 'v4-ai-chat-legacy', '使用现有工作台');
    legacy.type = 'button';
    legacy.dataset.v4ChatLegacy = 'workbench';
    header.append(copy, legacy);
    root.append(header);

    const continuity = makeNode('p', 'v4-ai-chat-continuity', '当前页对话不会在刷新后回显历史；连续上下文仍由既有网页会话路径处理。');
    continuity.setAttribute('role', 'status');
    continuity.setAttribute('aria-live', 'polite');
    root.append(continuity);

    const transcript = makeNode('section', 'v4-ai-chat-transcript');
    transcript.setAttribute('aria-labelledby', 'v4AiChatTranscriptTitle');
    const transcriptTitle = makeNode('h3', 'v4-ai-chat-transcript-title', '当前页对话');
    transcriptTitle.id = 'v4AiChatTranscriptTitle';
    transcript.append(transcriptTitle);
    if (turns.length) turns.forEach((turn) => transcript.append(appendTurnCard(turn)));
    else transcript.append(makeNode('p', 'v4-ai-chat-empty', '可以直接提问，或交代一件需要持续推进的事。'));
    if (submitting) {
      const pending = makeNode('p', 'v4-ai-chat-pending', '正在处理本次请求…');
      pending.setAttribute('role', 'status');
      pending.setAttribute('aria-live', 'polite');
      transcript.append(pending);
    }
    root.append(transcript);

    if (retryableRequest && !submitting) {
      const retry = makeNode('section', 'v4-ai-chat-retry');
      retry.append(makeNode('p', '', '尚未收到可确认的处理结果。重试将沿用同一请求标识，不会另起一项工作。'));
      const retryButton = makeNode('button', 'v4-ai-chat-retry-button', '重试本次请求');
      retryButton.type = 'button';
      retryButton.dataset.v4ChatRetry = 'true';
      retry.append(retryButton);
      root.append(retry);
    }

    const form = makeNode('form', 'v4-ai-chat-composer');
    form.id = 'v4AiChatComposer';
    const label = makeNode('label', 'v4-ai-chat-composer-label', '交代给当前 Assistant');
    label.htmlFor = 'v4AiChatPrompt';
    const textarea = document.createElement('textarea');
    textarea.id = 'v4AiChatPrompt';
    textarea.name = 'message';
    textarea.maxLength = 12000;
    textarea.rows = 4;
    textarea.value = draft;
    textarea.placeholder = '问一个问题，或说明你希望完成什么。';
    textarea.disabled = submitting || Boolean(retryableRequest);
    const footer = makeNode('div', 'v4-ai-chat-composer-footer');
    footer.append(makeNode('p', '', '模型路由、确认和工作权限继续遵循现有受控路径。'));
    const submit = makeNode('button', 'v4-ai-chat-submit', submitting ? '处理中…' : '发送');
    submit.type = 'submit';
    submit.disabled = submitting || Boolean(retryableRequest) || !draft.trim();
    footer.append(submit);
    form.append(label, textarea, footer);
    root.append(form);
  }

  async function submitCurrentRequest() {
    const request = currentRequest();
    if (!active || submitting || !request.message) return;
    const version = ++requestVersion;
    submitting = true;
    retryableRequest = request;
    if (!hasVisibleRequest(request)) turns.push({ kind: 'user', requestId: request.id, text: request.message });
    render();
    try {
      if (typeof window.bridge !== 'function') throw new Error('v4_ai_chat_bridge_unavailable');
      const result = await window.bridge(dispatchEndpoint, {
        method: 'POST',
        headers: {
          'X-QQ-Message-ID': request.id,
          'X-QQ-Actor-ID': 'web-console',
        },
        body: JSON.stringify({
          user_id: 'web-console',
          source: 'web-console',
          message: request.message,
          trace_id: request.id,
          force: 'auto',
          timeout: 180,
        }),
      });
      if (version !== requestVersion || !active) return;
      if (!result?.ok) throw new Error('v4_ai_chat_unconfirmed');
      turns.push(assistantTurn(result));
      retryableRequest = null;
      draft = '';
    } catch (_) {
      if (version !== requestVersion || !active) return;
    } finally {
      if (version === requestVersion && active) {
        submitting = false;
        render();
      }
    }
  }

  function activate() {
    if (!root) return;
    document.body.removeAttribute('data-v4-chat-legacy-mode');
    root.hidden = false;
    root.inert = false;
    active = true;
    render();
  }

  function deactivate() {
    if (!root) return;
    requestVersion += 1;
    active = false;
    submitting = false;
    retryableRequest = null;
    draft = '';
    turns = [];
    root.hidden = true;
    root.inert = true;
    document.body.removeAttribute('data-v4-chat-legacy-mode');
  }

  function mount() {
    const viewport = document.getElementById('contentViewport');
    if (!viewport || root) return;
    root = makeNode('section', 'v4-ai-chat-surface');
    root.id = 'v4AiChatSurface';
    root.hidden = true;
    root.inert = true;
    root.setAttribute('aria-labelledby', 'v4AiChatTitle');
    root.addEventListener('input', (event) => {
      if (event.target?.id !== 'v4AiChatPrompt') return;
      draft = event.target.value;
      const button = root.querySelector('.v4-ai-chat-submit');
      if (button) button.disabled = submitting || Boolean(retryableRequest) || !draft.trim();
    });
    root.addEventListener('submit', (event) => {
      if (event.target?.id !== 'v4AiChatComposer') return;
      event.preventDefault();
      submitCurrentRequest();
    });
    root.addEventListener('click', (event) => {
      const target = event.target.closest('button');
      if (!target) return;
      if (target.dataset.v4ChatTask) openTask(target.dataset.v4ChatTask);
      else if (target.dataset.v4ChatLegacy) showLegacyWorkbench();
      else if (target.dataset.v4ChatRetry) submitCurrentRequest();
    });
    viewport.prepend(root);
    document.addEventListener(routeEvent, (event) => {
      if (event.detail?.routeId === 'chat' && document.body.dataset.v4Experience === 'active') activate();
      else deactivate();
    });
    document.addEventListener('nekoagent:v4-experience-disable', deactivate);
    if (document.body.dataset.v4Experience === 'active' && document.body.dataset.v4ActiveView === 'chat') activate();
  }

  document.addEventListener('DOMContentLoaded', mount, { once: true });
})();
