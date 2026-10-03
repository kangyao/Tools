# App 调试启动器

用于输入启动参数并管理 `AICore_profile.exe` 等 App。默认程序路径是 `C:\MiniGame\Bin64\AICore_profile.exe`；可浏览选择其他 EXE。

## 使用

双击 [`run_launcher.bat`](run_launcher.bat)，或在本目录运行：

```powershell
python .\launcher.py
```

主界面只负责启动，配置的编辑和激活都在 **配置管理** 窗口中完成。详细设计见 [CONFIGURATION_PLAN.md](CONFIGURATION_PLAN.md)。

1. 点击 **配置管理**，新建或编辑 Host、Client 配置：填写配置名称、类型、程序路径、端口、房间和开发账号序号，Client 另有主机地址和“多开时自动分配不同账号”。点击 **保存** 后才会生效。
2. 在配置管理中为 Host、Client 各勾选一份 **激活**（列表中以 ✓ 标记）。每种类型只能激活一份，勾选后立即在主界面显示，无需点保存。
3. 主界面显示激活的 Host、Client 配置及参数摘要。设置 Client **数量** 后，点击 **启动 Host**、**启动 N 个 Client** 单独启动，或点击 **一键启动 Host + N 个 Client**：先启动 Host（已有相同配置的受管 Host 时直接复用），等待设定的间隔（默认 2 秒）后依次启动 N 个 Client。该间隔只是启动间隔，不代表已确认 Host 网络就绪。激活状态、数量和间隔都会被记住。

网络字段生成与 AIGamePlay `Tools/Start-NetDebug.ps1` 一致的参数：

```text
-MGFDevAccount 1 -MGFNetRole Host -MGFNetPort 7000 -MGFNetRoomId 1
-MGFDevAccount 2 -MGFNetRole Client -MGFNetHost 127.0.0.1 -MGFNetPort 7000 -MGFNetRoomId 1
```

`-MGFDevAccount N` 选 `AIGamePlay/.dev/account.json` 的第 N 个开发账号登录（Host 默认 1、Client 默认 2）。除云服外所有角色都要真实登录，联机身份取登录账号的 uin；旧的 `-MGFNetUin` 已被 AICore 移除，传入会判 `NetLaunchInvalid: uin`，启动器读取旧配置时自动丢弃，额外参数中填写会提示删除。

**额外参数** 用于填写其他 App 参数，含空格的值用双引号；按 Windows 命令行规则解析，不通过 shell 执行。额外参数中不能重复填写 `-MGFNet*` 网络参数与 `-MGFDevAccount`、`-script-debug-wait-client` 或 `-lua-debug-port`，应改用对应的表单字段或选项。命令预览与实际启动使用同一份参数列表。

配置管理中的 **启动命令** 可以直接编辑：能解析的修改会立即同步到表单字段（程序路径、类型、地址、端口、房间、开发账号、等待调试器、额外参数），解析失败时表单保持不变并提示原因，按 Esc 放弃无效的命令文字；配置名称、显示控制台、自动分配账号等命令中没有的设置保持不变。编辑结果同样需要点 **保存** 才生效。

配置管理规则：

- **新建 / 复制 / 另存为** 都会创建一份独立配置；配置名称必须唯一（不区分大小写）。
- **取消** 放弃当前未保存的修改；切换配置或关闭窗口时，若有未保存修改，会提示保存、放弃或继续编辑。
- **删除** 已激活的配置后，该类型变为未激活，已启动的实例不受影响。
- 修改已激活配置的类型后，它不再是原类型的激活配置。
- 当前有未保存修改时勾选“激活”，会先提示保存、放弃或继续编辑；新建的配置在保存时按勾选状态激活。

一键启动前会检查激活的 Client 是否能连接激活的 Host：地址须为本机（`127.0.0.1`、`localhost`、`::1`），端口和房间须与 Host 一致，否则列出不匹配的字段。Host 启动失败时不会启动 Client；Client 启动失败时 Host 继续由启动器管理，可单独重试 Client 或关闭 Host。

AICore 按开发账号加锁，同一台机器上每个进程必须用不同账号。启用自动分配时，Client 从配置的账号序号开始，跳过本启动器管理的所有实例（含 Host，不论房间）已占用的序号；实际序号只属于本次实例，保存的配置不会被递增。关闭自动分配时使用固定序号，与已运行实例冲突时给出错误，且 Client 数量只能为 1。多个 Client 中某个启动失败时停止后续启动，已启动的保留，并提示已启动的数量。

其他功能：

- **运行实例**：显示配置名称、角色、PID、网络端点、房间、实际开发账号序号、App 状态，以及调试端口、Attach 连接和调试服务状态；列表空间不足时可滚动。
- **关闭选中实例 / 关闭全部**：结束本启动器创建的指定实例或全部实例及其子进程。
- **重启选中实例**：使用该实例的启动快照（参数、实际开发账号、调试端口），不受之后的配置编辑影响。要使用修改后的配置，关闭旧实例后重新启动。
- **查看启动命令**：显示所选实例实际传递的完整命令，包括自动分配的 `-lua-debug-port`，可复制。
- **打开日志**（或双击实例行）：用系统关联程序打开该实例的日志文件。路径按 AICore `WinGameStart.cpp` 的规则推算，位于 EXE 所在目录：`-AICoreLogFile` 指定的文件名优先；其次 `-AICoreNetRole Dedicated` 为 `AICoreApp-server.log`、`Client` 为 `AICoreApp-client-<PID>.log`；再次 `-MGFDevAccount N` 为 `AICoreApp_devN.log`；否则 `AICoreApp.log`。文件尚不存在时提示路径，并可打开所在目录。注意同一开发账号的实例共用一个日志文件。
- **等待 Lua 调试器**：追加 `-script-debug-wait-client`，新建配置默认关闭。勾选后 App 会停在启动阶段，直到 IDE 通过 Attach Lua 连接后才继续运行。
- **Attach 状态**：从 `.run/Lua.run.xml` 读取配置名和端口；两个状态图片分别显示 IDE 到 DAP/桥接、桥接到 App ScriptDebugger 的连接情况。缺失调试组件时只提示一次，App 仍可正常启动。
- **窗口记忆**：保存激活配置、Client 数量、双端启动间隔和窗口位置。

## 配置存储与迁移

配置保存在本目录的 `settings.local.json`（版本 4，已被 Git 忽略），包含具名配置集合（旧存档里的 `uin` 字段读取时忽略、`auto_uin` 沿用为 `auto_dev_account`）、激活的 Host / Client 配置（`selected_host` / `selected_client`）、Client 数量（`client_count`）、双端启动间隔和窗口位置。配置用稳定标识关联，改名后激活状态保持不变。

首次读取旧版本配置时自动迁移为“默认 Host”和“默认 Client”：

- 版本 1 / 2（单份参数）：保留程序路径、有效网络参数、控制台和调试等待选项；原角色专属的额外参数只迁移到对应角色，单机参数同时保留到两端；缺失的另一端使用脚本默认值，并沿用原端口和房间，便于本机联调。
- 版本 3（Host、Client 两份表单）：分别迁移两份设置；启用了“跟随 Host”的 Client 改为连接本机，端口和房间与 Host 一致；原“自动递增 UIN”开关沿用为“自动分配开发账号”，保留启动间隔和窗口位置。
- 版本 1 的旧玩法参数单选列表、`-MGFTopBattle`、`-TopBattleNetwork*` 不再保留，也不再依赖 `Games.AppConfigs`；旧默认端口 `19120` 改为 `7000`，旧默认 EXE `C:\MiniGame\Bin64\AIFramework_d.exe` 替换为 AICore，用户自选路径保留。

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
