# Bot menu 验证记录（开发分支，非飞书租户端到端验收）

## 当前结果

- 父会话独立复跑全套：`514 passed, 6 warnings in 157.52s`。
- 固定菜单仅 `model` / `session`；会话列表选中目标后先查看该会话消息，再确认切换，不提供独立「当前会话」菜单。
- Ruff：`All checks passed!`。
- Mypy：`Success: no issues found in 27 source files`。
- 插件 verify：`native platform interfaces available; no old injections found`，不发送飞书消息。
- Hermes 核心源码工作区保持干净；没有修改已安装插件、生产配置或运行依赖，没有重启线上 Gateway。
- **已验证真实 Hermes 0.23.2 Gateway + SessionAuthority 原生命令行为**，不是仅 mock 切换函数。
- **尚未连接真实飞书租户进行菜单发布、桌面与 iPhone 点击验收。**
- 多 Profile multiplex 路由暂不支持，明确拒绝菜单读取/切换。

## 可复现命令

工作目录：`/home/pi5/src/hermes-lark-menu`，分支 `feat/bot-session-menu`。

```bash
PY=/home/pi5/.hermes/cache/scratch/lark-menu-test-venv/bin/python
export HERMES_DISABLE_LAZY_INSTALLS=1
$PY -m pytest tests -q
$PY -m ruff check hermes_lark_streaming tests scripts __init__.py
$PY -m mypy hermes_lark_streaming scripts
$PY -m hermes_lark_streaming verify
$PY -m build --no-isolation --outdir /home/pi5/.hermes/cache/scratch/lark-menu-dist
```

独立环境使用 Python 3.14.9，通过只读 `.pth` 复用已存在的 Hermes 运行依赖。
开发工具只安装到新建的 scratch venv，没有修改生产依赖。6 条 warning 为依赖弃用提醒。

## 原生集成测试

`tests/test_menu_gateway_integration.py` 为每个场景先建立隔离进程和 HERMES_HOME，再导入 Gateway，
避免 `gateway.run._hermes_home` 在导入时绑定到用户的真实配置。每个子进程初始化真实
GatewayRunner、SessionAuthority、SessionDB；飞书使用测试传输，模型请求走本地 HTTP 测试服务，
不使用用户提供商密钥或实际模型服务。

覆盖以下场景：

- `/model --session` 持久化后，用新只读 SessionDB 读回，并确认下一轮真实 Agent 请求携带所选模型。
- `/resume` 路由持久化读回；下一条真实消息与回复写入恢复后的目标会话。
- 当前会话忙碌、过时卡片、外人会话和已被并发执行占用的目标均拒绝。
- 共享运行时新输入不能越过原生模型提交的临界区。
- 原生 `/new` 不因锁重入而死锁。
- 保留原生模型费用及破坏性确认；延迟确认重新检查原会话和忙碌状态。
- 取消不执行切换；任务取消时等原生 worker 真正提交/发布结束才释放执行保护。

`menu_host.py` 将必要的版本敏感接口隔离在窄兼容层：原生 owner 验证、准确的历史读取集合、
共享运行时 claim gate / mutation lock；不替换宿主函数，不写核心源码，不直接写数据库。
版本升级后须重跑这些测试，不把测试通过等同于真实飞书客户端验收。

## 菜单与隐私覆盖

- 真实 `lark_oapi.EventDispatcherHandler` 解析官方 `application.bot.menu_v6` 形状和卡片回传。
- 组合官方 SDK dispatcher，不修改原生注册表；原有消息、审批、澄清、choice picker 回调保留。
- 模型目录来自 Hermes 原生接口，展示 provider slug 和 model 原始 ID。
- 私聊可信入站映射，open_id/user_id/union_id 不混用；权限撤销与跨用户访问拒绝。
- 随机短期 token 绑定 actor/chat/message，拒绝旧页、过期、重放、并发重放及跨卡片点击。
- 历史翻页不切换；预览页可确认切换；新建需确认；模型确认提示上下文可能交给所选提供商。
- 实际读取 `_resume_lineage_ids` 的已验证集合，显式读取已校验分段，覆盖第二个压缩子分支、
  普通分支边界、异主祖先和多模态字段，不验证一条链却读另一条。
- 原生 user-originated 投影处理元数据丢失的压缩摘要和复合 carrier：不泄露摘要，不丢真实用户问题。
- 历史只渲染普通 user/assistant 纯文本；不显示系统提示、工具、推理与内部消息。
- 卡片多客户端同步、禁转发；预览仍留在聊天记录中，不防截图。

## TDD 与基线

实现前先观察缺失菜单/兼容模块导致测试失败，再逐项实现；后续隐私和交互问题同样先写失败回归。
复合压缩消息隐私回归首次为 `10 failed, 3 passed`，修复后 13 项全通过。

改动前基线为 `1 failed, 456 passed`：原有 clarify 测试假定 Hermes 0.22 的 text-only 行为，
但 0.23 原生按钮卡需要 connected SDK client。仅将该测试的网络交付 helper 改为 mock，
保留真实 clarify 注册、awaiting_text 和 resolve 检查，没有修改生产澄清逻辑。

## 代码入口

- `hermes_lark_streaming/adapter.py`：事件/回调扩展，原生命令同步交付，延迟原生确认保护。
- `hermes_lark_streaming/bot_menu.py`：菜单与分页卡片、私聊映射、短期 token、SDK dispatcher 组合。
- `hermes_lark_streaming/menu_host.py`：原生命令保护、只读会话历史、owner 与版本兼容边界。
- `README.md`：安装边界、飞书后台固定 key、使用与限制。

当前仍为待提交开发分支，插件版本号保持 0.14.0；不是上游正式新版本。
