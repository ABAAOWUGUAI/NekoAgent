# 小菲 Starter Pack

这是公开、可选的中文助手模板。它包含一份 Persona、一个 PetPack 和五张可审计表情；不包含也不会读取聊天记录、关系、记忆、任务、学习、模型、渠道或凭据。

模板中的“小菲”只是展示名和表达设定，既不是平台默认品牌，也不是外部角色、真人、官方账号或作品权利主体。

## 使用

启动本项目的 Bridge 后，执行：

```powershell
python tools/install_starter_pack.py --pack-dir starter-packs/xiaofei --base-url http://127.0.0.1:18777 --token-file <你的本地Bridge令牌文件> --apply-to-current
```

安装器会先校验本地素材哈希，再经既有受控 API 导入 PetPack 与表情，并为**当前** Assistant Instance 新建一个 Persona Version。它不会创建或激活另一个 Assistant，也不会触碰任何历史状态。

先预览而不写入：

```powershell
python tools/install_starter_pack.py --pack-dir starter-packs/xiaofei --dry-run
```

## 表情资源

表情为上下文可用资源，不代表每次回复都应发送。是否使用仍由群聊策略、Capability 与 Delivery Gate 决定。
