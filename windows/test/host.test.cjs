'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const { EventEmitter } = require('node:events');
const { PassThrough, Writable } = require('node:stream');
const { Backend, validateAnnouncement, validateStorage, ownsURL, parseArguments, requestJSON } = require('../backend.cjs');
const { windowPreferences, trustedSettingsEvent, secureContents, readSettings, writeSettings } = require('../main.cjs');

const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
class Child extends EventEmitter {
  constructor(options = {}) {
    super(); this.pid = 9123; this.exitCode = null; this.signalCode = null;
    this.stdout = new PassThrough(); this.stderr = new PassThrough(); this.commands = []; this.kills = [];
    this.stdin = new Writable({ write: (chunk, _encoding, callback) => { this.commands.push(chunk.toString()); callback(); } });
    this.stdin.on('finish', () => { if (options.graceful !== false) setImmediate(() => this.exit(0)); });
    this.killResult = options.killResult !== false;
  }
  exit(code = 0) { this.exitCode = code; this.emit('exit', code); }
  kill(signal) {
    this.kills.push(signal);
    if (this.killResult) setImmediate(() => { this.signalCode = signal; this.emit('exit', null, signal); });
    else this.emit('error', new Error('kill failed'));
    return this.killResult;
  }
}

function fixture(t, options = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-dag-host-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const backendDir = path.join(directory, 'backend'), python = path.join(directory, 'python');
  fs.mkdirSync(backendDir); fs.writeFileSync(path.join(backendDir, 'monitor.py'), ''); fs.writeFileSync(python, '');
  const settings = { dataDir: path.join(directory, 'data'), codexDir: path.join(directory, 'codex') };
  const logs = [], spawns = [];
  const child = options.child || new Child();
  const backend = new Backend({ backendDir, python, resourceDirectories: [backendDir], readyTimeout: 80,
    stopTimeout: 20, killTimeout: 20, pollInterval: 5000, log: value => logs.push(value),
    spawn: (executable, args, spawnOptions) => {
      spawns.push({ executable, args, options: spawnOptions });
      setImmediate(() => child.stdout.write(options.announcement || JSON.stringify({ url: 'http://127.0.0.1:58999',
        pid: child.pid, token: 'secret-token-do-not-log', started_at: '2026-10-06T01:00:00Z' }) + '\n'));
      return child;
    }, request: async (_endpoint, route) => route === '/api/health' ? { pid: child.pid, status: 'ready' } :
      { project_path: '/project', root_session_name: 'Actual name', live: { running_agents: 2 }, collection: { error: null } },
    ...options.overrides });
  t.after(async () => { if (backend.child) { child.killResult = true; await backend.stop(); } });
  return { backend, child, settings, logs, spawns, directory, backendDir };
}

test('startup response requires exact owned PID, valid loopback origin and safe token', () => {
  const good = { url: 'http://127.0.0.1:1234', token: 'token', pid: 5, started_at: 'now' };
  assert.equal(validateAnnouncement(good, 5).url, 'http://127.0.0.1:1234');
  for (const change of [{ pid: 6 }, { url: 'http://localhost:1234' }, { url: 'https://127.0.0.1:1234' },
    { url: 'http://user:pass@127.0.0.1:1234' }, { url: 'http://127.0.0.1:1234/?token=x' },
    { url: 'http://127.0.0.1:1234/other' }, { token: 'bad\r\nvalue' }, { token: '' }]) {
    assert.throws(() => validateAnnouncement({ ...good, ...change }, 5));
  }
  const endpoint = validateAnnouncement(good, 5);
  assert.equal(ownsURL(endpoint, 'http://127.0.0.1:1234/?token=token'), true);
  assert.equal(ownsURL(endpoint, 'http://127.0.0.1:4321/'), false);
  assert.equal(ownsURL(endpoint, 'http://localhost:1234/'), false);
  assert.equal(ownsURL(endpoint, 'file:///etc/passwd'), false);
});

test('owned launch arguments and graceful stdin shutdown never use stale PID files', async t => {
  const { backend, child, settings, spawns, logs } = fixture(t);
  fs.mkdirSync(settings.dataDir); fs.writeFileSync(path.join(settings.dataDir, '.server.json'), JSON.stringify({ pid: process.pid }));
  await backend.start(settings);
  assert.equal(backend.phase, 'ready'); assert.equal(backend.summary.live.running_agents, 2);
  const launch = spawns[0];
  assert.deepEqual(launch.args.slice(-6), ['serve', '--port', '0', '--parent-pid', String(process.pid), '--control-stdin']);
  assert.equal(launch.options.windowsHide, true); assert.equal(launch.options.stdio[0], 'pipe');
  assert.equal(launch.options.env.PYTHONDONTWRITEBYTECODE, '1');
  await backend.start(settings); assert.equal(spawns.length, 1);
  child.stderr.write('Bearer secret-token-do-not-log\n');
  await backend.stop();
  assert.deepEqual(child.commands, ['shutdown\n']); assert.deepEqual(child.kills, []);
  assert.equal(backend.child, null); assert.equal(backend.phase, 'stopped');
  assert.ok(!logs.join('\n').includes('secret-token-do-not-log'));
  assert.equal(JSON.parse(fs.readFileSync(path.join(settings.dataDir, '.server.json'))).pid, process.pid);
});

test('timeout kills only the child actually spawned by this host', async t => {
  const child = new Child({ graceful: false });
  const { backend, settings } = fixture(t, { child });
  await backend.start(settings); await backend.stop();
  assert.deepEqual(child.kills, ['SIGKILL']); assert.equal(backend.child, null);
});

test('kill error retains owned process and restart cannot create a duplicate', async t => {
  const child = new Child({ graceful: false, killResult: false });
  const { backend, settings, spawns } = fixture(t, { child });
  await backend.start(settings);
  await assert.rejects(backend.stop());
  assert.equal(backend.child, child); assert.equal(backend.phase, 'failed');
  assert.ok(backend.error.includes('종료하지 못했습니다'));
  await backend.start(settings); assert.equal(spawns.length, 1);
  await assert.rejects(backend.restart(settings)); assert.equal(spawns.length, 1);
});

test('mismatched health PID stops owned process without becoming ready', async t => {
  const { backend, settings, child } = fixture(t, { overrides: { request: async () => ({ status: 'ready', pid: 7777 }) } });
  let ready = false; backend.on('ready', () => { ready = true; });
  await backend.start(settings);
  assert.equal(ready, false); assert.equal(backend.phase, 'failed'); assert.equal(backend.child, null);
  assert.deepEqual(child.commands, ['shutdown\n']);
});

test('invalid or oversized startup announcement is discarded without logging credentials', async t => {
  for (const announcement of ['{"url":"https://evil.invalid","token":"do-not-log"}\n', 'x'.repeat(66000)]) {
    const { backend, settings, logs } = fixture(t, { announcement });
    await backend.start(settings);
    assert.equal(backend.phase, 'failed'); assert.equal(backend.child, null);
    assert.ok(!logs.join('\n').includes('do-not-log'));
  }
});

test('stop during pending startup suppresses readiness and is idempotent', async t => {
  const { backend, settings, child } = fixture(t, { announcement: '' });
  const starting = backend.start(settings);
  await delay(5);
  const stopping = backend.stop(); assert.equal(backend.stop(), stopping);
  await stopping; await starting;
  assert.equal(backend.phase, 'stopped'); assert.equal(backend.endpoint, null);
  assert.deepEqual(child.commands, ['shutdown\n']);
});

test('symlink and ancestor overlap cannot put writable data in input or installation folders', t => {
  const { directory, backendDir, settings } = fixture(t);
  fs.mkdirSync(settings.codexDir);
  assert.throws(() => validateStorage(settings.codexDir, settings.codexDir, [backendDir]));
  assert.throws(() => validateStorage(path.join(settings.codexDir, 'new'), settings.codexDir, [backendDir]));
  assert.throws(() => validateStorage(directory, settings.codexDir, [backendDir]));
  assert.throws(() => validateStorage(settings.dataDir, path.join(backendDir, 'input'), [backendDir]));
  const link = path.join(directory, 'link'); fs.symlinkSync(settings.codexDir, link, process.platform === 'win32' ? 'junction' : 'dir');
  assert.throws(() => validateStorage(path.join(link, 'missing'), settings.codexDir, [backendDir]));
  assert.deepEqual(validateStorage(settings.dataDir, settings.codexDir, [backendDir]), settings);
});

test('settings IPC requires exact window, main frame and local file URL', () => {
  const mainFrame = { url: 'file:///app/settings.html' }, webContents = { mainFrame };
  const window = { webContents, isDestroyed: () => false };
  const event = { sender: webContents, senderFrame: mainFrame };
  assert.equal(trustedSettingsEvent(event, window, mainFrame.url), true);
  assert.equal(trustedSettingsEvent({ ...event, sender: {} }, window, mainFrame.url), false);
  assert.equal(trustedSettingsEvent({ ...event, senderFrame: { url: mainFrame.url } }, window, mainFrame.url), false);
  assert.equal(trustedSettingsEvent(event, window, 'file:///other/settings.html'), false);
  assert.equal(trustedSettingsEvent(event, { ...window, isDestroyed: () => true }, mainFrame.url), false);
});

test('renderer has no Node access, denies external navigation, windows and permissions', () => {
  const preferences = windowPreferences('/preload');
  assert.equal(preferences.nodeIntegration, false); assert.equal(preferences.contextIsolation, true);
  assert.equal(preferences.sandbox, true); assert.equal(preferences.webviewTag, false);
  const contents = new EventEmitter(), session = new EventEmitter(); contents.session = session;
  contents.setWindowOpenHandler = handler => { contents.open = handler; };
  session.setPermissionRequestHandler = handler => { session.permission = handler; };
  session.setPermissionCheckHandler = handler => { session.check = handler; };
  secureContents(contents, url => url === 'http://127.0.0.1:1234/');
  let prevented = false; const event = { preventDefault: () => { prevented = true; } };
  contents.emit('will-navigate', event, 'https://evil.invalid'); assert.equal(prevented, true);
  prevented = false; contents.emit('will-navigate', event, 'http://127.0.0.1:1234/'); assert.equal(prevented, false);
  contents.emit('will-redirect', event, 'file:///private'); assert.equal(prevented, true);
  assert.deepEqual(contents.open(), { action: 'deny' });
  session.permission(null, 'camera', value => assert.equal(value, false)); assert.equal(session.check(), false);
});

test('settings persist without modifying Codex files; invalid source config remains untouched', t => {
  const { directory, backendDir, settings } = fixture(t);
  const filename = path.join(directory, 'desktop-settings.json');
  const expected = { ...settings, startOnLaunch: false, openWindowOnLaunch: true };
  writeSettings(filename, expected);
  assert.deepEqual(readSettings(filename, { dataDir: settings.dataDir }, [backendDir]), expected);
  assert.equal(fs.existsSync(settings.codexDir), false);
  fs.writeFileSync(filename, '{invalid');
  assert.throws(() => readSettings(filename, { dataDir: settings.dataDir }, [backendDir]));
  assert.equal(fs.readFileSync(filename, 'utf8'), '{invalid');
});

test('installer shutdown flag is explicit and missing development paths are rejected', () => {
  assert.deepEqual(parseArguments(['--quit-for-install']), { quitForInstall: true });
  assert.equal(parseArguments(['--data-dir', '/isolated', '--python', '/python']).python, '/python');
  assert.throws(() => parseArguments(['--codex-dir', '--python', '/python']));
});

test('authenticated lightweight requests never follow redirects or accept arbitrary routes', async t => {
  const requests = [];
  const server = http.createServer((request, response) => {
    requests.push({ path: request.url, auth: request.headers.authorization });
    response.writeHead(302, { Location: 'https://example.invalid/?token=secret' }); response.end();
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const endpoint = { url: `http://127.0.0.1:${server.address().port}`, token: 'secret' };
  await assert.rejects(requestJSON(endpoint, '/api/summary'));
  assert.deepEqual(requests, [{ path: '/api/summary', auth: 'Bearer secret' }]);
  await assert.rejects(requestJSON(endpoint, 'https://evil.invalid/'));
  assert.equal(requests.length, 1);
});
