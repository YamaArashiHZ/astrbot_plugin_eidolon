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

import httpx

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"          # 火山方舟(Seedream)
PLUGIN_NAME = "astrbot_plugin_eidolon"
VALID_ASPECTS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9"}
VALID_SIZES = {"1K", "1.5K", "2K"}                            # Seedream 5.0 pro 档位
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
    "enable_nl_trigger": False,
    "nl_min_prompt_len": 5,
    "enable_prompt_enhance": False,
    "enhance_lang": "zh",
    "enhance_provider_id": "",
    "enhance_system_prompt_zh": ENHANCE_SYSTEM_PROMPT_ZH,
    "enhance_system_prompt_en": ENHANCE_SYSTEM_PROMPT_EN,
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


class EidolonPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        plugin_name = getattr(self, "name", PLUGIN_NAME)
        self.plugin_name = plugin_name
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
        self.image_dir = self.data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_dir / "config.json"
        self.config = self._load_config()

        self._generation_condition = asyncio.Condition()
        self._generation_queue: deque[object] = deque()
        self._active_generations = 0
        self._last_gen_at: dict[str, float] = {}
        self._total_count = 0
        self._user_counts: dict[str, int] = {}
        self._usage_counts: dict[str, int] = {}   # 今日各用户使用量(含管理员,仅展示用)
        self._total_usage: dict[str, int] = {}    # 各用户历史累计使用量(总使用量)
        self._user_names: dict[str, str] = {}     # QQ号 -> QQ昵称
        self._today = ""
        self.usage_path = self.data_dir / "usage.json"
        self._load_usage()

        # 插件页面后端 API
        context.register_web_api(
            f"/{plugin_name}/config", self.web_get_config, ["GET"], "获取插件配置")
        context.register_web_api(
            f"/{plugin_name}/config/save", self.web_save_config, ["POST"], "保存插件配置")
        context.register_web_api(
            f"/{plugin_name}/stats", self.web_get_stats, ["GET"], "获取今日用量")
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
        except Exception as e:
            logger.warning(f"配置读取失败,使用默认配置: {e}")
        if migrated:
            try:
                self.config_path.write_text(
                    json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
                logger.info("已迁移旧配置:request_timeout 90 -> %s", DEFAULT_CONFIG["request_timeout"])
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
        """返回润色 system prompt 默认值(供前端「恢复默认」按钮使用)"""
        return json_response({
            "zh": ENHANCE_SYSTEM_PROMPT_ZH,
            "en": ENHANCE_SYSTEM_PROMPT_EN,
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

    def _save_image(self, img_b64: str, mime: str = "image/png") -> str:
        ext = EXT_BY_MIME.get(mime, "png")
        fname = f"eid_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.{ext}"
        path = self.image_dir / fname
        try:
            raw = img_b64.strip()
            if raw.startswith("data:"):  # 兼容 data:image/png;base64, 前缀
                raw = raw.split(",", 1)[-1]
            path.write_bytes(base64.b64decode(raw))
        except Exception as e:
            raise RuntimeError(f"图片数据解码失败：{e}") from e
        return str(path)

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
                             api_key: str = "", model: str = "") -> tuple[str, str]:
        """火山方舟 Seedream 图片生成(OpenAI 兼容子集)

        官方文档要点:
          - size: 分辨率档位(1K/1.5K/2K),宽高比通过 prompt 自然语言描述(方式1,推荐)
          - response_format 默认 url(响应体小,避免大 JSON 传输超时),失败降级 b64_json
          - watermark/output_format 独立参数,无 seed 参数
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
        logger.info(f"图片下载完成 {path} ({len(resp.content)} bytes)")
        return str(path)

    async def _generate_one(self, prompt: str, aspect: str, image_size: str) -> tuple[str, str]:
        """适配层入口(当前仅 Seedream)"""
        return await self._call_seedream(prompt, aspect, image_size)

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
        resp = await provider.text_chat(
            prompt=prompt,
            session_id="",
            system_prompt=system_prompt,
        )
        text = getattr(resp, "completion_text", "") or ""
        return text.strip()

    # ------------------------------------------------------------------
    # 共享生图流程
    # ------------------------------------------------------------------
    async def _generate_and_send(self, event: AstrMessageEvent, prompt: str,
                                 aspect: str = "", image_size: str = ""):
        sender_id = event.get_sender_id()
        is_admin = event.is_admin()
        group_id = event.get_group_id() or event.unified_msg_origin
        self._record_user_name(sender_id, event)

        ok, reason = self._check_quota(group_id, sender_id, is_admin)
        if not ok:
            yield event.plain_result(reason)
            event.stop_event()
            return

        aspect = aspect or str(self.config.get("aspect_ratio", "1:1"))
        image_size = image_size or str(self.config.get("image_size", "2K"))

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
            yield event.plain_result(
                "已开始生成图片。高分辨率或复杂提示词通常需要 1 至 3 分钟，请耐心等待。")
            success_count = 0
            try:
                if self.config.get("enable_prompt_enhance", False):
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
                path, _mime = await self._generate_one(prompt, aspect, image_size)
                success_count = 1
                logger.info(f"出图成功 耗时 {time.time() - started:.1f}s 文件={path}")
                yield event.image_result(path)
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

    # ------------------------------------------------------------------
    # 触发方式一:指令
    # ------------------------------------------------------------------
    @filter.command("画图", alias={"draw"})
    async def draw(self, event: AstrMessageEvent, prompt: str = ""):
        """文生图: /画图 <提示词> [-a 宽高比] [-s 分辨率]"""
        prompt, aspect, image_size = self._parse_draw_request(
            event.message_str, prompt)
        if not prompt:
            yield event.plain_result(
                "用法：/画图 <提示词> [-a 宽高比] [-s 分辨率]\n"
                "示例：/画图 一只坐在云朵上的橘猫\n"
                "      /画图 赛博朋克城市夜景 -a 16:9 -s 1.5K"
            )
            return
        logger.info(
            f"指令参数解析 aspect={aspect or '默认'} "
            f"size={image_size or '默认'} prompt={prompt!r}")
        async for r in self._generate_and_send(event, prompt, aspect, image_size):
            yield r
        event.stop_event()

    # ------------------------------------------------------------------
    # 触发方式二:群内 @机器人 自然语言(可选)
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        if not self.config.get("enable_nl_trigger", False):
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
        logger.info(f"NL触发生图 group={event.get_group_id()} prompt={prompt[:50]!r}")
        async for r in self._generate_and_send(event, prompt):
            yield r
        event.stop_event()
