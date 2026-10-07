'use strict';

const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { pathToFileURL } = require('node:url');
const { Backend, parseArguments, defaultDataDirectory, directory, validateStorage, ownsURL } = require('./backend.cjs');

function windowPreferences(preload) {
  return { nodeIntegration: false, nodeIntegrationInWorker: false, nodeIntegrationInSubFrames: false,
    contextIsolation: true, sandbox: true, webSecurity: true, allowRunningInsecureContent: false,
    webviewTag: false, ...(preload ? { preload } : {}) };
}

function trustedSettingsEvent(event, settingsWindow, settingsURL) {
  return !!settingsWindow && !settingsWindow.isDestroyed() && event.sender === settingsWindow.webContents &&
    event.senderFrame === settingsWindow.webContents.mainFrame && event.senderFrame?.url === settingsURL;
}

function readSettings(filename, options, resources) {
  let saved = {};
  if (fs.existsSync(filename)) {
    saved = JSON.parse(fs.readFileSync(filename, 'utf8'));
    if (!saved || typeof saved !== 'object' || Array.isArray(saved)) throw new Error('설정 파일 형식이 잘못되었습니다. 기존 파일을 보존했습니다.');
  }
  const dirs = validateStorage(options.dataDir || defaultDataDirectory(), options.codexDir || saved.codexDir || path.join(os.homedir(), '.codex'), resources);
  return { ...dirs, startOnLaunch: saved.startOnLaunch !== false, openWindowOnLaunch: saved.openWindowOnLaunch !== false };
}

function writeSettings(filename, settings) {
  const temporary = `${filename}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify({ codexDir: settings.codexDir, startOnLaunch: settings.startOnLaunch,
    openWindowOnLaunch: settings.openWindowOnLaunch }, null, 2) + '\n', { mode: 0o600 });
  fs.renameSync(temporary, filename);
}

function secureContents(contents, owns) {
  const navigation = (event, url) => { if (!owns(url)) event.preventDefault(); };
  contents.on('will-navigate', navigation);
  contents.on('will-redirect', navigation);
  contents.on('will-attach-webview', event => event.preventDefault());
  contents.setWindowOpenHandler(() => ({ action: 'deny' }));
  contents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  contents.session.setPermissionCheckHandler(() => false);
  contents.session.on('will-download', event => event.preventDefault());
}

async function launch() {
  const { app, BrowserWindow, Tray, Menu, nativeImage, ipcMain, dialog, shell } = require('electron');
  const options = parseArguments(process.argv.slice(1));
  const dataDir = directory(options.dataDir || defaultDataDirectory());
  const backendDir = app.isPackaged ? path.join(process.resourcesPath, 'backend') : path.join(__dirname, '..', 'src', 'monitor');
  const resources = app.isPackaged ? [process.resourcesPath, path.dirname(process.execPath)] : [__dirname, backendDir];
  const filename = path.join(dataDir, 'desktop-settings.json');
  // Validate persisted input selection before any Electron cache write, too.
  const initialSettings = options.quitForInstall ? null : readSettings(filename, { ...options, dataDir }, resources);
  const dirs = initialSettings || validateStorage(dataDir, options.codexDir || path.join(os.homedir(), '.codex'), resources);
  // Configure before app readiness and locking so development profiles cannot reuse installed app caches.
  fs.mkdirSync(dirs.dataDir, { recursive: true });
  const userData = path.join(dirs.dataDir, 'electron');
  fs.mkdirSync(userData, { recursive: true });
  const sessionData = path.join(userData, 'cache');
  fs.mkdirSync(sessionData, { recursive: true });
  app.setPath('userData', userData);
  app.setPath('sessionData', sessionData);
  app.setAppUserModelId('CodexDAG.Desktop');
  const primary = app.requestSingleInstanceLock({ quitForInstall: options.quitForInstall });
  if (!primary || options.quitForInstall) {
    // A fresh --quit-for-install invocation must never launch a collector.
    app.quit(); return;
  }
  let quitting = false, quitFinished = false, monitorWindow = null, settingsWindow = null, tray = null;
  let readyToShow = false, backend = null, settings = null;
  const settingsURL = pathToFileURL(path.join(__dirname, 'settings.html')).href;
  const logDirectory = path.join(dirs.dataDir, 'logs');
  const logFile = path.join(logDirectory, 'desktop.log');
  const log = line => {
    try {
      fs.mkdirSync(logDirectory, { recursive: true });
      if (fs.existsSync(logFile) && fs.statSync(logFile).size > 5_000_000) {
        fs.rmSync(`${logFile}.1`, { force: true }); fs.renameSync(logFile, `${logFile}.1`);
      }
      fs.appendFileSync(logFile, `${new Date().toISOString()} ${line}${line.endsWith('\n') ? '' : '\n'}`, { mode: 0o600 });
    } catch { /* A log error must not change process ownership. */ }
  };
  const run = task => task().catch(() => dialog.showErrorBox('Codex DAG', '작업을 완료하지 못했습니다. 로그와 수집기 상태를 확인하세요.'));
  const showWaiting = () => {
    if (!monitorWindow || monitorWindow.isDestroyed()) return;
    monitorWindow.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent('<!doctype html><html lang="ko"><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'"><title>Codex DAG</title><body style="background:#0b0f12;color:#d7e0e7;font:16px system-ui;padding:50px"><h1>Codex DAG</h1><p>수집기를 준비하는 중이거나 중지된 상태입니다.</p><p>트레이 메뉴에서 수집기 시작 또는 설정을 선택하세요.</p></body></html>')).catch(() => {});
  };
  const openMonitor = () => {
    if (!readyToShow) return;
    if (!monitorWindow || monitorWindow.isDestroyed()) {
      monitorWindow = new BrowserWindow({ width: 1320, height: 900, minWidth: 760, minHeight: 550, show: false,
        title: 'Codex DAG · 실행 모니터', backgroundColor: '#0b0f12', autoHideMenuBar: true, webPreferences: windowPreferences() });
      secureContents(monitorWindow.webContents, url => ownsURL(backend.endpoint, url));
      monitorWindow.on('close', event => { if (!quitting) { event.preventDefault(); monitorWindow.hide(); } });
      monitorWindow.on('closed', () => { monitorWindow = null; });
      if (backend.endpoint) loadMonitor(backend.endpoint); else showWaiting();
    }
    if (monitorWindow.isMinimized()) monitorWindow.restore();
    monitorWindow.show(); monitorWindow.focus();
  };
  const loadMonitor = endpoint => {
    if (!monitorWindow || monitorWindow.isDestroyed()) return;
    const url = new URL('/', endpoint.url); url.searchParams.set('token', endpoint.token);
    monitorWindow.loadURL(url.href).catch(() => log('Monitor window load failed.'));
  };
  const openSettings = () => {
    if (!readyToShow) return;
    if (settingsWindow && !settingsWindow.isDestroyed()) { settingsWindow.show(); settingsWindow.focus(); return; }
    settingsWindow = new BrowserWindow({ width: 720, height: 470, resizable: false, show: false, title: 'Codex DAG 설정',
      backgroundColor: '#0b0f12', autoHideMenuBar: true,
      webPreferences: windowPreferences(path.join(__dirname, 'preload.cjs')) });
    secureContents(settingsWindow.webContents, url => url === settingsURL);
    settingsWindow.once('ready-to-show', () => settingsWindow?.show());
    settingsWindow.on('closed', () => { settingsWindow = null; });
    settingsWindow.loadFile(path.join(__dirname, 'settings.html')).catch(() => log('Settings window load failed.'));
  };
  const refreshTray = () => {
    if (!tray || !backend) return;
    const summary = backend.summary || {};
    const agents = Number.isInteger(summary.live?.running_agents) ? summary.live.running_agents : null;
    const unknown = Number.isInteger(summary.live?.unknown_agents) ? summary.live.unknown_agents : 0;
    const phaseNames = { stopped: '중지됨', starting: '시작 중', ready: '수집 중', stopping: '종료 중', failed: '오류' };
    tray.setToolTip(`Codex DAG · ${phaseNames[backend.phase]}${agents == null ? '' : ` · 에이전트 ${agents}명 실행 중`}`);
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: `수집기 · ${phaseNames[backend.phase]}`, enabled: false },
      { label: agents == null ? '실행 에이전트 · 확인 전' : `실행 에이전트 · ${agents}명`, enabled: false },
      ...(unknown ? [{ label: `상태 미확인 에이전트 · ${unknown}명`, enabled: false }] : []),
      ...(summary.project_path ? [{ label: `프로젝트 · ${path.basename(summary.project_path)}`, enabled: false }] : []),
      ...(summary.root_session_name ? [{ label: `세션 · ${String(summary.root_session_name).slice(0, 90)}`, enabled: false }] : []),
      ...(summary.collection?.error ? [{ label: '기록 수집 오류 · 모니터 창에서 확인', enabled: false }] : []),
      ...(backend.error ? [{ label: backend.error, enabled: false }] : []),
      { type: 'separator' }, { label: '모니터 창 열기', click: openMonitor },
      { label: '수집기 시작', enabled: !backend.child && !backend.stopping, click: () => run(() => backend.start(settings)) },
      { label: '수집기 중지', enabled: !!backend.child && !backend.stopping, click: () => run(() => backend.stop()) },
      { label: '수집기 재시작', enabled: !backend.stopping, click: () => run(() => backend.restart(settings)) },
      { type: 'separator' }, { label: '설정…', click: openSettings },
      { label: '로그 폴더 열기', click: () => { fs.mkdirSync(logDirectory, { recursive: true }); shell.openPath(logDirectory); } },
      { type: 'separator' }, { label: 'Codex DAG 종료', click: () => app.quit() }
    ]));
    if (!backend.endpoint && backend.phase !== 'starting') showWaiting();
  };
  app.on('second-instance', (_event, argv, _directory, additionalData) => {
    if (additionalData?.quitForInstall === true || argv.includes('--quit-for-install')) app.quit();
    else openMonitor();
  });
  app.on('before-quit', event => {
    if (quitFinished) return;
    event.preventDefault();
    if (quitting) return;
    quitting = true;
    Promise.resolve(backend?.stop()).then(() => { quitFinished = true; app.quit(); }, () => {
      quitting = false;
      dialog.showErrorBox('Codex DAG 종료 오류', '소유 수집기를 종료하지 못했습니다. 트레이에서 수집기 중지를 다시 시도하세요.');
    });
  });
  app.on('window-all-closed', () => {});
  app.on('activate', openMonitor);
  app.on('will-quit', () => tray?.destroy());
  await app.whenReady();
  if (quitting) return;
  settings = initialSettings;
  backend = new Backend({ backendDir, python: options.python || (app.isPackaged ? path.join(process.resourcesPath, 'python', 'python.exe') : process.platform === 'win32' ? 'python.exe' : '/usr/bin/python3'),
    resourceDirectories: resources, log });
  backend.on('change', refreshTray);
  backend.on('ready', endpoint => { loadMonitor(endpoint); refreshTray(); });
  // A valid PNG fallback keeps the tray usable even if an installation icon is unavailable.
  const fallback = nativeImage.createFromDataURL('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAAS0lEQVR4nGNgGJ7g/K3/tDMYHQ8dC7AZPvQswWcB1S3CZzHNAD6LhmYwDu1URzPDaRoHVEtFNMsHNE+CQzNVELKEqoDmFiBbNFQAAGTrUWjArDsZAAAAAElFTkSuQmCC');
  let icon = nativeImage.createFromPath(path.join(__dirname, 'assets', process.platform === 'win32' ? 'icon.ico' : 'tray.png'));
  if (icon.isEmpty()) icon = fallback.resize({ width: 20, height: 20 });
  tray = new Tray(icon);
  tray.on('click', openMonitor); tray.on('double-click', openMonitor);
  readyToShow = true;
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: 'Codex DAG', submenu: [
      { label: '모니터 창 열기', accelerator: 'CommandOrControl+O', click: openMonitor },
      { label: '설정…', accelerator: 'CommandOrControl+,', click: openSettings },
      { type: 'separator' }, { label: '종료', accelerator: 'CommandOrControl+Q', click: () => app.quit() }
    ] },
    { label: '수집기', submenu: [
      { label: '시작', click: () => run(() => backend.start(settings)) },
      { label: '중지', click: () => run(() => backend.stop()) },
      { label: '재시작', click: () => run(() => backend.restart(settings)) },
      { label: '로그 폴더 열기', click: () => { fs.mkdirSync(logDirectory, { recursive: true }); shell.openPath(logDirectory); } }
    ] },
    { label: '편집', submenu: [{ role: 'undo' }, { role: 'redo' }, { type: 'separator' }, { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' }] }
  ]));
  const assertSettings = event => {
    if (!trustedSettingsEvent(event, settingsWindow, settingsURL)) throw new Error('허용되지 않은 설정 요청입니다.');
  };
  ipcMain.handle('settings:get', event => { assertSettings(event); return settings; });
  ipcMain.handle('settings:browse', async (event, field) => {
    assertSettings(event);
    if (!['codexDir'].includes(field)) throw new Error('허용되지 않은 폴더 요청입니다.');
    const result = await dialog.showOpenDialog(settingsWindow, { title: 'Codex 데이터 폴더 선택',
      defaultPath: settings[field], properties: ['openDirectory'] });
    return result.canceled ? null : result.filePaths[0];
  });
  ipcMain.handle('settings:save', async (event, input) => {
    assertSettings(event);
    try {
      if (!input || typeof input !== 'object' || typeof input.startOnLaunch !== 'boolean' || typeof input.openWindowOnLaunch !== 'boolean') throw new Error('설정 값이 올바르지 않습니다.');
      const validated = validateStorage(settings.dataDir, input.codexDir, resources);
      const next = { ...validated, startOnLaunch: input.startOnLaunch, openWindowOnLaunch: input.openWindowOnLaunch };
      writeSettings(filename, next);
      settings = next;
      if (backend.child || settings.startOnLaunch) await backend.restart(settings);
      settingsWindow?.close();
      return { ok: true };
    } catch (error) { return { ok: false, error: error.message }; }
  });
  refreshTray();
  if (settings.openWindowOnLaunch) openMonitor();
  if (settings.startOnLaunch) await backend.start(settings);
}

// Electron's default app loads the package entry with require(), so require.main is its own bootstrap.
if (process.versions.electron && process.type === 'browser') launch().catch(() => {
  // Fixed message only: failed startup may contain a credential-bearing URL.
  const { app, dialog } = require('electron');
  app.whenReady().then(() => { dialog.showErrorBox('Codex DAG 시작 오류', '앱의 저장 경로 또는 런타임을 확인하세요.'); app.quit(); });
});

module.exports = { windowPreferences, trustedSettingsEvent, readSettings, writeSettings, secureContents };
