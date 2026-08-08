; ---------------------------------------------------------------------------
; HydraMangaTL_Online.iss
; Phase 1 Online Bootstrap Installer for Hydra Manga TL
;
; This installer downloads the full offline setup EXE from a cloud URL
; (specified in a hosted manifest.json), verifies its SHA-256, caches it
; in a user-selected directory, and launches it.
; ---------------------------------------------------------------------------

#define MyAppName      "Hydra Manga TL"
#ifndef MyAppVersion
#define MyAppVersion   "1.0.0"
#endif
#define MyAppPublisher "Hydra"

#define ManifestUrl    "https://hydramangatl.annomous.com/offline_installer/v1/manifest.json"

[Setup]
AppId={{7A2F8C3D-E4B1-4D6A-9F52-1C3E7A8B9D0F}
AppName={#MyAppName} Online Installer
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} Online Installer v{#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}.0
VersionInfoProductVersion={#MyAppVersion}
VersionInfoProductName={#MyAppName} Online Installer
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} online bootstrap installer
VersionInfoCopyright=Copyright (c) 2026 Hydra and Hydra Manga TL Contributors

; Default download/cache folder — user can change this on the Select Directory page
DefaultDirName={localappdata}\Hydra Manga TL\InstallerCache
CreateAppDir=yes
DisableDirPage=no
DisableProgramGroupPage=yes
DisableReadyPage=no
DisableFinishedPage=yes
DisableWelcomePage=no
UsePreviousAppDir=no
DirExistsWarning=no
PrivilegesRequired=lowest

OutputDir=D:\Tools\Hydra_EXE
OutputBaseFilename=Hydra Manga TL Online Setup
SetupIconFile=D:\Tools\HydraMangaTL\assets\icons\app.ico
WizardStyle=modern dynamic windows11

Compression=none
ArchitecturesAllowed=x64compatible
Uninstallable=no

[Messages]
WelcomeLabel1=Welcome to the {#MyAppName} Online Installer
WelcomeLabel2=This installer will download and launch the full {#MyAppName} setup.%n%nClick Next to choose your download folder and continue.
SelectDirLabel3=Select the folder where the {#MyAppName} setup file should be downloaded and cached:
SelectDirBrowseLabel=To continue, click Next. If you would like to select a different folder, click Browse.

[Code]
type
  TMsg = record
    hwnd: LongWord;
    message: Integer;
    wParam: LongInt;
    lParam: LongInt;
    time: LongWord;
    pt: record x, y: Integer; end;
  end;

function PeekMessage(var lpMsg: TMsg; hWnd: LongWord; wMsgFilterMin, wMsgFilterMax, wRemoveMsg: Integer): BOOL;
  external 'PeekMessageW@user32.dll stdcall';
function TranslateMessage(var lpMsg: TMsg): BOOL;
  external 'TranslateMessage@user32.dll stdcall';
function DispatchMessage(var lpMsg: TMsg): LongInt;
  external 'DispatchMessageW@user32.dll stdcall';

procedure AppProcessMessages;
var
  Msg: TMsg;
begin
  while PeekMessage(Msg, 0, 0, 0, 1) do begin
    TranslateMessage(Msg);
    DispatchMessage(Msg);
  end;
end;

var
  ManifestVersion:  String;
  ManifestFileName: String;
  ManifestUrl_:     String;
  ManifestSHA256:   String;
  ManifestSize:     Int64;

  ProgressPage:     TOutputProgressWizardPage;
  ConfirmPage:      TOutputMsgWizardPage;
  CachedFilePath:   String;
  DidSkipDownload:  Boolean;
  ManifestLoaded:   Boolean;

// ---------------------------------------------------------------------------
// Minimal JSON value extractor
// ---------------------------------------------------------------------------
function JsonGetValue(const Json, Key: String): String;
var
  SearchKey: String;
  P, StartPos, EndPos: Integer;
begin
  Result := '';
  SearchKey := '"' + Key + '"';
  P := Pos(SearchKey, Json);
  if P = 0 then
    Exit;

  P := P + Length(SearchKey);
  while (P <= Length(Json)) and (Json[P] <> ':') do
    P := P + 1;
  P := P + 1;

  while (P <= Length(Json)) and ((Json[P] = ' ') or (Json[P] = #9) or (Json[P] = #10) or (Json[P] = #13)) do
    P := P + 1;

  if P > Length(Json) then
    Exit;

  if Json[P] = '"' then begin
    StartPos := P + 1;
    EndPos := StartPos;
    while (EndPos <= Length(Json)) and (Json[EndPos] <> '"') do
      EndPos := EndPos + 1;
    Result := Copy(Json, StartPos, EndPos - StartPos);
  end else begin
    StartPos := P;
    EndPos := StartPos;
    while (EndPos <= Length(Json)) and (Json[EndPos] <> ',') and (Json[EndPos] <> '}') and (Json[EndPos] <> ' ') and (Json[EndPos] <> #13) and (Json[EndPos] <> #10) do
      EndPos := EndPos + 1;
    Result := Copy(Json, StartPos, EndPos - StartPos);
  end;
end;

function FormatSize(Bytes: Int64): String;
begin
  if Bytes >= 1073741824 then
    Result := Format('%.2f GB', [Bytes / 1073741824.0])
  else if Bytes >= 1048576 then
    Result := Format('%.1f MB', [Bytes / 1048576.0])
  else
    Result := Format('%d KB', [Bytes div 1024]);
end;

// ---------------------------------------------------------------------------
// Helper: Fetch and parse manifest.json dynamically from cloud URL
// ---------------------------------------------------------------------------
function FetchManifest: Boolean;
var
  ManifestJson: AnsiString;
  ManifestTmpPath: String;
begin
  if ManifestLoaded then begin
    Result := True;
    Exit;
  end;

  Result := False;
  try
    DownloadTemporaryFile('{#ManifestUrl}', 'manifest.json', '', nil);
  except
    MsgBox('Failed to download the installer manifest.' + #13#10 + #13#10 +
           'Please check your internet connection and try again.' + #13#10 + #13#10 +
           'Technical details: ' + GetExceptionMessage,
           mbCriticalError, MB_OK);
    Exit;
  end;

  ManifestTmpPath := ExpandConstant('{tmp}\manifest.json');
  if not LoadStringFromFile(ManifestTmpPath, ManifestJson) then begin
    MsgBox('Failed to read the downloaded manifest file.', mbCriticalError, MB_OK);
    Exit;
  end;

  ManifestVersion  := Trim(JsonGetValue(ManifestJson, 'version'));
  ManifestFileName := Trim(JsonGetValue(ManifestJson, 'fileName'));
  ManifestUrl_     := Trim(JsonGetValue(ManifestJson, 'url'));
  ManifestSHA256   := Lowercase(Trim(JsonGetValue(ManifestJson, 'sha256')));
  ManifestSize     := StrToInt64Def(Trim(JsonGetValue(ManifestJson, 'sizeBytes')), 0);

  if (ManifestFileName = '') or (ManifestUrl_ = '') or (ManifestSHA256 = '') then begin
    MsgBox('The installer manifest is incomplete or malformed.' + #13#10 +
           'Please contact the developer.',
           mbCriticalError, MB_OK);
    Exit;
  end;

  ManifestLoaded := True;
  Result := True;
end;

// ---------------------------------------------------------------------------
// Setup Wizard pages
// ---------------------------------------------------------------------------
procedure InitializeWizard;
begin
  ManifestLoaded := False;

  // Download progress page
  ProgressPage := CreateOutputProgressPage('Downloading Installer', 'Downloading Hydra Manga TL full installer directly to your chosen folder...');

  // Confirmation page shown after download / cache hit
  ConfirmPage := CreateOutputMsgPage(
    wpReady,
    'Ready to Install',
    'The full installer is downloaded and verified.',
    'Click Next to launch the ' + '{#MyAppName}' + ' installer.');
end;

// ---------------------------------------------------------------------------
// Execution flow: Welcome -> SelectDir (dynamic space text) -> Download -> Confirm -> Launch
// ---------------------------------------------------------------------------
function NextButtonClick(CurPageID: Integer): Boolean;
var
  TargetDir: String;
  TargetDrive: String;
  FreeBytes, TotalBytes: Int64;
  ExistingHash: String;
  ResultCode: Integer;
  PS1Code: AnsiString;
  ProgStr: AnsiString;
  ProgVal: Integer;
  StrSplit: Integer;
  ProgText: String;
begin
  Result := True;

  // Step 1: On Welcome page Next, fetch manifest and update folder page free space text dynamically
  if CurPageID = wpWelcome then begin
    if not FetchManifest then begin
      Result := False;
      Exit;
    end;

    // Dynamically update the disk space label on the Directory Page from JSON sizeBytes
    if ManifestSize > 0 then
      WizardForm.DiskSpaceLabel.Caption := 'At least ' + FormatSize(ManifestSize) + ' of free disk space is required.';
  end;

  // Step 2: On Select Directory page Next, check free disk space dynamically & download
  if CurPageID = wpSelectDir then begin
    if not FetchManifest then begin
      Result := False;
      Exit;
    end;

    TargetDir := ExpandConstant('{app}');
    TargetDrive := ExtractFileDrive(TargetDir);

    // Dynamically check free disk space on the target drive against sizeBytes from JSON
    if (ManifestSize > 0) and GetSpaceOnDisk64(TargetDrive, FreeBytes, TotalBytes) then begin
      if FreeBytes < ManifestSize then begin
        MsgBox('There is not enough free disk space on drive ' + TargetDrive + '.' + #13#10 + #13#10 +
               'Required space: ' + FormatSize(ManifestSize) + #13#10 +
               'Available space: ' + FormatSize(FreeBytes) + #13#10 + #13#10 +
               'Please select a different folder or free up space.',
               mbCriticalError, MB_OK);
        Result := False;
        Exit;
      end;
    end;

    CachedFilePath := AddBackslash(TargetDir) + ManifestFileName;
    DidSkipDownload := False;

    if FileExists(CachedFilePath) then begin
      Log('Cached file found: ' + CachedFilePath);
      ExistingHash := Lowercase(GetSHA256OfFile(CachedFilePath));
      if ExistingHash = ManifestSHA256 then begin
        Log('Cache hit — SHA-256 matches, skipping download.');
        DidSkipDownload := True;
      end else begin
        Log('Cache miss — SHA-256 mismatch, re-downloading.');
        DeleteFile(CachedFilePath);
      end;
    end;

    // Download if needed directly to the target directory (bypassing {tmp} on C: drive)
    if not DidSkipDownload then begin
      ForceDirectories(TargetDir);
      
      ProgressPage.SetText('Downloading files...', ManifestFileName);
      ProgressPage.ProgressBar.Style := npbstNormal; 
      ProgressPage.SetProgress(0, 100);
      ProgressPage.Show;
      try
        // Delete old status files
        DeleteFile(ExpandConstant('{tmp}\progress.txt'));
        DeleteFile(ExpandConstant('{tmp}\done.txt'));
        DeleteFile(ExpandConstant('{tmp}\error.txt'));
        
        // Generate the PowerShell script for chunked streaming, writing progress to a file
        PS1Code :=
          '[CmdletBinding()]' + #13#10 +
          'param()' + #13#10 +
          'try {' + #13#10 +
          '$url = "' + ManifestUrl_ + '"' + #13#10 +
          '$path = "' + CachedFilePath + '"' + #13#10 +
          '$progFile = "' + ExpandConstant('{tmp}\progress.txt') + '"' + #13#10 +
          '$doneFile = "' + ExpandConstant('{tmp}\done.txt') + '"' + #13#10 +
          '$errFile = "' + ExpandConstant('{tmp}\error.txt') + '"' + #13#10 +
          '$request = [System.Net.HttpWebRequest]::Create($url)' + #13#10 +
          '$response = $request.GetResponse()' + #13#10 +
          '$total = $response.ContentLength' + #13#10 +
          '$stream = $response.GetResponseStream()' + #13#10 +
          '$fileStream = [System.IO.File]::Create($path)' + #13#10 +
          '$buffer = New-Object byte[] 65536' + #13#10 +
          '$downloaded = 0' + #13#10 +
          '$lastUpdate = [DateTime]::Now' + #13#10 +
          'while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {' + #13#10 +
          '    $fileStream.Write($buffer, 0, $read)' + #13#10 +
          '    $downloaded += $read' + #13#10 +
          '    if ($total -gt 0 -and ([DateTime]::Now - $lastUpdate).TotalMilliseconds -gt 250) {' + #13#10 +
          '        $percent = [math]::Floor(($downloaded / $total) * 100)' + #13#10 +
          '        $mbStr = "$([math]::Round($downloaded/1MB,1)) MB / $([math]::Round($total/1MB,1)) MB"' + #13#10 +
          '        [System.IO.File]::WriteAllText($progFile, "$percent|$mbStr")' + #13#10 +
          '        $lastUpdate = [DateTime]::Now' + #13#10 +
          '    }' + #13#10 +
          '}' + #13#10 +
          '$fileStream.Close()' + #13#10 +
          '$stream.Close()' + #13#10 +
          '[System.IO.File]::WriteAllText($doneFile, "OK")' + #13#10 +
          '} catch {' + #13#10 +
          '  if ($null -ne $fileStream) { $fileStream.Close() }' + #13#10 +
          '  if ($null -ne $stream) { $stream.Close() }' + #13#10 +
          '  [System.IO.File]::WriteAllText($errFile, $_.Exception.Message)' + #13#10 +
          '}';
        
        SaveStringToFile(ExpandConstant('{tmp}\download.ps1'), PS1Code, False);
        
        // Launch hidden powershell asynchronously
        if not Exec('powershell.exe', '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + ExpandConstant('{tmp}\download.ps1') + '"', '', SW_HIDE, ewNoWait, ResultCode) then begin
          MsgBox('Failed to launch the download process.', mbError, MB_OK);
          Result := False;
          Exit;
        end;
        
        // Custom message pump loop to keep UI responsive and poll progress
        while True do begin
          AppProcessMessages;
          Sleep(100);
          
          if FileExists(ExpandConstant('{tmp}\progress.txt')) then begin
            if LoadStringFromFile(ExpandConstant('{tmp}\progress.txt'), ProgStr) then begin
              ProgStr := Trim(String(ProgStr));
              StrSplit := Pos('|', String(ProgStr));
              if StrSplit > 0 then begin
                ProgVal := StrToIntDef(Copy(String(ProgStr), 1, StrSplit - 1), 0);
                ProgText := Copy(String(ProgStr), StrSplit + 1, Length(String(ProgStr)));
                ProgressPage.SetProgress(ProgVal, 100);
                ProgressPage.SetText('Downloading files...', ManifestFileName + ' (' + ProgText + ')');
              end;
            end;
          end;
          
          if FileExists(ExpandConstant('{tmp}\done.txt')) then Break;
          if FileExists(ExpandConstant('{tmp}\error.txt')) then begin
            LoadStringFromFile(ExpandConstant('{tmp}\error.txt'), ProgStr);
            MsgBox('Download failed:' + #13#10 + String(ProgStr), mbError, MB_OK);
            Result := False;
            Exit;
          end;
        end;
        
      finally
        ProgressPage.Hide;
      end;
    end;

    // Update confirmation message
    if DidSkipDownload then
      ConfirmPage.MsgLabel.Caption :=
        '{#MyAppName} v' + ManifestVersion + ' installer was found in your selected folder ' +
        'and its integrity has been verified.' + #13#10 + #13#10 +
        'Folder: ' + TargetDir + #13#10 +
        'File: ' + ManifestFileName + #13#10 +
        'Size: ' + FormatSize(ManifestSize) + #13#10 + #13#10 +
        'Click Next to launch the installer.'
    else
      ConfirmPage.MsgLabel.Caption :=
        '{#MyAppName} v' + ManifestVersion + ' installer has been downloaded ' +
        'and verified successfully.' + #13#10 + #13#10 +
        'Download Location: ' + TargetDir + #13#10 +
        'File: ' + ManifestFileName + #13#10 +
        'Size: ' + FormatSize(ManifestSize) + #13#10 + #13#10 +
        'Click Next to launch the installer.';
  end;

  // Launch offline installer on final click
  if CurPageID = ConfirmPage.ID then begin
    Log('Launching installer: ' + CachedFilePath);
    if not Exec(CachedFilePath, '', '', SW_SHOWNORMAL, ewNoWait, ResultCode) then begin
      MsgBox('Failed to launch the installer.' + #13#10 + #13#10 +
             'You can run it manually from:' + #13#10 +
             CachedFilePath,
             mbCriticalError, MB_OK);
    end;
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  if PageID = wpReady then
    Result := True
  else
    Result := False;
end;
