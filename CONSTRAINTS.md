# 开发约束

1. 开发新功能必须添加或更新对应测试。
2. 测试使用 pytest 开发。
3. 测试必须在 `%ASTRBOT_ROOT%` 的 uv 环境中运行。
4. 每次更新必须同步更新 `CHANGELOG.md`。
5. 每次修改必须更新 `metadata.yaml` 的版本号，并更新 `README.md` 的对应部分。
6. `README.md` 和 `metadata.yaml` 中用于展示给用户的内容必须保持中文。
7. 每次更改后必须运行 `python scripts/package_plugin.py`，生成对应版本的 AstrBot 本地安装 zip 压缩包，方便本地安装验证。
8. 所有代码修改必须仅限于当前插件目录，不修改 AstrBot 源码。
9. 每个版本更新要进行对应的git提交
