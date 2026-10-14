# DS 管理工具

双击 `Start.bat` 打开桌面窗口。需要 Windows、Python 3.10 或更高版本以及 Tkinter；使用 Python 标准库，无须 pip 安装。工具直接连接 AICore 的现有 HTTP 管理接口，不需要在 MiniGame 工程中运行。

启动后自动扫描本机监听端口，验证 `/api/aicore/v1/info` 与 `caps`，显示实际可连接的 AICore 服务。默认仅显示 Dedicated Server，取消“仅显示 DS”可以查看客户端。扫描不限定工程，因此可以同时管理不同工程的 DS。不会扫描其他机器。

点击服务列表的一行选中实例，顶部显示 PID、UDP/HTTP 端口及工程路径。可以指定游戏 UDP 或管理 HTTP 端口查找。点击“查看玩家”“查看 World”“地图模板”打开对应表格；玩家表可以直接踢出选中玩家，模板表可以直接创建副本。

底部输入命令，按 Enter 或点击“执行”。上下方向键切换历史命令。操作日志及 JSON 结果在“命令与结果”页显示。

```text
instances
use pid 18584
use port 7000
status
players
worlds
maps
create AIGamePlay.BattleA
kick 900000001 测试踢出
request 12
stop
help
```

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
