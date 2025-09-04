import os
import random
import shutil
import time
import yaml
from astrbot.api.all import (
    AstrMessageEvent,
    Context,
    EventMessageType,
    Star,
    event_message_type,
    register,
    PlatformAdapterType,
    logger,
)

@register("poke_monitor_omini", "原：长安某。改：IGCrystal", "监控戳一戳事件插件（精简版，仅 QQ 平台，次数重置优化）", "1.7.1")
class PokeMonitorPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 记录每个用户的戳信息：{user_id: [last_timestamp, count]}
        self.user_poke_info = {}
        self.config_path = os.path.join(
            "data", "plugins", "astrbot_plugin_pock", "config.yml"
        )
        self._ensure_config()
        self._load_config()
        self._clean_legacy_directories()

    def _ensure_config(self):
        if not os.path.exists(self.config_path):
            default = {
                "poke_responses": [
                    "别戳啦！",
                    "哎呀，还戳呀，别闹啦！",
                    "别戳我啦，你要做什么，不理你了",
                ],
                "feature_switches": {
                    "poke_response_enabled": True,
                    "poke_back_enabled": True
                },
                "poke_back_probability": 0.3,
                "super_poke_probability": 0.1,
                "reset_interval_seconds": 60,
                "llm_prompt_template": "这是一条系统消息，请不要对该消息本身进行回复，你应该依据以下情景进行回复:{username} 在{chat_type}戳了你，已经戳了{count}次，请你回复一下，回复要确保符合人设，切记不要重复发言，戳的次数越高你的反应应该要越来越强烈，考虑上下文，确保通顺不突兀。"
            }
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, 'w', encoding='utf-8') as f:
                yaml.dump(default, f, allow_unicode=True, default_flow_style=False)

    def _load_config(self):
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                cfg = yaml.safe_load(f) or {}
            self.poke_responses = cfg.get('poke_responses', [])
            self.feature_switches = cfg.get('feature_switches', {})
            self.poke_back_probability = cfg.get('poke_back_probability', 0.0)
            self.super_poke_probability = cfg.get('super_poke_probability', 0.0)
            self.reset_interval = cfg.get('reset_interval_seconds', 60)
            self.llm_prompt_template = cfg.get(
                'llm_prompt_template',
                "这是一条系统消息，请不要对该消息本身进行回复，你应该依据以下情景进行回复:{username} 在{chat_type}戳了你，已经戳了{count}次，请你回复一下，回复要确保符合人设，切记不要重复发言，戳的次数越高你的反应应该要越来越强烈，考虑上下文，确保通顺不突兀。"
            )
        except Exception as e:
            logger.error(f"加载配置失败：{e}")
            # 默认值
            self.poke_responses = []
            self.feature_switches = {}
            self.poke_back_probability = 0.0
            self.super_poke_probability = 0.0
            self.reset_interval = 60
            self.llm_prompt_template = "这是一条系统消息，请不要对该消息本身进行回复，你应该依据以下情景进行回复: {username} 在{chat_type}戳了你，已经戳了{count}次，请你回复一下，回复要确保符合人设，切记不要重复发言，戳的次数越高你的反应应该要越来越强烈，考虑上下文，确保通顺不突兀。"

    def _clean_legacy_directories(self):
        for d in ("./data/plugins/poke_monitor", "./data/plugins/plugins/poke_monitor"):
            try:
                path = os.path.abspath(d)
                if os.path.exists(path):
                    shutil.rmtree(path)
            except Exception as e:
                logger.warning(f"清理旧目录 {d} 失败：{e}")

    def _update_and_get_poke_count(self, user_id: int) -> int:
        now = time.time()
        last_ts, count = self.user_poke_info.get(user_id, (0, 0))
        # 如果距离上次戳超过重置间隔，则重置次数
        if now - last_ts > self.reset_interval:
            count = 1
        else:
            count += 1
        # 更新记录
        self.user_poke_info[user_id] = (now, count)
        return count

    @event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        async for r in self._handle_poke_event(event, chat_type="群聊"):
            yield r

    @event_message_type(EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        async for r in self._handle_poke_event(event, chat_type="私聊"):
            yield r

    async def _handle_poke_event(self, event: AstrMessageEvent, chat_type: str):
        # 检查是否是QQ平台 - 直接通过类型检查而不是属性
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
            # 如果不是QQ平台事件，直接返回
            if not isinstance(event, AiocqhttpMessageEvent):
                return
                
            # 确保有message_obj和raw_message
            if not hasattr(event, 'message_obj') or not event.message_obj:
                return
                
            if not hasattr(event.message_obj, 'raw_message'):
                return
                
            raw = event.message_obj.raw_message
            if not isinstance(raw, dict):
                return
                
            # 仅处理 QQ 平台的戳一戳通知
            if not (
                raw.get('post_type') == 'notice'
                and raw.get('notice_type') == 'notify'
                and raw.get('sub_type') == 'poke'
                and str(raw.get('target_id')) == str(raw.get('self_id'))
            ):
                return
                
            sender_id = raw.get("user_id")
            group_id = raw.get("group_id")
            # 获取 QQ 用户名称
            client = event.bot
            if group_id:
                info = await client.get_group_member_info(
                    group_id=group_id, user_id=sender_id, no_cache=True
                )
            else:
                info = await client.get_stranger_info(user_id=sender_id)
            user_name = info.get("card") or info.get("nickname") or str(sender_id)
        except Exception as e:
            logger.warning(f"处理戳一戳事件失败，可能不是QQ平台: {e}")
            return
            
        # 更新计数
        count = self._update_and_get_poke_count(sender_id)
        prompt = self.llm_prompt_template.format(
            username=user_name,
            chat_type=chat_type,
            count=count
        )
        logger.info(f"[PokeMonitor] 用户名称：{user_name}，Prompt：{prompt}")
        # LLM 回复
        if self.feature_switches.get('poke_response_enabled', True):
            try:
                cid = await self.context.conversation_manager.get_curr_conversation_id(
                    event.unified_msg_origin
                )
                conv = await self.context.conversation_manager.get_conversation(
                    event.unified_msg_origin, cid
                ) if cid else None
                yield event.request_llm(
                    prompt=prompt,
                    func_tool_manager=self.context.get_llm_tool_manager(),
                    session_id=cid or event.unified_msg_origin,
                    image_urls=[],
                    conversation=conv
                )
            except Exception as e:
                logger.error(f"LLM 调用失败：{e}")
                fallback = (
                    self.poke_responses[count - 1]
                    if count <= len(self.poke_responses) and self.poke_responses
                    else "别戳啦！"
                )
                yield event.plain_result(fallback)
        # 随机戳回
        if self.feature_switches.get('poke_back_enabled', True) and random.random() < self.poke_back_probability:
            times = 10 if random.random() < self.super_poke_probability else 1
            action = "喜欢戳是吧" if times > 1 else "戳回去"
            yield event.plain_result(action)
            for _ in range(times):
                try:
                    payload = {"user_id": sender_id}
                    if group_id:
                        payload["group_id"] = group_id
                    await client.api.call_action("send_poke", **payload)
                except Exception as e:
                    logger.warning(f"send_poke 调用失败: {e}")
