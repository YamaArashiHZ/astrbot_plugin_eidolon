"""
AstrBot 插件:群文生图(仅适配火山方舟 Seedream 5.0 Pro)

功能:
  1. 指令触发: /画图 <提示词> [-n 张数] [-a 宽高比] [-s 分辨率]
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
VALID_ASPECTS = {"1:1", "16:9", "9:16", "4:3", "3:4"}
VALID_SIZES = {"1K", "1.5K", "2K"}                            # Seedream 5.0 pro 档位
EXT_BY_MIME = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
FLAG_RE = re.compile(r"(?:^|\s)-(n|a|s)\s+(\S+)")
SAFETY_SUFFIX = ", high quality, detailed, safe for work, no text watermark"

# 插件默认配置(插件页面编辑,存于 data/plugin_data/<plugin>/config.json)
DEFAULT_CONFIG = {
    "seedream_api_key": "",
    "seedream_model": "doubao-seedream-5-0-pro",
    "enable_proxy": True,
    "proxy": "http://127.0.0.1:7897",
    "aspect_ratio": "1:1",
    "image_size": "2K",
    "output_format": "png",
    "watermark": True,
    "max_num": 4,
    "cooldown_seconds": 30,
    "total_limit": 200,
    "per_user_limit": 0,
    "admin_ignore_limit": True,
    "request_timeout": 90,
    "enable_nl_trigger": False,
    "nl_min_prompt_len": 5,
    "enable_prompt_enhance": False,
    "enhance_lang": "zh",
    "enhance_llm_base_url": "",
    "enhance_llm_api_key": "",
    "enhance_llm_model": "doubao-1-5-pro",
}

# 宽高比 -> prompt 自然语言描述(方式1:模型据描述判断生成尺寸)
ASPECT_DESC = {
    "1:1": "正方形构图 / square composition",
    "16:9": "横版构图,宽高比16:9 / landscape 16:9",
    "9:16": "竖版构图,宽高比9:16 / portrait 9:16",
    "4:3": "横版构图,宽高比4:3 / landscape 4:3",
    "3:4": "竖版构图,宽高比3:4 / portrait 3:4",
}

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

        self._group_locks: dict[str, asyncio.Lock] = {}
        self._last_gen_at: dict[str, float] = {}
        self._total_count = 0
        self._user_counts: dict[str, int] = {}
        self._today = ""

        # 插件页面后端 API
        context.register_web_api(
            f"/{plugin_name}/config", self.web_get_config, ["GET"], "获取插件配置")
        context.register_web_api(
            f"/{plugin_name}/config/save", self.web_save_config, ["POST"], "保存插件配置")
        context.register_web_api(
            f"/{plugin_name}/stats", self.web_get_stats, ["GET"], "获取今日用量")
        context.register_web_api(
            f"/{plugin_name}/test", self.web_test_key, ["POST"], "测试 API Key 连通性")

    # ------------------------------------------------------------------
    # 配置管理(插件页面读写,持久化到 data/plugin_data/<plugin>/config.json)
    # ------------------------------------------------------------------
    def _load_config(self) -> dict:
        cfg = dict(DEFAULT_CONFIG)
        try:
            if self.config_path.exists():
                saved = json.loads(self.config_path.read_text(encoding="utf-8"))
                for k in DEFAULT_CONFIG:
                    if k in saved:
                        cfg[k] = saved[k]
        except Exception as e:
            logger.warning(f"配置读取失败,使用默认配置: {e}")
        return cfg

    def _save_config(self, data: dict):
        for k, v in data.items():
            if k in DEFAULT_CONFIG:
                self.config[k] = v
        self.config_path.write_text(
            json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # 插件页面 Web API
    # ------------------------------------------------------------------
    async def web_get_config(self):
        return json_response(self.config)

    async def web_save_config(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("invalid payload", status_code=400)
        unknown = [k for k in payload if k not in DEFAULT_CONFIG]
        if unknown:
            return error_response(f"unknown config keys: {unknown}", status_code=400)
        try:
            self._save_config(payload)
        except Exception as e:
            logger.error(f"配置保存失败: {e}")
            return error_response(f"保存失败: {e}", status_code=500)
        logger.info("插件配置已更新(来自插件页面)")
        return json_response({"saved": True})

    async def web_get_stats(self):
        self._reset_day_if_needed()
        return json_response({
            "total_limit": int(self.config.get("total_limit", 0)),
            "total_used": self._total_count,
            "per_user_limit": int(self.config.get("per_user_limit", 0)),
        })

    async def web_test_key(self):
        """测试火山方舟 API Key:GET /models 验证鉴权"""
        payload = await request.json(default={})
        key = (payload.get("key") or "").strip() or self._api_key()
        if not key:
            return error_response("API Key 为空", status_code=400)
        try:
            async with httpx.AsyncClient(proxy=self._proxy(),
                                         timeout=httpx.Timeout(15.0)) as client:
                resp = await client.get(
                    self._join(ARK_BASE, "models"),
                    headers={"Authorization": f"Bearer {key}"})
            if resp.status_code == 200:
                return json_response({"ok": True, "message": "连接成功,API Key 有效"})
            body = resp.text[:200]
            return error_response(f"鉴权失败({resp.status_code}): {body}",
                                  status_code=resp.status_code)
        except httpx.ProxyError:
            return error_response("无法连接代理,请检查 proxy 配置", status_code=400)
        except httpx.HTTPError as e:
            return error_response(f"网络请求失败: {e}", status_code=400)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def _get_lock(self, key: str) -> asyncio.Lock:
        if key not in self._group_locks:
            self._group_locks[key] = asyncio.Lock()
        return self._group_locks[key]

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

    def _check_quota(self, group_id: str, sender_id: str, is_admin: bool) -> tuple[bool, str]:
        """返回 (是否放行, 拒绝原因);管理员可豁免总/人限额(仍受冷却)"""
        self._reset_day_if_needed()
        now = time.time()
        cd = int(self.config.get("cooldown_seconds", 30))
        last = self._last_gen_at.get(group_id, 0)
        if now - last < cd:
            return False, f"群内生成冷却中,请 {int(cd - (now - last))} 秒后再试"
        if not (is_admin and self.config.get("admin_ignore_limit", True)):
            total = int(self.config.get("total_limit", 200))
            per = int(self.config.get("per_user_limit", 0))
            if total > 0 and self._total_count >= total:
                return False, "今日生成已达总限额,明天再来吧"
            if per > 0 and self._user_counts.get(sender_id, 0) >= per:
                return False, "你今天生成已达个人限额,明天再来吧"
        return True, ""

    def _apply_quota(self, group_id: str, sender_id: str, num: int, is_admin: bool):
        self._last_gen_at[group_id] = time.time()
        if not (is_admin and self.config.get("admin_ignore_limit", True)):
            self._total_count += num
            self._user_counts[sender_id] = self._user_counts.get(sender_id, 0) + num

    def _save_image(self, img_b64: str, mime: str = "image/png") -> str:
        ext = EXT_BY_MIME.get(mime, "png")
        fname = f"eid_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.{ext}"
        path = self.image_dir / fname
        path.write_bytes(base64.b64decode(img_b64))
        return str(path)

    async def _post(self, url: str, headers: dict, payload: dict) -> dict:
        """统一 POST:代理/超时/重试(仅超时与 5xx)/错误分类"""
        timeout = httpx.Timeout(float(self.config.get("request_timeout", 90)))
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(proxy=self._proxy(), timeout=timeout) as client:
                    resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code == 429:
                    raise RuntimeError("API 限流(429),请稍后再试或调大冷却时间")
                resp.raise_for_status()
                return resp.json()
            except httpx.TimeoutException:
                last_err = RuntimeError("请求 API 超时,请检查网络/代理配置")
            except httpx.ProxyError:
                last_err = RuntimeError("无法连接代理,请检查 proxy 配置")
            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500:
                    last_err = RuntimeError(f"API 服务端错误({e.response.status_code})")
                else:
                    raise RuntimeError(f"API 错误({e.response.status_code}): "
                                       f"{e.response.text[:200]}") from e
            except httpx.HTTPError as e:
                last_err = RuntimeError(f"网络请求失败: {e}")
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
        raise last_err or RuntimeError("生成失败,请稍后再试")

    # ------------------------------------------------------------------
    # 适配层:Seedream(火山方舟)
    # ------------------------------------------------------------------
    def _build_prompt(self, prompt: str, aspect: str) -> str:
        """拼接宽高比描述(API 方式1:档位 + prompt 自然语言描述)"""
        desc = ASPECT_DESC.get(aspect)
        if desc:
            return f"{prompt.strip()},{desc}"
        return prompt.strip()

    async def _call_seedream(self, prompt: str, aspect: str,
                             image_size: str = "") -> tuple[str, str]:
        """火山方舟 Seedream 图片生成(OpenAI 兼容子集)

        官方文档要点:
          - size: 分辨率档位(1K/1.5K/2K),宽高比通过 prompt 自然语言描述(方式1,推荐)
          - response_format: url(24h 有效) / b64_json
          - watermark/output_format 独立参数,无 seed 参数
          - 顶层 error 为整体错误;data[].error 为单图错误
        """
        key = self._api_key()
        if not key:
            raise RuntimeError("未配置火山方舟 API Key(seedream_api_key)")
        model = (self.config.get("seedream_model") or "").strip() \
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
            "response_format": "b64_json",
            "output_format": output_format,
            "watermark": bool(self.config.get("watermark", True)),
        }
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            data = await self._post(url, headers, payload)
        except RuntimeError as e:
            # 平台不支持 b64_json 时降级为 url
            if "response_format" in str(e) or "invalid" in str(e).lower() or "参数" in str(e):
                payload["response_format"] = "url"
                data = await self._post(url, headers, payload)
            else:
                raise
        if data.get("error"):
            err = data["error"]
            raise RuntimeError(f"生成失败: {err.get('code', '')} {err.get('message', '')}".strip())
        items = data.get("data") or []
        if not items:
            raise RuntimeError("响应中未找到图片数据")
        item = items[0]
        if item.get("error"):
            err = item["error"]
            raise RuntimeError(f"生成失败: {err.get('code', '')} {err.get('message', '')}".strip())
        b64 = item.get("b64_json")
        if b64:
            mime = f"image/{output_format}"
            return self._save_image(b64, mime), mime
        if item.get("url"):
            return await self._download(item["url"]), f"image/{output_format}"
        raise RuntimeError("响应中未找到图片(b64_json/url 均缺失)")

    async def _download(self, url: str) -> str:
        async with httpx.AsyncClient(proxy=self._proxy(),
                                     timeout=httpx.Timeout(float(self.config.get("request_timeout", 90)))) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        fmt = str(self.config.get("output_format", "png"))
        ext = "jpg" if fmt == "jpeg" else "png"
        path = self.image_dir / f"eid_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.{ext}"
        path.write_bytes(resp.content)
        return str(path)

    async def _generate_one(self, prompt: str, aspect: str, image_size: str) -> tuple[str, str]:
        """适配层入口(当前仅 Seedream)"""
        return await self._call_seedream(prompt, aspect)

    # ------------------------------------------------------------------
    # LLM 润色(可选)
    # ------------------------------------------------------------------
    async def _enhance_prompt(self, prompt: str) -> str:
        lang = str(self.config.get("enhance_lang", "zh"))
        system_prompt = ENHANCE_SYSTEM_PROMPT_ZH if lang == "zh" else ENHANCE_SYSTEM_PROMPT_EN
        base = (self.config.get("enhance_llm_base_url") or "").strip() or ARK_BASE
        key = (self.config.get("enhance_llm_api_key") or "").strip() or self._api_key()
        model = (self.config.get("enhance_llm_model") or "").strip() \
            or ("gemini-3.1-flash-lite" if lang == "en" else "doubao-1-5-pro")
        url = self._join(base, "chat/completions")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.9,
            "max_tokens": 300,
        }
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        data = await self._post(url, headers, payload)
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()

    # ------------------------------------------------------------------
    # 共享生图流程
    # ------------------------------------------------------------------
    async def _generate_and_send(self, event: AstrMessageEvent, prompt: str,
                                 num: int = 1, aspect: str = "", image_size: str = "",
                                 is_nl: bool = False):
        sender_id = event.get_sender_id()
        is_admin = event.is_admin()
        group_id = event.get_group_id() or event.unified_msg_origin

        ok, reason = self._check_quota(group_id, sender_id, is_admin)
        if not ok:
            yield event.plain_result(reason)
            event.stop_event()
            return

        aspect = aspect or str(self.config.get("aspect_ratio", "1:1"))
        image_size = image_size or str(self.config.get("image_size", "2K"))
        num = max(1, min(num, int(self.config.get("max_num", 4))))

        async with self._get_lock(group_id):  # 同群串行
            logger.info(f"生图 group={group_id} sender={sender_id} prompt={prompt[:50]!r}")
            if is_nl:
                yield event.plain_result("🎨 收到,正在生成图片,请稍等...")
            try:
                if self.config.get("enable_prompt_enhance", False):
                    try:
                        enhanced = await self._enhance_prompt(prompt)
                        if enhanced:
                            logger.info(f"润色结果: {enhanced[:80]!r}")
                            prompt = enhanced
                    except Exception as e:
                        logger.warning(f"提示词润色失败,回退原文: {e}")
                for _ in range(num):
                    path, _mime = await self._generate_one(prompt, aspect, image_size)
                    yield event.image_result(path)
            except RuntimeError as e:
                yield event.plain_result(f"❌ {e}")
            finally:
                self._apply_quota(group_id, sender_id, num, is_admin)

    # ------------------------------------------------------------------
    # 触发方式一:指令
    # ------------------------------------------------------------------
    @filter.command("画图", alias={"绘图", "生图", "draw", "img"})
    async def draw(self, event: AstrMessageEvent, prompt: str = ""):
        """文生图: /画图 <提示词> [-n 张数] [-a 宽高比] [-s 分辨率]"""
        prompt = (prompt or "").strip()
        if not prompt:
            yield event.plain_result(
                "用法: /画图 <提示词> [-n 张数] [-a 宽高比] [-s 分辨率]\n"
                "示例: /画图 一只坐在云朵上的橘猫\n"
                "      /画图 赛博朋克城市夜景 -n 2 -a 16:9 -s 1.5K"
            )
            return
        num, aspect, image_size = 1, "", ""
        for m in FLAG_RE.finditer(prompt):
            val = m.group(2)
            if m.group(1) == "n":
                try:
                    num = int(val)
                except ValueError:
                    pass
            elif m.group(1) == "a" and val in VALID_ASPECTS:
                aspect = val
            elif m.group(1) == "s" and val in VALID_SIZES:
                image_size = val
        prompt = FLAG_RE.sub("", prompt).strip()
        async for r in self._generate_and_send(event, prompt, num, aspect, image_size):
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
        async for r in self._generate_and_send(event, prompt, 1, is_nl=True):
            yield r
        event.stop_event()
