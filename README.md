# 网页快照兜底解析 · AstrBot

**内置 Playwright + Chromium 准备与生命周期管理，不需要额外浏览器插件，不调用第三方截图 API。**

作为其他链接解析插件之后的保守兜底：普通网页生成分片快照，NodeSeek 帖子单独提取正文和置顶 / Hot 评论。支持 HTTP / SOCKS5 上游代理，以及按域名直连、代理或禁止访问。

> **自动兜底遵循“其他插件已回复，就不再截图”。** v1.1.1 增加独立响应记录，兼容 `astrbot_plugin_multi_parser` 回复后恢复发送状态、OneBot 合并转发等情形；其他插件没有匹配 / 没有回复时仍可兜底。独立客户端、过晚的后台回复等观察边界见下文。

## 功能

- 自动发现消息中的 HTTP(S) 链接，监听优先级 `-10000`；手动 `/截图 URL` 可单独触发。
- 自动解析和手动命令均忽略当前 Bot 自己发出的消息（发送者 ID 与 Bot ID 相同），避免适配器回传自发消息时循环触发；缺失 ID 时不误判。
- 先只观察响应，再以低优先级兜底；识别普通回复、OneBot 合并转发 / 文件发送、事件停止及常见处理标记。即使解析器清空结果或恢复 `_has_send_oper`，也不重复截图；截图期间出现其他回复会在下一次发送前避让。
- 普通网页按高度分片；默认宽 1080px、总高上限 12000px、每片 3000px。
- NodeSeek 专用内容卡片：标题、作者信息、正文、置顶 / Hot 评论；去掉导航、侧栏、页脚和普通评论。保留基本排版、链接及可加载图片，不是原站 UI 的像素复刻。
- 域名路由 `direct / proxy / block`，可给不同域名配置不同上游。
- 有限并发、有限队列、总超时、用户冷却、同会话链接去重；发送完成后删除临时图片。
- 启动时后台检查 Chromium；缺少时调用官方 Playwright CLI 下载当前 Python 包对应的浏览器，失败有限重试，不阻塞 AstrBot 插件加载。
- 实际启动、独立上下文代理及离线 PNG 渲染探测成功后，配置中的 `browser_ready` 自动勾选；`browser_status` 自动记录准备进度或脱敏错误提示。
- 浏览器进程由本插件复用和管理：默认空闲约 120 秒休眠、下次截图唤醒；每次截图创建独立上下文，不复用个人资料、登录 Cookie 或持久状态。卸载时取消任务并清理自有浏览器 / 安装进程。

## 安装

要求 **Python 3.11+、AstrBot 4.16.x 至 4.x**，以及可以运行 Chromium 的环境。截图会占用额外内存，建议并发保持为 1，并对运行环境设置资源上限。

1. 在 AstrBot 插件管理中通过仓库 URL 安装：

   ```text
   https://github.com/VellowK/astrbot_plugin_screenshot
   ```

2. 在 **AstrBot 实际使用的 Python 环境** 中安装 Python 依赖。通常 AstrBot 会根据插件依赖文件安装；必要时手动执行：

   ```bash
   python -m pip install 'playwright>=1.48,<2' 'python-socks[asyncio]>=2.5,<4'
   ```

3. **无需再安装浏览器前置插件。** 保持 `auto_install_browser=true`（默认），重载本插件。后台先尝试启动现有 Chromium，只有缺少当前版本浏览器时才自动执行：

   ```bash
   python -m playwright install chromium
   ```

   此命令使用 **AstrBot 当前 Python 解释器**，下载到 Playwright 标准缓存（可用环境变量 `PLAYWRIGHT_BROWSERS_PATH` 调整）。首次通常需数百 MB 空间和可访问官方下载源的网络；**内置管理不等于把浏览器二进制打包进插件，也不等于离线免安装**。

4. 刷新插件配置页面，查看 **内置浏览器已就绪（自动勾选）** 和 **浏览器准备状态**；就绪后发送 `/截图 https://example.com/` 测试。首次下载时请等待，不要反复发消息触发；准备期间手动截图会返回进度提示，自动失败默认静默。

### 内置浏览器准备与状态

- 本插件不安装、不导入、不启用其他浏览器插件；不提供 BrowserUse Agent 工具，也不接管已有浏览器插件的会话。
- 状态流程：后台检查 → 缺少时下载 → 启动及离线渲染探测 → 自动勾选 `browser_ready`。探测同时验证独立上下文代理支持；不是只检查文件存在。`browser_status` 提示下载中、就绪、休眠或失败原因。
- 配置面板**不是实时推送**，需要刷新 / 重新打开查看。两个状态字段由插件维护，手动修改不能使浏览器就绪，也不决定后端选择。
- 默认每次下载总超时 600 秒，失败额外重试 1 次、间隔 30 秒；用 `browser_install_timeout` / `browser_install_retries` 调整。只有插件启动准备时会下载，普通消息不会启动安装器。最终失败后请按提示处理并重载。
- 下载代理用独立的 `browser_install_proxy`，支持 `http://` / `https://`，**不支持 SOCKS**；留空继承环境变量。网页访问仍用 `proxy` / `routes_json`，这两者不会被自动拿去下载。安装器遵循 Playwright 的 `HTTPS_PROXY`、`PLAYWRIGHT_DOWNLOAD_HOST`、`NODE_EXTRA_CA_CERTS` 等[官方环境变量](https://playwright.dev/python/docs/browsers#install-behind-a-firewall-or-a-proxy)，仅使用可信代理 / 镜像。
- 可设置 `auto_install_browser=false`，在镜像构建阶段预装浏览器，运行时只验证和使用。设置 `executable_path` 时只验证指定的系统 Chromium / Chrome / Edge，失败不会擅自替换或下载另一个浏览器。
- 默认 `browser_idle_seconds=120`，空闲检查间隔最多 15 秒；无运行任务时释放 Chromium，保留驱动，下次截图自动唤醒。休眠时勾选仍表示可用；设为 `0` 常驻。唤醒失败会清除就绪状态并提示重载。
- 更新 Playwright 后，重载会检查对应版本浏览器；缺少时可再次自动准备。无需手动降级任何 AstrBot 核心依赖。

### 系统库 / 容器权限仍需满足

自动准备只负责**官方浏览器下载与用户态进程**。它不会调用 `sudo` / `apt`、请求 root、关闭 Chromium 沙箱或修改宿主机权限。Linux 缺共享库时，管理员仍需在 **AstrBot 所在同一容器 / 系统环境** 安装：

```bash
python -m playwright install --with-deps chromium
```

例如 Docker（将 `astrbot` 替换成实际容器名）：

```bash
docker exec -u 0 astrbot python -m playwright install --with-deps chromium
```

浏览器缓存与运行用户必须一致或可读；必要时在构建镜像时预装系统依赖，再由实际运行用户完成浏览器下载。非支持发行版、缺系统库、无可写缓存 / 临时目录、网络不可达等问题，**不能仅靠集成代码消除**。按 `browser_status` 提示处理后重载。

### 更新说明

- v1.1.1：修复其他解析插件已响应后仍触发截图的问题，兼容 `astrbot_plugin_multi_parser` 恢复发送状态及 OneBot 合并转发；截图期间再次检查响应，自己的分片不会误判为其他插件回复。补充 WebUI 更新检测与手动更新说明。
- v1.1.0：内置 Chromium 后台检查 / 自动下载、真实离线渲染就绪探测、配置状态自动保存、进程复用及空闲休眠。不再依赖浏览器前置插件，不增加第三方截图 API。
- v1.0.2：自动解析和手动命令入口增加自消息过滤，防止 Bot 自发消息回传后再次触发截图。

更新后重载插件即可生效。

### WebUI 没提示更新？

**“GitHub 有新代码 / Release”不等于“WebUI 自动显示可更新”。** 本仓库会同步更新插件元数据版本、主分支、版本标签与 GitHub Release，但没有擅自注册 AstrBot Cloud 账号或保证已被插件市场收录。

根据 [AstrBot 官方使用文档](https://docs.astrbot.app/use/plugin.html)，**更新**下载新代码，**重载**只加载本地代码，不会下载新版本。[市场发布文档](https://docs.astrbot.app/dev/star/plugin-publish.html)要求另行通过 AstrBot Cloud 提交插件；仅把代码推送到 GitHub 不会自动完成市场发布。

同时核对了 [AstrBot v4.29.1 的更新检查实现](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.1/dashboard/src/views/extension/useExtensionPage.js)：

- 自动更新提示使用安装来源对应的**插件市场索引版本**，不是逐个轮询 GitHub Release。只有允许更新、来源为 `market` 且能在该源匹配到插件的记录参与比较。
- 仓库 URL 安装、ZIP 安装，或市场未收录 / 索引仍是旧版本时，可能没有更新提示。不同 AstrBot 版本的界面和来源迁移逻辑也可能不同。
- 比较器支持版本号的 `v` 前缀，不能仅凭没有提示就认定是 `version: v1.1.1` 的格式问题。

建议按以下顺序操作：

1. 插件详情确认本地版本及**更新来源**。若有“更新 / 更改更新来源”，选本仓库 `https://github.com/VellowK/astrbot_plugin_screenshot`，执行更新，再确认本地版本为 `v1.1.1`。**不必等出现更新角标才尝试手动更新。**
2. 若按市场更新，确认该插件已在所选源收录、在线版本正确，再刷新市场；下载失败时检查 AstrBot **容器内**到 GitHub 的网络及加速源缓存。
3. 若当前界面不支持仓库更新，可备份配置和持久化数据后，用本仓库重新安装；需要先删除时，不要勾选删除配置 / 持久化数据。也可以从 [v1.1.1 Release](https://github.com/VellowK/astrbot_plugin_screenshot/releases/tag/v1.1.1) 下载源代码 ZIP，按官方方式从文件安装。

本插件不会修改 AstrBot 核心或伪造市场索引。未连接你的 AstrBot 实例，无法直接确认其安装来源、市场响应或代理缓存；若仍无更新，请提供 AstrBot 版本、插件本地版本及更新来源（不要发凭据）。

### 从 v1.0.0 升级

v1.0.1 修正了 `python-socks` 依赖上限，兼容 AstrBot 4.29.1 核心固定的 `python-socks==3.1.1`。若安装或重载时看到 `<3` 与 `==3.1.1` 冲突，请先更新插件代码到 v1.0.1，再在插件面板重载失败插件；不要降级核心依赖或关闭其版本保护。

### Docker / Linux 注意事项

- 在宿主机安装浏览器不能代替容器内安装。构建镜像时安装依赖和浏览器，并确保运行用户可访问浏览器缓存。
- Chromium 默认启用沙箱。优先使用非 root 用户，配置可用的用户命名空间及容器沙箱支持。
- `no_sandbox=true` 会显著降低隔离能力；仅在确有需要并具备外部隔离时使用，不要把它当作通用修复方案。
- 安装中文字体（例如发行版的 Noto CJK 字体包），否则图片里的中文可能显示为方框。
- 若配置 `executable_path` 使用已有 Chrome / Chromium / Edge，需填写容器或服务器内的绝对路径；系统浏览器与 Playwright 的版本兼容性需自行验证。
- 浏览器不会在处理消息时自动下载。更新 Playwright 后重载插件可触发启动检查与自动准备；关闭自动安装时需自行补装对应版本。

## 使用

直接发送：

```text
https://example.com/
https://www.nodeseek.com/post-123456-1
```

手动触发与帮助：

```text
/截图 https://example.com/
/截图 https://www.nodeseek.com/post-123456-1
/截图 帮助
```

命令前缀以 AstrBot 的实际唤醒配置为准。自动兜底忽略以 `/` 开头的消息，不抢其他命令。

手动截图能覆盖 `skip_domains` 和已有解析结果，但**不能**覆盖禁用状态、会话限制、域名白名单、`block` 路由、冷却、去重或公网访问限制。

### NodeSeek 的处理范围

- 仅适配 `nodeseek.com` 和 `www.nodeseek.com` 下的 `/post-数字-页码` 帖子链接。
- 会统一到帖子**第一页**，只提取该页实际可见的置顶 / Hot 评论，不遍历所有分页，也不调用私有接口寻找其他热评。
- 置顶和 Hot 根据评论元信息中的徽标识别，不根据正文里出现的“HOT”等字样判断；同时带两种标记的评论只显示一次。
- 默认最多 10 条精选评论，置顶优先；`max_comments=0` 只保留正文。没有标记评论时只显示正文及提示。
- 登录限定内容、站点改版、风控、失效链接和无法加载的图片都可能导致缺失或失败。插件**不自动登录、不解验证码、不绕过 Cloudflare**。

## 与其他解析插件配合

**只在其他插件没有回复时自动兜底，不要求为了 `multi_parser` 额外填写域名。** 处理分为两个阶段：

1. 优先级 `10000` 的前置监听器只安装**当前事件的响应观察器**，不截图、不停止事件、不改解析器的结果或发送状态，也不替换共享平台客户端。
2. 通常优先级的解析插件运行后，本插件以 `-10000` 再判断是否需要兜底。成功响应记录独立存于当前事件，不会因为其他插件恢复 `_has_send_oper` 或 AstrBot 清空结果而丢失。

观察范围包括：

- 经 `event.send(...)` 成功发送的非空消息，以及 `after_message_sent` 钩子中仍存在的非空结果；
- 当前事件 `event.bot` 上的 OneBot `call_action` / `call_api` 与同名便捷方法：普通消息、群 / 私聊合并转发及群 / 私聊文件上传；查询、表情回应等非消息操作不算已回复，抛出异常或返回失败状态的 API 调用也不算成功回复；
- 事件停止状态、AstrBot 已发送状态及当前消息链；
- 整条消息标记：`screenshot_handled`、`url_parser_handled`、`link_parser_handled`；
- URL 列表标记：`handled_urls`、`parsed_urls`、`screenshot_handled_urls`。

针对 `astrbot_plugin_multi_parser`，已覆盖回复后恢复发送状态、普通发送与 OneBot 合并转发的回归场景；它未匹配 / 未回复时仍允许截图兜底。截图完成后、每片发送前都会再次检查，若期间观察到其他插件回复，则不再发送后续截图；已经发出的图片无法撤回。自己的截图发送不会被当作“其他插件已回复”，并发事件的记录互不共享。

**观察边界：** 这不是 AstrBot 官方的全局响应协议。其他插件若绕过当前事件、使用自己保存的独立客户端，或在本插件已发送后才后台回复，无法保证自动避免重复；比前置监听器优先级更高 / 比兜底更晚运行的插件也需单独协作。此类场景可配置 `skip_domains` 保证指定域名永不自动兜底，例如：

```json
["bilibili.com", "b23.tv", "youtube.com", "youtu.be"]
```

将上述数组填写到 `skip_domains`。域名列表只是示例，请按实际安装的解析器调整。

如维护其他解析器，可以在完成处理后设置：

```python
event.set_extra("url_parser_handled", True)  # 整条消息已处理
# 或只标记其中一个链接，让兜底仍可处理其他链接：
event.set_extra("handled_urls", ["https://example.com/a"])
```

如果前面的解析器已经回复，本插件会保守地跳过整条消息，而非尝试猜测其回复对应哪个链接。截图成功后设置本插件的处理标记并停止该事件；自动截图失败默认静默，不停止事件。

## 代理与域名路由

### 全部默认走代理

`proxy` 留空表示直连。配置后，所有未被域名规则覆盖的页面和资源都使用该上游：

```text
http://127.0.0.1:7890
socks5://127.0.0.1:1080
http://user:password@proxy.example.com:8080
```

支持 `http://` 和 `socks5://` 上游，可带认证。代理 URL 必须显式填写端口，特殊字符应进行 URL 编码。不支持 PAC、`https://` 代理或远端域名解析模式。

### 只让部分域名走代理

将 `proxy` 留空，在 `routes_json` 中填写以下 **JSON 数组文本**：

```json
[
  {"domain": "nodeseek.com", "action": "proxy", "proxy": "http://127.0.0.1:7890"},
  {"domain": "blocked.example", "action": "block"},
  {"domain": "example.cn", "action": "direct"}
]
```

规则按顺序**首条匹配生效**，不是最长域名优先。`proxy` 动作可省略规则内的 `proxy` 字段，此时使用全局代理，但必须已经配置全局代理。

- `example.com` 匹配自身及其子域名；`*.example.com` 只匹配子域名。
- 规则填写域名，不含协议、路径或端口。
- 规则作用于网络连接，包括重定向和资源。只给 NodeSeek 配代理时，它的外部图片 / CDN 域名仍按各自规则路由，需要时补充规则。
- Docker 中 `127.0.0.1` 指容器自身，不是宿主机；请按部署网络填写代理地址。

### DNS 与安全取舍

插件先在 **AstrBot 所在环境** 解析目标域名、校验全部解析结果，再向经过验证的公网数字 IP 建立直连或上游代理隧道。HTTPS 的原始主机名、SNI 和证书校验仍由浏览器保留。代理不是远端 DNS 的替代方案：本地 DNS 被污染、返回内网地址或无法解析时，请先配置可信 DNS。

只使用可信上游代理。代理入口由管理员配置，允许是本机或内网地址；这不意味着用户提供的网页链接能访问内网。

## 配置项

完整配置由 AstrBot 插件面板提供，定义见 [_conf_schema.json](./_conf_schema.json)。修改后重载插件。

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 插件总开关 |
| `auto_install_browser` | `true` | 启动时缺少 Chromium 则后台运行官方安装器，不安装系统库 |
| `browser_ready` | `false` | 自动维护的就绪勾选，需通过启动 / 代理 / 渲染探测；休眠仍就绪 |
| `browser_status` | 等待插件启动检查 | 自动维护的准备进度 / 脱敏失败提示，刷新面板查看 |
| `browser_install_timeout` | `600` | 每次下载总超时 60–1800 秒 |
| `browser_install_retries` | `1` | 下载失败后额外重试 0–3 次，间隔 30 秒 |
| `browser_install_proxy` | 空 | 官方安装器专用 HTTP(S) 代理，留空继承环境变量 |
| `browser_idle_seconds` | `120` | 空闲休眠 0–3600 秒；`0` 常驻，运行中不关闭 |
| `auto_enabled` | `true` | 自动兜底开关；关闭后仍可手动截图 |
| `require_at` | `false` | 自动截图要求被唤醒 / @ |
| `report_auto_errors` | `false` | 自动失败是否回复；手动失败始终回复 |
| `proxy` | 空 | 默认上游代理 |
| `routes_json` | `[]` | 首条匹配的域名路由 JSON 文本 |
| `skip_domains` | `[]` | 自动解析跳过域名 |
| `allowed_domains` | `[]` | 网络域名白名单；空表示不额外限制公网域名。非空时同时限制页面、重定向和资源，需加入必要 CDN |
| `allowed_sessions` | `[]` | 可使用的 `unified_msg_origin` 列表，空表示所有会话 |
| `max_links` | `1` | 每条消息处理 1–3 个链接 |
| `concurrency` | `1` | 截图并发 1–2；运行中加排队最多为该值的两倍 |
| `timeout` | `45` | 单个截图总超时 10–90 秒，含排队 |
| `settle_ms` | `1500` | DOM 加载后额外等待 0–5000 毫秒 |
| `width` | `1080` | 宽度 720–1440px |
| `max_height` | `12000` | 总高上限 900–16000px，超出截断 |
| `slice_height` | `3000` | 每片高度 900–4000px |
| `cooldown` | `15` | 每用户每会话请求冷却 0–300 秒，同一次消息的后续链接不重复冷却 |
| `dedup_seconds` | `180` | 同会话同链接成功去重 0–3600 秒；失败不加入成功去重，但仍受冷却限制 |
| `max_comments` | `10` | NodeSeek 精选评论上限 0–20 |
| `javascript` | `true` | 允许网页执行 JS；关闭可能影响动态内容，不影响插件自身 DOM 提取 |
| `no_sandbox` | `false` | 是否禁用 Chromium 沙箱，不推荐开启 |
| `executable_path` | 空 | 使用自动准备的 Playwright Chromium；指定系统浏览器时仅验证该路径 |

## 安全与限制

- 仅接受无内嵌账号密码的 HTTP(S) 网页，目标端口限 80 / 443。
- 阻止回环、内网、链路本地、保留、组播、非公网 IPv4 映射和特定 IPv6 转换地址；同一域名混合公网 / 非公网解析结果时整个拒绝。
- 浏览器使用临时本地转发网关，逐目标验证 DNS 并固定到已验证 IP，降低 DNS 重绑定与代理二次解析风险。域名白名单及路由禁止规则也在网关层执行。
- 拦截浏览器路由中的非 GET / HEAD 请求、WebSocket、流媒体、事件流、下载和服务工作线程。自有浏览器进程默认指向不可用的本地代理，每次截图上下文单独接入验证网关；启动参数额外限制 WebRTC 非代理 UDP、QUIC 等旁路。浏览器代理配置**不是操作系统防火墙**，仍须用网络层隔离补足浏览器旁路及浏览器漏洞风险。
- 每次截图的网关入站流量限制 40 MiB、并行连接限制 64、解析目标缓存限制 128 项；这不是浏览器进程的硬内存 / CPU 上限。对不可信链接建议使用无宿主敏感挂载的容器、出站防火墙、资源限制和非 root 用户。
- NodeSeek 内容卡片通过元素白名单重建，移除脚本、事件属性、表单、iframe 等。普通网页仍执行其原始内容，请按不可信网页对待。
- 不自动复用账号、登录 Cookie；不保证验证码、反爬、受限内容可访问。网页快照也不保证完整或永久有效。
- 回复标题不回显 URL 查询参数；日志只记录错误类型，避免泄漏代理凭据。但**网页图片本身可能包含敏感信息**，请勿在群里发送含令牌或私人信息的链接。
- 代理字段可能保存明文凭据；请限制 AstrBot 面板、配置文件及备份的访问权限。
- 图片在 `data/plugin_data/astrbot_plugin_screenshot/cache/` 的临时目录生成，等待发送后删除；进程崩溃或强制终止可能遗留临时文件。

## 开发与测试

在仓库目录，使用 Python 3.11+：

```bash
python -m pip install -r requirements.txt ruff
python -m unittest discover -s tests -v
ruff check .
ruff format --check .
```

普通测试不需要运行 AstrBot 服务，事件及 Playwright / 子进程使用接口替身；覆盖链接选择、解析器合作、路由、IP / DNS 限制、网关流量上限、分片坐标，以及启动探测、缺浏览器下载、有限重试、配置状态持久化、空闲唤醒、取消 / 卸载与安装器进程树清理。

真实浏览器测试默认跳过。安装 Chromium 后可显式开启，使用本地 NodeSeek HTML 夹具而非访问线上 NodeSeek：

```bash
# Linux / macOS
SCREENSHOT_TEST_BROWSER=chromium python -m unittest discover -s tests -v
```

```powershell
# Windows PowerShell
$env:SCREENSHOT_TEST_BROWSER = "chromium"
python -m unittest discover -s tests -v
```

也可把 `SCREENSHOT_TEST_BROWSER` 设为兼容浏览器的绝对路径。浏览器测试验证 DOM 提取、净化、PNG 分片，以及复用浏览器时的独立上下文 / 网关流量与既有会话隔离。成功时生成被 Git 忽略的 `.research/fixture-preview.png` 供目视检查。测试会显式替换网关连接到本地夹具，生产代码不放开内网访问。

当前开发环境中普通测试通过；此前真实浏览器测试尝试启动本机 Edge 时，被主机以 `EPERM: operation not permitted, mkdtemp ... playwright-artifacts-XXXXXX` 拒绝创建临时目录。此次自动下载与进程管理使用模拟测试验证，**尚未完成真实浏览器安装 / 渲染、线上 NodeSeek 或 AstrBot 平台发图联调**。上线前请在实际部署环境运行浏览器测试，并检查沙箱、中文字体及代理出口。

## 许可证

[MIT License](./LICENSE) · Copyright (c) 2026 VellowK
