'use strict';

const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const { EventEmitter } = require('node:events');
const { spawn } = require('node:child_process');

function parseArguments(argv) {
  const result = { quitForInstall: argv.includes('--quit-for-install') };
  for (const [flag, key] of [['--data-dir', 'dataDir'], ['--codex-dir', 'codexDir'], ['--python', 'python']]) {
    const index = argv.indexOf(flag);
    if (index !== -1) {
      if (!argv[index + 1] || argv[index + 1].startsWith('--')) throw new Error(`${flag} 경로가 필요합니다.`);
      result[key] = argv[index + 1];
    }
  }
  return result;
}

function directory(input) {
  if (typeof input !== 'string' || !input.trim() || input.includes('\0')) throw new Error('올바른 폴더 경로가 필요합니다.');
  let expanded = input.trim();
  if (expanded === '~' || /^~[\\/]/.test(expanded)) expanded = path.join(os.homedir(), expanded.slice(2));
  if (!path.isAbsolute(expanded)) throw new Error('폴더는 절대 경로로 지정하세요.');
  return path.normalize(expanded);
}

// Resolve an existing ancestor so a missing leaf under a symlink cannot bypass the boundary.
function canonicalPath(input) {
  let current = directory(input);
  const missing = [];
  while (!fs.existsSync(current)) {
    const parent = path.dirname(current);
    if (parent === current) break;
    missing.unshift(path.basename(current));
    current = parent;
  }
  return path.join(fs.realpathSync.native(current), ...missing);
}

function contains(parent, child) {
  const relative = path.relative(parent, child);
  return !relative || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative));
}

function validateStorage(dataInput, codexInput, resourceDirectories) {
  const dataDir = directory(dataInput), codexDir = directory(codexInput);
  const data = canonicalPath(dataDir), codex = canonicalPath(codexDir);
  const resources = resourceDirectories.map(canonicalPath);
  for (const source of resources) {
    if (contains(source, codex) || contains(codex, source)) throw new Error('Codex 입력 폴더와 앱 리소스 폴더는 겹칠 수 없습니다.');
  }
  for (const [label, source] of [['Codex', codex], ...resources.map(p => ['앱 리소스', p])]) {
    if (contains(source, data) || contains(data, source)) throw new Error(`데이터 폴더와 ${label} 폴더는 겹칠 수 없습니다.`);
  }
  for (const value of [dataDir, codexDir]) {
    if (fs.existsSync(value) && !fs.statSync(value).isDirectory()) throw new Error('폴더 경로에 파일이 있습니다.');
  }
  return { dataDir, codexDir };
}

function defaultDataDirectory(env = process.env, platform = process.platform) {
  if (platform === 'win32') {
    if (!env.LOCALAPPDATA || !path.isAbsolute(env.LOCALAPPDATA)) throw new Error('LOCALAPPDATA 폴더를 확인하지 못했습니다.');
    return path.join(env.LOCALAPPDATA, 'Codex DAG');
  }
  // Development on macOS must not reuse the installed native app's writable data.
  return path.join(os.homedir(), '.codex-dag-windows-dev');
}

function validateAnnouncement(info, pid) {
  let url;
  try { url = new URL(info.url); } catch { throw new Error('수집기 시작 주소를 확인하지 못했습니다.'); }
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port || Number(url.port) < 1 || Number(url.port) > 65535 ||
      url.username || url.password || url.search || url.hash || url.pathname !== '/' ||
      typeof info.token !== 'string' || !info.token || info.token.length > 512 || /[\r\n]/.test(info.token) ||
      !Number.isInteger(info.pid) || info.pid !== pid || typeof info.started_at !== 'string') {
    throw new Error('수집기 시작 응답의 루프백 주소 또는 소유 PID가 올바르지 않습니다.');
  }
  return Object.freeze({ url: url.origin, token: info.token, pid: info.pid, startedAt: info.started_at });
}

function ownsURL(endpoint, input) {
  try {
    const url = new URL(input);
    return !!endpoint && url.origin === endpoint.url && url.hostname === '127.0.0.1' && !url.username && !url.password;
  } catch { return false; }
}

function requestJSON(endpoint, route, timeout = 3000) {
  return new Promise((resolve, reject) => {
    if (!['/api/health', '/api/summary'].includes(route) || !ownsURL(endpoint, endpoint?.url)) {
      reject(new Error('허용되지 않은 수집기 요청입니다.')); return;
    }
    // Node's HTTP client does not follow redirects. The token is sent only to the validated owned origin.
    const request = http.get(new URL(route, endpoint.url), { headers: { Authorization: `Bearer ${endpoint.token}`, Accept: 'application/json' } }, response => {
      if (response.statusCode !== 200) { response.resume(); reject(new Error('수집기 응답을 확인하지 못했습니다.')); return; }
      let bytes = 0, body = '';
      response.setEncoding('utf8');
      response.on('data', chunk => {
        bytes += Buffer.byteLength(chunk);
        if (bytes > 1024 * 1024) { request.destroy(new Error('수집기 상태 응답이 너무 큽니다.')); return; }
        body += chunk;
      });
      response.on('error', reject);
      response.on('end', () => { try { resolve(JSON.parse(body)); } catch { reject(new Error('수집기 상태 응답 형식이 잘못되었습니다.')); } });
    });
    request.setTimeout(timeout, () => request.destroy(new Error('수집기 응답 대기 시간이 초과되었습니다.')));
    request.on('error', () => reject(new Error('수집기에 연결하지 못했습니다.')));
  });
}

class Backend extends EventEmitter {
  constructor(options) {
    super();
    this.options = options;
    this.spawn = options.spawn || spawn;
    this.request = options.request || requestJSON;
    this.phase = 'stopped';
    this.child = null;
    this.endpoint = null;
    this.summary = null;
    this.error = null;
    this.stopping = null;
    this.generation = 0;
    this.pollTimer = null;
  }

  changed() { this.emit('change'); }
  log(text) { this.options.log?.(text); }

  async start(settings) {
    if (this.child || this.phase === 'starting' || this.stopping) return;
    const generation = ++this.generation;
    this.phase = 'starting'; this.error = null; this.summary = null; this.endpoint = null; this.pendingSummary = false; this.changed();
    try {
      const { dataDir, codexDir } = validateStorage(settings.dataDir, settings.codexDir, this.options.resourceDirectories);
      fs.mkdirSync(dataDir, { recursive: true });
      const entry = path.join(this.options.backendDir, 'monitor.py');
      if (!fs.statSync(entry).isFile() || !fs.statSync(this.options.python).isFile()) throw new Error('번들 Python 또는 수집기가 없습니다. 앱을 다시 설치하세요.');
      const child = this.spawn(this.options.python, ['-B', '-u', entry, '--data-dir', dataDir, '--codex-dir', codexDir,
        '--shared-journal', 'serve', '--port', '0', '--parent-pid', String(process.pid), '--control-stdin'], {
        cwd: this.options.backendDir, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
        env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONDONTWRITEBYTECODE: '1', PYTHONNOUSERSITE: '1',
          CODEX_DAG_APP_VERSION: require('./package.json').version }
      });
      this.child = child;
      child.on('error', () => this.log('Owned backend process error.'));
      child.on('exit', () => {
        if (this.child !== child) return;
        this.child = null; this.endpoint = null; this.summary = null;
        clearInterval(this.pollTimer); this.pollTimer = null;
        if (this.phase !== 'stopping' && this.phase !== 'failed') {
          this.phase = 'failed'; this.error = '수집기가 종료되었습니다. 로그를 확인하거나 다시 시작하세요.';
        }
        this.changed();
      });
      child.stderr.on('data', chunk => {
        const line = chunk.toString().replace(/(?:Bearer\s+|token[=:]\s*|codex_dag_token=)[^\s;"&]+/gi, '[redacted]');
        // Startup JSON is never logged, and an exact token in an error is removed as well.
        this.log(this.endpoint?.token ? line.split(this.endpoint.token).join('[redacted]') : 'Owned backend stderr received before readiness.');
      });
      child.stdin.on('error', () => {});
      const endpoint = await new Promise((resolve, reject) => {
        let buffer = '', settled = false;
        const finish = (error, value) => {
          if (settled) return; settled = true; clearTimeout(timer);
          child.stdout.removeListener('data', onData);
          child.removeListener('error', onError); child.removeListener('exit', onExit);
          error ? reject(error) : resolve(value);
        };
        const onError = () => finish(new Error('수집기 프로세스를 시작하지 못했습니다.'));
        const onExit = () => finish(new Error('수집기 시작 응답 전에 프로세스가 종료되었습니다.'));
        const onData = chunk => {
          buffer += chunk.toString();
          if (Buffer.byteLength(buffer) > 65536) return finish(new Error('수집기 시작 응답이 너무 큽니다.'));
          const newline = buffer.indexOf('\n');
          if (newline === -1) return;
          try { finish(null, validateAnnouncement(JSON.parse(buffer.slice(0, newline)), child.pid)); }
          catch { finish(new Error('수집기 시작 응답의 주소 또는 소유 PID가 올바르지 않습니다.')); }
        };
        const timer = setTimeout(() => finish(new Error('수집기가 제한 시간 안에 준비되지 않았습니다.')), this.options.readyTimeout || 15000);
        child.stdout.on('data', onData); child.once('error', onError); child.once('exit', onExit);
      });
      // Drain later stdout without recording it; no message bodies or credentials enter host logs.
      child.stdout.resume();
      if (this.child !== child || generation !== this.generation || this.phase !== 'starting') return;
      this.endpoint = endpoint;
      const deadline = Date.now() + (this.options.readyTimeout || 15000);
      let healthy = false;
      while (this.child === child && this.phase === 'starting' && Date.now() < deadline) {
        try {
          const health = await this.request(endpoint, '/api/health');
          if (health.pid !== child.pid) throw new Error('수집기 health PID가 소유 프로세스와 다릅니다.');
          if (health.status === 'ready' || health.status === 'ok') { healthy = true; break; }
        } catch (error) {
          if (error.message.includes('PID')) throw error;
        }
        await new Promise(resolve => setTimeout(resolve, 200));
      }
      if (this.child !== child || generation !== this.generation || this.phase !== 'starting') return;
      if (!healthy) throw new Error('수집기가 제한 시간 안에 준비되지 않았습니다.');
      this.phase = 'ready'; this.changed(); this.emit('ready', endpoint);
      this.log(`Owned backend ready pid=${child.pid}`);
      await this.fetchSummary();
      if (this.phase === 'ready') this.pollTimer = setInterval(() => this.fetchSummary(), this.options.pollInterval || 3000);
    } catch (error) {
      if (generation !== this.generation || this.phase === 'stopping') return;
      this.error = error.message; this.phase = 'failed'; this.changed();
      this.log('Owned backend startup failed.');
      await this.stop(true);
    }
  }

  async fetchSummary() {
    if (this.phase !== 'ready' || this.pendingSummary || !this.endpoint) return;
    const endpoint = this.endpoint, generation = this.generation;
    this.pendingSummary = true;
    try {
      const summary = await this.request(endpoint, '/api/summary');
      if (generation === this.generation && this.phase === 'ready') { this.summary = summary; this.error = null; this.changed(); }
    } catch {
      if (generation === this.generation && this.phase === 'ready') { this.error = '수집기 상태를 확인하지 못했습니다.'; this.changed(); }
    } finally { if (generation === this.generation) this.pendingSummary = false; }
  }

  stop(preserveFailure = false) {
    if (this.stopping) return this.stopping;
    ++this.generation; clearInterval(this.pollTimer); this.pollTimer = null;
    const child = this.child;
    if (!child) { this.endpoint = null; this.summary = null; if (!preserveFailure) this.phase = 'stopped'; this.changed(); return Promise.resolve(); }
    if (!preserveFailure) this.phase = 'stopping'; this.changed();
    const operation = new Promise((resolve, reject) => {
      let finished = false, killWatchdog;
      const finish = () => {
        if (finished) return; finished = true; clearTimeout(timer); clearTimeout(killWatchdog);
        child.removeListener('exit', finish);
        if (this.child === child) this.child = null;
        this.endpoint = null; this.summary = null;
        if (!preserveFailure) this.phase = 'stopped';
        this.log(`Stopped owned backend pid=${child.pid || 'unavailable'}`); this.changed(); resolve();
      };
      const timer = setTimeout(() => {
        // No PID read from disk, process tree kill, or unrelated monitor is ever used here.
        let killed = true;
        if (child.exitCode == null && child.signalCode == null) {
          try { killed = child.kill('SIGKILL'); } catch { killed = false; }
        }
        if (child.exitCode != null || child.signalCode != null) { finish(); return; }
        // A failed kill is not an exit. Keep the exact child handle so restart cannot create a duplicate.
        const fail = () => {
          if (finished) return;
          finished = true; child.removeListener('exit', finish);
          this.phase = 'failed'; this.error = '소유 수집기를 종료하지 못했습니다. 앱을 종료하기 전에 다시 중지하세요.';
          this.changed(); reject(new Error(this.error));
        };
    if (!killed) fail();
        else killWatchdog = setTimeout(fail, this.options.killTimeout || 1000);
      }, this.options.stopTimeout || 3000);
      child.once('exit', finish);
      if (child.exitCode != null || child.signalCode != null || !child.pid) { finish(); return; }
      try { child.stdin.end('shutdown\n'); } catch { /* The timeout will stop this exact child. */ }
    });
    this.stopping = operation.finally(() => { this.stopping = null; });
    return this.stopping;
  }

  async restart(settings) { await this.stop(); await this.start(settings); }
}

module.exports = { Backend, parseArguments, directory, canonicalPath, contains, validateStorage, defaultDataDirectory,
  validateAnnouncement, ownsURL, requestJSON };
