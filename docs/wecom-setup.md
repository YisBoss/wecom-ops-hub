# 企业微信后台配置指南

本文件只讲**在企微管理端要做什么**，不讲代码侧。代码侧见 [README](../README.md) 和 [部署文档](deploy.md)。

## 一、创建自建应用

1. 登录 [企业微信管理端](https://work.weixin.qq.com/)（需要管理员权限）
2. **应用管理** → **应用** → **自建** → **创建应用**
3. 填应用名称、Logo、可见范围（哪些部门/人能用这个应用、能收告警）
4. 创建后进入应用详情页，记下三个值：

| 值 | 在哪 | 对应面板字段 |
|---|---|---|
| 企业 ID（Corp ID） | 「我的企业」页面顶部 | `wecom.corp_id` |
| AgentId | 应用详情页顶部 | `wecom.agent_id` |
| Secret | 应用详情页 → 「Secret」→ 查看 | `wecom.secret` |

> Secret 只在创建时完整显示一次，忘了就得重置。重置后面板要同步更新。

## 二、开启「接收消息」（回调）

1. 应用详情页 → 找到「接收消息」→ 点「设置 API 接收」
2. **URL** 填：`https://<你的公网地址>/wecom/callback`
   - 必须是 HTTPS（企微要求）
   - 公网可达，详见 [部署文档·公网回调链路](deploy.md#五公网回调链路)
3. **Token**：点「随机获取」，记下来
4. **EncodingAESKey**：点「随机获取」，记下来（43 位）
5. 点「保存」—— 企微会向你的 URL 发 GET 验证请求

| 对应面板字段 | 填什么 |
|---|---|
| `wecom.callback_token` | 第 3 步的 Token |
| `wecom.callback_aes_key` | 第 4 步的 EncodingAESKey |

> 保存不报错 = 验签 + 解密 + 返回 echostr 成功，回调链路通了。
> 保存报错见 [部署文档·常见故障](deploy.md#六常见故障)。

## 三、企业可信 IP 白名单（按需）

企微调用 `qyapi.weixin.qq.com` 取 access_token 时会校验**调用方出口 IP**。

1. 查你的服务器出口公网 IP（`curl ifconfig.me`）
2. 管理端 → **管理** → **企业信息** → **企业可信IP**（部分版本在「应用管理」→ 应用详情里）
3. 把你的出口 IP 加进去

如果加不了（例如出口 IP 动态变 / 不想加 / 不在白名单机器上）：

- 在一台白名单内的机器上搭反代，把 `qyapi.weixin.qq.com` 的路径原样转发
- 面板 `wecom.proxy_url` 填反代地址，例如 `https://wx-proxy.example.com`
- 服务端会把 `qyapi` 请求改成 `{proxy_url}/cgi-bin/...`

## 四、应用可见范围与接收人

告警消息只发给「应用可见范围内的人」。

- **可见范围**：应用详情页 → 可见范围 → 加部门/人
- **接收人**：面板 `wecom.touser` 填 userid（多个逗号分隔），或 `@all`（= 应用可见范围内全部）
- **接收部门**：面板 `wecom.toparty` 填部门 id（多个逗号分隔）

> 告警测试：面板「设置」→「测试消息」会向当前 `wecom.touser` 发一条。只发给指定的人，不会 @all（除非你显式填 `@all`）。

## 五、菜单配置

面板「菜单」页可编辑并推送。两种按钮类型：

| 类型 | 字段 | 说明 |
|---|---|---|
| `view` | `url` | 点击在企微内打开网页（需要 `panel.public_url` 已填） |
| `click` | `key` | 触发服务端动作，回一条消息 |

默认模板（面板「一键生成」）：

```
📊 状态      → {public_url}/#/status       (view)
🧪 自检      → {public_url}/#/selftest     (view)
⚙️ 面板      → {public_url}/#/settings     (view)
🔕 静音1小时  → key=SILENCE_1H              (click)
```

`SILENCE_1H` 的 click 事件在 `POST /wecom/callback` 里处理：全局静音 60 分钟，回一条确认消息。

> `panel.public_url` 为空时不要生成 `view` 型按钮（企微会报错），只生成 `click` 型。

## 六、验签/解密机制（理解用，代码已封装）

接收消息的安全机制：

1. **签名校验**：`sha1(sort([token, timestamp, nonce, encrypt]))` 比对 `msg_signature`
2. **AES 解密**：AES-256-CBC，key = `base64decode(EncodingAESKey + "=")`，解出 XML 明文
3. **返回**：GET 验证返回明文 echostr；POST 消息 5 秒内返回 `success`

这部分代码在服务端的 `/wecom/callback` 路由里已实现，你只需保证 Token 和 EncodingAESKey 与企微后台一致。

## 七、检查清单

部署完照着过一遍：

- [ ] 自建应用已创建，corpid / agentid / secret 已填进面板
- [ ] 「接收消息」已开启，保存不报错
- [ ] callback_token / callback_aes_key 已填进面板
- [ ] 企业可信 IP 已加（或配了 `wecom.proxy_url` 反代）
- [ ] 应用可见范围已加接收人
- [ ] 面板「测试消息」发成功
- [ ] 加一个故意填错 URL 的探针，收到告警
- [ ] 改回正确 URL，收到恢复通知
- [ ] 菜单已推送，企微里能看到
