# App 调试启动器

用于输入启动参数并管理 `AICore_profile.exe` 等 App。默认程序路径是 `C:\MiniGame\Bin64\AICore_profile.exe`；可浏览选择其他 EXE。

## 使用

双击 [`run_launcher.bat`](run_launcher.bat)，或在本目录运行：

```powershell
python .\launcher.py
```

1. 选择要启动的程序。
2. 在 **App 启动参数** 中直接输入参数，或点击 **填入 Host 参数** / **填入 Client 参数** 后编辑。这里只填写参数，不包含 EXE 路径；含空格的值用双引号包裹。
3. 按需勾选 **等待 Lua 调试器连接**。若只想直接运行 App、不等待 IDE 接入，取消该选项。
4. 查看命令预览，点击 **启动新实例**。

两个预设分别对应 `C:\MiniGame\Bin64\StartAICoreHost.bat` 和 `StartAICoreClient.bat`，使用相同的参数名、顺序和默认值。

Host 参数：

```text
-MGFNetRole Host -MGFNetPort 7000 -MGFNetRoomId 1
```

Client 参数：

```text
-MGFNetRole Client -MGFNetHost 127.0.0.1 -MGFNetPort 7000 -MGFNetRoomId 1 -MGFNetUin 10001
```

修改输入框中的地址、端口、房间号和 UIN 即可连接其他主机或房间。Client 的 UIN 不能为 `1`（主机本地玩家 ID），多开 Client 时应分别使用 `10001`、`10002` 等不同 UIN。启动器会阻止自己管理的实例重复监听同一 Host 端口，或以相同地址、端口、房间和 UIN 启动第二个 Client。

点击预设会用脚本默认值替换现有的 `-MGFNet*` 参数，保留其他自定义参数；**清除网络参数** 只移除这五个网络参数及其值。参数输入、命令预览、保存配置与实际进程启动使用同一份参数列表；按 Windows 命令行规则解析，不通过 shell 执行。

其他功能：

- **关闭 / 关闭全部**：结束本启动器创建的指定实例或全部实例及其子进程。
- **重新启动**：使用所选实例启动时的原始参数和调试端口。要使用编辑后的参数，请关闭旧实例再点击“启动新实例”。
- **等待 Lua 调试器连接**：追加 `-script-debug-wait-client`，默认启用。启动实例时会自动分配并追加独立的 `-lua-debug-port`，该端口无需手动输入。
- **命令预览**：显示所选 EXE 与 App 参数，可选中或复制。实例的调试端口在启动时分配，因此不出现在启动前的预览中。
- **运行实例**：显示角色、PID、网络端点、房间、Client UIN、调试端口和连接状态。
- **Attach 状态**：从 `.run/Lua.run.xml` 读取配置名和端口；两个状态图片分别显示 IDE 到 DAP/桥接、桥接到 App ScriptDebugger 的连接情况。
- **保存配置 / 窗口记忆**：保存程序、参数和控制台选项；关闭时保存正常窗口的大小和屏幕位置。

## 旧配置迁移

已移除旧的玩法参数单选列表、`Games.AppConfigs` 解析、`Scripts/Games/init.lua` 文件监听和 TopBattle 登录入口。启动器不再自动传入 `-MGFTopBattle` 或 `-TopBattleNetwork*`。

首次读取版本 1 的 `settings.local.json` 时会自动迁移到新的参数模型：移除旧固定玩法列表，保留调试等待开关、控制台选项及窗口位置；Host / Client 角色转换为 `-MGFNet*` 参数，旧默认端口 `19120` 改为 `7000`，其他自定义端口和 Client 地址保留，房间默认 `1`、UIN 默认 `10001`。旧登录角色转换为默认启动。仅旧默认 EXE `C:\MiniGame\Bin64\AIFramework_d.exe` 会替换为 AICore，用户自选路径保留。

保存后使用版本 2，不再另存一套网络字段，网络角色和实例信息直接从输入的参数中读取。版本 2 的自定义参数会原样保留。本地配置文件已被 Git 忽略。

## PyCharm / WebStorm 常驻调试

常驻调试服务已经集成到 App 调试启动器。双击 `run_launcher.bat` 打开 GUI 后，启动器会自动在后台启动常驻桥和 Node DAP，不需要再单独运行 `persistent_debug_server.py`。

默认 `127.0.0.1:4711` Attach 服务会在 App 尚未启动时先进入监听；可以先从 IDE Attach，再在 GUI 中启动实例。关闭或自然退出 App 只会让调试目标离线，不会关闭 Attach 服务或 IDE 会话。

Node DAP 的标准输出和错误输出由启动器持续接管并显示在运行日志中，避免无控制台的 `pythonw.exe` 启动方式使 DAP 在 IDE 接入时因无效输出句柄退出；`Attach Lua` 状态由 DAP 实际接受和关闭 IDE 连接的事件驱动。

启动器通过 `dap_adapter_compat.js` 延后 DAP `initialized` 事件，直到 `attach` 已经完成。这样 Rider/LSP4IJ 即使立即并发下发断点，也不会在 ScriptDebugger 尚未连接时触发 `ScriptDebugRuntime is not connected` 并导致 DAP 进程退出。

若启动器打开前已有 App 正在监听 ScriptDebugger `3382`，启动器会把它视为可连接的现有调试目标，仍正常启动桥接端口 `3383` 和 DAP 端口 `4711`。此时再从启动器创建新实例会自动使用下一组空闲端口，避免与现有目标冲突。

Lua 调试服务由 GUI 内置守护循环管理。Node DAP、常驻桥或服务线程异常退出后，会按 `1、2、4、8、10` 秒退避自动重新启动；关闭 GUI 时则正常停止，不会再次拉起。Node DAP 断开时，常驻桥会沿用现有的断点清理和 `continue` 逻辑，避免 App 停在断点处失去调试连接。

常驻链路为：

```text
PyCharm / WebStorm --DAP 4711--> LuauDebugAdapter --3383--> 常驻桥 --3382--> App
```

常驻桥会保留 IDE 与 Debug Adapter 的连接。App 退出时不会向 IDE 发送 `terminated`；新的 App 启动后，常驻桥会自动连接并在发送 `configurationDone` 前恢复缓存的断点。GUI 启动参数需保留：

```text
-script-debug-wait-client
```

首次使用时按以下顺序操作：

1. 停止 PyCharm / WebStorm 之前启动的旧 Debug Adapter，确保 DAP 端口 `4711` 空闲。
2. 双击 `run_launcher.bat`，等待界面显示“常驻调试：运行中”。
3. 在 PyCharm / WebStorm 中 Debug `Lua`；该配置使用 Attach 模式连接 `127.0.0.1:4711`，并将运行时 Lua 服务映射到 `Scripts` 目录。
4. 用本 GUI 任意启动、关闭或重新启动 App。IDE 调试会话无需停止。

PyCharm / Rider 的 EmmyLua 插件创建的是 `lua-line` 断点，而 LSP4IJ DAP 只直接处理 `dap-breakpoint`。启动器会自动查找直接打开 AIGamePlay 的 `.idea/workspace.xml`，也支持 Rider 解决方案的嵌套 `.idea/.idea.<Solution>/.idea/workspace.xml`，将 `Scripts/**/*.lua` 下启用的 `lua-line` 断点自动转换成运行时断点；同时监听 JetBrains `idea.log` 的实时增删事件，避免尚未写回 `workspace.xml` 的旧断点在游戏重启时被恢复。启用该同步后，IDE 中可见的断点是运行时的唯一权威来源，LSP4IJ 重发的历史 `dap-breakpoint` 不会再产生不可见或错位的断点。日志出现 `Using JetBrains workspace` 表示已定位当前工作区，出现 `Watching JetBrains breakpoint events` 表示实时增删监听已启用，出现 `Loaded N JetBrains Lua breakpoint(s)` 表示已读取断点，出现 `Applied JetBrains Lua breakpoints` 表示改动已同步到当前 App。

如果旧进程仍占用 `4711`，界面会显示“常驻调试：启动失败”。停止旧调试会话后，点击“重试服务”即可，无需重启 GUI。

GUI 运行日志出现 `AIFramework offline; waiting for restart` 表示正在等待 EXE；出现 `restored N breakpoint(s)` 表示新进程已连接且断点已恢复。

## 进程安全边界

启动器使用 Windows Job Object 管理进程树，只能关闭当前 GUI 实例亲自启动的进程，不会按名称查找或终止其他 App 实例。关闭启动器窗口时，它管理的 App、常驻调试桥和 Node DAP 也会被关闭。

## 测试

```powershell
python -m unittest discover -s .\tests -v
```
