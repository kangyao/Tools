# MiniGame 仓库管理工具设计

日期：2026-09-30。交付范围：Python 桌面程序、使用文档、回归测试，以及保留的设计阶段交互示意。

用户已选择：仓库列表可编辑，支持保存多套根目录与分支方案。初始配置以 E:\pull - E.bat 的 8 个仓库为准。

**目标与日常流程**

面向 Windows 上需要初始化、切换和更新 MiniGame 多仓库环境的开发者。选择方案后，即可看清各仓库的位置、当前分支、目标分支、工作区状态与预计操作；执行过程中能定位失败仓库，处理后单独重试。

主要流程：选择方案 → 检查状态 → 查看计划 → 同步所选 → 查看结果。直接点击“同步所选”也会先检查，无须每次手动走两遍。

当前版本支持完整克隆、获取指定分支、切换本地分支、快进更新、单仓库重试、构建环境准备、文件级 Diff 和备份后 Discard、并行状态检查、Windows 原生右键菜单，以及按仓库忽略修改提醒和不备份的强制更新。内置流程不加入提交、推送、图形化解决冲突、自动定时更新；原生菜单的外部扩展保留其自身功能。

**技术选择**

| 方案 | 适合之处 | 代价与选择 |
|---|---|---|
| Python + PySide6 + 系统 Git | 多列树形仓库列表、可调整分栏、异步进程输出和多套配置管理 | 推荐；发布时需要打包 Qt 运行库 |
| Python + Tkinter/ttk + 系统 Git | 依赖少，适合简单列表和日志窗口 | 可行；复杂表格、详情编辑和持续演进需要更多界面组装 |

用 Git CLI 执行实际操作，复用当前机器的 SSH、Git Credential Manager 和 Git 配置。Git 操作以程序路径与参数数组启动，普通 Git 调用不拼接 shell 命令。

PySide6 的 QTreeWidget 表达层级仓库；QThread 中的 subprocess.Popen 执行 Git 并持续读取输出，以 Qt 信号向界面传递状态与日志。独立状态检查在该后台任务内使用最多 8 个线程。Git 操作不阻塞 UI 线程，控件由主线程更新；Git 核心不依赖 Qt。Windows Shell 菜单通过 ctypes 调用系统 COM 接口，在窗口主线程使用原生菜单消息循环。

参考：[QTreeView](https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QTreeView.html)、[QProcess](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QProcess.html)、[Tkinter](https://docs.python.org/3/library/tkinter.html)。

**主窗口**

主窗口初始尺寸为 1420 × 900，可调整大小。

- 顶部：方案下拉框、管理方案、根目录、游戏分支和引擎分支。目标分支可输入，也可按需读取远端列表。修改后显示未保存标记。
- 中部：可勾选的树形仓库列表。主要列为仓库、当前分支、目标分支、工作区、计划动作和执行状态；窄窗口优先保留仓库、状态与动作，其余放进详情。
- 右侧：完整路径、远端地址、完整分支名、失败原因和下一步处理入口。可编辑配置或单独检查、同步该仓库。
- 底部：执行计划与实时日志两个页签。可查看全部任务或选中仓库，支持复制、导出。显示实际阶段进度；无法确定百分比时使用不定进度，不将对象接收百分比当成整个任务的百分比。
- 操作栏：检查状态、同步所选、重试失败、停止队列、准备构建环境。执行期间冻结相关方案编辑。

配置管理使用独立对话框：左侧方案列表，右侧根目录、分支组和仓库表格。支持新建、复制、重命名、删除方案，以及增删改仓库。删除条目仅删除配置记录。

仓库字段：显示名称、相对路径、远端 URL、分支组或固定分支、排序参考、是否默认勾选、环境必需，以及忽略修改提醒和强制更新选项。排序参考兼容旧 depends_on 字段，仅影响列表展示，不作为同步前置条件。

**初始仓库配置**

| 仓库 | 相对根目录路径 | 分支组 | 目录父级（展示） |
|---|---|---|---|
| MiniGame | . | game | 无 |
| AssetRuntime | AssetRuntime | game | MiniGame |
| CommonResource | AssetRuntime/CommonResource | game | AssetRuntime |
| Source | Source | game | MiniGame |
| SandboxEngine | Source/SandboxEngine | game | Source |
| AIGamePlay | Source/AIGamePlay | game | Source |
| RainbowEngine | Source/Core/RainbowEngine | engine | Source |
| Editor | Source/Editor | engine | Source |

初始 game 为 miniw/release/v1.55.50_ai_framework_V2，engine 为 Engine/Release_v6.2.9_ai_framework_V2。远端沿用当前批处理中的 SSH 地址。

现有 Tools/AutoPull/AutoPull.json 把 Editor 放在 Source/Core/RainbowEngine/Editor，与当前批处理不同。默认模板使用当前批处理的 Source/Editor，配置编辑器允许修改，不能混用两个来源。

**仓库识别与计划生成**

必须验证目标目录自身是工作树根目录，不能仅检查 is-inside-work-tree。Git 会向父目录寻找仓库，正是当前空 AssetRuntime 目录容易被误判的原因。

使用 rev-parse --show-toplevel 得到的根路径，与目标目录的规范化真实路径比较；接受合法 .git 目录或 gitfile/worktree，拒绝把父仓库识别为当前条目的仓库。无 HEAD 的仓库标为初始化不完整。

| 检查结果 | 显示状态 | 执行规则 |
|---|---|---|
| 目录不存在 | 未克隆 | 验证目标分支后克隆 |
| 目录为空，包含隐藏项检查 | 空目录 | 可以克隆，不需要删除目录 |
| 非空目录，没有自己的有效仓库 | 目录被占用 | 标为待处理，展示路径和原因 |
| 完整仓库，目标分支一致且无更新 | 已是最新 | 记录完成，无须改工作区 |
| 目标本地分支存在且可快进 | 待更新 | 获取目标分支后切换并快进 |
| 目标分支只存在于远端 | 待切换 | 创建对应本地跟踪分支 |
| 本地分支只领先远端 | 本地领先 N 个提交 | 保留本地提交，不称为与远端一致 |
| 本地与远端分叉 | 分支已分叉 | 标为待处理，展示两侧提交数量 |
| 未提交修改、冲突或正在 rebase/merge | 工作区待处理 | 暂停该仓库，其他所选仓库继续 |
| 远端分支不存在、认证失败、连接失败 | 检查失败 | 分别给出原因，不统一伪装成分支不存在 |
| 父仓库执行失败或待处理 | 各仓库自身状态 | 选中的子仓库仍独立检查和同步 |

初次启动先展示本地状态，详情区标记检查时间以及本次使用的是本地缓存还是刚获取的远端信息。点击检查或同步时，fetch 更新选中现有仓库的目标远端跟踪引用，再计算提交关系；未克隆仓库用 ls-remote 验证目标分支。远端检查不会切换分支或修改工作区文件。

origin 与配置不一致时展示两个地址并标为待处理，提供编辑配置或“采用实际 origin”的操作；后一操作只更新配置。更换仓库实际 origin 留给外部 Git 工具。

检查工作区时，仅排除已验证为独立仓库、与配置路径完全对应的未跟踪子仓库目录；父仓库已跟踪的文件变化仍然计入。不会写入用户的 ignore 配置。

如果父仓库目标提交开始跟踪子仓库占用的路径，或将子仓库任一祖先变成普通文件、符号链接或 gitlink，先报告布局冲突。同时检查目标远端提交和已有目标本地分支，按 Windows 大小写规则匹配路径；切换与快进命令使用 --no-overwrite-ignore 保护忽略文件。真正的 Git submodule 在克隆前也检查父 index 的 gitlink；第一版不通过递归更新或独立切换分支改变父仓库要求的指针。

参考：[工作树根目录识别](https://git-scm.com/docs/git-rev-parse)、[克隆到现有空目录](https://git-scm.com/docs/git-clone)。

**执行与失败恢复**

1. 加载方案，校验路径、仓库 ID、分支名、重复目标和展示排序参考。相对路径不得逃出根目录；比较时规范化 Windows 路径、大小写和目录链接。排序参考与目录父级不能形成环。
2. 目录层级用于树形展示和路径保护，不作为同步前置条件。克隆、切分支、快进更新按配置列表逐项执行；独立状态检查允许并行，含不同仓库的远端 fetch。Discard 使用独立流程。
3. 同步队列严格使用本次明确选择，不加入、勾选或更新未选择的父仓库，也不检查其同步就绪状态。克隆子仓库可创建必要的父目录，但不自动克隆父仓库。
4. 执行前重新核对工作区和目标引用。以配置目录内的系统文件锁串行工具任务批次，覆盖使用该配置目录的跨方案和多实例；一个检查批次内部允许并行。原生菜单显示及命令调用期间同样持锁；其他 Git 客户端及不同配置目录不受此锁控制。
5. 克隆指定目标分支并使用 single-branch，默认完整历史，与当前脚本一致。已有仓库只获取需要的分支、切换并执行 fast-forward-only 更新。
6. 一个仓库失败或待处理后，后续所选仓库继续，包括它的子仓库。每项只根据自身检查和执行结果统计成功、待处理、失败、未执行、取消。
7. 重试只针对上次失败或待处理条目，从真实目录状态重新生成计划，不追加父仓库，不把上一次步骤编号当成当前事实。
8. “停止队列”停止派发新仓库，已开始的仓库完成后停止。长时间网络操作可中断；共享取消事件通知本批次全部活动 runner，进程管理覆盖 Git 及其 SSH 子进程，重新检查残留状态后才允许重试。
9. 克隆中断留下的非空目录或不完整仓库必须可见，不承诺自动续传；保留内容，提供更换目标路径或人工处理后的重新检查。

不自动执行 stash、reset --hard、clean、变基或创建合并提交。默认未提交改动和分叉直接进入待处理状态；明确开启强制更新后按下述规则处理未提交普通文件，分叉仍然阻止。

**仓库提醒与强制更新策略**

RepoSpec 增加 ignore_changes、force_update 两个布尔字段，默认 false，保持 schema_version=1 的旧配置兼容。方案表在仓库名称后展示两列复选框。忽略提醒只在 UI 中调整 dirty 状态的文字和颜色，省略自动展开的修改列表，不修改 Snapshot 的真实状态、Git 配置或文件列表服务；其他错误照常显示。

强制模式下 Inspector 继续读取远端、提交关系与目录布局；通过这些检查后，将有普通文件改动的可执行动作标为 force_update。检查仅生成计划，实际执行前再次检查状态。同步队列只包含明确选择的仓库，实际丢弃入口仍校验本次选择。UI 不自动勾选其他仓库，失败重试也仅让仍已勾选的强制仓库进入明确选择范围。

ForceUpdate 在 RepoService 的现有运行锁内执行，复用 ChangesService 的完整文件快照、路径和指纹校验。先拒绝其他 worktree 占用目标分支、未完成操作、子模块、不可丢弃条目与 ignored 恢复路径，再使用固定 HEAD 和 NUL literal pathspec 恢复所列普通文件，并仅 unlink 重新核对过的普通未跟踪文件。它不创建备份、stash、恢复分支或补丁，不递归删除目录；明确配置且验证为独立仓库的未跟踪子目录保留。恢复后重新检查工作区，再沿用原有切分支/快进更新。本地提交保留，分叉停止；错误和中断报告实际状态，不承诺事务回滚。手动 Discard 的强制备份流程独立保留。

**并行状态检查**

启动本地扫描和显式“检查状态”由 ParallelChecks 调度。按配置列表顺序取待查仓库，最多维持 8 个已派发任务，完成一个补充一个；不预先把全部仓库塞入执行器队列。每项拥有独立 GitClient、ProcessRunner 和固定仓库 ID 的日志回调，避免多个任务串用当前仓库字段。完成时立即发送 snapshot 和 completed/total/active 进度，最终结果按配置顺序整理。

远端检查会写 Git 引用，因此先读取规范化的 git-common-dir；同批次中共享该目录的 linked worktree 使用同一把锁，其远端检查依次执行。本地扫描不需要此锁。停止只影响后续派发；取消同时传入全部 runner，等待共享锁的任务也能响应取消。单仓库预期错误转换为该仓库快照，其他仓库继续。

**Windows 原生菜单**

仓库树右键命中某一行后，使用该行的实际目录构造 Shell 文件夹背景菜单；不改动复选框。通过 IContextMenu 和可用的 IContextMenu2/3 转发菜单消息，保留系统及已安装 Shell 扩展的实际菜单项。ctypes 适配模块与 Qt 坐标转换模块分离，不增加 pywin32 依赖。坐标由 Qt 逻辑像素转为窗口客户区物理像素，再用 ClientToScreen 转为屏幕坐标。

菜单打开时禁用其他任务入口并持有配置目录运行锁。取消菜单不触发扫描；成功调用菜单命令后读取全部仓库的本地状态。外部程序可以在菜单返回后继续运行，用户需在其完成后重新检查。外部菜单命令不经过工具的 Discard 备份流程。

菜单关闭时在 finally 中释放子类回调、COM 接口、PIDL 和原生菜单句柄。若菜单消息循环期间请求关闭窗口，先记录关闭请求，等菜单返回并释放资源后关闭，不启动新的扫描线程。

**文件改动和显式 Discard**

主窗口的仓库详情提供“文件改动 / Discard”，双击仓库也可打开。独立对话框展示 Git porcelain v2 -z 文件状态，暂存区和工作区分栏；文件默认不勾选。Diff 使用 literal pathspec，分别预览 HEAD → index 与 index → worktree，并关闭 external diff/textconv。列表和预览都在后台线程执行。

Discard 必须由用户勾选文件并在确认框核对后触发。已跟踪文件恢复到确认时的固定 HEAD，同时恢复 index 和 worktree；未跟踪普通文件只删除明确选中的路径。rename 包括原路径与目标路径；copy 仅包括目标路径。子模块 gitlink、嵌套仓库、目录、符号链接、junction/reparse path、合并冲突和未完成 Git 操作均不可作为普通文件丢弃。子模块自身工作树可处理普通文件，保留其 HEAD。

执行流程：重新扫描并对比用户看到的快照 → 逐文件校验规范路径与内容 → 在仓库之外备份原始工作区字节及完整 binary staged patch → 写入清单和恢复说明 → 再次检查 HEAD/index/文件指纹 → 恢复/删除所选文件 → 检查实际结果 → 更新清单与运行记录 → 刷新 UI。工作区内容使用 SHA-256 校验；补丁由 Git 直接写文件，不受控制台输出截断或日志脱敏影响。批量 restore 从 NUL 分隔的路径文件读取 literal pathspec，并明确 --no-recurse-submodules。

备份失败或校验失效时禁止开始修改。开始修改后发生错误，报告部分失败并保留备份，不承诺跨文件事务回滚。用户可以打开备份目录按 RECOVERY.md 恢复；本版不提供自动恢复。配置目录锁仅协调工具实例，无法锁住其他 Git 客户端，因此在备份前后检查状态仍有必要。

参考：[Git restore 路径与递归规则](https://git-scm.com/docs/git-restore)、[Git status porcelain v2](https://git-scm.com/docs/git-status)、[Git diff binary/output](https://git-scm.com/docs/git-diff)。

**准备构建环境**

把当前脚本末尾的 Win_Setup 提升为独立任务。按钮明确叫“准备构建环境”。

默认手动触发；可按方案勾选“所需仓库全部就绪后自动准备”。需要检查全部必需仓库，不能因为本次只选中的一个仓库成功，就认为整个工程可用。

原 Win_Setup.bat 从 SetupScript 执行 pushd ..，实际在工程根目录启动 Python。程序直接解压 Python39 并以工程根目录为 cwd 执行 Tools/Setup/SetupWin.py，保留解压及 Python 步骤的实际退出码。准备失败时保留 Git 同步成功结果，可以单独重试准备步骤。

**模块边界**

| 模块 | 责任 | 主要输出 |
|---|---|---|
| ui | 主窗口、配置编辑、仓库详情、计划与日志 | 用户操作意图 |
| models.py / profiles.py | 配置读写、版本、路径与展示排序校验 | Profile、RepoSpec、ProfileDocument |
| git_ops.py | 仓库识别、工作区、远端、提交关系与计划动作 | Snapshot |
| checks.py | 有界并行检查、每仓库日志、共享 Git 目录互斥与检查进度 | Snapshot、check_progress 事件 |
| changes.py | 文件改动快照、Diff、路径保护、备份与显式 Discard | ChangeSnapshot、DiscardResult |
| force_update.py | 明确启用后的不备份文件清理、ignored/嵌套仓库保护、更新前核对 | 已丢弃项数或失败原因 |
| windows_shell_menu.py / ui/native_menu.py | Windows Shell 菜单、消息转发、资源释放与 Qt 坐标适配 | 是否调用了菜单命令 |
| process.py | Popen、输出解码、退出码、超时与进程树中断 | ProcessResult |
| service.py | 所选仓库独立调度、互斥、重新检查与执行 | RepoResult、结构化事件 |
| setup.py | 环境准备及实际退出码 | RepoResult |
| storage.py | 运行记录、日志、脱敏与系统文件锁 | runs、run.lock |

数据流：配置与选择 → inspector → planner → coordinator → runner → 结构化事件 → UI 与日志。

数据类区分静态配置、检测快照、执行计划和结果。界面不解析中文状态文本决定 Git 操作，runner 不直接更新控件。

实现使用 QMainWindow + QTreeWidget + QSplitter + QPlainTextEdit；方案编辑器使用 QDialog + QTableWidget。

**配置与持久化**

工程固定放在 D:\git\Tools\MiniGameRepoTool，文档放在该工程的 docs 目录。工具独立于所管理的 MiniGame 仓库，从空磁盘初始化工程时也能启动。配置和日志默认放在 %LOCALAPPDATA%\MiniGameRepoTool。配置采用 JSON 原子替换写入，并保留上一版备份。

~~~json
{
  "schema_version": 1,
  "active_profile_id": "minigame-e",
  "profiles": [{
    "id": "minigame-e",
    "name": "MiniGame 开发",
    "root": "E:/MiniGame",
    "branch_groups": {
      "game": "miniw/release/v1.55.50_ai_framework_V2",
      "engine": "Engine/Release_v6.2.9_ai_framework_V2"
    },
    "repositories": [
      {
        "id": "main",
        "name": "MiniGame",
        "path": ".",
        "remote": "git@client-gitlab.miniworldplus.com:miniwan/MiniGame.git",
        "branch_group": "game",
        "enabled": true,
        "ignore_changes": false,
        "force_update": false
      },
      {
        "id": "assets",
        "name": "AssetRuntime",
        "path": "AssetRuntime",
        "remote": "git@client-gitlab.miniworldplus.com:miniwan/assetruntimenew.git",
        "branch_group": "game",
        "enabled": true
      }
    ],
    "setup": {
      "auto_run": false,
      "required_repositories": ["main", "assets"]
    }
  }]
}
~~~

上述 JSON 是两仓库最小格式示例；内置方案实际包含前表全部 8 个仓库，并将这 8 个仓库列为环境准备的必需项。分支使用 branch_group 或固定 branch 二选一，不使用任意字符串模板。depends_on 保留为列表排序参考；目录父级用于树形展示，二者均不参与同步等待或自动扩展选择。

配置可导入导出。配置保存地址，不保存密码、令牌或私钥；拒绝包含用户信息的 HTTP(S) URL；界面日志、导出日志和结构化运行记录均对 URL 凭据部分脱敏。完整 8 仓库配置见 ../examples/profiles.json。

**验收重点**

- 主仓库内部的空 AssetRuntime 能进入克隆计划，不会误操作父仓库。
- 不存在目录、空目录、隐藏文件目录、合法 gitfile、无 HEAD 仓库可正确区分。
- 只同步所选仓库，不自动加入父仓库；父失败或有本地修改不阻止所选子仓库。单选子仓库可独立克隆或快进，父仓库的分支、提交与本地修改保持不变。
- 本地领先、落后、分叉、脏工作区、认证失败、分支不存在得到不同结果。
- 切换分支前，比较目标本地分支与远端关系，不误用当前分支。
- 多方案指向同一仓库不会同时修改；日志持续输出时窗口仍可交互。
- 检查并发不超过 8，各项日志归属正确；停止不派发后续项，中断覆盖全部活动 Git 进程，共享 worktree 远端检查互斥。
- 原生菜单使用右键所在目录，保留勾选，支持中文路径；取消不刷新、命令后刷新，关闭窗口不遗留新启动的线程。
- 两个仓库选项随方案持久化，旧配置默认关闭；忽略提醒不隐藏其他错误或改变文件/Diff；强制更新不备份且保留本地提交，检查不丢弃，独立同步和重试不绕过明确选择，ignored 文件和子仓库受保护。
- 中止和重试后状态真实，原有文件与本地提交保留。
- 配置增删改、导入导出、保存恢复、中文与空格路径均可用。
- 环境准备失败不覆盖 Git 成功记录，必需仓库未就绪时不会自动执行。

界面示意是保留的早期 HTML 原型，包含已取消的前置等待规则；当前行为以本文和桌面程序为准。示意中的状态是演示数据，所有交互只改变示意内容，不调用本机 Git。
