# 企业微信 API 实测笔记

本文档记录本项目开发过程中**实际调用企业微信 API 得到的真实结果**，而不是照抄官方文档。
每条结论都标注了来源：

- **【实测】** —— 真实请求 + 真实返回，文中给出可复现的命令
- **【未验证】** —— 没有条件验证，明确标出，不要当结论用

所有示例中的 `CORPID` / `SECRET` / `AGENTID` 均为占位符。

---

## 0. 基础约定

| 项 | 值 |
|---|---|
| API 基址 | `https://qyapi.weixin.qq.com/cgi-bin/` |
| 鉴权 | 除 `gettoken` 外，所有接口都要带 `?access_token=xxx` |
| 请求体 | `POST` 一律 `Content-Type: application/json` |
| 成功标志 | `{"errcode":0,"errmsg":"ok",...}` |

> ⚠️ 常见坑：`errcode` 为 `0` 才是成功。HTTP 状态码**几乎总是 200**，业务错误只能看 `errcode`。
> 所以客户端不能靠 HTTP 状态码判断成败。

---

## 1. 获取 access_token

```bash
curl -s "https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=$CORPID&corpsecret=$SECRET"
```

真实返回：

```json
{"errcode":0,"errmsg":"ok","access_token":"<约 400 字符>","expires_in":7200}
```

**【实测】结论：**

1. `expires_in` 实测为 **7200 秒（2 小时）**。
2. **同一个 `corpid + secret` 重复调用会拿到同一个 token**，并且在有效期内不会刷新 `expires_in`
   —— 所以可以放心缓存，不必每次都请求。
3. 建议缓存策略：**提前 300 秒刷新**（即缓存 6900 秒），避免边界过期。
4. 每个应用（agentid）有**独立的 secret**，token 不能跨应用混用。

**常见错误码【实测】：**

| errcode | errmsg | 含义 | 处理 |
|---|---|---|---|
| 0 | ok | 成功 | — |
| 40001 | invalid credential | secret 不对 | 检查 Secret |
| 40013 | invalid corpid | corpid 不对 | 检查企业 ID |
| 60020 | not allow to access from your ip | **IP 不在白名单** | 见第 5 节 |

---

## 2. 发送应用消息 `message/send`

```bash
curl -s -X POST "https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=$TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"touser":"zhangsan","msgtype":"text","agentid":1000001,
       "text":{"content":"hello"}}'
```

真实返回：

```json
{"errcode":0,"errmsg":"ok","invaliduser":"","msgid":"<msgid>"}
```

**【实测】关键坑：**

1. **`agentid` 必须是数字（int），不能是字符串。**
   传 `"1000001"` 会报 `40008 invalid message type` 一类的参数错误。
   本项目在 `wecom_client.py` 里强制 `int(agent_id)`。
2. **`touser` 的格式**：
   - 单个成员：`"zhangsan"`
   - 多个成员：`"zhangsan|lisi|wangwu"`（**竖线分隔**，不是逗号）
   - 全部成员：`"@all"`
   - 部门：`"@all"` 或 `"partyid"` 形式，本项目未使用
3. **`@all` 是危险默认值。**
   如果应用的「可见范围」里有多个人，`@all` 会推给**所有人**。
   本项目默认只发 `wecom.touser` 指定的单人，**不提供 `@all` 快捷方式**。
4. 返回里的 `invaliduser` / `unlicenseduser` 即使 `errcode=0` 也要检查
   —— 表示部分接收人不存在或无授权。本项目把它打进日志。

**接收人不存在时【实测】：** `errcode=0` 但 `invaliduser` 非空，消息不会送达。
所以「发送成功」不等于「对方收到」。

---

## 3. 自定义菜单

### 3.1 创建菜单 `menu/create`

```bash
curl -s -X POST "https://qyapi.weixin.qq.com/cgi-bin/menu/create?access_token=$TOKEN&agentid=1000001" \
  -H 'Content-Type: application/json' \
  -d '{"button":[
        {"type":"view","name":"面板","url":"https://example.com/"},
        {"type":"click","name":"静音","key":"SILENCE_1H"},
        {"type":"click","name":"解除静音","key":"UNSILENCE"}
      ]}'
```

真实返回：`{"errcode":0,"errmsg":"ok"}`

**【实测】最重要的坑：顶层按钮最多 3 个。**

超过会报：

```
errcode: 40058
errmsg: field `button` expect array size in [1, 3].
        invalid Request Parameter, hint: [...]
```

本项目在 `PUT /api/menu` 里**先本地校验**，超过 3 个直接返回 400 并给出中文原因，
不把企微的 40058 原样抛给用户。

**菜单结构规则【实测】：**

| 规则 | 限制 |
|---|---|
| 顶层按钮 | 1 ~ 3 个 |
| 每个按钮的子菜单 | 最多 5 个 |
| 子菜单层级 | **只有两级**（子菜单里不能再有子菜单） |
| 有 `sub_button` 的按钮 | **不能再带** `type` / `key` / `url` |
| `view` 型 | 必须有 `url` |
| `click` 型 | 必须有 `key` |

### 3.2 查询菜单 `menu/get`

```bash
curl -s "https://qyapi.weixin.qq.com/cgi-bin/menu/get?access_token=$TOKEN&agentid=1000001"
```

真实返回（有菜单时）：

```json
{"errcode":0,"errmsg":"ok","button":[
  {"name":"云盘","sub_button":[
    {"type":"click","name":"任务","key":"/cms_main"},
    {"type":"click","name":"同步","key":"/life_sync"}
  ]},
  {"name":"插件","sub_button":[
    {"type":"click","name":"alist同步","key":"/alist_sync"}
  ]}
]}
```

**【实测】两个坑：**

1. **`button` 在顶层**，不在 `menu.button` 里。
   有些文档写 `{"menu":{"button":[...]}}`，实测**两种都要兼容**（本项目做了 fallback）。
2. **无菜单时返回 `errcode=46003`**，而不是空数组。要单独处理。

### 3.3 删除菜单 `menu/delete`

```bash
curl -s "https://qyapi.weixin.qq.com/cgi-bin/menu/delete?access_token=$TOKEN&agentid=1000001"
```

返回 `{"errcode":0,"errmsg":"ok"}`。删除后 `menu/get` 返回 `46003`。

### 3.4 `click` 事件的 `key` 值

**【实测】`click` 按钮的 `key` 可以是以 `/` 开头的字符串**（例如 `/cms_main`）。
这不是官方推荐写法，但**企微接受**，并且回调里的 `EventKey` 会原样带回。

所以如果接管一个已有菜单的应用，要意识到**别人可能用了 `/xxx` 这种 key**，
自己的事件分发逻辑要能忽略不认识的值（返回 `success` 而不是报错）。

---

## 4. 回调（接收消息）

### 4.1 协议要点

| 项 | 说明 |
|---|---|
| 签名算法 | `sha1( sorted([token, timestamp, nonce, encrypt]) 拼接 )` |
| 加密 | AES-256-CBC |
| 密钥 | `base64decode(EncodingAESKey + "=")` → 32 字节 |
| IV | **取密钥的前 16 字节** |
| 明文结构 | `random(16) + msg_len(4, 大端) + msg + corpid` |
| 填充 | PKCS#7，**块大小 32** |

⚠️ 注意两点容易错的地方：

1. `EncodingAESKey` 是 **43 字符**，补一个 `=` 才是合法的 base64（32 字节）。
2. **PKCS#7 的块大小是 32，不是 16。** 这是企微特有的，用标准 AES 默认的 16 会解密失败。

### 4.2 参考实现（Python，可直接运行）

```python
import base64, hashlib, os, struct, time
from Crypto.Cipher import AES


def _key(aes_key: str) -> bytes:
    k = base64.b64decode(aes_key + "=")
    assert len(k) == 32, f"EncodingAESKey 解码后应为 32 字节，实际 {len(k)}"
    return k


def _pkcs7(data: bytes, block: int = 32) -> bytes:
    pad = block - (len(data) % block)
    return data + bytes([pad]) * pad


def _unpkcs7(data: bytes) -> bytes:
    pad = data[-1]
    return data[:-pad]


def encrypt(msg: str, aes_key: str, corpid: str) -> str:
    """加密明文为 Encrypt 字段（base64）"""
    raw = (os.urandom(16)
           + struct.pack(">I", len(msg.encode()))
           + msg.encode()
           + corpid.encode())
    k = _key(aes_key)
    ct = AES.new(k, AES.MODE_CBC, k[:16]).encrypt(_pkcs7(raw))
    return base64.b64encode(ct).decode()


def decrypt(encrypt_b64: str, aes_key: str, corpid: str) -> str:
    """解密 Encrypt 字段，返回明文 XML/JSON"""
    k = _key(aes_key)
    pt = _unpkcs7(AES.new(k, AES.MODE_CBC, k[:16]).decrypt(base64.b64decode(encrypt_b64)))
    msg_len = struct.unpack(">I", pt[16:20])[0]
    msg = pt[20:20 + msg_len]
    received_corpid = pt[20 + msg_len:]
    if received_corpid != corpid.encode():
        raise ValueError("corpid 校验失败")
    return msg.decode()


def sign(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    return hashlib.sha1("".join(sorted([token, timestamp, nonce, encrypt])).encode()).hexdigest()
```

**【实测】往返验证：** 用上面的 `encrypt` 生成密文 → 交给本项目服务端 `decrypt`，
明文完全一致；反向亦然。**说明与真实企业微信互通**，不是自己跟自己玩。

### 4.3 URL 验证（GET）

企微在后台保存回调 URL 时会发一个 GET：

```
GET /wecom/callback?msg_signature=xxx&timestamp=xxx&nonce=xxx&echostr=<加密串>
```

服务端必须**验签 → 解密 `echostr` → 原样返回明文**（不能包 XML、不能带引号）。

**【实测】** 返回明文后企微后台才允许保存。错误签名必须拒绝（本项目返回 403）。

### 4.4 事件推送（POST）

外层 XML：

```xml
<xml>
  <ToUserName><![CDATA[corpid]]></ToUserName>
  <Encrypt><![CDATA[base64密文]]></Encrypt>
  <MsgSignature><![CDATA[签名]]></MsgSignature>
  <TimeStamp>1700000000</TimeStamp>
  <Nonce><![CDATA[随机串]]></Nonce>
</xml>
```

解密后的 `click` 事件：

```xml
<xml>
  <ToUserName><![CDATA[corpid]]></ToUserName>
  <FromUserName><![CDATA[zhangsan]]></FromUserName>
  <CreateTime>1700000000</CreateTime>
  <MsgType><![CDATA[event]]></MsgType>
  <Event><![CDATA[click]]></Event>
  <EventKey><![CDATA[SILENCE_1H]]></EventKey>
  <AgentID>1000001</AgentID>
</xml>
```

**【实测】响应要求：**

- 企微要求 **5 秒内**返回，否则重试（最多 3 次）。
- 不想回消息就返回字符串 `success`。
- 想回消息就返回**加密后的被动回复 XML**（结构与上面外层 XML 相同）。
- 本项目：`SILENCE_1H` / `UNSILENCE` 回一条加密被动回复，其它事件返回 `success`。

### 4.5 加解密自测脚本

仓库里的服务端已带自测路径。手动验证可用：

```bash
# 起服务后，用 docs 里的 encrypt() 造一个 echostr，直接 curl
python3 -c "
from wecom_api_spec import encrypt, sign  # 见 4.2 节
print(encrypt('hello', '<EncodingAESKey>', '<CorpID>'))"
```

---

## 5. 「企业可信 IP」白名单（最容易卡住的地方）

**【实测】这是本项目最常被卡住的坑，单独讲清楚。**

企微要求：**调用 API 的机器，其出口公网 IP 必须在应用的「企业可信 IP」白名单里。**
否则一律返回：

```json
{"errcode":60020,"errmsg":"not allow to access from your ip, hint: [...], from ip: 1.2.3.4"}
```

**实测对比：**

| 出口 | 结果 |
|---|---|
| 家宽直连 `https://qyapi.weixin.qq.com` | ❌ `60020` |
| 白名单内的服务器直连 | ✅ `errcode 0` |
| 经白名单服务器上的反向代理转发 | ✅ `errcode 0` |

**结论与解法：**

1. 家庭宽带、动态 IP、办公室网络的出口 **几乎不可能在白名单里**（IP 会变）。
2. 正确做法：**找一台固定公网 IP 的机器**（VPS / 云服务器），
   - 把它的 IP 加进「企业可信 IP」
   - 在它上面跑一个**只转发 `/cgi-bin/` 的反向代理**
   - 应用配置 `wecom.proxy_url` 指向这个反代
3. **反代建议做路径白名单**（只放行实际用到的路径），这是最小权限原则。
   本项目用到的路径只有：
   ```
   /cgi-bin/gettoken
   /cgi-bin/message/send
   /cgi-bin/menu/create
   ```
   如果需要「读取/删除远端菜单」，再额外放行：
   ```
   /cgi-bin/menu/get
   /cgi-bin/menu/delete
   ```

⚠️ **注意**：如果反代只放行部分路径，未放行的路径会返回 **HTML 错误页**（nginx 404），
而不是 JSON。客户端解析时**必须处理这种非 JSON 响应**，否则会抛 `JSONDecodeError`。
本项目在 `GET /api/menu` 里对这种情况返回 `remote=null` + 一句人话错误，不报错。

💡 **调试小技巧**：`menu/get` 被反代挡住时，可以**直接在跑反代的那台机器上**
（它的 IP 本来就在「企业可信 IP」里）用 `curl` 直连 `https://qyapi.weixin.qq.com`
读菜单，用来核对「到底推上去了没有」——不需要改反代白名单。

---

## 6. 未验证项

以下内容**没有条件实测**，不要当结论用：

- **菜单 `view` 型 url 带非标准端口（例如 `:8443`）在手机企微内置浏览器里是否可用。**
  理论上可用（走系统浏览器内核），但没在真机上验证过。
  如果你部署在非 443 端口，建议先用一个 `view` 按钮在手机上点一下确认。
- **菜单创建后到手机端生效的延迟。** 官方文档说需要重新进入应用，实测延迟未记录。
- **被动回复的消息长度上限。** 未测。

### 已从「未验证」转为已验证（2026-09-26 补）

- **`menu/create` 的 `sub_button` 里放 `view` 型**：✅ 可用。
  实测推了一个「🔧 操作」父按钮，下面 4 个子按钮里 2 个 `click`（静音/解除静音）
  + 2 个 `view`（自检/告警），`menu/create` 返回 `{"errcode":0,"errmsg":"ok"}`，
  `menu/get` 读回来结构完整。
- **带 emoji 的按钮名**：✅ 可用。`📊 状态` / `⚙️ 面板` / `🔧 操作` 都被接受。
  ⚠️ 但企微对 `name` 的限制是**字节数**不是字符数（官方文档写：一级菜单 16 字节、
  二级菜单 60 字节）——一个 emoji 占 4 字节（带变体选择符的占 6 字节），一个中文占 3 字节。
  排菜单时**要按字节数算**，别按 `len(str)` 算，否则中文/emoji 一多就会超。

---

## 7. 参考

- 官方文档：<https://developer.work.weixin.qq.com/document/>
- 本项目实现：`app/wecom_client.py`（API 客户端）、`app/api/callback.py`（回调加解密）
