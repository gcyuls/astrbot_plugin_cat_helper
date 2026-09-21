import os
import re
import json
import random
import asyncio
import string
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Set

from astrbot.api import AstrBotConfig
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
import astrbot.api.message_components as Comp

# 怪物猎人 12 位集会码字符集定义
UPPERCASE_SET = set(string.ascii_uppercase)
LOWERCASE_SET = set(string.ascii_lowercase)
DIGIT_SET = set(string.digits)
SPECIAL_SET = {'!', '@', '#', '$', '-', '=', '+', '?', '&'}
CHAR_SETS = [UPPERCASE_SET, LOWERCASE_SET, DIGIT_SET, SPECIAL_SET]

# 特殊句式复读正则（例如叠词结构）
RE_REPEAT_PATTERN_1 = re.compile(r"^(.+)(.+)\1的$")
RE_REPEAT_PATTERN_2 = re.compile(r"^帅(.+)帅$")

def is_valid_gathering_code(text: str) -> bool:
    """校验是否符合怪物猎人 12 位集会码特征（至少包含两种字符集，且全字符落在合法字符集中）"""
    if len(text) != 12:
        return False
    if all(any(c in s for s in CHAR_SETS) for c in text):
        set_count = sum(1 for s in CHAR_SETS if any(c in s for c in text))
        return set_count >= 2
    return False

@dataclass
class GroupRepeatState:
    """群聊复读跟踪状态"""
    last_text: str = ""
    repeat_count: int = 0

@register("astrbot_plugin_cat_helper", "gcyuls", "呆猫群聊管家与怪猎助手", "1.0.1")
class CatHelperPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config

        # OneBot 适配器标准标识
        self.platform_name = "aiocqhttp"

        # 1. 静态资源路径初始化
        self.plugin_dir = Path(__file__).parent
        self.assets_dir = self.plugin_dir / "assets"
        self.weapons_dir = self.assets_dir / "weapons"

        # 2. 动态持久化数据路径（适配 AstrBot 规范并支持兼容兜底）
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            base_data_path = Path(get_astrbot_data_path())
        except Exception:
            base_data_path = Path("data")

        self.data_dir = base_data_path / "plugin_data" / "cat_helper"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.code_file = self.data_dir / "code_info.json"

        # 3. 运行时内存状态（按用户/群聊隔离）
        self.weapon_cooldown: Dict[str, datetime] = {}
        self.group_repeat_states: Dict[str, GroupRepeatState] = {}

        # 4. 后台调度维护任务
        self.scheduler_task = asyncio.create_task(self._daily_clean_loop())

    async def _daily_clean_loop(self):
        """每日定时清理任务（时间可配）"""
        while True:
            try:
                now = datetime.now()
                clean_hour = int(self.config.get("clean_hour", 4))
                target = now.replace(hour=clean_hour, minute=0, second=0, microsecond=0)
                if now >= target:
                    target += timedelta(days=1)
                wait_seconds = (target - now).total_seconds()

                await asyncio.sleep(wait_seconds)

                # 清理过期集会码
                if self.code_file.exists():
                    self.code_file.unlink()

                # 清理冷却和复读历史状态
                self.weapon_cooldown.clear()
                self.group_repeat_states.clear()
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(60)

    async def terminate(self):
        """插件卸载/重载生命周期回调"""
        if self.scheduler_task and not self.scheduler_task.done():
            self.scheduler_task.cancel()

    # ================= 1. 传话 / Cosplay 模块 =================
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message_proxy(self, event: AstrMessageEvent):
        """私聊消息自动转发到目标群聊（目标群号从配置项读取）"""
        target_group = str(self.config.get("cosplay_target_group", "")).strip()
        if not target_group:
            return

        platform_id = event.unified_msg_origin.split(":", 1)[0] if event.unified_msg_origin and ":" in event.unified_msg_origin else self.platform_name
        # 原样转发消息链内容（封装为 MessageChain 实例）
        raw_message = event.message_obj.message
        if isinstance(raw_message, MessageChain):
            chain = raw_message
        elif isinstance(raw_message, list):
            chain = MessageChain(chain=list(raw_message))
        elif raw_message:
            chain = MessageChain().message(str(raw_message))
        else:
            return

        await self.context.send_message(target_umo, chain)

    # ================= 2. 录入集会码模块 =================
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.regex(r"^.{12}$")
    async def on_set_code(self, event: AstrMessageEvent):
        """自动识别 12 位怪猎集会码并存储"""
        text = event.message_str.strip()
        if is_valid_gathering_code(text):
            sender_name = event.get_sender_name() or "猎人老大"
            code_info = {
                "code": text,
                "host": sender_name,
                "time": datetime.now().strftime("%m-%d %H:%M")
            }
            with open(self.code_file, "w", encoding="utf-8") as f:
                json.dump(code_info, f, ensure_ascii=False, indent=2)
            yield event.plain_result("收到了老大的集会码喵！")
            event.stop_event()

    # ================= 3. 查询集会码模块 =================
    @filter.regex(r"^[\/／]?码来$")
    async def on_get_code(self, event: AstrMessageEvent):
        """查询当前最新的集会码（兼容直接发送“码来”或带前缀“/码来”）"""
        if not self.code_file.exists():
            yield event.plain_result("老大，目前还没有收到集会码喵！")
            event.stop_event()
            return

        try:
            with open(self.code_file, "r", encoding="utf-8") as f:
                info = json.load(f)
            # 第一条提示时间与发布人
            yield event.plain_result(f"老大，当前最新的集会码是 {info['host']} 在 {info['time']} 发布的喵！")
            # 第二条单独发送集会码（方便手机端长按复制）
            yield event.plain_result(info["code"])
        except Exception:
            yield event.plain_result("老大，集会码解析失败喵！")
        finally:
            event.stop_event()

    # ================= 4. 武器抽卡模块 =================
    @filter.regex(r"^[\/／]?今天玩什么$")
    async def on_get_weapon(self, event: AstrMessageEvent):
        """随机抽取怪猎 14 种武器之一（兼容直接发送“今天玩什么”或带前缀“/今天玩什么”）"""
        sender_id = str(event.get_sender_id())
        now = datetime.now()

        # 从配置中读取冷却时长
        cd_seconds = int(self.config.get("weapon_cooldown_seconds", 60))
        if sender_id in self.weapon_cooldown:
            time_passed = (now - self.weapon_cooldown[sender_id]).total_seconds()
            if time_passed < cd_seconds:
                yield event.chain_result([
                    Comp.At(qq=sender_id),
                    Comp.Plain(" 老大太快了喵！休息一下喵！")
                ])
                event.stop_event()
                return

        self.weapon_cooldown[sender_id] = now

        # 获取武器图片列表
        if not self.weapons_dir.exists():
            yield event.plain_result("老大，未找到武器资源目录喵！")
            event.stop_event()
            return

        weapon_images = list(self.weapons_dir.glob("*.png"))
        if not weapon_images:
            yield event.plain_result("老大，武器图片未就绪喵！")
            event.stop_event()
            return

        chosen_img = random.choice(weapon_images)
        weapon_name = chosen_img.stem  # 如 "太刀"

        chain = [
            Comp.At(qq=sender_id),
            Comp.Plain(f" 老大今天玩{weapon_name}喵！不想玩的话1分钟之后再来喵！\n"),
            Comp.Image.fromFileSystem(str(chosen_img))
        ]
        yield event.chain_result(chain)
        event.stop_event()

    # ================= 5. 复读机与渐进式打断模块 =================
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_repeat_handler(self, event: AstrMessageEvent):
        """复读机逻辑：句式复读 + 渐进式概率打断"""
        text = event.message_str.strip()
        if not text:
            return

        # 过滤指令与集会码，避免误入复读逻辑
        clean_text = text.lstrip("/／")
        if clean_text in ("码来", "今天玩什么") or is_valid_gathering_code(text):
            return

        group_id = str(event.message_obj.group_id)

        # 规则 A：特定前缀/后缀/结构复读（尾部附加零宽空格 \u200b，防止自死循环）
        prefixes = ("啊啊", "唉", "并非", "看看")
        if text.startswith(prefixes) or text.endswith("导致的"):
            yield event.plain_result(text + "\u200b")
            return

        if (text.startswith("帅") and text.endswith("帅") and len(text) > 2) or \
           RE_REPEAT_PATTERN_1.match(text) or RE_REPEAT_PATTERN_2.match(text):
            yield event.plain_result(text + "\u200b")
            return

        # 规则 B：群隔离的渐进概率打断复读
        state = self.group_repeat_states.setdefault(group_id, GroupRepeatState())

        if text == state.last_text:
            state.repeat_count += 1

            allow_count = int(self.config.get("repeat_allow_count", 2))
            max_count = int(self.config.get("repeat_max_count", 6))
            base_prob = float(self.config.get("repeat_base_prob", 0.25))
            prob_step = float(self.config.get("repeat_prob_step", 0.25))
            suffix = str(self.config.get("repeat_interrupt_suffix", "喵"))

            # 计算当前打断概率
            if state.repeat_count <= allow_count:
                prob = 0.0
            elif state.repeat_count >= max_count:
                prob = 1.0
            else:
                prob = min(1.0, base_prob + (state.repeat_count - allow_count - 1) * prob_step)

            # 概率判定打断
            if prob > 0 and random.random() < prob:
                # 触发打断，重置状态
                state.last_text = ""
                state.repeat_count = 0
                yield event.plain_result(f"{text}{suffix}")
        else:
            # 出现新内容，重置计数
            state.last_text = text
            state.repeat_count = 1

    def _parse_reminder_rules(self) -> List[dict]:
        """解析多用户提醒规则（兼容多行文本、列表及旧版字典结构）"""
        raw_rules = self.config.get("reminder_rules", "")
        rules = []

        if isinstance(raw_rules, str):
            for line in raw_rules.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # 支持冒号分割或空格分割
                if ":" in line or "：" in line:
                    sep = ":" if ":" in line else "："
                    parts = line.split(sep, 1)
                    target_qq = parts[0].strip()
                    kws_str = parts[1].strip()
                else:
                    parts = line.split(None, 1)
                    if len(parts) >= 2:
                        target_qq = parts[0].strip()
                        kws_str = parts[1].strip()
                    else:
                        continue

                kws = [k.strip() for k in re.split(r"[,，、;；\s]+", kws_str) if k.strip()]
                if target_qq and kws:
                    rules.append({"target_qq": target_qq, "keywords": kws})

        elif isinstance(raw_rules, list):
            for item in raw_rules:
                if isinstance(item, dict):
                    rules.append(item)
                elif isinstance(item, str):
                    item = item.strip()
                    if not item or item.startswith("#"):
                        continue
                    if ":" in item or "：" in item:
                        sep = ":" if ":" in item else "："
                        parts = item.split(sep, 1)
                        target_qq = parts[0].strip()
                        kws = [k.strip() for k in re.split(r"[,，、;；\s]+", parts[1]) if k.strip()]
                        if target_qq and kws:
                            rules.append({"target_qq": target_qq, "keywords": kws})

        return rules

    # ================= 6. 多用户关键词呼叫私聊通知模块 =================
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_keyword_reminder(self, event: AstrMessageEvent):
        """群聊内提及配置的关键词时，主动私聊通知对应的被提醒人（支持多用户与去重）"""
        text = event.message_str
        if not text:
            return

        sender_id = str(event.get_sender_id())
        sender_name = event.get_sender_name() or "群友"
        group_id = str(event.message_obj.group_id)

        # 解析多用户规则（支持多行文本与列表）
        rules: List[dict] = self._parse_reminder_rules()
        if not rules:
            return

        # 记录本条消息已通知的目标 QQ，避免同条消息命中同一用户的多个词而重复打扰
        notified_qqs: Set[str] = set()

        for rule in rules:
            target_qq = str(rule.get("target_qq", "")).strip()
            if not target_qq or target_qq in notified_qqs:
                continue

            # 排除自己提到自己关键词的情况
            if sender_id == target_qq:
                continue

            raw_keywords = rule.get("keywords", [])
            if isinstance(raw_keywords, str):
                keywords = [k.strip() for k in raw_keywords.split(",") if k.strip()]
            else:
                keywords = [str(k).strip() for k in raw_keywords if str(k).strip()]

            # 匹配关键词
            hit_keywords = [kw for kw in keywords if kw in text]
            if hit_keywords:
                notified_qqs.add(target_qq)
                # 动态获取当前事件的平台ID，并将单聊消息类型设为 AstrBot 规范的 FriendMessage
                platform_id = event.unified_msg_origin.split(":", 1)[0] if event.unified_msg_origin and ":" in event.unified_msg_origin else self.platform_name
                target_umo = f"{platform_id}:FriendMessage:{target_qq}"

                notice_text = (
                    f"【群聊提醒】\n"
                    f"来自群 [{group_id}] 的 [{sender_name}] 提及了你（触发词: {', '.join(hit_keywords)}）：\n"
                    f"“{text}”"
                )
                notice_chain = MessageChain().message(notice_text)
                await self.context.send_message(target_umo, notice_chain)
