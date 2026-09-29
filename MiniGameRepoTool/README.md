# MiniGame 仓库管理器

可运行的 Windows Python 桌面工具，使用 PySide6 和系统 Git 管理 MiniGame 的多个仓库。支持多套目录与分支方案、可编辑仓库列表、克隆、分支切换、快进更新、失败重试、实时日志和构建环境准备。

工程和文档均位于 **D:\git\Tools\MiniGameRepoTool**。工具位置与被管理的工程位置相互独立；内置方案管理 E:\MiniGame，也可以新建其他根目录的方案。

## 启动

双击 **[Start.bat](Start.bat)**。本机已配置项目专用虚拟环境和依赖，可以直接启动。

在其他机器上，先安装 Python 3.11+ 与 Git for Windows，再运行 Start.bat。首次启动会调用 [Install.bat](Install.bat) 创建本目录下的 .venv 并安装依赖。依赖安装需要网络；以后启动不需要重新安装。SSH 密钥和 Git 凭据沿用机器已有配置。

也可以从终端启动：

~~~powershell
cd D:\git\Tools\MiniGameRepoTool
.\.venv\Scripts\python.exe run.py
~~~

## 日常操作

1. 在“管理方案”中设置根目录、游戏与引擎分支、仓库列表；可以复制方案以维护不同工作目录。
2. 勾选仓库，点击“检查状态”，查看各仓库状态和“执行计划”。
3. 点击“同步所选”。未克隆或空目录会克隆；已有仓库获取目标分支后切换、快进更新。
4. 从右侧详情或运行日志定位失败原因。处理完成后点击“重试失败”，或“同步此仓库”。
5. 需要初始化依赖时，点击“准备构建环境”；也可在方案中启用同步后的自动准备。

初次打开只读取本地仓库状态。点击检查状态会访问远端并更新目标分支的远端跟踪引用；点击同步才会切换分支和更新工作区。

遇到“有本地修改”时，选中仓库，点击右侧“文件改动 / Discard”（也可双击仓库）。列表展示文件路径、已暂存和未暂存状态；双击文件查看 Diff。勾选需要放弃的文件，点击“备份并丢弃所选（Discard）”，核对路径并确认。子模块可以在此查看自己的文件，也可以从父仓库的文件列表中选中子模块后点击“进入子仓库”。

Discard 将选中的已跟踪文件恢复到当前提交，同时丢弃其暂存和未暂存改动；选中的未跟踪文件备份后删除。操作前完整备份工作区文件和暂存改动，完成后可以“打开上次备份”。未勾选文件保留。具体边界和恢复步骤见[文件改动与 Discard](docs/usage.md#文件改动与-discard)。

## 已实现

- 多方案的新建、复制、编辑、删除、导入与导出；JSON 原子保存和上一版备份。
- 内置原批处理的 8 个仓库；支持分支组、每仓库固定分支和额外依赖。
- 识别缺失、空目录、占用目录、正常工作树、gitfile/worktree、不完整仓库和子模块。
- 按目录和显式依赖顺序执行；必要时加入前置仓库；父失败阻止后代，其他仓库继续。
- 区分未提交修改、本地领先、可快进、分叉、远端不匹配、目标分支缺失和认证失败。
- 文件改动列表、已暂存/未暂存 Diff、未跟踪文件预览、勾选后备份并 Discard，以及进入已配置子仓库。
- 异步执行、实时日志、按仓库过滤、复制与导出、持久化运行结果。
- 停止后续队列，以及中断当前 Git/SSH 进程树。
- 独立或自动准备构建环境，检查全部必需仓库，保留解压和 Python 脚本的实际退出码。

同步保留本地提交，使用 Git 的快进模式和忽略文件防覆盖选项。脏工作区、分叉、目录冲突等情况会留给用户处理。工具不自动 stash、reset、clean、rebase，不提供 commit 或 push 按钮。

## 配置和日志

默认位置：

~~~text
%LOCALAPPDATA%\MiniGameRepoTool\
  profiles.json       多套方案
  profiles.json.bak   上一版有效保存的备份
  run.lock            同一配置目录的实例运行锁
  runs\               每次同步/环境准备的 JSON 结果与完整日志
  discard-backups\    每次 Discard 的文件备份、暂存补丁、清单与恢复说明
~~~

启动参数支持 --data-dir 指定独立配置目录、--no-scan 跳过启动扫描。独立配置目录主要用于测试；并行实例管理同一工程时应使用相同配置目录，共享运行锁。

## 开发和测试

~~~powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe run.py --smoke-test --data-dir artifacts\smoke-data --screenshot artifacts\main-window.png
~~~

测试使用系统临时目录中的真实本地 Git 仓库和离屏 Qt 窗口，覆盖克隆、快进、分支切换、目录保护、子模块、配置、进程中断、文件 Discard、备份恢复和界面联动。真实 E:\MiniGame 仅用于只读核对文件列表，不执行克隆、更新、环境准备或 Discard。

当前交付为 Python 源码和项目虚拟环境，没有打包独立 exe。验证环境：Windows、Python 3.13.14、Git 2.45.1、PySide6 Essentials 6.11.2。

## 文档和目录

- [使用说明](docs/usage.md)：操作步骤、状态处理和故障恢复。
- [设计文档](docs/design.md)：执行规则、数据格式与架构。
- [实现计划](docs/implementation-plan.md)与[验证记录](docs/implementation-progress.md)。
- [设计阶段交互预览](docs/ui-preview.html)：使用演示数据的 HTML 原型；实际桌面程序从 Start.bat 启动。
- [完整示例配置](examples/profiles.json)。

~~~text
MiniGameRepoTool/
  src/repo_tool/         配置、Git 核心、进程执行和 Qt 界面
  tests/                真实 Git 与界面回归测试
  examples/             可导入的默认配置
  docs/                 设计、使用说明与验证记录
  run.py                源码启动入口
  Start.bat             双击启动
  Install.bat           依赖安装
  pyproject.toml         Python 项目定义
~~~

该目录属于上级 D:\git\Tools 仓库，不另建嵌套 Git 仓库。.venv、缓存和测试截图不纳入 Git。
