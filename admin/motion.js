(() => {
  'use strict';

  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const animeApi = window.anime;
  const running = new Set();
  const motionTargets = [
    ':scope > .panel',
    ':scope > .catalog-workspace',
    ':scope > .workbench-layout',
    ':scope > .assistant-home-layout',
    ':scope > .content-grid',
    ':scope > section',
  ].join(', ');

  document.documentElement.classList.add('motion-ready');

  function cancelRunning() {
    running.forEach((animation) => {
      try {
        if (typeof animation.revert === 'function') animation.revert();
        else if (typeof animation.cancel === 'function') animation.cancel();
      } catch (_error) {
        // Cancellation is best-effort during navigation and tab hiding.
      }
    });
    running.clear();
  }

  function setEngine(value) {
    document.documentElement.dataset.motionEngine = value;
  }

  function canUseAnime() {
    return !reducedMotion.matches
      && typeof animeApi?.animate === 'function'
      && typeof animeApi?.stagger === 'function';
  }

  function canUseWaapi() {
    return !reducedMotion.matches && typeof Element.prototype.animate === 'function';
  }

  function visibleTargets(root) {
    if (!(root instanceof Element)) return [];
    const explicit = Array.from(root.querySelectorAll('[data-motion-item]'));
    const candidates = explicit.length ? explicit : Array.from(root.querySelectorAll(motionTargets));
    return candidates
      .filter((node) => !node.hidden && !node.classList.contains('hidden'))
      .slice(0, 10);
  }

  function trackWaapi(animation) {
    running.add(animation);
    animation.finished
      .catch(() => {})
      .finally(() => running.delete(animation));
  }

  function enterView(root) {
    cancelRunning();
    if (reducedMotion.matches) {
      setEngine('none');
      return;
    }

    const heading = document.getElementById('viewTitle');
    const targets = [heading, ...visibleTargets(root)].filter(Boolean);
    if (!targets.length) {
      setEngine('none');
      return;
    }

    if (canUseAnime()) {
      setEngine('animejs');
      const animation = animeApi.animate(targets, {
        y: { from: 8, to: 0 },
        duration: 220,
        delay: animeApi.stagger(18),
        ease: 'out(3)',
        onComplete: (self) => {
          running.delete(self);
          if (typeof animeApi.cleanInlineStyles === 'function') animeApi.cleanInlineStyles(self);
        },
      });
      running.add(animation);
      return;
    }

    if (!canUseWaapi()) {
      setEngine('none');
      return;
    }

    setEngine('waapi');
    targets.forEach((node, index) => {
      const animation = node.animate(
        [
          { transform: 'translateY(8px)' },
          { transform: 'translateY(0)' },
        ],
        {
          duration: index === 0 ? 160 : 220,
          delay: Math.min(index * 18, 144),
          easing: 'cubic-bezier(.2, .8, .2, 1)',
          fill: 'both',
        },
      );
      trackWaapi(animation);
    });
  }

  function handleMotionPreference() {
    if (reducedMotion.matches) {
      cancelRunning();
      setEngine('none');
    }
  }

  function confirmStatus(node) {
    if (!(node instanceof Element) || reducedMotion.matches) return;
    if (canUseAnime()) {
      const animation = animeApi.animate(node, {
        opacity: { from: .45, to: 1 },
        y: { from: 4, to: 0 },
        duration: 180,
        ease: 'out(3)',
        onComplete: (self) => {
          running.delete(self);
          if (typeof animeApi.cleanInlineStyles === 'function') animeApi.cleanInlineStyles(self);
        },
      });
      running.add(animation);
      return;
    }
    if (!canUseWaapi()) return;
    const animation = node.animate(
      [{ opacity: .45, transform: 'translateY(4px)' }, { opacity: 1, transform: 'translateY(0)' }],
      { duration: 180, easing: 'cubic-bezier(.2, .8, .2, 1)' },
    );
    trackWaapi(animation);
  }

  if (typeof reducedMotion.addEventListener === 'function') {
    reducedMotion.addEventListener('change', handleMotionPreference);
  } else if (typeof reducedMotion.addListener === 'function') {
    reducedMotion.addListener(handleMotionPreference);
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) cancelRunning();
  });

  setEngine(reducedMotion.matches ? 'none' : (canUseAnime() ? 'animejs' : (canUseWaapi() ? 'waapi' : 'none')));
  window.AdminMotion = Object.freeze({
    enterView,
    confirmStatus,
    cancelRunning,
    getState: () => ({ engine: document.documentElement.dataset.motionEngine, running: running.size }),
  });
})();
