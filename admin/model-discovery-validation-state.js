(() => {
  'use strict';

  // 目录只是一轮读取到的候选项；本页状态不持久化、不自动绑定角色。
  const recordsByModel = new Map();

  function formatTime(value) {
    const date = new Date(value || Date.now());
    if (Number.isNaN(date.getTime())) return '刚刚';
    return date.toLocaleString('zh-CN', { hour12: false });
  }

  function get(model) {
    return recordsByModel.get(String(model || '')) || null;
  }

  function clear() {
    recordsByModel.clear();
  }

  function record(model, result) {
    recordsByModel.set(String(model || ''), {
      ok: Boolean(result?.ok),
      validatedAt: result?.validatedAt || new Date().toISOString(),
      message: String(result?.message || ''),
    });
  }

  function optionSuffix(model) {
    const result = get(model);
    return result ? (result.ok ? ' · 本页已实时验证' : ' · 本页验证未通过') : ' · 未验证';
  }

  function describe(model) {
    const selected = String(model || '');
    const result = get(selected);
    if (!selected) return { tone: '', ok: false, text: '选择候选模型后，才能查看本页实时验证状态。' };
    if (!result) return { tone: '', ok: false, text: `${selected}：本页尚未实时验证，不能填入或标为已验证。` };
    const timestamp = formatTime(result.validatedAt);
    return result.ok
      ? { tone: 'ok', ok: true, text: `${selected}：本页最近一次实时验证通过（服务器时间 ${timestamp}）。结果会随令牌、路由和 Provider 状态变化。` }
      : { tone: 'error', ok: false, text: `${selected}：本页最近一次实时验证未通过（服务器时间 ${timestamp}）。${result.message}` };
  }

  window.modelDiscoveryValidationState = Object.freeze({ clear, describe, formatTime, get, optionSuffix, record });
})();
