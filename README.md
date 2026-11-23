# 嘉立创自动签到工具  
*By zhangMonday*

[立创开源平台](https://oshwhub.com/)

[嘉立创金豆中心](https://activity.jlc.com/goods/goodsList?spm=JLC.MEMBER)

请确保你的立创账号已经在开源平台设置了昵称，否则开源平台自动签到将无法运行

每个账号完整跑完签到流程用时至多5分钟，Github Actions单次任务执行时长限制在6小时以内，因此不建议设置自动签到70个账号以上。如果有大量账号自动签到需求，可以手动多克隆几个本仓库来批量运行。

---

## 项目简介  
该项目利用 **Github Actions** 实现立创开源平台以及立创金豆的每天自动化签到

支持多账号签到

支持自动领取7日礼包和每月礼包

支持自动过滑块验证

支持自动通过登录信息抓包X-JLC-AccessToken和secretkey实现金豆签到

支持金豆奖励领取

支持 Server酱3 推送签到总结（多账号可分别配置 SendKey，同 key 自动合并）

---

## 关于滑块验证（阿里云验证码 2.0）

嘉立创登录页的验证码已经从网易易盾换成了**阿里云验证码 2.0**（`#aliyunCaptcha-window-embed`，
"请按住滑块，拖动到最右边"）。相关代码拆成了两个模块：

- `captcha_solver.py`：滑块识别 + 拟人轨迹拖拽
- `stealth.py`：清除 Selenium 自动化特征

实测结论（对本项目的排错很有用）：

1. 只用 `ActionChains` 拖两下必失败，阿里云会分析轨迹的加减速、纵向抖动、采样间隔；
   这里用 CDP `Input.dispatchMouseEvent` 逐点下发，并自己控制事件时间戳，
   轨迹是"先加速、中段巡航、末段减速 + 轻微手抖 + 随机停顿"。
2. 滑块必须**顶到轨道最右端**，差 1px 都会被弹回去重来。
3. **最关键的一点**：即使轨迹完美，前端显示"验证通过"，服务端
   `/api/cas/captcha/v2/check-ali-captcha` 仍会返回 `checkSuccess=false`（"滑块验证失败"）。
   原因是页面里留着驱动注入的 `window.cdc_adoQpoasnfa76pfcZLmcfl_*` 标记。
   这些属性删不掉时会被重定义成 `undefined` 的 getter，`stealth.py` 做的就是这件事，
   顺带补上 `navigator.webdriver`、`window.chrome`、`languages/plugins`、权限查询等指纹。
4. 原代码里的 `--blink-settings=imagesEnabled=false` 会让滑块背景图不渲染、
   元素尺寸变成 0，已移除。

可用环境变量（本地调试用）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `JLC_HEADLESS` | `1` | 设为 `0` 打开浏览器窗口，方便肉眼看拖拽过程 |
| `JLC_BROWSER` | `chrome` | 本地没有 Chrome 时可设为 `edge` |

本地单独验证滑块（不跑签到）：

```bash
python test_captcha.py --account 你的账号 --password 你的密码
python test_captcha.py --account 你的账号 --password 你的密码 --browser edge --headless
python test_captcha.py --fake            # 用假账号触发验证码，只看滑块能不能过
```

脚本会把每一步截图存到 `debug/`，方便判断是"没拖到底"还是"被判定机器"。

---

## 配置方法  

### 1. Fork 项目到自己的仓库  
- 访问项目页面，点击 `Fork` 按钮将项目复制到自己的仓库。  

![步骤1](img/1.jpg)

---

### 2. 配置嘉立创账号和密码  
1. 进入你 Fork 的仓库，点击 **Settings** → **Secrets** → **New repository secret**。  
2. 添加以下两个密钥：  
   - **Name**: `JLC_USERNAME`  
     **Value**: 你的嘉立创登录账户邮箱/手机号/客编，多账号用英文逗号分割，要和密码一一对应
   - **Name**: `JLC_PASSWORD`  
     **Value**: 你的嘉立创登录密码，多账号用英文逗号分割，要和账号一一对应

![步骤2](img/2.jpg)

---

### 3. 配置 Actions 自动化执行  
1. 在你 Fork 的 GitHub 仓库中点击 **Actions**  
2. 按照图示方法启用 GitHub Actions

![步骤3](img/3.png)

3. 点击 **Run workflow** 手动触发一次，如果运行结束出现绿色对勾则为成功

![步骤4](img/4.jpg)

---

### 4. 定时自动化执行  
在 `.github/workflows/main.yml` 文件中，根据注释修改自动签到时间。  
如果不修改，默认自动签到时间为北京时间早上 7 点（23:00 UTC）和晚上 19 点（11:00 UTC）

实际运行时间会比设置时间延迟一小时以内，具体取决于当时 GitHub 资源占用情况。

保活用的心跳记录会统一推到独立的 `heartbeat` 分支（首次运行自动创建），分支上只有一个 `heartbeat.txt`，内容是最近一次运行时间；main 分支不再产生空提交。

![步骤5](img/5.png)

---

### 5. 获取更新

在你fork的仓库中找到sync fork，点击即可更新，如图

![获取更新](img/7.png)

下一次自动签到会自动运行更新后的程序

推荐定期检查更新来优化脚本并获取最新功能

---

### 6. 配置 Server酱3 推送（可选）

签到全部完成后会统一推送一条总结。推送走 [Server酱3](https://sc3.ft07.com/)，
不配置就不会推送。

1. 登录 `https://sc3.ft07.com/`，在 **SendKey** 页面复制你的 SendKey（形如 `sctp12345tAbCd...`）。
2. 在你 Fork 的仓库里 **Settings** → **Secrets** → **New repository secret**，添加：
   - **Name**: `SERVERCHAN3_SCKEY`
   - **Value**: 你的 SendKey

多账号可以给每个账号配不同的 SendKey，**按账号顺序用英文逗号分隔**，和 `JLC_USERNAME` 一一对应：

```
SERVERCHAN3_SCKEY=sctp111tAAA,sctp222tBBB
```

分配规则：

| 情况 | 结果 |
| --- | --- |
| 不配置 | 不推送 |
| 只配一个 `sctp111tAAA` | 所有账号合并成 **一条** 推送 |
| 配两个，共 3 个账号 | 账号 1 用第 1 个、账号 2 用第 2 个、账号 3 回落到第 1 个 |
| 中间留空 `sctp111tAAA,,sctp333tCCC` | 账号 2 回落到第 1 个 |
| 多个账号共用同一个 SendKey | 结果合并成 **一条** 推送，不会重复发 |

推送内容（同一 SendKey 下的账号合并成一条，正文每行之间留空行，适配 Server酱3 的 markdown 渲染）：

```
签到统计

成功：4

已签到：0

失败：0

【签到成功】

✅ 账号(123****78A)：获取2个金豆，当前总数：90.0

✅ 账号(123****79A)：获取2个金豆，当前总数：71.0
```

- **标题**：该条推送里「失败」为 0 时是 `嘉立创签到成功`，否则 `嘉立创签到失败`。
- **签到统计**：按金豆签到结果计数。`成功` = 本次签到拿到金豆；`已签到` = 今天之前已经签过；
  `失败` = 签到失败、Token 提取失败、密码错误等。
- **账号打码**：前后各保留 3 位、中间固定 4 个星号（`12345678A` → `123****78A`，邮箱保留域名）。
- 日志里只打印打码后的 SendKey（如 `sctp1234***WxYz`）。

> 注意：Server酱3 和 Server酱Turbo 是两套账号体系，Turbo 的 `SCT...` 开头 key 不能用在这里，
> 旧的 `SERVERCHAN_SCKEY`（Turbo）仍然独立生效。

---

### 本地运行与调试（推荐先在本地验证）

#### 1. 准备环境

需要 Python 3.7+ 和 Chrome（没有 Chrome 会自动改用 Edge）。

```bash
git clone <你的仓库地址>
cd jlc-auto-sign
python -m venv .venv
.venv/Scripts/activate           # macOS / Linux: source .venv/bin/activate
pip install -r requirements.txt
```

selenium 4 会自己下载并匹配浏览器驱动，不用手动装 chromedriver。

#### 2. 先跑自测（不需要真实账号）

```bash
# 只验证「签到页 -> 登录页」这一段，最快（约 10 秒）
.venv/Scripts/python.exe test_captcha.py --fake --only-entry

# 验证完整链路：进登录页 -> 填假账号 -> 过滑块（最后提示"密码不正确"属正常）
.venv/Scripts/python.exe test_captcha.py --fake

# 想肉眼看浏览器操作过程
.venv/Scripts/python.exe test_captcha.py --fake --headful

# 用真实账号完整跑一遍（不写进命令行，避免留在历史记录里）
#   PowerShell:  $env:JLC_USERNAME="你的账号"; $env:JLC_PASSWORD="你的密码"
#   cmd:         set JLC_USERNAME=你的账号 & set JLC_PASSWORD=你的密码
.venv/Scripts/python.exe test_captcha.py
```

每一步的截图会存到 `debug/`（已被 `.gitignore` 忽略），出问题直接看 `01_login_entry.png`、`03_captcha.png`、`04_after_solve.png`。

#### 3. 运行主脚本

```bash
# 参数方式
python jlc.py 账号1,账号2 密码1,密码2 [失败退出标志]

# 环境变量方式（推荐本地调试，密码不落在命令历史里）
python jlc.py            # 自动读 JLC_USERNAME / JLC_PASSWORD
```

#### 4. 环境变量一览

| 变量 | 作用 | 示例 |
| --- | --- | --- |
| `JLC_USERNAME` | 账号，多个用逗号分隔 | `13800138000,abc@qq.com` |
| `JLC_PASSWORD` | 密码，与账号一一对应 | `pwd1,pwd2` |
| `JLC_BROWSER` | 浏览器，`chrome` 或 `edge` | `edge` |
| `JLC_HEADLESS` | 设为 `0` 显示浏览器窗口（默认无头） | `0` |
| `JLC_FAIL_EXIT` | 设为 `true` 时任意账号失败返回非零退出码 | `true` |
| `SERVERCHAN3_SCKEY` | Server酱3 推送 key，多个用逗号分隔 | `sctp111tAAA` |

注意：`JLC_HEADLESS=0` 表示「显示窗口」，没有 `JLC_HEADFUL` 这个变量。

注入方式（Windows）：

PowerShell（只对当前窗口有效）：

```powershell
$env:JLC_BROWSER = "edge"
$env:JLC_HEADLESS = "0"
$env:JLC_USERNAME = "13800138000"
$env:JLC_PASSWORD = "你的密码"
.\venv\Scripts\python.exe jlc.py
```

cmd：

```bat
set JLC_BROWSER=edge
set JLC_HEADLESS=0
set JLC_USERNAME=13800138000
set JLC_PASSWORD=你的密码
.venv\Scripts\python.exe jlc.py
```

Git Bash / macOS / Linux（只对本次命令有效）：

```bash
JLC_BROWSER=edge JLC_HEADLESS=0 JLC_USERNAME=13800138000 JLC_PASSWORD=你的密码 \
  .venv/Scripts/python.exe jlc.py
```

清除：PowerShell 用 `Remove-Item Env:JLC_BROWSER`，cmd 用 `set JLC_BROWSER=`。

#### 5. 常见问题

- **卡在「页面未自动跳转」**：签到页里拿不到可见的登录链接，通常是网络慢、页面还没渲染完。脚本会等 12 秒，再退到默认的 passport 登录地址；仍然失败时会打印当前 URL，配合 `debug/` 截图排查。
- **滑块一直「验证失败」**：先确认 `stealth.py` 注入成功（`window.cdc_*` 标记已抹掉），并检查是否误加了 `--blink-settings=imagesEnabled=false`（会让滑块背景图不渲染、尺寸变 0）。
- **想看错误现场**：加 `JLC_HEADLESS=0`，或给 `test_captcha.py` 加 `--keep`，浏览器不会自动关，可以直接在窗口里操作。

#### 6. 无头模式 / GitHub Actions 注意事项

无头模式最容易踩的坑是 **UA 请求头里的 `HeadlessChrome`**：

- `--headless` 启动的浏览器，HTTP 请求头里的 User-Agent 是 `...HeadlessChrome/xxx...`；
- 只在页面里改 `navigator.userAgent` 没用，服务端和 WAF 看的是请求头；
- oshwhub 的 WAF（瑞数 CloudWAF）会把这类请求直接拦掉，返回 **418「访问被拦截！」**，
  表现为：登录明明成功、也跳回了签到页，但 `/api/users` 取不到昵称和积分，后续签到全失败。

脚本已在 `create_driver()` 里用 CDP `Emulation.setUserAgentOverride` 在会话层覆盖 UA
（自动取当前浏览器真实版本，去掉 `Headless` 字样，不写死版本号），Windows / Ubuntu 都适用。
启动时会打印：

```
[18:13:26] 已覆盖无头 UA 请求头（去掉 Headless 标识）
```

如果看到的是 `警告: UA 覆盖未生效`，说明当前驱动不支持 CDP，需要升级浏览器或驱动。

排查顺序（无头失败时按这个走）：

1. 日志里有 `422/418`、`访问被拦截`、`/api/users 被 WAF 拦截` → UA / 指纹问题；
2. 日志里 `/api/users` 是 `401` → 会话没建立，看上面的登录流程；
3. `无法获取用户昵称` 后面紧跟着签到失败 → 基本就是第 1 条。

另外无头模式的 `screen` 会报成 800×600（窗口却是 1920×1080），`stealth.py` 会一起修正，
避免「高分辨率窗口 + 十几年前的分辨率」这种明显矛盾。

GitHub Actions（ubuntu-22.04 + Chrome + selenium 3.141）额外注意：

- 工作流里不需要装 xvfb，脚本本身就是无头；
- Chrome 低于 109 时 `--headless=new` 会被自动降级成 `--headless`；
- 服务器 IP 属于机房段，风控更严，偶尔需要重试，脚本自带重试逻辑。

---

### 成功运行结果（节选）
![成功运行结果](img/6.png)

## 致谢  
本项目参考和复用了 [https://github.com/wmathor/Check_In](https://github.com/wmathor/Check_In) 的部分代码，感谢！

本项目参考了 [https://github.com/sudojia/AutoTaskScript](https://github.com/sudojia/AutoTaskScript/) 的嘉立创脚本，感谢！

欢迎提交issue和PR，如果本项目对你有帮助，请给我点个star~
