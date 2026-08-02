    let voiceResponsePolicy = null;
    let voiceResponsePolicyLoadedAt = 0;
    let voiceResponseEventsBound = false;
    let voicePackTuning = null;
    let voicePackTuningLoadedAt = 0;
    let voicePackTuningEventsBound = false;

    const VOICE_EMOTION_LABELS = Object.freeze({
      happy: '开心与庆祝',
      sad: '难过',
      tired: '疲惫',
      annoyed: '烦躁或生气',
      playful: '玩笑与活泼互动',
      comfort: '需要安慰',
    });

    function ensureVoiceResponsePolicyForm() {
      let mount = $('voiceResponsePolicyMount');
      if (!mount) {
        const panel = document.createElement('section');
        panel.id = 'voiceResponsePolicyPanel';
        panel.className = 'panel';
        panel.setAttribute('aria-labelledby', 'voiceResponsePolicyTitle');
        panel.setAttribute('aria-busy', 'true');
        panel.innerHTML = '<div id="voiceResponsePolicyMount"></div>';
        document.querySelector('#view-brain details.panel')?.before(panel);
        mount = $('voiceResponsePolicyMount');
      }
      if (!mount || $('voiceResponsePolicyForm')) return;
      const emotions = Object.entries(VOICE_EMOTION_LABELS).map(([value, label]) =>
        `<label><input type="checkbox" value="${value}" data-voice-emotion> ${label}</label>`,
      ).join('');
      mount.innerHTML = `<div class="panel-header"><div><h2 id="voiceResponsePolicyTitle">语音回复</h2><span id="voiceResponsePolicyVersion" class="meta">正在读取策略</span></div>
        <button id="reloadVoiceResponsePolicyBtn" class="secondary" type="button">重新载入</button></div><div class="panel-body">
        <form id="voiceResponsePolicyForm" class="form-stack">
        <div class="voice-response-grid">
          <label for="voiceResponseMode">回复媒介策略<select id="voiceResponseMode" required>
            <option value="text_only">只使用文字</option><option value="explicit_only">仅在明确要求时发语音</option>
            <option value="emotion_auto">情绪表达时可自动发语音</option><option value="always">普通聊天也优先语音</option>
          </select></label>
          <label for="voiceEmotionConfidence">情绪触发最低置信度<input id="voiceEmotionConfidence" type="number" min="0.5" max="1" step="0.01" required></label>
          <label for="voiceAutoCooldown">自动语音冷却（秒）<input id="voiceAutoCooldown" type="number" min="0" max="86400" step="1" required></label>
          <label for="voiceAutoDailyLimit">每天自动语音上限<input id="voiceAutoDailyLimit" type="number" min="0" max="100" step="1" required></label>
        </div>
        <fieldset><legend>允许触发自动语音的情绪</legend><div class="voice-emotion-options">${emotions}</div></fieldset>
        <p class="voice-policy-boundary">自动语音只用于 Owner 私聊的普通聊天；工作状态、代码、命令、日志、链接和哈希保留文字。明确要求语音不占自动语音预算，但仍受 VoicePack、TTS 与 Delivery Gate 约束。</p>
        <div class="button-row"><button id="saveVoiceResponsePolicyBtn" class="primary" type="submit">保存语音策略</button>
          <span id="voiceResponsePolicyStatus" class="provider-status pending" role="status" aria-live="polite">尚未载入。</span></div>
      </form></div>`;
    }

    function ensureVoicePackTuningForm() {
      if ($('voicePackTuningPanel')) return;
      const panel = document.createElement('section');
      panel.id = 'voicePackTuningPanel';
      panel.className = 'panel';
      panel.setAttribute('aria-labelledby', 'voicePackTuningTitle');
      panel.setAttribute('aria-busy', 'true');
      panel.innerHTML = `<div class="panel-header"><div><h2 id="voicePackTuningTitle">VoicePack 声音调校</h2>
        <span id="voicePackTuningVersion" class="meta">正在读取当前 VoicePack</span></div>
        <button id="reloadVoicePackTuningBtn" class="secondary" type="button">重新载入</button></div>
        <div class="panel-body"><form id="voicePackTuningForm" class="form-stack">
        <div class="voice-response-grid">
          <label for="voiceTuningPreset">调校预设<select id="voiceTuningPreset" required></select></label>
          <label for="voiceLengthScale">语速长度<input id="voiceLengthScale" type="number" min="0.75" max="1.5" step="0.01" required><span class="field-help">数值越大越慢。</span></label>
          <label for="voiceNoiseScale">音色变化度<input id="voiceNoiseScale" type="number" min="0" max="1.5" step="0.01" required></label>
          <label for="voiceNoiseWScale">节奏变化度<input id="voiceNoiseWScale" type="number" min="0" max="1.5" step="0.01" required></label>
          <label for="voiceSentenceSilence">句间停顿（秒）<input id="voiceSentenceSilence" type="number" min="0" max="1" step="0.01" required></label>
          <label for="voiceVolume">音量<input id="voiceVolume" type="number" min="0.2" max="2" step="0.05" required></label>
        </div>
        <div class="voice-tuning-toggle"><input id="voiceEmotionVariation" type="checkbox"><label for="voiceEmotionVariation">按本轮情绪对语速、变化度和停顿做小幅调整</label></div>
        <p class="voice-policy-boundary">这些参数保存在当前 Assistant 绑定的 VoicePack 中，不修改 Persona。Piper 调校能改善节奏，但不能把基础模型变成另一位可识别说话者；更换或复刻声线必须通过独立音源授权与 Voice Executor Gate。</p>
        <div class="button-row"><button id="saveVoicePackTuningBtn" class="primary" type="submit">保存为当前默认声音</button>
          <span id="voicePackTuningStatus" class="provider-status pending" role="status" aria-live="polite">尚未载入。</span></div>
        </form></div>`;
      $('voiceResponsePolicyPanel')?.after(panel);
    }

    function renderVoicePackTuning(tuning) {
      voicePackTuning = tuning;
      const select = $('voiceTuningPreset');
      const options = [...(tuning.presets || []), { id: 'custom', label: '自定义' }];
      select.innerHTML = options.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label)}</option>`).join('');
      const synthesis = tuning.synthesis || {};
      select.value = synthesis.preset || 'warm_natural_v1';
      $('voiceLengthScale').value = String(synthesis.length_scale ?? 1.04);
      $('voiceNoiseScale').value = String(synthesis.noise_scale ?? 0.72);
      $('voiceNoiseWScale').value = String(synthesis.noise_w_scale ?? 0.85);
      $('voiceSentenceSilence').value = String(synthesis.sentence_silence ?? 0.16);
      $('voiceVolume').value = String(synthesis.volume ?? 1);
      $('voiceEmotionVariation').checked = synthesis.emotion_variation !== false;
      $('voicePackTuningVersion').textContent = `${tuning.voice_pack_name || '当前 VoicePack'} · ${tuning.engine || ''}`;
      $('voicePackTuningStatus').className = 'provider-status ok';
      $('voicePackTuningStatus').textContent = '声音调校已载入；保存后用于后续新生成的语音。';
    }

    async function loadVoicePackTuning({ force = false } = {}) {
      ensureVoicePackTuningForm();
      bindVoicePackTuningEvents();
      if (!force && voicePackTuning && Date.now() - voicePackTuningLoadedAt < 300000) {
        renderVoicePackTuning(voicePackTuning);
        return voicePackTuning;
      }
      $('voicePackTuningPanel')?.setAttribute('aria-busy', 'true');
      try {
        const result = await bridge('/assistant/voice-pack/tuning');
        renderVoicePackTuning(result.result || {});
        voicePackTuningLoadedAt = Date.now();
        return result.result;
      } catch (error) {
        $('voicePackTuningStatus').className = 'provider-status error';
        $('voicePackTuningStatus').textContent = error.message || String(error);
        throw error;
      } finally {
        $('voicePackTuningPanel')?.setAttribute('aria-busy', 'false');
      }
    }

    function renderVoiceResponsePolicy(policy) {
      voiceResponsePolicy = policy;
      $('voiceResponseMode').value = policy.mode || 'explicit_only';
      $('voiceEmotionConfidence').value = String(policy.min_emotion_confidence ?? 0.72);
      $('voiceAutoCooldown').value = String(policy.cooldown_seconds ?? 300);
      $('voiceAutoDailyLimit').value = String(policy.daily_limit ?? 8);
      const selected = new Set(policy.emotion_kinds || []);
      document.querySelectorAll('[data-voice-emotion]').forEach((input) => {
        input.checked = selected.has(input.value);
      });
      const modeLabel = $('voiceResponseMode').selectedOptions[0]?.textContent || policy.mode;
      $('voiceResponsePolicyVersion').textContent = `策略 v${policy.version} · ${modeLabel}`;
      $('voiceResponsePolicyStatus').className = 'provider-status ok';
      $('voiceResponsePolicyStatus').textContent = '语音回复策略已载入。显式要求优先；自动语音只用于 Owner 私聊中的非工作型回复。';
    }

    async function loadVoiceResponsePolicy({ force = false } = {}) {
      ensureVoiceResponsePolicyForm();
      bindVoiceResponsePolicyEvents();
      if (!force && voiceResponsePolicy && Date.now() - voiceResponsePolicyLoadedAt < 300000) {
        renderVoiceResponsePolicy(voiceResponsePolicy);
        return voiceResponsePolicy;
      }
      $('voiceResponsePolicyPanel')?.setAttribute('aria-busy', 'true');
      try {
        const result = await bridge('/assistant/voice-response-policy');
        renderVoiceResponsePolicy(result.policy || result.result || {});
        voiceResponsePolicyLoadedAt = Date.now();
        await loadVoicePackTuning({ force });
        return result.policy || result.result;
      } catch (error) {
        $('voiceResponsePolicyStatus').className = 'provider-status error';
        $('voiceResponsePolicyStatus').textContent = error.message || String(error);
        throw error;
      } finally {
        $('voiceResponsePolicyPanel')?.setAttribute('aria-busy', 'false');
      }
    }

    async function saveVoiceResponsePolicy(event) {
      event.preventDefault();
      const form = $('voiceResponsePolicyForm');
      if (!form.checkValidity()) {
        form.reportValidity();
        return;
      }
      const emotions = [...document.querySelectorAll('[data-voice-emotion]:checked')].map((item) => item.value);
      if (!emotions.length) {
        $('voiceResponsePolicyStatus').className = 'provider-status error';
        $('voiceResponsePolicyStatus').textContent = '至少选择一种允许触发自动语音的情绪。';
        return;
      }
      const button = $('saveVoiceResponsePolicyBtn');
      button.disabled = true;
      $('voiceResponsePolicyStatus').className = 'provider-status pending';
      $('voiceResponsePolicyStatus').textContent = '正在保存语音回复策略。';
      try {
        const result = await bridge('/assistant/voice-response-policy', {
          method: 'POST',
          body: JSON.stringify({
            mode: $('voiceResponseMode').value,
            emotion_kinds: emotions,
            min_emotion_confidence: Number($('voiceEmotionConfidence').value),
            cooldown_seconds: Number($('voiceAutoCooldown').value),
            daily_limit: Number($('voiceAutoDailyLimit').value),
            expected_version: voiceResponsePolicy?.version,
          }),
        });
        renderVoiceResponsePolicy(result.policy || result.result || {});
        voiceResponsePolicyLoadedAt = Date.now();
      } catch (error) {
        $('voiceResponsePolicyStatus').className = 'provider-status error';
        $('voiceResponsePolicyStatus').textContent = error.message || String(error);
      } finally {
        button.disabled = false;
      }
    }

    function bindVoiceResponsePolicyEvents() {
      if (voiceResponseEventsBound) return;
      voiceResponseEventsBound = true;
      $('voiceResponsePolicyForm')?.addEventListener('submit', saveVoiceResponsePolicy);
      $('reloadVoiceResponsePolicyBtn')?.addEventListener('click', () => {
        loadVoiceResponsePolicy({ force: true }).catch(() => {});
      });
    }

    function applyVoiceTuningPreset() {
      const preset = (voicePackTuning?.presets || []).find((item) => item.id === $('voiceTuningPreset').value);
      if (!preset) return;
      $('voiceLengthScale').value = String(preset.length_scale);
      $('voiceNoiseScale').value = String(preset.noise_scale);
      $('voiceNoiseWScale').value = String(preset.noise_w_scale);
      $('voiceSentenceSilence').value = String(preset.sentence_silence);
      $('voiceVolume').value = String(preset.volume);
    }

    async function saveVoicePackTuning(event) {
      event.preventDefault();
      const form = $('voicePackTuningForm');
      if (!form.checkValidity()) {
        form.reportValidity();
        return;
      }
      const button = $('saveVoicePackTuningBtn');
      button.disabled = true;
      $('voicePackTuningStatus').className = 'provider-status pending';
      $('voicePackTuningStatus').textContent = '正在保存当前 VoicePack 默认调校。';
      try {
        const result = await bridge('/assistant/voice-pack/tuning', {
          method: 'POST',
          body: JSON.stringify({
            expected_updated_at: voicePackTuning?.updated_at,
            synthesis: {
              preset: $('voiceTuningPreset').value,
              length_scale: Number($('voiceLengthScale').value),
              noise_scale: Number($('voiceNoiseScale').value),
              noise_w_scale: Number($('voiceNoiseWScale').value),
              sentence_silence: Number($('voiceSentenceSilence').value),
              volume: Number($('voiceVolume').value),
              emotion_variation: $('voiceEmotionVariation').checked,
            },
          }),
        });
        renderVoicePackTuning(result.result || {});
        voicePackTuningLoadedAt = Date.now();
      } catch (error) {
        $('voicePackTuningStatus').className = 'provider-status error';
        $('voicePackTuningStatus').textContent = error.message || String(error);
      } finally {
        button.disabled = false;
      }
    }

    function bindVoicePackTuningEvents() {
      if (voicePackTuningEventsBound) return;
      voicePackTuningEventsBound = true;
      $('voicePackTuningForm')?.addEventListener('submit', saveVoicePackTuning);
      $('reloadVoicePackTuningBtn')?.addEventListener('click', () => {
        loadVoicePackTuning({ force: true }).catch(() => {});
      });
      $('voiceTuningPreset')?.addEventListener('change', applyVoiceTuningPreset);
      ['voiceLengthScale', 'voiceNoiseScale', 'voiceNoiseWScale', 'voiceSentenceSilence', 'voiceVolume'].forEach((id) => {
        $(id)?.addEventListener('input', () => { $('voiceTuningPreset').value = 'custom'; });
      });
    }
