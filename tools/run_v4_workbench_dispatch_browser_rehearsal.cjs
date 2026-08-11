'use strict';

/* Exercises the real Legacy Home Workbench source with a local-only fixture.
 * It never starts the production bridge or sends a network request externally. */
const fs = require('fs');
const http = require('http');
const path = require('path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const workbenchSource = fs.readFileSync(path.join(root, 'admin', 'views-workbench.js'), 'utf8');
const chromePath = process.env.V4_BROWSER_EXECUTABLE || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const fixture = `<!doctype html><html lang="zh-CN"><body>
<form id="homeDispatchForm"><textarea id="homeDispatchPrompt"></textarea><select id="homeDispatchMode"><option value="auto">auto</option></select><button id="homeDispatchBtn" type="submit">开始</button></form>
<p id="homeDispatchHint"></p><section id="homeDispatchResult" hidden><span id="homeDispatchStatus"></span><h2 id="homeDispatchResultTitle"></h2><p id="homeDispatchReply"></p><button id="homeDispatchOpenTaskBtn" class="hidden"></button></section><button id="homeDispatchAgainBtn" type="button">再次编辑</button>
<script>
window.$=(id)=>document.getElementById(id); window.state={viewLoadedAt:{}}; window.loadAssistantHome=async()=>{}; window.setConnection=()=>{}; window.loadTask=async()=>{};
window.__requests=[]; window.__dispatchResolvers=[];
window.bridge=(route,options)=>{window.__requests.push({route,options});return new Promise((resolve)=>window.__dispatchResolvers.push(resolve));};
</script><script defer src="/views-workbench.js"></script></body></html>`;

function assert(condition, message) { if (!condition) throw new Error(message); }

async function main() {
  const server = http.createServer((request, response) => {
    if (request.url.startsWith('/views-workbench.js')) return response.end(workbenchSource);
    response.setHeader('content-type', 'text/html; charset=utf-8');
    response.end(fixture);
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true, executablePath: chromePath });
  try {
    const page = await browser.newPage({ viewport: { width: 1024, height: 720 } });
    await page.goto(`http://127.0.0.1:${server.address().port}/`, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => typeof window.bindHomeWorkbench === 'function');
    await page.evaluate(() => window.bindHomeWorkbench());
    await page.locator('#homeDispatchPrompt').fill('Legacy request identity must be stable');
    await page.locator('#homeDispatchForm').evaluate((form) => {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    });
    await page.waitForFunction(() => window.__requests.length === 1);
    const first = await page.evaluate(() => window.__requests[0]);
    const firstPayload = JSON.parse(first.options.body);
    assert(first.route === '/assistant/dispatch', `unexpected dispatch route: ${first.route}`);
    assert(first.options.headers['X-QQ-Message-ID'].startsWith('web-workbench-'), 'Legacy Workbench did not send a browser request ID');
    assert(firstPayload.trace_id === first.options.headers['X-QQ-Message-ID'], 'Legacy request ID and trace ID diverged');
    assert(firstPayload.message === 'Legacy request identity must be stable', 'Legacy request body drifted');
    await page.evaluate(() => window.__dispatchResolvers.shift()({ok:true,dispatch:'chat',reply:'done'}));
    await page.waitForFunction(() => !document.querySelector('#homeDispatchBtn').disabled);
    await page.locator('#homeDispatchForm').evaluate((form) => form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
    await page.waitForFunction(() => window.__requests.length === 2);
    const second = await page.evaluate(() => window.__requests[1]);
    assert(first.options.headers['X-QQ-Message-ID'] !== second.options.headers['X-QQ-Message-ID'], 'a later deliberate submit reused the prior request ID');
    process.stdout.write(JSON.stringify({
      scenarios: ['legacy-workbench-header-and-double-submit-lock', 'new-submit-gets-new-id'],
      requestCount: await page.evaluate(() => window.__requests.length),
      chromePath,
    }));
  } finally {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}

main().catch((error) => { console.error(error.stack || error); process.exitCode = 1; });
