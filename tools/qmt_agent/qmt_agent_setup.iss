#define MyAppName "QuantMind QMT Agent"
#define MyAppDisplayName "QuantMind QMT 交易助手"
#define MyAppVersion "0.3.0"
#define MyAppPublisher "QuantMind"
#define MyAppExeName "QuantMindQMTAgent.exe"
#define MyAppId "com.quantmind.qmt-agent"

[Setup]
AppId={#MyAppId}
AppName={#MyAppDisplayName}
AppVerName={#MyAppDisplayName} {#MyAppVersion}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\QuantMindQMTAgent
DefaultGroupName={#MyAppDisplayName}
DisableProgramGroupPage=yes
OutputDir=..\..\dist\qmt_agent\installer
OutputBaseFilename=QuantMindQMTAgent-Setup-{#MyAppVersion}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "schinese"; MessagesFile: "..\..\dist\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
schinese.LaunchProgram=安装完成后启动量化助手
english.LaunchProgram=Launch QuantMind QMT Agent after setup

[Messages]
schinese.ButtonNext=下一步(&N)
schinese.ButtonBack=上一步(&B)
schinese.ButtonCancel=取消
schinese.ButtonInstall=安装
schinese.ButtonFinish=完成
schinese.ButtonBrowse=浏览...
schinese.ButtonYes=是
schinese.ButtonNo=否
schinese.WelcomeLabel1=欢迎使用 QuantMind QMT 交易助手 安装向导
schinese.WelcomeLabel2=安装程序将引导你完成安装。建议先关闭其他应用程序，然后继续。安装后可在程序内“帮助中心”查看本地启动与日志命令。
schinese.SelectDirLabel3=请选择 QuantMind QMT 交易助手 的安装目录：
schinese.SelectDirBrowseLabel=点击“浏览”选择其他安装目录。
schinese.SelectStartMenuFolderLabel3=请选择开始菜单文件夹：
schinese.SelectStartMenuFolderBrowseLabel=点击“浏览”选择开始菜单文件夹。
schinese.ReadyLabel1=准备安装 QuantMind QMT 交易助手。
schinese.FinishedHeadingLabel=QuantMind QMT 交易助手 安装完成
schinese.FinishedLabel=点击“完成”退出安装程序。
schinese.BeveledLabel=QuantMind QMT 交易助手

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\..\dist\qmt_agent\QuantMindQMTAgent.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram}"; Flags: nowait postinstall skipifsilent
