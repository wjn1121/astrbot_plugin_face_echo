"""
表情附和（贴表情）- AstrBot QQ 群聊插件

群友给某条群消息贴上 QQ 表情回应后，机器人给同一条消息贴上相同的表情——
就是消息右下角那个「😂 1」。

协议说明（OneBot v11 扩展，NapCat / Lagrange 均已实现）：
- 上报事件：post_type=notice, notice_type=group_msg_emoji_like
  字段：group_id、user_id、message_id、likes=[{emoji_id, count}]
  其中 count 为 0 表示取消贴表情。
- 发送接口：set_msg_emoji_like {message_id, emoji_id}
  该接口不是 OneBot 标准接口，而是 NapCat / Lagrange 的扩展，
  因此插件只支持 aiocqhttp 平台且协议端需支持该扩展。

AstrBot 侧的关键点（决定了实现方式）：
- 适配器把 notice 事件构造成「群消息类型、消息链为空」的事件，
  原始字段保存在 event.message_obj.raw_message，AstrBot 没有对应的消息组件，
  所以表情 id 和消息 id 只能从 raw_message 里取。
- event.message_obj.message_id 对 notice 事件是适配器生成的随机值，
  真正要贴的那条消息 id 在 raw_message["message_id"]。
- 此类事件的 is_at_or_wake_command 为 False，不会触发 LLM 请求，
  插件也不 yield 任何消息，只调用协议端接口，因此不会产生聊天回复。
"""

import random
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

NOTICE_TYPE = "group_msg_emoji_like"
"""QQ 表情回应（贴表情）的 notice 事件类型。"""

API_SET_EMOJI_LIKE = "set_msg_emoji_like"
"""NapCat / Lagrange 提供的贴表情接口。"""

_ECHO_CACHE_LIMIT = 2000
"""去重缓存条数上限，超过后丢弃最旧的记录，避免长期运行内存持续增长。"""


class FaceEchoPlugin(Star):
    """群聊贴表情附和插件。

    继承 Star（AstrBot 插件基类），通过 @filter.event_message_type()
    监听群事件，识别贴表情通知后给同一条消息贴上相同的表情。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        """
        插件初始化。

        context 由 AstrBot 框架注入；config 对应 _conf_schema.json 定义的配置项。
        """
        super().__init__(context)
        self.config = config

        # 群号 -> 上次附和时间，用于冷却限流；只存内存，重载后自动清空
        self._last_echo_at: dict[str, float] = {}

        # (群号, 消息id, 表情id) -> 贴上时间。
        # QQ 对同一条消息的同一个表情是「再贴一次即取消」，所以必须记住已贴过的组合：
        # 第二个群友贴同一个表情时若再调一次接口，会把机器人刚贴的表情取消掉。
        self._echoed: dict[tuple[str, str, str], float] = {}

        logger.info("表情附和 插件已加载")

    # ──────────────────────────────────────────────
    # 事件处理
    # ──────────────────────────────────────────────

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_group_message(self, event: AstrMessageEvent):
        """
        监听群事件，发现有人贴表情就给同一条消息贴上相同表情。

        装饰器说明：
          - @filter.event_message_type: 仅群事件（notice 事件也被适配器归为群消息类型）
          - @filter.platform_adapter_type: 仅 QQ(OneBot v11) 平台，
            贴表情接口是该平台的扩展能力
        """
        notice = self._extract_notice(event)
        if notice is None:
            # 普通聊天消息：完全不介入，交给后续流程处理
            return

        if not self._switch("enable", True):
            return

        group_id = event.get_group_id() or str(notice.get("group_id", ""))

        # 机器人自己贴的表情同样会被协议端上报，必须跳过：
        # 否则机器人会跟着自己的表情再贴一次，等于立刻取消
        if self._switch("ignore_self", True) and (
            str(notice.get("user_id", "")) == event.get_self_id()
        ):
            return

        if not self._in_whitelist(group_id):
            return

        message_id = str(notice.get("message_id") or "")
        if not message_id:
            logger.warning("贴表情通知里没有 message_id，已忽略")
            return

        if not self._cooldown_passed(group_id) or not self._roll_probability():
            return

        pending = self._pick_pending_emojis(notice, group_id, message_id)
        if not pending:
            # 这些表情之前已经贴过（或只有取消操作），无需再调接口
            return

        bot = getattr(event, "bot", None)
        if bot is None:
            logger.error("当前事件没有 bot 实例，无法调用贴表情接口")
            return

        now = time.time()
        for emoji_id in pending:
            try:
                await self._set_emoji_like(bot, event, message_id, emoji_id)
            except Exception as e:
                # 单个表情失败不影响其他表情；失败不记录，下次通知还能补上
                logger.error(f"贴表情失败(消息 {message_id}, 表情 {emoji_id}): {e}")
                continue
            self._echoed[(group_id, message_id, emoji_id)] = now
            logger.debug(f"群 {group_id} 消息 {message_id} 已附和表情 {emoji_id}")

        self._last_echo_at[group_id] = now
        self._prune_echo_cache()

    # ──────────────────────────────────────────────
    # 事件解析
    # ──────────────────────────────────────────────

    @staticmethod
    def _extract_notice(event: AstrMessageEvent) -> dict | None:
        """取出原始事件里的贴表情通知；不是贴表情事件时返回 None。"""
        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            return None
        if raw.get("post_type") != "notice" or raw.get("notice_type") != NOTICE_TYPE:
            return None
        return raw

    @staticmethod
    def _collect_emoji_ids(notice: dict) -> list[str]:
        """
        取出本次通知中「被贴上」的表情 id，按出现顺序去重。

        count 为 0 表示该表情被取消，不属于附和范围，直接跳过。
        """
        likes = notice.get("likes")
        if not isinstance(likes, list):
            return []

        emoji_ids: list[str] = []
        for like in likes:
            if not isinstance(like, dict):
                continue

            try:
                count = int(like.get("count", 0))
            except (TypeError, ValueError):
                count = 0
            if count <= 0:
                continue

            emoji_id = str(like.get("emoji_id", "")).strip()
            if emoji_id and emoji_id not in emoji_ids:
                emoji_ids.append(emoji_id)

        return emoji_ids

    def _pick_pending_emojis(
        self,
        notice: dict,
        group_id: str,
        message_id: str,
    ) -> list[str]:
        """挑出本次需要贴的表情 id，最多 max_faces 个，已贴过的不再重复。"""
        max_faces = max(1, self._int_config("max_faces", 1))
        pending: list[str] = []

        for emoji_id in self._collect_emoji_ids(notice):
            if (group_id, message_id, emoji_id) in self._echoed:
                continue
            pending.append(emoji_id)
            if len(pending) >= max_faces:
                break

        return pending

    # ──────────────────────────────────────────────
    # 调用协议端接口
    # ──────────────────────────────────────────────

    @staticmethod
    async def _set_emoji_like(
        bot,
        event: AstrMessageEvent,
        message_id: str,
        emoji_id: str,
    ) -> None:
        """
        调用 OneBot 扩展接口给指定消息贴表情。

        额外传 self_id 是为了在 AstrBot 同时连接多个 QQ 账号时，
        把请求路由到上报该事件的那个账号（与适配器内部的发送逻辑一致）。
        """
        params: dict = {
            "message_id": int(message_id) if message_id.isdigit() else message_id,
            "emoji_id": emoji_id,
        }
        self_id = event.get_self_id()
        if self_id:
            params["self_id"] = self_id

        await bot.call_action(API_SET_EMOJI_LIKE, **params)

    def _prune_echo_cache(self) -> None:
        """缓存过大时丢弃最早的记录，控制内存占用。"""
        if len(self._echoed) <= _ECHO_CACHE_LIMIT:
            return

        overflow = len(self._echoed) - _ECHO_CACHE_LIMIT
        oldest = sorted(self._echoed.items(), key=lambda item: item[1])[:overflow]
        for key, _ in oldest:
            self._echoed.pop(key, None)

    # ──────────────────────────────────────────────
    # 配置读取（WebUI 存进来的是 JSON 值，做一层容错）
    # ──────────────────────────────────────────────

    def _switch(self, key: str, default: bool) -> bool:
        """读取布尔配置，兼容字符串形式的 "true"/"1"。"""
        value = self.config.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "on"}
        return bool(value)

    def _int_config(self, key: str, default: int) -> int:
        """读取整数配置，非法值回退到默认值。"""
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            logger.warning(f"配置项 {key} 不是整数，已使用默认值 {default}")
            return default

    def _float_config(self, key: str, default: float) -> float:
        """读取浮点配置，非法值回退到默认值。"""
        try:
            return float(self.config.get(key, default))
        except (TypeError, ValueError):
            logger.warning(f"配置项 {key} 不是数字，已使用默认值 {default}")
            return default

    # ──────────────────────────────────────────────
    # 限流与过滤
    # ──────────────────────────────────────────────

    def _cooldown_passed(self, group_id: str) -> bool:
        """同群冷却是否已过；cooldown <= 0 表示不限制。"""
        cooldown = self._int_config("cooldown", 0)
        if cooldown <= 0:
            return True
        return time.time() - self._last_echo_at.get(group_id, 0.0) >= cooldown

    def _roll_probability(self) -> bool:
        """按概率决定本次是否附和，用于避免机器人显得过于机械。"""
        probability = self._float_config("probability", 1.0)
        if probability >= 1:
            return True
        if probability <= 0:
            return False
        return random.random() < probability

    def _in_whitelist(self, group_id: str) -> bool:
        """群白名单过滤；白名单为空表示所有群生效。"""
        whitelist = self.config.get("group_whitelist") or []
        if not whitelist:
            return True
        return str(group_id) in {str(item).strip() for item in whitelist}

    # ──────────────────────────────────────────────
    # 生命周期
    # ──────────────────────────────────────────────

    async def terminate(self):
        """插件被卸载/禁用时调用；本插件无后台任务与外部连接，无需清理。"""
        logger.info("表情附和 插件已卸载")
