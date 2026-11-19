# Hermes Lark Streaming

[![Python](https://img.shields.io/badge/python-%E2%89%A53.11-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.en.md)

![Hermes Lark Streaming](assets/cover.jpg)

Hermes 飞书 / Lark CardKit 2.0 流式卡片插件。0.14.0 起通过 Hermes 原生平台接口接入，在 Gateway 进程内运行。

- 保留内置飞书的连接、用户权限、配对、群策略、附件、话题和审批
- AI 回复持续更新到卡片，保留思考、工具状态、图片解析和长内容拆卡
- 在线与独立 Cron、后台任务结果使用卡片投递
- 卡片失败时交回内置回复链路
- 不修改 Hermes 源码，不使用 AST 注入、monkey-patch 或 sidecar

## 要求

- Hermes **>= 0.22.0**，已配置内置飞书渠道
- Python >= 3.11；Hermes 的运行环境由 Hermes 自己管理
- `lark-oapi >= 1.6.4`、`PyYAML >= 6.0`
- 飞书应用具备消息发送/回复、CardKit 读写和所用媒体权限

旧注入版用户必须先阅读 [升级说明](MIGRATION.md)，用旧版卸载注入后再安装新版。

## 安装

```bash
hermes plugins install https://github.com/Cheerwhy/hermes-lark-streaming
hermes plugins enable hermes-lark-streaming
hermes gateway restart
```

本地开发可将仓库符号链接到 `~/.hermes/plugins/hermes-lark-streaming`，再执行上述启用和重启命令。修改源码后重启 Gateway 生效。

插件使用正式平台注册接口替换 `feishu` 的展示适配器。保留现有 `platforms.feishu`、凭据、会话和 Cron 目标，不另建渠道。

## 机器人自定义菜单（开发分支）

菜单与现有流式卡片共用内置飞书连接，不另开长连接，也不修改 Hermes 核心。
**菜单功能以 Hermes 0.23.2 接口测试；原有流式功能的最低要求仍为 0.22.0。**

已在隔离环境的 **Hermes 0.23.2 Gateway + SessionAuthority** 上验证模型切换持久化、
下一轮请求使用所选模型，以及恢复会话后写入正确历史；未连接真实飞书租户验收。
安装本开发分支，不要使用上方上游主分支安装命令期待获得尚未合并的菜单功能。
安装来源须指向包含本功能的 Git 仓库及精确提交，使用
`hermes plugins install <Git仓库URL> --ref <40位提交SHA> --yes-deps --enable`，无需修改核心源码。
已有同名插件时先备份旧目录；`--force` 仅适用于同一来源重新安装，跨 fork 更换来源须按
Hermes 的来源保护提示先移除旧插件再安装，不能绕过来源校验。本地开发也可用上文的目录插件方式。

### 飞书开放平台设置

1. 在应用的「机器人 → 机器人菜单」中新增两个「发送事件」菜单项。显示名称可以修改，
   **事件 Key 必须分别为下面的固定值**，不要填显示名称或斜杠命令：

   | 显示名称（示例） | 事件 Key | 功能 |
   | --- | --- | --- |
   | 切换模型 | `model` | 当前会话模型、分页选择、刷新与确认 |
   | 切换会话 | `session` | 选中会话 → 查看该会话消息并翻页 → 确认切换；支持新建会话 |

   不设置「当前会话」菜单；如果按此前说明配置过 `current`，请移除此菜单项。

2. 在「事件与回调」添加订阅 **机器人自定义菜单事件 `application.bot.menu_v6`**；
   保留现有消息接收事件和卡片回传交互 **`card.action.trigger`**，不要替换已有订阅。
   沿用已有长连接模式，或原有 Webhook 地址、Verification Token 与 Encrypt Key。
3. 发布应用版本并更新可用范围，使菜单对目标用户生效。确认已具备原有的消息发送权限。
4. 用户须先给机器人发送一条普通**私聊消息**并通过原有 Hermes 权限/配对，再点菜单。
   菜单事件没有可信 `chat_id`，插件只从已核验私聊入站保存 open_id → DM 的映射，
   **不把 open_id、user_id、union_id 互相代用**；群聊或未建立映射的点击不会枚举任何会话。

### 使用与安全边界

- 模型列表读取 Hermes 原生 `list_session_model_picker_providers` 和
  `get_session_model_settings`，使用配置中的 provider slug 与原始 model ID，无内置硬编码目录。
  每次打开/翻页/刷新重新读取宿主目录。刷新遵循宿主非阻塞缓存策略，后台更新可能需要再次点击，
  不为获取目录而探测自定义服务或改写用户配置。
- 选择后必须点「确认切换」。模型走 **`/model --session --provider <slug> <raw-model-id>`**；
  会话走 **`/resume <id>`**，新建走 **`/new`**。经原生 synthetic-event/guarded ingress 进入 Gateway，
  不直接调用 runner 的模型/切换处理函数、不写数据库或全局默认模型。
  Hermes 的命令权限、路由策略、模型校验及额外费用确认仍然有效；提交命令不等于切换成功，
  **以 Hermes 随后发回的原生命令结果为准**。
- 每页最多 8 个模型/会话或 8 段普通历史。历史只显示 user/assistant 的纯文本；
  不显示系统提示、工具、tool_calls、reasoning 字段、压缩摘要或内部 display_kind 消息。
  原生只读 `SessionDB` API 在查询前按当前 session_key/platform 限定范围，且每个结果与
  每个压缩祖先均再次经 Hermes 原生 owner 验证。长消息分段翻页，不截断尾部。
  会话列表只提供「查看消息」入口，先展示选中会话的消息，不是固定显示当前会话。
  预览不调用 `/resume`，不会改变当前会话；在消息页选择「切换到此会话」再确认。
  使用宿主的复合压缩消息投影，仅保留其中真实用户问题；准确验证实际读取的祖先集合，
  不验证一条压缩链却读取另一条。预览卡片禁转发，但仍是留在聊天中的消息，不防截图。
- 按钮只有随机 opaque token，实际操作/目标保存在内存中；有效期 **5 分钟**，
  绑定 open_id/DM/卡片 message_id。新卡片页使旧页按钮失效；重复、过期、跨用户、
  跨聊天或跨卡片点击均拒绝。确认会再次检查忙碌状态和当前 session ID；忙碌时不排队切换。
- DM 映射最多保留 24 小时、1024 个用户，按钮最多 2048 个；Gateway 重启后均失效，
  请重新发送私聊并打开菜单。旧/无归属元数据的历史默认拒绝读取。

### 已知兼容性限制（不要绕过）

- Hermes 尚无公开的 adapter 级会话列表/历史/owner API。`menu_host.py` 将必要的
  `_session_key_for_source`、`_resolve_profile_home_for_source`、`_resume_target_allowed`
  兼容调用隔离起来；只通过显式 `adapter.gateway_runner` 取宿主，不从 bound handler
  猜 runner。接口缺失即关闭相应菜单，不回退到全库列表。
- **多 Profile multiplex 路由目前拒绝菜单读取/切换**。不要误把启动 Profile 的数据库用于其他用户。
- 菜单命令复用原生每聊天锁，并通过 `menu_host.py` 的窄兼容层持有共享 authority 的
  claim gate / mutation lock，直到原生命令提交与结果发布结束；检查源与目标的待处理任务，
  再核对 expected-session。`/new` 按原生锁顺序单独处理，避免重入死锁。
  延迟的原生费用/破坏性确认也重新检查权限、会话与忙碌状态；取消不触发切换。
  这些兼容接口不是 Hermes 稳定公共 API，升级宿主后须重跑原生集成测试，接口缺失时拒绝操作。
- 带空白或以 `-` 开头的 provider/model 原始 ID 不自动修复、不拼接成歧义命令；
  请使用宿主命令确认其支持方式。原有消息、多模态、审批、澄清、choice picker、Cron 通知逻辑不变。
- 自动化测试使用真实 SDK dispatcher、Hermes Gateway/SessionAuthority/SessionDB/owner verifier，
  每个原生切换场景在独立进程与临时配置运行，模型提供商为本地 HTTP 测试服务，
  飞书网络发送为模拟传输；未连接真实飞书租户进行菜单发布/端到端验收。

## 配置

编辑当前 Hermes Profile 的 `config.yaml`（默认 `~/.hermes/config.yaml`），合并以下配置，保留已有的飞书连接和权限设置。

### 最小配置

```yaml
streaming:
  enabled: true

platforms:
  feishu:
    enabled: true
    extra:
      card:
        enabled: true

display:
  platforms:
    feishu:
      tool_progress: 'off'
      show_tool_use: true
      show_reasoning: false
```

`streaming.enabled` 是 Hermes 的流式总开关，默认 `false`，必须开启；同时不要将 `streaming.transport` 设为 `off`，或将 `display.platforms.feishu.streaming` 设为 `false`。

### 卡片参数

以下键均位于 `platforms.feishu.extra.card`，未设置时采用表中默认值。外观选项用于流式卡片及其完成态；独立 Cron、后台结果等静态卡片使用简洁固定样式。

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `enabled` | `true` | 启用原生流式卡片；`false` 仅关闭流式接管，停用整个插件见下文 |
| `width_mode` | `default` | 卡片宽度：`default`、`compact`、`fill`；无效值回退为 `default` |
| `panel_expanded` | `false` | 完成后保持思考、工具面板展开；`false` 表示完成后折叠 |
| `header.enabled` | `false` | 显示顶部状态标题 |
| `body.text_size` | `normal_v2` | 正文 Markdown 字号，使用 CardKit 支持的字号标记 |
| `footer.enabled` | `true` | 显示完成态页脚 |
| `footer.text_size` | `notation` | 页脚字号，使用 CardKit 支持的字号标记 |
| `footer.fields` | `[[status, elapsed, context, model]]` | 页脚字段及排列，详见下节 |
| `footer.show_label` | `false` | 为耗时和上下文增加文字标签 |

例如调整卡片外观，在已有 `card` 下合并：

```yaml
width_mode: fill
panel_expanded: true
header:
  enabled: true
body:
  text_size: normal_v2
```

### 页脚布局

`footer.fields` 每个内层数组对应一行，行内用 `·` 分隔。支持以下五个字段；没有数据的字段自动省略。

| 字段 | 内容 |
| --- | --- |
| `status` | 完成、出错或已停止 |
| `elapsed` | 卡片耗时 |
| `context` | 最近一次 API 请求的完整输入量 / 宿主上下文上限及占比，包含缓存输入 |
| `tokens` | 本回合 API 用量累计，`↑` 非缓存输入、`↓` 输出；宿主仅提供完整输入时以完整输入计 |
| `model` | 模型名称 |

例如在 `platforms.feishu.extra.card` 下设置两行页脚：

```yaml
footer:
  enabled: true
  text_size: notation
  show_label: true
  fields:
    - [status, elapsed]
    - [tokens, model]
```

一维数组如 `[status, elapsed]` 表示单行；空数组恢复默认布局，隐藏页脚请用 `footer.enabled: false`。`context` 缺少完整输入量或窗口上限时省略；默认布局为单行 `[status, elapsed, context, model]`，`tokens` 可按需加入。

### 思考与工具面板

| 配置路径 | 默认值 | 作用 |
| --- | --- | --- |
| `display.platforms.feishu.show_tool_use` | `true` | 显示卡片工具面板：名称、简短参数、状态和耗时；不展示完整成功结果 |
| `display.platforms.feishu.show_reasoning` | `false` | 显示模型提供的思考内容，还需开启下方事件开关 |
| `plugins.stream_reasoning_deltas` | `false` | 允许 Hermes 向插件提供流式思考事件 |
| `display.platforms.feishu.tool_progress` | Hermes 决定 | 建议设为 `'off'`，避免宿主工具进度文本与卡片工具面板重复 |

`show_tool_use`、`show_reasoning` 的读取顺序为飞书专属设置 → `display` 下的同名全局设置 → 表中默认值。例如展示思考：

```yaml
plugins:
  stream_reasoning_deltas: true
display:
  platforms:
    feishu:
      show_reasoning: true
```

展示开关不限制工具权限，也不会让模型产生原本未提供的思考内容。

### 生效与渠道设置

修改卡片参数或宿主流式设置后执行 `hermes gateway restart`。`show_tool_use`、`show_reasoning` 会在运行中重新读取，作用于后续渲染，不主动刷新已结束的历史卡片。

凭据与渠道设置继续使用 Hermes 内置飞书配置：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_DOMAIN`，以及 `platforms.feishu.extra` 下的连接、群策略等选项，不放入 `card`。插件不新增渠道权限配置。

旧版卡片参数从 `streaming.*` 移至 `platforms.feishu.extra.card`，全局 `streaming.enabled` 保留原位；插件不会自动迁移旧配置，详见 [升级说明](MIGRATION.md)。

命令审批后的答案遵循 Hermes 0.22.0 的协议，一次性发送终态卡；澄清后可恢复流式。澄清选项沿用内置文本回复流程，审批按钮沿用内置卡片回调。

## 卡片展示

回答持续更新到卡片，支持思考和工具面板，长内容自动拆卡。

![流式卡片](assets/streaming.jpg)

使用 `/stop` 停止生成：

![停止生成后的卡片](assets/abort.jpg)

新消息打断当前回复：

![消息打断后的卡片](assets/interrupt.jpg)

以上为既有渲染效果截图，实际展示取决于卡片配置与宿主版本。0.14.0 原生接入已通过自动化测试，真实飞书端验收尚未执行。

## 停用与诊断

```bash
hermes plugins disable hermes-lark-streaming
hermes gateway restart
```

停用后恢复内置飞书展示。`python -m hermes_lark_streaming verify` 只读检查宿主接口和遗留注入，不会发消息或改配置。旧 `install`、`uninstall`、`restore` 补丁命令已移除。

## 开发与验证

在隔离开发环境安装本插件与固定的 Hermes 0.22.0 源码，然后执行：

```bash
python -m pip install -e '.[dev]'
python -m ruff check hermes_lark_streaming tests scripts __init__.py
python -m mypy hermes_lark_streaming scripts
python -m pytest tests -q
```

插件复用内置飞书适配器，并读取宿主的消息上下文；这些依赖包含私有接口，升级 Hermes 后应重新验证兼容性。

CI 固定测试 Hermes 0.22.0 和 0.22.5。API 测试使用模拟飞书服务，集成测试使用实际 Hermes 注册器和流式消费者；真实飞书收发需单独验收。

真实出站验收：设置 `FEISHU_E2E_APP_ID`、`FEISHU_E2E_APP_SECRET` 后，执行 `python scripts/feishu_smoke.py --chat-id <测试群ID> --scenario all`。脚本发送固定测试卡片，覆盖流式、序号恢复、拆卡、停止和静态更新；可加 `--reply-to <消息ID>` 验证引用，不调用模型或真实工具。入站权限、澄清和 Cron 调度仍需人工验收。

## 贡献者

感谢以下贡献者的 Issue 和 PR：

<a href="https://github.com/Mxin-9527"><img src="https://avatars.githubusercontent.com/u/178271393?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/gitteeee"><img src="https://avatars.githubusercontent.com/u/128769493?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/Bandersnatch0x"><img src="https://avatars.githubusercontent.com/u/13325067?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/runfali"><img src="https://avatars.githubusercontent.com/u/39327978?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/thunderfight127-svg"><img src="https://avatars.githubusercontent.com/u/275854191?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/willggy"><img src="https://avatars.githubusercontent.com/u/74762604?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/atomperson"><img src="https://avatars.githubusercontent.com/u/14934637?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/linjunxin01"><img src="https://avatars.githubusercontent.com/u/63715504?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/mouxangithub"><img src="https://avatars.githubusercontent.com/u/48978046?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/numuly"><img src="https://avatars.githubusercontent.com/u/137970054?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/wzgrx"><img src="https://avatars.githubusercontent.com/u/39661556?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/zhaomingcheng01"><img src="https://avatars.githubusercontent.com/u/46734892?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>

灵感来源于 [openclaw-lark](https://github.com/larksuite/openclaw-lark) 和 [hermes-feishu-streaming-card](https://github.com/baileyh8/hermes-feishu-streaming-card)。

## 许可证

[MIT](LICENSE)
