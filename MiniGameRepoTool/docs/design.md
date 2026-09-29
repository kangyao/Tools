# MiniGame 仓库管理工具设计

日期：2026-09-29。交付范围：Python 桌面程序、使用文档、回归测试，以及保留的设计阶段交互示意。

用户已选择：仓库列表可编辑，支持保存多套根目录与分支方案。初始配置以 E:\pull - E.bat 的 8 个仓库为准。

**目标与日常流程**

面向 Windows 上需要初始化、切换和更新 MiniGame 多仓库环境的开发者。选择方案后，即可看清各仓库的位置、当前分支、目标分支、工作区状态与预计操作；执行过程中能定位失败仓库，处理后单独重试。

主要流程：选择方案 → 检查状态 → 查看计划 → 同步所选 → 查看结果。直接点击“同步所选”也会先检查，无须每次手动走两遍。

第一版支持完整克隆、获取指定分支、切换本地分支、快进更新、单仓库重试和构建环境准备。第一版不加入提交、推送、图形化解决冲突、自动定时更新。

**技术选择**

| 方案 | 适合之处 | 代价与选择 |
|---|---|---|
| Python + PySide6 + 系统 Git | 多列树形仓库列表、可调整分栏、异步进程输出和多套配置管理 | 推荐；发布时需要打包 Qt 运行库 |
| Python + Tkinter/ttk + 系统 Git | 依赖少，适合简单列表和日志窗口 | 可行；复杂表格、详情编辑和持续演进需要更多界面组装 |

用 Git CLI 执行实际操作，复用当前机器的 SSH、Git Credential Manager 和 Git 配置。Git 操作以程序路径与参数数组启动，普通 Git 调用不拼接 shell 命令。

PySide6 的 QTreeWidget 表达层级仓库；QThread 中的 subprocess.Popen 执行 Git 并持续读取输出，以 Qt 信号向界面传递状态与日志。UI 线程不使用阻塞式等待，控件由主线程更新；Git 核心不依赖 Qt。

参考：[QTreeView](https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QTreeView.html)、[QProcess](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QProcess.html)、[Tkinter](https://docs.python.org/3/library/tkinter.html)。

**主窗口**

主窗口初始尺寸为 1420 × 900，可调整大小。

- 顶部：方案下拉框、管理方案、根目录、游戏分支和引擎分支。目标分支可输入，也可按需读取远端列表。修改后显示未保存标记。
- 中部：可勾选的树形仓库列表。主要列为仓库、当前分支、目标分支、工作区、计划动作和执行状态；窄窗口优先保留仓库、状态与动作，其余放进详情。
- 右侧：完整路径、远端地址、完整分支名、依赖、失败原因和下一步处理入口。可编辑配置或单独检查、同步该仓库。
- 底部：执行计划与实时日志两个页签。可查看全部任务或选中仓库，支持复制、导出。显示实际阶段进度；无法确定百分比时使用不定进度，不将对象接收百分比当成整个任务的百分比。
- 操作栏：检查状态、同步所选、重试失败、停止队列、准备构建环境。执行期间冻结相关方案编辑。

配置管理使用独立对话框：左侧方案列表，右侧根目录、分支组和仓库表格。支持新建、复制、重命名、删除方案，以及增删改仓库。删除条目仅删除配置记录。

仓库字段：显示名称、相对路径、远端 URL、分支组或固定分支、显式依赖、是否默认勾选。常用设置直接展示，额外依赖收进高级设置。

**初始仓库配置**

| 仓库 | 相对根目录路径 | 分支组 | 目录依赖 |
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
| 未提交修改、冲突或正在 rebase/merge | 工作区待处理 | 暂停该仓库，其他无依赖仓库可以继续 |
| 远端分支不存在、认证失败、连接失败 | 检查失败 | 分别给出原因，不统一伪装成分支不存在 |
| 父仓库执行失败 | 依赖未就绪 | 阻止后代任务，允许独立分支继续 |

初次启动先展示本地状态，详情区标记检查时间以及本次使用的是本地缓存还是刚获取的远端信息。点击检查或同步时，fetch 更新选中现有仓库的目标远端跟踪引用，再计算提交关系；未克隆仓库用 ls-remote 验证目标分支。远端检查不会切换分支或修改工作区文件。

origin 与配置不一致时展示两个地址并标为待处理，提供编辑配置或“采用实际 origin”的操作；后一操作只更新配置。更换仓库实际 origin 留给外部 Git 工具。

检查工作区时，仅排除已验证为独立仓库、与配置路径完全对应的未跟踪子仓库目录；父仓库已跟踪的文件变化仍然计入。不会写入用户的 ignore 配置。

如果父仓库目标提交开始跟踪子仓库占用的路径，或将子仓库任一祖先变成普通文件、符号链接或 gitlink，先报告布局冲突。同时检查目标远端提交和已有目标本地分支，按 Windows 大小写规则匹配路径；切换与快进命令使用 --no-overwrite-ignore 保护忽略文件。真正的 Git submodule 在克隆前也检查父 index 的 gitlink；第一版不通过递归更新或独立切换分支改变父仓库要求的指针。

参考：[工作树根目录识别](https://git-scm.com/docs/git-rev-parse)、[克隆到现有空目录](https://git-scm.com/docs/git-clone)。

**执行与失败恢复**

1. 加载方案，校验路径、仓库 ID、分支名、重复目标和依赖环。相对路径不得逃出根目录；比较时规范化 Windows 路径、大小写和目录链接。
2. 从目录嵌套关系推导父子依赖，再合并显式依赖。第一版按顺序执行 Git 写操作，并发更新留待后续评估。
3. 勾选子仓库时，如父仓库尚未准备好，界面同步勾选必要父仓库并说明原因。父仓库已经可用时，只检查其状态，不强制更新未勾选父仓库。
4. 执行前重新核对工作区和目标引用。第一版以配置目录内的系统文件锁串行所有工具任务，覆盖使用该配置目录的跨方案和多实例；其他 Git 客户端及不同配置目录不受此锁控制。
5. 克隆指定目标分支并使用 single-branch，默认完整历史，与当前脚本一致。已有仓库只获取需要的分支、切换并执行 fast-forward-only 更新。
6. 一个仓库失败后，后代标为依赖未就绪；其他分支继续。结果分别统计成功、待处理、失败、未执行、取消。
7. 重试从真实目录状态重新生成计划，连带重新评估受阻后代，不把上一次步骤编号当成当前事实。
8. “停止队列”停止派发下一个仓库，当前仓库完成后停止。长时间网络操作可在详情中单独中断；进程管理覆盖 Git 及其 SSH 子进程，重新检查残留状态后才允许重试。
9. 克隆中断留下的非空目录或不完整仓库必须可见，不承诺自动续传；保留内容，提供更换目标路径或人工处理后的重新检查。

不自动执行 stash、reset --hard、clean、变基或创建合并提交。未提交改动和分叉直接进入待处理状态。

**准备构建环境**

把当前脚本末尾的 Win_Setup 提升为独立任务。按钮明确叫“准备构建环境”。

默认手动触发；可按方案勾选“所需仓库全部就绪后自动准备”。需要检查全部必需仓库，不能因为本次只选中的一个仓库成功，就认为整个工程可用。

原 Win_Setup.bat 从 SetupScript 执行 pushd ..，实际在工程根目录启动 Python。程序直接解压 Python39 并以工程根目录为 cwd 执行 Tools/Setup/SetupWin.py，保留解压及 Python 步骤的实际退出码。准备失败时保留 Git 同步成功结果，可以单独重试准备步骤。

**模块边界**

| 模块 | 责任 | 主要输出 |
|---|---|---|
| ui | 主窗口、配置编辑、仓库详情、计划与日志 | 用户操作意图 |
| models.py / profiles.py | 配置读写、版本、路径与依赖校验 | Profile、RepoSpec、ProfileDocument |
| git_ops.py | 仓库识别、工作区、远端、提交关系与计划动作 | Snapshot |
| process.py | Popen、输出解码、退出码、超时与进程树中断 | ProcessResult |
| service.py | 调度、依赖阻塞、互斥、重新检查与执行 | RepoResult、结构化事件 |
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
        "enabled": true
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

上述 JSON 是两仓库最小格式示例；内置方案实际包含前表全部 8 个仓库，并将这 8 个仓库列为环境准备的必需项。分支使用 branch_group 或固定 branch 二选一，不使用任意字符串模板。显式 depends_on 按需添加，普通父子目录依赖自动推导。

配置可导入导出。配置保存地址，不保存密码、令牌或私钥；拒绝包含用户信息的 HTTP(S) URL；界面日志、导出日志和结构化运行记录均对 URL 凭据部分脱敏。完整 8 仓库配置见 ../examples/profiles.json。

**验收重点**

- 主仓库内部的空 AssetRuntime 能进入克隆计划，不会误操作父仓库。
- 不存在目录、空目录、隐藏文件目录、合法 gitfile、无 HEAD 仓库可正确区分。
- 父子仓库按依赖执行；父失败只阻塞后代；选单个子仓库不会创建错误的父目录布局。
- 本地领先、落后、分叉、脏工作区、认证失败、分支不存在得到不同结果。
- 切换分支前，比较目标本地分支与远端关系，不误用当前分支。
- 多方案指向同一仓库不会同时修改；日志持续输出时窗口仍可交互。
- 中止和重试后状态真实，原有文件与本地提交保留。
- 配置增删改、导入导出、保存恢复、中文与空格路径均可用。
- 环境准备失败不覆盖 Git 成功记录，必需仓库未就绪时不会自动执行。

界面示意中的状态是演示数据，所有交互只改变示意内容，不调用本机 Git。
