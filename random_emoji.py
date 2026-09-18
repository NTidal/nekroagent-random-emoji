"""
# 随机表情包 (Random Emoji)

收到"随机表情包"指令后，从本地表情包图库或图床中随机选取一张图片发送到当前聊天。

## 主要功能

- **本地图库**: 从自定义目录（可填绝对路径，或填相对路径挂载在插件数据目录下）中递归扫描图片并随机发送一张。
  默认目录为插件数据目录下的 `emojis/` 文件夹，可直接把表情包图片放进该文件夹。
- **图床**: 在配置中填写远程图片 URL 列表（每行一个），插件会随机选取一张并下载发送。
- **来源优先级**: 通过 `PREFER_LOCAL` 配置控制优先使用本地还是图床，本地无图时自动回退到图床（反之亦然）。

## 使用方法（四种触发方式）

1. **戳一戳触发（跳过 LLM 思考）**: 开启 `ENABLE_POKE_TRIGGER` 后，**机器人被戳一戳**时直接回一张随机表情包（受 `POKE_COOLDOWN` 冷却限制），零延迟、不消耗 token。仅支持 OneBot v11 适配器。
2. **直连触发（推荐，跳过 LLM 思考）**: 开启 `ENABLE_DIRECT_TRIGGER` 后，收到 `TRIGGER_KEYWORDS` 中的指令（默认「随机表情包」等）会**直接发送图片并阻止 LLM 再回复**，零延迟、不消耗 token。
3. **AI 自动触发**: 对 AI 说"随机表情包 / 来张图 / 发张表情包"，AI 调用 `random_emoji` 工具完成发送。
4. **命令直接触发**: 使用 `/随机表情包`（别名 `/random_emoji`、`/来张图`、`/发张图`）。

## 配置说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `EMOJI_DIR` | str | 空 | 本地表情包目录。绝对路径直接用；相对路径相对于插件数据目录。留空则用默认 `emojis/` 文件夹（自建） |
| `REMOTE_EMOJI_URLS` | list[str] | 空 | 图床图片 URL 列表，每行一个；留空则不使用图床 |
| `PREFER_LOCAL` | bool | true | 优先使用本地图库；本地无图时自动回退到图床 |
| `ENABLE_DIRECT_TRIGGER` | bool | true | 启用直连触发：收到触发词即直接发送并跳过 LLM 思考 |
| `TRIGGER_KEYWORDS` | list[str] | 随机表情包等 | 直连触发的触发词列表，消息文本完全等于其中任一即触发 |
| `ENABLE_POKE_TRIGGER` | bool | true | 启用戳一戳触发：机器人被戳一戳时直接回随机表情包并跳过 LLM 思考（仅 OneBot v11） |
| `POKE_COOLDOWN` | int | 10 | 戳一戳触发冷却秒数，同一用户在同一频道内冷却期内重复戳不再回复；0 表示无冷却 |
"""

import random
import time
from pathlib import Path
from typing import Annotated, List, Optional

import httpx
from pydantic import Field

from nekro_agent.api import i18n
from nekro_agent.api.core import logger
from nekro_agent.api.plugin import ConfigBase, ExtraField, NekroPlugin, SandboxMethodType
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.schemas.chat_message import ChatMessage, ChatMessageSegmentType
from nekro_agent.schemas.signal import MsgSignal
from nekro_agent.services.command.base import CommandPermission
from nekro_agent.services.command.ctl import CmdCtl
from nekro_agent.services.command.schemas import (
    Arg,
    CommandExecutionContext,
    CommandOutputSegment,
    CommandOutputSegmentType,
    CommandResponse,
)
from nekro_agent.tools.common_util import copy_to_upload_dir
from nekro_agent.tools.path_convertor import convert_filename_to_sandbox_upload_path

plugin = NekroPlugin(
    name="随机表情包插件",
    module_name="random_emoji",
    description="收到\"随机表情包\"指令或被戳一戳时，从本地图库或图床随机发送一张图片",
    version="0.2.0",
    author="NTidal",
    url="https://github.com/NTidal/nekroagent-random-emoji",
    support_adapter=["onebot_v11", "sse", "discord", "qqbot_openclaw"],
    i18n_name=i18n.i18n_text(
        zh_CN="随机表情包插件",
        en_US="Random Emoji Plugin",
    ),
    i18n_description=i18n.i18n_text(
        zh_CN="收到\"随机表情包\"指令后，从本地图库或图床随机发送一张图片",
        en_US="Randomly send an image from a local library or image hosting when requested",
    ),
    sleep_brief="仅在用户要求发送随机表情包/随机图片时激活。",
)


@plugin.mount_config()
class RandomEmojiConfig(ConfigBase):
    """随机表情包配置"""

    EMOJI_DIR: str = Field(
        default="",
        title="本地表情包目录",
        description="表情包图片所在目录。绝对路径直接用；相对路径相对于插件数据目录。留空则使用默认 emojis/ 文件夹",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(
                zh_CN="本地表情包目录",
                en_US="Local Emoji Directory",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="表情包图片所在目录。留空则使用插件数据目录下的 emojis/ 文件夹",
                en_US="Directory of emoji images. Leave empty to use the emojis/ folder under the plugin data dir",
            ),
        ).model_dump(),
    )
    REMOTE_EMOJI_URLS: List[str] = Field(
        default=[],
        title="图床图片 URL 列表",
        description="远程图床图片链接，每行一个；留空则不使用图床",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(
                zh_CN="图床图片 URL 列表",
                en_US="Image Hosting URL List",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="远程图床图片链接，每行一个；留空则不使用图床",
                en_US="Remote image URLs, one per line; leave empty to disable",
            ),
        ).model_dump(),
    )
    PREFER_LOCAL: bool = Field(
        default=True,
        title="优先使用本地图库",
        description="优先使用本地图库；本地无图时自动回退到图床",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(
                zh_CN="优先使用本地图库",
                en_US="Prefer Local Library",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="优先使用本地图库；本地无图时自动回退到图床",
                en_US="Prefer local library; fall back to image hosting when empty",
            ),
        ).model_dump(),
    )
    ENABLE_DIRECT_TRIGGER: bool = Field(
        default=True,
        title="启用直连触发",
        description="收到触发词时直接发送图片并跳过 LLM 思考（不消耗 token）；关闭则仅由 AI 自主调用",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(
                zh_CN="启用直连触发",
                en_US="Enable Direct Trigger",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="收到触发词时直接发送图片并跳过 LLM 思考；关闭则仅由 AI 自主调用",
                en_US="Send image directly on trigger keyword and skip LLM; disable to rely on AI calling",
            ),
        ).model_dump(),
    )
    TRIGGER_KEYWORDS: List[str] = Field(
        default=["随机表情包", "来张图", "发张图", "发张表情包"],
        title="直连触发词列表",
        description="消息文本完全等于其中任一关键词时，直接发送随机表情包",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(
                zh_CN="直连触发词列表",
                en_US="Direct Trigger Keywords",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="消息文本完全等于其中任一关键词时，直接发送随机表情包",
                en_US="Send a random emoji when message text exactly equals one of these keywords",
            ),
        ).model_dump(),
    )
    RECORD_IMAGE_TO_CONTEXT: bool = Field(
        default=False,
        title="将发送的图片记录到上下文",
        description="是否把发出的随机表情包图片写入对话历史（供 LLM 参考）。默认 false：图片只发给用户，不进 LLM 上下文",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(
                zh_CN="将发送的图片记录到上下文",
                en_US="Record Sent Image to Context",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="是否把发出的随机表情包图片写入对话历史（供 LLM 参考）。默认 false：图片只发给用户，不进 LLM 上下文",
                en_US="Whether to record the sent emoji image into conversation history for the LLM. Default false: image goes only to the user, not the LLM context",
            ),
        ).model_dump(),
    )
    RECORD_TRIGGER_MESSAGE: bool = Field(
        default=True,
        title="记录触发消息",
        description="是否把用户发出的触发词消息（如\"随机表情包\"）记录到对话历史。默认 true：记录但阻止 LLM 触发；false：连触发消息也不记录",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(
                zh_CN="记录触发消息",
                en_US="Record Trigger Message",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="是否把用户发出的触发词消息（如\"随机表情包\"）记录到对话历史。默认 true：记录但阻止 LLM 触发；false：连触发消息也不记录",
                en_US="Whether to record the trigger message into conversation history. Default true: record but block LLM trigger; false: do not record the trigger message either",
            ),
        ).model_dump(),
    )
    ENABLE_POKE_TRIGGER: bool = Field(
        default=True,
        title="被戳一戳时回随机表情",
        description="机器人被戳一戳（poke）时直接发送一张随机表情包并跳过 LLM 思考；仅支持 OneBot v11 适配器",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(
                zh_CN="被戳一戳时回随机表情",
                en_US="Reply Random Emoji on Poke",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="机器人被戳一戳（poke）时直接发送一张随机表情包并跳过 LLM 思考；仅支持 OneBot v11 适配器",
                en_US="Send a random emoji directly when the bot is poked, skipping the LLM. OneBot v11 only",
            ),
        ).model_dump(),
    )
    POKE_COOLDOWN: int = Field(
        default=10,
        title="戳一戳触发冷却（秒）",
        description="同一用户在同一频道内触发戳一戳回复的冷却时间，冷却期内重复戳不再回复；0 表示无冷却",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(
                zh_CN="戳一戳触发冷却（秒）",
                en_US="Poke Trigger Cooldown (seconds)",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="同一用户在同一频道内触发戳一戳回复的冷却时间，冷却期内重复戳不再回复；0 表示无冷却",
                en_US="Cooldown for poke replies per user per channel; repeated pokes within the cooldown are ignored. 0 disables cooldown",
            ),
        ).model_dump(),
    )


config: RandomEmojiConfig = plugin.get_config(RandomEmojiConfig)

# 支持的图片扩展名
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".svg"}

# 戳一戳触发冷却记录：{f"{chat_key}:{platform_userid}": 上次触发时间戳}
_poke_last_trigger: dict[str, float] = {}


def _get_poke_segment(message: ChatMessage):
    """若消息为戳一戳事件，返回 poke 消息段；否则返回 None。

    戳一戳由 OneBot v11 适配器将 notify/poke 通知转换而来，content_data 中含 type 为 "poke" 的段。
    """
    for seg in message.content_data:
        seg_type = getattr(seg, "type", None)
        if seg_type == ChatMessageSegmentType.POKE or seg_type == ChatMessageSegmentType.POKE.value:
            return seg
    return None


def _is_bot_poked(message: ChatMessage) -> bool:
    """判断该戳一戳事件的目标是否为机器人自己。

    优先用适配器标记的 is_tome；若适配器因 BOT_QQ 未配置等原因未置位，
    则比对 poke 段的 target_id 与当前连接 Bot 的 self_id（通过 nonebot 获取）。
    用户之间互戳（目标不是 Bot）返回 False。
    """
    seg = _get_poke_segment(message)
    if seg is None:
        return False
    if message.is_tome:
        return True

    target_id = str(getattr(seg, "target_id", "") or "").strip()
    if not target_id:
        return False
    try:
        from nonebot import get_bots

        bots = get_bots()
        return any(str(getattr(bot, "self_id", "") or "") == target_id for bot in bots.values())
    except Exception as e:  # noqa: BLE001
        logger.debug(f"戳一戳目标判断失败，无法获取 Bot self_id: {e}")
        return False


def _resolve_emoji_dir() -> Path:
    """解析本地表情包目录。

    绝对路径直接使用；相对路径挂载到插件数据目录下；留空则使用默认 emojis/ 文件夹。
    """
    configured = config.EMOJI_DIR.strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_absolute():
            return path
        return plugin.get_plugin_data_dir() / configured
    return plugin.get_plugin_data_dir() / "emojis"


def _scan_local_images() -> List[Path]:
    """递归扫描本地表情包目录中的所有图片文件。"""
    base_dir = _resolve_emoji_dir()
    if not base_dir.is_dir():
        return []
    return [
        p
        for p in base_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    ]


def _pick_local_image() -> Optional[Path]:
    """从本地图库随机选取一张图片。"""
    images = _scan_local_images()
    return random.choice(images) if images else None


async def _send_random_emoji(
    _ctx: AgentCtx,
    prefer_local: Optional[bool] = None,
) -> tuple[bool, str]:
    """选取并发送一张随机表情包，供工具 / 命令 / 消息直连触发共用。

    Args:
        _ctx: AgentCtx 上下文
        prefer_local: 是否优先本地；None 时按配置决定

    Returns:
        (是否成功, 描述信息)
    """
    if prefer_local is None:
        prefer_local = config.PREFER_LOCAL

    candidates = ["local", "remote"] if prefer_local else ["remote", "local"]

    for candidate in candidates:
        if candidate == "local":
            local_path = _pick_local_image()
            if local_path:
                try:
                    # 复制到 uploads 目录并返回 /app/uploads/... 沙盒路径。
                    # 注意：不能走 shared 路径，因为直连触发的 _ctx.container_key 为 None，
                    # shared 路径转换需要 container_key，会报 "Container key is required for shared paths"。
                    host_upload_path, _ = await copy_to_upload_dir(
                        str(local_path),
                        local_path.name,
                        from_chat_key=_ctx.chat_key,
                    )
                    sandbox_path = str(convert_filename_to_sandbox_upload_path(Path(host_upload_path)))
                    # record 由配置控制：默认 False，图发给用户但不写入对话历史，LLM 不会参考
                    await _ctx.send_image(sandbox_path, record=config.RECORD_IMAGE_TO_CONTEXT)
                    return True, f"已发送随机表情包：{local_path.name}"
                except Exception as e:  # noqa: BLE001
                    logger.exception(f"发送本地表情包失败: {local_path}")
                    return False, f"发送本地表情包失败: {e!s}"
        else:
            urls = [u.strip() for u in config.REMOTE_EMOJI_URLS if u and u.strip()]
            if urls:
                url = random.choice(urls)
                try:
                    # mixed_forward_file 的 URL 分支下载到 uploads 目录，返回 /app/uploads/... 路径，
                    # 无需 container_key，直连触发同样可用。
                    sandbox_path = await _ctx.fs.mixed_forward_file(url)
                    # record 由配置控制：默认 False，图发给用户但不写入对话历史，LLM 不会参考
                    await _ctx.send_image(sandbox_path, record=config.RECORD_IMAGE_TO_CONTEXT)
                    return True, f"已发送图床表情包：{url}"
                except Exception as e:  # noqa: BLE001
                    logger.exception(f"发送图床表情包失败: {url}")
                    # 若本地仍可回退且尚未尝试，继续下一候选
                    continue

    return False, "没有可用的表情包图片。请先往表情包目录添加图片，或在配置中填写图床 URL。"


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="随机表情包",
    description="从本地表情包图库或图床中随机选取一张图片并发送到当前聊天。当用户要求\"随机表情包/随机发一张图/来张图/发张表情包\"时调用。",
)
async def random_emoji(_ctx: AgentCtx, source: str = "") -> str:
    """随机发送一张表情包图片。

    Args:
        source (str): 图片来源，可选 `local`（仅本地）/ `remote`（仅图床）/ 留空（按配置自动选择）。

    Returns:
        str: 发送结果描述（已包含发送动作，无需再次发送）。
    """
    prefer_local = config.PREFER_LOCAL
    if source == "local":
        prefer_local = True
    elif source == "remote":
        prefer_local = False

    _ok, msg = await _send_random_emoji(_ctx, prefer_local)
    return msg


@plugin.mount_on_user_message()
async def on_user_message(
    _ctx: AgentCtx,
    message: ChatMessage,
) -> Optional[MsgSignal]:
    """收到用户消息时的直连触发处理：

    1. 戳一戳触发：开启 `ENABLE_POKE_TRIGGER` 后，机器人被戳一戳时直接回一张随机表情包
       （按 用户+频道 维度受 `POKE_COOLDOWN` 冷却限制），跳过 LLM 思考。
    2. 关键词触发：开启 `ENABLE_DIRECT_TRIGGER` 后，消息文本命中触发词则直接发送随机表情包。

    返回 `MsgSignal.BLOCK_TRIGGER` / `BLOCK_ALL`：消息仍会（或不会）被记录到历史，
    但阻止 LLM 继续思考/回复，从而实现"零延迟、不消耗 token"的直接响应。
    """
    # 戳一戳触发：消息为 poke 事件且被戳的是机器人自己（用户之间互戳不响应）
    if config.ENABLE_POKE_TRIGGER and _is_bot_poked(message):
        cd_key = f"{message.chat_key}:{message.platform_userid or message.sender_id}"
        cooldown = max(0, int(config.POKE_COOLDOWN))
        now = time.time()
        if cooldown > 0 and now - _poke_last_trigger.get(cd_key, 0.0) < cooldown:
            # 冷却中：静默忽略，不重复发图，也不触发 LLM
            return MsgSignal.BLOCK_TRIGGER
        _poke_last_trigger[cd_key] = now

        ok, msg = await _send_random_emoji(_ctx)
        if not ok:
            await _ctx.send_text(msg, record=False)
        return MsgSignal.BLOCK_TRIGGER

    if not config.ENABLE_DIRECT_TRIGGER:
        return None

    text = (message.content_text or "").strip()
    keywords = [k.strip() for k in config.TRIGGER_KEYWORDS if k and k.strip()]
    if not text or not keywords:
        return None

    if text not in keywords:
        return None

    ok, msg = await _send_random_emoji(_ctx)
    if not ok:
        await _ctx.send_text(msg, record=False)

    # RECORD_TRIGGER_MESSAGE=true  -> BLOCK_TRIGGER：记录触发消息但阻止 LLM 触发
    # RECORD_TRIGGER_MESSAGE=false -> BLOCK_ALL：连触发消息也不记录
    if config.RECORD_TRIGGER_MESSAGE:
        return MsgSignal.BLOCK_TRIGGER
    return MsgSignal.BLOCK_ALL


@plugin.mount_command(
    name="random_emoji",
    description="随机发送一张表情包图片",
    aliases=["随机表情包", "来张图", "发张图", "发张表情包"],
    permission=CommandPermission.PUBLIC,
    category="表情包",
    usage="/随机表情包",
)
async def random_emoji_cmd(
    context: CommandExecutionContext,
    source: Annotated[str, Arg("图片来源(local/remote，可选)", positional=False)] = "",
) -> CommandResponse:
    """命令方式直接触发随机表情包发送。"""
    prefer_local = config.PREFER_LOCAL
    if source == "remote":
        prefer_local = False
    elif source == "local":
        prefer_local = True

    if prefer_local or not config.REMOTE_EMOJI_URLS:
        local_path = _pick_local_image()
        if local_path:
            # 只返回图片段（命令系统仍会自动加 AI_COMMAND_OUTPUT_PREFIX 前缀，
            # 想要纯图片零文字请使用直连触发，不加 / 前缀）
            return CmdCtl.success([
                CommandOutputSegment(
                    type=CommandOutputSegmentType.IMAGE,
                    file_path=str(local_path),
                ),
            ])

    # 本地无图，回退到图床
    urls = [u.strip() for u in config.REMOTE_EMOJI_URLS if u and u.strip()]
    if urls:
        url = random.choice(urls)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                tmp = _resolve_emoji_dir() / f"_remote_{random.randint(0, 999999)}{Path(url.split('?')[0]).suffix or '.png'}"
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(resp.content)
            return CmdCtl.success([
                CommandOutputSegment(
                    type=CommandOutputSegmentType.IMAGE,
                    file_path=str(tmp),
                ),
            ])
        except Exception as e:  # noqa: BLE001
            logger.exception(f"下载图床表情包失败: {url}")
            return CmdCtl.failed(f"下载图床表情包失败: {e!s}")

    return CmdCtl.failed("没有可用的表情包图片，请先往表情包目录添加图片或配置图床 URL")


@plugin.mount_cleanup_method()
async def clean_up() -> None:
    """清理插件。"""
