"""Render compact Weixin image cards for terminal outbound-call results."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re
from typing import Any

from PIL import Image, ImageDraw, ImageFont


CARD_WIDTH = 1080
CARD_MARGIN = 54
CARD_BACKGROUND = "#F3F6FA"
PANEL_BACKGROUND = "#FFFFFF"
TEXT_PRIMARY = "#172033"
TEXT_SECONDARY = "#667085"
ACCENT = "#1677FF"
SUCCESS = "#12A150"
WARNING = "#D97706"
FAILURE = "#D92D20"

_REGULAR_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/ukai.ttc",
)
_BOLD_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/ukai.ttc",
)


def _font_path(*, bold: bool) -> Path:
    configured = os.getenv(
        "AUDIOAGENT_CARD_FONT_BOLD" if bold else "AUDIOAGENT_CARD_FONT", ""
    ).strip()
    candidates = ([configured] if configured else []) + list(
        _BOLD_FONT_CANDIDATES if bold else _REGULAR_FONT_CANDIDATES
    )
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if candidate and path.is_file():
            return path
    raise RuntimeError(
        "Chinese card font is missing; configure AUDIOAGENT_CARD_FONT or install Noto Sans CJK"
    )


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_font_path(bold=bold)), size=size)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    *,
    max_lines: int,
) -> list[str]:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return []
    lines: list[str] = []
    current = ""
    for character in normalized:
        candidate = current + character
        if current and _text_width(draw, candidate, font) > max_width:
            lines.append(current.rstrip())
            current = character.lstrip()
            if len(lines) == max_lines:
                break
        else:
            current = candidate
    if len(lines) < max_lines and current:
        lines.append(current.rstrip())
    consumed = "".join(lines).replace(" ", "")
    original = normalized.replace(" ", "")
    if len(consumed) < len(original) and lines:
        last = lines[-1]
        while last and _text_width(draw, last + "…", font) > max_width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


def _duration_seconds(item: dict[str, Any]) -> int | None:
    try:
        answered = datetime.fromisoformat(
            str(item.get("answered_at") or "").replace("Z", "+00:00")
        )
        ended = datetime.fromisoformat(
            str(item.get("ended_at") or "").replace("Z", "+00:00")
        )
    except ValueError:
        return None
    return max(0, int((ended - answered).total_seconds()))


def _duration_label(seconds: int | None) -> str:
    if seconds is None:
        return "--"
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes}分{remainder}秒" if minutes else f"{remainder}秒"


def _status_label(value: str) -> str:
    return {
        "completed": "已完成",
        "failed": "失败",
        "cancelled": "已取消",
        "canceled": "已取消",
        "blocked": "已拦截",
        "active": "通话中",
        "running": "进行中",
        "queued": "排队中",
        "draft": "草稿",
    }.get(value.lower(), value or "未知")


def _status_color(value: str) -> str:
    normalized = value.lower()
    if normalized == "completed":
        return SUCCESS
    if normalized in {"failed", "blocked", "cancelled", "canceled"}:
        return FAILURE
    return WARNING


def _summary(item: dict[str, Any]) -> str:
    summary = str(item.get("summary") or "").strip()
    if summary:
        return summary
    detail = str(item.get("failure_detail") or "")
    if "room disconnected" in detail.lower():
        return "客户主动挂断，未形成完整业务摘要。"
    return "本次通话未形成业务摘要。"


def _card_output_path(campaign_id: str) -> Path:
    hermes_home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes")))
    directory = hermes_home / "media" / "audioagent-results"
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "-", campaign_id).strip(".-") or "result"
    return directory / f"{safe_id}.png"


def render_result_card(
    status: dict[str, Any],
    *,
    campaign_id: str,
    output_path: Path | None = None,
) -> Path:
    """Create a readable PNG summary card and return its local path."""

    results = [item for item in status.get("results") or [] if isinstance(item, dict)]
    visible_results = results[:10]
    row_height = 250
    extra_height = 62 if len(results) > len(visible_results) else 0
    height = 310 + max(1, len(visible_results)) * row_height + extra_height + 64
    image = Image.new("RGB", (CARD_WIDTH, height), CARD_BACKGROUND)
    draw = ImageDraw.Draw(image)
    title_font = _font(46, bold=True)
    heading_font = _font(31, bold=True)
    body_font = _font(28)
    small_font = _font(24)
    badge_font = _font(25, bold=True)

    draw.rounded_rectangle((0, 0, CARD_WIDTH, 220), radius=0, fill=ACCENT)
    draw.text((CARD_MARGIN, 42), "电话外呼结果", font=title_font, fill="#FFFFFF")
    campaign_name = str(
        status.get("campaign_name") or status.get("task_id") or "未命名任务"
    )
    title_lines = _wrap_text(
        draw, campaign_name, body_font, CARD_WIDTH - CARD_MARGIN * 2 - 210, max_lines=2
    )
    for index, line in enumerate(title_lines):
        draw.text((CARD_MARGIN, 108 + index * 40), line, font=body_font, fill="#EAF2FF")
    campaign_status = str(status.get("status") or "")
    badge_text = _status_label(campaign_status)
    badge_width = _text_width(draw, badge_text, badge_font) + 44
    draw.rounded_rectangle(
        (CARD_WIDTH - CARD_MARGIN - badge_width, 50, CARD_WIDTH - CARD_MARGIN, 104),
        radius=27,
        fill="#FFFFFF",
    )
    draw.text(
        (CARD_WIDTH - CARD_MARGIN - badge_width + 22, 61),
        badge_text,
        font=badge_font,
        fill=ACCENT,
    )

    total = len(results)
    completed = sum(str(item.get("status") or "").lower() == "completed" for item in results)
    failed = total - completed
    stats_y = 244
    draw.rounded_rectangle(
        (CARD_MARGIN, stats_y, CARD_WIDTH - CARD_MARGIN, stats_y + 90),
        radius=22,
        fill=PANEL_BACKGROUND,
    )
    stats = (
        ("客户", total, TEXT_PRIMARY),
        ("完成", completed, SUCCESS),
        ("其他", failed, FAILURE if failed else TEXT_SECONDARY),
    )
    column_width = (CARD_WIDTH - CARD_MARGIN * 2) // 3
    for index, (label, value, color) in enumerate(stats):
        x = CARD_MARGIN + index * column_width
        if index:
            draw.line((x, stats_y + 20, x, stats_y + 70), fill="#E4E7EC", width=2)
        value_text = str(value)
        draw.text((x + 34, stats_y + 24), value_text, font=heading_font, fill=color)
        draw.text(
            (x + 34 + _text_width(draw, value_text, heading_font) + 16, stats_y + 31),
            label,
            font=small_font,
            fill=TEXT_SECONDARY,
        )

    y = 360
    if not visible_results:
        visible_results = [{"status": campaign_status or "未知"}]
    for index, item in enumerate(visible_results, start=1):
        panel_bottom = y + row_height - 18
        draw.rounded_rectangle(
            (CARD_MARGIN, y, CARD_WIDTH - CARD_MARGIN, panel_bottom),
            radius=24,
            fill=PANEL_BACKGROUND,
        )
        customer = item.get("customer") if isinstance(item.get("customer"), dict) else {}
        name = str(customer.get("name") or f"客户{index}")
        phone = str(item.get("phone") or "")
        phone_label = f"尾号 {phone[-4:]}" if phone else "号码未提供"
        item_status = str(item.get("status") or "")
        draw.text((CARD_MARGIN + 30, y + 24), name, font=heading_font, fill=TEXT_PRIMARY)
        name_width = _text_width(draw, name, heading_font)
        draw.text(
            (CARD_MARGIN + 48 + name_width, y + 31),
            phone_label,
            font=small_font,
            fill=TEXT_SECONDARY,
        )
        call_badge = _status_label(item_status)
        call_badge_width = _text_width(draw, call_badge, badge_font) + 36
        badge_right = CARD_WIDTH - CARD_MARGIN - 28
        draw.rounded_rectangle(
            (badge_right - call_badge_width, y + 22, badge_right, y + 70),
            radius=24,
            fill=_status_color(item_status),
        )
        draw.text(
            (badge_right - call_badge_width + 18, y + 31),
            call_badge,
            font=badge_font,
            fill="#FFFFFF",
        )
        draw.text(
            (CARD_MARGIN + 30, y + 86),
            f"通话时长  {_duration_label(_duration_seconds(item))}",
            font=small_font,
            fill=TEXT_SECONDARY,
        )
        summary_lines = _wrap_text(
            draw,
            _summary(item),
            body_font,
            CARD_WIDTH - CARD_MARGIN * 2 - 60,
            max_lines=2,
        )
        for line_index, line in enumerate(summary_lines):
            draw.text(
                (CARD_MARGIN + 30, y + 128 + line_index * 40),
                line,
                font=body_font,
                fill=TEXT_PRIMARY,
            )
        recent = [str(value).strip() for value in item.get("last_user_messages") or []]
        if recent:
            recent_text = "客户回应：" + " · ".join(recent[-2:])
            recent_lines = _wrap_text(
                draw,
                recent_text,
                small_font,
                CARD_WIDTH - CARD_MARGIN * 2 - 60,
                max_lines=1,
            )
            if recent_lines:
                draw.text(
                    (CARD_MARGIN + 30, y + 204),
                    recent_lines[0],
                    font=small_font,
                    fill=ACCENT,
                )
        y += row_height

    if len(results) > 10:
        draw.text(
            (CARD_MARGIN, y + 4),
            f"另有 {len(results) - 10} 位客户，请在任务详情中查看。",
            font=small_font,
            fill=TEXT_SECONDARY,
        )

    target = output_path or _card_output_path(campaign_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    image.save(temporary, format="PNG", optimize=True)
    temporary.chmod(0o600)
    temporary.replace(target)
    return target
