"""房间服务集成测试（设计文档 11.1、11.3、11.4）。"""

from __future__ import annotations

import random

import pytest
from game import loot
from game.models import Phase, RuleError
from game.repository import GameRepository
from game.service import ACTION_COMMAND_PREFIX, GameService, Reply, RequestContext
from game.tokens import TokenSigner

pytestmark = pytest.mark.asyncio

SECRET = bytes(range(32))


class Clock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def make_deck():
    """测试用牌组：10 张牌面完全一致，保证断言不受抽牌顺序影响。"""
    entries = [
        {
            "card_id": f"card-{index}",
            "amount": 8,
            "ante": 1,
            "bonus_role": None,
        }
        for index in range(loot.DECK_SIZE)
    ]
    return loot.validate_deck(entries)


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def service(tmp_path, clock) -> GameService:
    repository = GameRepository(tmp_path / "data" / "game.sqlite3")
    repository.initialize()
    return GameService(
        repository,
        TokenSigner(SECRET),
        deck_factory=make_deck,
        now=clock,
        rng=random.Random(7),
    )


def ctx(member: str, message_id: str, name: str = "") -> RequestContext:
    return RequestContext(
        platform_id="qq_official_instance",
        group_openid="group-1",
        member_openid=member,
        display_name=name or member,
        message_id=message_id,
    )


def token_from(reply: Reply) -> str:
    data = reply.buttons[0].data
    assert data.startswith(ACTION_COMMAND_PREFIX)
    return data[len(ACTION_COMMAND_PREFIX) :]


def button_token(button) -> str:
    assert button.data.startswith(ACTION_COMMAND_PREFIX)
    return button.data[len(ACTION_COMMAND_PREFIX) :]


def selection_message(reply: Reply, member: str) -> Reply:
    for item in reply.extra:
        if member in item.text:
            return item
    raise AssertionError(f"没有给 {member} 的选择消息")


async def make_lobby(service: GameService, players: list[str]) -> None:
    await service.create(ctx(players[0], "m-create"))
    for index, member in enumerate(players[1:], start=1):
        await service.join(ctx(member, f"m-join-{index}"))


async def start_game(service: GameService, players: list[str]) -> Reply:
    await make_lobby(service, players)
    return await service.start(ctx(players[0], "m-start"))


async def choose(service: GameService, member: str, reply: Reply, index: int = 0) -> Reply:
    token = button_token(selection_message(reply, member).buttons[index])
    return await service.handle_token(ctx(member, f"m-role-{member}-{index}"), token)


# ----------------------------------------------------------------------
# 大厅
# ----------------------------------------------------------------------


async def test_lobby_flow_and_rejections(service: GameService) -> None:
    reply = await service.create(ctx("a", "m1", "小明"))
    assert "首领" in reply.text

    # 幂等：同一条消息重复处理返回同一结果
    again = await service.create(ctx("a", "m1", "小明"))
    assert again.text == reply.text

    # 新消息重复创建会被拒绝
    duplicate = await service.create(ctx("a", "m2", "小明"))
    assert "已经有一局" in duplicate.text

    # 重复加入被拒绝
    await service.join(ctx("b", "m3", "小红"))
    assert "已经在房间" in (await service.join(ctx("b", "m4", "小红"))).text

    # 非首领不能开始
    assert "只有首领" in (await service.start(ctx("b", "m5"))).text

    # 人数不足不能开始
    assert "3～8" in (await service.start(ctx("a", "m6", "小明"))).text


async def test_status_reports_phase_without_leaking_hidden_roles(
    service: GameService,
) -> None:
    await start_game(service, ["a", "b", "c", "d"])

    status = await service.status(ctx("a", "m-status"))

    assert "阶段" in status.text
    assert "回合" in status.text
    assert "a" in status.text
    assert "隐藏" not in status.text


# ----------------------------------------------------------------------
# 选角
# ----------------------------------------------------------------------


async def test_start_deals_eight_cards_and_private_buttons(service: GameService) -> None:
    reply = await start_game(service, ["a", "b", "c", "d"])

    snapshot = service._repo.load("qq_official_instance", "group-1")
    assert snapshot is not None
    assert len(snapshot.loot_deck) == 8
    assert snapshot.phase is Phase.ROLE_SELECTION
    assert len(reply.extra) == 4
    for item in reply.extra:
        assert len(item.buttons) == 3  # 4 人局只有司机、暴徒、恶棍
        assert item.buttons[0].only_for is not None
        assert item.buttons[0].data.startswith(ACTION_COMMAND_PREFIX)


async def test_role_selection_moves_to_negotiation_and_collects_ante(
    service: GameService,
) -> None:
    players = ["a", "b", "c", "d"]
    start_reply = await start_game(service, players)
    roles = {
        "a": "driver",
        "b": "brute",
        "c": "crook",
        "d": "driver",
    }

    for member in players:
        selection = selection_message(start_reply, member)
        index = [button.label for button in selection.buttons].index(
            {"driver": "司机", "brute": "暴徒", "crook": "恶棍"}[roles[member]]
        )
        token = button_token(selection.buttons[index])
        await service.handle_token(ctx(member, f"role-{member}"), token)

    snapshot = service._repo.load("qq_official_instance", "group-1")
    assert snapshot is not None
    assert snapshot.phase is Phase.NEGOTIATION
    assert snapshot.negotiation_started_at == 1000.0
    assert all(slot.ante_total > 0 for slot in snapshot.all_slots())
    assert sum(snapshot.public_role_counts.values()) == 3  # 4 个槽位隐藏 1 个


async def test_three_players_choose_two_different_roles(service: GameService) -> None:
    players = ["a", "b", "c"]
    start_reply = await start_game(service, players)
    selection = selection_message(start_reply, "a")
    assert len(selection.buttons) == 5

    first_token = button_token(selection.buttons[0])
    first_reply = await service.handle_token(ctx("a", "r-a-1"), first_token)

    # 第二次选角只发给本人，且排除已选角色
    second = selection_message(first_reply, "a")
    labels = [button.label for button in second.buttons]
    assert len(labels) == 4
    assert selection.buttons[0].label not in labels

    # 旧令牌在代次递增后失效
    replay = await service.handle_token(ctx("a", "r-a-1-replay"), first_token)
    assert "失效" in replay.text


async def test_token_replay_is_rejected(service: GameService) -> None:
    players = ["a", "b", "c", "d"]
    start_reply = await start_game(service, players)
    token = button_token(selection_message(start_reply, "a").buttons[0])

    first = await service.handle_token(ctx("a", "replay-1"), token)
    assert "已提交" in first.text

    second = await service.handle_token(ctx("a", "replay-2"), token)
    assert "失效" in second.text


async def test_other_player_cannot_use_someone_elses_button(
    service: GameService,
) -> None:
    players = ["a", "b", "c", "d"]
    start_reply = await start_game(service, players)
    token = button_token(selection_message(start_reply, "a").buttons[0])

    stolen = await service.handle_token(ctx("b", "steal"), token)

    assert "失效" in stolen.text


# ----------------------------------------------------------------------
# 谈判期操作
# ----------------------------------------------------------------------


async def enter_negotiation(service: GameService, roles: dict[str, str]) -> None:
    players = list(roles)
    start_reply = await start_game(service, players)
    labels = {"driver": "司机", "brute": "暴徒", "crook": "恶棍"}
    for member in players:
        selection = selection_message(start_reply, member)
        index = [button.label for button in selection.buttons].index(labels[roles[member]])
        await service.handle_token(
            ctx(member, f"role-{member}"), button_token(selection.buttons[index])
        )


async def test_transfer_moves_money_without_leaving(service: GameService) -> None:
    await enter_negotiation(
        service,
        {"a": "driver", "b": "brute", "c": "crook", "d": "driver"},
    )

    menu = await service.transfer_menu(ctx("a", "t-menu"))
    assert {button.label for button in menu.buttons} == {"b", "c", "d"}

    amounts = await service.transfer_amounts(ctx("a", "t-amounts"), "b")
    button = next(item for item in amounts.buttons if item.label == "2 百万")
    await service.handle_token(ctx("a", "t-confirm"), button_token(button))

    snapshot = service._repo.load("qq_official_instance", "group-1")
    assert snapshot is not None
    assert snapshot.players[0].cash == 2
    assert snapshot.players[1].cash == 6
    assert all(slot.active for slot in snapshot.all_slots())
    assert all(player.ready is False for player in snapshot.players)


async def test_leave_refunds_ante_and_blocks_further_actions(
    service: GameService,
) -> None:
    await enter_negotiation(
        service,
        {"a": "driver", "b": "brute", "c": "crook", "d": "driver"},
    )

    menu = await service.leave_menu(ctx("c", "l-menu"))
    await service.handle_token(ctx("c", "l-confirm"), token_from(menu))

    snapshot = service._repo.load("qq_official_instance", "group-1")
    assert snapshot is not None
    assert snapshot.players[2].slots[0].active is False
    assert snapshot.players[2].cash == 5

    # 已退出的玩家不能继续转账（由 main 捕获后回复文本）
    with pytest.raises(RuleError):
        await service.transfer_menu(ctx("c", "l-transfer"))


async def test_ready_requires_all_participants(service: GameService) -> None:
    await enter_negotiation(
        service,
        {"a": "driver", "b": "brute", "c": "crook", "d": "driver"},
    )

    first = await service.set_ready(ctx("a", "ready-a"), True)
    assert first.text.startswith("a已准备")

    await service.set_ready(ctx("b", "ready-b"), True)
    await service.set_ready(ctx("c", "ready-c"), True)
    last = await service.set_ready(ctx("d", "ready-d"), True)

    assert "立即结算" in last.text
    snapshot = service._repo.load("qq_official_instance", "group-1")
    assert snapshot is not None
    assert snapshot.phase in {Phase.ROLE_SELECTION, Phase.GAME_OVER}


async def test_force_rob_is_leader_only_and_delayed(service: GameService, clock: Clock) -> None:
    await enter_negotiation(
        service,
        {"a": "driver", "b": "brute", "c": "crook", "d": "driver"},
    )

    assert "只有当前首领" in (await service.force_rob(ctx("b", "fr-b"))).text

    early = await service.force_rob(ctx("a", "fr-a"))
    assert "秒" in early.text

    clock.value += 61
    forced = await service.force_rob(ctx("a", "fr-a-late"))
    assert "强制结束谈判" in forced.text

    # 强制抢劫后本轮谈判立即结束
    snapshot = service._repo.load("qq_official_instance", "group-1")
    assert snapshot is not None
    assert snapshot.phase is not Phase.NEGOTIATION


async def test_threat_card_buttons_are_private_and_plaintext(
    service: GameService,
) -> None:
    await enter_negotiation(
        service,
        {"a": "brute", "b": "driver", "c": "crook", "d": "driver"},
    )
    snapshot = service._repo.load("qq_official_instance", "group-1")
    assert snapshot is not None
    # 给 a 手工补一张威胁牌用于测试
    snapshot.players[0].threat_cards = 1
    service._repo.save(snapshot)

    reply = await service.threat_card_menu(ctx("a", "threat"))

    assert reply.buttons
    for button in reply.buttons:
        assert button.only_for == "a"
        assert button.data.startswith("查看结果：")
        assert button.visited_label == "已查看"


async def test_action_token_requires_current_phase(service: GameService) -> None:
    players = ["a", "b", "c", "d"]
    start_reply = await start_game(service, players)
    token = button_token(selection_message(start_reply, "a").buttons[0])

    # 换一个房间使用同一令牌必须失败
    with pytest.raises(RuleError):
        await service.handle_token(
            RequestContext(
                platform_id="qq_official_instance",
                group_openid="group-2",
                member_openid="a",
                display_name="a",
                message_id="cross-room",
            ),
            token,
        )


# ----------------------------------------------------------------------
# 端到端一局
# ----------------------------------------------------------------------


async def test_full_round_reaches_resolution_and_next_round(
    service: GameService,
) -> None:
    """4 人一局：选角 → 转账 → 一个人退出 → 全员准备 → 结算并进入下一轮。"""
    players = ["a", "b", "c", "d"]
    start_reply = await start_game(service, players)
    labels = {"driver": "司机", "brute": "暴徒", "crook": "恶棍"}
    chosen = {"a": "driver", "b": "brute", "c": "crook", "d": "driver"}

    for member in players:
        selection = selection_message(start_reply, member)
        index = [button.label for button in selection.buttons].index(labels[chosen[member]])
        await service.handle_token(
            ctx(member, f"e2e-role-{member}"), button_token(selection.buttons[index])
        )

    # 转账：a 给 b 1 百万
    amounts = await service.transfer_amounts(ctx("a", "e2e-transfer-menu"), "b")
    one = next(button for button in amounts.buttons if button.label == "1 百万")
    await service.handle_token(ctx("a", "e2e-transfer"), button_token(one))

    # d 退出本轮
    leave = await service.leave_menu(ctx("d", "e2e-leave"))
    await service.handle_token(ctx("d", "e2e-leave-confirm"), button_token(leave.buttons[0]))

    # 剩下 a、b、c 准备
    for member in ["a", "b", "c"]:
        await service.set_ready(ctx(member, f"e2e-ready-{member}"), True)

    snapshot = service._repo.load("qq_official_instance", "group-1")
    assert snapshot is not None
    # d 退出后只剩 a（司机）、b（暴徒）、c（恶棍）三个活动槽位
    # 每个玩家：5 - 1 保证金（结算后退回）= 5
    # 再叠加：b 威胁牌 1 张；赃款 8 / 3 = 每份 2；每个分赃槽位向司机 a 付 1；
    #         恶棍 c 从暴徒 b 处夺取 2。
    cash = {player.member_openid: player.cash for player in snapshot.players}
    assert cash["a"] == 5 - 1 + 2 + 2  # 自己那份付给自己净额为 0，只收 b/c 各 1
    assert cash["b"] == 5 + 1 + 2 - 1 - 2
    assert cash["c"] == 5 + 2 - 1 + 2
    assert cash["d"] == 5  # 退出者收回完整保证金
    assert snapshot.players[1].threat_cards == 1  # 唯一暴徒保留威胁牌到下一轮
    assert snapshot.round_number == 2
    assert snapshot.phase is Phase.ROLE_SELECTION


async def test_full_round_with_the_verified_deck(tmp_path, clock) -> None:
    """使用真实已核验牌组跑完一整轮（保证金可能为 2 百万美元）。"""
    repository = GameRepository(tmp_path / "real.sqlite3")
    repository.initialize()
    service = GameService(
        repository,
        TokenSigner(SECRET),
        now=clock,
        rng=random.Random(2026),
    )

    players = ["a", "b", "c", "d"]
    start_reply = await start_game(service, players)
    snapshot = service._repo.load("qq_official_instance", "group-1")
    assert snapshot is not None
    assert len(snapshot.loot_deck) == 8
    assert len({card.card_id for card in snapshot.loot_deck}) == 8
    assert all(card.amount in {8, 9, 10, 12} for card in snapshot.loot_deck)
    ante = snapshot.current_loot.ante
    assert ante in {1, 2}

    labels = {"driver": "司机", "brute": "暴徒", "crook": "恶棍"}
    chosen = {"a": "driver", "b": "brute", "c": "crook", "d": "driver"}
    for member in players:
        selection = selection_message(start_reply, member)
        index = [button.label for button in selection.buttons].index(labels[chosen[member]])
        await service.handle_token(
            ctx(member, f"real-role-{member}"), button_token(selection.buttons[index])
        )

    snapshot = service._repo.load("qq_official_instance", "group-1")
    assert snapshot is not None
    assert snapshot.phase is Phase.NEGOTIATION
    assert all(slot.ante_total == ante for slot in snapshot.all_slots())

    for member in players:
        await service.set_ready(ctx(member, f"real-ready-{member}"), True)

    snapshot = service._repo.load("qq_official_instance", "group-1")
    assert snapshot is not None
    assert snapshot.phase in {Phase.ROLE_SELECTION, Phase.GAME_OVER}
    assert all(player.cash >= 0 for player in snapshot.players)


async def test_full_game_reaches_game_over(tmp_path, clock) -> None:
    """用真实牌组连续跑到第 8 回合结束，验证轮次循环与胜利判定。"""
    repository = GameRepository(tmp_path / "loop.sqlite3")
    repository.initialize()
    service = GameService(
        repository,
        TokenSigner(SECRET),
        now=clock,
        rng=random.Random(99),
    )

    players = ["a", "b", "c", "d"]
    await start_game(service, players)
    labels = {"driver": "司机", "brute": "暴徒", "crook": "恶棍"}
    plan = {"a": "driver", "b": "brute", "c": "crook", "d": "driver"}

    for round_index in range(12):
        snapshot = service._repo.load("qq_official_instance", "group-1")
        assert snapshot is not None
        if snapshot.phase is Phase.GAME_OVER:
            break

        # 每位玩家的选角按钮是各自独立的秘密消息，逐个取回
        for member in players:
            snapshot = service._repo.load("qq_official_instance", "group-1")
            assert snapshot is not None
            selection = next(
                item
                for item in service._role_selection_replies(snapshot)
                if item.buttons and item.buttons[0].only_for == member
            )
            index = [button.label for button in selection.buttons].index(labels[plan[member]])
            token = button_token(selection.buttons[index])
            await service.handle_token(
                ctx(member, f"loop-{round_index}-role-{member}"), token
            )

        for member in players:
            await service.set_ready(
                ctx(member, f"loop-{round_index}-ready-{member}"), True
            )
    else:  # pragma: no cover - 12 轮内必然结束
        raise AssertionError("游戏没有在第 8 回合结束")

    snapshot = service._repo.load("qq_official_instance", "group-1")
    assert snapshot is not None
    assert snapshot.phase is Phase.GAME_OVER
    assert snapshot.winners
    assert snapshot.round_number <= 8
    assert all(player.cash >= 0 for player in snapshot.players)


# ----------------------------------------------------------------------
# 帮助与身份揭露
# ----------------------------------------------------------------------


async def test_help_outputs_the_rules_card(service: GameService) -> None:
    reply = await service.help(ctx("a", "help-1"))

    assert "规则速览" in reply.text
    assert reply.images
    for relative in reply.images:
        assert relative == "docs/sources/rule-cards/rule-card.jpg"
        assert service_help_path(relative).is_file()


def service_help_path(relative: str):
    from game import help as help_module

    return help_module.resolve(relative)


async def test_resolution_reply_carries_the_table_pile_with_card_back(
    service: GameService,
) -> None:
    """揭露图 = 本轮中心牌堆：每个提交过角色的槽位一张，被隐藏的那张是卡背。"""
    from collections import Counter

    players = ["a", "b", "c", "d", "e"]
    start_reply = await start_game(service, players)
    chosen = {
        "a": "driver",
        "b": "brute",
        "c": "crook",
        "d": "snitch",
        "e": "driver",
    }
    labels = {"driver": "司机", "brute": "暴徒", "crook": "恶棍", "snitch": "告密人"}
    for member in players:
        selection = selection_message(start_reply, member)
        index = [button.label for button in selection.buttons].index(labels[chosen[member]])
        await service.handle_token(
            ctx(member, f"reveal-role-{member}"), button_token(selection.buttons[index])
        )

    leave = await service.leave_menu(ctx("d", "reveal-leave-menu"))
    await service.handle_token(ctx("d", "reveal-leave"), token_from(leave))

    reply = None
    for member in ["a", "b", "c", "e"]:
        reply = await service.set_ready(ctx(member, f"reveal-ready-{member}"), True)

    assert reply is not None
    cards = list(reply.reveal_cards)
    submitted = Counter(chosen.values())

    # 5 个槽位各一张牌，其中恰好一张被隐藏成卡背
    assert len(cards) == 5
    assert cards.count("card_back") == 1
    visible = Counter(card for card in cards if card != "card_back")
    # 可见卡面是提交牌面的子集，且只少一张（被隐藏的那张）
    assert sum((submitted - visible).values()) == 1
    # 谈判期退出的玩家，其角色牌仍匿名留在中央牌堆
    assert submitted - visible


async def test_snitch_selection_defers_the_reveal_image(service: GameService) -> None:
    """进入告密人指定阶段时先不发揭露图，等本轮真正结算再发。"""
    players = ["a", "b", "c", "d", "e"]
    start_reply = await start_game(service, players)
    chosen = {
        "a": "snitch",
        "b": "driver",
        "c": "brute",
        "d": "crook",
        "e": "driver",
    }
    labels = {"driver": "司机", "brute": "暴徒", "crook": "恶棍", "snitch": "告密人"}
    for member in players:
        selection = selection_message(start_reply, member)
        index = [button.label for button in selection.buttons].index(labels[chosen[member]])
        await service.handle_token(
            ctx(member, f"snitch-role-{member}"), button_token(selection.buttons[index])
        )

    reply = None
    for member in players:
        reply = await service.set_ready(ctx(member, f"snitch-ready-{member}"), True)

    assert reply is not None
    snapshot = service._repo.load("qq_official_instance", "group-1")
    assert snapshot is not None
    assert snapshot.phase is Phase.SNITCH_SELECTION
    assert reply.reveal_cards == []

    # 告密人指定后，结算回执带上整堆卡面（含 1 张卡背）
    snitch_reply = service._post_resolution_replies(snapshot)
    assert snitch_reply
    token = button_token(snitch_reply[0].buttons[0])
    final = await service.handle_token(ctx("a", "snitch-choose"), token)

    assert len(final.reveal_cards) == 5
    assert final.reveal_cards.count("card_back") == 1


async def test_menu_matches_the_reference_layout(service: GameService) -> None:
    """菜单参考恶魔轮盘插件：Markdown 说明 + 分行按钮组，data 等价文本指令。"""
    reply = await service.menu(ctx("a", "menu-1"))

    assert reply.text.startswith("## 百万美金")
    assert "大厅" in reply.text and "谈判" in reply.text and "结算" in reply.text
    assert "帮助" in reply.text

    # 5 行：第一行房间管理 4 个按钮，其余每行 2 个
    rows: dict[int, list] = {}
    for button in reply.buttons:
        rows.setdefault(button.row, []).append(button)
    assert sorted(rows) == [0, 1, 2, 3, 4]
    assert [len(rows[index]) for index in sorted(rows)] == [4, 2, 2, 2, 2]

    # 公开按钮 + 按钮文案即 visited_label + data 是可手动输入的指令
    for button in reply.buttons:
        assert button.only_for is None
        assert button.visited_label == button.label
        assert button.data.startswith("百万美金 ")
        assert button.button_id.startswith("menu_")

    labels = [button.label for button in reply.buttons]
    assert labels[:4] == ["创建房间", "加入", "退出房间", "关闭房间"]
    assert "帮助（规则卡）" in labels
    assert next(b.data for b in reply.buttons if b.label == "帮助（规则卡）") == "百万美金 帮助"


# ----------------------------------------------------------------------
# 房间管理：人数计数 / 退出房间 / 关闭房间
# ----------------------------------------------------------------------


async def test_lobby_replies_show_player_count(service: GameService) -> None:
    created = await service.create(ctx("a", "cnt-1", "小明"))
    assert "（1/8 人）" in created.text
    assert "至少 3 人" in created.text

    joined = await service.join(ctx("b", "cnt-2", "小红"))
    assert "（2/8 人）" in joined.text
    assert "还需要 1 人才能开局" in joined.text

    await service.join(ctx("c", "cnt-3", "小刚"))
    third = await service.join(ctx("d", "cnt-4", "小强"))
    assert "（4/8 人）" in third.text
    assert "人数已满足" in third.text

    status = await service.status(ctx("a", "cnt-5"))
    assert "人数：4/8" in status.text
    # 大厅阶段显示「已加入」，不显示回合数
    assert "已加入" in status.text
    assert "已退出" not in status.text
    assert "回合" not in status.text


async def test_start_reports_how_many_players_are_missing(
    service: GameService,
) -> None:
    await make_lobby(service, ["a", "b"])
    reply = await service.start(ctx("a", "need-more"))

    assert "当前 2 人" in reply.text
    assert "还需要 1 人才能开局" in reply.text


async def test_leave_room_removes_player_and_tracks_count(
    service: GameService,
) -> None:
    await make_lobby(service, ["a", "b", "c"])
    snapshot = service._repo.load("qq_official_instance", "group-1")
    assert snapshot is not None and len(snapshot.players) == 3

    reply = await service.leave_room(ctx("b", "leave-room-1", "小红"))

    assert "退出了房间" in reply.text
    assert "（2/8 人）" in reply.text
    snapshot = service._repo.load("qq_official_instance", "group-1")
    assert snapshot is not None
    assert [player.member_openid for player in snapshot.players] == ["a", "c"]
    assert [player.join_order for player in snapshot.players] == [0, 1]
    # 非首领退出，首领不变
    assert snapshot.leader is not None and snapshot.leader.member_openid == "a"

    # 没加入的人不能退出
    assert "还没有加入" in (await service.leave_room(ctx("z", "leave-room-2"))).text


async def test_leave_room_transfers_leadership(service: GameService) -> None:
    await make_lobby(service, ["a", "b", "c", "d"])

    reply = await service.leave_room(ctx("a", "leave-leader"))

    assert "首领已转交" in reply.text
    snapshot = service._repo.load("qq_official_instance", "group-1")
    assert snapshot is not None
    assert snapshot.leader is not None
    assert snapshot.leader.member_openid == "b"
    # 转交后仍可正常开局
    assert "游戏开始" in (await service.start(ctx("b", "start-after-leader-left"))).text


async def test_leave_room_closes_the_room_when_empty(service: GameService) -> None:
    await make_lobby(service, ["a"])

    reply = await service.leave_room(ctx("a", "leave-last"))

    assert "房间已关闭" in reply.text
    assert service._repo.load("qq_official_instance", "group-1") is None
    # 关闭后可以重新创建
    assert "已创建房间" in (await service.create(ctx("b", "recreate"))).text


async def test_leave_room_is_lobby_only(service: GameService) -> None:
    await enter_negotiation(
        service,
        {"a": "driver", "b": "brute", "c": "crook", "d": "driver"},
    )

    reply = await service.leave_room(ctx("a", "leave-too-late"))

    assert "对局已经开始" in reply.text
    assert "百万美金 退出" in reply.text


async def test_close_room_requires_leader_or_admin(service: GameService) -> None:
    await make_lobby(service, ["a", "b", "c"])

    assert "只有首领或管理员" in (await service.close_room(ctx("b", "close-b"))).text

    admin = RequestContext(
        platform_id="qq_official_instance",
        group_openid="group-1",
        member_openid="b",
        display_name="小红",
        message_id="close-admin",
        is_admin=True,
    )
    closed = await service.close_room(admin)
    assert "房间已关闭" in closed.text
    assert "3/8 人" in closed.text
    assert service._repo.load("qq_official_instance", "group-1") is None


async def test_close_room_works_mid_game(service: GameService) -> None:
    await enter_negotiation(
        service,
        {"a": "driver", "b": "brute", "c": "crook", "d": "driver"},
    )

    reply = await service.close_room(ctx("a", "close-mid"))

    assert "房间已关闭" in reply.text
    assert service._repo.load("qq_official_instance", "group-1") is None


async def test_close_room_without_game(service: GameService) -> None:
    reply = await service.close_room(ctx("a", "close-empty"))

    assert "没有进行中的对局" in reply.text


async def test_player_facing_text_has_no_internal_jargon(
    service: GameService,
) -> None:
    """玩家看到的文字里不应出现内部英文取值或实现术语。"""
    await make_lobby(service, ["a", "b", "c"])

    texts = [
        (await service.menu(ctx("a", "jargon-1"))).text,
        (await service.help(ctx("a", "jargon-2"))).text,
        (await service.status(ctx("a", "jargon-3"))).text,
    ]
    started = await service.start(ctx("a", "jargon-4"))
    texts.append(started.text)

    for text in texts:
        for internal in (
            "role_selection",
            "snitch_selection",
            "round_end",
            "game_over",
            "phase",
            "槽位",
        ):
            assert internal not in text, f"玩家文案里出现了内部术语：{internal}\n{text}"

    # 阶段用中文展示
    assert "阶段：选角" in (await service.status(ctx("a", "jargon-5"))).text
