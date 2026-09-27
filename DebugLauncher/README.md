# AIFramework 调试启动器

这是 `C:\launcherDebug.bat` 的 GUI 版本，用于管理调试版 `AIFramework_d.exe`。

## 使用

双击 [`run_launcher.bat`](run_launcher.bat)，或在本目录运行：

```powershell
python .\launcher.py
```

界面支持：

- **启动**：保存当前配置并启动 AIFramework。
- **关闭**：结束本启动器创建的 AIFramework 及其子进程。
- **重新启动**：使用界面中的最新参数关闭后重新启动。
- **玩法参数**：按 `Scripts/Games/init.lua` 中 `Games.AppConfigs` 的顺序完整列出并使用单选按钮，同一时间只能启用一个；该文件变化后会自动刷新列表。
- **TopBattle 网络**：在 TopBattle 路由下可选择单机、登录联机、主机或客机；“登录联机”只打开游戏内认证和角色选择页，认证后再由游戏决定启动主机或客户端。
- **其他参数**：调试器自身参数列在独立分组中，通过复选框决定是否启用。
- **命令预览**：完整启动命令保持单行显示，可直接选中，也可点击“复制”写入系统剪贴板。
- **Attach 状态**：从 `.run/Lua.run.xml` 读取配置名和端口；状态栏用两个独立的状态图片分别显示 `Attach Lua`（IDE 到 DAP/桥接）与 `MiniGamePlay lua`（桥接到 AIFramework ScriptDebugger）的连接状态，绿色表示已连接、灰色表示未连接、红色表示调试服务失败；已连接时同时显示对应客户端或地址。
- **保存配置**：把程序路径、结构化参数列表和控制台选项保存到本地。
- **窗口记忆**：关闭时自动保存正常窗口的大小和屏幕位置，下次启动时恢复。

默认启动命令为：

```text
C:\MiniGame\Bin64\AIFramework_d.exe -MGFTopBattle -script-debug-wait-client
```

启动参数是固定集合：启动器会从 `Scripts/Games/init.lua` 的 `Games.AppConfigs` 按配置顺序读取全部玩法参数，并补充 `-script-debug-wait-client`。GUI 每 500 毫秒检查一次 `init.lua`；文件变化且能解析出有效玩法时会立即重建玩法单选项，保留仍然存在的当前选择，否则回退到 `-MGFTopBattle` 或表内第一项。若编辑期间暂时解析不到玩法，界面会保留旧列表并在运行日志中提示。所有玩法入口显示在“玩法参数（单选）”分组，调试器参数显示在“其他启动参数（可多选）”分组；不再扫描其他 Lua 文件中的玩法内部参数，也不提供添加、编辑、删除和排序功能。

旧配置会按参数名称迁移到固定列表；不在 `Games.AppConfigs` 中的旧玩法参数和未知自定义参数会被忽略。如果旧配置启用了多个玩法参数，只保留旧列表中第一个启用项。未保存过有效玩法选择时优先使用 `-MGFTopBattle`；该参数不在配置表中时使用表内第一项。

选择“登录联机”时，启动器只追加 `-TopBattleNetworkLogin`，不会预先传入 `-TopBattleNetworkHost`、`-TopBattleNetworkClient` 或角色端口参数，避免与游戏内登录后的角色选择冲突。主机和客机仍是直启调试/自动化模式，不显示游戏内登录 UI。

本地设置保存为同目录的 `settings.local.json`，包括窗口的 `宽x高+X+Y`；该文件已被忽略，不会进入版本控制。

## PyCharm / WebStorm 常驻调试

常驻调试服务已经集成到 AIFramework 调试启动器。双击 `run_launcher.bat` 打开 GUI 后，启动器会自动在后台启动常驻桥和 Node DAP，不需要再单独运行 `persistent_debug_server.py`。

默认 `127.0.0.1:4711` Attach 服务会在 AIFramework 尚未启动时先进入监听；可以先从 IDE Attach，再在 GUI 中启动实例。关闭或自然退出 AIFramework 只会让调试目标离线，不会关闭 Attach 服务或 IDE 会话。

Node DAP 的标准输出和错误输出由启动器持续接管并显示在运行日志中，避免无控制台的 `pythonw.exe` 启动方式使 DAP 在 IDE 接入时因无效输出句柄退出；`Attach Lua` 状态由 DAP 实际接受和关闭 IDE 连接的事件驱动。

启动器通过 `dap_adapter_compat.js` 延后 DAP `initialized` 事件，直到 `attach` 已经完成。这样 Rider/LSP4IJ 即使立即并发下发断点，也不会在 ScriptDebugger 尚未连接时触发 `ScriptDebugRuntime is not connected` 并导致 DAP 进程退出。

若启动器打开前已有 AIFramework 正在监听 ScriptDebugger `3382`，启动器会把它视为可连接的现有调试目标，仍正常启动桥接端口 `3383` 和 DAP 端口 `4711`。此时再从启动器创建新实例会自动使用下一组空闲端口，避免与现有目标冲突。

Lua 调试服务由 GUI 内置守护循环管理。Node DAP、常驻桥或服务线程异常退出后，会按 `1、2、4、8、10` 秒退避自动重新启动；关闭 GUI 时则正常停止，不会再次拉起。Node DAP 断开时，常驻桥会沿用现有的断点清理和 `continue` 逻辑，避免 AIFramework 停在断点处失去调试连接。

常驻链路为：

```text
PyCharm / WebStorm --DAP 4711--> LuauDebugAdapter --3383--> 常驻桥 --3382--> AIFramework
```

常驻桥会保留 IDE 与 Debug Adapter 的连接。AIFramework 退出时不会向 IDE 发送 `terminated`；新的 AIFramework 启动后，常驻桥会自动连接并在发送 `configurationDone` 前恢复缓存的断点。GUI 启动参数需保留：

```text
-script-debug-wait-client
```

首次使用时按以下顺序操作：

1. 停止 PyCharm / WebStorm 之前启动的旧 Debug Adapter，确保 DAP 端口 `4711` 空闲。
2. 双击 `run_launcher.bat`，等待界面显示“常驻调试：运行中”。
3. 在 PyCharm / WebStorm 中 Debug `Lua`；该配置使用 Attach 模式连接 `127.0.0.1:4711`，并将运行时 Lua 服务映射到 `Scripts` 目录。
4. 用本 GUI 任意启动、关闭或重新启动 AIFramework。IDE 调试会话无需停止。

PyCharm / Rider 的 EmmyLua 插件创建的是 `lua-line` 断点，而 LSP4IJ DAP 只直接处理 `dap-breakpoint`。启动器会自动查找直接打开 AIGamePlay 的 `.idea/workspace.xml`，也支持 Rider 解决方案的嵌套 `.idea/.idea.<Solution>/.idea/workspace.xml`，将 `Scripts/**/*.lua` 下启用的 `lua-line` 断点自动转换成运行时断点；同时监听 JetBrains `idea.log` 的实时增删事件，避免尚未写回 `workspace.xml` 的旧断点在游戏重启时被恢复。启用该同步后，IDE 中可见的断点是运行时的唯一权威来源，LSP4IJ 重发的历史 `dap-breakpoint` 不会再产生不可见或错位的断点。日志出现 `Using JetBrains workspace` 表示已定位当前工作区，出现 `Watching JetBrains breakpoint events` 表示实时增删监听已启用，出现 `Loaded N JetBrains Lua breakpoint(s)` 表示已读取断点，出现 `Applied JetBrains Lua breakpoints` 表示改动已同步到当前 AIFramework。

如果旧进程仍占用 `4711`，界面会显示“常驻调试：启动失败”。停止旧调试会话后，点击“重试服务”即可，无需重启 GUI。

GUI 运行日志出现 `AIFramework offline; waiting for restart` 表示正在等待 EXE；出现 `restored N breakpoint(s)` 表示新进程已连接且断点已恢复。

## 进程安全边界

启动器使用 Windows Job Object 管理进程树，只能关闭当前 GUI 实例亲自启动的进程，不会按名称查找或终止其他 AIFramework 实例。关闭启动器窗口时，它管理的 AIFramework、常驻调试桥和 Node DAP 也会被关闭。

## 测试

```powershell
python -m unittest discover -s .\tests -v
```
