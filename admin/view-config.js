(() => {
  'use strict';

  window.AdminViewConfig = Object.freeze({
    freshness: Object.freeze({
      overview: 30000,
      tasks: 15000,
      artifacts: 60000,
      automations: 60000,
      projects: 120000,
      assistant: 300000,
      brain: 300000,
      relationship: 120000,
      social: 60000,
      models: 300000,
      capabilities: 300000,
      proxy: 60000,
      services: 30000,
      qq: 60000,
      logs: 60000,
      settings: 600000,
      growth: 30000,
    }),
    assets: Object.freeze({
      artifacts: ['admin-artifacts.css', 'views-artifacts.js'],
      automations: ['views-automation.js'],
      projects: ['admin-projects.css', 'views-workspace.js', 'views-projects.js'],
      assistant: ['views-workspace.js', 'views-persona.js', 'admin-knowledge.css', 'views-knowledge.js'],
      brain: ['admin-persona.css', 'admin-projects.css', 'views-workspace.js', 'views-persona.js', 'views-projects.js'],
      relationship: ['admin-gate8.css', 'views-gate8.js', 'admin-social-virtual.css', 'views-social-virtual.js'],
      social: ['views-workspace.js', 'views-persona.js', 'views-catalog.js'],
      models: ['views-model-playground.js', 'model-validation-diagnostics.js', 'model-discovery-validation-state.js', 'views-models.js', 'admin-gate8.css', 'views-gate8.js'],
      capabilities: ['views-catalog.js', 'components/network-policy.js'],
      growth: ['views-learning.js'],
      proxy: ['views-infrastructure.js'],
      services: ['views-infrastructure.js', 'admin-gate8.css', 'views-gate8.js'],
      qq: ['views-infrastructure.js', 'admin-qq-access.css', 'components/qq-access-editor.js'],
      settings: ['views-pets.js'],
    }),
  });
})();
