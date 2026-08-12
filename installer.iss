; Inno Setup 安装向导脚本 —— 抓包测效助手 (captrace) C/S 桌面版
; 用法：用 ISCC 编译本文件，生成 dist_setup/captrace-setup-x.y.z.exe
; 前置：先运行 build_installer.ps1 生成 dist/captrace-gui 单目录包

#define MyAppName "抓包测效助手"
#define MyAppExeName "captrace-gui.exe"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "captrace"
#define MyAppURL "https://github.com/your-org/captrace"
#define MyAppId "{{8F3C2A1B-6E4D-4C9A-9B2E-1A0C5D7F3E8B}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
LicenseFile=
OutputDir=dist_setup
OutputBaseFilename=captrace-setup-{#MyAppVersion}
SetupIconFile=assets\icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
WizardSizePercent=120
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "chinese"; MessagesFile: "compiler:Default.isl"

[Files]
; 把 PyInstaller 生成的单目录整体拷进安装目录
Source: "dist\captrace-gui\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务"; Flags: unchecked

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "安装完成后启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
