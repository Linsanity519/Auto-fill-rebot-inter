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
Name: "{autoprograms}\配置助手"; Filename: "{app}\配置助手.exe"
Name: "{autodesktop}\配置助手"; Filename: "{app}\配置助手.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; Flags: unchecked

[Run]
; 正常首次安装与程序内静默升级完成后都自动启动新版本。
Filename: "{app}\配置助手.exe"; Flags: nowait

[Code]
{ ============================================================================
  从「老的绿色分发包」自动迁移用户数据。

  ⚠ 这一段是必需的，不是锦上添花：老同事手上的配置助手是一个解压出来的文件夹
    （配置助手分发包_v1.0.6\），他们的策略中心配置、准备参数、Excel、使用统计
    全在那个文件夹里。而这个安装包装到的是 %LOCALAPPDATA%\配置助手 —— 一个
    全新的空目录。不迁移的话，第一次升级在他们眼里就是「恢复出厂设置」：
    策略中心空了、统计归零。build.bat 里那条注释记过一次同类事故
    （"实测干掉过一次别人配了一下午的策略"），这次是同一个坑换了个位置。

  ⚠ 为什么是「自动探测 + 一个是/否」，而不是让用户填路径：
    ① 用户根本不知道该填哪儿 —— 那个文件夹是当初别人用企微发给他的，
       解压在哪早忘了，让他填等于让他猜。
    ② TInputDirWizardPage 自带「必须是带盘符的完整路径」校验，且它跑在脚本的
       NextButtonClick 之前，留空会直接弹错并中止安装。实测踩过：交互安装点
       下一步弹 "You must enter a full path with drive letter"，/VERYSILENT
       安装则整个装不上（日志里是同一句 + "aborting"）。
    所以这里一个向导页都不加：装完自己去找，找到了把路径摆给用户看，问一句
    要不要搬。找不到就安静跳过，不打扰。

  ⚠ .chrome-profile 不迁移：动辄几百 MB，拷贝慢且换路径后未必可用。
    重新登录一次的成本远低于装到一半卡住，所以在提示里说清楚要重登。
  ============================================================================ }

function IsUpgrade(): Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\配置助手.exe'));
end;

function LooksLikeOldInstall(Dir: String): Boolean;
begin
  { 认「有 config\settings.yaml」而不是认文件夹名：有人会把文件夹改名 }
  Result := (Dir <> '') and DirExists(Dir)
            and FileExists(AddBackslash(Dir) + 'config\settings.yaml');
end;

{ 在 Parent 下找一层：Parent\配置助手*\ }
function ScanParent(Parent: String): String;
var
  FindRec: TFindRec;
  Candidate: String;
begin
  Result := '';
  if not DirExists(Parent) then Exit;
  if FindFirst(AddBackslash(Parent) + '*', FindRec) then
  try
    repeat
      if (FindRec.Name = '.') or (FindRec.Name = '..') then Continue;
      if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) = 0 then Continue;
      Candidate := AddBackslash(Parent) + FindRec.Name;
      if LooksLikeOldInstall(Candidate) then
      begin
        Result := Candidate;
        Exit;
      end;
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;
end;

{ Parent 自己 → Parent\* → Parent\*\*，最多两层。
  两层是因为常见摆法是「桌面\工作\配置助手分发包_v1.0.6」。 }
function ScanDeep(Parent: String): String;
var
  FindRec: TFindRec;
  Child: String;
begin
  Result := '';
  if LooksLikeOldInstall(Parent) then
  begin
    Result := Parent;
    Exit;
  end;
  Result := ScanParent(Parent);
  if Result <> '' then Exit;

  if not DirExists(Parent) then Exit;
  if FindFirst(AddBackslash(Parent) + '*', FindRec) then
  try
    repeat
      if (FindRec.Name = '.') or (FindRec.Name = '..') then Continue;
      if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) = 0 then Continue;
      { 跳过明显不可能、且巨大的系统目录，别让安装卡在那儿扫半天 }
      if (CompareText(FindRec.Name, 'AppData') = 0)
         or (CompareText(FindRec.Name, 'Windows') = 0)
         or (CompareText(FindRec.Name, 'Program Files') = 0)
         or (CompareText(FindRec.Name, 'Program Files (x86)') = 0)
         or (CompareText(FindRec.Name, '$Recycle.Bin') = 0) then Continue;
      Child := AddBackslash(Parent) + FindRec.Name;
      Result := ScanParent(Child);
      if Result <> '' then Exit;
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;
end;

function GuessOldFolder(): String;
var
  Places: array[0..6] of String;
  I: Integer;
begin
  Places[0] := ExpandConstant('{userdesktop}');
  Places[1] := ExpandConstant('{%USERPROFILE|}\Downloads');
  Places[2] := ExpandConstant('{userdocs}');
  Places[3] := ExpandConstant('{%USERPROFILE|}');
  Places[4] := 'D:\';
  Places[5] := 'E:\';
  Places[6] := 'F:\';
  for I := 0 to 6 do
  begin
    Result := ScanDeep(Places[I]);
    if Result <> '' then Exit;
  end;
  Result := '';
end;

{ 递归复制。Overwrite=True 时用户的旧数据盖过安装包刚铺的默认值 —— 这是想要的：
  他自己配的策略当然比出厂默认重要。 }
procedure CopyTree(Src, Dst: String; Overwrite: Boolean);
var
  FindRec: TFindRec;
  S, D: String;
begin
  if not DirExists(Src) then Exit;
  ForceDirectories(Dst);
  if FindFirst(AddBackslash(Src) + '*', FindRec) then
  try
    repeat
      if (FindRec.Name = '.') or (FindRec.Name = '..') then Continue;
      S := AddBackslash(Src) + FindRec.Name;
      D := AddBackslash(Dst) + FindRec.Name;
      if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
        CopyTree(S, D, Overwrite)
      else
        CopyFile(S, D, not Overwrite);
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;
end;

procedure MigrateFrom(Old: String);
var
  App: String;
begin
  App := ExpandConstant('{app}');
  { 用户自己的配置：策略中心、准备参数、settings.yaml }
  CopyTree(AddBackslash(Old) + 'config', AddBackslash(App) + 'config', True);
  { Excel 数据 }
  CopyTree(AddBackslash(Old) + 'data', AddBackslash(App) + 'data', True);
  { 使用统计：只搬这两个，别把几百 MB 的截图也搬过来 }
  ForceDirectories(AddBackslash(App) + 'output');
  CopyFile(AddBackslash(Old) + 'output\usage.jsonl',
           AddBackslash(App) + 'output\usage.jsonl', False);
  CopyFile(AddBackslash(Old) + 'output\usage-reported.json',
           AddBackslash(App) + 'output\usage-reported.json', False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Old: String;
begin
  if CurStep <> ssPostInstall then Exit;
  { 静默安装（程序内自动更新走的就是这条）和普通升级都没有绿色版可迁移 }
  if WizardSilent or IsUpgrade() then Exit;

  Old := GuessOldFolder();
  if Old = '' then Exit;

  if MsgBox('发现你之前在用的配置助手：' + #13#10 + #13#10 + Old + #13#10 + #13#10 +
            '要把里面的策略中心配置、准备参数、Excel 数据和使用统计'
            + '搬到新版本吗？' + #13#10 +
            '（选「否」的话新版本是一份干净的默认配置，旧文件夹不会被动）',
            mbConfirmation, MB_YESNO) <> IDYES then Exit;

  { ⚠ 迁移过来的 settings.yaml 是老版本写的，没有 update: 段。
    这不要紧 —— src/settings.py 会拿 assets/settings.default.yaml 兜底把缺的
    字段补上，否则「升上来的人反而永远收不到下一次更新」。 }
  MigrateFrom(Old);
  MsgBox('配置已搬过来了。' + #13#10 + #13#10 +
         '旧文件夹还在原地没动，确认新版本没问题后可以自行删除。' + #13#10 +
         '浏览器登录态没有迁移，第一次使用时请重新登录一次。',
         mbInformation, MB_OK);
end;
