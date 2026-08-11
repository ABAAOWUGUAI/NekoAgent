from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import admin_console


class V4ShellPhase1Tests(unittest.TestCase):
    def _routing_contract(self) -> dict[str, object]:
        script = (
            "const contract = require(process.argv[1]);"
            "process.stdout.write(JSON.stringify(contract));"
        )
        result = subprocess.run(
            ["node", "-e", script, str(ROOT / "admin" / "v4-shell.js")],
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8',
        )
        return json.loads(result.stdout)

    def test_v4_assets_are_versioned_opt_in_resources(self):
        self.assertIn('/admin/static/admin-v4-shell.css?v=', admin_console.ADMIN_HTML)
        self.assertIn('/admin/static/v4-shell.js?v=', admin_console.ADMIN_HTML)
        self.assertIsNotNone(admin_console.admin_asset('admin-v4-shell.css'))
        self.assertIsNotNone(admin_console.admin_asset('v4-shell.js'))

    def test_all_legacy_views_have_one_primary_owner_surface(self):
        contract = self._routing_contract()
        expected = {
            'overview', 'tasks', 'artifacts', 'automations', 'projects', 'assistant',
            'brain', 'relationship', 'social', 'models', 'capabilities', 'proxy',
            'services', 'qq', 'logs', 'settings', 'growth',
        }
        self.assertEqual(expected, set(contract['legacyViewOwners']))
        self.assertEqual(17, len(contract['legacyViewOwners']))
        self.assertEqual('work', contract['legacyViewOwners']['projects'])
        self.assertEqual('console', contract['legacyViewOwners']['proxy'])
        self.assertEqual('assistant', contract['legacyViewOwners']['growth'])
        self.assertEqual('memory', contract['legacyViewOwners']['brain'])

    def test_frozen_owner_surfaces_are_complete_without_restoring_legacy_nav(self):
        contract = self._routing_contract()
        routes = {route['id']: route for route in contract['ownerSurfaces']}
        self.assertEqual(
            {'overview', 'qq', 'chat', 'work', 'artifact', 'memory', 'assistant', 'console', 'settings'},
            set(routes),
        )
        self.assertEqual([], routes['chat']['legacyViews'])
        self.assertEqual('overview', routes['chat']['fallbackLegacyView'])
        self.assertEqual(['tasks', 'projects', 'automations'], routes['work']['legacyViews'])
        self.assertEqual(['models', 'capabilities', 'proxy', 'services', 'logs'], routes['console']['legacyViews'])
        self.assertEqual('partial', routes['artifact']['implementationState'])
        self.assertEqual('partial', routes['chat']['implementationState'])
        self.assertEqual('legacy', routes['qq']['implementationState'])
        self.assertIn('implementationStates', contract)

    def test_shell_has_single_transition_command_navigation_and_no_data_transport(self):
        source = (ROOT / 'admin' / 'v4-shell.js').read_text(encoding='utf-8')
        self.assertIn("createRouteButton(route, 'v4-command-route', { onBeforeNavigate: () => dialog.close(), command: true })", source)
        self.assertIn('explicitTransitionRouteId', source)
        self.assertIn('function trackedSwitchView(view, options)', source)
        self.assertIn('renderSelection({ explicitRouteId, legacyView: currentLegacyView() })', source)
        self.assertNotIn('new MutationObserver', source)
        self.assertIn("button.setAttribute('aria-current', 'page')", source)
        self.assertIn("function ownerForLegacyView(view) { return routeById.get(V4_LEGACY_VIEW_OWNERS[view]) || null; }", source)
        self.assertIn("'nekoagent:v4-experience-disable'", source)
        self.assertIn("'nekoagent:v4-experience-enable'", source)
        self.assertIn('commandNavigationInProgress', source)
        self.assertIn('restoreLegacyFocus', source)
        self.assertIn("command.setAttribute('aria-label', '前往工作区')", source)
        self.assertNotIn('fetch(', source)
        self.assertNotIn('bridge(', source)

    def test_artifact_daily_slice_uses_existing_read_path_and_keeps_full_management(self):
        source = (ROOT / 'admin' / 'v4-artifact-surface.js').read_text(encoding='utf-8')
        css = (ROOT / 'admin' / 'admin-v4-artifact-surface.css').read_text(encoding='utf-8')
        index = (ROOT / 'admin' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('/assistant/artifacts?limit=6&offset=0', source)
        self.assertIn('window.bridge', source)
        self.assertIn('v4-artifact-legacy-mode', source)
        self.assertIn('打开完整成品库', source)
        self.assertIn('source_goal_id', source)
        self.assertIn("available: '已就绪'", source)
        self.assertIn('已关联工作目标', source)
        self.assertNotIn("method: 'POST'", source)
        self.assertIn('admin-v4-artifact-surface.css', index)
        self.assertIn('v4-artifact-surface.js', index)
        self.assertIn('body[data-v4-experience="active"][data-v4-active-view="artifact"]', css)
        self.assertIn('.v4-artifact-daily { display: none; }', css)
        self.assertIn('root.inert = true', source)
        self.assertIn('requestVersion += 1', source)
        self.assertIn("'nekoagent:v4-experience-disable'", source)
        self.assertIsNotNone(admin_console.admin_asset('admin-v4-artifact-surface.css'))
        self.assertIsNotNone(admin_console.admin_asset('v4-artifact-surface.js'))


if __name__ == '__main__':
    unittest.main()
