# App 调试启动器

用于输入启动参数并管理 `AICore_profile.exe` 等 App。默认程序路径是 `C:\MiniGame\Bin64\AICore_profile.exe`；可浏览选择其他 EXE。

## 使用

双击 [`DebugLauncher.bat`](DebugLauncher.bat)，或在本目录运行：

```powershell
python .\launcher.py
```

主界面只负责启动，配置的编辑和激活都在 **配置管理** 窗口中完成。详细设计见 [CONFIGURATION_PLAN.md](CONFIGURATION_PLAN.md)。

1. 主界面点击 **启动单机** 即可独立运行；点击 **启动联机** 自动启动服务端与设定数量的 Client。仍可分别启动 Host / Client。
2. 在 **配置管理** 中分别设置单机、Host、Client 的参数，每种类型各激活一份配置（✓）。修改后点击 **保存** 生效。
3. 通用字段包括程序路径、目标工程、角色选择、账号、控制台、调试等待和额外参数。单机账号 0 表示沿用程序默认账号。联机另有服务端模式、地址、端口、房间、测试模式、票据与服务身份；单机隐藏联机字段。
4. 联机先启动或复用相同配置的 Host，确认其 PID 监听 UDP 端口后，再按间隔启动 Client。默认间隔 2 秒，状态与数量会保存；自动平铺在进程创建前规划窗口位置。

默认服务端模式为 `CloudServer`，与 AIGamePlay `StartGame.bat` 一致；配置类型 Host 表示服务端配置，运行实例中 DS 显示为 **DS / mock**。需要本地主机玩家时，把两端服务端模式都改为 `Host`。

**目标工程** 默认 `C:\MiniGame\Source\AIGamePlay`，可输入路径或浏览选择目录；启动时生成 `-AICoreRoot <目录>`，留空使用程序默认工程。旧配置额外参数中的 `-AICoreRoot` 自动迁入此字段；编辑启动命令也会同步此字段。

配置管理中的 **跳过角色选择（默认 Avatar）** 默认开启，与 AIGamePlay `GameLauncher.bat` 一致，自动追加 `-MGFInitialCharacter avatar_default`。取消勾选后不再自动追加，可手动选择角色；开关随每份配置保存，旧配置缺少该字段时默认开启。额外参数中显式指定的 `-MGFInitialCharacter`（含 `=值` 写法）优先保留，不重复追加；要恢复手动选择，也需删除显式参数。

工程使用 `dev-runtime.json` 格式 2 时，启动器按 `sdkRoot` 补充 `-AICoreFrameworkRoot <SDK>/AICoreScripts`，预览与实际命令一致。显式框架参数保留；SDK 缺少入口或工程残留旧 `AICoreScripts/` 目录时，启动前报告错误。Host 端口被其他进程占用时也在启动前提示，不关闭外部进程。

默认网络参数：

```text
-MGFNetRole CloudServer -MGFNetPort 7000 -MGFNetRoomId 1 -AICoreTestMode -AICoreTestServiceIdentity debug-launcher
-MGFDevAccount 2 -MGFNetRole Client -MGFNetHost 127.0.0.1 -MGFNetPort 7000 -MGFNetRoomId 1 -MGFNetServer CloudServer -AICoreTestMode
```

本地联调默认开启测试模式；票据留空由宿主提供默认测试票据。自定义票据最多 32 字节，两端须一致；CloudServer 的测试服务身份最多 128 字节。单机不携带这些参数，也不等待服务端。

`-MGFDevAccount N` 选 `AIGamePlay/.project/account.json` 的第 N 个开发账号登录（Listen Host 默认 1、Client 默认 2；DS 为 mock，不占开发账号）。除云服外所有角色都要真实登录，联机身份取登录账号的 uin；旧的 `-MGFNetUin` 已被 AICore 移除，传入会判 `NetLaunchInvalid: uin`，启动器读取旧配置时自动丢弃，额外参数中填写会提示删除。

**额外参数** 用于填写其他 App 参数，含空格的值用双引号；按 Windows 命令行规则解析，不通过 shell 执行。额外参数中不能重复填写 `-MGFNet*` 网络参数与 `-MGFDevAccount`、`-script-debug-wait-client` 或 `-lua-debug-port`，应改用对应的表单字段或选项。命令预览与实际启动使用同一份参数列表。

配置管理中的 **启动命令** 可以直接编辑：能解析的修改会立即同步到表单字段（程序路径、类型、地址、端口、房间、开发账号、等待调试器、额外参数），解析失败时表单保持不变并提示原因，按 Esc 放弃无效的命令文字；配置名称、显示控制台、自动分配账号等命令中没有的设置保持不变。编辑结果同样需要点 **保存** 才生效。

配置管理规则：

- **新建 / 复制 / 另存为** 都会创建一份独立配置；配置名称必须唯一（不区分大小写）。
- **保存** 校验并保存配置，成功后关闭配置管理窗口；保存失败时保留窗口和输入，便于修改后重试。
- **取消** 放弃当前未保存的修改并关闭配置管理窗口；切换配置或点击窗口右上角关闭时，若有未保存修改，会提示保存、放弃或继续编辑。
- **删除** 已激活的配置后，该类型变为未激活，已启动的实例不受影响。
- 修改已激活配置的类型后，它不再是原类型的激活配置。
- 当前有未保存修改时勾选“激活”，会先提示保存、放弃或继续编辑；新建的配置在保存时按勾选状态激活。

联机启动前检查地址为本机，端口、房间、服务端模式、测试模式和票据两端一致。Host 失败时不启动 Client；Client 失败时 Host 保留，可单独重试或关闭。

AICore 按开发账号加锁，同一台机器上每个进程必须用不同账号。启用自动分配时，Client 从配置的账号序号开始，跳过本启动器管理的所有实例（含 Host，不论房间）已占用的序号，并用与 StartGame 相同的全局 dev<N>.lock 检查避开其他启动器占用的账号；实际序号只属于本次实例，保存的配置不会被递增。关闭自动分配时使用固定序号，与已运行实例冲突时给出错误，且 Client 数量只能为 1。多个 Client 中某个启动失败时停止后续启动，已启动的保留，并提示已启动的数量。

其他功能：

- **DS 自动收尾**：同一受管 DS 的最后一个本机 Client 退出后自动正常停服；多开时首个 Client 退出不会关闭 DS。单独启动且尚无 Client 的 DS 保留，供后续连接。另一房间或其他启动器创建的 DS 不受影响。
- **DS 窗口**：DS 没有游戏窗口，不参与平铺；取消“显示控制台”时追加 `-AICoreNoConsole`。

- **批量启动**：设置 Client 数量后使用“启动 N 个 Client”或“一键启动 Host + N 个 Client”；完成后自动选中本次启动的全部实例（一键启动包含复用的 Host），便于接着批量关闭或重启。
- **运行实例**：显示配置名称、角色、PID、网络端点、房间、实际开发账号序号、App 状态，以及调试端口、Attach 连接和调试服务状态；列表空间不足时可滚动。按 Ctrl / Shift 多选，或用“全选”按钮、列表中的 Ctrl+A 选择全部；状态刷新保留多选与焦点。
- **关闭选中实例 / 批量关闭 / 关闭全部**：多选时按钮显示选中数量，只关闭本次选中的实例；“关闭全部”处理所有受管实例。各实例的关闭请求并发发起，不再等前一个退出后才关闭下一个；每个实例仍优先通过已验证 PID / instance_id / 工程的 AICore HTTP 正常停止，不支持或超时才结束受管 Job 进程树。等待全部请求完成后汇总成功数量和失败原因，未关闭的实例继续受管。各进程实际退出时间可能不同。
- **重启选中实例 / 批量重启**：支持多选，分别使用每个实例的启动快照（参数、实际开发账号、调试端口），不受之后的配置编辑影响；某个实例失败后继续处理其余实例并汇总结果，完成后统一平铺一次。要使用修改后的配置，关闭旧实例后重新启动。查看命令、打开日志需单选实例。
- **查看启动命令**：显示所选实例实际传递的完整命令，包括自动分配的 `-lua-debug-port`，可复制。
- **打开日志**（或双击实例行）：用系统关联程序打开该实例的日志文件。路径按 AICore `WinGameStart.cpp` 的规则推算，位于 EXE 所在目录：`-AICoreLogFile` 指定的文件名优先；其次 `-AICoreNetRole Dedicated` 为 `AICoreApp-server.log`、`Client` 为 `AICoreApp-client-<PID>.log`；再次 `-MGFDevAccount N` 为 `AICoreApp_devN.log`；否则 `AICoreApp.log`。文件尚不存在时提示路径，并可打开所在目录。注意同一开发账号的实例共用一个日志文件。
- **等待 Lua 调试器**：追加 `-script-debug-wait-client`，新建配置默认关闭。勾选后 App 会停在启动阶段，直到 IDE 通过 Attach Lua 连接后才继续运行。
- **Attach 状态**：从 `.run/Lua.run.xml` 读取配置名和端口；两个状态图片分别显示 IDE 到 DAP/桥接、桥接到 App ScriptDebugger 的连接情况。缺失调试组件时只提示一次，App 仍可正常启动。
- **窗口启动位置**：保存启动器窗口尺寸；每次打开时忽略上次所在显示器，并在主显示器中央启动。
- **自动平铺 App 窗口**：主界面的开关默认开启。一键启动先按主显示器可用工作区预留 Host / Client 的位置和尺寸，创建进程时通过 Windows 启动信息传入；随后分别监听各实例的窗口（包括尚未显示的窗口），尽早定位并校正启动期间的自动居中，不再等全部 Client 启动后统一搬动。已有窗口在启动前一起重新安排，DS 不占游戏窗口的位置。新窗口的尺寸比例读取 EXE 目录的 `devices/win.xml`，缺失或无效时按 `1280×720` 规划；旧窗口沿用实际尺寸比例。单独启动、批量 Client 启动和重启仍在完成后统一平铺。窗口按 Host 在前、Client 按实例顺序，从工作区左上角逐行紧挨排列，保留比例且只缩小不放大，控制台窗口不参与平铺。预规划失败会记录日志并回退到启动后平铺；窗口延迟创建或无法定位时只写入日志，不影响进程运行。关闭开关后不预设位置，也不移动窗口。

## 配置存储与迁移

配置保存在本目录的 `settings.local.json`（版本 7，Git 忽略）。每份配置独立保存参数，三种模式独立保存激活状态；保留 Client 数量、启动间隔、窗口尺寸和自动平铺设置。

版本 1～6 自动补充“默认单机”，优先继承已激活 Client 的程序与工程路径，不复制联机参数或改变原有激活状态。联机旧配置缺少的新字段默认使用本地测试模式和测试服务身份。版本 7 删除单机配置后不会再次自动补回。

首次读取旧版本配置时自动迁移为“默认 Host”和“默认 Client”：

- 版本 1 / 2（单份参数）：保留程序路径、有效网络参数、控制台和调试等待选项；原角色专属的额外参数只迁移到对应角色，单机参数同时保留到两端；缺失的另一端使用脚本默认值，并沿用原端口和房间，便于本机联调。
- 版本 3（Host、Client 两份表单）：分别迁移两份设置；启用了“跟随 Host”的 Client 改为连接本机，端口和房间与 Host 一致；原“自动递增 UIN”开关沿用为“自动分配开发账号”，保留启动间隔和窗口位置。
- 版本 1 的旧玩法参数单选列表、`-MGFTopBattle`、`-TopBattleNetwork*` 不再保留，也不再依赖 `Games.AppConfigs`；旧默认端口 `19120` 改为 `7000`，旧默认 EXE `C:\MiniGame\Bin64\AIFramework_d.exe` 替换为 AICore，用户自选路径保留。

## PyCharm / WebStorm 常驻调试

常驻调试服务已经集成到 App 调试启动器。双击 `DebugLauncher.bat` 打开 GUI 后，启动器会自动在后台启动常驻桥和 Node DAP，不需要再单独运行 `persistent_debug_server.py`。

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
2. 双击 `DebugLauncher.bat`，等待界面显示“常驻调试：运行中”。
3. 在 PyCharm / WebStorm 中 Debug `Lua`；该配置使用 Attach 模式连接 `127.0.0.1:4711`，并将运行时 Lua 服务映射到 `Scripts` 目录。
4. 用本 GUI 任意启动、关闭或重新启动 App。IDE 调试会话无需停止。

PyCharm / Rider 的 EmmyLua 插件创建的是 `lua-line` 断点，而 LSP4IJ DAP 只直接处理 `dap-breakpoint`。启动器会自动查找直接打开 AIGamePlay 的 `.idea/workspace.xml`，也支持 Rider 解决方案的嵌套 `.idea/.idea.<Solution>/.idea/workspace.xml`，将 `Scripts/**/*.lua` 下启用的 `lua-line` 断点自动转换成运行时断点；同时监听 JetBrains `idea.log` 的实时增删事件，避免尚未写回 `workspace.xml` 的旧断点在游戏重启时被恢复。启用该同步后，IDE 中可见的断点是运行时的唯一权威来源，LSP4IJ 重发的历史 `dap-breakpoint` 不会再产生不可见或错位的断点。日志出现 `Using JetBrains workspace` 表示已定位当前工作区，出现 `Watching JetBrains breakpoint events` 表示实时增删监听已启用，出现 `Loaded N JetBrains Lua breakpoint(s)` 表示已读取断点，出现 `Applied JetBrains Lua breakpoints` 表示改动已同步到当前 App。

如果旧进程仍占用 `4711`，界面会显示“常驻调试：启动失败”。停止旧调试会话后，点击“重试服务”即可，无需重启 GUI。

GUI 运行日志出现 `AIFramework offline; waiting for restart` 表示正在等待 EXE；出现 `restored N breakpoint(s)` 表示新进程已连接且断点已恢复。

## 进程安全边界

启动器使用 Windows Job Object 管理进程树，只能关闭当前 GUI 实例亲自启动的进程，不会按名称查找或终止其他 App 实例。关闭启动器窗口时，它管理的 App、常驻调试桥和 Node DAP 也会被关闭。

## 验证记录

2026-10-20：205 项回归中 204 项 `pass`，1 项仍因缺少 `.run/Lua.run.xml` 报错。通过主界面按钮启动真实 AICore：单机登录成功；1 DS + 1 Client 的会话和房间均 Ready，Hello 验票通过，服务端记录玩家加入。测试进程已正常关闭。

2026-10-15（一键启动预设位置）：完整 Python 回归共 183 项，182 项通过，另 1 项仍因缺少下述 IDE 配置文件报错。覆盖整批位置预规划、Listen Host / DS 区别、禁用开关、规划失败回退、启动中逐个定位及部分启动失败；原生子进程验证了 Windows 启动坐标和尺寸，真实 Win32 测试窗口验证了隐藏阶段定位及显示后的坐标校正。未启动真实 AICore 游戏实例。

2026-10-15（并发关闭修复）：完整 Python 回归共 171 项，170 项通过；另 1 项仍因缺少下述 IDE 配置文件报错。核心与界面测试共 114 项通过，新增阻塞退出测试验证所有目标都先收到关闭请求、再统一放行退出，覆盖“关闭全部”和“关闭选中”按钮；并验证失败实例及未选中实例继续受管。使用模拟受管进程验证，未关闭真实游戏实例。

2026-10-15：完整 Python 回归共 167 项，166 项通过（含 42 项界面测试）；新增覆盖批量选中与刷新保留、仅关闭选中实例、部分关闭失败、批量重启快照、部分重启失败及关闭全部的失败保留。另 1 项既有 IDE 配置测试因缺少 `D:\git\skills\.run\Lua.run.xml` 报错。批量操作使用模拟受管进程验证，未启动真实游戏实例。

1. `pass`（2026-10-14）：160 项 Python 回归测试；DS 参数、旧配置迁移、Listen Host、错 PID / 工程 / 实例拒绝与自动收尾均覆盖。
2. `pass / RealTransport / Dedicated`：GUI 分三轮各启动 1 DS + 2 Client；HTTP 回读 Dedicated、大厅 Running、2 个认证玩家。
   关闭首个游戏窗口时 DS 保留；最后一个退出后 DS 自动关闭；直接关闭 GUI 也正常收尾。
   隐藏 DS 与显示控制台两种模式均验证；所有测试 App 退出码为 0，独立 DS 保持运行；证据在 AIGamePlay 的 `.local/debug-launcher-ds-evidence.json`。
3. `blocked`：既有 IDE 配置测试缺少 `D:\git\skills\.run\Lua.run.xml`，与本次改动无关。
4. 当前桌面旧启动器来自 W 盘，D / W 是独立副本；使用本次修改需关闭旧窗口，重新双击 D 盘 `DebugLauncher.bat`。

## 测试

```powershell
python -m unittest discover -s .\tests -v
```
