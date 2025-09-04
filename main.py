import os
import random
import shutil
import time
import yaml
import tempfile
from pathlib import Path

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
from astrbot.api.star import StarTools

PLUGIN_NAME = "astrbot_plugin_pock_omini"
PLUGIN_DATA_SUBDIR = "astrbot_plugin_pock_omini"

# 把完整提示词定义为常量并在全类中复用
DEFAULT_LLM_PROMPT = (
    "这是一条系统消息，请不要对该消息本身进行回复，你应该依据以下情景进行回复:"
    "{username} 在{chat_type}戳了你，已经戳了{count}次，请你回复一下，回复要确保符合人设，切记不要重复发言，"
    "戳的次数越高你的反应应该要越来越强烈，考虑上下文，确保通顺不突兀。"
)

@register(PLUGIN_NAME, "原：长安某。改：IGCrystal", "监控戳一戳事件插件", "1.7.2")
class PokeMonitorPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.user_poke_info = {}

        try:
            data_dir = StarTools.get_data_dir(PLUGIN_NAME)
            cfg_dir = Path(data_dir)
        except Exception:
            cfg_dir = Path(__file__).resolve().parent.parent / "data" / "plugins" / PLUGIN_DATA_SUBDIR

        cfg_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = str((cfg_dir / "config.yml").resolve())

        self._ensure_config()
        self._load_config()
        
    def _ensure_config(self):
        cfg_path = Path(self.config_path)

        if cfg_path.exists() and cfg_path.is_dir():
            try:
                bak = cfg_path.with_name(cfg_path.name + ".dir.bak")
                cfg_path.rename(bak)
                logger.warning(f"配置路径原为目录，已改名为 {bak}")
            except Exception as e:
                logger.warning(f"配置路径为目录且改名失败：{e}")

        if not cfg_path.exists():
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
                "llm_prompt_template": DEFAULT_LLM_PROMPT,  # 使用统一常量
            }

            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile("w", delete=False, dir=str(cfg_path.parent), encoding="utf-8") as tf:
                    yaml.dump(default, tf, allow_unicode=True, default_flow_style=False)
                    tmp_path = tf.name
                os.replace(tmp_path, str(cfg_path))
                logger.info(f"写入默认配置到 {cfg_path}")
            except Exception as e:
                logger.error(f"写入默认配置失败：{e}")
                try:
                    if tmp_path and os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass

    def _load_config(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            if not isinstance(cfg, dict):
                logger.warning("配置文件格式不正确（非 dict），将使用默认或回退值")
                cfg = {}

            self.poke_responses = cfg.get("poke_responses", [])
            self.feature_switches = cfg.get("feature_switches", {})

            try:
                self.poke_back_probability = float(cfg.get("poke_back_probability", 0.0))
            except Exception:
                self.poke_back_probability = 0.0
            try:
                self.super_poke_probability = float(cfg.get("super_poke_probability", 0.0))
            except Exception:
                self.super_poke_probability = 0.0
            try:
                self.reset_interval = float(cfg.get("reset_interval_seconds", 60))
            except Exception:
                self.reset_interval = 60.0

            # 统一回退到相同的完整提示词常量
            self.llm_prompt_template = cfg.get("llm_prompt_template", DEFAULT_LLM_PROMPT)
        except FileNotFoundError:
            logger.warning("配置文件不存在，已使用内置默认值")
            self.poke_responses = []
            self.feature_switches = {}
            self.poke_back_probability = 0.0
            self.super_poke_probability = 0.0
            self.reset_interval = 60.0
            self.llm_prompt_template = DEFAULT_LLM_PROMPT
        except Exception as e:
            logger.error(f"加载配置失败：{e}")
            self.poke_responses = []
            self.feature_switches = {}
            self.poke_back_probability = 0.0
            self.super_poke_probability = 0.0
            self.reset_interval = 60.0
            self.llm_prompt_template = DEFAULT_LLM_PROMPT

    def _update_and_get_poke_count(self, user_id: int) -> int:
        now = time.time()
        last_ts, count = self.user_poke_info.get(user_id, (0, 0))
        if now - last_ts > self.reset_interval:
            count = 1
        else:
            count += 1
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
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
            if not isinstance(event, AiocqhttpMessageEvent):
                return
            if not hasattr(event, 'message_obj') or not event.message_obj:
                return
            if not hasattr(event.message_obj, 'raw_message'):
                return
            raw = event.message_obj.raw_message
            if not isinstance(raw, dict):
                return
            if not (
                raw.get('post_type') == 'notice'
                and raw.get('notice_type') == 'notify'
                and raw.get('sub_type') == 'poke'
                and str(raw.get('target_id')) == str(raw.get('self_id'))
            ):
                return

            sender_id = raw.get("user_id")
            group_id = raw.get("group_id")
            client = event.bot
            if group_id:
                info = await client.get_group_member_info(group_id=group_id, user_id=sender_id, no_cache=True)
            else:
                info = await client.get_stranger_info(user_id=sender_id)
            user_name = info.get("card") or info.get("nickname") or str(sender_id)
        except Exception as e:
            logger.warning(f"处理戳一戳事件失败，可能不是QQ平台: {e}")
            return

        count = self._update_and_get_poke_count(sender_id)
        prompt = self.llm_prompt_template.format(username=user_name, chat_type=chat_type, count=count)
        logger.info(f"[PokeMonitor] 用户名称：{user_name}，Prompt：{prompt}")

        if self.feature_switches.get('poke_response_enabled', True):
            try:
                cid = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
                conv = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, cid) if cid else None
                yield event.request_llm(
                    prompt=prompt,
                    func_tool_manager=self.context.get_llm_tool_manager(),
                    session_id=cid or event.unified_msg_origin,
                    image_urls=[],
                    conversation=conv
                )
            except Exception as e:
                logger.error(f"LLM 调用失败：{e}")
                fallback = (self.poke_responses[count - 1] if count <= len(self.poke_responses) and self.poke_responses else "别戳啦！")
                yield event.plain_result(fallback)

        if self.feature_switches.get('poke_back_enabled', True) and random.random() < float(self.poke_back_probability):
            times = 10 if random.random() < float(self.super_poke_probability) else 1
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
