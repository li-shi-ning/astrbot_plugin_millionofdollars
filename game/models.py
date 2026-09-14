"""领域模型：枚举、快照数据结构与严格序列化。

设计依据：``docs/game-implementation-design.md`` 第 4 节。

约定：

* 金额一律使用整数"百万美元"，初始现金 5，即时胜利线 20，不使用浮点数。
* 快照必须携带显式 ``schema_version``，反序列化时做严格校验。
* 秘密角色映射允许进入快照，但不得进入公开回复或常规日志。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = 1
"""快照 schema 版本。字段结构变化时必须递增。"""

INITIAL_CASH = 5
"""每位玩家的初始现金，单位百万美元。"""

INSTANT_WIN_CASH = 20
"""分赃后达到该金额立即获胜。"""

MAX_ROUNDS = 8
"""赃物牌堆张数，也是最后一轮。"""

MAX_PLAYERS = 8
MIN_PLAYERS = 3


class Phase(StrEnum):
    """对局阶段。"""

    LOBBY = "lobby"
    ROLE_SELECTION = "role_selection"
    NEGOTIATION = "negotiation"
    SNITCH_SELECTION = "snitch_selection"
    RESOLVING = "resolving"
    ROUND_END = "round_end"
    GAME_OVER = "game_over"


class Role(StrEnum):
    """五种角色。"""

    DRIVER = "driver"
    BRUTE = "brute"
    CROOK = "crook"
    SNITCH = "snitch"
    MASTERMIND = "mastermind"


PHASE_LABELS: dict[str, str] = {
    Phase.LOBBY: "大厅",
    Phase.ROLE_SELECTION: "选角",
    Phase.NEGOTIATION: "谈判",
    Phase.SNITCH_SELECTION: "告密人指定",
    Phase.RESOLVING: "抢劫结算",
    Phase.ROUND_END: "回合结束",
    Phase.GAME_OVER: "游戏结束",
}
"""给玩家看的阶段中文名，避免把内部英文取值直接展示出来。"""


ROLE_LABELS: dict[str, str] = {
    Role.DRIVER: "司机",
    Role.BRUTE: "暴徒",
    Role.CROOK: "恶棍",
    Role.SNITCH: "告密人",
    Role.MASTERMIND: "谋士",
}


class SnapshotError(ValueError):
    """快照校验失败。"""


class RuleError(ValueError):
    """玩法规则校验失败。"""


def phase_label(phase: str | Phase | None) -> str:
    """返回阶段的中文名，未知阶段返回原值。"""
    if phase is None:
        return ""
    try:
        key = Phase(phase)
    except ValueError:
        return str(phase)
    return PHASE_LABELS.get(key, str(phase))


def role_label(role: str | Role | None) -> str:
    """返回角色的中文名，未知角色返回原值。"""
    if role is None:
        return ""
    return ROLE_LABELS.get(Role(role), str(role))


@dataclass(frozen=True)
class LootCard:
    """赃物牌。

    ``amount`` 为可分赃款，``ante`` 为每人保证金，``bonus_role`` 为额外获益角色。
    """

    card_id: str
    amount: int
    ante: int
    bonus_role: Role | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "amount": self.amount,
            "ante": self.ante,
            "bonus_role": self.bonus_role.value if self.bonus_role else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LootCard:
        bonus = data.get("bonus_role")
        return cls(
            card_id=str(data["card_id"]),
            amount=_require_int(data, "amount"),
            ante=_require_int(data, "ante"),
            bonus_role=Role(bonus) if bonus else None,
        )


@dataclass
class CharacterSlot:
    """人物槽位。

    3 人局每位玩家两个槽位，其余人数每人一个槽位。
    """

    slot_id: str
    role: Role | None = None
    ante_total: int = 0
    player_paid: int = 0
    reserve_paid: int = 0
    active: bool = True
    eliminated: bool = False
    ante_returned: bool = False
    revealed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "role": self.role.value if self.role else None,
            "ante_total": self.ante_total,
            "player_paid": self.player_paid,
            "reserve_paid": self.reserve_paid,
            "active": self.active,
            "eliminated": self.eliminated,
            "ante_returned": self.ante_returned,
            "revealed": self.revealed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CharacterSlot:
        role = data.get("role")
        return cls(
            slot_id=str(data["slot_id"]),
            role=Role(role) if role else None,
            ante_total=_require_int(data, "ante_total"),
            player_paid=_require_int(data, "player_paid"),
            reserve_paid=_require_int(data, "reserve_paid"),
            active=bool(data.get("active", True)),
            eliminated=bool(data.get("eliminated", False)),
            ante_returned=bool(data.get("ante_returned", False)),
            revealed=bool(data.get("revealed", False)),
        )


@dataclass
class Player:
    """玩家。"""

    member_openid: str
    display_name: str
    join_order: int
    cash: int = INITIAL_CASH
    threat_cards: int = 0
    action_generation: int = 0
    ready: bool = False
    slots: list[CharacterSlot] = field(default_factory=list)

    @property
    def active_slots(self) -> list[CharacterSlot]:
        return [slot for slot in self.slots if slot.active]

    def has_active_slot(self) -> bool:
        return any(slot.active for slot in self.slots)

    def slot(self, slot_id: str) -> CharacterSlot | None:
        for slot in self.slots:
            if slot.slot_id == slot_id:
                return slot
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "member_openid": self.member_openid,
            "display_name": self.display_name,
            "join_order": self.join_order,
            "cash": self.cash,
            "threat_cards": self.threat_cards,
            "action_generation": self.action_generation,
            "ready": self.ready,
            "slots": [slot.to_dict() for slot in self.slots],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Player:
        return cls(
            member_openid=str(data["member_openid"]),
            display_name=str(data.get("display_name") or ""),
            join_order=_require_int(data, "join_order"),
            cash=_require_int(data, "cash"),
            threat_cards=_require_int(data, "threat_cards"),
            action_generation=_require_int(data, "action_generation"),
            ready=bool(data.get("ready", False)),
            slots=[CharacterSlot.from_dict(item) for item in data.get("slots", [])],
        )


@dataclass
class SnitchSelection:
    """告密人指定角色的待选择状态。"""

    actor_openid: str
    slot_id: str
    targets: list[Role] = field(default_factory=list)
    chosen: Role | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_openid": self.actor_openid,
            "slot_id": self.slot_id,
            "targets": [role.value for role in self.targets],
            "chosen": self.chosen.value if self.chosen else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SnitchSelection:
        chosen = data.get("chosen")
        return cls(
            actor_openid=str(data["actor_openid"]),
            slot_id=str(data["slot_id"]),
            targets=[Role(item) for item in data.get("targets", [])],
            chosen=Role(chosen) if chosen else None,
        )


@dataclass
class GameSnapshot:
    """完整对局快照。"""

    game_uuid: str
    platform_id: str
    group_openid: str
    phase: Phase = Phase.LOBBY
    round_number: int = 1
    leader_index: int = 0
    players: list[Player] = field(default_factory=list)
    loot_deck: list[LootCard] = field(default_factory=list)
    current_loot_index: int = 0
    public_role_counts: dict[str, int] = field(default_factory=dict)
    hidden_slot_id: str | None = None
    negotiation_started_at: float = 0.0
    force_rob_used: bool = False
    snitch_selection: SnitchSelection | None = None
    winners: list[str] = field(default_factory=list)
    last_public_result: str = ""
    schema_version: int = SCHEMA_VERSION

    # ---- 查询辅助 ----

    @property
    def current_loot(self) -> LootCard | None:
        if 0 <= self.current_loot_index < len(self.loot_deck):
            return self.loot_deck[self.current_loot_index]
        return None

    def player(self, member_openid: str) -> Player | None:
        for player in self.players:
            if player.member_openid == member_openid:
                return player
        return None

    def slot_owner(self, slot_id: str) -> Player | None:
        for player in self.players:
            if player.slot(slot_id) is not None:
                return player
        return None

    def all_slots(self) -> list[CharacterSlot]:
        return [slot for player in self.players for slot in player.slots]

    def find_slot(self, slot_id: str) -> CharacterSlot | None:
        for player in self.players:
            slot = player.slot(slot_id)
            if slot is not None:
                return slot
        return None

    @property
    def leader(self) -> Player | None:
        if not self.players:
            return None
        return self.players[self.leader_index % len(self.players)]

    def clone(self) -> GameSnapshot:
        return copy.deepcopy(self)

    # ---- 序列化 ----

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "game_uuid": self.game_uuid,
            "platform_id": self.platform_id,
            "group_openid": self.group_openid,
            "phase": self.phase.value,
            "round_number": self.round_number,
            "leader_index": self.leader_index,
            "players": [player.to_dict() for player in self.players],
            "loot_deck": [card.to_dict() for card in self.loot_deck],
            "current_loot_index": self.current_loot_index,
            "public_role_counts": dict(self.public_role_counts),
            "hidden_slot_id": self.hidden_slot_id,
            "negotiation_started_at": self.negotiation_started_at,
            "force_rob_used": self.force_rob_used,
            "snitch_selection": (
                self.snitch_selection.to_dict() if self.snitch_selection else None
            ),
            "winners": list(self.winners),
            "last_public_result": self.last_public_result,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GameSnapshot:
        if not isinstance(data, dict):
            raise SnapshotError("快照必须是 JSON 对象。")
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise SnapshotError(
                f"不支持的快照版本：{version!r}，期望 {SCHEMA_VERSION}。"
            )
        try:
            phase = Phase(data.get("phase"))
        except ValueError as exc:
            raise SnapshotError(f"未知阶段：{data.get('phase')!r}") from exc
        snitch_raw = data.get("snitch_selection")
        return cls(
            game_uuid=str(data["game_uuid"]),
            platform_id=str(data["platform_id"]),
            group_openid=str(data["group_openid"]),
            phase=phase,
            round_number=_require_int(data, "round_number"),
            leader_index=_require_int(data, "leader_index"),
            players=[Player.from_dict(item) for item in data.get("players", [])],
            loot_deck=[LootCard.from_dict(item) for item in data.get("loot_deck", [])],
            current_loot_index=_require_int(data, "current_loot_index"),
            public_role_counts={
                str(key): int(value)
                for key, value in (data.get("public_role_counts") or {}).items()
            },
            hidden_slot_id=data.get("hidden_slot_id"),
            negotiation_started_at=float(data.get("negotiation_started_at") or 0.0),
            force_rob_used=bool(data.get("force_rob_used", False)),
            snitch_selection=(
                SnitchSelection.from_dict(snitch_raw) if snitch_raw else None
            ),
            winners=[str(item) for item in data.get("winners", [])],
            last_public_result=str(data.get("last_public_result") or ""),
            schema_version=SCHEMA_VERSION,
        )


def _require_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SnapshotError(f"字段 {key!r} 必须是整数，收到 {value!r}。")
    return value


def public_role_counts(slots: list[CharacterSlot], hidden_slot_id: str | None) -> dict[str, int]:
    """按槽位计算公开角色计数，隐藏槽位不计入。"""
    counts: dict[str, int] = {}
    for slot in slots:
        if slot.role is None:
            continue
        if slot.slot_id == hidden_slot_id:
            continue
        counts[slot.role.value] = counts.get(slot.role.value, 0) + 1
    return counts
