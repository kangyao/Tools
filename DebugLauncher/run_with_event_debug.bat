@echo off
rem 一次性调查启动器：带事件探针启动 AIFramework 调试客户端。
rem 用完即弃；探针日志带 [EventsDBG]/[OverlayDBG]/[PopoverDBG] 前缀。
set MINIGUI_EVENT_DEBUG=1
call "%~dp0run_launcher.bat"
