"""房间服务：状态机、锁、幂等与用例编排。

设计依据：``docs/game-implementation-design.md`` 第 5、8、9 节。

本模块返回与平台无关的 :class:`Reply` DTO；QQOfficial 的 keyboard 结构由
``qqofficial.py`` 负责拼装，本模块不得导入 botpy。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import help as help_module
from . import loot as loot_module
from . import rules
from .models import (
    MAX_PLAYERS,
    MIN_PLAYERS,
    CharacterSlot,
    GameSnapshot,
    LootCard,
    Phase,
    Player,
    Role,
    RuleError,
    role_label,
)
from .repository import GameRepository
from .tokens import TokenAction, TokenContext, TokenSigner

DEFAULT_FORCE_ROB_DELAY = 60.0
ACTION_COMMAND_PREFIX = "百万美金 操作 "
MENU_COMMAND_PREFIX = "百万美金 "

MENU_TEXT = """## 百万美金
3～8 人的银行抢劫桌游。所有按钮都等价于一条可以手动输入的文本指令。

**大厅**：创建房间 → 队友加入 → 首领开始（3～8 人）
**谈判**：直接在群里交涉；转账和退出是两件互不关联的独立操作
**结算**：仍有活动槽位的玩家全部准备后自动结算、分赃，现金满 2000 万立即获胜

首次游玩建议点「帮助」查看规则卡；强制抢劫等低频操作直接发送文本指令即可。"""

MENU_ROWS: tuple[tuple[tuple[str, str, str], ...], ...] = (
    (
        ("menu_create", "创建房间", f"{MENU_COMMAND_PREFIX}创建"),
        ("menu_join", "加入", f"{MENU_COMMAND_PREFIX}加入"),
        ("menu_leave_room", "退出房间", f"{MENU_COMMAND_PREFIX}退出房间"),
        ("menu_close", "关闭房间", f"{MENU_COMMAND_PREFIX}关闭"),
    ),
    (
        ("menu_start", "开始游戏", f"{MENU_COMMAND_PREFIX}开始"),
        ("menu_status", "查看状态", f"{MENU_COMMAND_PREFIX}状态"),
    ),
    (
        ("menu_transfer", "转账", f"{MENU_COMMAND_PREFIX}转账"),
        ("menu_leave", "退出本轮", f"{MENU_COMMAND_PREFIX}退出"),
    ),
    (
        ("menu_ready", "准备", f"{MENU_COMMAND_PREFIX}准备"),
        ("menu_unready", "取消准备", f"{MENU_COMMAND_PREFIX}取消准备"),
    ),
    (
        ("menu_threat", "使用威胁牌", f"{MENU_COMMAND_PREFIX}使用威胁牌"),
        ("menu_help", "帮助（规则卡）", f"{MENU_COMMAND_PREFIX}帮助"),
    ),
)
"""菜单按钮：外层是行，内层是 ``(button_id, label, data)``。"""


@dataclass(frozen=True)
class ButtonSpec:
    """与平台无关的按钮描述。"""

    button_id: str
    label: str
    data: str
    visited_label: str = "已提交"
    only_for: str | None = None
    """``None`` 表示公开按钮（``permission.type = 2``），否则为该 openid 专属按钮。"""

    row: int | None = None
    """同一 ``row`` 值的按钮排在同一行；``None`` 时由适配层每 5 个一行自动排列。"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "button_id": self.button_id,
            "label": self.label,
            "data": self.data,
            "visited_label": self.visited_label,
            "only_for": self.only_for,
            "row": self.row,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ButtonSpec:
        row = data.get("row")
        return cls(
            button_id=str(data["button_id"]),
            label=str(data["label"]),
            data=str(data["data"]),
            visited_label=str(data.get("visited_label", "已提交")),
            only_for=data.get("only_for"),
            row=int(row) if row is not None else None,
        )


@dataclass
class Reply:
    """一条待发送的群消息。"""

    text: str
    buttons: list[ButtonSpec] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    """相对插件根目录的图片路径，按顺序在文本之前发送。"""

    reveal_cards: list[str] = field(default_factory=list)
    """身份揭露的卡面键；被隐藏的那张用卡背键，适配层打乱顺序后合并成一张图片。"""

    extra: list[Reply] = field(default_factory=list)
    """需要额外发送的消息（例如每位玩家各自的秘密按钮）。"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "buttons": [button.to_dict() for button in self.buttons],
            "images": list(self.images),
            "reveal_cards": list(self.reveal_cards),
            "extra": [item.to_dict() for item in self.extra],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Reply:
        return cls(
            text=str(data.get("text", "")),
            buttons=[
                ButtonSpec.from_dict(item) for item in data.get("buttons", [])
            ],
            images=[str(item) for item in data.get("images", [])],
            reveal_cards=[str(item) for item in data.get("reveal_cards", [])],
            extra=[Reply.from_dict(item) for item in data.get("extra", [])],
        )


@dataclass(frozen=True)
class RequestContext:
    """一次用户请求的可信上下文。"""

    platform_id: str
    group_openid: str
    member_openid: str
    display_name: str = ""
    message_id: str = ""
    is_admin: bool = False
    """发送者是 AstrBot 管理员（可关闭他人房间）。"""


class GameService:
    """对局服务。"""

    def __init__(
        self,
        repository: GameRepository,
        signer: TokenSigner,
        *,
        deck_factory: Callable[[], Sequence[LootCard]] = loot_module.build_deck,
        now: Callable[[], float] = time.time,
        force_rob_delay: float = DEFAULT_FORCE_ROB_DELAY,
        rng: Any | None = None,
    ) -> None:
        self._repo = repository
        self._signer = signer
        self._deck_factory = deck_factory
        self._now = now
        self._force_rob_delay = force_rob_delay
        self._rng = rng
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # 锁
    # ------------------------------------------------------------------

    def _lock_for(self, platform_id: str, group_openid: str) -> asyncio.Lock:
        key = (platform_id, group_openid)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    # ------------------------------------------------------------------
    # 公开指令
    # ------------------------------------------------------------------

    async def menu(self, ctx: RequestContext) -> Reply:
        """公开菜单：Markdown 说明 + 分行按钮组，所有按钮等价于文本指令。"""
        buttons: list[ButtonSpec] = []
        for row_index, row in enumerate(MENU_ROWS):
            for button_id, label, data in row:
                buttons.append(
                    ButtonSpec(
                        button_id=button_id,
                        label=label,
                        data=data,
                        visited_label=label,
                        row=row_index,
                    )
                )
        return Reply(text=MENU_TEXT, buttons=buttons)

    async def create(self, ctx: RequestContext) -> Reply:
        async with self._lock_for(ctx.platform_id, ctx.group_openid):
            with self._repo.transaction() as conn:
                replay = self._replay(conn, ctx)
                if replay is not None:
                    return replay

                existing = self._repo.load_snapshot(
                    conn, ctx.platform_id, ctx.group_openid
                )
                if existing is not None and existing.phase is not Phase.GAME_OVER:
                    reply = Reply("本群已经有一局未结束的《百万美金》了，先发送「百万美金 状态」查看。")
                    return self._store(conn, ctx, reply)

                snapshot = GameSnapshot(
                    game_uuid=str(uuid.uuid4()),
                    platform_id=ctx.platform_id,
                    group_openid=ctx.group_openid,
                    phase=Phase.LOBBY,
                )
                snapshot.players.append(
                    Player(
                        member_openid=ctx.member_openid,
                        display_name=ctx.display_name or ctx.member_openid[:8],
                        join_order=0,
                    )
                )
                self._repo.store_snapshot(conn, snapshot)
                reply = Reply(
                    text=(
                        f"已创建房间（人数 {_room_size(snapshot)}），"
                        f"{snapshot.players[0].display_name} 成为首领。\n"
                        f"其他玩家发送「百万美金 加入」；"
                        f"至少 {MIN_PLAYERS} 人后首领发送「百万美金 开始」。"
                    ),
                    buttons=[
                        _public_button("lobby_join", "加入", f"{MENU_COMMAND_PREFIX}加入"),
                        _public_button("lobby_start", "开始", f"{MENU_COMMAND_PREFIX}开始"),
                    ],
                )
                return self._store(conn, ctx, reply)

    async def join(self, ctx: RequestContext) -> Reply:
        async with self._lock_for(ctx.platform_id, ctx.group_openid):
            with self._repo.transaction() as conn:
                replay = self._replay(conn, ctx)
                if replay is not None:
                    return replay

                snapshot = self._require_game(conn, ctx)
                if snapshot.phase is not Phase.LOBBY:
                    return self._store(conn, ctx, Reply("对局已经开始，本局无法再加入。"))
                if snapshot.player(ctx.member_openid) is not None:
                    return self._store(conn, ctx, Reply("你已经在房间里了。"))
                if len(snapshot.players) >= 8:
                    return self._store(conn, ctx, Reply("房间已满，最多 8 人。"))

                player = Player(
                    member_openid=ctx.member_openid,
                    display_name=ctx.display_name or ctx.member_openid[:8],
                    join_order=len(snapshot.players),
                )
                snapshot.players.append(player)
                self._repo.store_snapshot(conn, snapshot)
                reply = Reply(
                    text=(
                        f"{player.display_name} 加入了房间（人数 {_room_size(snapshot)}）。\n"
                        f"{_start_hint(snapshot)}"
                    )
                )
                return self._store(conn, ctx, reply)

    async def start(self, ctx: RequestContext) -> Reply:
        async with self._lock_for(ctx.platform_id, ctx.group_openid):
            with self._repo.transaction() as conn:
                replay = self._replay(conn, ctx)
                if replay is not None:
                    return replay

                snapshot = self._require_game(conn, ctx)
                if snapshot.phase is not Phase.LOBBY:
                    return self._store(conn, ctx, Reply("对局已经开始了。"))
                leader = snapshot.leader
                if leader is None or leader.member_openid != ctx.member_openid:
                    return self._store(conn, ctx, Reply("只有首领可以开始游戏。"))
                player_count = len(snapshot.players)
                if not MIN_PLAYERS <= player_count <= MAX_PLAYERS:
                    return self._store(
                        conn,
                        ctx,
                        Reply(
                            f"人数必须是 {MIN_PLAYERS}～{MAX_PLAYERS} 人，"
                            f"当前 {player_count} 人，{_start_hint(snapshot)}"
                        ),
                    )

                snapshot.loot_deck = list(
                    loot_module.draw_loot(
                        self._deck_factory(),
                        rng=_make_rng(self._rng),
                    )
                )
                snapshot.current_loot_index = 0
                snapshot.round_number = 1
                snapshot.leader_index = 0
                snapshot.phase = Phase.ROLE_SELECTION
                for player in snapshot.players:
                    player.slots = _make_slots(len(snapshot.players), player)
                self._repo.store_snapshot(conn, snapshot)

                card = snapshot.current_loot
                reply = Reply(
                    text=_opening_text(snapshot, card),
                    extra=self._role_selection_replies(snapshot),
                )
                return self._store(conn, ctx, reply)

    async def help(self, ctx: RequestContext) -> Reply:
        """帮助接口：输出规则卡图片与规则速览。"""
        card = help_module.rules_card_path()
        images = [help_module.RULES_CARD.relative_path] if card.is_file() else []
        text = help_module.HELP_TEXT
        if not images:
            text += "\n\n（规则卡图片缺失，请联系管理员检查插件文件。）"
        return Reply(text=text, images=images)

    async def leave_room(self, ctx: RequestContext) -> Reply:
        """大厅阶段退出房间。

        参考 ``astrbot_plugin_buckshot_roulette`` 的房间逻辑：

        * 仅开局前可退出；
        * 最后一人退出时房间自动关闭；
        * 首领退出时把首领转交给剩余的第一位玩家。
        """
        async with self._lock_for(ctx.platform_id, ctx.group_openid):
            with self._repo.transaction() as conn:
                replay = self._replay(conn, ctx)
                if replay is not None:
                    return replay
                snapshot = self._repo.load_snapshot(
                    conn, ctx.platform_id, ctx.group_openid
                )
                if snapshot is None:
                    return self._store(conn, ctx, Reply("本群没有进行中的对局。"))
                if snapshot.phase is not Phase.LOBBY:
                    return self._store(
                        conn,
                        ctx,
                        Reply("对局已经开始，不能退出房间（谈判阶段请用「百万美金 退出」）。"),
                    )

                player = snapshot.player(ctx.member_openid)
                if player is None:
                    return self._store(conn, ctx, Reply("你还没有加入本局。"))

                was_leader = snapshot.leader
                leader_left = (
                    was_leader is not None
                    and was_leader.member_openid == player.member_openid
                )
                snapshot.players.remove(player)

                if not snapshot.players:
                    self._repo.delete_snapshot(
                        conn, ctx.platform_id, ctx.group_openid
                    )
                    return self._store(
                        conn,
                        ctx,
                        Reply(
                            f"{player.display_name} 退出了房间，房间已关闭（0/{MAX_PLAYERS} 人）。"
                        ),
                    )

                for index, item in enumerate(snapshot.players):
                    item.join_order = index
                if leader_left:
                    snapshot.leader_index = 0
                else:
                    snapshot.leader_index = next(
                        (
                            index
                            for index, item in enumerate(snapshot.players)
                            if was_leader is not None
                            and item.member_openid == was_leader.member_openid
                        ),
                        0,
                    )
                self._repo.store_snapshot(conn, snapshot)
                leader = snapshot.leader
                leader_text = leader.display_name if leader is not None else "未知"
                extra = "首领已转交给该玩家。" if leader_left else ""
                return self._store(
                    conn,
                    ctx,
                    Reply(
                        f"{player.display_name} 退出了房间（人数 {_room_size(snapshot)}）。"
                        f"{extra}\n当前首领：{leader_text}。{_start_hint(snapshot)}"
                    ),
                )

    async def close_room(self, ctx: RequestContext) -> Reply:
        """关闭当前群房间：首领或 AstrBot 管理员可用，任何阶段都可以关闭。"""
        async with self._lock_for(ctx.platform_id, ctx.group_openid):
            with self._repo.transaction() as conn:
                replay = self._replay(conn, ctx)
                if replay is not None:
                    return replay
                snapshot = self._repo.load_snapshot(
                    conn, ctx.platform_id, ctx.group_openid
                )
                if snapshot is None:
                    return self._store(conn, ctx, Reply("本群没有进行中的对局。"))

                leader = snapshot.leader
                is_leader = (
                    leader is not None and leader.member_openid == ctx.member_openid
                )
                if not (ctx.is_admin or is_leader):
                    return self._store(
                        conn,
                        ctx,
                        Reply("只有首领或管理员可以关闭房间。"),
                    )

                names = "、".join(player.display_name for player in snapshot.players)
                size = _room_size(snapshot)
                self._repo.delete_snapshot(
                    conn, ctx.platform_id, ctx.group_openid
                )
                return self._store(
                    conn,
                    ctx,
                    Reply(
                        f"房间已关闭（关闭前 {size}：{names}）。\n"
                        "需要重新开始时发送「百万美金 创建」。"
                    ),
                )

    async def status(self, ctx: RequestContext) -> Reply:
        async with self._lock_for(ctx.platform_id, ctx.group_openid):
            with self._repo.transaction() as conn:
                replay = self._replay(conn, ctx)
                if replay is not None:
                    return replay
                snapshot = self._require_game(conn, ctx)
                return self._store(conn, ctx, Reply(_status_text(snapshot)))

    async def transfer_menu(self, ctx: RequestContext) -> Reply:
        async with self._lock_for(ctx.platform_id, ctx.group_openid):
            with self._repo.transaction() as conn:
                replay = self._replay(conn, ctx)
                if replay is not None:
                    return replay
                snapshot = self._require_game(conn, ctx)
                player = self._require_active_player(snapshot, ctx)
                if snapshot.phase is not Phase.NEGOTIATION:
                    return self._store(conn, ctx, Reply("只有谈判阶段可以转账。"))
                if player.cash <= 0:
                    return self._store(conn, ctx, Reply("你没有可转账的现金。"))

                buttons = [
                    ButtonSpec(
                        button_id=f"transfer_target_{index}",
                        label=target.display_name,
                        data=f"百万美金转账目标 {target.member_openid}",
                        visited_label="已选择",
                    )
                    for index, target in enumerate(snapshot.players)
                    if target.member_openid != ctx.member_openid
                ]
                if not buttons:
                    return self._store(conn, ctx, Reply("房间里没有其他玩家。"))
                return self._store(
                    conn,
                    ctx,
                    Reply(
                        text=f"你想转账给谁？（当前现金 {player.cash}）",
                        buttons=buttons,
                    ),
                )

    async def transfer_amounts(
        self, ctx: RequestContext, target_openid: str
    ) -> Reply:
        async with self._lock_for(ctx.platform_id, ctx.group_openid):
            with self._repo.transaction() as conn:
                replay = self._replay(conn, ctx)
                if replay is not None:
                    return replay
                snapshot = self._require_game(conn, ctx)
                player = self._require_active_player(snapshot, ctx)
                if snapshot.phase is not Phase.NEGOTIATION:
                    return self._store(conn, ctx, Reply("只有谈判阶段可以转账。"))
                target = snapshot.player(target_openid)
                if target is None:
                    return self._store(conn, ctx, Reply("找不到该玩家。"))
                if target.member_openid == ctx.member_openid:
                    return self._store(conn, ctx, Reply("不能向自己转账。"))
                if player.cash <= 0:
                    return self._store(conn, ctx, Reply("你没有可转账的现金。"))

                token_context = _token_context(snapshot, player)
                buttons = []
                for amount in range(1, player.cash + 1):
                    token = self._signer.issue(
                        token_context,
                        TokenAction("transfer", {"target": target_openid, "amount": amount}),
                    )
                    buttons.append(
                        ButtonSpec(
                            button_id=f"transfer_amount_{amount}",
                            label=f"{amount} 百万",
                            data=f"{ACTION_COMMAND_PREFIX}{token}",
                            only_for=player.member_openid,
                        )
                    )
                return self._store(
                    conn,
                    ctx,
                    Reply(
                        text=(
                            f"转账给 {target.display_name}，选择金额"
                            f"（当前现金 {player.cash}）。\n"
                            "点击按钮后需要发送才能生效。"
                        ),
                        buttons=buttons,
                    ),
                )

    async def leave_menu(self, ctx: RequestContext) -> Reply:
        async with self._lock_for(ctx.platform_id, ctx.group_openid):
            with self._repo.transaction() as conn:
                replay = self._replay(conn, ctx)
                if replay is not None:
                    return replay
                snapshot = self._require_game(conn, ctx)
                player = self._require_active_player(snapshot, ctx)
                if snapshot.phase is not Phase.NEGOTIATION:
                    return self._store(conn, ctx, Reply("只有谈判阶段可以退出。"))

                token_context = _token_context(snapshot, player)
                active = [slot for slot in player.slots if slot.active]
                if len(active) == 1:
                    token = self._signer.issue(
                        token_context,
                        TokenAction("leave", {"slot_id": active[0].slot_id}),
                    )
                    return self._store(
                        conn,
                        ctx,
                        Reply(
                            text="确认退出本轮抢劫？保证金会全额退回。",
                            buttons=[
                                ButtonSpec(
                                    button_id="leave_confirm",
                                    label="退出本轮",
                                    data=f"{ACTION_COMMAND_PREFIX}{token}",
                                    only_for=player.member_openid,
                                )
                            ],
                        ),
                    )

                buttons = [
                    ButtonSpec(
                        button_id=f"leave_slot_{index}",
                        label=f"退出人物 {index + 1}",
                        data=(
                            f"{ACTION_COMMAND_PREFIX}"
                            f"{self._signer.issue(token_context, TokenAction('leave', {'slot_id': slot.slot_id}))}"
                        ),
                        only_for=player.member_openid,
                    )
                    for index, slot in enumerate(active)
                ]
                return self._store(
                    conn,
                    ctx,
                    Reply(
                        text=(
                            "你有两个人物槽位，选择要退出的那一个。"
                            "退出按钮只对你自己可见，点击后需要发送才会生效。"
                        ),
                        buttons=buttons,
                    ),
                )

    async def set_ready(self, ctx: RequestContext, ready: bool) -> Reply:
        async with self._lock_for(ctx.platform_id, ctx.group_openid):
            with self._repo.transaction() as conn:
                replay = self._replay(conn, ctx)
                if replay is not None:
                    return replay
                snapshot = self._require_game(conn, ctx)
                player = self._require_active_player(snapshot, ctx)
                if snapshot.phase is not Phase.NEGOTIATION:
                    return self._store(conn, ctx, Reply("只有谈判阶段可以准备。"))

                player.ready = ready
                events = [f"{player.display_name} {'已准备' if ready else '取消准备'}。"]
                reveal_cards: list[str] = []
                if ready and _all_ready(snapshot):
                    events.append("全员准备完毕，立即结算本轮抢劫。")
                    resolved, reveal_cards = self._resolve(snapshot)
                    events.extend(resolved)
                self._repo.store_snapshot(conn, snapshot)
                reply = Reply(
                    text="\n".join(events),
                    reveal_cards=reveal_cards,
                    extra=self._post_resolution_replies(snapshot),
                )
                return self._store(conn, ctx, reply)

    async def force_rob(self, ctx: RequestContext) -> Reply:
        async with self._lock_for(ctx.platform_id, ctx.group_openid):
            with self._repo.transaction() as conn:
                replay = self._replay(conn, ctx)
                if replay is not None:
                    return replay
                snapshot = self._require_game(conn, ctx)
                self._require_active_player(snapshot, ctx)
                if snapshot.phase is not Phase.NEGOTIATION:
                    return self._store(conn, ctx, Reply("只有谈判阶段可以强制抢劫。"))
                leader = snapshot.leader
                if leader is None or leader.member_openid != ctx.member_openid:
                    return self._store(conn, ctx, Reply("只有当前首领可以强制抢劫。"))
                if snapshot.force_rob_used:
                    return self._store(conn, ctx, Reply("本轮的强制抢劫已经用过了。"))
                elapsed = self._now() - snapshot.negotiation_started_at
                if elapsed < self._force_rob_delay:
                    remaining = int(self._force_rob_delay - elapsed) + 1
                    return self._store(
                        conn,
                        ctx,
                        Reply(f"谈判开始满 {int(self._force_rob_delay)} 秒后才能强制抢劫，还要等约 {remaining} 秒。"),
                    )

                snapshot.force_rob_used = True
                resolved, reveal_cards = self._resolve(snapshot)
                events = ["首领强制结束谈判，立即进入抢劫结算。", *resolved]
                self._repo.store_snapshot(conn, snapshot)
                return self._store(
                    conn,
                    ctx,
                    Reply(
                        text="\n".join(events),
                        reveal_cards=reveal_cards,
                        extra=self._post_resolution_replies(snapshot),
                    ),
                )

    async def threat_card_menu(self, ctx: RequestContext) -> Reply:
        async with self._lock_for(ctx.platform_id, ctx.group_openid):
            with self._repo.transaction() as conn:
                replay = self._replay(conn, ctx)
                if replay is not None:
                    return replay
                snapshot = self._require_game(conn, ctx)
                player = self._require_active_player(snapshot, ctx)
                if snapshot.phase is not Phase.NEGOTIATION:
                    return self._store(conn, ctx, Reply("只有谈判阶段可以使用威胁牌。"))
                if player.threat_cards <= 0:
                    return self._store(conn, ctx, Reply("你没有威胁牌。"))

                targets = [
                    (holder, slot)
                    for holder in snapshot.players
                    for slot in holder.slots
                    if slot.active
                ]
                if not targets:
                    return self._store(conn, ctx, Reply("当前没有可查看的活动人物槽位。"))

                multiple = rules.slots_per_player(len(snapshot.players)) > 1
                buttons = []
                for index, (holder, slot) in enumerate(targets):
                    label = holder.display_name
                    if multiple:
                        label = f"{label}·人物 {index + 1}"
                    buttons.append(
                        ButtonSpec(
                            button_id=f"threat_view_{index}",
                            label=f"查看 {label}",
                            data=_threat_card_data(slot, holder),
                            visited_label="已查看",
                            only_for=player.member_openid,
                        )
                    )
                return self._store(
                    conn,
                    ctx,
                    Reply(
                        text=(
                            "选择要查看的人物。点击后身份会出现在输入框中，"
                            "**请勿发送**；本次查看会消耗 1 张威胁牌。"
                        ),
                        buttons=buttons,
                    ),
                )

    async def handle_token(self, ctx: RequestContext, token: str) -> Reply:
        async with self._lock_for(ctx.platform_id, ctx.group_openid):
            with self._repo.transaction() as conn:
                replay = self._replay(conn, ctx)
                if replay is not None:
                    return replay
                snapshot = self._require_game(conn, ctx)
                player = snapshot.player(ctx.member_openid)
                if player is None:
                    return self._store(conn, ctx, Reply("你不在本局房间内。"))

                candidates = self._candidates(snapshot, player)
                matched = self._signer.match(
                    token, _token_context(snapshot, player), candidates
                )
                if matched is None:
                    return self._store(
                        conn,
                        ctx,
                        Reply("操作已失效或无权执行，请重新点击按钮。"),
                    )

                events, extra, reveal_cards = self._apply(snapshot, player, matched)
                self._repo.store_snapshot(conn, snapshot)
                return self._store(
                    conn,
                    ctx,
                    Reply(
                        text="\n".join(events),
                        reveal_cards=reveal_cards,
                        extra=extra,
                    ),
                )

    # ------------------------------------------------------------------
    # 动作派发
    # ------------------------------------------------------------------

    def _candidates(self, snapshot: GameSnapshot, player: Player) -> list[TokenAction]:
        actions: list[TokenAction] = []
        if snapshot.phase is Phase.ROLE_SELECTION:
            chosen = {slot.role for slot in player.slots if slot.role is not None}
            for role in rules.roles_for_player_count(len(snapshot.players)):
                if role not in chosen:
                    actions.append(TokenAction("choose_role", {"role": role.value}))
        elif snapshot.phase is Phase.NEGOTIATION:
            if player.has_active_slot():
                actions.append(TokenAction("ready"))
                actions.append(TokenAction("cancel_ready"))
                for slot in player.slots:
                    if slot.active:
                        actions.append(TokenAction("leave", {"slot_id": slot.slot_id}))
                for target in snapshot.players:
                    if target.member_openid == player.member_openid:
                        continue
                    for amount in range(1, player.cash + 1):
                        actions.append(
                            TokenAction(
                                "transfer",
                                {"target": target.member_openid, "amount": amount},
                            )
                        )
            leader = snapshot.leader
            if leader is not None and leader.member_openid == player.member_openid:
                actions.append(TokenAction("force_rob"))
        elif snapshot.phase is Phase.SNITCH_SELECTION:
            selection = snapshot.snitch_selection
            if (
                selection is not None
                and selection.actor_openid == player.member_openid
                and selection.chosen is None
            ):
                for role in selection.targets:
                    actions.append(TokenAction("snitch_choose", {"role": role.value}))
        return actions

    def _apply(
        self,
        snapshot: GameSnapshot,
        player: Player,
        matched: TokenAction,
    ) -> tuple[list[str], list[Reply], list[str]]:
        extra: list[Reply] = []
        reveal_cards: list[str] = []
        if matched.action == "choose_role":
            events = self._apply_choose_role(snapshot, player, Role(matched.params["role"]))
            if snapshot.phase is Phase.ROLE_SELECTION:
                # 3 人局需要第二次选角：只给当前玩家重新发一组按钮
                extra = self._role_selection_replies(snapshot, only=player)
        elif matched.action == "snitch_choose":
            self._bump(snapshot, player)
            events = rules.resolve_snitch_designation(
                snapshot, Role(matched.params["role"])
            )
            # 五种角色已经结算过，这里只做收尾，不能重复结算
            resolved, reveal_cards = self._finish_heist(snapshot)
            events.extend(resolved)
            extra = self._post_resolution_replies(snapshot)
        elif matched.action == "transfer":
            self._bump(snapshot, player)
            events = rules.transfer(
                snapshot,
                sender_openid=player.member_openid,
                recipient_openid=str(matched.params["target"]),
                amount=int(matched.params["amount"]),
            )
        elif matched.action == "leave":
            self._bump(snapshot, player)
            events = rules.leave_slot(
                snapshot,
                actor_openid=player.member_openid,
                slot_id=str(matched.params["slot_id"]),
            )
        elif matched.action in {"ready", "cancel_ready"}:
            self._bump(snapshot, player)
            ready = matched.action == "ready"
            player.ready = ready
            events = [f"{player.display_name} {'已准备' if ready else '取消准备'}。"]
            if ready and _all_ready(snapshot):
                events.append("全员准备完毕，立即结算本轮抢劫。")
                resolved, reveal_cards = self._resolve(snapshot)
                events.extend(resolved)
                extra = self._post_resolution_replies(snapshot)
        elif matched.action == "force_rob":
            self._bump(snapshot, player)
            snapshot.force_rob_used = True
            resolved, reveal_cards = self._resolve(snapshot)
            events = ["首领强制结束谈判，立即进入抢劫结算。", *resolved]
            extra = self._post_resolution_replies(snapshot)
        else:  # pragma: no cover - 枚举与派发必须保持一致
            raise RuleError(f"未实现的动作：{matched.action}")
        return events, extra, reveal_cards

    def _apply_choose_role(
        self,
        snapshot: GameSnapshot,
        player: Player,
        role: Role,
    ) -> list[str]:
        if snapshot.phase is not Phase.ROLE_SELECTION:
            raise RuleError("当前阶段不能选角。")

        chosen = [slot.role for slot in player.slots if slot.role is not None]
        if len(chosen) >= len(player.slots):
            raise RuleError("你已经选完了本局的角色。")

        rules.validate_single_choice(len(snapshot.players), chosen, role)
        free = next(slot for slot in player.slots if slot.role is None)
        free.role = role
        self._bump(snapshot, player)

        events = [f"{player.display_name} 已提交角色选择。"]
        if _all_roles_chosen(snapshot):
            card = snapshot.current_loot
            if card is None:  # pragma: no cover - 开局必定有牌
                raise RuleError("缺少当前赃物牌。")
            events.extend(rules.collect_antes(snapshot, card))
            rules.publish_roles(snapshot, rng=_make_rng(self._rng))
            snapshot.phase = Phase.NEGOTIATION
            snapshot.negotiation_started_at = self._now()
            events.append(_public_role_text(snapshot, card))
        else:
            events.append("请等待其他玩家提交。")
        return events

    def _resolve(self, snapshot: GameSnapshot) -> tuple[list[str], list[str]]:
        """开始抢劫结算：按固定顺序结算五种角色。

        若进入告密人指定阶段，则本轮尚未结束、先不返回揭露卡面，等
        :meth:`_finish_heist` 收尾时再发。
        """
        events = rules.resolve_heist_roles(snapshot)
        if snapshot.phase is Phase.SNITCH_SELECTION:
            return events, []
        resolved, reveal_cards = self._finish_heist(snapshot)
        return [*events, *resolved], reveal_cards

    def _finish_heist(self, snapshot: GameSnapshot) -> tuple[list[str], list[str]]:
        """收尾本轮抢劫：只剩告密人特例 + 分赃 + 回合结束/胜利。

        返回 ``(公开事件, 身份揭露卡面)``。卡面在此处采集，等于本轮中心牌堆：
        每个提交过角色的槽位一张，被隐藏的那张用卡背代替。
        """
        reveal_cards = _table_card_keys(snapshot)
        events = rules.finish_resolving(snapshot)
        events.extend(self._finish_round(snapshot))
        return events, reveal_cards

    def _finish_round(self, snapshot: GameSnapshot) -> list[str]:
        winners = rules.evaluate_victory(snapshot)
        if winners:
            snapshot.winners = winners
            snapshot.phase = Phase.GAME_OVER
            names = [
                player.display_name
                for player in snapshot.players
                if player.member_openid in winners
            ]
            return ["🏆 游戏结束，获胜者：" + "、".join(names)]
        rules.start_next_round(snapshot)
        return [f"第 {snapshot.round_number} 轮开始，首领是 {snapshot.leader.display_name if snapshot.leader else '未知'}。"]

    def _bump(self, snapshot: GameSnapshot, player: Player) -> None:
        player.action_generation += 1

    def _post_resolution_replies(self, snapshot: GameSnapshot) -> list[Reply]:
        extra: list[Reply] = []
        if snapshot.phase is Phase.SNITCH_SELECTION:
            selection = snapshot.snitch_selection
            snitch = snapshot.player(selection.actor_openid) if selection else None
            if selection is not None and snitch is not None:
                buttons = [
                    ButtonSpec(
                        button_id=f"snitch_{index}",
                        label=role_label(role),
                        data=(
                            f"{ACTION_COMMAND_PREFIX}"
                            f"{self._signer.issue(_token_context(snapshot, snitch), TokenAction('snitch_choose', {'role': role.value}))}"
                        ),
                        only_for=snitch.member_openid,
                    )
                    for index, role in enumerate(selection.targets)
                ]
                extra.append(
                    Reply(
                        text="你是唯一告密人，请指定一种已公开的角色。",
                        buttons=buttons,
                    )
                )
        elif snapshot.phase is Phase.ROLE_SELECTION:
            extra.extend(self._role_selection_replies(snapshot))
        return extra

    def _role_selection_replies(
        self,
        snapshot: GameSnapshot,
        *,
        only: Player | None = None,
    ) -> list[Reply]:
        replies: list[Reply] = []
        for player in snapshot.players:
            if only is not None and player.member_openid != only.member_openid:
                continue
            chosen = [slot.role for slot in player.slots if slot.role is not None]
            if len(chosen) >= len(player.slots):
                continue
            token_context = _token_context(snapshot, player)
            buttons = [
                ButtonSpec(
                    button_id=f"role_{index}",
                    label=role_label(role),
                    data=(
                        f"{ACTION_COMMAND_PREFIX}"
                        f"{self._signer.issue(token_context, TokenAction('choose_role', {'role': role.value}))}"
                    ),
                    only_for=player.member_openid,
                )
                for index, role in enumerate(
                    role
                    for role in rules.roles_for_player_count(len(snapshot.players))
                    if role not in chosen
                )
            ]
            replies.append(
                Reply(
                    text=(
                        f"{player.display_name}，请秘密选择角色"
                        f"（第 {len(chosen) + 1}/{len(player.slots)} 个）。"
                    ),
                    buttons=buttons,
                )
            )
        return replies

    # ------------------------------------------------------------------
    # 事务辅助
    # ------------------------------------------------------------------

    def _require_game(
        self, conn: Any, ctx: RequestContext
    ) -> GameSnapshot:
        snapshot = self._repo.load_snapshot(conn, ctx.platform_id, ctx.group_openid)
        if snapshot is None:
            raise RuleError("本群还没有对局，先发送「百万美金 创建」。")
        return snapshot

    def _require_active_player(
        self, snapshot: GameSnapshot, ctx: RequestContext
    ) -> Player:
        player = snapshot.player(ctx.member_openid)
        if player is None:
            raise RuleError("你不在本局房间内。")
        if snapshot.phase is not Phase.LOBBY and not player.has_active_slot():
            raise RuleError("你的人物槽位已经退出或被淘汰，本阶段无法行动。")
        return player

    def _replay(self, conn: Any, ctx: RequestContext) -> Reply | None:
        cached = self._repo.load_processed(
            conn, ctx.platform_id, ctx.group_openid, ctx.message_id
        )
        if cached is None:
            return None
        return Reply.from_dict(cached)

    def _store(self, conn: Any, ctx: RequestContext, reply: Reply) -> Reply:
        self._repo.store_processed(
            conn,
            ctx.platform_id,
            ctx.group_openid,
            ctx.message_id,
            reply.to_dict(),
        )
        return reply


# ----------------------------------------------------------------------
# 模块级辅助
# ----------------------------------------------------------------------


def _make_rng(rng: Any) -> Any:
    if rng is None:
        return loot_module.secure_rng()
    return rng


def _make_slots(player_count: int, player: Player) -> list[CharacterSlot]:
    count = rules.slots_per_player(player_count)
    return [
        CharacterSlot(slot_id=f"{player.member_openid}:{index}")
        for index in range(count)
    ]


def _token_context(snapshot: GameSnapshot, player: Player) -> TokenContext:
    return TokenContext(
        game_uuid=snapshot.game_uuid,
        group_openid=snapshot.group_openid,
        round_number=snapshot.round_number,
        phase=snapshot.phase.value,
        generation=player.action_generation,
        actor_openid=player.member_openid,
    )


def _room_size(snapshot: GameSnapshot) -> str:
    """房间人数计数，例如 ``3/8 人``。"""
    return f"{len(snapshot.players)}/{MAX_PLAYERS} 人"


def _start_hint(snapshot: GameSnapshot) -> str:
    """根据当前人数给出开局限定提示。"""
    count = len(snapshot.players)
    if count < MIN_PLAYERS:
        return f"还需要 {MIN_PLAYERS - count} 人才能开局（最少 {MIN_PLAYERS} 人）。"
    if snapshot.phase is Phase.LOBBY:
        return "人数已满足，首领发送「百万美金 开始」即可开局。"
    return ""


def _table_card_keys(snapshot: GameSnapshot) -> list[str]:
    """本轮中心牌堆的卡面键。

    每个提交过角色的槽位一张牌（含谈判期退出的槽位：它们的角色牌仍匿名留在
    中央牌堆）；被随机隐藏的那一张用卡背键表示，其余公开。
    """
    keys: list[str] = []
    for player in snapshot.players:
        for slot in player.slots:
            if slot.role is None:
                continue
            if slot.slot_id == snapshot.hidden_slot_id:
                keys.append(help_module.CARD_BACK_KEY)
            else:
                keys.append(slot.role.value)
    return keys


def _all_roles_chosen(snapshot: GameSnapshot) -> bool:
    return all(
        all(slot.role is not None for slot in player.slots)
        for player in snapshot.players
    )


def _all_ready(snapshot: GameSnapshot) -> bool:
    participants = [player for player in snapshot.players if player.has_active_slot()]
    return bool(participants) and all(player.ready for player in participants)


def _public_role_text(snapshot: GameSnapshot, card: LootCard) -> str:
    counts = "、".join(
        f"{role_label(role)}×{count}"
        for role, count in sorted(snapshot.public_role_counts.items())
    )
    bonus = (
        f"，奖励角色：{role_label(card.bonus_role)}"
        if card.bonus_role is not None
        else ""
    )
    return (
        f"第 {snapshot.round_number} 轮赃物牌：赃款 {card.amount} 百万美元，"
        f"保证金 {card.ante} 百万美元{bonus}。\n"
        f"公开角色：{counts or '无'}\n"
        "谈判开始，玩家可以在群里自行交涉，然后使用「百万美金 准备」。"
    )


def _opening_text(snapshot: GameSnapshot, card: LootCard | None) -> str:
    names = "、".join(player.display_name for player in snapshot.players)
    amount = f"{card.amount} 百万美元" if card else "未知"
    return (
        f"游戏开始，共 {len(snapshot.players)} 人：{names}。\n"
        f"第 1 轮赃物牌：赃款 {amount}。\n"
        "每位玩家会收到只对自己可见的选角按钮；也可以发送「百万美金 状态」查看进度。"
    )


def _status_text(snapshot: GameSnapshot) -> str:
    lines = [
        f"人数：{_room_size(snapshot)}",
        f"阶段：{snapshot.phase.value}",
        f"回合：{snapshot.round_number} / {len(snapshot.loot_deck) or 8}",
    ]
    leader = snapshot.leader
    if leader is not None:
        lines.append(f"首领：{leader.display_name}")
    card = snapshot.current_loot
    if card is not None:
        bonus = f"，奖励角色：{role_label(card.bonus_role)}" if card.bonus_role else ""
        lines.append(f"赃物：{card.amount} 百万美元，保证金 {card.ante} 百万美元{bonus}")
    if snapshot.public_role_counts:
        counts = "、".join(
            f"{role_label(role)}×{count}"
            for role, count in sorted(snapshot.public_role_counts.items())
        )
        lines.append(f"公开角色：{counts or '无'}")
    lines.append("玩家：")
    for player in snapshot.players:
        flags = []
        if player.has_active_slot():
            flags.append("在场" if not player.ready else "已准备")
        else:
            flags.append("已退出")
        lines.append(
            f"- {player.display_name}：{player.cash} 百万美元"
            f"｜威胁牌 {player.threat_cards}｜{'/'.join(flags)}"
        )
    if snapshot.phase is Phase.GAME_OVER and snapshot.winners:
        names = [
            player.display_name
            for player in snapshot.players
            if player.member_openid in snapshot.winners
        ]
        lines.append("🏆 获胜者：" + "、".join(names))
    return "\n".join(lines)


def _public_button(button_id: str, label: str, data: str) -> ButtonSpec:
    return ButtonSpec(
        button_id=button_id,
        label=label,
        data=data,
        visited_label="已提交",
    )


def _threat_card_data(slot: CharacterSlot, holder: Player) -> str:
    return (
        f"查看结果：{holder.display_name} 本轮身份是{role_label(slot.role)}。"
        "请勿发送此内容。"
    )
