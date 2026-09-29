# MiniGame Repo Tool Implementation Plan

> For agentic workers: use superpowers:executing-plans to execute this plan task by task.

Goal: 在 D:\git\Tools\MiniGameRepoTool 交付可运行的 Python 桌面程序。

Architecture: 配置与 Git 核心独立于 Qt；界面通过工作线程执行同步过程，通过 Qt 信号接收状态和日志。任务按依赖顺序执行，异常保留现场。

Tech Stack: Python 3.11+、PySide6 Essentials、系统 Git、pytest。

Spec: docs/design.md。

全局约束：只在指定工程目录修改文件；不向远端 push；不用实际 MiniGame 仓库验证写操作；不自动 stash/reset/clean/rebase；不创建合并提交。

Review Focus: 子目录误认父仓库、目标分支与当前分支混淆、嵌套仓库未跟踪文件过滤、停止与进程树终止、配置异常与路径逃逸。

## Task 1: 配置、进程执行与 Git 核心

Files: src/repo_tool/models.py、profiles.py、process.py、git_ops.py、service.py、storage.py、setup.py；tests/test_models.py、test_git_service.py、test_process.py。

Interfaces: Profile/RepoSpec 表达配置；Snapshot 表达检测结果；ProcessRunner.run(program, arguments, cwd, timeout) 执行进程；RepoService.check/sync/setup 返回独立结果并通过 emit(kind, payload) 发布事件。

- [x] 先写配置验证、目录识别、实际 Git 克隆/更新/分叉/分支切换与停止测试，运行确认失败。
- [x] 实现配置原子保存、默认 8 仓库、依赖推导、仓库识别和提交关系检查。
- [x] 实现进程输出、取消、同配置目录实例互斥、依赖调度、运行记录、环境准备。
- [x] 运行核心测试，修复失败。

## Task 2: 桌面交互与配置编辑

Files: src/repo_tool/ui/window.py、dialogs.py、worker.py；tests/test_ui.py。

Interfaces: MainWindow(data_dir) 持有 ProfileStore；JobThread 将 RepoService 回调转成 Qt 信号；对话框返回校验通过的配置副本。

- [x] 写离屏 Qt 测试，验证主窗口、方案编辑、选择与 Git 任务完成状态。
- [x] 实现主窗口、详情、日志过滤、方案和仓库增删改、导入导出、远端分支读取、单仓库同步、失败重试、停止和中断。
- [x] 实现环境准备入口、退出时任务处理与异常记录。
- [x] 运行 GUI 测试与全部测试。

## Task 3: 运行入口、交付与复核

Files: pyproject.toml、run.py、Start.bat、Install.bat、README.md、docs/usage.md。

- [x] 提供虚拟环境安装与双击启动入口，以及独立配置目录参数。
- [x] 更新设计差异和使用说明；执行真实界面启动、截图和完整测试。
- [x] 请求独立代码审查并处理重要问题；记录验证结果。

执行裁定：用户已明确要求实现，直接在本任务完成，不再重复索要设计确认。按用户指定目录原地工作，使用 codex/minigame-repo-tool 分支。

进程裁定：用 subprocess.Popen 在 QThread 中运行 Git，提供纯 Python 可测核心与 Windows 进程树中断；与原 QProcess 设想相比保持界面非阻塞，减少核心对 Qt 的依赖。

互斥裁定：第一版同一配置目录下的工具实例共享一把运行锁，串行整个任务，覆盖跨方案共享工作树。采用系统文件锁，进程退出后自动释放。
