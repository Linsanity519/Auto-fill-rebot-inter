; 配置助手的用户级安装包。升级时覆盖程序文件，但不碰用户数据和登录态。
; build.bat 通过 /DMyAppVersion=1.0.7 注入版本号。
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{B5DEB99C-93D5-49A6-B7D0-A7FDFEFCFAD4}
AppName=大会员业务后台 配置助手
AppVersion={#MyAppVersion}
AppPublisher=大会员业务后台
DefaultDirName={localappdata}\配置助手
DefaultGroupName=配置助手
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=dist
OutputBaseFilename=配置助手-Setup-{#MyAppVersion}
SetupIconFile=assets\icon.ico
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=配置助手

[Files]
; 程序本体：升级时始终覆盖。
Source: "dist\配置助手.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\配置助手更新器.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion
; 表单映射与团队快照属于随版本发布的默认内容。
Source: "config\forms\*"; DestDir: "{app}\config\forms"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "config\team.json"; DestDir: "{app}\config"; Flags: ignoreversion skipifsourcedoesntexist
; settings / 策略 / 准备参数是本机可写内容，已有文件一律保留。
Source: "config\settings.yaml"; DestDir: "{app}\config"; Flags: onlyifdoesntexist
Source: "config\strategies\*"; DestDir: "{app}\config\strategies"; Flags: recursesubdirs createallsubdirs onlyifdoesntexist skipifsourcedoesntexist
Source: "config\prep\*"; DestDir: "{app}\config\prep"; Flags: recursesubdirs createallsubdirs onlyifdoesntexist skipifsourcedoesntexist
; 仅提供尚不存在的模板，绝不覆盖同事已填的数据文件。
Source: "data\*模板.xlsx"; DestDir: "{app}\data"; Flags: onlyifdoesntexist skipifsourcedoesntexist

[Icons]
Name: "{autoprograms}\配置助手"; Filename: "{app}\配置助手.exe"
Name: "{autodesktop}\配置助手"; Filename: "{app}\配置助手.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; Flags: unchecked

[Run]
; 正常首次安装与程序内静默升级完成后都自动启动新版本。
Filename: "{app}\配置助手.exe"; Flags: nowait
