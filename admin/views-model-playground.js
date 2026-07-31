    // Isolated model validation lab. It never writes QQ or formal conversation state.

    function renderModelPlaygroundOptions(preferredModelId = '') {
      const select = $('modelPlaygroundModel');
      if (!select) return;
      const selected = preferredModelId || select.value;
      const available = state.modelCatalog.filter((item) => Number(item.enabled) && Number(item.provider_enabled));
      select.innerHTML = available.length
        ? available.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label || item.model || item.id)} · ${escapeHtml(item.provider_name || item.provider_id)}</option>`).join('')
        : '<option value="">暂无可验证模型</option>';
      if (available.some((item) => item.id === selected)) {
        select.value = selected;
      } else {
        const providerModel = available.find((item) => item.provider_id === state.selectedModelProvider);
        if (providerModel) select.value = providerModel.id;
      }
    }

    function modelPlaygroundErrorText(result) {
      const labels = {
        waf: '上游安全策略拦截了请求',
        auth: '密钥无效或当前令牌没有权限',
        invalid_model: '模型名称不存在或当前令牌无权使用',
        quota: '额度不足',
        rate_limit: '请求频率受限',
        timeout: '上游响应超时',
        network: '无法连接上游服务',
        upstream: '上游服务异常',
      };
      const label = labels[result.error_kind] || '模型调用失败';
      const guidance = {
        invalid_model: '请从该令牌实际返回的模型列表中重新选择模型名称。',
        upstream: '令牌已被服务接受，但上游暂时不可用；可稍后重试或选择已验证模型。',
        rate_limit: '请稍后重试，或降低该模型的并发请求。',
        quota: '请检查供应商侧配额后再试。',
      };
      const hint = guidance[result.error_kind] || '';
      return `${label}：${result.error || '未知错误'}${hint ? `。${hint}` : ''}`;
    }

    function renderModelPlaygroundResult(result) {
      const ok = Boolean(result.ok);
      $('modelPlaygroundStatus').className = `status-chip ${ok ? 'green' : 'red'}`;
      $('modelPlaygroundStatus').textContent = ok ? '验证通过' : '验证失败';
      $('modelPlaygroundResultTitle').textContent = ok ? '模型已返回结果' : '本次调用失败';
      $('modelPlaygroundReply').textContent = ok
        ? (result.reply || '(空响应)')
        : modelPlaygroundErrorText(result);
      const usage = result.usage || {};
      const tokenText = usage.total_tokens == null
        ? '未上报'
        : `${Number(usage.total_tokens).toLocaleString()}（入 ${Number(usage.prompt_tokens || 0).toLocaleString()} / 出 ${Number(usage.completion_tokens || 0).toLocaleString()}）`;
      const values = [
        result.provider_label || result.provider || '-',
        result.model || '-',
        result.duration == null ? '-' : `${result.duration}s`,
        tokenText,
      ];
      $('modelPlaygroundMetrics').querySelectorAll('dd').forEach((node, index) => {
        node.textContent = values[index] || '-';
      });
    }

    async function runModelPlayground(event) {
      event.preventDefault();
      const button = $('runModelPlaygroundBtn');
      const modelId = $('modelPlaygroundModel').value;
      if (!modelId) {
        setConnection('请先添加并启用一个模型。', 'error');
        return;
      }
      button.disabled = true;
      button.textContent = '正在验证…';
      $('modelPlaygroundStatus').className = 'status-chip amber';
      $('modelPlaygroundStatus').textContent = '运行中';
      $('modelPlaygroundResultTitle').textContent = '正在等待模型响应';
      try {
        const result = await bridge('/assistant/models/playground', {
          method: 'POST',
          body: JSON.stringify({
            model_id: modelId,
            system_prompt: $('modelPlaygroundSystem').value,
            user_prompt: $('modelPlaygroundPrompt').value,
            temperature: Number($('modelPlaygroundTemperature').value || 0.7),
            max_tokens: Number($('modelPlaygroundMaxTokens').value || 900),
          }),
        });
        renderModelPlaygroundResult(result);
        setConnection(result.ok ? '模型验证完成。' : '模型返回失败。', result.ok ? 'ok' : 'error');
      } catch (error) {
        renderModelPlaygroundResult({ ok: false, error: error.message || String(error), error_kind: 'request' });
        setConnection(error.message || String(error), 'error');
      } finally {
        button.disabled = false;
        button.textContent = '运行验证';
      }
    }

    function bindModelPlaygroundEvents() {
      $('modelPlaygroundForm')?.addEventListener('submit', runModelPlayground);
    }
