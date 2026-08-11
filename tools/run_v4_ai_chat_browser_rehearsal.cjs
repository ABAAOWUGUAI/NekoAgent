'use strict';

/* External local-browser rehearsal. It is deliberately not a portable CI
 * dependency and never contacts a production server. */
const fs = require('fs');
const http = require('http');
const path = require('path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const read = (name) => fs.readFileSync(path.join(root, 'admin', name), 'utf8');
const shellSource = read('v4-shell.js');
const chatSource = read('v4-ai-chat-surface.js');
const chatCss = read('admin-v4-ai-chat-surface.css');
const shellCss = read('admin-v4-shell.css');
const chromePath = process.env.V4_BROWSER_EXECUTABLE || 'C:/Program Files/Google/Chrome/Application/chrome.exe';

const fixture = `<!doctype html><html lang="zh-CN"><head><link rel="stylesheet" href="/shell.css"><link rel="stylesheet" href="/chat.css"></head><body>
<section id="loginShell" class="hidden"></section>
<section id="appShell" class="app-shell"><aside class="sidebar"><div class="brand"></div><button id="logoutBtn" type="button">Logout</button></aside><main class="main"><header class="topbar"><div><h1 id="viewTitle" tabindex="-1">Overview</h1></div><div class="toolbar"><button id="quickTaskBtn" type="button">New</button></div></header><div id="contentViewport"><section id="view-overview" class="view">Legacy workbench</section><section id="view-tasks" class="view hidden">Legacy work</section><section id="view-projects" class="view hidden"></section><section id="view-automations" class="view hidden"></section><section id="view-artifacts" class="view hidden"></section><section id="view-brain" class="view hidden"></section><section id="view-assistant" class="view hidden"></section><section id="view-qq" class="view hidden"></section><section id="view-models" class="view hidden"></section><section id="view-capabilities" class="view hidden"></section><section id="view-proxy" class="view hidden"></section><section id="view-services" class="view hidden"></section><section id="view-logs" class="view hidden"></section><section id="view-relationship" class="view hidden"></section><section id="view-social" class="view hidden"></section><section id="view-growth" class="view hidden"></section><section id="view-settings" class="view hidden"></section></div></main></section>
<script>
window.__calls=[]; window.__requests=[]; window.__taskIds=[]; window.__mode='chat'; window.__retryCount=0; window.__resolve=null;
window.switchView=(view, options)=>{window.__calls.push({view, options});document.querySelectorAll('.view').forEach((node)=>node.classList.toggle('hidden',node.id!==('view-'+view)));if(options?.focusHeading)requestAnimationFrame(()=>document.getElementById('viewTitle').focus());};
window.loadTask=async(id)=>{window.__taskIds.push(id);window.switchView('tasks',{focusHeading:true});};
window.bridge=(route, options)=>{window.__requests.push({route,options});if(window.__mode==='retry'&&window.__retryCount++===0)return Promise.reject(new Error('simulated_network_gap'));if(window.__mode==='permission'){const error=new Error('qq_project_required');error.status=409;error.payload={ok:false,error:'qq_project_required'};return Promise.reject(error);}if(window.__mode==='conflict'){const error=new Error('web_dispatch_request_id_payload_conflict');error.status=409;error.payload={ok:false,error:'web_dispatch_request_id_payload_conflict'};return Promise.reject(error);}if(window.__mode==='auth'){const error=new Error('session_expired');error.status=403;error.payload={ok:false,error:'forbidden'};return Promise.reject(error);}if(window.__mode==='task')return Promise.resolve({ok:true,dispatch:'task',reply:'已创建工作。',task:{id:'task-42'}});if(window.__mode==='delayed')return new Promise((resolve)=>{window.__resolve=resolve;});return Promise.resolve({ok:true,dispatch:'chat',reply:window.__mode==='retry'?'重试结果已确认。':'即时回答已确认。'});};
</script><script defer src="/v4-shell.js"></script><script defer src="/v4-ai-chat-surface.js"></script></body></html>`;

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function main() {
  const server = http.createServer((request, response) => {
    if (request.url.startsWith('/v4-shell.js')) return response.end(shellSource);
    if (request.url.startsWith('/v4-ai-chat-surface.js')) return response.end(chatSource);
    if (request.url.startsWith('/chat.css')) return response.end(chatCss);
    if (request.url.startsWith('/shell.css')) return response.end(shellCss);
    response.setHeader('content-type', 'text/html; charset=utf-8');
    response.end(fixture);
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true, executablePath: chromePath });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    await page.goto(`http://127.0.0.1:${server.address().port}/?experience=v4`, { waitUntil: 'domcontentloaded' });
    const sidebar = (route) => page.locator(`#v4ShellSidebar [data-v4-route="${route}"]`).click();
    const inputAndSend = async (value) => {
      await page.locator('#v4AiChatPrompt').fill(value);
      await page.locator('#v4AiChatComposer').press('Control+Enter').catch(() => {});
      await page.locator('#v4AiChatComposer button[type="submit"]').click();
    };

    // A: AI Chat is an independent frontstage, not an Overview alias.
    await sidebar('chat');
    await page.waitForFunction(() => !document.querySelector('#v4AiChatSurface').hidden);
    const chatOpened = await page.evaluate(() => ({
      surface: getComputedStyle(document.querySelector('#v4AiChatSurface')).display,
      legacyOverview: getComputedStyle(document.querySelector('#view-overview')).display,
      inert: document.querySelector('#v4AiChatSurface').inert,
    }));
    assert(chatOpened.surface !== 'none' && chatOpened.legacyOverview === 'none' && !chatOpened.inert, `chat did not own its frontstage: ${JSON.stringify(chatOpened)}`);

    // B: a hidden Overview background refresh must not remount the Chat surface.
    await page.locator('#v4AiChatPrompt').fill('中文 IME 草稿 mixed ASCII');
    await page.locator('#v4AiChatPrompt').focus();
    const beforeOverviewRefresh = await page.evaluate(() => {
      const textarea = document.querySelector('#v4AiChatPrompt');
      textarea.setSelectionRange(textarea.value.length, textarea.value.length);
      const root = document.querySelector('#v4AiChatSurface');
      const original = root.replaceChildren.bind(root);
      window.__chatRenderCount = 0;
      root.replaceChildren = (...args) => { window.__chatRenderCount += 1; return original(...args); };
      return { value: textarea.value, caret: textarea.selectionStart, outline: getComputedStyle(textarea).outlineStyle };
    });
    await page.evaluate(() => document.querySelector('#view-overview').classList.toggle('overview-background-refresh'));
    await page.waitForTimeout(60);
    const afterOverviewRefresh = await page.evaluate(() => {
      const textarea = document.querySelector('#v4AiChatPrompt');
      return { active: document.activeElement?.id, value: textarea?.value, caret: textarea?.selectionStart, renders: window.__chatRenderCount, outline: getComputedStyle(textarea).outlineStyle };
    });
    assert(
      afterOverviewRefresh.active === 'v4AiChatPrompt'
        && afterOverviewRefresh.value === beforeOverviewRefresh.value
        && afterOverviewRefresh.caret === beforeOverviewRefresh.caret
        && afterOverviewRefresh.renders === 0
        && afterOverviewRefresh.outline !== 'none',
      `hidden Overview refresh remounted or obscured Chat input: ${JSON.stringify({beforeOverviewRefresh, afterOverviewRefresh})}`,
    );

    // C: real dispatch payload remains auto-routed and avoids model controls.
    await inputAndSend('帮我解释这项工作');
    await page.waitForFunction(() => document.querySelector('#v4AiChatSurface')?.textContent.includes('即时回答已确认。'));
    const firstRequest = await page.evaluate(() => window.__requests[0]);
    const payload = JSON.parse(firstRequest.options.body);
    assert(firstRequest.route === '/assistant/dispatch' && payload.user_id === 'web-console' && payload.source === 'web-console' && payload.force === 'auto', `dispatch contract drifted: ${JSON.stringify({route:firstRequest.route,payload})}`);
    assert(firstRequest.options.headers['X-QQ-Message-ID'] && firstRequest.options.headers['X-QQ-Actor-ID'] === 'web-console', 'receipt headers are missing or malformed');

    // D: an uncertain response can only query the same protected request.
    await page.evaluate(() => { window.__mode = 'retry'; });
    await inputAndSend('请继续这个请求');
    await page.waitForFunction(() => document.querySelector('[data-v4-chat-retry="true"]'));
    const retryBefore = await page.evaluate(() => ({ request: window.__requests.at(-1), users: document.querySelectorAll('.v4-ai-chat-turn-user').length }));
    await page.locator('[data-v4-chat-retry="true"]').click();
    await page.waitForFunction(() => document.querySelector('#v4AiChatSurface')?.textContent.includes('重试结果已确认。'));
    const retryAfter = await page.evaluate(() => ({ request: window.__requests.at(-1), users: document.querySelectorAll('.v4-ai-chat-turn-user').length }));
    assert(retryBefore.request.options.headers['X-QQ-Message-ID'] === retryAfter.request.options.headers['X-QQ-Message-ID'] && retryBefore.users === retryAfter.users, 'retry created a different receipt or duplicate turn');

    // E: deterministic, conflict and auth errors do not receive a generic retry.
    await page.evaluate(() => { window.__mode = 'permission'; });
    await inputAndSend('这项需要项目的工作');
    await page.waitForFunction(() => document.querySelector('#v4AiChatFailure')?.textContent.includes('选择项目'));
    assert(!await page.locator('[data-v4-chat-retry="true"]').count(), 'deterministic project error incorrectly exposed a retry');
    await page.evaluate(() => { window.__mode = 'conflict'; });
    await page.locator('#v4AiChatPrompt').fill('产生冲突的请求');
    await page.locator('#v4AiChatComposer button[type="submit"]').click();
    await page.waitForFunction(() => document.querySelector('#v4AiChatFailure')?.textContent.includes('另一份内容'));
    assert(!await page.locator('[data-v4-chat-retry="true"]').count(), 'payload conflict incorrectly exposed a retry');
    await page.evaluate(() => { window.__mode = 'auth'; });
    await page.locator('#v4AiChatPrompt').fill('认证状态测试');
    await page.locator('#v4AiChatComposer button[type="submit"]').click();
    await page.waitForFunction(() => document.querySelector('[data-v4-chat-recovery="login"]'));

    // F: task result opens the existing Work owner through its actual task function.
    await sidebar('chat');
    await page.evaluate(() => { window.__mode = 'task'; });
    await inputAndSend('整理并完成这个工作');
    await page.waitForFunction(() => document.querySelector('[data-v4-chat-task="task-42"]'));
    await page.locator('[data-v4-chat-task="task-42"]').click();
    await page.waitForFunction(() => document.querySelector('[data-v4-route][aria-current="page"]')?.dataset.v4Route === 'work');
    assert(await page.evaluate(() => window.__taskIds.join(',')) === 'task-42', 'task handoff bypassed the existing task opener');

    // G: stale results cannot become visible after leaving the Owner Surface.
    await sidebar('chat');
    await page.evaluate(() => { window.__mode = 'delayed'; });
    await inputAndSend('这个响应不应在离开后显示');
    await page.waitForFunction(() => typeof window.__resolve === 'function');
    await sidebar('work');
    await page.evaluate(() => window.__resolve({ok:true,dispatch:'chat',reply:'陈旧响应'}));
    await page.waitForTimeout(60);
    const afterLeave = await page.evaluate(() => ({
      hidden: document.querySelector('#v4AiChatSurface').hidden,
      inert: document.querySelector('#v4AiChatSurface').inert,
      staleVisible: document.querySelector('#v4AiChatSurface').textContent.includes('陈旧响应'),
    }));
    assert(afterLeave.hidden && afterLeave.inert && !afterLeave.staleVisible, `stale response leaked after owner exit: ${JSON.stringify(afterLeave)}`);

    // H: repeated lifecycle transitions retain exactly one surface and one handler path.
    await page.locator('#v4ShellToggle').click();
    await page.locator('#v4ShellToggle').click();
    await sidebar('chat');
    assert(await page.locator('#v4AiChatSurface').count() === 1, 'lifecycle transitions duplicated the Chat root');

    // I: Return Legacy restores focus to a visible legacy heading.
    await sidebar('chat');
    await page.locator('#v4ReturnLegacyBtn').click();
    await page.waitForFunction(() => document.activeElement?.id === 'viewTitle');
    const legacyReturn = await page.evaluate(() => ({ hidden: document.querySelector('#v4AiChatSurface').hidden, inert: document.querySelector('#v4AiChatSurface').inert, overview: getComputedStyle(document.querySelector('#view-overview')).display }));
    assert(legacyReturn.hidden && legacyReturn.inert && legacyReturn.overview !== 'none', `legacy return did not retain a visible fallback: ${JSON.stringify(legacyReturn)}`);

    // J: the narrow surface retains its labelled compositor without page overflow.
    await page.locator('#v4ShellToggle').click();
    await sidebar('chat');
    await page.setViewportSize({ width: 390, height: 844 });
    const narrow = await page.evaluate(() => ({
      documentOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      surfaceOverflow: document.querySelector('#v4AiChatSurface').scrollWidth > document.querySelector('#v4AiChatSurface').clientWidth,
      label: document.querySelector('label[for="v4AiChatPrompt"]')?.textContent || '',
      submitName: document.querySelector('#v4AiChatComposer button[type="submit"]')?.textContent || '',
    }));
    assert(!narrow.documentOverflow && !narrow.surfaceOverflow && narrow.label && narrow.submitName, `narrow accessibility/responsiveness regression: ${JSON.stringify(narrow)}`);

    process.stdout.write(JSON.stringify({
      scenarios: ['A:frontstage-not-overview', 'B:hidden-overview-refresh-no-remount', 'C:auto-dispatch-contract', 'D:uncertain-same-id-query', 'E:error-classification', 'F:task-handoff', 'G:stale-response-cancelled', 'H:repeated-lifecycle', 'I:return-legacy-focus', 'J:narrow-labelled-composer'],
      requestCount: await page.evaluate(() => window.__requests.length),
      chromePath,
    }));
  } finally {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}

main().catch((error) => { console.error(error.stack || error); process.exitCode = 1; });
