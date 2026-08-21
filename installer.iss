; 配置助手的用户级安装包。升级时覆盖程序文件，但不碰用户数据和登录态。
; build.bat 通过 /DMyAppVersion=1.0.9 /DMyRuntimeId=1 注入。
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef MyRuntimeId
  #define MyRuntimeId "0"
#endif

[Setup]
AppId={{B5DEB99C-93D5-49A6-B7D0-A7FDFEFCFAD4}
AppName=大会员业务后台 配置助手
AppVersion={#MyAppVersion}
AppPublisher=大会员业务后台
DefaultDirName={localappdata}\配置助手
DefaultGroupName=配置助手
DisableProgramGroupPage=yes
; 安装流程只留「选路径 -> 安装 -> 完成」：
;   欢迎页 Inno 6 默认就不显示；这里再去掉「确认安装」汇总页。
DisableReadyPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=dist
; Release 附件名保持 ASCII，避免不同代码页下中文文件名被截断。
OutputBaseFilename=ConfigAssistant-Setup-{#MyAppVersion}
SetupIconFile=assets\icon.ico
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=配置助手

[Files]
; ---- 运行时外壳：PyInstaller onedir 的产物，只有依赖变了才会变 ----
Source: "dist\配置助手\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "dist\配置助手更新器.exe"; DestDir: "{app}"; Flags: ignoreversion
; 运行时代号。必须在代码包之外 —— 代码包会被更新覆盖，放进去就等于让它自称是新的。
Source: "dist\runtime.txt"; DestDir: "{app}"; Flags: ignoreversion

; ---- 代码本体：外置成普通文件，才能被 300KB 的代码包增量更新掉 ----
Source: "main.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "src\*"; DestDir: "{app}\src"; Excludes: "__pycache__,*.pyc"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "assets\*"; DestDir: "{app}\assets"; Excludes: "__pycache__,*.pyc"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion

; ---- 随版本发布的默认内容：每次升级都刷新 ----
Source: "config\forms\*"; DestDir: "{app}\config\forms"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "config\team.json"; DestDir: "{app}\config"; Flags: ignoreversion skipifsourcedoesntexist
; 统计回传地址。⚠ 必须 ignoreversion（每次升级都刷新）：升级不覆盖 settings.yaml，
; 如果地址只存在 settings.yaml 里，从老版本升上来的人就永远是空的、统计静默失效。
Source: "config\webhook.txt"; DestDir: "{app}\config"; Flags: ignoreversion skipifsourcedoesntexist

; ---- 本机可写内容：已有文件一律保留 ----
Source: "config\settings.yaml"; DestDir: "{app}\config"; Flags: onlyifdoesntexist
Source: "config\strategies\*"; DestDir: "{app}\config\strategies"; Flags: recursesubdirs createallsubdirs onlyifdoesntexist skipifsourcedoesntexist
Source: "config\prep\*"; DestDir: "{app}\config\prep"; Flags: recursesubdirs createallsubdirs onlyifdoesntexist skipifsourcedoesntexist
; 仅提供尚不存在的模板，绝不覆盖同事已填的数据文件。
Source: "data\*模板.xlsx"; DestDir: "{app}\data"; Flags: onlyifdoesntexist skipifsourcedoesntexist

[Icons]
; 桌面快捷方式默认就建，不再单开一页问 —— 内部工具，同事得找得到。
Name: "{autoprograms}\配置助手"; Filename: "{app}\配置助手.exe"
Name: "{autodesktop}\配置助手"; Filename: "{app}\配置助手.exe"

[Run]
; 正常首次安装与程序内静默升级完成后都自动启动新版本。
Filename: "{app}\配置助手.exe"; Flags: nowait

; ⚠ 老的绿色版用户不会被自动迁移（这一步按要求去掉了）。
;   他们的策略中心配置、准备参数、Excel、使用统计都还在原来那个解压出来的
;   「配置助手分发包_vX.Y.Z\」文件夹里，而这里装到的是 %LOCALAPPDATA%\配置助手，
;   是个全新的空目录。需要的话手动把旧文件夹里的这几样拷过来：
;       config\strategies\    策略中心配置
;       config\prep\          原生商广的准备阶段参数
;       data\                 自己的 Excel
;       output\usage.jsonl    使用统计（不拷的话首页数字从零开始）
;   settings.yaml 不建议直接拷 —— 新版多了 update: 段；真拷了也没关系，
;   src/settings.py 会用 assets/settings.default.yaml 把缺的字段补上。
