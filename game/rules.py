"""纯规则函数。

设计依据：``docs/game-implementation-design.md`` 第 5 节与
``docs/million-of-dollars-rules-zh.md``（2016 初版）。

本模块**不得**导入 AstrBot、botpy、sqlite3 或网络组件；所有函数接收
:class:`~game.models.GameSnapshot`，内部在副本上计算并返回公开事件列表，
不修改调用方传入的快照。
"""

from __future__ import annotations

import random
from collections.abc import Sequence

from .models import (
    INSTANT_WIN_CASH,
    MAX_PLAYERS,
    MIN_PLAYERS,
    CharacterSlot,
    GameSnapshot,
    LootCard,
    Phase,
    Player,
    Role,
    RuleError,
    SnitchSelection,
    role_label,
)

__all__ = [
    "roles_for_player_count",
    "slots_per_player",
    "validate_role_selection",
    "collect_antes",
    "publish_roles",
    "refund_ante",
    "transfer",
    "leave_slot",
    "resolve_heist_roles",
    "resolve_snitch_designation",
    "finish_resolving",
    "evaluate_victory",
    "start_next_round",
]

HEIST_ORDER: tuple[Role, ...] = (
    Role.SNITCH,
    Role.BRUTE,
    Role.DRIVER,
    Role.CROOK,
    Role.MASTERMIND,
)
"""抢劫阶段的固定角色结算顺序。"""

_ROLE_SETS: dict[int, tuple[Role, ...]] = {
    3: (Role.DRIVER, Role.BRUTE, Role.CROOK, Role.SNITCH, Role.MASTERMIND),
    4: (Role.DRIVER, Role.BRUTE, Role.CROOK),
    5: (Role.DRIVER, Role.BRUTE, Role.CROOK, Role.SNITCH),
    6: (Role.DRIVER, Role.BRUTE, Role.CROOK, Role.SNITCH),
    7: (
        Role.DRIVER,
        Role.BRUTE,
        Role.CROOK,
        Role.SNITCH,
        Role.MASTERMIND,
    ),
    8: (
        Role.DRIVER,
        Role.BRUTE,
        Role.CROOK,
        Role.SNITCH,
        Role.MASTERMIND,
    ),
}


def assert_player_count(player_count: int) -> None:
    if not MIN_PLAYERS <= player_count <= MAX_PLAYERS:
        raise RuleError(
            f"游戏人数必须是 {MIN_PLAYERS}～{MAX_PLAYERS} 人，当前 {player_count} 人。"
        )


def roles_for_player_count(player_count: int) -> tuple[Role, ...]:
    """返回该人数下可选的角色集合。"""
    assert_player_count(player_count)
    return _ROLE_SETS[player_count]


def slots_per_player(player_count: int) -> int:
    """3 人局每位玩家两个槽位，其余人数每人一个。"""
    assert_player_count(player_count)
    return 2 if player_count == 3 else 1


def validate_role_selection(
    player_count: int,
    chosen_roles: Sequence[Role],
) -> None:
    """校验收角阶段的角色选择。"""
    expected = slots_per_player(player_count)
    roles = roles_for_player_count(player_count)
    if len(chosen_roles) != expected:
        raise RuleError(f"本局每位玩家必须选择 {expected} 个角色。")
    for role in chosen_roles:
        if role not in roles:
            raise RuleError(f"{player_count} 人局不能选择{role_label(role)}。")
    if len(set(chosen_roles)) != len(chosen_roles):
        raise RuleError("同一玩家不能重复选择相同角色。")


def validate_single_choice(
    player_count: int,
    already_chosen: Sequence[Role],
    role: Role,
) -> None:
    """校验 3 人局逐个提交的单次角色选择。"""
    if len(already_chosen) >= slots_per_player(player_count):
        raise RuleError("你已经选完了本局的角色。")
    if role not in roles_for_player_count(player_count):
        raise RuleError(f"{player_count} 人局不能选择{role_label(role)}。")
    if role in already_chosen:
        raise RuleError("同一玩家不能重复选择相同角色。")


def collect_antes(snapshot: GameSnapshot, card: LootCard) -> list[str]:
    """收取当前赃物牌要求的保证金，现金不足时由储备区补齐。

    返回公开事件；``player_paid`` 与 ``reserve_paid`` 分开记录，便于结算与退款
    时避免重复扣款。
    """
    events: list[str] = []
    for player in snapshot.players:
        for slot in player.slots:
            if slot.role is None:
                continue
            ante = card.ante
            paid = min(player.cash, ante)
            player.cash -= paid
            slot.ante_total = ante
            slot.player_paid = paid
            slot.reserve_paid = ante - paid
            if slot.reserve_paid:
                events.append(
                    f"{player.display_name} 现金不足，储备区补齐 {slot.reserve_paid} 百万美元保证金。"
                )
    return events


def refund_ante(player: Player, slot: CharacterSlot) -> None:
    """退还完整保证金。

    原版规则中不足部分由储备区直接交给玩家，玩家把整笔保证金压在人物牌上；
    因此退保证金时玩家收回**完整** ante，而不是仅收回自己支付的部分。
    该行为由 ``tests/test_rules.py`` 独立覆盖。
    """
    if slot.ante_returned or slot.ante_total <= 0:
        return
    player.cash += slot.ante_total
    slot.ante_returned = True


def publish_roles(
    snapshot: GameSnapshot,
    *,
    rng: random.Random,
) -> list[str]:
    """洗牌已选角色、随机隐藏一个槽位，并公开其余角色的计数。"""
    slots = [slot for slot in snapshot.all_slots() if slot.role is not None]
    if not slots:
        raise RuleError("没有可公开的角色。")
    rng.shuffle(slots)
    hidden = slots[0]
    hidden.revealed = False
    snapshot.hidden_slot_id = hidden.slot_id
    counts: dict[str, int] = {}
    for slot in slots[1:]:
        slot.revealed = True
        counts[slot.role.value] = counts.get(slot.role.value, 0) + 1
    snapshot.public_role_counts = counts
    return []


def public_roles(snapshot: GameSnapshot) -> list[Role]:
    """返回已公开（未被隐藏）的角色集合，用于告密人合法目标。"""
    return [Role(key) for key in sorted(snapshot.public_role_counts)]


def transfer(
    snapshot: GameSnapshot,
    *,
    sender_openid: str,
    recipient_openid: str,
    amount: int,
) -> list[str]:
    """独立原子转账：只改变双方现金并清空全部准备。"""
    if snapshot.phase != Phase.NEGOTIATION:
        raise RuleError("只有谈判阶段可以转账。")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise RuleError("转账金额必须是正整数百万美元。")
    if sender_openid == recipient_openid:
        raise RuleError("不能向自己转账。")

    sender = snapshot.player(sender_openid)
    recipient = snapshot.player(recipient_openid)
    if sender is None or recipient is None:
        raise RuleError("转账双方必须都在本局房间内。")
    if not sender.has_active_slot():
        raise RuleError("你的人物已经退出，不能转账。")
    if amount > sender.cash:
        raise RuleError(
            f"现金不足：你只有 {sender.cash} 百万美元，无法转账 {amount}。"
        )

    sender.cash -= amount
    recipient.cash += amount
    _clear_ready(snapshot)
    return [
        f"{sender.display_name} 转账 {amount} 百万美元给 {recipient.display_name}。"
    ]


def leave_slot(
    snapshot: GameSnapshot,
    *,
    actor_openid: str,
    slot_id: str,
) -> list[str]:
    """独立原子退出：只退出指定槽位并退还其保证金。"""
    if snapshot.phase != Phase.NEGOTIATION:
        raise RuleError("只有谈判阶段可以退出。")
    player = snapshot.player(actor_openid)
    if player is None:
        raise RuleError("你不在本局房间内。")
    slot = snapshot.find_slot(slot_id)
    if slot is None or player.slot(slot_id) is None:
        raise RuleError("找不到这个人物。")
    if not slot.active:
        raise RuleError("这个人物已经退出或被淘汰。")

    slot.active = False
    refund_ante(player, slot)
    _clear_ready(snapshot)
    return [f"{player.display_name} 退出了人物槽位，收回保证金。"]


def resolve_heist_roles(snapshot: GameSnapshot) -> list[str]:
    """按固定顺序结算五种角色，并在需要时进入告密人指定阶段。"""
    if snapshot.phase not in (Phase.NEGOTIATION, Phase.RESOLVING):
        raise RuleError("当前阶段不能进行抢劫结算。")

    events: list[str] = []
    snitch_slots: list[tuple[Player, CharacterSlot]] = []

    for role in HEIST_ORDER:
        bucket: list[tuple[Player, CharacterSlot]] = [
            (player, slot)
            for player in snapshot.players
            for slot in player.slots
            if slot.active and slot.role == role
        ]
        label = role_label(role)

        if len(bucket) >= 2:
            for player, slot in bucket:
                _eliminate(player, slot, refund=role is Role.BRUTE)
            events.append(f"{label} 撞车，{len(bucket)} 名{label}全部被淘汰。")
            continue

        if not bucket:
            continue

        player, slot = bucket[0]
        refund_ante(player, slot)
        if role is Role.BRUTE:
            player.threat_cards += 1
            events.append(f"唯一的{label}收回保证金并获得 1 张威胁牌。")
        elif role is Role.SNITCH:
            events.append(f"唯一的{label}收回保证金。")
            snitch_slots.append((player, slot))
        else:
            events.append(f"唯一的{label}收回保证金。")

    targets = public_roles(snapshot)
    if snitch_slots and targets:
        player, slot = snitch_slots[0]
        snapshot.snitch_selection = SnitchSelection(
            actor_openid=player.member_openid,
            slot_id=slot.slot_id,
            targets=targets,
        )
        snapshot.phase = Phase.SNITCH_SELECTION
    else:
        snapshot.phase = Phase.RESOLVING

    return events


def resolve_snitch_designation(snapshot: GameSnapshot, role: Role) -> list[str]:
    """结算告密人指定的角色：属于该角色的活动槽位全部淘汰。"""
    selection = snapshot.snitch_selection
    if selection is None or snapshot.phase != Phase.SNITCH_SELECTION:
        raise RuleError("当前没有待结算的告密人指定。")
    if role not in selection.targets:
        raise RuleError("该角色不是合法的指定目标。")

    selection.chosen = role
    events = [f"告密人指定了{role_label(role)}。"]
    for player in snapshot.players:
        for slot in player.slots:
            if slot.active and slot.role == role:
                _eliminate(player, slot, refund=role is Role.BRUTE)
    snapshot.phase = Phase.RESOLVING
    return events


def finish_resolving(snapshot: GameSnapshot) -> list[str]:
    """处理"只剩告密人"特例，然后进入分赃或跳过。"""
    if snapshot.phase != Phase.RESOLVING:
        raise RuleError("当前阶段不能结算抢劫结果。")

    active = _active_bucket(snapshot)
    if len(active) == 1:
        player, slot = active[0]
        if slot.role is Role.SNITCH:
            penalty = min(3, player.cash)
            player.cash -= penalty
            _eliminate(player, slot, refund=False)
            snapshot.phase = Phase.ROUND_END
            snapshot.last_public_result = "只剩告密人，本轮跳过分赃。"
            return [
                f"抢劫中只剩告密人一人，{player.display_name} 损失 3 百万美元并被淘汰，本轮跳过分赃。"
            ]

    events = _share_loot(snapshot)
    snapshot.phase = Phase.ROUND_END
    return events


def evaluate_victory(snapshot: GameSnapshot) -> list[str]:
    """分赃后判定胜利。返回胜者 openid 列表（空列表表示继续下一轮）。"""
    threshold_winners = [
        player for player in snapshot.players if player.cash >= INSTANT_WIN_CASH
    ]
    if threshold_winners:
        return _richest(threshold_winners)

    if snapshot.current_loot_index + 1 >= len(snapshot.loot_deck):
        return _richest(snapshot.players)

    return []


def start_next_round(snapshot: GameSnapshot) -> list[str]:
    """清理本轮状态、轮换首领并回到选角阶段。"""
    if not snapshot.players:
        raise RuleError("没有玩家，无法开始下一轮。")
    snapshot.round_number += 1
    snapshot.current_loot_index += 1
    snapshot.leader_index = (snapshot.leader_index + 1) % len(snapshot.players)
    snapshot.phase = Phase.ROLE_SELECTION
    snapshot.public_role_counts = {}
    snapshot.hidden_slot_id = None
    snapshot.snitch_selection = None
    snapshot.negotiation_started_at = 0.0
    snapshot.force_rob_used = False
    snapshot.last_public_result = ""
    for player in snapshot.players:
        player.ready = False
        for slot in player.slots:
            slot.role = None
            slot.ante_total = 0
            slot.player_paid = 0
            slot.reserve_paid = 0
            slot.active = True
            slot.eliminated = False
            slot.ante_returned = False
            slot.revealed = False
    return []


# --------------------------------------------------------------------------
# 内部辅助
# --------------------------------------------------------------------------


def _active_bucket(snapshot: GameSnapshot) -> list[tuple[Player, CharacterSlot]]:
    return [
        (player, slot)
        for player in snapshot.players
        for slot in player.slots
        if slot.active
    ]


def _eliminate(player: Player, slot: CharacterSlot, *, refund: bool) -> None:
    slot.active = False
    slot.eliminated = True
    slot.revealed = True
    if refund:
        refund_ante(player, slot)


def _clear_ready(snapshot: GameSnapshot) -> None:
    for player in snapshot.players:
        player.ready = False


def _share_loot(snapshot: GameSnapshot) -> list[str]:
    card = snapshot.current_loot
    if card is None:
        raise RuleError("缺少当前赃物牌，无法分赃。")

    events: list[str] = []
    active = _active_bucket(snapshot)
    if not active:
        return ["没有玩家参与分赃。"]

    amount = card.amount
    if any(slot.role is Role.MASTERMIND for _, slot in active):
        amount += 2
        events.append("活动谋士使赃款增加 2 百万美元。")

    share = amount // len(active)
    for player, _slot in active:
        player.cash += share
    events.append(
        f"赃款共 {amount} 百万美元，按 {len(active)} 个活动人物槽位平均分配，每份 {share} 百万美元。"
    )

    driver = [item for item in active if item[1].role is Role.DRIVER]
    if len(driver) == 1:
        driver_player = driver[0][0]
        for player, _slot in active:
            pay = min(1, player.cash)
            player.cash -= pay
            driver_player.cash += pay
        events.append(f"每个分得赃款的槽位向司机 {driver_player.display_name} 支付 1 百万美元。")

    crook = [item for item in active if item[1].role is Role.CROOK]
    brute = [item for item in active if item[1].role is Role.BRUTE]
    if len(crook) == 1 and len(brute) == 1:
        crook_player = crook[0][0]
        brute_player = brute[0][0]
        stolen = min(2, brute_player.cash)
        brute_player.cash -= stolen
        crook_player.cash += stolen
        events.append(
            f"恶棍 {crook_player.display_name} 从暴徒 {brute_player.display_name} 处夺取 {stolen} 百万美元。"
        )

    if card.bonus_role is not None:
        bonus = [
            (player, slot)
            for player, slot in active
            if slot.role is card.bonus_role
        ]
        if bonus:
            bonus_player = bonus[0][0]
            bonus_player.cash += 1
            events.append(
                f"赃物牌奖励角色{role_label(card.bonus_role)} {bonus_player.display_name} 额外获得 1 百万美元。"
            )

    snapshot.last_public_result = "；".join(events)
    return events


def _richest(players: Sequence[Player]) -> list[str]:
    if not players:
        return []
    best = max(player.cash for player in players)
    return [player.member_openid for player in players if player.cash == best]
