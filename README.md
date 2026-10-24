# 网页快照兜底解析 · AstrBot

**本机 Playwright + Chromium 截图，不调用第三方截图 API。**

作为其他链接解析插件之后的保守兜底：普通网页生成分片快照，NodeSeek 帖子单独提取正文和置顶 / Hot 评论。支持 HTTP / SOCKS5 上游代理，以及按域名直连、代理或禁止访问。

> 低优先级不是解析器之间的统一协议，不能保证识别所有插件的“已处理”状态。生产环境请把已有专用解析器的域名加入 `skip_domains`，详见下文。

## 功能

- 自动发现消息中的 HTTP(S) 链接，监听优先级 `-10000`；手动 `/截图 URL` 可单独触发。
- 自动解析和手动命令均忽略当前 Bot 自己发出的消息（发送者 ID 与 Bot ID 相同），避免适配器回传自发消息时循环触发；缺失 ID 时不误判。
- 识别事件停止、已有回复、常见处理标记和已处理 URL 列表，避免重复回复。
- 普通网页按高度分片；默认宽 1080px、总高上限 12000px、每片 3000px。
- NodeSeek 专用内容卡片：标题、作者信息、正文、置顶 / Hot 评论；去掉导航、侧栏、页脚和普通评论。保留基本排版、链接及可加载图片，不是原站 UI 的像素复刻。
- 域名路由 `direct / proxy / block`，可给不同域名配置不同上游。
- 有限并发、有限队列、总超时、用户冷却、同会话链接去重；发送完成后删除临时图片。
- 独立临时浏览器上下文，不复用个人浏览器资料、登录 Cookie 或持久登录状态。

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

3. 安装浏览器（Python 包安装不会自动完成这一步）：

   ```bash
   python -m playwright install chromium
   ```

   Linux 缺少系统库时，可由有权限的管理员在**同一运行环境**执行：

   ```bash
   python -m playwright install --with-deps chromium
   ```

4. 重载插件，打开插件配置页面，按需设置代理和跳过域名，再发送链接测试。

### 更新说明

v1.0.2 在自动解析和手动命令入口增加自消息过滤，防止 Bot 自发消息回传后再次触发截图。更新后重载插件即可生效。

### 从 v1.0.0 升级

v1.0.1 修正了 `python-socks` 依赖上限，兼容 AstrBot 4.29.1 核心固定的 `python-socks==3.1.1`。若安装或重载时看到 `<3` 与 `==3.1.1` 冲突，请先更新插件代码到 v1.0.1，再在插件面板重载失败插件；不要降级核心依赖或关闭其版本保护。

### Docker / Linux 注意事项

- 在宿主机安装浏览器不能代替容器内安装。构建镜像时安装依赖和浏览器，并确保运行用户可访问浏览器缓存。
- Chromium 默认启用沙箱。优先使用非 root 用户，配置可用的用户命名空间及容器沙箱支持。
- `no_sandbox=true` 会显著降低隔离能力；仅在确有需要并具备外部隔离时使用，不要把它当作通用修复方案。
- 安装中文字体（例如发行版的 Noto CJK 字体包），否则图片里的中文可能显示为方框。
- 若配置 `executable_path` 使用已有 Chrome / Chromium / Edge，需填写容器或服务器内的绝对路径；系统浏览器与 Playwright 的版本兼容性需自行验证。
- 浏览器不会在处理消息时自动下载。更新 Playwright 后应重新执行其浏览器安装命令。

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

默认监听优先级为 `-10000`，以便通常优先级的处理器先运行。随后检查：

- `event.is_stopped()`；
- AstrBot 的已发送状态 `_has_send_oper`（内部实现细节，尽力兼容）；
- `event.get_result()` 是否已有消息链；
- 整条消息标记：`screenshot_handled`、`url_parser_handled`、`link_parser_handled`；
- URL 列表标记：`handled_urls`、`parsed_urls`、`screenshot_handled_urls`。

这些额外标记不是 AstrBot 官方统一标准。若另一个插件使用后台任务、平台直发、延后发送，或本身优先级更低，本插件可能无法及时观察到结果。因此推荐显式配置，例如：

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
| `executable_path` | 空 | 使用 Playwright Chromium；也可填兼容的系统浏览器绝对路径 |

## 安全与限制

- 仅接受无内嵌账号密码的 HTTP(S) 网页，目标端口限 80 / 443。
- 阻止回环、内网、链路本地、保留、组播、非公网 IPv4 映射和特定 IPv6 转换地址；同一域名混合公网 / 非公网解析结果时整个拒绝。
- 浏览器使用临时本地转发网关，逐目标验证 DNS 并固定到已验证 IP，降低 DNS 重绑定与代理二次解析风险。域名白名单及路由禁止规则也在网关层执行。
- 拦截浏览器路由中的非 GET / HEAD 请求、WebSocket、流媒体、事件流、下载和服务工作线程；关闭 WebRTC 非代理 UDP 等旁路。浏览器代理配置**不是操作系统防火墙**，这些措施不能替代网络层隔离。
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

普通测试不需要运行 AstrBot 服务，事件集成使用接口替身；覆盖链接选择、解析器合作、路由、IP / DNS 限制、网关流量上限、生命周期和分片坐标。

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

也可把 `SCREENSHOT_TEST_BROWSER` 设为兼容浏览器的绝对路径。浏览器测试验证 DOM 提取、净化和 PNG 分片，并生成被 Git 忽略的 `.research/fixture-preview.png` 供目视检查。测试会显式替换网关连接到本地夹具，生产代码不放开内网访问。

当前开发环境中普通测试通过；Chromium 安装被主机以 `EPERM` 拒绝，因此未在该环境完成真实浏览器渲染、线上 NodeSeek 或真实 AstrBot 平台发图联调。上线前请在实际部署环境运行浏览器测试，并检查沙箱、中文字体及代理出口。

## 许可证

[MIT License](./LICENSE) · Copyright (c) 2026 VellowK
