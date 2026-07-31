    const petMessages = [
      '状态灯都亮着。要我帮你看哪一段？',
      '代理和模型已经分开管理，不会偷偷换路线。',
      '先给我目标，我会把运行和证据整理好。',
      '今天也要让服务器安安稳稳地跑着。',
    ];
    let petDragState = null;
    let petClickSuppressed = false;
    let petPositionSaveTimer = null;
    let petAnimationTimer = null;
    let petAnimationResetTimer = null;
    let petAnimationFrame = 0;
    let petAnimationState = 'idle';
    let petAnimatedRequests = 0;
    const petMotionPreference = window.matchMedia('(prefers-reduced-motion: reduce)');
    const petStateLabels = {
      idle: '待机', 'running-right': '向右移动', 'running-left': '向左移动',
      waving: '挥手', jumping: '跳跃', failed: '失败反馈', waiting: '等待输入',
      running: '正在工作', review: '检查结果',
    };

    function selectedPetPack() {
      return (state.pet?.packs || []).find((item) => item.id === state.pet.pack_id) || null;
    }

    function animatedPetManifest(pack = selectedPetPack()) {
      const manifest = pack?.manifest || {};
      return manifest.renderer === 'spritesheet' && manifest.atlas && manifest.states?.idle ? manifest : null;
    }

    function petMotionEnabled() {
      return state.pet?.motion === 'auto' && !petMotionPreference.matches;
    }

    function renderPetAnimationFrame() {
      const pack = selectedPetPack();
      const manifest = animatedPetManifest(pack);
      const sprite = $('petCharacterSprite');
      const image = $('petCharacterImage');
      if (!sprite || !image || !pack) return;
      const versionedAsset = `${pack.asset_url}?v=${encodeURIComponent(pack.updated_at || ADMIN_BUILD)}`;
      if (!manifest) {
        sprite.hidden = true;
        image.hidden = false;
        if (image.src !== new URL(versionedAsset, window.location.href).href) image.src = versionedAsset;
        image.alt = `${pack.name}桌面宠物`;
        return;
      }
      image.hidden = true;
      sprite.hidden = false;
      const requested = manifest.states[petAnimationState] ? petAnimationState : (manifest.default_state || 'idle');
      const spec = manifest.states[requested] || manifest.states.idle;
      const atlas = manifest.atlas;
      const frame = petMotionEnabled() ? petAnimationFrame % Number(spec.frames || 1) : 0;
      const x = Number(atlas.columns) <= 1 ? 0 : frame * 100 / (Number(atlas.columns) - 1);
      const y = Number(atlas.rows) <= 1 ? 0 : Number(spec.row || 0) * 100 / (Number(atlas.rows) - 1);
      sprite.style.backgroundImage = `url("${versionedAsset.replaceAll('\\', '\\\\').replaceAll('"', '\\"')}")`;
      sprite.style.backgroundSize = `${Number(atlas.columns) * 100}% ${Number(atlas.rows) * 100}%`;
      sprite.style.setProperty('--pet-frame-x', `${x}%`);
      sprite.style.setProperty('--pet-frame-y', `${y}%`);
      sprite.setAttribute('aria-label', `${pack.name}桌面宠物，${petStateLabels[requested] || requested}`);
      $('desktopPet').dataset.petState = requested;
    }

    function updatePetAccessibleName(displayName = '') {
      const name = String(displayName || '').trim() || '助手';
      $('petCharacterSprite')?.setAttribute('aria-label', `${name}动态桌面宠物`);
      $('petCharacterImage')?.setAttribute('alt', `${name}桌面宠物`);
      $('petCharacterBtn')?.setAttribute('aria-label', `拖动${name}桌面宠物；点击可互动，方向键可微调`);
    }

    window.updatePetAccessibleName = updatePetAccessibleName;
    updatePetAccessibleName(state.assistantSettings?.display_name);

    function schedulePetAnimationFrame() {
      window.clearTimeout(petAnimationTimer);
      renderPetAnimationFrame();
      const manifest = animatedPetManifest();
      if (!manifest || !petMotionEnabled()) return;
      const spec = manifest.states[petAnimationState] || manifest.states.idle;
      petAnimationTimer = window.setTimeout(() => {
        petAnimationFrame = (petAnimationFrame + 1) % Number(spec.frames || 1);
        schedulePetAnimationFrame();
      }, Math.round(1000 / Math.max(1, Number(spec.fps || 4))));
    }

    function setPetAnimationState(nextState, options = {}) {
      const manifest = animatedPetManifest();
      const next = manifest?.states?.[nextState] ? nextState : 'idle';
      if (petAnimationState !== next) petAnimationFrame = 0;
      petAnimationState = next;
      window.clearTimeout(petAnimationResetTimer);
      schedulePetAnimationFrame();
      if (Number(options.duration) > 0 && next !== 'idle') {
        petAnimationResetTimer = window.setTimeout(() => setPetAnimationState('idle'), Number(options.duration));
      }
    }

    window.setPetAnimationState = setPetAnimationState;

    window.shouldAnimatePetRequest = (path, options = {}) => {
      const method = String(options.method || 'GET').toUpperCase();
      return method !== 'GET' || /\/(workbench|playground|execute|sync|restart|tasks)(?:\/|$)/.test(String(path));
    };
    window.setPetRequestActivity = (phase) => {
      if (phase === 'start') {
        petAnimatedRequests += 1;
        setPetAnimationState('running');
        return;
      }
      petAnimatedRequests = Math.max(0, petAnimatedRequests - 1);
      if (phase === 'failed') {
        petAnimatedRequests = 0;
        setPetAnimationState('failed', { duration: 2800 });
      } else if (!petAnimatedRequests) {
        setPetAnimationState('review', { duration: 1600 });
      }
    };
    window.setPetFeedback = (kind, text) => {
      if (kind === 'error') setPetAnimationState('failed', { duration: 2600 });
      else if (/请输入|等待|确认|扫码/.test(String(text || ''))) setPetAnimationState('waiting', { duration: 2600 });
    };

    function applyDesktopPet() {
      const pet = state.pet || {};
      const pack = selectedPetPack();
      const root = $('desktopPet');
      // Appearance is optional Assistant state, not a permanent control-plane
      // overlay. Keep it on the calm home and its own settings surface only.
      const visible = Boolean(
        state.authenticated
        && pet.enabled
        && pack
        && ['overview', 'settings'].includes(state.activeView),
      );
      root.hidden = !visible;
      if (!visible) return;
      root.classList.toggle('dock-left', pet.dock === 'bottom-left');
      root.classList.toggle('dock-free', pet.dock === 'free');
      root.classList.toggle('bubble-right', pet.dock === 'bottom-left');
      root.classList.toggle('motion-reduced', pet.motion === 'reduced');
      root.classList.toggle('motion-off', pet.motion === 'off');
      root.style.setProperty('--pet-scale', String(pet.scale || 1));
      if (pet.dock !== 'free') {
        root.style.removeProperty('left');
        root.style.removeProperty('top');
      } else {
        window.requestAnimationFrame(() => positionPetFromState());
      }
      schedulePetAnimationFrame();
    }

    function renderPetState(pet, message = '桌宠设置已加载。', kind = 'ok') {
      state.pet = pet || { enabled: false, packs: [] };
      const packs = state.pet.packs || [];
      $('petPackSelect').innerHTML = packs.map((item) => (
        `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}${item.preinstalled_source ? ' · 预装来源' : ''}</option>`
      )).join('');
      $('petPackSelect').value = state.pet.pack_id || '';
      $('petEnabled').checked = Boolean(state.pet.enabled);
      $('petScale').value = String(state.pet.scale || 1);
      $('petScaleValue').textContent = Number(state.pet.scale || 1).toFixed(2);
      $('petDock').value = state.pet.dock || 'bottom-right';
      $('petMotion').value = state.pet.motion || 'auto';
      $('petStatus').textContent = message;
      $('petStatus').className = `provider-status ${kind}`.trim();
      $('petPackList').innerHTML = packs.length ? packs.map((item) => `
        <div class="settings-row pet-pack-row">
          <div><strong>${escapeHtml(item.name)}</strong><p>${escapeHtml(item.author || '未知作者')} · ${escapeHtml(item.license || '未声明授权')} · ${item.manifest?.renderer === 'spritesheet' ? `${Object.keys(item.manifest.states || {}).length} 状态动画` : '图片/GIF'}</p></div>
          <div class="inline-actions">
            ${item.preinstalled_source ? '<span class="status-chip green">预装来源</span>' : ''}
            ${item.deletable
              ? `<button class="danger" type="button" data-delete-pet="${escapeHtml(item.id)}" data-delete-pet-name="${escapeHtml(item.name)}" aria-label="删除形象包 ${escapeHtml(item.name)}">删除</button>`
              : '<span class="status-chip">当前版本受保护</span>'}
          </div>
        </div>
      `).join('') : '<div class="empty">暂无 PetPack。</div>';
      applyDesktopPet();
    }

    window.applyDesktopPet = applyDesktopPet;

    async function loadPetState() {
      if (!state.authenticated) return;
      const result = await bridge('/assistant/pets');
      renderPetState(result.pet || {}, '桌宠设置已加载。', 'ok');
    }

    async function savePetSettings(overrides = {}, options = {}) {
      const payload = {
        enabled: $('petEnabled').checked,
        pack_id: $('petPackSelect').value,
        scale: Number($('petScale').value || 1),
        dock: $('petDock').value,
        motion: $('petMotion').value,
        position_x: Number(state.pet?.position_x ?? 0.82),
        position_y: Number(state.pet?.position_y ?? 0.72),
        ...overrides,
      };
      $('savePetSettingsBtn').disabled = true;
      try {
        const result = await bridge('/assistant/pets/settings', { method: 'POST', body: JSON.stringify(payload) });
        renderPetState(result.pet, '桌宠设置已保存。', 'ok');
        if (options.celebrate) setPetAnimationState('jumping', { duration: 1500 });
      } finally {
        $('savePetSettingsBtn').disabled = false;
      }
    }

    function fileAsBase64(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.addEventListener('load', () => resolve(String(reader.result || '').split(',', 2)[1] || ''), { once: true });
        reader.addEventListener('error', () => reject(new Error('读取桌宠资源失败。')), { once: true });
        reader.readAsDataURL(file);
      });
    }

    function fileAsText(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.addEventListener('load', () => resolve(String(reader.result || '')), { once: true });
        reader.addEventListener('error', () => reject(new Error('读取 PetPack manifest 失败。')), { once: true });
        reader.readAsText(file, 'utf-8');
      });
    }

    async function importPetPack() {
      const file = $('petImportFile').files?.[0];
      if (!file) throw new Error('请选择 PNG、WebP 或 GIF 文件。');
      if (file.size > 5 * 1024 * 1024) throw new Error('桌宠资源不能超过 5 MB。');
      const manifestFile = $('petImportManifest').files?.[0];
      let manifest = null;
      if (manifestFile) {
        try {
          manifest = JSON.parse(await fileAsText(manifestFile));
        } catch (error) {
          throw new Error('PetPack manifest 不是有效 JSON。');
        }
      }
      const result = await bridge('/assistant/pets/import', {
        method: 'POST',
        body: JSON.stringify({
          name: $('petImportName').value.trim(),
          author: $('petImportAuthor').value.trim(),
          license: $('petImportLicense').value.trim(),
          mime_type: file.type,
          asset_base64: await fileAsBase64(file),
          manifest,
        }),
      });
      const pet = result.result?.state || result.result;
      renderPetState(pet, '自定义 PetPack 已验证并导入。', 'ok');
      $('petImportFile').value = '';
      $('petImportManifest').value = '';
    }

    async function deletePetPack(packId, packName = '这个形象包') {
      const unbind = state.pet?.pack_id === packId;
      const prompt = unbind
        ? `${packName} 当前正在使用。删除后会解除当前助手的形象绑定并关闭桌宠显示，确认永久删除吗？`
        : `确认从你的形象资源中永久删除 ${packName} 吗？`;
      if (!window.confirm(prompt)) return;
      const result = await bridge('/assistant/pets/delete', {
        method: 'POST', body: JSON.stringify({
          pack_id: packId,
          confirm: true,
          unbind,
        }),
      });
      renderPetState(result.pet, `${packName} 已删除。`, 'ok');
    }

    function positionPetFromState() {
      const root = $('desktopPet');
      if (!root || root.hidden || state.pet?.dock !== 'free') return;
      const width = root.offsetWidth;
      const height = root.offsetHeight;
      const maxLeft = Math.max(8, window.innerWidth - width - 8);
      const maxTop = Math.max(8, window.innerHeight - height - 8);
      const x = Math.max(0, Math.min(Number(state.pet.position_x ?? 0.82), 1));
      const y = Math.max(0, Math.min(Number(state.pet.position_y ?? 0.72), 1));
      const left = Math.round(8 + x * (maxLeft - 8));
      root.style.left = `${left}px`;
      root.style.top = `${Math.round(8 + y * (maxTop - 8))}px`;
      root.classList.toggle('bubble-right', left < 230);
    }

    function movePetTo(left, top) {
      const root = $('desktopPet');
      const maxLeft = Math.max(8, window.innerWidth - root.offsetWidth - 8);
      const maxTop = Math.max(8, window.innerHeight - root.offsetHeight - 8);
      const clampedLeft = Math.max(8, Math.min(left, maxLeft));
      const clampedTop = Math.max(8, Math.min(top, maxTop));
      state.pet.dock = 'free';
      state.pet.position_x = maxLeft === 8 ? 0 : (clampedLeft - 8) / (maxLeft - 8);
      state.pet.position_y = maxTop === 8 ? 0 : (clampedTop - 8) / (maxTop - 8);
      root.classList.add('dock-free');
      root.classList.remove('dock-left');
      root.classList.toggle('bubble-right', clampedLeft < 230);
      root.style.left = `${Math.round(clampedLeft)}px`;
      root.style.top = `${Math.round(clampedTop)}px`;
      $('petDock').value = 'free';
    }

    function schedulePetPositionSave() {
      window.clearTimeout(petPositionSaveTimer);
      petPositionSaveTimer = window.setTimeout(() => {
        savePetSettings({
          dock: 'free',
          position_x: state.pet.position_x,
          position_y: state.pet.position_y,
        }).catch((error) => setConnection(error.message || String(error), 'error'));
      }, 280);
    }

    function startPetDrag(event) {
      if (event.button !== 0) return;
      const root = $('desktopPet');
      const rect = root.getBoundingClientRect();
      petDragState = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        left: rect.left,
        top: rect.top,
        moved: false,
      };
      $('petCharacterBtn').setPointerCapture(event.pointerId);
      root.classList.add('dragging');
    }

    function dragPet(event) {
      if (!petDragState || event.pointerId !== petDragState.pointerId) return;
      const deltaX = event.clientX - petDragState.startX;
      const deltaY = event.clientY - petDragState.startY;
      if (Math.abs(deltaX) + Math.abs(deltaY) > 5) petDragState.moved = true;
      if (!petDragState.moved) return;
      event.preventDefault();
      if (Math.abs(deltaX) >= 2) setPetAnimationState(deltaX >= 0 ? 'running-right' : 'running-left');
      movePetTo(petDragState.left + deltaX, petDragState.top + deltaY);
    }

    function finishPetDrag(event) {
      if (!petDragState || event.pointerId !== petDragState.pointerId) return;
      const moved = petDragState.moved;
      petDragState = null;
      $('desktopPet').classList.remove('dragging');
      try { $('petCharacterBtn').releasePointerCapture(event.pointerId); } catch (_error) { /* pointer already released */ }
      if (moved) {
        petClickSuppressed = true;
        window.setTimeout(() => { petClickSuppressed = false; }, 0);
        schedulePetPositionSave();
      }
      setPetAnimationState('idle');
    }

    function movePetWithKeyboard(event) {
      if (event.key === 'Home') {
        event.preventDefault();
        savePetSettings({ dock: 'bottom-right', position_x: 0.82, position_y: 0.72 })
          .catch((error) => setConnection(error.message || String(error), 'error'));
        return;
      }
      const deltas = { ArrowUp: [0, -10], ArrowDown: [0, 10], ArrowLeft: [-10, 0], ArrowRight: [10, 0] };
      const delta = deltas[event.key];
      if (!delta) return;
      event.preventDefault();
      const rect = $('desktopPet').getBoundingClientRect();
      setPetAnimationState(delta[0] < 0 ? 'running-left' : delta[0] > 0 ? 'running-right' : 'jumping', { duration: 700 });
      movePetTo(rect.left + delta[0], rect.top + delta[1]);
      schedulePetPositionSave();
    }

    function bindPetEvents() {
      $('petScale').addEventListener('input', () => {
        $('petScaleValue').textContent = Number($('petScale').value || 1).toFixed(2);
      });
      $('savePetSettingsBtn').addEventListener('click', () => savePetSettings({}, { celebrate: true }).catch((error) => {
        $('petStatus').textContent = error.message || String(error);
        $('petStatus').className = 'provider-status error';
      }));
      $('importPetPackBtn').addEventListener('click', () => importPetPack().catch((error) => {
        $('petStatus').textContent = error.message || String(error);
        $('petStatus').className = 'provider-status error';
      }));
      $('petPackList').addEventListener('click', (event) => {
        const button = event.target.closest('[data-delete-pet]');
        if (button) deletePetPack(
          button.dataset.deletePet,
          button.dataset.deletePetName,
        ).catch((error) => setConnection(error.message || String(error), 'error'));
      });
      $('resetPetPositionBtn').addEventListener('click', () => savePetSettings({
        dock: 'bottom-right', position_x: 0.82, position_y: 0.72,
      }).catch((error) => setConnection(error.message || String(error), 'error')));
      $('petCharacterBtn').addEventListener('pointerdown', startPetDrag);
      $('petCharacterImage').addEventListener('dragstart', (event) => event.preventDefault());
      window.addEventListener('pointermove', dragPet, { passive: false });
      window.addEventListener('pointerup', finishPetDrag);
      window.addEventListener('pointercancel', finishPetDrag);
      $('petCharacterBtn').addEventListener('keydown', movePetWithKeyboard);
      $('petCharacterBtn').addEventListener('click', () => {
        if (petClickSuppressed) {
          petClickSuppressed = false;
          return;
        }
        const bubble = $('petBubble');
        setPetAnimationState('waving', { duration: 2200 });
        bubble.textContent = petMessages[Math.floor(Math.random() * petMessages.length)];
        bubble.hidden = false;
        window.setTimeout(() => { bubble.hidden = true; }, 5000);
      });
      $('petCloseBtn').addEventListener('click', () => savePetSettings({ enabled: false }).catch((error) => setConnection(error.message || String(error), 'error')));
      window.addEventListener('resize', () => window.requestAnimationFrame(positionPetFromState));
      petMotionPreference.addEventListener('change', schedulePetAnimationFrame);
    }
