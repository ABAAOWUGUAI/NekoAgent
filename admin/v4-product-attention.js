(() => {
  'use strict';

  const asList = (value) => Array.isArray(value) ? value : [];
  const text = (value) => String(value ?? '');
  const routeFor = Object.freeze({
    approval: 'work',
    goal: 'work',
    run: 'work',
    delivery: 'qq',
    business_health: 'console',
  });

  function projectHome(home = {}) {
    const attention = asList(home?.attention?.items).map((item) => {
      const sourceType = text(item?.source_type);
      const sourceId = text(item?.source_id);
      const dedupeKey = text(item?.dedupe_key || `${sourceType}:${sourceId}`);
      return {
        id: `${sourceType}:${sourceId}`,
        dedupeKey,
        sourceType,
        sourceId,
        priority: text(item?.priority || 'normal'),
        title: text(item?.title),
        reason: text(item?.reason),
        risk: text(item?.risk),
        updatedAt: text(item?.updated_at),
        route: routeFor[sourceType] || 'today',
      };
    });
    const actionedSources = new Set(attention.map((item) => item.id));

    return {
      assistantId: text(home?.assistant?.id || 'unknown'),
      attention,
      activeTasks: asList(home?.active_tasks).filter((item) => (
        !actionedSources.has(`${text(item?.source_type)}:${text(item?.source_id)}`)
      )),
    };
  }

  function createSeenStore(storage, assistantId) {
    const key = `nekoagent-v4-attention-seen:${text(assistantId || 'unknown')}`;
    const read = () => {
      try {
        const parsed = JSON.parse(storage?.getItem(key) || '{}');
        return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
      } catch (_) {
        return {};
      }
    };
    const write = (value) => {
      try { storage?.setItem(key, JSON.stringify(value)); } catch (_) { /* local prompts remain optional */ }
    };
    const markSeen = (items) => {
      const next = read();
      asList(items).forEach((item) => {
        const dedupeKey = text(item?.dedupeKey);
        if (dedupeKey) next[dedupeKey] = text(item?.updatedAt);
      });
      write(next);
    };
    return Object.freeze({
      read,
      seed: markSeen,
      markSeen,
      unseen: (items) => {
        const known = read();
        return asList(items).filter((item) => (
          text(item?.dedupeKey) && known[text(item.dedupeKey)] !== text(item?.updatedAt)
        ));
      },
    });
  }

  function freshAttention(previousItems, currentItems) {
    const previous = new Map(asList(previousItems).map((item) => [
      text(item?.dedupeKey), text(item?.updatedAt),
    ]));
    return asList(currentItems).filter((item) => (
      text(item?.dedupeKey) && previous.get(text(item.dedupeKey)) !== text(item?.updatedAt)
    ));
  }

  function browserPrompt(items) {
    return Object.freeze({ title: 'NekoAgent', body: '有新的需要你处理事项。' });
  }

  const api = Object.freeze({ projectHome, createSeenStore, freshAttention, browserPrompt });
  if (typeof window !== 'undefined') window.V4Attention = api;
  if (typeof module !== 'undefined') module.exports = api;
})();
