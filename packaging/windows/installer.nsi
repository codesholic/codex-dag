Unicode true
!include "MUI2.nsh"
!include "x64.nsh"
Name "Codex DAG"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\Codex DAG"
RequestExecutionLevel user
SetCompressor /SOLID lzma
VIProductVersion "${VERSION}.0"
VIAddVersionKey /LANG=1033 "ProductName" "Codex DAG"
VIAddVersionKey /LANG=1033 "FileDescription" "Codex DAG Windows x64 Setup"
VIAddVersionKey /LANG=1033 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=1033 "LegalCopyright" "Codex DAG"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\Codex DAG.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Codex DAG 실행"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Korean"
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "Codex DAG은 Windows x64용입니다."
    Abort
  ${EndIf}
  SetRegView 64
  SetShellVarContext current
FunctionEnd

Function WaitForAppClose
  IfFileExists "$INSTDIR\Codex DAG.exe" 0 closed
  ExecWait '"$INSTDIR\Codex DAG.exe" --quit-for-install'
retry:
  StrCpy $1 20
wait:
  System::Call 'kernel32::CreateFileW(w "$INSTDIR\Codex DAG.exe", i 0x40000000, i 0, p 0, i 3, i 0, p 0) p.r0'
  IntCmp $0 -1 locked
  System::Call 'kernel32::CloseHandle(p r0)'
  Goto closed
locked:
  Sleep 250
  IntOp $1 $1 - 1
  IntCmp $1 0 blocked wait wait
blocked:
  MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "Codex DAG을 트레이에서 종료한 뒤 다시 시도하세요. 실행 파일이 사용 중이라 설치할 수 없습니다." IDRETRY retry
  Abort
closed:
FunctionEnd

Section "Codex DAG" Main
  Call WaitForAppClose
  SetOutPath "$INSTDIR"
  ClearErrors
  File /r "${BUNDLEDIR}/*"
  IfErrors copy_failed
  WriteUninstaller "$INSTDIR\Uninstall Codex DAG.exe"
  CreateDirectory "$SMPROGRAMS\Codex DAG"
  CreateShortcut "$SMPROGRAMS\Codex DAG\Codex DAG.lnk" "$INSTDIR\Codex DAG.exe"
  CreateShortcut "$SMPROGRAMS\Codex DAG\제거.lnk" "$INSTDIR\Uninstall Codex DAG.exe"
  CreateShortcut "$DESKTOP\Codex DAG.lnk" "$INSTDIR\Codex DAG.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Codex DAG" "DisplayName" "Codex DAG"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Codex DAG" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Codex DAG" "Publisher" "Codex DAG"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Codex DAG" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Codex DAG" "UninstallString" '"$INSTDIR\Uninstall Codex DAG.exe"'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Codex DAG" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Codex DAG" "NoRepair" 1
  Goto installed
copy_failed:
  MessageBox MB_ICONSTOP "앱 파일을 복사하지 못했습니다. 설치를 다시 실행하세요. 부분 설치를 완료로 등록하지 않았습니다."
  SetErrorLevel 1
  Abort
installed:
SectionEnd

Function un.onInit
  SetRegView 64
  SetShellVarContext current
  ; The install directory is fixed; never remove a user-selected data directory.
  StrCpy $INSTDIR "$LOCALAPPDATA\Programs\Codex DAG"
FunctionEnd

Function un.WaitForAppClose
  IfFileExists "$INSTDIR\Codex DAG.exe" 0 closed
  ExecWait '"$INSTDIR\Codex DAG.exe" --quit-for-install'
retry:
  StrCpy $1 20
wait:
  System::Call 'kernel32::CreateFileW(w "$INSTDIR\Codex DAG.exe", i 0x40000000, i 0, p 0, i 3, i 0, p 0) p.r0'
  IntCmp $0 -1 locked
  System::Call 'kernel32::CloseHandle(p r0)'
  Goto closed
locked:
  Sleep 250
  IntOp $1 $1 - 1
  IntCmp $1 0 blocked wait wait
blocked:
  MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "Codex DAG을 트레이에서 종료한 뒤 다시 시도하세요. 실행 파일이 사용 중이라 제거할 수 없습니다." IDRETRY retry
  Abort
closed:
FunctionEnd

Section "Uninstall"
  Call un.WaitForAppClose
  InitPluginsDir
  ClearErrors
  CopyFiles /SILENT "$INSTDIR\Uninstall Codex DAG.exe" "$PLUGINSDIR\retry-uninstall.exe"
  IfErrors backup_failed
  SetOutPath "$TEMP"
  ClearErrors
  RMDir /r "$INSTDIR"
  IfErrors removal_failed
  Delete "$DESKTOP\Codex DAG.lnk"
  Delete "$SMPROGRAMS\Codex DAG\Codex DAG.lnk"
  Delete "$SMPROGRAMS\Codex DAG\제거.lnk"
  RMDir "$SMPROGRAMS\Codex DAG"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Codex DAG"
  ; Settings and evidence in LocalAppData\Codex DAG are intentionally preserved.
  Goto done
removal_failed:
  CreateDirectory "$INSTDIR"
  CopyFiles /SILENT "$PLUGINSDIR\retry-uninstall.exe" "$INSTDIR\Uninstall Codex DAG.exe"
  MessageBox MB_ICONEXCLAMATION "일부 앱 파일을 제거하지 못했습니다. 실행 중인 앱을 종료하고 제거를 다시 실행하세요. 설정과 작업 기록은 보존됩니다."
  Abort
backup_failed:
  MessageBox MB_ICONEXCLAMATION "제거 파일을 준비하지 못했습니다. 앱 파일은 보존했습니다."
  Abort
done:
SectionEnd
