# DS 管理工具

双击 `DSManager.bat` 打开桌面窗口。需要 Windows、Python 3.10 或更高版本以及 Tkinter；使用 Python 标准库，无须 pip 安装。工具直接连接 AICore 的现有 HTTP 管理接口，不需要在 MiniGame 工程中运行。

启动后自动扫描本机监听端口，验证 `/api/aicore/v1/info` 与 `caps`，显示实际可连接的 AICore 服务。默认仅显示 Dedicated Server，取消“仅显示 DS”可以查看客户端。扫描不限定工程，因此可以同时管理不同工程的 DS。不会扫描其他机器。

仅有一台 DS 时自动选中并加载大厅；有多台时点击需要管理的服务。顶部显示 PID、UDP/HTTP 端口及工程路径，也可以指定游戏 UDP 或管理 HTTP 端口查找。

默认打开“大厅与匹配”，同屏显示玩家、排队状态和比赛，每 3 秒自动刷新。刷新保留选中玩家，操作后留在当前页。

最少操作进入一局：

1. 启动 DS 和四个客户端：PowerShell 执行 `& "C:\MiniGame\Source\AIGamePlay\StartGame.bat" -Clients 4`。
2. 双击本目录 `DSManager.bat`，等待出现四名“大厅空闲”玩家。
3. 点击“一键凑齐一局”：按列表顺序选择足够的空闲玩家补到四人；已有一人排队时只加入另外三人。四人齐备后服务器自动创建副本并切换真实客户端。
4. 查看玩家状态从“排队中 / 切换中”变成“比赛中”，下方比赛显示 `Playing`。

自行选择队友：在玩家表使用 Ctrl / Shift 多选，点击“加入匹配”。取消时多选“排队中”玩家，点击“取消排队”。人数不足时首页提示还需要连接多少个客户端。

结束比赛：下方选中比赛，点击“结束选中比赛并返回大厅”；也可以选中比赛中的玩家，点击“返回大厅（整场）”。确认框显示受影响比赛数和总人数。当前玩法是一人返回就结束整场，同场成员全部返回；批量选择同场多人只提交一次返回命令。

各按钮按玩家状态启用，切换中的玩家不能再次加入匹配。批量操作逐人检查服务器最新状态，忽略已离线或状态不符的玩家；遇到失败或结果未知，停止后续提交，展示已完成结果，不重试写命令。连接失败时禁用匹配按钮，恢复连接后重新启用。

“World 管理”“地图模板”保留副本查看/创建功能；首页可踢出单个选中玩家。高级操作在“命令与日志”页输入命令，按 Enter 或点击“执行”。上下方向键切换历史命令，日志显示请求 ID 及 JSON 结果。命令行 JSON 接口保持原有用法。

```text
instances
use pid 18584
use port 7000
status
players
worlds
maps
match 900000001
cancel 900000001
matches
queue
return 900000001
create AIGamePlay.BattleA
kick 900000001 测试踢出
request 12
stop
help
```

`match` 把已在大厅的玩家加入排队，四人自动分两队并创建副本；`cancel` 取消尚未组成比赛的排队。`matches` 查看比赛与其 World/名单/队伍，`queue` 查看排队玩家。`return` 按当前玩法规则结束该玩家所在比赛，其他成员也返回大厅；工具等待目标玩家回大厅及已存在的客户端旅行记录达到 Active。

`create` 必须指定 `maps` 中列出的模板；含空格的 ID 使用双引号。副本为临时、无渲染 World，创建后不自动迁移玩家。工具会等待 World 到达 Running 后报告成功。旧服务器只返回分配结果时，工具继续查询 World；失败 World 消失时只能报告失败并提示查看服务器日志。

`kick` 使用现有权威接口，踢出的玩家本局不能重入。`stop` 正常停止当前选中的 DS，并等待原进程退出，不会强杀。关闭工具保持 DS 运行。网络超时或关闭工具不会撤销已经受理的操作；超时结果未确定时不要重复创建，可用 `request <请求ID>` 或 `worlds` 查询。

每次请求校验实例 ID、PID、工程路径及进程启动时间，拒绝旧实例和 PID 复用。扫描中和命令执行中暂不切换目标。只允许固定管理命令，不执行输入中的 Python、PowerShell 或任意 Lua。

也可从命令行使用，输出 JSON，错误返回非零退出码：

```powershell
python D:\git\skills\Tools\DSManager\ds_manager.py --scan
python D:\git\skills\Tools\DSManager\ds_manager.py --port 7000 --command players
python D:\git\skills\Tools\DSManager\ds_manager.py --pid 18584 --command worlds
```

扫描依赖 Windows `netstat` 和进程访问权限。服务必须已经启用 AICore HTTP；不是所有监听端口都是可管理服务。服务未启动、接口缺失或权限不足时不会列出为可连接实例。

运行验证：`python -B -m unittest discover -s tests -v`。

2026-10-14 验证结果：15 项协议测试全部通过；测试使用独立 HTTP 服务验证踢人参数、World Loading→Running/失败、超时不重发、实例/PID 复用拒绝，以及私有子进程正常停服。真实 AICore 实跑通过扫描、PID 选择、状态/玩家查询；Tkinter 界面通过扫描、实例选择、玩家/World/模板表格更新及执行期间保持目标一致的冒烟验证。本次未在已有真实 DS 上执行踢人、创建副本或停服。

2026-10-14 匹配扩展验证：19 项协议测试通过，Tkinter 的加入/取消/返回按钮和比赛/队列表格通过冒烟验证。同一 Python Client 在本次构建的 Profile DS 上驱动八个真实客户端，通过 A/B 副本轮换、双副本隔离、不同 World ID 映射、返回隔离与断线回收；DS 和八个客户端均正常退出。实跑详细证据保存在 AIGamePlay 的 .local/match-c8837f39-evidence.json。默认 StartGame.bat 使用的新 Profile 程序已构建；客户端自身 M/C/H 操作仍需人工输入验收。

2026-10-14 操作体验改进验证：24 项协议及 Tkinter 界面测试通过，覆盖多选加入/取消、只补缺少人数、刷新保留选择、断线禁用与恢复、返回整场人数确认及同场只提交一次、批量超时停止且不重发。另用独立 Profile DS 和两个真实客户端驱动实际界面按钮，通过自动选择实例、批量加入/取消、每三秒自动刷新、人数不足禁用及提示；三个自建进程均正常退出，证据为 AIGamePlay 的 `.local/dashboard-935e8548-evidence.json`。已有六个客户端占用了开发账号，因此本轮未重新跑四个真实客户端开局/返回；一键补足与整场返回的新界面流程使用独立 HTTP 测试服务验证，原有真实四人副本切换见上一轮证据。
