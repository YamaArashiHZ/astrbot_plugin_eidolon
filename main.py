"""
AstrBot 插件:群文生图(仅适配火山方舟 Seedream 5.0 Pro)

功能:
  1. 指令触发: /画图 <提示词> [-a 宽高比] [-s 分辨率]
  2. 群内 @机器人 + 自然语言直接生图(可选 enable_nl_trigger)
  3. 生图后端:火山方舟 Seedream(OpenAI 兼容子集,按官方 images/generations API)
  4. 三级限额:总限额 / 每人限额 / 管理员豁免
  5. LLM 提示词润色(可选,默认中文,失败回退原文)

接口依据:火山方舟「图片生成 API」官方文档(2026-08-01 核对)
  - POST https://ark.cn-beijing.volces.com/api/v3/images/generations
  - size 支持方式1(推荐):分辨率档位 1K/1.5K/2K + prompt 自然语言描述宽高比
  - 无 seed 参数;watermark/output_format 为独立参数
"""
import asyncio
import base64
from collections import deque
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from pydantic import Field
from pydantic.dataclasses import dataclass

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult, ToolSet
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"          # 火山方舟(Seedream)
PLUGIN_NAME = "astrbot_plugin_eidolon"
VALID_ASPECTS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9"}
VALID_SIZES = {"1K", "1.5K", "2K"}                            # Seedream 5.0 pro 档位
IMG2IMG_MAX_IMAGES = 8                                        # 图生图最大参考图数量(固定)
EXT_BY_MIME = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
FLAG_RE = re.compile(r"(?:^|\s)[-－—](a|s)(?:\s+|=)(\S+)", re.IGNORECASE)
DRAW_COMMAND_RE = re.compile(r"^\s*[!！/／]?\s*(?:画图|draw)(?:\s+|$)", re.IGNORECASE)
SAFETY_SUFFIX = ", high quality, detailed, safe for work, no text watermark"

# 润色 system prompt 默认值(可在插件页面自定义,空则回退此默认值)
ENHANCE_SYSTEM_PROMPT_ZH = (
    "你是一名专业的 AI 绘画提示词工程师。请将用户的中文描述润色为:结构清晰、"
    "细节丰富、适合图像生成模型的中文提示词。要求:只输出润色后的提示词本身,"
    "不要任何解释、引号或多余内容。"
)
ENHANCE_SYSTEM_PROMPT_EN = (
    "You are a professional AI image prompt engineer. Polish the user's description "
    "into a well-structured, detailed English prompt suitable for image generation "
    "models. Output ONLY the polished prompt, no explanations or quotes."
)

# 自然语言触发:函数工具(由主对话模型自行判断是否生图,取代独立的 LLM 意图判断)
DRAW_TOOL_NAME = "eidolon_draw"
DRAW_TOOL_DESCRIPTION = (
    "生成、绘制或修改图片,并直接把图片发送到当前会话。"
    "当用户明确要求画图、生图、出图、改图(如换背景、换风格、把某物改成某物、"
    "融合多张图)时调用本工具;用户只是在聊天中讨论图片、评价图片或询问绘画知识时不要调用。"
    "prompt 要写成完整、具体、可直接用于图像生成的画面描述,"
    "并整合对话上下文(例如用户此前提到的风格、主体或修改要求)。"
)

# 插件默认配置(插件页面编辑,存于 data/plugin_data/<plugin>/config.json)
DEFAULT_CONFIG = {
    "seedream_api_key": "",
    "seedream_model": "doubao-seedream-5-0-pro",
    "enable_proxy": False,
    "proxy": "http://127.0.0.1:7897",
    "aspect_ratio": "1:1",
    "image_size": "2K",
    "output_format": "png",
    "watermark": True,
    "max_concurrency": 5,
    "cooldown_seconds": 30,
    "total_limit": 200,
    "per_user_limit": 0,
    "admin_ignore_limit": True,
    "request_timeout": 300,
    "image_cache_max_mb": 500,
    "enable_nl_trigger": False,
    "nl_min_prompt_len": 5,
    "nl_trigger_mode": "keyword",
    "nl_keywords": "画,绘制,生成,图片,图像,插画,海报,壁纸,头像,表情包,logo,照片,draw,image,picture,photo,illustration,wallpaper",
    "enable_prompt_enhance": False,
    "enhance_timeout": 30,
    "enhance_lang": "zh",
    "enhance_provider_id": "",
    "enhance_system_prompt_zh": ENHANCE_SYSTEM_PROMPT_ZH,
    "enhance_system_prompt_en": ENHANCE_SYSTEM_PROMPT_EN,
    "enable_img2img": True,
}

# 宽高比 -> prompt 自然语言描述(方式1:模型据描述判断生成尺寸)
ASPECT_DESC = {
    "1:1": "正方形构图 / square composition",
    "16:9": "横版构图,宽高比16:9 / landscape 16:9",
    "9:16": "竖版构图,宽高比9:16 / portrait 9:16",
    "4:3": "横版构图,宽高比4:3 / landscape 4:3",
    "3:4": "竖版构图,宽高比3:4 / portrait 3:4",
    "3:2": "横版构图,宽高比3:2 / landscape 3:2",
    "2:3": "竖版构图,宽高比2:3 / portrait 2:3",
    "21:9": "超宽横版构图,宽高比21:9 / ultra-wide 21:9",
}

# 工具模式下,工具调用内最多同步等待生图的秒数;
# 超过后工具先返回、生成任务继续在后台跑,避免超过 AstrBot 的 tool_call_timeout(默认 120 秒)
TOOL_SYNC_WAIT_SECONDS = 90


@dataclass
class EidolonDrawTool(FunctionTool[AstrAgentContext]):
    """生图函数工具:把「是否生图」的判断交给主对话模型本身。

    取代了旧版「单独调用一次 LLM 做意图判断」的做法:模型本来就要处理这条消息,
    由它带着完整会话上下文决定是否调用本工具,省去一次额外的 LLM 请求。
    工具仅由插件在群内 @机器人 且开启自然语言触发时注入当前请求。
    """

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    plugin: Any = None
    """插件实例(EidolonPlugin),由插件构造工具时注入。"""

    name: str = DRAW_TOOL_NAME
    description: str = DRAW_TOOL_DESCRIPTION
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "完整、具体的画面描述(中文),直接作为图像生成提示词。"
                        "需要包含主体、风格、构图、光线等可执行细节。"
                    ),
                },
                "aspect_ratio": {
                    "type": "string",
                    "enum": sorted(VALID_ASPECTS),
                    "description": "画面宽高比;不确定时留空,使用插件默认值。",
                },
                "image_size": {
                    "type": "string",
                    "enum": sorted(VALID_SIZES),
                    "description": "输出分辨率档位;不确定时留空,使用插件默认值。",
                },
            },
            "required": ["prompt"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], prompt: str,
                   aspect_ratio: str = "", image_size: str = "") -> ToolExecResult:
        plugin = self.plugin
        event = getattr(getattr(context, "context", None), "event", None)
        if plugin is None or event is None:
            logger.warning("生图工具缺少插件或事件上下文,已拒绝本次调用")
            return "生图工具当前不可用,请如实告知用户稍后再试。"

        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            return "生图失败:prompt 为空。请先用一句话描述要生成的画面,再重新调用本工具。"

        aspect = str(aspect_ratio or "").strip()
        if aspect and aspect not in VALID_ASPECTS:
            logger.warning(f"工具传入的宽高比不支持: {aspect!r},已回退插件默认值")
            aspect = ""
        image_size = str(image_size or "").strip()
        if image_size and image_size not in VALID_SIZES:
            logger.warning(f"工具传入的分辨率不支持: {image_size!r},已回退插件默认值")
            image_size = ""

        # 参考图只能来自消息本身(工具参数无法承载图片):同条消息的图片或引用回复的图片
        images: list[Comp.Image] = []
        if plugin.config.get("enable_img2img", True):
            images = plugin._collect_reference_images(event)
            if len(images) > IMG2IMG_MAX_IMAGES:
                logger.info(
                    f"参考图数量 {len(images)} 超过上限 {IMG2IMG_MAX_IMAGES},"
                    f"已截取前 {IMG2IMG_MAX_IMAGES} 张")
                images = images[:IMG2IMG_MAX_IMAGES]

        # 前置校验配额/冷却:被拒绝时直接回话,避免先答应用户再发失败提示
        is_admin = event.is_admin()
        group_id = event.get_group_id() or event.unified_msg_origin
        ok, reason = plugin._check_quota(group_id, event.get_sender_id(), is_admin)
        if not ok:
            logger.info(f"工具触发生图被限额拦截 group={group_id}: {reason}")
            return f"生图被拒绝:{reason} 请如实转述给用户,不要声称已生成图片。"

        logger.info(
            f"工具触发生图 group={group_id} sender={event.get_sender_id()} "
            f"images={len(images)} aspect={aspect or '默认'} size={image_size or '默认'} "
            f"prompt={clean_prompt[:50]!r}")
        task = asyncio.create_task(
            plugin._run_tool_generation(event, clean_prompt, aspect, image_size, images))
        plugin._track_tool_task(task)
        done, _pending = await asyncio.wait({task}, timeout=TOOL_SYNC_WAIT_SECONDS)
        if not done:
            logger.info(f"生图仍在后台执行(>{TOOL_SYNC_WAIT_SECONDS}s),工具先返回")
            return (
                "图片仍在生成中,完成后插件会自动把图片发到群里。"
                "请简短告知用户正在生成(例如「马上画好」),不要复述画面内容,也不要再次调用本工具。")
        try:
            return task.result()
        except Exception as e:
            logger.exception(f"工具生图任务异常: {e}")
            return f"生图失败:{e}。请如实告知用户失败原因,不要声称图片已生成。"

    def __repr__(self) -> str:
        return f"EidolonDrawTool(name={self.name!r})"


class EidolonPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        plugin_name = getattr(self, "name", PLUGIN_NAME)
        self.plugin_name = plugin_name
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.image_dir = self.data_dir / "images"
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_dir / "config.json"
        self.config = self._load_config()

        self._generation_condition = asyncio.Condition()
        self._generation_queue: deque[object] = deque()
        self._active_generations = 0
        self._active_image_paths: set[Path] = set()
        self._clear_cache_pending = False
        self._last_gen_at: dict[str, float] = {}
        self._total_count = 0
        self._user_counts: dict[str, int] = {}
        self._usage_counts: dict[str, int] = {}   # 今日各用户使用量(含管理员,仅展示用)
        self._total_usage: dict[str, int] = {}    # 各用户历史累计使用量(总使用量)
        self._user_names: dict[str, str] = {}     # QQ号 -> QQ昵称
        self._today = ""
        self.usage_path = self.data_dir / "usage.json"
        self._load_usage()

        # 自然语言触发(工具模式):工具实例按需注入请求,后台生图任务保持强引用
        self._draw_tool = EidolonDrawTool(plugin=self)
        self._tool_tasks: set[asyncio.Task] = set()

        # 插件页面后端 API
        context.register_web_api(
            f"/{plugin_name}/config", self.web_get_config, ["GET"], "获取插件配置")
        context.register_web_api(
            f"/{plugin_name}/config/save", self.web_save_config, ["POST"], "保存插件配置")
        context.register_web_api(
            f"/{plugin_name}/stats", self.web_get_stats, ["GET"], "获取今日用量")
        context.register_web_api(
            f"/{plugin_name}/cache", self.web_get_cache, ["GET"], "获取图片缓存状态")
        context.register_web_api(
            f"/{plugin_name}/cache/clear", self.web_clear_cache, ["POST"], "清空图片缓存")
        context.register_web_api(
            f"/{plugin_name}/test", self.web_test_key, ["POST"], "测试 API Key 连通性")
        context.register_web_api(
            f"/{plugin_name}/test-gen", self.web_test_gen, ["POST"], "测试生成(消耗 1 张配额)")
        context.register_web_api(
            f"/{plugin_name}/providers", self.web_get_providers, ["GET"], "获取 AstrBot 已配置的 LLM 模型列表")
        context.register_web_api(
            f"/{plugin_name}/prompt-defaults", self.web_get_prompt_defaults, ["GET"], "获取润色提示词默认值")
        context.register_web_api(
            f"/{plugin_name}/quota-detail", self.web_get_quota_detail, ["GET"], "获取今日配额使用明细")
        context.register_web_api(
            f"/{plugin_name}/quota/reset-today", self.web_quota_reset_today, ["POST"], "重置今日全部配额使用")
        context.register_web_api(
            f"/{plugin_name}/quota/reset-all", self.web_quota_reset_all, ["POST"], "完全重置(清空全部记录)")
        context.register_web_api(
            f"/{plugin_name}/quota/reset-user-today", self.web_quota_reset_user_today, ["POST"], "重置指定用户今日配额")
        context.register_web_api(
            f"/{plugin_name}/quota/reset-user", self.web_quota_reset_user, ["POST"], "完全重置指定用户记录")
        context.register_web_api(
            f"/{plugin_name}/about", self.web_get_about, ["GET"], "获取插件信息")

    # ------------------------------------------------------------------
    # 配置管理(插件页面读写,持久化到 data/plugin_data/<plugin>/config.json)
    # ------------------------------------------------------------------
    def _load_config(self) -> dict:
        cfg = dict(DEFAULT_CONFIG)
        migrated = False
        notes: list[str] = []
        try:
            if self.config_path.exists():
                saved = json.loads(self.config_path.read_text(encoding="utf-8"))
                for k in DEFAULT_CONFIG:
                    if k in saved:
                        cfg[k] = saved[k]
                # 旧默认值迁移:request_timeout=90(旧默认)升级为新默认
                if saved.get("request_timeout") == 90 and DEFAULT_CONFIG["request_timeout"] != 90:
                    cfg["request_timeout"] = DEFAULT_CONFIG["request_timeout"]
                    migrated = True
                    notes.append(
                        f"request_timeout 90 -> {DEFAULT_CONFIG['request_timeout']}")
                # 旧模式迁移:独立 LLM 意图判断已被函数工具取代,原 llm 模式转为 tool 模式
                if str(cfg.get("nl_trigger_mode", "")) == "llm":
                    cfg["nl_trigger_mode"] = "tool"
                    migrated = True
                    notes.append("nl_trigger_mode llm -> tool")
        except Exception as e:
            logger.warning(f"配置读取失败,使用默认配置: {e}")
        if migrated:
            try:
                self.config_path.write_text(
                    json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
                logger.info("已迁移旧配置:%s", "; ".join(notes))
            except Exception:
                pass
        return cfg

    def _save_config(self, data: dict):
        for k, v in data.items():
            if k in DEFAULT_CONFIG:
                if k == "max_concurrency":
                    try:
                        v = min(100, max(1, int(v)))
                    except (TypeError, ValueError):
                        v = DEFAULT_CONFIG[k]
                elif k == "image_cache_max_mb":
                    try:
                        v = max(1, int(v))
                    except (TypeError, ValueError):
                        v = DEFAULT_CONFIG[k]
                self.config[k] = v
        self.config_path.write_text(
            json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # 用量数据持久化(usage.json:今日计数 + 历史累计,重启不丢)
    # ------------------------------------------------------------------
    def _load_usage(self):
        try:
            if self.usage_path.exists():
                data = json.loads(self.usage_path.read_text(encoding="utf-8"))
                self._today = str(data.get("date", ""))
                self._total_count = int(data.get("total_count", 0))
                self._user_counts = {str(k): int(v) for k, v in (data.get("user_counts") or {}).items()}
                self._usage_counts = {str(k): int(v) for k, v in (data.get("usage_counts") or {}).items()}
                self._user_names = {str(k): str(v) for k, v in (data.get("user_names") or {}).items()}
                self._total_usage = {str(k): int(v) for k, v in (data.get("total_usage") or {}).items()}
        except Exception as e:
            logger.warning(f"用量数据读取失败,使用空数据: {e}")

    def _save_usage(self):
        try:
            self.usage_path.write_text(json.dumps({
                "date": self._today,
                "total_count": self._total_count,
                "user_counts": self._user_counts,
                "usage_counts": self._usage_counts,
                "user_names": self._user_names,
                "total_usage": self._total_usage,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"用量数据保存失败: {e}")

    def _record_user_name(self, sender_id: str, event) -> str:
        """记录 QQ 昵称(优先 OneBot sender.nickname,而非群名片)"""
        name = ""
        raw = getattr(event.message_obj, "raw_message", None)
        try:
            if isinstance(raw, dict):
                sender = raw.get("sender") or {}
                name = str(sender.get("nickname") or "").strip()
        except Exception:
            pass
        if not name:
            name = event.get_sender_name() or ""
        name = name.strip()
        if name and self._user_names.get(sender_id) != name:
            self._user_names[sender_id] = name
            self._save_usage()
        return name

    def _get_admin_ids(self) -> set[str]:
        """读取 AstrBot 全局管理员列表(配置 admins_id)"""
        try:
            cfg = self.context.get_config()
            return {str(a) for a in (cfg.get("admins_id") or []) if a}
        except Exception:
            return set()

    # ------------------------------------------------------------------
    # 插件页面 Web API
    # ------------------------------------------------------------------
    async def web_get_config(self):
        return json_response(self.config)

    async def web_save_config(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求内容格式无效", status_code=400)
        # 未知字段忽略(兼容前后端版本不同步),不阻断保存
        ignored = [k for k in payload if k not in DEFAULT_CONFIG]
        try:
            self._save_config(payload)
            self._prune_image_cache()
        except Exception as e:
            logger.error(f"配置保存失败: {e}")
            return error_response(f"保存配置失败：{e}", status_code=500)
        if ignored:
            logger.warning(f"忽略未知配置项: {ignored}")
        async with self._generation_condition:
            self._generation_condition.notify_all()
        logger.info("插件配置已更新(来自插件页面)")
        return json_response({"saved": True, "ignored": ignored})

    async def web_get_stats(self):
        self._reset_day_if_needed()
        return json_response({
            "total_limit": int(self.config.get("total_limit", 0)),
            "total_used": self._total_count,
            "total_generated": sum(self._total_usage.values()),
            "per_user_limit": int(self.config.get("per_user_limit", 0)),
        })

    async def web_get_cache(self):
        """返回生成图片缓存用量。"""
        files = self._cached_image_files()
        total_bytes = sum(size for _path, size, _mtime in files)
        return json_response({
            "file_count": len(files),
            "total_bytes": total_bytes,
            "total_mb": round(total_bytes / (1024 * 1024), 2),
            "max_mb": self._image_cache_limit_mb(),
        })

    async def web_clear_cache(self):
        """清空插件生成的图片缓存，不影响配置和用量数据。"""
        self._clear_cache_pending = bool(self._active_image_paths)
        deleted_count, deleted_bytes = self._clear_image_cache()
        logger.info(
            "图片缓存已清空 count=%s bytes=%s (来自插件页面)",
            deleted_count,
            deleted_bytes,
        )
        return json_response({
            "ok": True,
            "deleted_count": deleted_count,
            "deleted_bytes": deleted_bytes,
        })

    async def web_test_key(self):
        """测试火山方舟 API Key:GET /models 验证鉴权"""
        payload = await request.json(default={})
        key = (payload.get("key") or "").strip() or self._api_key()
        if not key:
            return error_response("请先填写火山方舟 API Key", status_code=400)
        try:
            async with httpx.AsyncClient(proxy=self._proxy(),
                                         timeout=httpx.Timeout(15.0)) as client:
                resp = await client.get(
                    self._join(ARK_BASE, "models"),
                    headers={"Authorization": f"Bearer {key}"})
            if resp.status_code == 200:
                return json_response({"ok": True, "message": "API Key 有效"})
            body = resp.text[:200]
            return error_response(f"鉴权失败（HTTP {resp.status_code}）：{body}",
                                  status_code=resp.status_code)
        except httpx.ProxyError:
            return error_response("无法连接代理，请检查代理地址或关闭代理", status_code=400)
        except httpx.HTTPError as e:
            return error_response(f"网络请求失败：{e}", status_code=400)

    async def web_test_gen(self):
        """测试生成:实际调用方舟生成 1 张图(1K 档),验证 key→出图→存盘全链路"""
        payload = await request.json(default={})
        key = (payload.get("key") or "").strip() or self._api_key()
        if not key:
            return error_response("请先填写火山方舟 API Key", status_code=400)
        model = (payload.get("model") or "").strip() \
            or (self.config.get("seedream_model") or "").strip()
        prompt = (payload.get("prompt") or "一只坐在云朵上的橘猫").strip()
        started = time.time()
        try:
            path, _mime = await self._call_seedream(prompt, "1:1", "1K", key, model)
            elapsed = time.time() - started
            return json_response({
                "ok": True,
                "elapsed": round(elapsed, 1),
                "path": path,
                "message": f"测试图片生成成功，耗时 {elapsed:.1f} 秒；文件已保存至 {path}",
            })
        except RuntimeError as e:
            elapsed = time.time() - started
            return error_response(f"生成失败（耗时 {elapsed:.1f} 秒）：{e}", status_code=400)
        except Exception as e:
            logger.exception(f"测试生成异常: {e}")
            return error_response(f"生成过程中发生异常：{e}", status_code=500)

    async def web_get_providers(self):
        """获取 AstrBot 中已配置的 LLM 提供商(供润色模型选择)"""
        try:
            providers = self.context.get_all_providers()
            items = []
            for p in providers:
                try:
                    meta = p.meta()
                    provider_id = meta.id
                    provider_type = meta.type
                except Exception:
                    provider_id = p.provider_config.get("id", "")
                    provider_type = p.provider_config.get("type", "")
                model_name = p.get_model() or p.provider_config.get("model", "")
                items.append({
                    "id": provider_id,
                    "name": p.provider_config.get("id", provider_id),
                    "type": provider_type,
                    "model_name": model_name,
                })
            return json_response({"providers": items})
        except Exception as e:
            logger.exception(f"获取模型提供商列表失败: {e}")
            return error_response(f"获取模型列表失败：{e}", status_code=500)

    async def web_get_prompt_defaults(self):
        """返回润色 system prompt 默认值与触发关键词默认值(供前端「恢复默认」按钮使用)"""
        return json_response({
            "zh": ENHANCE_SYSTEM_PROMPT_ZH,
            "en": ENHANCE_SYSTEM_PROMPT_EN,
            "nl_keywords": DEFAULT_CONFIG["nl_keywords"],
        })

    async def web_get_quota_detail(self):
        """今日配额明细:总限额/已用 + 各用户今日/总使用量(含管理员)"""
        self._reset_day_if_needed()
        admins = self._get_admin_ids()
        users = []
        for uid in set(self._usage_counts) | set(self._total_usage):
            used_today = self._usage_counts.get(uid, 0)
            used_total = self._total_usage.get(uid, 0)
            if used_today == 0 and used_total == 0:
                continue
            users.append({
                "qq": uid,
                "name": self._user_names.get(uid, ""),
                "used_today": used_today,
                "used_total": used_total,
                "is_admin": uid in admins,
            })
        return json_response({
            "total_limit": int(self.config.get("total_limit", 0)),
            "total_used": self._total_count,
            "total_generated": sum(self._total_usage.values()),
            "per_user_limit": int(self.config.get("per_user_limit", 0)),
            "admin_ignore_limit": bool(self.config.get("admin_ignore_limit", True)),
            "admins": sorted(admins),
            "users": users,
        })

    async def web_quota_reset_today(self):
        """仅重置今日配额使用:今日计数清零,保留历史累计记录"""
        self._reset_day_if_needed()
        self._total_count = 0
        self._user_counts.clear()
        self._usage_counts.clear()
        self._last_gen_at.clear()
        self._save_usage()
        logger.info("今日配额使用已重置(来自插件页面)")
        return json_response({"ok": True})

    async def web_quota_reset_all(self):
        """完全重置:今日配额使用与历史累计记录全部清零"""
        self._reset_day_if_needed()
        self._total_count = 0
        self._user_counts.clear()
        self._usage_counts.clear()
        self._total_usage.clear()
        self._last_gen_at.clear()
        self._save_usage()
        logger.info("全部记录已完全重置(来自插件页面)")
        return json_response({"ok": True})

    async def web_quota_reset_user_today(self):
        """仅重置指定用户的今日使用量,保留历史累计"""
        payload = await request.json(default={})
        qq = str(payload.get("qq") or "").strip()
        if not qq:
            return error_response("缺少用户 QQ 号", status_code=400)
        counted_today = self._user_counts.get(qq, 0)
        self._user_counts.pop(qq, None)
        self._usage_counts.pop(qq, None)
        self._last_gen_at.pop(qq, None)
        self._total_count = max(0, self._total_count - counted_today)
        self._save_usage()
        logger.info(f"已重置用户今日配额: {qq}(来自插件页面)")
        return json_response({"ok": True})

    async def web_quota_reset_user(self):
        """完全重置指定用户的今日与历史累计使用量"""
        payload = await request.json(default={})
        qq = str(payload.get("qq") or "").strip()
        if not qq:
            return error_response("缺少用户 QQ 号", status_code=400)
        counted_today = self._user_counts.get(qq, 0)
        self._user_counts.pop(qq, None)
        self._usage_counts.pop(qq, None)
        self._total_usage.pop(qq, None)
        self._last_gen_at.pop(qq, None)
        self._total_count = max(0, self._total_count - counted_today)
        self._save_usage()
        logger.info(f"已完全重置用户记录: {qq}(来自插件页面)")
        return json_response({"ok": True})

    async def web_get_about(self):
        """读取 metadata.yaml 返回插件信息"""
        info = {
            "name": PLUGIN_NAME,
            "display_name": "Eidolon",
            "version": "",
            "author": "",
            "repo": "",
            "desc": "",
        }
        try:
            import yaml
            meta_path = Path(__file__).resolve().parent / "metadata.yaml"
            if meta_path.exists():
                meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
                info["name"] = meta.get("name", PLUGIN_NAME)
                info["display_name"] = meta.get("display_name") or meta.get("name", PLUGIN_NAME)
                info["version"] = str(meta.get("version", ""))
                info["author"] = meta.get("author", "")
                info["repo"] = meta.get("repo", "")
                info["desc"] = meta.get("desc", "")
        except Exception as e:
            logger.warning(f"读取 metadata.yaml 失败: {e}")
        return json_response(info)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def _max_concurrency(self) -> int:
        try:
            return min(100, max(1, int(self.config.get("max_concurrency", 5))))
        except (TypeError, ValueError):
            return 5

    async def _reserve_generation_slot(self) -> tuple[object | None, int]:
        """立即占用空闲槽位，或按 FIFO 入队并返回当前排队位次。"""
        async with self._generation_condition:
            if (self._active_generations < self._max_concurrency()
                    and not self._generation_queue):
                self._active_generations += 1
                return None, 0
            ticket = object()
            self._generation_queue.append(ticket)
            return ticket, len(self._generation_queue)

    async def _wait_generation_slot(self, ticket: object):
        async with self._generation_condition:
            await self._generation_condition.wait_for(
                lambda: self._generation_queue
                and self._generation_queue[0] is ticket
                and self._active_generations < self._max_concurrency())
            self._generation_queue.popleft()
            self._active_generations += 1
            self._generation_condition.notify_all()

    async def _remove_queued_ticket(self, ticket: object):
        async with self._generation_condition:
            try:
                self._generation_queue.remove(ticket)
            except ValueError:
                return
            self._generation_condition.notify_all()

    async def _release_generation_slot(self):
        async with self._generation_condition:
            self._active_generations = max(0, self._active_generations - 1)
            self._generation_condition.notify_all()

    def _api_key(self) -> str:
        """Seedream(火山方舟)API Key"""
        return (self.config.get("seedream_api_key") or "").strip()

    def _proxy(self):
        if self.config.get("enable_proxy", True):
            return (self.config.get("proxy") or "").strip() or None
        return None

    @staticmethod
    def _join(base: str, endpoint: str) -> str:
        return base.rstrip("/") + "/" + endpoint.lstrip("/")

    def _reset_day_if_needed(self):
        today = time.strftime("%Y-%m-%d")
        if today != self._today:
            self._today = today
            self._total_count = 0
            self._user_counts.clear()
            self._usage_counts.clear()
            self._save_usage()

    def _check_quota(self, group_id: str, sender_id: str, is_admin: bool) -> tuple[bool, str]:
        """返回 (是否放行, 拒绝原因);管理员(admin_ignore_limit 开启时)豁免冷却/总限额/每人限额"""
        self._reset_day_if_needed()
        if is_admin and self.config.get("admin_ignore_limit", True):
            return True, ""
        now = time.time()
        cd = int(self.config.get("cooldown_seconds", 30))
        last = self._last_gen_at.get(group_id, 0)
        if now - last < cd:
            return False, f"本群生成冷却中，请在 {int(cd - (now - last))} 秒后重试。"
        total = int(self.config.get("total_limit", 200))
        per = int(self.config.get("per_user_limit", 0))
        if total > 0 and self._total_count >= total:
            return False, "今日图片生成总量已达上限，请明天再试。"
        if per > 0 and self._user_counts.get(sender_id, 0) >= per:
            return False, "你今日的图片生成数量已达个人上限，请明天再试。"
        return True, ""

    def _apply_quota(self, group_id: str, sender_id: str, num: int, is_admin: bool):
        # 今日使用量与历史累计都记录(含管理员,用于配额页展示)
        self._usage_counts[sender_id] = self._usage_counts.get(sender_id, 0) + num
        self._total_usage[sender_id] = self._total_usage.get(sender_id, 0) + num
        if is_admin and self.config.get("admin_ignore_limit", True):
            self._save_usage()
            return
        self._last_gen_at[group_id] = time.time()
        self._total_count += num
        self._user_counts[sender_id] = self._user_counts.get(sender_id, 0) + num
        self._save_usage()

    def _image_cache_limit_mb(self) -> int:
        try:
            return max(1, int(self.config.get("image_cache_max_mb", 500)))
        except (TypeError, ValueError):
            return 500

    def _cached_image_files(self) -> list[tuple[Path, int, float]]:
        """列出插件生成的缓存图片；兼容旧版本保存在 data_dir 根目录的文件。"""
        files: list[tuple[Path, int, float]] = []
        candidates = list(self.image_dir.glob("eid_*"))
        if self.image_dir != self.data_dir:
            candidates.extend(self.data_dir.glob("eid_*"))
        for path in candidates:
            try:
                if path.is_file():
                    stat = path.stat()
                    files.append((path, stat.st_size, stat.st_mtime))
            except OSError as e:
                logger.warning(f"读取缓存图片信息失败 {path}: {e}")
        return files

    def _prune_image_cache(self, preserve: Path | None = None) -> tuple[int, int]:
        """按修改时间删除最旧缓存，直至总大小不超过配置上限。"""
        limit_bytes = self._image_cache_limit_mb() * 1024 * 1024
        files = self._cached_image_files()
        total_bytes = sum(size for _path, size, _mtime in files)
        deleted_count = 0
        deleted_bytes = 0
        preserve_resolved = preserve.resolve() if preserve else None
        protected = {path.resolve() for path in self._active_image_paths}
        for path, size, _mtime in sorted(files, key=lambda item: item[2]):
            if total_bytes <= limit_bytes:
                break
            try:
                resolved = path.resolve()
                if resolved in protected or (
                        preserve_resolved is not None and resolved == preserve_resolved):
                    continue
                path.unlink()
                total_bytes -= size
                deleted_count += 1
                deleted_bytes += size
            except OSError as e:
                logger.warning(f"清理缓存图片失败 {path}: {e}")
        if deleted_count:
            logger.info(
                "图片缓存自动清理 count=%s bytes=%s remaining=%s limit=%s",
                deleted_count,
                deleted_bytes,
                total_bytes,
                limit_bytes,
            )
        return deleted_count, deleted_bytes

    def _clear_image_cache(self) -> tuple[int, int]:
        deleted_count = 0
        deleted_bytes = 0
        protected = {path.resolve() for path in self._active_image_paths}
        for path, size, _mtime in self._cached_image_files():
            try:
                if path.resolve() in protected:
                    continue
                path.unlink()
                deleted_count += 1
                deleted_bytes += size
            except OSError as e:
                logger.warning(f"清空缓存图片失败 {path}: {e}")
        return deleted_count, deleted_bytes

    def _save_image(self, img_b64: str, mime: str = "image/png") -> str:
        ext = EXT_BY_MIME.get(mime, "png")
        fname = f"eid_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.{ext}"
        path = self.image_dir / fname
        try:
            raw = img_b64.strip()
            if raw.startswith("data:"):  # 兼容 data:image/png;base64, 前缀
                raw = raw.split(",", 1)[-1]
            path.write_bytes(base64.b64decode(raw))
            self._prune_image_cache(preserve=path)
        except Exception as e:
            raise RuntimeError(f"图片数据解码失败：{e}") from e
        return str(path)

    def _collect_reference_images(self, event: AstrMessageEvent) -> list[Comp.Image]:
        """收集消息附带的参考图:优先同条消息中的图片,其次回复消息中的图片"""
        chain = getattr(event.message_obj, "message", []) or []
        images: list[Comp.Image] = []
        seen: set[str] = set()
        def add(img: Comp.Image):
            key = f"{img.url or ''}|{img.file or ''}|{img.path or ''}"
            if key and key in seen:
                return
            if key:
                seen.add(key)
            images.append(img)
        for seg in chain:
            if isinstance(seg, Comp.Image):
                add(seg)
        for seg in chain:
            if isinstance(seg, Comp.Reply):
                for sub in (getattr(seg, "chain", None) or []):
                    if isinstance(sub, Comp.Image):
                        add(sub)
        return images

    @staticmethod
    def _sniff_image_mime(path: Path) -> str:
        """通过文件头嗅探图片格式(小写);无法识别返回空串"""
        try:
            with open(path, "rb") as f:
                head = f.read(16)
        except OSError:
            return ""
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if head.startswith(b"\xff\xd8\xff"):
            return "jpeg"
        if head.startswith(b"GIF8"):
            return "gif"
        if head.startswith(b"BM"):
            return "bmp"
        if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
            return "webp"
        if head[:4] in (b"II*\x00", b"MM\x00*"):
            return "tiff"
        if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"hevc", b"hevx"):
            return "heic"
        return ""

    async def _image_to_data_uri(self, img: Comp.Image) -> str:
        """将消息图片组件转换为 data URI(base64),供 Seedream 参考图上传"""
        local = ""
        for cand in (getattr(img, "path", ""), getattr(img, "file", "")):
            if not cand:
                continue
            if str(cand).startswith("file:///"):
                local = str(cand)[8:]
                break
            if Path(cand).is_absolute() and Path(cand).is_file():
                local = str(cand)
                break
        if not local:
            try:
                local = await img.convert_to_file_path()
            except Exception as e:
                raise RuntimeError(f"获取图片数据失败：{e}") from e
        path = Path(local)
        if not path.is_file():
            raise RuntimeError("图片数据不存在，请重新发送")
        size = path.stat().st_size
        if size > 30 * 1024 * 1024:
            raise RuntimeError("参考图超过 30MB 上限，请压缩后重试")
        mime = self._sniff_image_mime(path)
        if not mime:
            suffix = path.suffix.lower().lstrip(".")
            mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png",
                    "webp": "webp", "gif": "gif", "bmp": "bmp"}.get(suffix, "")
        if not mime:
            raise RuntimeError("不支持的参考图格式（支持 jpeg/png/webp 等常见格式）")
        b64_data = base64.b64encode(path.read_bytes()).decode()
        return f"data:image/{mime};base64,{b64_data}"

    async def _post(self, url: str, headers: dict, payload: dict) -> dict:
        """统一 POST:代理/超时/重试/错误分类,全程日志

        重试策略:仅网络类错误(连接/代理)重试;ReadTimeout(服务端生成慢)不重试,
        避免重复计费与更长等待。
        """
        timeout = httpx.Timeout(float(self.config.get("request_timeout", 300)))
        proxy = self._proxy()
        logger.info(f"API 请求 {url} proxy={proxy!r}")
        last_err: Exception | None = None
        for attempt in range(3):
            started = time.time()
            retryable = True
            try:
                async with httpx.AsyncClient(proxy=proxy, timeout=timeout) as client:
                    resp = await client.post(url, headers=headers, json=payload)
                elapsed = time.time() - started
                if resp.status_code == 429:
                    raise RuntimeError("火山方舟触发限流（HTTP 429），请稍后重试或降低并发数")
                resp.raise_for_status()
                logger.info(f"API 响应 {resp.status_code} 耗时 {elapsed:.1f}s (第{attempt + 1}次)")
                return resp.json()
            except httpx.ConnectTimeout:
                last_err = RuntimeError("连接火山方舟 API 超时，请检查网络和代理设置")
            except httpx.ReadTimeout:
                last_err = RuntimeError(
                    f"火山方舟生成超时（超过 {timeout.read:.0f} 秒），请调大请求超时时间，"
                    "或尝试更短的提示词和更低的分辨率")
                retryable = False
            except httpx.WriteTimeout:
                last_err = RuntimeError("发送请求超时，请检查网络连接")
                retryable = False
            except httpx.TimeoutException:
                last_err = RuntimeError("API 请求超时，请检查网络和代理设置")
            except httpx.ConnectError:
                last_err = RuntimeError("无法连接火山方舟 API，请检查网络和代理设置")
            except httpx.ProxyError:
                last_err = RuntimeError("无法连接代理，请检查代理地址或关闭代理")
            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500:
                    last_err = RuntimeError(f"火山方舟服务异常（HTTP {e.response.status_code}）")
                else:
                    raise RuntimeError(f"火山方舟请求失败（HTTP {e.response.status_code}）："
                                       f"{e.response.text[:200]}") from e
            except httpx.HTTPError as e:
                last_err = RuntimeError(f"网络请求失败：{e}")
            elapsed = time.time() - started
            logger.error(f"API 请求失败(第{attempt + 1}次) 耗时 {elapsed:.1f}s: {last_err}")
            if attempt < 2 and retryable:
                await asyncio.sleep(2 * (attempt + 1))
        raise last_err or RuntimeError("图片生成失败，请稍后重试")

    # ------------------------------------------------------------------
    # 适配层:Seedream(火山方舟)
    # ------------------------------------------------------------------
    def _build_prompt(self, prompt: str, aspect: str) -> str:
        """拼接宽高比描述(API 方式1:档位 + prompt 自然语言描述)"""
        desc = ASPECT_DESC.get(aspect)
        if desc:
            return f"{prompt.strip()},{desc}"
        return prompt.strip()

    @staticmethod
    def _parse_draw_request(message: str, fallback_prompt: str = "") -> tuple[str, str, str]:
        """从完整消息解析生图参数,避免指令框架截断包含空格的长提示词"""
        text = DRAW_COMMAND_RE.sub("", (message or "").strip(), count=1)
        if not text:
            text = (fallback_prompt or "").strip()
        aspect, image_size = "", ""
        for match in FLAG_RE.finditer(text):
            flag = match.group(1).lower()
            value = match.group(2).strip()
            if flag == "a" and value in VALID_ASPECTS:
                aspect = value
            elif flag == "s":
                normalized_size = value.upper()
                if normalized_size in VALID_SIZES:
                    image_size = normalized_size
        return FLAG_RE.sub("", text).strip(), aspect, image_size

    async def _call_seedream(self, prompt: str, aspect: str, image_size: str = "",
                             api_key: str = "", model: str = "",
                             images: list[str] | None = None) -> tuple[str, str]:
        """火山方舟 Seedream 图片生成(OpenAI 兼容子集)

        官方文档要点:
          - size: 分辨率档位(1K/1.5K/2K),宽高比通过 prompt 自然语言描述(方式1,推荐)
          - response_format 默认 url(响应体小,避免大 JSON 传输超时),失败降级 b64_json
          - watermark/output_format 独立参数,无 seed 参数
          - image: 参考图(图生图),支持 URL 或 data URI,可传单张或数组(多图融合)
          - 顶层 error 为整体错误;data[].error 为单图错误
        """
        key = (api_key or "").strip() or self._api_key()
        if not key:
            raise RuntimeError("尚未配置火山方舟 API Key")
        model = (model or "").strip() or (self.config.get("seedream_model") or "").strip() \
            or "doubao-seedream-5-0-pro"
        image_size = image_size or str(self.config.get("image_size", "2K"))
        if image_size not in VALID_SIZES:
            image_size = "2K"
        output_format = str(self.config.get("output_format", "png"))
        if output_format not in ("png", "jpeg"):
            output_format = "png"
        url = self._join(ARK_BASE, "images/generations")
        payload = {
            "model": model,
            "prompt": self._build_prompt(prompt, aspect),
            "size": image_size,
            "response_format": "url",
            "output_format": output_format,
            "watermark": bool(self.config.get("watermark", True)),
        }
        if images:
            payload["image"] = images[0] if len(images) == 1 else images
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            data = await self._post(url, headers, payload)
        except RuntimeError as e:
            # 平台不支持 url 时降级为 b64_json
            if "response_format" in str(e) or "invalid" in str(e).lower() or "参数" in str(e):
                payload["response_format"] = "b64_json"
                data = await self._post(url, headers, payload)
            else:
                raise
        if data.get("error"):
            err = data["error"]
            raise RuntimeError(f"火山方舟返回错误：{err.get('code', '')} {err.get('message', '')}".strip())
        items = data.get("data") or []
        if not items:
            raise RuntimeError("火山方舟响应中没有图片数据")
        item = items[0]
        if item.get("error"):
            err = item["error"]
            raise RuntimeError(f"火山方舟返回错误：{err.get('code', '')} {err.get('message', '')}".strip())
        if item.get("url"):
            return await self._download(item["url"]), f"image/{output_format}"
        if item.get("b64_json"):
            mime = f"image/{output_format}"
            return self._save_image(item["b64_json"], mime), mime
        raise RuntimeError("火山方舟响应中缺少图片 URL 或 Base64 数据")

    async def _download(self, url: str) -> str:
        try:
            async with httpx.AsyncClient(
                    proxy=self._proxy(),
                    timeout=httpx.Timeout(float(self.config.get("request_timeout", 180)))) as client:
                resp = await client.get(url)
                resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"图片下载失败（HTTP {e.response.status_code}）") from e
        except httpx.TimeoutException:
            raise RuntimeError("图片下载超时，图片链接可能已失效") from None
        except httpx.HTTPError as e:
            raise RuntimeError(f"图片下载失败：{e}") from e
        fmt = str(self.config.get("output_format", "png"))
        ext = "jpg" if fmt == "jpeg" else "png"
        path = self.image_dir / f"eid_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.{ext}"
        path.write_bytes(resp.content)
        self._prune_image_cache(preserve=path)
        logger.info(f"图片下载完成 {path} ({len(resp.content)} bytes)")
        return str(path)

    async def _generate_one(self, prompt: str, aspect: str, image_size: str,
                            images: list[str] | None = None) -> tuple[str, str]:
        """适配层入口(当前仅 Seedream)"""
        return await self._call_seedream(prompt, aspect, image_size, images=images)

    # ------------------------------------------------------------------
    # 自然语言触发:工具模式(由主对话模型调用生图工具)
    # ------------------------------------------------------------------
    def _keyword_hit(self, prompt: str) -> bool:
        """检查消息是否命中自定义关键词(支持中英文逗号、顿号、换行分隔)"""
        keywords = re.split(r"[,，、\n]+", str(self.config.get("nl_keywords", "")))
        text = prompt.lower()
        return any(k.strip() and k.strip().lower() in text for k in keywords)

    def _tool_mode_enabled(self) -> bool:
        """自然语言触发是否处于工具模式"""
        return (bool(self.config.get("enable_nl_trigger", False))
                and str(self.config.get("nl_trigger_mode", "keyword")) == "tool")

    def _nl_draw_candidate(self, event: AstrMessageEvent) -> bool:
        """群内 @机器人 且提示词长度达标时,才把生图工具挂到本次请求上"""
        if not event.get_group_id():
            return False
        chain = getattr(event.message_obj, "message", []) or []
        at_me = any(
            isinstance(seg, Comp.At) and str(getattr(seg, "qq", "")) == str(event.get_self_id())
            for seg in chain
        )
        if not at_me:
            return False
        text = re.sub(r"@\S+", "", event.message_str).strip()
        try:
            min_len = int(self.config.get("nl_min_prompt_len", 5))
        except (TypeError, ValueError):
            min_len = 5
        return len(text) >= min_len

    def _track_tool_task(self, task: asyncio.Task) -> None:
        """持有后台生图任务的强引用,避免被垃圾回收;结束后自动移除"""
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)

    async def _run_tool_generation(self, event: AstrMessageEvent, prompt: str, aspect: str,
                                   image_size: str, images: list[Comp.Image] | None) -> str:
        """工具触发的生图:结果直接发送到会话,并把结论作为工具返回值交给模型"""
        image_sent = False
        texts: list[str] = []
        try:
            async for result in self._generate_results(
                    event, prompt, aspect, image_size, images):
                chain = list(getattr(result, "chain", []) or [])
                if not chain:
                    continue
                if any(isinstance(seg, Comp.Image) for seg in chain):
                    image_sent = True
                await event.send(MessageChain(chain))
                text = "".join(
                    seg.text for seg in chain
                    if isinstance(seg, Comp.Plain) and getattr(seg, "text", ""))
                if text:
                    texts.append(text)
        except Exception as e:
            logger.exception(f"工具触发生图异常: {e}")
            return f"生图失败:{e}。请如实告知用户失败原因,不要声称图片已生成。"
        if image_sent:
            return (
                "图片已生成并发送到当前会话。请用一句话简短回应(例如「画好了」),"
                "不要重复描述画面内容,也不要再次调用本工具。")
        if texts:
            return (
                f"生图未成功,已把原因发送给用户:{' '.join(texts)}"
                "请如实转述失败原因,不要声称图片已生成。")
        return "生图没有产生任何结果,请如实告知用户生成失败。"

    @filter.on_llm_request()
    async def inject_draw_tool(self, event: AstrMessageEvent, req: ProviderRequest):
        """工具模式:群内 @机器人 时把生图工具注入本次请求的工具列表。

        只在必要时挂载,避免工具污染所有会话,也避免模型在普通闲聊里自发画图。
        """
        if not self._tool_mode_enabled() or not self._nl_draw_candidate(event):
            return
        if req.func_tool is None:
            req.func_tool = ToolSet()
        req.func_tool.add_tool(self._draw_tool)
        logger.info(
            f"已注入生图工具 group={event.get_group_id()} "
            f"prompt={re.sub(r'@\S+', '', event.message_str).strip()[:50]!r}")

    async def terminate(self):
        """插件卸载:取消尚未完成的工具生图任务"""
        pending = [t for t in self._tool_tasks if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tool_tasks.clear()

    # ------------------------------------------------------------------
    # LLM 润色(可选,使用 AstrBot 已配置的模型提供商)
    # ------------------------------------------------------------------
    async def _enhance_prompt(self, prompt: str) -> str:
        lang = str(self.config.get("enhance_lang", "zh"))
        if lang == "en":
            system_prompt = (self.config.get("enhance_system_prompt_en") or "").strip() \
                or ENHANCE_SYSTEM_PROMPT_EN
        else:
            system_prompt = (self.config.get("enhance_system_prompt_zh") or "").strip() \
                or ENHANCE_SYSTEM_PROMPT_ZH
        provider_id = (self.config.get("enhance_provider_id") or "").strip()
        if not provider_id:
            raise RuntimeError("尚未选择提示词润色模型")
        provider = self.context.get_provider_by_id(provider_id)
        if provider is None:
            raise RuntimeError(f"未找到提示词润色模型「{provider_id}」，请重新选择")
        try:
            timeout = max(1.0, float(self.config.get("enhance_timeout", 30) or 30))
        except (TypeError, ValueError):
            timeout = 30.0
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    session_id="",
                    system_prompt=system_prompt,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"提示词润色超时(>{timeout:.0f}秒)") from None
        text = getattr(resp, "completion_text", "") or ""
        return text.strip()

    # ------------------------------------------------------------------
    # 共享生图流程
    # ------------------------------------------------------------------
    async def _generate_results(self, event: AstrMessageEvent, prompt: str,
                                aspect: str = "", image_size: str = "",
                                images: list[Comp.Image] | None = None):
        """核心生图流程:产出待发送的结果,由调用方决定发送方式。

        - 指令/关键词触发:由 _generate_and_send 直接作为事件结果返回;
        - 工具触发:由 _run_tool_generation 通过 event.send 主动发送。
        """
        sender_id = event.get_sender_id()
        is_admin = event.is_admin()
        group_id = event.get_group_id() or event.unified_msg_origin
        self._record_user_name(sender_id, event)

        ok, reason = self._check_quota(group_id, sender_id, is_admin)
        if not ok:
            yield event.plain_result(reason)
            return

        explicit_aspect = bool(aspect)
        aspect = aspect or str(self.config.get("aspect_ratio", "1:1"))
        image_size = image_size or str(self.config.get("image_size", "2K"))

        ref_images: list[str] = []
        if images:
            try:
                for img in images:
                    ref_images.append(await self._image_to_data_uri(img))
            except RuntimeError as e:
                yield event.plain_result(f"参考图处理失败：{e}")
                return
        if ref_images:
            logger.info(
                f"图生图 group={group_id} sender={sender_id} "
                f"参考图={len(ref_images)}张 prompt={prompt!r}")
        # 图生图时模型默认参考输入图构图,仅当用户显式指定 -a 时才附加宽高比描述
        prompt_aspect = aspect if (not ref_images or explicit_aspect) else ""

        ticket, queue_position = await self._reserve_generation_slot()
        slot_acquired = ticket is None
        try:
            if ticket is not None:
                yield event.plain_result(
                    f"当前并发任务已满，你的请求已加入队列。"
                    f"前方有 {queue_position - 1} 个任务，当前排队第 {queue_position} 位。")
                await self._wait_generation_slot(ticket)
                slot_acquired = True

            logger.info(f"生图 group={group_id} sender={sender_id} prompt={prompt!r}")
            if ref_images:
                yield event.plain_result(
                    f"已收到 {len(ref_images)} 张参考图，正在基于参考图生成。"
                    "高分辨率或复杂修改通常需要 1 至 3 分钟，请耐心等待。")
            else:
                yield event.plain_result(
                    "已开始生成图片。高分辨率或复杂提示词通常需要 1 至 3 分钟，请耐心等待。")
            success_count = 0
            try:
                # 图生图不做 LLM 润色(修改指令需保持原意),仅文生图可选润色
                if not ref_images and self.config.get("enable_prompt_enhance", False):
                    try:
                        enhanced = await self._enhance_prompt(prompt)
                        if enhanced:
                            logger.info(f"润色成功({len(enhanced)}字),完整润色后提示词: {enhanced!r}")
                            prompt = enhanced
                        else:
                            logger.warning("提示词润色结果为空,回退原文")
                    except Exception as e:
                        logger.warning(f"提示词润色失败,回退原文: {e}")
                started = time.time()
                path, _mime = await self._generate_one(
                    prompt, prompt_aspect, image_size, ref_images or None)
                success_count = 1
                logger.info(f"出图成功 耗时 {time.time() - started:.1f}s 文件={path}")
                active_path = Path(path).resolve()
                self._active_image_paths.add(active_path)
                try:
                    yield event.image_result(path)
                finally:
                    self._active_image_paths.discard(active_path)
                    if self._clear_cache_pending:
                        self._clear_image_cache()
                        self._clear_cache_pending = bool(self._active_image_paths)
                    else:
                        self._prune_image_cache()
            except Exception as e:
                logger.exception(f"生图异常: {e}")
                yield event.plain_result(f"图片生成失败：{e}")
            finally:
                if success_count > 0:
                    self._apply_quota(group_id, sender_id, success_count, is_admin)
        finally:
            if slot_acquired:
                await self._release_generation_slot()
            elif ticket is not None:
                await self._remove_queued_ticket(ticket)

    async def _generate_and_send(self, event: AstrMessageEvent, prompt: str,
                                 aspect: str = "", image_size: str = "",
                                 images: list[Comp.Image] | None = None):
        """指令/关键词触发生图:禁止默认 LLM 回复,结果由调用方截断事件后发出"""
        # 禁止 AstrBot 默认 LLM；事件在所有生图结果 yield 完成后由调用方截断。
        event.should_call_llm(True)
        async for result in self._generate_results(
                event, prompt, aspect, image_size, images):
            yield result

    # ------------------------------------------------------------------
    # 触发方式一:指令
    # ------------------------------------------------------------------
    @filter.command("画图", alias={"draw"})
    async def draw(self, event: AstrMessageEvent, prompt: str = ""):
        """文生图/图生图: /画图 <提示词> [-a 宽高比] [-s 分辨率];附带或回复图片时自动图生图"""
        prompt, aspect, image_size = self._parse_draw_request(
            event.message_str, prompt)
        images = []
        if self.config.get("enable_img2img", True):
            images = self._collect_reference_images(event)
            if len(images) > IMG2IMG_MAX_IMAGES:
                logger.info(
                    f"参考图数量 {len(images)} 超过上限 {IMG2IMG_MAX_IMAGES},"
                    f"已截取前 {IMG2IMG_MAX_IMAGES} 张")
                images = images[:IMG2IMG_MAX_IMAGES]
        if not prompt:
            event.should_call_llm(True)
            yield event.plain_result(
                "用法：/画图 <提示词> [-a 宽高比] [-s 分辨率]\n"
                "示例：/画图 一只坐在云朵上的橘猫\n"
                "      /画图 赛博朋克城市夜景 -a 16:9 -s 1.5K\n"
                "图生图：提示词后附带图片或回复带图消息，将基于参考图生成")
            return
        logger.info(
            f"指令参数解析 aspect={aspect or '默认'} "
            f"size={image_size or '默认'} images={len(images)} prompt={prompt!r}")
        async for r in self._generate_and_send(
                event, prompt, aspect, image_size, images):
            yield r
        event.stop_event()

    # ------------------------------------------------------------------
    # 触发方式二:群内 @机器人 自然语言(可选)
    #   - keyword 模式:命中关键词由插件直接生图(不进入 LLM)
    #   - tool 模式:不拦截消息,改由 inject_draw_tool 把生图工具交给模型自行判断
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        if not self.config.get("enable_nl_trigger", False):
            return
        if self._tool_mode_enabled():
            # 工具模式:消息交给 AstrBot 常规对话流程,由模型决定是否调用生图工具
            return
        chain = getattr(event.message_obj, "message", [])
        at_me = any(
            isinstance(seg, Comp.At) and str(getattr(seg, "qq", "")) == str(event.get_self_id())
            for seg in chain
        )
        if not at_me:
            return
        prompt = re.sub(r"@\S+", "", event.message_str).strip()
        if not prompt or len(prompt) < int(self.config.get("nl_min_prompt_len", 5)):
            return
        logger.info(
            f"NL触发检查 group={event.get_group_id()} mode=keyword prompt={prompt[:50]!r}")
        if not self._keyword_hit(prompt):
            logger.info(f"NL关键词未命中,放行给正常对话: {prompt[:50]!r}")
            return
        images = []
        if self.config.get("enable_img2img", True):
            images = self._collect_reference_images(event)
            if len(images) > IMG2IMG_MAX_IMAGES:
                logger.info(
                    f"参考图数量 {len(images)} 超过上限 {IMG2IMG_MAX_IMAGES},"
                    f"已截取前 {IMG2IMG_MAX_IMAGES} 张")
                images = images[:IMG2IMG_MAX_IMAGES]
        logger.info(f"NL触发生图 group={event.get_group_id()} images={len(images)} prompt={prompt[:50]!r}")
        event.should_call_llm(True)
        async for r in self._generate_and_send(event, prompt, images=images):
            yield r
        event.stop_event()
