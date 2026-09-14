# astrbot_plugin_millionofdollars

《百万美金》（Millions of Dollars）桌游的 AstrBot 插件，规则基线为 2016 年初版。

当前版本为 QQ 官方机器人群聊提供了完整的可玩流程：创建房间、加入、选角、谈判、
独立转账、独立退出、准备、角色结算、分赃与胜利判定。

## 快速开始

在 QQ 群里发送：

```text
@机器人 百万美金 菜单
```

菜单按钮等价于一条文本指令，点击后仍需**发送**才会生效：

| 行 | 按钮 |
| --- | --- |
| 1 | 创建房间 / 加入 / 退出房间 / 关闭房间 |
| 2 | 开始游戏 / 查看状态 |
| 3 | 转账 / 退出本轮 |
| 4 | 准备 / 取消准备 |
| 5 | 使用威胁牌 / 帮助（规则卡） |

低频操作（强制抢劫等）直接发送文本指令即可，详见下表。

## 玩法指令

所有按钮都等价于一条文本指令；秘密动作（选角、转账金额、退出确认、告密人指定）
只提供 `百万美金 操作 <HMAC令牌>` 按钮，没有明文回退。

| 指令 | 说明 |
| --- | --- |
| `百万美金 创建` | 在当前群创建房间，发送者成为首领 |
| `百万美金 加入` | 加入房间，仅大厅阶段可用，最多 8 人 |
| `百万美金 退出房间` | 大厅阶段退出房间；最后一人退出时房间自动关闭，首领退出时首领转交 |
| `百万美金 关闭房间` | 关闭当前房间（`关闭`/`结束`/`解散` 同义），仅首领或 AstrBot 管理员可用 |
| `百万美金 开始` | 首领开局，人数需 3～8 人 |
| `百万美金 状态` | 查看人数、阶段、回合、赃物牌、公开角色计数与各玩家现金 |
| `百万美金 菜单` | 显示公开菜单按钮 |
| `百万美金 转账` | 先选收款人，再选金额；转账不会导致退出 |
| `百万美金 退出` | 退出指定人物槽位并收回保证金；3 人局需要选择槽位 |
| `百万美金 准备` / `百万美金 取消准备` | 有活动槽位的玩家全部准备后立即结算 |
| `百万美金 强制抢劫` | 谈判开始 60 秒后仅首领可用，立即进入结算 |
| `百万美金 使用威胁牌` | 需要持有威胁牌；点击后身份只出现在输入框，请勿发送 |
| `百万美金 帮助` | 输出规则卡图片与规则速览（`规则`、`规则卡` 同义） |
| `百万美金 操作 <令牌>` | 秘密动作按钮自动生成的指令 |

支持 `/百万美金 创建` 与 `百万美金创建` 两种写法。所有指令都会显式终止事件，不会继续触发 AstrBot 的默认 LLM 回复。

### 卡图

- `百万美金 帮助` 会发送**规则卡**图片（`docs/sources/rule-cards/rule-card.jpg`）；
- 每轮**身份揭露**（抢劫结算）时，会把本轮**中心牌堆**合并成一张图片发送：
  每个提交过角色的槽位一张，其中被随机隐藏的那张显示**卡背**；
  卡片顺序每次随机打乱，避免顺序暗示玩家与角色的对应关系；
  超过 5 张时自动换行，仍然是同一张图片。
- 谈判期退出的玩家，其角色牌仍匿名留在中央牌堆里（牌堆本身不标注归属），
  因此也会出现在图中。

## 当前可玩状态

游戏逻辑与赃物牌组均已完成，可以直接开局。10 张赃物牌的牌面取自实物牌截图并
逐张核验，核验方法与结果见[赃物牌组核验记录](./docs/loot-deck-verification.md)。

| 编号 | 名称 | 赃款 | 保证金 | 奖励角色 |
| --- | --- | --- | --- | --- |
| loot-01 | 拉斯维加斯赌场 | 10 | 2 | 暴徒 |
| loot-02 | 皇家赌场 | 10 | 2 | 告密者 |
| loot-03 | 国家银行 | 9 | 1 | 恶棍 |
| loot-04 | 州际银行 | 9 | 1 | 暴徒 |
| loot-05 | 中央银行 | 9 | 1 | 司机 |
| loot-06 | 城市银行 | 8 | 1 | 告密者 |
| loot-07 | 县级银行 | 8 | 1 | 恶棍 |
| loot-08 | 农村信用社 | 8 | 1 | 司机 |
| loot-09 | 诺克斯堡金库 | 12 | 2 | 无 |
| loot-10 | 第一银行 | 8 | 1 | 暴徒 |

每局从这 10 张中无放回抽取 8 张，逐轮翻开。

## 规则文档

- [完整中文规则](./docs/million-of-dollars-rules-zh.md)
- [资料来源与版本说明](./docs/README.md)
- [QQ 官方 Bot 开发说明](./docs/qqofficial-bot-development.md)
- [QQ 官方 Bot 游戏开发实现设计](./docs/game-implementation-design.md)
- [赃物牌组核验记录](./docs/loot-deck-verification.md)
- [2016 初版英文规则书 PDF](./docs/sources/millions-of-dollars-2016-rulebook-en.pdf)
- [2024 二版英文规则书 PDF](./docs/sources/millions-of-dollars-2024-rulebook-en.pdf)

2016 初版与 2024 二版的轮数、胜利金额和角色体系均不同，插件开发不得混用两个版本的规则。

## 代码结构

```text
astrbot_plugin_millionofdollars/
├── main.py                 # 插件注册与命令路由
├── game/
│   ├── models.py           # 枚举、快照与严格序列化
│   ├── loot.py             # 赃物牌组常量与校验
│   ├── rules.py            # 纯规则函数
│   ├── tokens.py           # HMAC 动作令牌
│   ├── repository.py       # SQLite 仓储、事务与幂等
│   ├── service.py          # 状态机、房间锁与用例编排
│   └── qqofficial.py       # 事件字段、keyboard 与发送适配
└── tests/
    ├── test_rules.py
    ├── test_tokens.py
    ├── test_repository.py
    ├── test_service.py
    ├── test_qqofficial.py
    ├── test_loot_deck.py
    ├── test_cards.py
    └── test_plugin_entry.py
```

`game/rules.py` 不导入 AstrBot、botpy、sqlite3 或网络组件；QQ 适配层不直接修改游戏快照。

## 开发

测试需要在 AstrBot 的 uv 环境中运行：

```bash
python -m pytest tests/
```

如果 AstrBot 是源码目录而非已安装包，需要把仓库根目录加入 `PYTHONPATH`：

```bash
PYTHONPATH=/path/to/AstrBot python -m pytest tests/
```

打包本地安装包：

```bash
python scripts/package_plugin.py
```

运行数据（SQLite 数据库与 HMAC 密钥）写入 AstrBot 的 `data/plugin_data/astrbot_plugin_millionofdollars/`，
不进入源码目录，也不进入安装包。

## 相关链接

- [AstrBot 项目](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
