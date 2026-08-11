/* External local-browser rehearsal for the V4 Artifact migration pattern.
 * Requires Playwright and a local Chromium executable; it is intentionally not
 * part of the dependency-free public test gate. */
const fs = require('fs');
const http = require('http');
const path = require('path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const read = (name) => fs.readFileSync(path.join(root, 'admin', name), 'utf8');
const shellSource = read('v4-shell.js');
const artifactSource = read('v4-artifact-surface.js');
const artifactCss = read('admin-v4-artifact-surface.css');
const chromePath = process.env.V4_BROWSER_EXECUTABLE || 'C:/Program Files/Google/Chrome/Application/chrome.exe';

const fixture = `<!doctype html><html lang="zh-CN"><head><link rel="stylesheet" href="/artifact.css"></head><body>
<section id="loginShell" class="hidden"></section>
<section id="appShell" class="app-shell"><aside class="sidebar"><div class="brand"></div><button id="logoutBtn" type="button">Logout</button></aside><main class="main"><header class="topbar"><div><h1 id="viewTitle" tabindex="-1">Overview</h1></div><div class="toolbar"><button id="quickTaskBtn" type="button">New</button></div></header><div id="contentViewport"><section id="view-overview" class="view"></section><section id="view-tasks" class="view hidden"></section><section id="view-projects" class="view hidden"></section><section id="view-automations" class="view hidden"></section><section id="view-artifacts" class="view hidden">Legacy Artifact Center</section><section id="view-brain" class="view hidden"></section><section id="view-assistant" class="view hidden"></section><section id="view-qq" class="view hidden"></section><section id="view-models" class="view hidden"></section><section id="view-capabilities" class="view hidden"></section><section id="view-proxy" class="view hidden"></section><section id="view-services" class="view hidden"></section><section id="view-logs" class="view hidden"></section><section id="view-relationship" class="view hidden"></section><section id="view-social" class="view hidden"></section><section id="view-growth" class="view hidden"></section><section id="view-settings" class="view hidden"></section></div></main></section>
<script>
window.__calls=[]; window.__requests=[]; window.__artifactMode='items';
window.switchView=(view, options)=>{window.__calls.push({view, options});document.querySelectorAll('.view').forEach((node)=>node.classList.toggle('hidden',node.id!==('view-'+view)));if(options?.focusHeading)requestAnimationFrame(()=>document.getElementById('viewTitle').focus());};
window.bridge=async(route)=>{window.__requests.push(route);if(window.__artifactMode==='error')throw new Error('fixture_error');if(window.__artifactMode==='empty')return {items:[]};return {items:[{id:'artifact-a',title:'Artifact A',summary:'Recent product result',kind:'report',source_goal_id:'goal-a',updated_at:'2026-08-11T01:00:00Z',current_version:{version_number:2,state:'available'}}]};};
</script><script defer src="/v4-shell.js"></script><script defer src="/v4-artifact-surface.js"></script></body></html>`;

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function main() {
  const server = http.createServer((request, response) => {
    if (request.url.startsWith('/v4-shell.js')) return response.end(shellSource);
    if (request.url.startsWith('/v4-artifact-surface.js')) return response.end(artifactSource);
    if (request.url.startsWith('/artifact.css')) return response.end(artifactCss);
    response.setHeader('content-type', 'text/html; charset=utf-8');
    response.end(fixture);
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({headless: true, executablePath: chromePath});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 900}});
    await page.goto(`http://127.0.0.1:${server.address().port}/?experience=v4`, {waitUntil: 'domcontentloaded'});
    const sidebar = (route) => page.locator(`#v4ShellSidebar [data-v4-route="${route}"]`).click();
    const selected = () => page.evaluate(() => document.querySelector('[data-v4-route][aria-current="page"]')?.dataset.v4Route || '');
    const daily = () => page.evaluate(() => ({
      hidden: document.querySelector('#v4ArtifactDaily').hidden,
      inert: document.querySelector('#v4ArtifactDaily').inert,
      display: getComputedStyle(document.querySelector('#v4ArtifactDaily')).display,
      legacyDisplay: getComputedStyle(document.querySelector('#view-artifacts')).display,
      requests: window.__requests.length,
      mountedRoots: document.querySelectorAll('#v4ArtifactDaily').length,
      focused: document.activeElement?.id || '',
      legacyMode: document.body.hasAttribute('data-v4-artifact-legacy-mode'),
    }));

    // A: lifecycle on disable, including CSS and inert safety boundaries.
    await sidebar('artifact');
    await page.waitForFunction(() => document.querySelector('#v4ArtifactDaily')?.textContent.includes('Artifact A'));
    assert(await page.locator('#v4ArtifactDaily').textContent().then((text) => text.includes('已就绪') && text.includes('已关联工作目标')), 'daily item omitted the real status or source-work linkage');
    await page.locator('#v4ReturnLegacyBtn').click();
    await page.waitForFunction(() => document.activeElement?.id === 'viewTitle');
    const afterDisable = await daily();
    assert(afterDisable.hidden && afterDisable.inert && afterDisable.display === 'none' && afterDisable.legacyDisplay !== 'none' && afterDisable.mountedRoots === 1 && afterDisable.focused === 'viewTitle', `disable leakage or focus regression: ${JSON.stringify(afterDisable)}`);

    // B: re-enable and owner changes do not create duplicate data loads.
    await page.locator('#v4ShellToggle').click();
    await sidebar('artifact');
    await page.waitForFunction(() => window.__requests.length === 2);
    await sidebar('work');
    await sidebar('artifact');
    await page.waitForFunction(() => window.__requests.length === 3);
    assert((await selected()) === 'artifact', 'artifact owner selection lost after Work round-trip');

    // C: a real-ish legacy manager refresh must not be mistaken for navigation.
    await page.locator('.v4-artifact-manage').click();
    await page.waitForFunction(() => document.body.hasAttribute('data-v4-artifact-legacy-mode'));
    await page.waitForFunction(() => document.activeElement?.id === 'viewTitle');
    await page.evaluate(() => document.querySelector('#view-artifacts').classList.toggle('legacy-background-refresh'));
    await page.waitForTimeout(60);
    const afterLegacyMutation = await daily();
    assert(
      afterLegacyMutation.hidden
        && afterLegacyMutation.inert
        && afterLegacyMutation.legacyMode
        && afterLegacyMutation.focused === 'viewTitle',
      `legacy manager mutation incorrectly reactivated daily: ${JSON.stringify(afterLegacyMutation)}`,
    );

    // D: Command Palette creates one transition.
    const beforeCommand = await page.evaluate(() => window.__calls.length);
    await page.keyboard.press('Control+k');
    assert(await page.evaluate(() => document.activeElement?.id === 'v4CommandQuery'), 'command palette did not move keyboard focus to its query');
    await page.keyboard.press('Escape');
    await page.waitForTimeout(40);
    assert(await page.evaluate(() => document.activeElement?.id === 'v4CommandTrigger'), 'closing the command palette did not restore focus to its trigger');
    await page.keyboard.press('Control+k');
    await page.locator('#v4CommandPalette [data-v4-route="artifact"]').focus();
    await page.keyboard.press('Enter');
    await page.waitForTimeout(40);
    const afterCommand = await page.evaluate(() => ({calls: window.__calls.slice(), open: document.querySelector('#v4CommandPalette').open, focused: document.activeElement?.id}));
    assert(afterCommand.calls.length === beforeCommand + 1 && afterCommand.calls.at(-1).view === 'artifacts' && !afterCommand.open && afterCommand.focused === 'viewTitle', `duplicate command transition or focus leak: ${JSON.stringify(afterCommand)}`);

    // E/F: legacy paths retain their owner classification.
    await page.evaluate(() => window.switchView('proxy'));
    await page.waitForTimeout(30);
    assert((await selected()) === 'console', 'proxy did not map to Console');
    await page.evaluate(() => window.switchView('projects'));
    await page.waitForTimeout(30);
    assert((await selected()) === 'work', 'projects did not map to Work');

    // G/H: read failure and empty results remain truthful and keep management reachable.
    await sidebar('work');
    await page.evaluate(() => { window.__artifactMode = 'error'; });
    await sidebar('artifact');
    await page.waitForFunction(() => document.querySelector('.v4-artifact-daily-status')?.textContent.includes('读取摘要失败'));
    await page.locator('.v4-artifact-manage').click();
    const afterErrorFallback = await daily();
    assert(afterErrorFallback.hidden && afterErrorFallback.legacyDisplay !== 'none', `error fallback unavailable: ${JSON.stringify(afterErrorFallback)}`);
    await page.evaluate(() => { window.__artifactMode = 'empty'; });
    await sidebar('work');
    await sidebar('artifact');
    await page.waitForFunction(() => document.querySelector('.v4-artifact-empty'));
    const afterEmpty = await page.locator('.v4-artifact-empty').textContent();
    assert(afterEmpty.includes('没有可展示'), `empty state is not truthful: ${afterEmpty}`);

    process.stdout.write(JSON.stringify({
      scenarios: ['A:return-legacy-hidden-inert-visible-heading-focus', 'B:owner-round-trip', 'C:legacy-background-mutation-stays-management', 'D:keyboard-command-and-focus', 'E:proxy-owner', 'F:projects-owner', 'G:error-fallback', 'H:empty'],
      requestCount: await page.evaluate(() => window.__requests.length),
      selected: await selected(),
      chromePath,
    }));
  } finally {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}

main().catch((error) => { console.error(error.stack || error); process.exitCode = 1; });
