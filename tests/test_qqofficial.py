"""QQOfficial 适配层测试（设计文档 11.4、11.5）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from game import help as help_module
from game import qqofficial
from game.service import ButtonSpec, Reply


def make_event(
    *,
    module: str = "astrbot.core.platform.sources.qqofficial.qqofficial_message_event",
    class_name: str = "QQOfficialMessageEvent",
    group_openid: str = "group-1",
    member_openid: str = "user-a",
    message_id: str = "msg-1",
    msg_seq: int | None = 7,
    platform_id: str = "instance-1",
):
    raw_message = SimpleNamespace(
        id=message_id,
        msg_seq=msg_seq,
        group_openid=group_openid,
        author=SimpleNamespace(member_openid=member_openid, username="小明"),
    )
    message_obj = SimpleNamespace(raw_message=raw_message, message_id=message_id)
    event_type = type(
        class_name,
        (),
        {
            "__module__": module,
            "message_obj": message_obj,
            "get_group_id": lambda self: group_openid,
            "get_sender_id": lambda self: member_openid,
            "get_sender_name": lambda self: "小明",
            "get_platform_id": lambda self: platform_id,
        },
    )
    return event_type()


# ----------------------------------------------------------------------
# 事件识别与身份提取
# ----------------------------------------------------------------------


def test_is_qqofficial_message_event_accepts_both_adapters() -> None:
    assert qqofficial.is_qqofficial_message_event(make_event())
    assert qqofficial.is_qqofficial_message_event(
        make_event(
            module="astrbot.core.platform.sources.qqofficial_webhook.qo_webhook_event",
            class_name="QQOfficialWebhookMessageEvent",
        )
    )


def test_is_qqofficial_message_event_rejects_other_platforms() -> None:
    other = make_event(
        module="astrbot.core.platform.sources.aiocqhttp.cqhttp_message_event",
        class_name="CQHttpMessageEvent",
    )
    assert qqofficial.is_qqofficial_message_event(other) is False


def test_extract_context_uses_trusted_raw_fields() -> None:
    context = qqofficial.extract_context(make_event())

    assert context is not None
    assert context.platform_id == "instance-1"
    assert context.group_openid == "group-1"
    assert context.member_openid == "user-a"
    assert context.display_name == "小明"
    assert context.message_id == "msg-1"
    assert context.msg_seq == 7


def test_extract_context_falls_back_to_message_obj_id() -> None:
    event = make_event(message_id="msg-1")
    event.message_obj.raw_message.id = None

    context = qqofficial.extract_context(event)

    assert context is not None
    assert context.message_id == "msg-1"


def test_extract_context_returns_none_without_identity() -> None:
    event = make_event(member_openid="")
    event.message_obj.raw_message.author.member_openid = None

    assert qqofficial.extract_context(event) is None


# ----------------------------------------------------------------------
# 按钮与 payload
# ----------------------------------------------------------------------


def test_public_button_uses_permission_type_two() -> None:
    button = qqofficial.build_button(
        ButtonSpec("mod_start", "开始", "百万美金 开始")
    )

    assert button["action"]["type"] == 2
    assert button["action"]["permission"] == {"type": 2}
    assert button["action"]["data"] == "百万美金 开始"
    assert button["action"]["enter"] is False
    assert button["action"]["reply"] is True
    assert button["render_data"]["visited_label"] == "已提交"


def test_private_button_uses_permission_type_zero() -> None:
    button = qqofficial.build_button(
        ButtonSpec("mod_role", "司机", "百万美金 操作 token", only_for="user-a")
    )

    assert button["action"]["permission"] == {
        "type": 0,
        "specify_user_ids": ["user-a"],
    }


def test_secret_button_never_leaks_role_in_visited_label() -> None:
    button = qqofficial.build_button(
        ButtonSpec("mod_role", "司机", "百万美金 操作 token")
    )

    assert button["render_data"]["visited_label"] == "已提交"
    assert "司机" not in button["action"]["data"]


def test_threat_card_button_is_plaintext_and_private() -> None:
    button = qqofficial.build_button(
        ButtonSpec(
            "threat_1",
            "查看 小明",
            "查看结果：小明本轮身份是暴徒。请勿发送此内容。",
            visited_label="已查看",
            only_for="user-a",
        )
    )

    assert "暴徒" in button["action"]["data"]
    assert button["action"]["enter"] is False
    assert button["action"]["type"] == 2
    assert button["action"]["permission"]["type"] == 0


def test_keyboard_chunks_five_buttons_per_row_and_caps_total() -> None:
    buttons = [ButtonSpec(f"b{index}", f"按钮{index}", "百万美金 状态") for index in range(30)]

    keyboard = qqofficial.build_keyboard(buttons)

    assert keyboard is not None
    rows = keyboard["content"]["rows"]
    assert len(rows) == 5
    assert all(len(row["buttons"]) == 5 for row in rows)
    assert len(keyboard["content"]["rows"][0]["buttons"]) <= qqofficial.BUTTONS_PER_ROW


def test_keyboard_is_none_without_buttons() -> None:
    assert qqofficial.build_keyboard([]) is None


def test_build_payload_uses_markdown_when_buttons_exist() -> None:
    reply = Reply(
        text="请选择操作",
        buttons=[ButtonSpec("mod_1", "状态", "百万美金 状态")],
    )

    payload = qqofficial.build_payload(reply)

    assert payload["msg_type"] == 2
    assert payload["markdown"] == {"content": "请选择操作"}
    assert payload["keyboard"]["content"]["rows"][0]["buttons"][0]["id"] == "mod_1"


def test_build_payload_falls_back_to_plain_text() -> None:
    payload = qqofficial.build_payload(Reply(text="纯文本"))

    assert payload["msg_type"] == 0
    assert payload["markdown"] is None
    assert payload["keyboard"] is None
    assert payload["content"] == "纯文本"


# ----------------------------------------------------------------------
# 被动回复
# ----------------------------------------------------------------------


def test_passive_reply_prefers_msg_id() -> None:
    payload = qqofficial.add_passive_reply_context(
        {}, msg_id="msg-1", event_id="event-1", msg_seq=3
    )

    assert payload["msg_id"] == "msg-1"
    assert "event_id" not in payload
    assert payload["msg_seq"] == 3


def test_passive_reply_falls_back_to_event_id_and_generates_seq() -> None:
    payload = qqofficial.add_passive_reply_context({}, event_id="event-1")

    assert payload["event_id"] == "event-1"
    assert 1 <= payload["msg_seq"] <= 10000


def test_passive_reply_without_reference_keeps_payload_clean() -> None:
    payload = qqofficial.add_passive_reply_context({})

    assert payload == {}


# ----------------------------------------------------------------------
# 发送
# ----------------------------------------------------------------------


class _FakeGroupMessage:
    """替代 botpy.message.GroupMessage 的最小实现，用于验证发送路径。"""

    def __init__(self, group_openid: str = "group-1") -> None:
        self.group_openid = group_openid


def _send_event(calls: list, failure: Exception | None = None, sent: list | None = None):
    async def fake_post(**kwargs):
        if failure is not None:
            raise failure
        calls.append(kwargs)
        return {"id": "1"}

    async def fake_send(result):
        if sent is not None:
            sent.append(result)

    return SimpleNamespace(
        message_obj=SimpleNamespace(raw_message=_FakeGroupMessage()),
        bot=SimpleNamespace(api=SimpleNamespace(post_group_message=fake_post)),
        send=fake_send,
        plain_result=lambda text: SimpleNamespace(message_str=text),
        image_result=lambda path: SimpleNamespace(image_path=path),
    )


@pytest.mark.asyncio
async def test_send_reply_uses_group_api_with_passive_context(monkeypatch) -> None:
    monkeypatch.setattr(qqofficial.botpy_message, "GroupMessage", _FakeGroupMessage)
    calls: list = []
    event = _send_event(calls)
    context = qqofficial.QQOfficialContext(
        platform_id="instance-1",
        group_openid="group-1",
        member_openid="user-a",
        display_name="小明",
        message_id="msg-1",
        msg_seq=9,
    )
    reply = Reply(
        text="请选择操作",
        buttons=[ButtonSpec("mod_1", "状态", "百万美金 状态")],
    )

    ok = await qqofficial.send_reply(event, context, reply)

    assert ok is True
    assert len(calls) == 1
    payload = calls[0]
    assert payload["msg_id"] == "msg-1"
    assert payload["msg_seq"] == 9
    assert payload["group_openid"] == "group-1"
    assert payload["markdown"] == {"content": "请选择操作"}
    assert payload["keyboard"]["content"]["rows"]
    assert payload["msg_type"] == 2


@pytest.mark.asyncio
async def test_send_reply_sends_extra_messages(monkeypatch) -> None:
    monkeypatch.setattr(qqofficial.botpy_message, "GroupMessage", _FakeGroupMessage)
    calls: list = []
    event = _send_event(calls)
    context = qqofficial.QQOfficialContext(
        platform_id="instance-1",
        group_openid="group-1",
        member_openid="user-a",
        display_name="小明",
        message_id="msg-1",
    )
    reply = Reply(
        text="公开信息",
        extra=[Reply(text="你的秘密按钮", buttons=[ButtonSpec("mod_2", "司机", "token")])],
    )

    ok = await qqofficial.send_reply(event, context, reply)

    assert ok is True
    assert [item["content"] for item in calls] == ["公开信息", "你的秘密按钮"]
    assert calls[1]["keyboard"]["content"]["rows"][0]["buttons"][0]["action"]["type"] == 2


@pytest.mark.asyncio
async def test_send_failure_does_not_leak_secret_buttons(monkeypatch) -> None:
    monkeypatch.setattr(qqofficial.botpy_message, "GroupMessage", _FakeGroupMessage)
    sent: list = []
    event = _send_event([], failure=RuntimeError("api down"), sent=sent)
    context = qqofficial.QQOfficialContext(
        platform_id="instance-1",
        group_openid="group-1",
        member_openid="user-a",
        display_name="小明",
        message_id="msg-1",
    )
    reply = Reply(
        text="请选择角色",
        buttons=[
            ButtonSpec("mod_role", "司机", "百万美金 操作 secret-token", only_for="user-a")
        ],
    )

    ok = await qqofficial.send_reply(event, context, reply)

    assert ok is False
    assert sent
    rendered = [result.message_str for result in sent]
    assert all("secret-token" not in text for text in rendered)
    assert any("按钮发送失败" in text for text in rendered)


@pytest.mark.asyncio
async def test_send_reveal_image_merges_roles_into_one_message(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(qqofficial, "_temp_image_path", lambda: tmp_path / "reveal.jpg")
    sent: list = []
    event = _send_event([], sent=sent)

    ok = await qqofficial.send_reveal_image(
        event, ["driver", "brute", "crook"]
    )

    assert ok is True
    # 多张角色卡必须合并成一条图片消息
    assert len(sent) == 1
    image_path = Path(sent[0].image_path)
    assert image_path.is_file()
    from PIL import Image

    merged = Image.open(image_path)
    from game import help as help_module

    single = Image.open(help_module.role_card_path("driver"))
    assert merged.height >= single.height
    assert merged.width >= single.width * 3


@pytest.mark.asyncio
async def test_send_reveal_image_shuffles_before_merging(monkeypatch, tmp_path) -> None:
    """合成前必须使用打乱后的顺序，而不是固定顺序。"""
    captured: dict = {}

    def fake_shuffled(paths, rng=None):
        return list(reversed(list(paths)))

    def fake_compose(paths, output, **kwargs):
        captured["order"] = [Path(item).name for item in paths]
        Path(output).write_bytes(b"fake")
        return Path(output)

    monkeypatch.setattr(qqofficial, "_temp_image_path", lambda: tmp_path / "reveal.jpg")
    monkeypatch.setattr(qqofficial.cards_module, "shuffled", fake_shuffled)
    monkeypatch.setattr(qqofficial.cards_module, "compose_grid", fake_compose)

    roles = ["driver", "brute", "crook"]
    sent: list = []
    ok = await qqofficial.send_reveal_image(_send_event([], sent=sent), roles)

    assert ok is True
    expected = [
        help_module.role_card_path(role).name for role in reversed(roles)
    ]
    assert captured["order"] == expected


@pytest.mark.asyncio
async def test_send_reveal_image_falls_back_when_no_card(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(qqofficial, "_temp_image_path", lambda: tmp_path / "x.jpg")
    sent: list = []
    event = _send_event([], sent=sent)

    ok = await qqofficial.send_reveal_image(event, ["unknown-role"])

    assert ok is False
    assert sent == []


@pytest.mark.asyncio
async def test_send_reply_sends_rule_card_then_text(monkeypatch) -> None:
    monkeypatch.setattr(qqofficial.botpy_message, "GroupMessage", _FakeGroupMessage)
    calls: list = []
    sent: list = []
    event = _send_event(calls, sent=sent)
    context = qqofficial.QQOfficialContext(
        platform_id="instance-1",
        group_openid="group-1",
        member_openid="user-a",
        display_name="小明",
        message_id="msg-1",
    )
    reply = Reply(
        text="规则速览",
        images=["docs/sources/rule-cards/rule-card.jpg"],
    )

    ok = await qqofficial.send_reply(event, context, reply)

    assert ok is True
    assert len(sent) == 1
    assert "rule-card.jpg" in sent[0].image_path
    assert len(calls) == 1
    assert calls[0]["content"] == "规则速览"


@pytest.mark.asyncio
async def test_send_reply_with_reveal_cards_sends_one_image_plus_text(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(qqofficial.botpy_message, "GroupMessage", _FakeGroupMessage)
    monkeypatch.setattr(qqofficial, "_temp_image_path", lambda: tmp_path / "reveal.jpg")
    calls: list = []
    sent: list = []
    event = _send_event(calls, sent=sent)
    context = qqofficial.QQOfficialContext(
        platform_id="instance-1",
        group_openid="group-1",
        member_openid="user-a",
        display_name="小明",
        message_id="msg-1",
    )
    reply = Reply(
        text="抢劫结算：司机×2 全部淘汰。",
        reveal_cards=["driver", "brute"],
    )

    ok = await qqofficial.send_reply(event, context, reply)

    assert ok is True
    assert len(sent) == 1  # 两张角色卡合并成一条图片消息
    assert len(calls) == 1
    assert "司机×2" in calls[0]["content"]


def test_keyboard_uses_explicit_rows() -> None:
    buttons = [
        ButtonSpec("a", "创建", "百万美金 创建", row=0),
        ButtonSpec("b", "加入", "百万美金 加入", row=0),
        ButtonSpec("c", "开始", "百万美金 开始", row=1),
    ]

    keyboard = qqofficial.build_keyboard(buttons)

    rows = keyboard["content"]["rows"]
    assert [len(row["buttons"]) for row in rows] == [2, 1]
    assert [row["buttons"][0]["id"] for row in rows] == ["a", "c"]


def test_keyboard_splits_oversized_row_and_caps_rows() -> None:
    buttons = [
        ButtonSpec(f"b{index}", f"按钮{index}", "百万美金 状态", row=index // 7)
        for index in range(20)
    ]

    keyboard = qqofficial.build_keyboard(buttons)

    rows = keyboard["content"]["rows"]
    assert len(rows) <= qqofficial.MAX_ROWS
    assert all(len(row["buttons"]) <= qqofficial.BUTTONS_PER_ROW for row in rows)
