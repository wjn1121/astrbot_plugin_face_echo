# 😂 表情附和

> 群友给某条消息贴了个表情回应，机器人给同一条消息贴上一个一样的——群里最捧场的那个。

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.16-blue)](https://github.com/AstrBotDevs/AstrBot)
[![Platform](https://img.shields.io/badge/platform-QQ_(aiocqhttp)-green)](#)
[![Version](https://img.shields.io/badge/version-1.0.0-orange)](metadata.yaml)
[![Author](https://img.shields.io/badge/author-wjn1121-lightgrey)](https://github.com/wjn1121)

---

## ✨ 功能特性

- 😂 **跟随贴表情**：识别 QQ 的「表情回应」（贴表情），给同一条消息贴上完全相同的表情
- 🤫 **不发言**：不发送任何聊天消息，只调用协议端的贴表情接口，不打断对话
- 🔁 **防自我取消**：同一条消息的同一个表情只贴一次（QQ 的贴表情是"再贴即取消"，重复调用会把机器人的表情取消掉）
- 🧊 **冷却限流**：可按群设置冷却时间与附和概率
- 🙈 **自我防护**：默认忽略机器人自己贴的表情，避免自激
- 🚫 **不干扰对话**：普通聊天消息完全不介入，AI 对话与其他插件照常工作
- ⚙️ **可视化配置**：WebUI 插件配置面板直接调节，无需改代码

---

## 🎬 效果演示

```
群友A: 今天好累啊
群友B: [给上面这条消息贴了 😂]      ← 贴表情
Bot:  [给同一条消息贴上 😂]          ← 插件行为，不产生聊天消息

群友C: 同感 [给同一条消息贴了 😂]
Bot:  （已经贴过 😂，不再重复，避免把自己的表情取消）
```

---

## 📥 安装方式

### 方式一：插件市场（推荐）

在 AstrBot WebUI → 插件市场 → 搜索「**表情附和**」→ 一键安装

### 方式二：手动安装

```bash
cd AstrBot/data/plugins/
git clone https://github.com/wjn1121/astrbot_plugin_face_echo.git
```

安装后在 WebUI 重载插件即可生效，无需额外依赖。

> **前置要求（重要）**：协议端必须支持 NapCat / Lagrange 扩展接口 `set_msg_emoji_like`
> 与上报事件 `group_msg_emoji_like`。**go-cqhttp 不支持**，插件在该协议端下不会有任何反应。

---

## ⚙️ 配置项

在 AstrBot WebUI → 插件管理 → 表情附和 → 配置面板中调整：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|:------:|------|
| `enable` | bool | `true` | 插件总开关 |
| `probability` | float | `1.0` | 附和概率，`1` 表示必跟随，调低可减少存在感 |
| `cooldown` | int | `0` | 同群冷却秒数，`0` 表示不限制 |
| `max_faces` | int | `1` | 一条消息被同时贴上多个表情时，最多跟随几个 |
| `ignore_self` | bool | `true` | 忽略机器人自己的贴表情，**务必保持开启** |
| `group_whitelist` | list | `[]` | 生效群号白名单，留空表示所有群生效 |

---

## 🔍 工作原理

贴表情在协议层**不是消息段**，而是独立的一类事件，收发走两条专门的通道：

**接收（上报事件，OneBot v11 扩展）**

```json
{
  "post_type": "notice",
  "notice_type": "group_msg_emoji_like",
  "group_id": 123456,
  "user_id": 10001,
  "message_id": 555,
  "likes": [{ "emoji_id": "128514", "count": 1 }]
}
```

`likes[].count` 为 `0` 表示取消贴表情，插件只跟随 `count > 0` 的表情。

**发送（扩展接口）**

```
set_msg_emoji_like { "message_id": 555, "emoji_id": "128514" }
```

**AstrBot 侧的三个关键点**（都由框架源码行为决定）：

1. 适配器把 notice 事件构造成「群消息类型、消息链为空」的事件，
   原始字段保留在 `event.message_obj.raw_message`，AstrBot 没有对应消息组件，
   所以表情 id、消息 id 只能从 `raw_message` 取。
2. `event.message_obj.message_id` 对 notice 事件是适配器生成的随机值，
   真正要贴的那条消息 id 在 `raw_message["message_id"]`。
3. 此类事件的 `is_at_or_wake_command` 为 `False`，不会触发 LLM 请求，
   插件也不 yield 任何消息，因此不会产生聊天回复。

处理流程（`main.py`）：

```
群事件 → 是否 group_msg_emoji_like 通知？
      → 不是：直接返回，不干预普通聊天
      → 总开关 → 是否机器人自己贴的 → 群白名单 → 取 message_id
      → 冷却/概率 → 过滤掉已贴过的 (群, 消息, 表情)
      → 逐个调用 set_msg_emoji_like（带上 self_id 路由到正确的 QQ 账号）
```

---

## ⚠️ 限制与说明

- **依赖协议端扩展能力**：需要 NapCat 或 Lagrange；go-cqhttp 等不支持该扩展的协议端下插件无效。
- **仅 QQ（aiocqhttp）**：其他平台适配器没有该事件与接口。
- **仅群聊**：私聊的贴表情通知格式不同，当前不处理。
- **不会重复贴同一个表情**：这是刻意设计。QQ 对同一条消息的同一个表情是"再贴一次即取消"，
  若第二个群友贴同一表情时再调一次接口，会取消机器人已贴的表情。
- **冷却与去重缓存在内存中**：插件重载或 AstrBot 重启后清空，属于预期行为。
- **接口调用失败只记录日志**：不重试，等待该消息的下一次贴表情通知再补。

---

## 📁 目录结构

```
astrbot_plugin_face_echo/
├── main.py              # 插件入口，监听贴表情通知并调用贴表情接口
├── metadata.yaml        # 插件元信息
├── _conf_schema.json    # WebUI 可视化配置定义
└── README.md
```

无第三方 pip 依赖，仅使用 AstrBot SDK 内置模块。

---

## 📚 参考

| 资源 | 链接 |
|------|------|
| AstrBot 插件开发指南 | https://docs.astrbot.app/dev/star/plugin-new.html |
| 接收消息事件 | https://docs.astrbot.app/dev/star/guides/listen-message-event.html |
| 插件配置 | https://docs.astrbot.app/dev/star/guides/plugin-config.html |
| NapCat 接口文档（set_msg_emoji_like） | https://doc.napneko.icu/develop/api/doc |
| NapCat 事件文档（group_msg_emoji_like） | https://doc.napneko.icu/onebot/event |
| AstrBot 主仓库 | https://github.com/AstrBotDevs/AstrBot |
