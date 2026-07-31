(() => {
  const discoveryMessages = {
    model_discovery_requires_azure_deployment: 'Azure 连接需要先明确可执行的部署名；资源模型目录不能直接替代部署配置。',
    model_discovery_not_available_for_codex_connection: 'Codex 登录连接不使用 API Key，不能按“地址 + Key”读取模型目录。',
    model_discovery_unsupported_transport: '该连接协议尚未提供安全的模型目录接口。',
    provider_secret_or_url_unavailable: '连接地址或已保存的令牌不可用；请在控制台重新保存该连接后再试。',
  };
  const incompatibleShapes = new Set(['payload_not_object', 'choices_missing', 'choices_empty', 'choice_not_object', 'message_missing', 'content_missing', 'content_unsupported']);

  window.modelDiscoveryFailureMessage = (result) => {
    const reason = result?.error || result?.error_kind || '';
    return discoveryMessages[reason] || reason || '请检查连接。';
  };

  window.modelValidationFailureMessage = (result) => {
    const reason = result?.error || result?.error_kind || '';
    if (reason !== 'empty_provider_reply') return window.modelDiscoveryFailureMessage(result);
    if (result?.reasoning_only) return '连接已响应，但模型只返回了内部推理，没有可发送的最终正文；平台不会把推理内容当作回复。验证已使用 256 token，可检查该模型的最终答案输出设置后重试。';
    if (result?.finish_reason === 'length') return '连接已响应，但在验证输出上限内没有形成最终正文。请在 Provider 侧确认该模型的输出额度或推理额度后重试。';
    if (result?.response_shape === 'content_empty') return 'Provider 已返回 Chat Completions 结果，但 choices[0].message.content 为空；该模型本次没有产生最终正文，不能标为已验证。请改用能输出正文的模型，或请 Provider 修复该模型的兼容层。';
    if (incompatibleShapes.has(result?.response_shape)) return '连接返回的数据不是此 OpenAI Chat Completions 验证器可识别的最终文本结构。请确认该模型实际走的是 /v1/chat/completions，且响应包含 choices[0].message.content。';
    return '连接已响应，但没有取得可发送的最终正文。请重试；若持续出现，请检查该模型是否被 Provider 配置为只输出推理。';
  };
})();
