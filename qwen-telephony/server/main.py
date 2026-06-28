from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
from pathlib import Path
import json
import logging
import mimetypes
import os
import platform
import subprocess
import sys
from pathlib import Path
import uuid
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .dialogue import get_config, get_scene, handle_turn, list_scenes, start_session
from .db import connect, init_db, row_to_dict, rows_to_dicts, seed_db, seed_dialogue_db


logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = ROOT / "qwen-telephony" / "server" / "static"
AUDIO_DIR = STATIC_DIR / "dialogue-audio"
AUDIO_INDEX = AUDIO_DIR / "index.json"
AI_MODEL_CONFIG = STATIC_DIR / "ai-model-config.json"

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

if load_dotenv:
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "qwen-telephony" / "config" / "local.env", override=False)

app = FastAPI(title="Audio Agents Operations API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _load_audio_records() -> list[dict[str, Any]]:
    if not AUDIO_INDEX.exists():
        return []
    try:
        data = json.loads(AUDIO_INDEX.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _save_audio_record(record: dict[str, Any]) -> dict[str, Any]:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    records = [item for item in _load_audio_records() if item.get("id") != record.get("id")]
    records.insert(0, record)
    AUDIO_INDEX.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def _audio_public_url(filename: str) -> str:
    return f"/static/dialogue-audio/{filename}"


def _extension_for_mime(mime_type: str, fallback: str = ".wav") -> str:
    guessed = mimetypes.guess_extension((mime_type or "").split(";")[0].strip())
    if guessed in {".jpe", ".jfif"}:
        return ".jpg"
    return guessed or fallback


def _qwen_tts_class():
    agent_dir = ROOT / "qwen-telephony" / "agent"
    if str(agent_dir) not in sys.path:
        sys.path.insert(0, str(agent_dir))
    try:
        from qwen_providers import QwenTTS  # type: ignore
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Qwen TTS provider unavailable: {exc}") from exc
    return QwenTTS


AI_MODEL_CATALOG: dict[str, Any] = {
    "providers": [
        {
            "id": "qwen",
            "name": "千问",
            "models": [
                {"id": "qwen3-tts-flash", "name": "qwen3-tts-flash"},
                {"id": "qwen-tts-latest", "name": "qwen-tts-latest"},
            ],
            "voices": [
                {"id": "Cherry", "name": "芊悦", "gender": "女声", "description": "阳光积极、亲切自然小姐姐（女性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Cherry.wav"},
                {"id": "Serena", "name": "苏瑶", "gender": "女声", "description": "温柔小姐姐（女性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Serena.wav"},
                {"id": "Ethan", "name": "晨煦", "gender": "男声", "description": "标准普通话，带部分北方口音。阳光、温暖、活力、朝气（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Ethan.wav"},
                {"id": "Chelsie", "name": "千雪", "gender": "女声", "description": "二次元虚拟女友（女性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Chelsie.wav"},
                {"id": "Momo", "name": "茉兔", "gender": "女声", "description": "撒娇搞怪，逗你开心（女性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Momo.wav"},
                {"id": "Vivian", "name": "十三", "gender": "女声", "description": "拽拽的、可爱的小暴躁（女性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Vivian.wav"},
                {"id": "Moon", "name": "月白", "gender": "男声", "description": "率性帅气的月白（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Moon.wav"},
                {"id": "Maia", "name": "四月", "gender": "女声", "description": "知性与温柔的碰撞（女性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Maia.wav"},
                {"id": "Kai", "name": "凯", "gender": "男声", "description": "耳朵的一场 SPA（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Kai.wav"},
                {"id": "Nofish", "name": "不吃鱼", "gender": "男声", "description": "不会翘舌音的设计师（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Nofish.wav"},
                {"id": "Bella", "name": "萌宝", "gender": "女声", "description": "喝酒不打醉拳的小萝莉（女性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Bella.wav"},
                {"id": "Jennifer", "name": "詹妮弗", "gender": "女声", "description": "品牌级、电影质感般美语女声（女性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Jennifer.wav"},
                {"id": "Ryan", "name": "甜茶", "gender": "男声", "description": "节奏拉满，戏感炸裂，真实与张力共舞（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Ryan.wav"},
                {"id": "Katerina", "name": "卡捷琳娜", "gender": "女声", "description": "御姐音色，韵律回味十足（女性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Katerina.wav"},
                {"id": "Aiden", "name": "艾登", "gender": "男声", "description": "精通厨艺的美语大男孩（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Aiden.wav"},
                {"id": "Eldric Sage", "name": "沧明子", "gender": "男声", "description": "沉稳睿智的老者，沧桑如松却心明如镜（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Eldric_Sage.wav"},
                {"id": "Mia", "name": "乖小妹", "gender": "女声", "description": "温顺如春水，乖巧如初雪（女性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Mia.wav"},
                {"id": "Mochi", "name": "沙小弥", "gender": "男声", "description": "聪明伶俐的小大人，童真未泯却早慧如禅（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Mochi.wav"},
                {"id": "Bellona", "name": "燕铮莺", "gender": "女声", "description": "声音洪亮，吐字清晰，人物鲜活，听得人热血沸腾；金戈铁马入梦来，字正腔圆间尽显千面人声的江湖（女性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Bellona.wav"},
                {"id": "Vincent", "name": "田叔", "gender": "男声", "description": "一口独特的沙哑烟嗓，一开口便道尽了千军万马与江湖豪情（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Vincent.wav"},
                {"id": "Bunny", "name": "萌小姬", "gender": "女声", "description": "“萌属性”爆棚的小萝莉（女性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Bunny.wav"},
                {"id": "Neil", "name": "阿闻", "gender": "男声", "description": "平直的基线语调，字正腔圆的咬字发音，这就是最专业的新闻主持人（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Neil.wav"},
                {"id": "Elias", "name": "墨讲师", "gender": "女声", "description": "既保持学科严谨性，又通过叙事技巧将复杂知识转化为可消化的认知模块（女性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Elias.wav"},
                {"id": "Arthur", "name": "徐大爷", "gender": "男声", "description": "被岁月和旱烟浸泡过的质朴嗓音，不疾不徐地摇开了满村的奇闻异事（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Arthur.wav"},
                {"id": "Nini", "name": "邻家妹妹", "gender": "女声", "description": "糯米糍一样又软又黏的嗓音，那一声声拉长了的“哥哥”，甜得能把人的骨头都叫酥了（女性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Nini.wav"},
                {"id": "Seren", "name": "小婉", "gender": "女声", "description": "温和舒缓的声线，助你更快地进入睡眠，晚安，好梦（女性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Seren.wav"},
                {"id": "Pip", "name": "顽屁小孩", "gender": "男声", "description": "调皮捣蛋却充满童真的他来了，这是你记忆中的小新吗（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Pip.wav"},
                {"id": "Stella", "name": "少女阿月", "gender": "女声", "description": "平时是甜到发腻的迷糊少女音，但在喊出“代表月亮消灭你”时，瞬间充满不容置疑的爱与正义（女性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Stella.wav"},
                {"id": "Bodega", "name": "博德加", "gender": "男声", "description": "热情的西班牙大叔（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Bodega.wav"},
                {"id": "Sonrisa", "name": "索尼莎", "gender": "女声", "description": "热情开朗的拉美大姐（女性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Sonrisa.wav"},
                {"id": "Alek", "name": "阿列克", "gender": "男声", "description": "一开口，是战斗民族的冷，也是毛呢大衣下的暖（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Alek.wav"},
                {"id": "Dolce", "name": "多尔切", "gender": "男声", "description": "慵懒的意大利大叔（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Dolce.wav"},
                {"id": "Sohee", "name": "素熙", "gender": "女声", "description": "温柔开朗，情绪丰富的韩国欧尼（女性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Sohee.wav"},
                {"id": "Ono Anna", "name": "小野杏", "gender": "女声", "description": "鬼灵精怪的青梅竹马（女性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Ono_Anna.wav"},
                {"id": "Lenn", "name": "莱恩", "gender": "男声", "description": "理性是底色，叛逆藏在细节里——穿西装也听后朋克的德国青年（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Lenn.wav"},
                {"id": "Emilien", "name": "埃米尔安", "gender": "男声", "description": "浪漫的法国大哥哥（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Emilien.wav"},
                {"id": "Andre", "name": "安德雷", "gender": "男声", "description": "声音磁性，自然舒服、沉稳男生（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Andre.wav"},
                {"id": "Radio Gol", "name": "拉迪奥·戈尔", "gender": "男声", "description": "足球诗人", "verified": True, "sample_url": "/static/qwen-voice-samples/Radio_Gol.wav"},
                {"id": "Jada", "name": "上海-阿珍", "gender": "女声", "description": "风风火火的沪上阿姐（女性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Jada.wav"},
                {"id": "Dylan", "name": "北京-晓东", "gender": "男声", "description": "北京胡同里长大的少年（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Dylan.wav"},
                {"id": "Li", "name": "南京-老李", "gender": "男声", "description": "耐心的瑜伽老师（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Li.wav"},
                {"id": "Marcus", "name": "陕西-秦川", "gender": "男声", "description": "面宽话短，心实声沉——老陕的味道（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Marcus.wav"},
                {"id": "Roy", "name": "闽南-阿杰", "gender": "男声", "description": "诙谐直爽、市井活泼的台湾哥仔形象（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Roy.wav"},
                {"id": "Peter", "name": "天津-李彼得", "gender": "男声", "description": "天津相声，专业捧哏（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Peter.wav"},
                {"id": "Sunny", "name": "四川-晴儿", "gender": "女声", "description": "甜到你心里的川妹子（女性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Sunny.wav"},
                {"id": "Eric", "name": "四川-程川", "gender": "男声", "description": "一个跳脱市井的四川成都男子（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Eric.wav"},
                {"id": "Rocky", "name": "粤语-阿强", "gender": "男声", "description": "幽默风趣的阿强，在线陪聊（男性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Rocky.wav"},
                {"id": "Kiki", "name": "粤语-阿清", "gender": "女声", "description": "甜美的港妹闺蜜（女性）", "verified": True, "sample_url": "/static/qwen-voice-samples/Kiki.wav"},
            ],
            "emotions": ["neutral", "happy", "calm", "serious", "warm"],
            "styles": ["normal", "customer-service", "sales", "callback"],
        },
        {
            "id": "doubao",
            "name": "豆包",
            "models": [
                {"id": "seed-tts-2.0-expressive", "name": "seed-tts-2.0-expressive"},
                {"id": "seed-tts-2.0-standard", "name": "seed-tts-2.0-standard"},
            ],
            "voices": [
                {"id": "zh_female_xiaohe_uranus_bigtts", "name": "小荷", "gender": "女声", "description": "seed-tts-2.0 接口验证通过", "verified": True},
                {"id": "zh_female_vv_uranus_bigtts", "name": "VV", "gender": "女声", "description": "seed-tts-2.0 接口验证通过", "verified": True},
                {"id": "zh_female_peiqi_uranus_bigtts", "name": "佩奇", "gender": "女声", "description": "seed-tts-2.0 接口验证通过", "verified": True},
                {"id": "zh_male_m191_uranus_bigtts", "name": "M191", "gender": "男声", "description": "seed-tts-2.0 接口验证通过", "verified": True},
                {"id": "zh_male_taocheng_uranus_bigtts", "name": "陶城", "gender": "男声", "description": "seed-tts-2.0 接口验证通过", "verified": True},
                {"id": "zh_male_ruyayichen_uranus_bigtts", "name": "儒雅一辰", "gender": "男声", "description": "seed-tts-2.0 接口验证通过", "verified": True},
            ],
            "emotions": ["neutral", "happy", "sad", "angry", "calm", "excited"],
            "styles": ["normal", "expressive", "customer-service", "sales"],
        },
    ]
}


def _default_ai_model_config() -> dict[str, Any]:
    return {
        "provider": "qwen",
        "model": os.getenv("QWEN_TTS_MODEL", "qwen3-tts-flash"),
        "voice": os.getenv("QWEN_TTS_VOICE", "Cherry"),
        "emotion": "neutral",
        "style": "normal",
        "sample_text": "您好，请问现在方便沟通吗？",
        "speed": 1.0,
        "pitch": 1.0,
        "volume": 1.0,
    }


def _load_ai_model_config() -> dict[str, Any]:
    if not AI_MODEL_CONFIG.exists():
        return _default_ai_model_config()
    try:
        data = json.loads(AI_MODEL_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_ai_model_config()
    if not isinstance(data, dict):
        return _default_ai_model_config()
    return {**_default_ai_model_config(), **data}


def _save_ai_model_config(config: dict[str, Any]) -> dict[str, Any]:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    saved = {**_default_ai_model_config(), **config, "updated_at": datetime.now(timezone.utc).isoformat()}
    AI_MODEL_CONFIG.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    return saved


async def _synthesize_qwen_audio(text: str, config: dict[str, Any]) -> tuple[bytes, str, str]:
    qwen_tts = _qwen_tts_class()
    return await qwen_tts(
        model=str(config.get("model") or os.getenv("QWEN_TTS_MODEL", "qwen3-tts-flash")),
        voice=str(config.get("voice") or os.getenv("QWEN_TTS_VOICE", "Cherry")),
    ).synthesize_audio_bytes(text)


async def _synthesize_doubao_audio(text: str, config: dict[str, Any]) -> tuple[bytes, str, str]:
    endpoint = os.getenv("DOUBAO_TTS_ENDPOINT", "https://openspeech.bytedance.com/api/v3/tts/unidirectional").strip()
    api_key = os.getenv("DOUBAO_TTS_API_KEY", "").strip()
    resource_id = os.getenv("DOUBAO_TTS_RESOURCE_ID", "seed-tts-2.0").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="豆包 TTS 未配置，请在 .env 填写 DOUBAO_TTS_API_KEY")
    try:
        import httpx
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"HTTP client unavailable: {exc}") from exc
    payload = {
        "user": {
            "uid": "ai-login-replica",
        },
        "req_params": {
            "text": text,
            "model": config.get("model"),
            "speaker": config.get("voice"),
            "emotion": config.get("emotion") or "neutral",
            "style": config.get("style") or "normal",
            "format": "wav",
            "speed": config.get("speed") or 1.0,
            "pitch": config.get("pitch") or 1.0,
            "volume": config.get("volume") or 1.0,
        },
    }
    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": str(uuid.uuid4()),
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0)) as client:
        response = await client.post(endpoint, headers=headers, json=payload)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=response.status_code, detail=f"豆包 TTS 请求失败: {response.text[:600]}") from exc
    content_type = response.headers.get("content-type", "").split(";")[0].strip()
    request_id = response.headers.get("x-request-id", str(uuid.uuid4()))
    if content_type.startswith("audio/"):
        return response.content, content_type, request_id
    try:
        body = response.json()
    except ValueError:
        try:
            body = json.loads(response.text.strip())
        except ValueError as exc:
            audio_chunks: list[bytes] = []
            last_message = ""
            for line in response.text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                last_message = str(event.get("message") or last_message)
                if event.get("code") not in {None, 0, "0", 20000000, "20000000"}:
                    raise HTTPException(status_code=500, detail=f"豆包 TTS 请求失败: {event}")
                encoded = event.get("data")
                if isinstance(encoded, str) and encoded:
                    audio_chunks.append(base64.b64decode(encoded))
            if audio_chunks:
                return b"".join(audio_chunks), "audio/mpeg", request_id
            raise HTTPException(status_code=500, detail=f"豆包 TTS 返回中没有音频数据: {last_message or response.text[:300]}") from exc
    if isinstance(body, dict) and body.get("code") not in {None, 0, "0"}:
        raise HTTPException(status_code=500, detail=f"豆包 TTS 请求失败: {body}")
    audio = body.get("audio") or body.get("data") or body.get("result") or {}
    if isinstance(audio, str):
        return base64.b64decode(audio), "audio/wav", request_id
    if isinstance(audio, dict):
        encoded = audio.get("data") or audio.get("audio") or audio.get("base64")
        if encoded:
            if str(encoded).startswith("data:"):
                header, encoded = str(encoded).split(",", 1)
                mime_type = header.split(":", 1)[1].split(";", 1)[0]
            else:
                mime_type = audio.get("mime_type") or "audio/wav"
            return base64.b64decode(encoded), mime_type, request_id
    raise HTTPException(status_code=500, detail=f"豆包 TTS 返回中没有音频数据: {body}")


async def _synthesize_selected_audio(text: str, payload: DialogueAudioAuditionRequest) -> tuple[bytes, str, str, dict[str, Any]]:
    saved = _load_ai_model_config()
    config = {
        **saved,
        **{key: value for key, value in {
            "provider": payload.provider,
            "model": payload.model,
            "voice": payload.voice,
            "emotion": payload.emotion,
            "style": payload.style,
            "speed": payload.speed,
            "pitch": payload.pitch,
            "volume": payload.volume,
        }.items() if value not in {None, ""}},
    }
    provider = config.get("provider") or "qwen"
    if provider == "doubao":
        audio_bytes, mime_type, request_id = await _synthesize_doubao_audio(text, config)
    else:
        audio_bytes, mime_type, request_id = await _synthesize_qwen_audio(text, config)
    return audio_bytes, mime_type, request_id, config


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1)
    prompt: str = ""
    max_concurrency: int = Field(default=2, ge=1, le=100)
    retry_limit: int = Field(default=1, ge=0, le=10)


class ContactCreate(BaseModel):
    name: str = ""
    phone: str = Field(min_length=3)
    tags: str = ""
    notes: str = ""


class ContactImport(BaseModel):
    contacts: list[ContactCreate] = Field(default_factory=list)


class CallCreate(BaseModel):
    campaign_id: int | None = None
    contact_id: int | None = None
    phone: str = Field(min_length=3)


class CallUpdate(BaseModel):
    status: Literal["pending", "dialing", "ringing", "active", "completed", "failed", "no_answer", "busy"] | None = None
    duration_sec: int | None = Field(default=None, ge=0)
    failure_reason: str | None = None
    summary: str | None = None
    intent_level: Literal["unknown", "low", "medium", "high"] | None = None


class CallSimulation(BaseModel):
    event: Literal["ringing", "answer", "hangup", "no_answer", "busy", "failed"]
    duration_sec: int = Field(default=18, ge=0)
    summary: str = ""
    intent_level: Literal["unknown", "low", "medium", "high"] = "unknown"


class DialogueConfigUpdate(BaseModel):
    nlu_enabled: bool | None = None
    default_scene_id: int | None = None


class DialogueNluUpdate(BaseModel):
    enabled: bool


class DialogueSceneCreate(BaseModel):
    name: str = Field(min_length=1)
    industry: str = ""
    business_type: str = ""
    script_type: Literal["common", "variable"] = "common"
    auto_break: str = "否"
    audit_status: str = "待审核"
    ui: dict[str, Any] = Field(default_factory=dict)


class DialogueSceneUpdate(BaseModel):
    name: str | None = None
    industry: str | None = None
    business_type: str | None = None
    script_type: Literal["common", "variable"] | None = None
    auto_break: str | None = None
    audit_status: str | None = None
    ui: dict[str, Any] | None = None
    status: Literal["draft", "published", "disabled"] | None = None
    flow: dict[str, Any] | None = None


class KnowledgeCreate(BaseModel):
    title: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    keywords: str = ""
    sort_order: int = 10
    enabled: bool = True


class LabelRuleCreate(BaseModel):
    label: str = Field(min_length=1)
    priority: int = 10
    condition: dict[str, int] = Field(default_factory=dict)
    enabled: bool = True


class DialogueTurnRequest(BaseModel):
    session_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    scene_id: int | None = None
    channel: str = "api"
    nlu_enabled: bool | None = None


class DialogueStartRequest(BaseModel):
    session_id: str = Field(min_length=1)
    scene_id: int | None = None


class DialogueAudioAuditionRequest(BaseModel):
    text: str = Field(min_length=1)
    provider: Literal["qwen", "doubao"] | None = None
    model: str | None = None
    voice: str | None = None
    emotion: str | None = None
    style: str | None = None
    speed: float | None = None
    pitch: float | None = None
    volume: float | None = None


class DialogueAudioUploadRequest(BaseModel):
    name: str = Field(min_length=1)
    data_url: str = Field(min_length=1)
    content_type: str = "audio/wav"
    text: str = ""


class AIModelConfigUpdate(BaseModel):
    provider: Literal["qwen", "doubao"] = "qwen"
    model: str = Field(min_length=1)
    voice: str = Field(min_length=1)
    emotion: str = "neutral"
    style: str = "normal"
    sample_text: str = "您好，请问现在方便沟通吗？"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    pitch: float = Field(default=1.0, ge=0.5, le=2.0)
    volume: float = Field(default=1.0, ge=0.1, le=2.0)


class DialogueMicroSipTestRequest(BaseModel):
    phone: str = "1000@127.0.0.1:5066"
    visible: bool = True


class TaskTemplateCreate(BaseModel):
    name: str = Field(min_length=1)
    default_prompt: str = ""
    max_concurrency: int = Field(default=2, ge=1, le=100)
    retry_limit: int = Field(default=1, ge=0, le=10)
    default_scene_id: int | None = None
    status: Literal["enabled", "disabled"] = "enabled"
    notes: str = ""


class PushRecordCreate(BaseModel):
    campaign_id: int | None = None
    target: str = "Webhook"
    push_type: str = "Webhook"
    content: str = ""


@app.on_event("startup")
def startup() -> None:
    init_db()
    seed_db()
    seed_dialogue_db()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/dialogue/config")
def dialogue_config() -> dict:
    return get_config()


def _upsert_dialogue_config(*, nlu_enabled: bool | None = None, default_scene_id: int | None = None) -> dict:
    with connect() as conn:
        current = conn.execute("SELECT * FROM dialogue_config WHERE id = 1").fetchone()
        next_nlu_enabled = bool(current["nlu_enabled"]) if current else True
        next_default_scene_id = current["default_scene_id"] if current else None
        if nlu_enabled is not None:
            next_nlu_enabled = nlu_enabled
        if default_scene_id is not None:
            scene = conn.execute("SELECT id FROM dialogue_scenes WHERE id = ?", (default_scene_id,)).fetchone()
            if not scene:
                raise HTTPException(status_code=404, detail="Default dialogue scene not found")
            next_default_scene_id = default_scene_id
        conn.execute(
            """
            INSERT INTO dialogue_config (id, nlu_enabled, default_scene_id, updated_at)
            VALUES (1, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                nlu_enabled = excluded.nlu_enabled,
                default_scene_id = excluded.default_scene_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (1 if next_nlu_enabled else 0, next_default_scene_id),
        )
    return get_config()


@app.put("/api/dialogue/config")
def update_dialogue_config(payload: DialogueConfigUpdate) -> dict:
    payload_fields = getattr(payload, "model_fields_set", getattr(payload, "__fields_set__", set()))
    if not payload_fields:
        raise HTTPException(status_code=400, detail="No config fields provided")
    if "nlu_enabled" in payload_fields and "default_scene_id" in payload_fields:
        raise HTTPException(
            status_code=400,
            detail="Use /api/dialogue/config/nlu or /api/dialogue/scenes/{scene_id}/default for decoupled updates",
        )
    if "nlu_enabled" in payload_fields:
        if payload.nlu_enabled is None:
            raise HTTPException(status_code=400, detail="nlu_enabled cannot be null")
        return _upsert_dialogue_config(nlu_enabled=payload.nlu_enabled)
    return _upsert_dialogue_config(default_scene_id=payload.default_scene_id)


@app.post("/api/dialogue/config/nlu")
def update_dialogue_nlu(payload: DialogueNluUpdate) -> dict:
    return _upsert_dialogue_config(nlu_enabled=payload.enabled)


@app.get("/api/dialogue/scenes")
def dialogue_scenes() -> list[dict]:
    return list_scenes()


@app.post("/api/dialogue/scenes")
def create_dialogue_scene(payload: DialogueSceneCreate) -> dict:
    default_flow = {
        "entry_node": "start",
        "max_turns": 10,
        "unknown_route": "fallback",
        "nodes": [
            {
                "id": "start",
                "type": "scene",
                "name": "主流程开场白",
                "text": "您好，我是智能客服助手。请问您现在方便沟通吗？",
                "routes": {
                    "positive": "intro",
                    "negative": "fallback",
                    "reject": "hangup",
                    "neutral": "intro",
                    "unknown": "fallback",
                },
            },
            {
                "id": "intro",
                "type": "scene",
                "name": "业务介绍",
                "text": "好的，我简单介绍一下我们的服务。您想先了解功能、价格还是接入方式？",
                "routes": {
                    "positive": "end",
                    "negative": "fallback",
                    "reject": "hangup",
                    "neutral": "end",
                    "unknown": "fallback",
                },
            },
            {"id": "fallback", "type": "llm_fallback", "name": "LLM 兜底", "text": ""},
            {"id": "hangup", "type": "end", "name": "挂机", "text": "好的，那就不打扰您了。"},
            {"id": "end", "type": "end", "name": "结束", "text": "好的，感谢您的时间。"},
        ],
    }
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO dialogue_scenes (
                name, industry, business_type, script_type,
                auto_break, audit_status, ui_json, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'draft')
            """,
            (
                payload.name,
                payload.industry,
                payload.business_type,
                payload.script_type,
                payload.auto_break,
                payload.audit_status,
                json.dumps(payload.ui, ensure_ascii=False),
            ),
        )
        scene_id = cur.lastrowid
        version = conn.execute(
            """
            INSERT INTO dialogue_versions (scene_id, version, status, flow_json)
            VALUES (?, 1, 'draft', ?)
            """,
            (scene_id, json.dumps(default_flow, ensure_ascii=False)),
        )
        conn.execute(
            "UPDATE dialogue_scenes SET active_version_id = ? WHERE id = ?",
            (version.lastrowid, scene_id),
        )
    scene = get_scene(scene_id)
    return scene or {}


@app.get("/api/dialogue/scenes/{scene_id}")
def read_dialogue_scene(scene_id: int) -> dict:
    scene = get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="Dialogue scene not found")
    return scene


def validate_flow(flow: dict[str, Any]) -> None:
    nodes = flow.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise HTTPException(status_code=400, detail="Flow must contain nodes")
    node_ids = {node.get("id") for node in nodes}
    if not flow.get("entry_node") or flow["entry_node"] not in node_ids:
        raise HTTPException(status_code=400, detail="entry_node is missing or invalid")
    for node in nodes:
        if not node.get("id") or not node.get("type") or not node.get("name"):
            raise HTTPException(status_code=400, detail="Every node needs id, type, and name")
        if node.get("type") == "scene":
            routes = node.get("routes") or {}
            for target in routes.values():
                if target and target not in node_ids:
                    raise HTTPException(status_code=400, detail=f"Route target {target} does not exist")


def _prewarm_dialogue_audio(scene_id: int) -> bool:
    script = ROOT / "qwen-telephony" / "scripts" / "prewarm-dialogue-audio.py"
    if not script.exists():
        return False
    python = ROOT / "qwen-telephony" / ".venv-win" / "Scripts" / "python.exe"
    python_cmd = str(python) if python.exists() else sys.executable

    log_dir = ROOT / "qwen-telephony" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path = log_dir / f"dialogue-audio-prewarm-{scene_id}.out.log"
    err_path = log_dir / f"dialogue-audio-prewarm-{scene_id}.err.log"
    env = os.environ.copy()
    env.setdefault("QWEN_TTS_USE_SSE", "false")
    env.setdefault("QWEN_TTS_CACHE_ENABLED", "true")
    with out_path.open("ab") as out_file, err_path.open("ab") as err_file:
        subprocess.Popen(
            [python_cmd, str(script), "--scene-id", str(scene_id)],
            cwd=str(ROOT),
            env=env,
            stdout=out_file,
            stderr=err_file,
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system().lower() == "windows" else 0,
        )
    return True


@app.put("/api/dialogue/scenes/{scene_id}")
def update_dialogue_scene(scene_id: int, payload: DialogueSceneUpdate) -> dict:
    if payload.flow is not None:
        validate_flow(payload.flow)
    with connect() as conn:
        scene = conn.execute("SELECT * FROM dialogue_scenes WHERE id = ?", (scene_id,)).fetchone()
        if not scene:
            raise HTTPException(status_code=404, detail="Dialogue scene not found")
        if (
            payload.name is not None
            or payload.industry is not None
            or payload.business_type is not None
            or payload.script_type is not None
            or payload.auto_break is not None
            or payload.audit_status is not None
            or payload.ui is not None
            or payload.status is not None
        ):
            conn.execute(
                """
                UPDATE dialogue_scenes
                SET name = COALESCE(?, name),
                    industry = COALESCE(?, industry),
                    business_type = COALESCE(?, business_type),
                    script_type = COALESCE(?, script_type),
                    auto_break = COALESCE(?, auto_break),
                    audit_status = COALESCE(?, audit_status),
                    ui_json = COALESCE(?, ui_json),
                    status = COALESCE(?, status),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    payload.name,
                    payload.industry,
                    payload.business_type,
                    payload.script_type,
                    payload.auto_break,
                    payload.audit_status,
                    json.dumps(payload.ui, ensure_ascii=False) if payload.ui is not None else None,
                    payload.status,
                    scene_id,
                ),
            )
        if payload.flow is not None:
            active_version_id = scene["active_version_id"]
            conn.execute(
                "UPDATE dialogue_versions SET flow_json = ? WHERE id = ?",
                (json.dumps(payload.flow, ensure_ascii=False), active_version_id),
            )
    updated = get_scene(scene_id)
    return updated or {}


@app.post("/api/dialogue/scenes/{scene_id}/publish")
def publish_dialogue_scene(scene_id: int) -> dict:
    scene = get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="Dialogue scene not found")
    validate_flow(scene["flow"])
    with connect() as conn:
        conn.execute("UPDATE dialogue_scenes SET status = 'published', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (scene_id,))
        conn.execute("UPDATE dialogue_versions SET status = 'published', published_at = CURRENT_TIMESTAMP WHERE id = ?", (scene["active_version_id"],))
    updated = get_scene(scene_id) or {}
    updated["audio_prewarm_started"] = _prewarm_dialogue_audio(scene_id)
    return updated


@app.post("/api/dialogue/scenes/{scene_id}/default")
def set_default_dialogue_scene(scene_id: int) -> dict:
    return _upsert_dialogue_config(default_scene_id=scene_id)


def _microsip_exe() -> Path:
    return ROOT / "tools" / "microsip" / "MicroSIP.exe"


def _agent_worker_reachable() -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:18081/worker", timeout=2) as response:
            return response.status < 500
    except Exception:
        return False


def _livekit_http_url() -> str:
    value = os.getenv("LIVEKIT_HTTP_URL") or os.getenv("LIVEKIT_URL") or "http://127.0.0.1:7880"
    if value.startswith("ws://"):
        return "http://" + value.removeprefix("ws://")
    if value.startswith("wss://"):
        return "https://" + value.removeprefix("wss://")
    return value


async def _create_agent_dispatch_async(scene_id: int, room_name: str) -> dict[str, str]:
    from livekit import api

    agent_name = os.getenv("QWEN_AGENT_EXPLICIT_NAME") or os.getenv("LIVEKIT_AGENT_NAME") or ""
    if not agent_name:
        return {}
    lkapi = api.LiveKitAPI(
        url=_livekit_http_url(),
        api_key=os.getenv("LIVEKIT_API_KEY", "devkey"),
        api_secret=os.getenv("LIVEKIT_API_SECRET", "secret"),
    )
    try:
        dispatch = await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=agent_name,
                room=room_name,
                metadata=json.dumps({"scene_id": scene_id, "source": "microsip-test"}, ensure_ascii=False),
                attributes={
                    "qwen.dialogue_scene_id": str(scene_id),
                    "qwen.test_source": "microsip",
                },
            )
        )
        return {"id": dispatch.id, "agent_name": dispatch.agent_name, "room": dispatch.room}
    finally:
        await lkapi.aclose()


def _create_agent_dispatch(scene_id: int, room_name: str) -> dict[str, str] | None:
    try:
        return asyncio.run(_create_agent_dispatch_async(scene_id, room_name))
    except Exception:
        return None


async def _hangup_livekit_room_async(room_name: str, participant_identity: str = "") -> dict[str, Any]:
    from livekit import api

    result: dict[str, Any] = {
        "room": room_name,
        "removed_participants": [],
        "deleted_room": False,
        "errors": [],
    }
    if not room_name:
        result["errors"].append("room_name is empty")
        return result

    lkapi = api.LiveKitAPI(
        url=_livekit_http_url(),
        api_key=os.getenv("LIVEKIT_API_KEY", "devkey"),
        api_secret=os.getenv("LIVEKIT_API_SECRET", "secret"),
    )
    try:
        identities: list[str] = []
        if participant_identity:
            identities.append(participant_identity)
        else:
            try:
                participants = await lkapi.room.list_participants(api.ListParticipantsRequest(room=room_name))
                identities = [item.identity for item in participants.participants if item.identity]
            except Exception as exc:
                result["errors"].append(f"list_participants failed: {exc}")

        for identity in dict.fromkeys(identities):
            try:
                await lkapi.room.remove_participant(api.RoomParticipantIdentity(room=room_name, identity=identity))
                result["removed_participants"].append(identity)
            except Exception as exc:
                result["errors"].append(f"remove_participant {identity} failed: {exc}")

        try:
            await lkapi.room.delete_room(api.DeleteRoomRequest(room=room_name))
            result["deleted_room"] = True
        except Exception as exc:
            result["errors"].append(f"delete_room failed: {exc}")
    finally:
        await lkapi.aclose()
    return result


def _hangup_livekit_room(room_name: str, participant_identity: str = "") -> dict[str, Any]:
    try:
        return asyncio.run(_hangup_livekit_room_async(room_name, participant_identity))
    except Exception as exc:
        return {
            "room": room_name,
            "removed_participants": [],
            "deleted_room": False,
            "errors": [str(exc)],
        }


def _hangup_microsip_all() -> dict[str, Any]:
    exe = _microsip_exe()
    result: dict[str, Any] = {
        "microsip_exists": exe.exists(),
        "command": f"{exe} /hangupall",
        "started": False,
        "error": "",
    }
    if not exe.exists() or platform.system().lower() != "windows":
        return result
    try:
        subprocess.Popen(
            [str(exe), "/hangupall"],
            cwd=str(exe.parent),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        result["started"] = True
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _microsip_room_name() -> str:
    return os.getenv("QWEN_AGENT_ROOM", "qwen-phone-room")


CALL_SELECT_FIELDS = """
    calls.*,
    COALESCE(contacts.name, calls.caller_name) AS contact_name,
    COALESCE(
        campaigns.name,
        CASE WHEN dialogue_scenes.id IS NOT NULL THEN 'MicroSIP话术测试' ELSE '' END
    ) AS campaign_name,
    dialogue_scenes.name AS scene_name,
    dialogue_sessions.label AS dialogue_label,
    CASE
        WHEN calls.status IN ('dialing', 'ringing', 'active')
        THEN CASE
            WHEN calls.duration_sec > 0 THEN calls.duration_sec
            ELSE COALESCE(
                MIN(600, MAX(18, dialogue_sessions.turn_count * 18)),
                MIN(600, MAX(18, CAST(strftime('%s', 'now') - strftime('%s', calls.created_at) AS INTEGER)))
            )
        END
        ELSE calls.duration_sec
    END AS live_duration_sec
"""

CALL_SELECT_JOINS = """
    FROM calls
    LEFT JOIN contacts ON contacts.id = calls.contact_id
    LEFT JOIN campaigns ON campaigns.id = calls.campaign_id
    LEFT JOIN dialogue_sessions ON dialogue_sessions.id = calls.room_name
    LEFT JOIN dialogue_scenes ON dialogue_scenes.id = COALESCE(calls.scene_id, dialogue_sessions.scene_id)
"""


def _sync_call_dialogue_state(conn) -> None:
    room_name = _microsip_room_name()
    stale_seconds = int(os.getenv("QWEN_CALL_ACTIVE_STALE_SECONDS", "180"))
    conn.execute(
        """
        UPDATE calls
        SET room_name = ?,
            caller_name = '测试号'
        WHERE phone = '1000@127.0.0.1:5066'
          AND room_name LIKE 'microsip-script-test-%'
        """,
        (room_name,),
    )
    conn.execute("UPDATE calls SET caller_name = '测试号' WHERE phone = '1000@127.0.0.1:5066' AND caller_name = ''")
    conn.execute(
        """
        UPDATE calls
        SET status = 'completed',
            ended_at = COALESCE(ended_at, created_at),
            duration_sec = CASE
                WHEN duration_sec = 0 THEN COALESCE(
                    (
                        SELECT MIN(600, MAX(18, dialogue_sessions.turn_count * 18))
                        FROM dialogue_sessions
                        WHERE dialogue_sessions.id = calls.room_name
                    ),
                    60
                )
                ELSE duration_sec
            END
        WHERE room_name <> ''
          AND id < (SELECT MAX(latest.id) FROM calls AS latest WHERE latest.room_name = calls.room_name)
          AND status IN ('pending', 'dialing', 'ringing', 'active')
        """
    )
    conn.execute(
        """
        UPDATE calls
        SET duration_sec = COALESCE(
                (
                    SELECT MIN(600, MAX(18, dialogue_sessions.turn_count * 18))
                    FROM dialogue_sessions
                    WHERE dialogue_sessions.id = calls.room_name
                ),
                60
            )
        WHERE phone = '1000@127.0.0.1:5066'
          AND status = 'completed'
          AND duration_sec = 0
        """
    )
    conn.execute(
        """
        UPDATE calls
        SET scene_id = COALESCE(
                scene_id,
                (SELECT scene_id FROM dialogue_sessions WHERE dialogue_sessions.id = calls.room_name)
            ),
            status = CASE
                WHEN status IN ('pending', 'dialing', 'ringing')
                     AND EXISTS (SELECT 1 FROM dialogue_sessions WHERE dialogue_sessions.id = calls.room_name)
                THEN 'active'
                ELSE status
            END,
            started_at = CASE
                WHEN started_at IS NULL OR started_at < created_at THEN created_at
                ELSE started_at
            END,
            summary = CASE
                WHEN summary = '' THEN COALESCE(
                    (
                        SELECT response_text
                        FROM dialogue_turn_logs
                        WHERE dialogue_turn_logs.session_id = calls.room_name
                          AND response_text <> ''
                        ORDER BY id DESC
                        LIMIT 1
                    ),
                    summary
                )
                ELSE summary
            END,
            intent_level = CASE
                WHEN (SELECT label FROM dialogue_sessions WHERE dialogue_sessions.id = calls.room_name) LIKE 'A%' THEN 'high'
                WHEN (SELECT label FROM dialogue_sessions WHERE dialogue_sessions.id = calls.room_name) LIKE 'B%' THEN 'medium'
                WHEN (SELECT label FROM dialogue_sessions WHERE dialogue_sessions.id = calls.room_name) LIKE 'C%' THEN 'low'
                ELSE intent_level
            END
        WHERE room_name <> ''
          AND id = (SELECT MAX(latest.id) FROM calls AS latest WHERE latest.room_name = calls.room_name)
          AND EXISTS (SELECT 1 FROM dialogue_sessions WHERE dialogue_sessions.id = calls.room_name)
        """
    )
    conn.execute(
        """
        UPDATE calls
        SET status = 'completed',
            duration_sec = CASE
                WHEN duration_sec = 0 THEN COALESCE(
                    (
                        SELECT MIN(600, MAX(18, dialogue_sessions.turn_count * 18))
                        FROM dialogue_sessions
                        WHERE dialogue_sessions.id = calls.room_name
                    ),
                    60
                )
                ELSE duration_sec
            END,
            ended_at = COALESCE(
                ended_at,
                (
                    SELECT MAX(created_at)
                    FROM dialogue_turn_logs
                    WHERE dialogue_turn_logs.session_id = calls.room_name
                      AND dialogue_turn_logs.created_at >= calls.created_at
                ),
                datetime(
                    calls.created_at,
                    '+' || COALESCE(
                        (
                            SELECT MIN(600, MAX(18, dialogue_sessions.turn_count * 18))
                            FROM dialogue_sessions
                            WHERE dialogue_sessions.id = calls.room_name
                        ),
                        60
                    ) || ' seconds'
                )
            )
        WHERE status IN ('pending', 'dialing', 'ringing', 'active')
          AND room_name <> ''
          AND CAST(
              strftime('%s', 'now') - strftime(
                  '%s',
                  COALESCE(
                      (
                          SELECT MAX(created_at)
                          FROM dialogue_turn_logs
                          WHERE dialogue_turn_logs.session_id = calls.room_name
                            AND dialogue_turn_logs.created_at >= calls.created_at
                      ),
                      calls.created_at
                  )
              ) AS INTEGER
          ) > ?
        """,
        (stale_seconds,),
    )
    conn.execute(
        """
        UPDATE calls
        SET ended_at = datetime(COALESCE(started_at, created_at), '+' || duration_sec || ' seconds')
        WHERE status = 'completed'
          AND duration_sec > 0
          AND (
              ended_at IS NULL
              OR ABS(
                  (strftime('%s', ended_at) - strftime('%s', COALESCE(started_at, created_at)))
                  - duration_sec
              ) > 1
          )
        """
    )


@app.post("/api/dialogue/scenes/{scene_id}/microsip-test")
def microsip_test(scene_id: int, payload: DialogueMicroSipTestRequest) -> dict:
    scene = get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="Dialogue scene not found")

    room_name = _microsip_room_name()
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO calls (scene_id, caller_name, phone, status, room_name, started_at, summary, intent_level)
            VALUES (?, ?, ?, 'dialing', ?, CURRENT_TIMESTAMP, ?, 'unknown')
            """,
            (
                scene_id,
                "测试号" if payload.phone == "1000@127.0.0.1:5066" else "",
                payload.phone,
                room_name,
                f"话术发布后 MicroSIP 测试：{scene['name']}",
            ),
        )
        call_id = cur.lastrowid
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()

    exe = _microsip_exe()
    prerequisites = {
        "microsip_exists": exe.exists(),
        "agent_worker_reachable": _agent_worker_reachable(),
        "windows": platform.system().lower() == "windows",
    }
    manual = {
        "start_system": "qwen-telephony/scripts/start-system.ps1",
        "start_microsip": "qwen-telephony/scripts/start-microsip.ps1",
        "dial": f"sip:{payload.phone}",
        "agent_scene": f"QWEN_DIALOGUE_SCENE_ID={scene_id} 或使用默认场景 {scene_id}",
    }

    dispatch = None
    if not all(prerequisites.values()):
        return {
            "call": row_to_dict(row),
            "started": False,
            "message": "MicroSIP 测试任务已创建，但当前环境不能自动启动 MicroSIP。请按 manual 指令手动测试。",
            "prerequisites": prerequisites,
            "manual": manual,
        }

    dispatch = _create_agent_dispatch(scene_id, room_name)

    try:
        startupinfo = None
        creationflags = 0
        if not payload.visible:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = subprocess.CREATE_NO_WINDOW
        subprocess.Popen(
            [str(exe), payload.phone],
            cwd=str(exe.parent),
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
    except Exception as exc:
        return {
            "call": row_to_dict(row),
            "started": False,
            "message": f"MicroSIP 测试任务已创建，但 MicroSIP 启动失败：{exc}",
            "prerequisites": prerequisites,
            "manual": manual,
            "dispatch": dispatch,
        }

    return {
        "call": row_to_dict(row),
        "started": True,
        "message": f"已使用 MicroSIP 呼叫 sip:{payload.phone}。接通后请按话术进行语音测试。",
        "prerequisites": prerequisites,
        "manual": manual,
        "dispatch": dispatch,
    }


@app.post("/api/dialogue/scenes/{scene_id}/knowledge")
def create_knowledge(scene_id: int, payload: KnowledgeCreate) -> dict:
    if not get_scene(scene_id):
        raise HTTPException(status_code=404, detail="Dialogue scene not found")
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO knowledge_items (scene_id, title, answer, keywords, sort_order, enabled)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (scene_id, payload.title, payload.answer, payload.keywords, payload.sort_order, 1 if payload.enabled else 0),
        )
        row = conn.execute("SELECT * FROM knowledge_items WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_dict(row)


@app.put("/api/dialogue/knowledge/{item_id}")
def update_knowledge(item_id: int, payload: KnowledgeCreate) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM knowledge_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Knowledge item not found")
        conn.execute(
            """
            UPDATE knowledge_items
            SET title = ?, answer = ?, keywords = ?, sort_order = ?, enabled = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (payload.title, payload.answer, payload.keywords, payload.sort_order, 1 if payload.enabled else 0, item_id),
        )
        updated = conn.execute("SELECT * FROM knowledge_items WHERE id = ?", (item_id,)).fetchone()
    return row_to_dict(updated)


@app.delete("/api/dialogue/knowledge/{item_id}")
def delete_knowledge(item_id: int) -> dict:
    with connect() as conn:
        conn.execute("DELETE FROM knowledge_items WHERE id = ?", (item_id,))
    return {"deleted": item_id}


@app.post("/api/dialogue/scenes/{scene_id}/label-rules")
def create_label_rule(scene_id: int, payload: LabelRuleCreate) -> dict:
    if not get_scene(scene_id):
        raise HTTPException(status_code=404, detail="Dialogue scene not found")
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO intent_label_rules (scene_id, label, priority, condition_json, enabled)
            VALUES (?, ?, ?, ?, ?)
            """,
            (scene_id, payload.label, payload.priority, json.dumps(payload.condition, ensure_ascii=False), 1 if payload.enabled else 0),
        )
        row = conn.execute("SELECT * FROM intent_label_rules WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_dict(row)


@app.delete("/api/dialogue/label-rules/{rule_id}")
def delete_label_rule(rule_id: int) -> dict:
    with connect() as conn:
        conn.execute("DELETE FROM intent_label_rules WHERE id = ?", (rule_id,))
    return {"deleted": rule_id}


@app.get("/api/dialogue/unresolved")
def list_unresolved(scene_id: int | None = None) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if scene_id:
        clauses.append("scene_id = ?")
        params.append(scene_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM unresolved_questions {where} ORDER BY hit_count DESC, last_seen_at DESC LIMIT 100",
            params,
        ).fetchall()
    return rows_to_dicts(rows)


@app.post("/api/dialogue/turn")
def dialogue_turn(payload: DialogueTurnRequest) -> dict:
    return handle_turn(
        session_id=payload.session_id,
        text=payload.text,
        scene_id=payload.scene_id,
        channel=payload.channel,
        nlu_enabled_override=payload.nlu_enabled,
    )


@app.post("/api/dialogue/start")
def dialogue_start(payload: DialogueStartRequest) -> dict:
    return start_session(session_id=payload.session_id, scene_id=payload.scene_id)


@app.get("/api/ai-model/catalog")
def ai_model_catalog() -> dict[str, Any]:
    return AI_MODEL_CATALOG


@app.get("/api/ai-model/config")
def ai_model_config() -> dict[str, Any]:
    return _load_ai_model_config()


@app.put("/api/ai-model/config")
def update_ai_model_config(payload: AIModelConfigUpdate) -> dict[str, Any]:
    return _save_ai_model_config(payload.dict())


@app.post("/api/dialogue/audio/audition")
async def dialogue_audio_audition(payload: DialogueAudioAuditionRequest) -> dict:
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="AI话术不能为空")
    try:
        audio_bytes, mime_type, request_id, model_config = await _synthesize_selected_audio(text, payload)
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise exc
        logger.exception("Dialogue audio audition failed")
        detail = str(exc) or exc.__class__.__name__
        raise HTTPException(status_code=500, detail=f"在线试听生成失败: {detail}") from exc
    if not audio_bytes:
        raise HTTPException(status_code=500, detail="在线试听生成失败: empty audio")
    record_id = f"tts-{uuid.uuid4().hex}"
    extension = _extension_for_mime(mime_type, ".wav")
    filename = f"{record_id}{extension}"
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    (AUDIO_DIR / filename).write_bytes(audio_bytes)
    record = {
        "id": record_id,
        "name": f"合成音频-{text[:18]}",
        "text": text,
        "source": "synthesis",
        "audio_url": _audio_public_url(filename),
        "mime_type": mime_type,
        "request_id": request_id,
        "model_config": {
            "provider": model_config.get("provider"),
            "model": model_config.get("model"),
            "voice": model_config.get("voice"),
            "emotion": model_config.get("emotion"),
            "style": model_config.get("style"),
        },
        "size": len(audio_bytes),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return _save_audio_record(record)


@app.post("/api/dialogue/audio/upload")
def dialogue_audio_upload(payload: DialogueAudioUploadRequest) -> dict:
    if "," not in payload.data_url:
        raise HTTPException(status_code=400, detail="录音文件格式不正确")
    header, encoded = payload.data_url.split(",", 1)
    mime_type = payload.content_type or "audio/wav"
    if "audio" not in mime_type and "wav" not in payload.name.lower():
        raise HTTPException(status_code=400, detail="仅支持音频录音文件")
    try:
        audio_bytes = base64.b64decode(encoded)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="录音文件解析失败") from exc
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="录音文件为空")
    if len(audio_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="录音文件不能超过 10MB")
    record_id = f"upload-{uuid.uuid4().hex}"
    extension = Path(payload.name).suffix.lower() or _extension_for_mime(mime_type, ".wav")
    if extension != ".wav":
        raise HTTPException(status_code=400, detail="本地上传仅支持 wav 录音文件")
    filename = f"{record_id}{extension}"
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    (AUDIO_DIR / filename).write_bytes(audio_bytes)
    record = {
        "id": record_id,
        "name": payload.name,
        "text": payload.text.strip(),
        "source": "upload",
        "audio_url": _audio_public_url(filename),
        "mime_type": mime_type,
        "size": len(audio_bytes),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return _save_audio_record(record)


@app.get("/api/dialogue/audio/records")
def dialogue_audio_records(source: str = "all") -> list[dict]:
    source_map = {
        "1": "all",
        "2": "upload",
        "3": "synthesis",
        "all": "all",
        "upload": "upload",
        "synthesis": "synthesis",
    }
    normalized = source_map.get(source, "all")
    records = _load_audio_records()
    if normalized == "all":
        return records
    return [record for record in records if record.get("source") == normalized]


@app.get("/api/dashboard")
def dashboard() -> dict:
    with connect() as conn:
        _sync_call_dialogue_state(conn)
        total_calls = conn.execute("SELECT COUNT(*) AS value FROM calls").fetchone()["value"]
        active_calls = conn.execute("SELECT COUNT(*) AS value FROM calls WHERE status = 'active'").fetchone()["value"]
        completed = conn.execute("SELECT COUNT(*) AS value FROM calls WHERE status = 'completed'").fetchone()["value"]
        failed = conn.execute(
            "SELECT COUNT(*) AS value FROM calls WHERE status IN ('failed', 'no_answer', 'busy')"
        ).fetchone()["value"]
        high_intent = conn.execute("SELECT COUNT(*) AS value FROM calls WHERE intent_level = 'high'").fetchone()["value"]
        avg_duration = conn.execute("SELECT COALESCE(AVG(duration_sec), 0) AS value FROM calls WHERE duration_sec > 0").fetchone()["value"]
        status_rows = conn.execute("SELECT status, COUNT(*) AS total FROM calls GROUP BY status ORDER BY total DESC").fetchall()
        intent_rows = conn.execute("SELECT intent_level, COUNT(*) AS total FROM calls GROUP BY intent_level ORDER BY total DESC").fetchall()
        recent_calls = conn.execute(
            f"""
            SELECT {CALL_SELECT_FIELDS}
            {CALL_SELECT_JOINS}
            ORDER BY calls.id DESC
            LIMIT 8
            """
        ).fetchall()

    answer_rate = round((completed / total_calls) * 100, 1) if total_calls else 0
    return {
        "kpis": {
            "total_calls": total_calls,
            "answer_rate": answer_rate,
            "avg_duration": round(avg_duration or 0),
            "high_intent": high_intent,
            "failed": failed,
            "active_calls": active_calls,
        },
        "status_distribution": rows_to_dicts(status_rows),
        "intent_distribution": rows_to_dicts(intent_rows),
        "recent_calls": rows_to_dicts(recent_calls),
    }


@app.get("/api/campaigns")
def list_campaigns() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT campaigns.*,
                   COUNT(calls.id) AS call_count,
                   SUM(CASE WHEN calls.status = 'completed' THEN 1 ELSE 0 END) AS completed_count
            FROM campaigns
            LEFT JOIN calls ON calls.campaign_id = campaigns.id
            GROUP BY campaigns.id
            ORDER BY campaigns.id DESC
            """
        ).fetchall()
    return rows_to_dicts(rows)


@app.post("/api/campaigns")
def create_campaign(payload: CampaignCreate) -> dict:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO campaigns (name, status, prompt, max_concurrency, retry_limit)
            VALUES (?, 'draft', ?, ?, ?)
            """,
            (payload.name, payload.prompt, payload.max_concurrency, payload.retry_limit),
        )
        row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_dict(row)


@app.get("/api/task-stats")
def task_stats() -> list[dict]:
    with connect() as conn:
        _sync_call_dialogue_state(conn)
        rows = conn.execute(
            """
            SELECT campaigns.id,
                   campaigns.name,
                   campaigns.status,
                   COUNT(calls.id) AS total_calls,
                   SUM(CASE WHEN calls.status = 'pending' THEN 1 ELSE 0 END) AS pending_calls,
                   SUM(CASE WHEN calls.status IN ('dialing', 'ringing', 'active') THEN 1 ELSE 0 END) AS active_calls,
                   SUM(CASE WHEN calls.status = 'completed' THEN 1 ELSE 0 END) AS completed_calls,
                   SUM(CASE WHEN calls.status IN ('failed', 'no_answer', 'busy') THEN 1 ELSE 0 END) AS failed_calls,
                   SUM(CASE WHEN calls.intent_level = 'high' THEN 1 ELSE 0 END) AS high_intent_calls,
                   COALESCE(AVG(CASE WHEN calls.duration_sec > 0 THEN calls.duration_sec END), 0) AS avg_duration
            FROM campaigns
            LEFT JOIN calls ON calls.campaign_id = campaigns.id
            GROUP BY campaigns.id
            ORDER BY campaigns.id DESC
            """
        ).fetchall()
    stats = rows_to_dicts(rows)
    for item in stats:
        total = item.get("total_calls") or 0
        item["answer_rate"] = round(((item.get("completed_calls") or 0) / total) * 100, 1) if total else 0
        item["intent_rate"] = round(((item.get("high_intent_calls") or 0) / total) * 100, 1) if total else 0
        item["avg_duration"] = round(item.get("avg_duration") or 0)
    return stats


@app.get("/api/task-templates")
def list_task_templates() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT task_templates.*, dialogue_scenes.name AS scene_name
            FROM task_templates
            LEFT JOIN dialogue_scenes ON dialogue_scenes.id = task_templates.default_scene_id
            ORDER BY task_templates.id DESC
            """
        ).fetchall()
    return rows_to_dicts(rows)


@app.post("/api/task-templates")
def create_task_template(payload: TaskTemplateCreate) -> dict:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO task_templates (
                name, default_prompt, max_concurrency, retry_limit,
                default_scene_id, status, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.name,
                payload.default_prompt,
                payload.max_concurrency,
                payload.retry_limit,
                payload.default_scene_id,
                payload.status,
                payload.notes,
            ),
        )
        row = conn.execute("SELECT * FROM task_templates WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_dict(row)


@app.post("/api/task-templates/{template_id}/campaign")
def create_campaign_from_template(template_id: int) -> dict:
    with connect() as conn:
        template = conn.execute("SELECT * FROM task_templates WHERE id = ?", (template_id,)).fetchone()
        if not template:
            raise HTTPException(status_code=404, detail="Task template not found")
        cur = conn.execute(
            """
            INSERT INTO campaigns (name, status, prompt, max_concurrency, retry_limit)
            VALUES (?, 'draft', ?, ?, ?)
            """,
            (
                f"{template['name']}-新任务",
                template["default_prompt"],
                template["max_concurrency"],
                template["retry_limit"],
            ),
        )
        row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_dict(row)


@app.get("/api/contacts")
def list_contacts() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM contacts ORDER BY id DESC").fetchall()
    return rows_to_dicts(rows)


@app.post("/api/contacts")
def create_contact(payload: ContactCreate) -> dict:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO contacts (name, phone, tags, notes) VALUES (?, ?, ?, ?)",
            (payload.name, payload.phone, payload.tags, payload.notes),
        )
        row = conn.execute("SELECT * FROM contacts WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_dict(row)


@app.post("/api/contacts/import")
def import_contacts(payload: ContactImport) -> dict:
    created = 0
    skipped = 0
    with connect() as conn:
        for contact in payload.contacts:
            exists = conn.execute("SELECT 1 FROM contacts WHERE phone = ?", (contact.phone,)).fetchone()
            if exists:
                skipped += 1
                continue
            conn.execute(
                "INSERT INTO contacts (name, phone, tags, notes) VALUES (?, ?, ?, ?)",
                (contact.name, contact.phone, contact.tags, contact.notes),
            )
            created += 1
    return {"created": created, "skipped": skipped}


@app.get("/api/calls")
def list_calls(status: str | None = None, campaign_id: int | None = None) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if status:
        clauses.append("calls.status = ?")
        params.append(status)
    if campaign_id:
        clauses.append("calls.campaign_id = ?")
        params.append(campaign_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect() as conn:
        _sync_call_dialogue_state(conn)
        rows = conn.execute(
            f"""
            SELECT {CALL_SELECT_FIELDS}
            {CALL_SELECT_JOINS}
            {where}
            ORDER BY calls.id DESC
            """,
            params,
        ).fetchall()
    return rows_to_dicts(rows)


@app.get("/api/dispatch-records")
def list_dispatch_records() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT dispatch_records.*,
                   campaigns.name AS campaign_name,
                   calls.status AS call_status
            FROM dispatch_records
            LEFT JOIN campaigns ON campaigns.id = dispatch_records.campaign_id
            LEFT JOIN calls ON calls.id = dispatch_records.call_id
            ORDER BY dispatch_records.id DESC
            """
        ).fetchall()
    return rows_to_dicts(rows)


@app.post("/api/dispatch-records/{record_id}/retry")
def retry_dispatch_record(record_id: int) -> dict:
    with connect() as conn:
        record = conn.execute("SELECT * FROM dispatch_records WHERE id = ?", (record_id,)).fetchone()
        if not record:
            raise HTTPException(status_code=404, detail="Dispatch record not found")
        cur = conn.execute(
            "INSERT INTO calls (campaign_id, phone, status, caller_name) VALUES (?, ?, 'pending', ?)",
            (record["campaign_id"], record["phone"], record["contact_name"]),
        )
        conn.execute(
            """
            UPDATE dispatch_records
            SET call_id = ?, status = 'pending', failure_reason = '', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (cur.lastrowid, record_id),
        )
        row = conn.execute("SELECT * FROM dispatch_records WHERE id = ?", (record_id,)).fetchone()
    return row_to_dict(row)


@app.get("/api/push-records")
def list_push_records() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT push_records.*, campaigns.name AS campaign_name
            FROM push_records
            LEFT JOIN campaigns ON campaigns.id = push_records.campaign_id
            ORDER BY push_records.id DESC
            """
        ).fetchall()
    return rows_to_dicts(rows)


@app.post("/api/push-records")
def create_push_record(payload: PushRecordCreate) -> dict:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO push_records (campaign_id, target, push_type, content, status)
            VALUES (?, ?, ?, ?, 'success')
            """,
            (payload.campaign_id, payload.target, payload.push_type, payload.content),
        )
        row = conn.execute("SELECT * FROM push_records WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_dict(row)


@app.post("/api/calls")
def create_call(payload: CallCreate) -> dict:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO calls (campaign_id, contact_id, phone, status) VALUES (?, ?, ?, 'pending')",
            (payload.campaign_id, payload.contact_id, payload.phone),
        )
        call_id = cur.lastrowid
        contact_name = ""
        if payload.contact_id:
            contact = conn.execute("SELECT name FROM contacts WHERE id = ?", (payload.contact_id,)).fetchone()
            contact_name = contact["name"] if contact else ""
        if payload.campaign_id:
            conn.execute(
                """
                INSERT INTO dispatch_records (campaign_id, call_id, phone, contact_name, status)
                VALUES (?, ?, ?, ?, 'pending')
                """,
                (payload.campaign_id, call_id, payload.phone, contact_name),
            )
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_dict(row)


@app.patch("/api/calls/{call_id}")
def update_call(call_id: int, payload: CallUpdate) -> dict:
    updates = {key: value for key, value in payload.dict().items() if value is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")

    assignments = ", ".join(f"{key} = ?" for key in updates)
    params = list(updates.values()) + [call_id]
    with connect() as conn:
        conn.execute(f"UPDATE calls SET {assignments} WHERE id = ?", params)
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Call not found")
    return row_to_dict(row)


@app.post("/api/campaigns/{campaign_id}/enqueue")
def enqueue_campaign(campaign_id: int) -> dict:
    """Create pending call rows for campaign contacts that do not already have calls."""
    with connect() as conn:
        campaign = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        contacts = conn.execute("SELECT * FROM contacts ORDER BY id").fetchall()
        created = 0
        for contact in contacts:
            exists = conn.execute(
                "SELECT 1 FROM calls WHERE campaign_id = ? AND contact_id = ?",
                (campaign_id, contact["id"]),
            ).fetchone()
            if exists:
                continue
            call_cur = conn.execute(
                "INSERT INTO calls (campaign_id, contact_id, phone, status) VALUES (?, ?, ?, 'pending')",
                (campaign_id, contact["id"], contact["phone"]),
            )
            conn.execute(
                """
                INSERT INTO dispatch_records (campaign_id, call_id, phone, contact_name, status)
                VALUES (?, ?, ?, ?, 'pending')
                """,
                (campaign_id, call_cur.lastrowid, contact["phone"], contact["name"]),
            )
            created += 1
        conn.execute("UPDATE campaigns SET status = 'queued', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (campaign_id,))
    return {"campaign_id": campaign_id, "created_calls": created}


@app.post("/api/calls/{call_id}/dial")
def dial_call(call_id: int) -> dict:
    """MVP placeholder for LiveKit SIP outbound dialing."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Call not found")
        room_name = row["room_name"] or f"qwen-outbound-{call_id}"
        conn.execute(
            "UPDATE calls SET status = 'dialing', room_name = ? WHERE id = ?",
            (room_name, call_id),
        )
        conn.execute(
            "UPDATE dispatch_records SET status = 'dispatched', room_name = ?, updated_at = CURRENT_TIMESTAMP WHERE call_id = ?",
            (room_name, call_id),
        )
        updated = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    return {
        "call": row_to_dict(updated),
        "message": "Dial request queued. Configure an outbound SIP trunk to place real phone calls.",
    }


@app.post("/api/calls/{call_id}/hangup")
def hangup_call(call_id: int) -> dict:
    """Hang up the LiveKit room/participant, then mark the call completed."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Call not found")
        room_name = row["room_name"] or f"qwen-outbound-{call_id}"
        sip_participant_id = row["sip_participant_id"] or ""

    livekit_result = _hangup_livekit_room(room_name, sip_participant_id)
    microsip_result = _hangup_microsip_all()

    with connect() as conn:
        conn.execute(
            """
            UPDATE calls
            SET status = 'completed',
                room_name = ?,
                ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP),
                duration_sec = CASE
                    WHEN duration_sec > 0 THEN duration_sec
                    ELSE MAX(1, CAST(strftime('%s', 'now') - strftime('%s', COALESCE(started_at, created_at)) AS INTEGER))
                END,
                summary = CASE
                    WHEN COALESCE(summary, '') = '' THEN 'LiveKit hangup requested from operations UI.'
                    ELSE summary
                END
            WHERE id = ?
            """,
            (room_name, call_id),
        )
        conn.execute(
            "UPDATE dispatch_records SET status = 'completed', room_name = ?, updated_at = CURRENT_TIMESTAMP WHERE call_id = ?",
            (room_name, call_id),
        )
        updated = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()

    return {
        "call": row_to_dict(updated),
        "livekit": livekit_result,
        "microsip": microsip_result,
        "message": "LiveKit hangup requested.",
    }


@app.post("/api/calls/{call_id}/simulate")
def simulate_call(call_id: int, payload: CallSimulation) -> dict:
    """Simulate MicroSIP-side call events when no real outbound SIP trunk is available."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Call not found")

        room_name = row["room_name"] or f"qwen-outbound-{call_id}"
        if payload.event == "ringing":
            conn.execute(
                "UPDATE calls SET status = 'ringing', room_name = ? WHERE id = ?",
                (room_name, call_id),
            )
            conn.execute(
                "UPDATE dispatch_records SET status = 'ringing', room_name = ?, updated_at = CURRENT_TIMESTAMP WHERE call_id = ?",
                (room_name, call_id),
            )
            message = "Simulated MicroSIP ringing."
        elif payload.event == "answer":
            conn.execute(
                "UPDATE calls SET status = 'active', room_name = ?, started_at = COALESCE(started_at, CURRENT_TIMESTAMP) WHERE id = ?",
                (room_name, call_id),
            )
            conn.execute(
                "UPDATE dispatch_records SET status = 'active', room_name = ?, updated_at = CURRENT_TIMESTAMP WHERE call_id = ?",
                (room_name, call_id),
            )
            message = "Simulated MicroSIP answered."
        elif payload.event == "hangup":
            conn.execute(
                """
                UPDATE calls
                SET status = 'completed',
                    room_name = ?,
                    ended_at = CURRENT_TIMESTAMP,
                    duration_sec = ?,
                    summary = ?,
                    intent_level = ?
                WHERE id = ?
                """,
                (
                    room_name,
                    payload.duration_sec,
                    payload.summary or "MicroSIP 模拟通话已完成",
                    payload.intent_level,
                    call_id,
                ),
            )
            conn.execute(
                "UPDATE dispatch_records SET status = 'completed', room_name = ?, updated_at = CURRENT_TIMESTAMP WHERE call_id = ?",
                (room_name, call_id),
            )
            message = "Simulated MicroSIP hangup."
        elif payload.event == "no_answer":
            conn.execute(
                """
                UPDATE calls
                SET status = 'no_answer',
                    room_name = ?,
                    ended_at = CURRENT_TIMESTAMP,
                    failure_reason = 'MicroSIP 模拟无人接听'
                WHERE id = ?
                """,
                (room_name, call_id),
            )
            conn.execute(
                "UPDATE dispatch_records SET status = 'failed', room_name = ?, failure_reason = 'no_answer', updated_at = CURRENT_TIMESTAMP WHERE call_id = ?",
                (room_name, call_id),
            )
            message = "Simulated MicroSIP no answer."
        elif payload.event == "busy":
            conn.execute(
                """
                UPDATE calls
                SET status = 'busy',
                    room_name = ?,
                    ended_at = CURRENT_TIMESTAMP,
                    failure_reason = 'MicroSIP 模拟忙线'
                WHERE id = ?
                """,
                (room_name, call_id),
            )
            conn.execute(
                "UPDATE dispatch_records SET status = 'failed', room_name = ?, failure_reason = 'busy', updated_at = CURRENT_TIMESTAMP WHERE call_id = ?",
                (room_name, call_id),
            )
            message = "Simulated MicroSIP busy."
        else:
            conn.execute(
                """
                UPDATE calls
                SET status = 'failed',
                    room_name = ?,
                    ended_at = CURRENT_TIMESTAMP,
                    failure_reason = 'MicroSIP 模拟失败'
                WHERE id = ?
                """,
                (room_name, call_id),
            )
            conn.execute(
                "UPDATE dispatch_records SET status = 'failed', room_name = ?, failure_reason = 'failed', updated_at = CURRENT_TIMESTAMP WHERE call_id = ?",
                (room_name, call_id),
            )
            message = "Simulated MicroSIP failure."

        updated = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()

    return {"call": row_to_dict(updated), "message": message}
