# 杭电校园事务提醒助手

单用户微信校园助手。DeepSeek Harness 处理自然语言和工具调用。项目使用 SQLite 保存课表、通知、日程草稿和投递状态，使用 DSH-IM 将到期提醒和每日早报推送到已绑定的微信私聊。

## 先运行本地演示

环境为 Windows 和 Python 3.12。解压后在项目目录打开 PowerShell。

```powershell
python main.py demo
```

演示使用临时数据库、模拟课表和模拟通知，依次显示早报、确认口令、课前提醒和日程提醒。无需联网或安装 Python 依赖。

## 接入真实微信

### 1. 建立环境并配置投递目标

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-win.txt
python main.py agenda
```

首次运行需要配置的命令时，程序会从 `config.example.json` 自动生成 `config.json`。在 Harness 的「设置 → IM机器人」中打开微信机器人的已保存投递目标，复制 `botId` 和 `targetId`，分别填进 `config.json` 的 `bot_id`、`target_id`，并把 `mode` 改为 `wechat`。 [DSH-IM 主动投递指南](https://github.com/xmanrui/dsh-im/blob/main/PROACTIVE_DELIVERY.md)

`harness_url` 留空时，程序会寻找正在运行的 Windows 桌面版的本机投递接口。可用下面的命令检查识别结果。若要指定地址，填写形如 `http://127.0.0.1:3080` 的基础 URL。桌面版每次启动的实际端口可能不同。本项目使用 MCP 标准输入输出，业务代码无需占用自有 HTTP 端口。

```powershell
.\.venv\Scripts\python.exe main.py discover
.\.venv\Scripts\python.exe main.py test-send
```

第二条命令会发送一条真实的测试微信。以手机实际收到为准。

### 2. 把业务工具加入 Harness

```powershell
.\.venv\Scripts\python.exe main.py mcp-config
```

在 Harness 的设置中新增一个 MCP 服务，名称填 `hdu_campus`，传输方式选 `stdio`。把命令输出中 `command` 的值填到可执行文件，把 `args` 中的 3 项依次填入参数列表。界面有连接测试时，确认能列出 `campus_time`、`draft_event`、`confirm_event` 等工具。若设置中使用 JSON 导入，可使用命令输出的配置，导入后确认传输类型为 `stdio`。 [Harness MCP 管理器配置](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-manager/README.md)

为绑定的微信私聊使用单独的校园助手会话。MCP 服务在连接时加载 [校园助手指令](./校园助手指令.md)。用「查一下今天的课程」测试工具调用。随后发「明天上午 9 点交实验报告，提前 10 分钟提醒」，等助手给出草稿，再按它回显的完整口令确认。模型会从最新用户消息转交口令，项目验证口令、有效期并保存日程。MCP 服务信任调用它的 Harness 进程，所以首版仅用于你自己的私聊会话。

### 3. 启动调度并同步通知

```powershell
.\.venv\Scripts\python.exe main.py import-classes .\examples\课表样例.csv
.\.venv\Scripts\python.exe main.py run
```

第一行导入的是固定日期的模拟课程。固定日期的临时课程可继续通过 CSV 导入，重复导入相同课程会去重。学期课表可直接写到配置。采用课表配置时，同一门课只录入一次，避免重复提醒。

- `campus_periods` 按校区保存每节的开始和结束时间，例如 `"1": ["08:00", "08:45"]`。
- `timetables` 可存多份课表。每份设置第 1 周周一 `week1_monday`、每日和课前标题 `daily_title`、`reminder_title`，以及提前分钟数 `remind_minutes`（默认 15）。每条课填写名称、星期、首末节次、周次、校区和地点。星期一为 1，星期日为 7。周次 `"3,7-9(单),14-16(双)"` 对应第 3、7、9、14、16 周。
- `weather_locations` 每个地点填写展示名称 `name` 和中国天气网页网址中的九位 `city_code`。一天内可显示多个地点。天气取不到时，早报会注明相应地点暂未获取到预报。 [中国天气网](https://www.weather.com.cn/)

改完配置后重启调度进程和 Harness。CLI 与 MCP 使用同一份实际配置。运行 `python main.py preview YYYY-MM-DD` 可查看指定日期的早报和每日课表，不会发送微信。

保持 Harness、调度进程和电脑运行。默认北京时间 07:30 投递早报和每份处于教学周内的课表。早报包含所选地点天气、个人日程和最多五条新通知。微信回复「把 N12 加到日程」时，Harness 读取该通知原文并按人工确认流程创建日程。通知正文是按需读取的纯文本，图片和附件可打开原文核对。 [杭电教务处通知公告](https://jwc.hdu.edu.cn/13022/list.htm) · [通信工程学院教学动态](https://comm.hdu.edu.cn/jxdt/list.htm) · [通信工程学院公示公告](https://comm.hdu.edu.cn/gsggl/list.htm)

## 验证和排查

```powershell
.\.venv\Scripts\python.exe -m unittest -v
.\.venv\Scripts\python.exe main.py sync
.\.venv\Scripts\python.exe main.py status
```

- 测试覆盖 MCP 握手与创建日程、草稿过期、重复确认、取消、课表导入、公告解析和模拟微信投递接口。
- `sent` 表示投递接口已接受。接口结果不明时记录为 `uncertain` 或保留为 `sending` 供核查。机器人离线或暂时连不上时在提醒有效期内重试。微信群体推送与跨用户权限隔离尚未实现。
- 电脑短暂休眠或调度器重启时，早报可在设定时间后三小时内补发。提醒可在应提醒时间后约十分钟内补发，且默认在事项开始前有效。开始时提醒有约一分钟的投递窗口。
- 切换到真实微信前，给真实运行使用单独的数据库。演示模式和真实运行分开可保留各自的投递历史。

## 源码与提交

源码包含业务流程、MCP 服务、调度和采集实现。保存到 Git 私有仓库即可做版本管理。参赛时按平台要求提交可访问的仓库链接或源码包。保留样例配置、测试和指令；本地配置、SQLite 数据、API Key 和微信凭据留在各自的运行环境中。录屏可演示「早报 → 查通知 → 生成草稿 → 微信确认 → 到点收到微信提醒」。
