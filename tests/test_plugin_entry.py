"""插件入口与指令契约测试（设计文档第 9 节）。"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("astrbot", reason="需要在 AstrBot 的 uv 环境中运行")

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def load_main():
    spec = importlib.util.spec_from_file_location(
        "millionofdollars_main",
        PLUGIN_ROOT / "main.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def main_module():
    return load_main()


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("百万美金", ("", "")),
        ("/百万美金", ("", "")),
        ("百万美金 创建", ("创建", "")),
        ("/百万美金 创建", ("创建", "")),
        ("百万美金创建", ("创建", "")),
        ("百万美金 开始", ("开始", "")),
        ("百万美金 状态", ("状态", "")),
        ("百万美金 帮助", ("帮助", "")),
        ("百万美金 退出房间", ("退出房间", "")),
        ("百万美金退出房间", ("退出房间", "")),
        ("百万美金 退出", ("退出", "")),
        ("百万美金 关闭房间", ("关闭房间", "")),
        ("百万美金 关闭", ("关闭", "")),
        ("百万美金 规则", ("规则", "")),
        ("百万美金规则卡", ("规则卡", "")),
        ("百万美金 菜单", ("菜单", "")),
        ("百万美金 加入", ("加入", "")),
        ("百万美金 准备", ("准备", "")),
        ("百万美金 取消准备", ("取消准备", "")),
        ("百万美金 退出", ("退出", "")),
        ("百万美金 强制抢劫", ("强制抢劫", "")),
        ("百万美金 使用威胁牌", ("使用威胁牌", "")),
        ("百万美金 转账", ("转账", "")),
        ("百万美金 转账 user-b", ("转账", "user-b")),
        ("百万美金 操作 abcdef", ("操作", "abcdef")),
    ],
)
def test_parse_command_contract(main_module, message, expected) -> None:
    assert main_module._parse_command(message) == expected


def test_plugin_metadata_and_registration(main_module) -> None:
    from astrbot.api.star import Star
    from astrbot.core.star.star import star_map

    assert main_module.PLUGIN_NAME == "astrbot_plugin_millionofdollars"
    plugin_cls = main_module.MillionsOfDollarsPlugin
    assert issubclass(plugin_cls, Star)
    metadata = star_map.get(plugin_cls.__module__)
    assert metadata is not None
    assert metadata.name == main_module.PLUGIN_NAME
    # 实例化必须可用于 LogManager，且不依赖已初始化的 StarTools
    plugin = plugin_cls(SimpleNamespace())
    assert plugin._service is None


def test_plugin_initialize_creates_data_dir_and_secret(
    main_module, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        main_module.StarTools,
        "get_data_dir",
        classmethod(lambda cls, name=None: tmp_path / (name or "plugin")),
    )
    plugin = main_module.MillionsOfDollarsPlugin(SimpleNamespace())

    import asyncio

    asyncio.run(plugin.initialize())

    data_dir = tmp_path / main_module.PLUGIN_NAME
    assert (data_dir / main_module.DB_FILENAME).exists()
    secret = data_dir / main_module.SECRET_FILENAME
    assert secret.exists()
    assert len(secret.read_bytes()) == 32


def test_help_command_returns_the_rules_card_image(main_module, monkeypatch, tmp_path) -> None:
    """走 main.py 真实路由：帮助指令必须返回规则卡图片。"""
    import asyncio

    monkeypatch.setattr(
        main_module.StarTools,
        "get_data_dir",
        classmethod(lambda cls, name=None: tmp_path / (name or "plugin")),
    )
    plugin = main_module.MillionsOfDollarsPlugin(SimpleNamespace(get_config=lambda: None))
    asyncio.run(plugin.initialize())
    request = main_module.RequestContext(
        platform_id="qq_official_instance",
        group_openid="group-1",
        member_openid="user-a",
        display_name="小明",
        message_id="m-help",
    )

    reply = asyncio.run(plugin._dispatch("百万美金 帮助", request))

    assert "规则速览" in reply.text
    assert reply.images == ["docs/sources/rule-cards/rule-card.jpg"]


def test_plugin_can_start_a_game_with_the_verified_deck(
    main_module, monkeypatch, tmp_path
) -> None:
    """走 main.py 的真实路由：创建 → 加入 → 开始，验证已核验牌组可以开局。"""
    import asyncio

    monkeypatch.setattr(
        main_module.StarTools,
        "get_data_dir",
        classmethod(lambda cls, name=None: tmp_path / (name or "plugin")),
    )
    plugin = main_module.MillionsOfDollarsPlugin(SimpleNamespace(get_config=lambda: None))
    asyncio.run(plugin.initialize())

    def request(member: str, message_id: str) -> object:
        return main_module.RequestContext(
            platform_id="qq_official_instance",
            group_openid="group-1",
            member_openid=member,
            display_name=member,
            message_id=message_id,
        )

    async def play() -> tuple:
        created = await plugin._dispatch("百万美金 创建", request("a", "m1"))
        for index, member in enumerate(["b", "c", "d"], start=1):
            await plugin._dispatch("百万美金 加入", request(member, f"m-join-{index}"))
        started = await plugin._dispatch("百万美金 开始", request("a", "m-start"))
        status = await plugin._dispatch("百万美金 状态", request("a", "m-status"))
        return created, started, status

    created, started, status = asyncio.run(play())

    assert "已创建房间" in created.text
    assert "游戏开始" in started.text
    assert len(started.extra) == 4  # 每位玩家一条秘密选角消息
    assert "阶段：role_selection" in status.text
    assert re.search(r"赃物：(8|9|10|12) 百万美元", status.text)


def test_command_aliases_cover_no_space_writes(main_module) -> None:
    """无空格写法也要注册成别名，否则会漏进默认 LLM 链路。"""
    for name in ("创建", "加入", "退出房间", "关闭", "状态", "菜单", "帮助", "操作"):
        assert f"百万美金{name}" in main_module.COMMAND_ALIASES
    assert main_module.COMMAND_NAME not in main_module.COMMAND_ALIASES


def _make_qqofficial_event(
    main_module,
    monkeypatch,
    *,
    message: str,
    role: str = "member",
    message_id: str = "msg-1",
    posted: list,
    sent: list,
    flags: dict,
):
    """构造一个能跑通 main.py 处理链路的 QQOfficial 假事件。"""
    from types import SimpleNamespace

    qqofficial = main_module.qqofficial
    if "millionofdollars_fake" not in str(qqofficial.botpy_message.GroupMessage):
        class _FakeGroupMessage:
            pass

        monkeypatch.setattr(qqofficial.botpy_message, "GroupMessage", _FakeGroupMessage)

    async def fake_post(**kwargs):
        posted.append(kwargs)
        return {"id": "1"}

    async def fake_send(chain):
        sent.append(chain)

    raw = qqofficial.botpy_message.GroupMessage()
    raw.id, raw.msg_seq, raw.group_openid = message_id, 1, "group-1"
    raw.author = SimpleNamespace(member_openid="user-a", username="小明")
    event_type = type(
        "QQOfficialMessageEvent",
        (),
        {
            "__module__": (
                "astrbot.core.platform.sources.qqofficial.qqofficial_message_event"
            ),
            "message_obj": SimpleNamespace(raw_message=raw, message_id=message_id),
            "role": role,
            "bot": SimpleNamespace(api=SimpleNamespace(post_group_message=fake_post)),
            "get_message_str": lambda self: message,
            "get_group_id": lambda self: "group-1",
            "get_sender_id": lambda self: "user-a",
            "get_sender_name": lambda self: "小明",
            "get_platform_id": lambda self: "instance-1",
            "plain_result": lambda self, text: SimpleNamespace(message_str=text),
            "image_result": lambda self, path: SimpleNamespace(
                chain=[SimpleNamespace(file=path)], message_str=""
            ),
            "send": lambda self, chain: fake_send(chain),
            "stop_event": lambda self: flags.__setitem__("stopped", True),
            "should_call_llm": lambda self, value: flags.__setitem__(
                "call_llm", value
            ),
        },
    )
    return event_type()


def _run_handler(main_module, plugin, event) -> None:
    import asyncio

    async def run() -> None:
        async for _ in plugin.million_dollars(event):
            pass

    asyncio.run(run())


def test_handler_stops_event_so_text_commands_never_reach_llm(
    main_module, monkeypatch, tmp_path
) -> None:
    """文本指令走 post_group_message 直发，必须 stop_event，否则会触发默认 LLM。"""
    monkeypatch.setattr(
        main_module.StarTools,
        "get_data_dir",
        classmethod(lambda cls, name=None: tmp_path / (name or "plugin")),
    )
    plugin = main_module.MillionsOfDollarsPlugin(SimpleNamespace(get_config=lambda: None))
    import asyncio

    asyncio.run(plugin.initialize())

    posted: list = []
    sent: list = []
    flags: dict = {"stopped": False, "call_llm": None}
    event = _make_qqofficial_event(
        main_module,
        monkeypatch,
        message="百万美金 创建",
        posted=posted,
        sent=sent,
        flags=flags,
    )

    _run_handler(main_module, plugin, event)

    assert posted and "已创建房间" in posted[0]["content"]
    assert flags["stopped"] is True
    assert flags["call_llm"] is True


def test_handler_handles_no_space_alias(
    main_module, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        main_module.StarTools,
        "get_data_dir",
        classmethod(lambda cls, name=None: tmp_path / (name or "plugin")),
    )
    plugin = main_module.MillionsOfDollarsPlugin(SimpleNamespace(get_config=lambda: None))
    import asyncio

    asyncio.run(plugin.initialize())

    posted: list = []
    flags: dict = {"stopped": False, "call_llm": None}
    event = _make_qqofficial_event(
        main_module,
        monkeypatch,
        message="百万美金创建",
        posted=posted,
        sent=[],
        flags=flags,
    )

    _run_handler(main_module, plugin, event)

    assert posted and "已创建房间" in posted[0]["content"]
    assert flags["stopped"] is True


def test_handler_admin_can_close_room(
    main_module, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        main_module.StarTools,
        "get_data_dir",
        classmethod(lambda cls, name=None: tmp_path / (name or "plugin")),
    )
    plugin = main_module.MillionsOfDollarsPlugin(SimpleNamespace(get_config=lambda: None))
    import asyncio

    asyncio.run(plugin.initialize())

    posted: list = []
    flags: dict = {"stopped": False, "call_llm": None}
    create_event = _make_qqofficial_event(
        main_module,
        monkeypatch,
        message="百万美金 创建",
        message_id="msg-create",
        posted=posted,
        sent=[],
        flags=flags,
    )
    _run_handler(main_module, plugin, create_event)

    # 换一个管理员身份发送「关闭」
    close_flags: dict = {"stopped": False, "call_llm": None}
    close_event = _make_qqofficial_event(
        main_module,
        monkeypatch,
        message="百万美金 关闭",
        role="admin",
        message_id="msg-close",
        posted=posted,
        sent=[],
        flags=close_flags,
    )
    _run_handler(main_module, plugin, close_event)

    assert "房间已关闭" in posted[-1]["content"]
    assert close_flags["stopped"] is True
