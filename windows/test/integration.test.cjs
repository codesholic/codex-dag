'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const { Backend } = require('../backend.cjs');

test('open-writer stable-mtime requests and UTF-8 messages reach live SSE without restart', {
  skip: !process.env.CODEX_DAG_TEST_PYTHON,
  timeout: 20000
}, async t => {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-dag-real-host-'));
  const settings = { dataDir: path.join(base, 'data'), codexDir: path.join(base, 'codex') };
  const project = path.join(base, 'project');
  fs.mkdirSync(project); fs.mkdirSync(settings.dataDir); fs.mkdirSync(path.join(settings.codexDir, 'sessions'), { recursive: true });
  const rollout = path.join(settings.codexDir, 'sessions', 'fixture.jsonl');
  const rows = [
    { timestamp: '2026-10-06T01:00:00Z', type: 'session_meta', payload: { id: 'root-fixture', cwd: project, agent_nickname: 'Windows Host Fixture' } },
    { timestamp: '2026-10-06T01:00:01Z', type: 'event_msg', payload: { type: 'task_started', turn_id: 'fixture-turn' } }
  ];
  fs.writeFileSync(rollout, rows.map(row => JSON.stringify(row) + '\n').join(''));
  fs.writeFileSync(path.join(settings.dataDir, 'config.json'), JSON.stringify({ project_path: project, root_session_id: 'root-fixture', journal_dir: settings.dataDir }));
  const frozenTime = new Date('2026-10-06T01:00:00Z');
  fs.utimesSync(rollout, frozenTime, frozenTime);
  const writer = fs.openSync(rollout, 'a');
  let writerOpen = true, tailTimer;
  const original = fs.readFileSync(rollout, 'utf8');
  const backendDir = path.resolve(__dirname, '../../src/monitor');
  const logs = [], backend = new Backend({ backendDir, python: process.env.CODEX_DAG_TEST_PYTHON,
    resourceDirectories: [backendDir], pollInterval: 10000, log: value => logs.push(value) });
  t.after(async () => { clearTimeout(tailTimer); if (writerOpen) { fs.closeSync(writer); writerOpen = false; } await backend.stop(); fs.rmSync(base, { recursive: true, force: true }); });
  await backend.start(settings);
  assert.equal(backend.phase, 'ready', backend.error || 'Backend failed readiness');
  assert.equal(backend.summary.project_path, project);
  assert.equal(backend.summary.live.running_agents, 1);
  assert.ok(!('activity' in backend.summary)); assert.ok(!('messages' in backend.summary));
  const pid = backend.child.pid, endpoint = backend.endpoint;
  const health = await backend.request(endpoint, '/api/health');
  assert.equal(health.version, require('../package.json').version, 'Host and backend build version differ');
  assert.equal(fs.readFileSync(rollout, 'utf8'), original, 'Collector changed a source rollout');
  const received = await new Promise((resolve, reject) => {
    let buffer = '', appended = false;
    const timer = setTimeout(() => { request.destroy(); reject(new Error('No live SSE update')); }, 5000);
    const request = http.get(new URL('/api/stream', endpoint.url), { headers: { Authorization: `Bearer ${endpoint.token}` } }, response => {
      assert.equal(response.statusCode, 200); response.setEncoding('utf8');
      response.on('data', chunk => {
        buffer += chunk;
        if (!appended && buffer.includes('data:')) {
          appended = true;
          const appendedRows = [
            { timestamp: '2026-10-06T01:00:02Z', type: 'event_msg', payload: { type: 'task_complete', turn_id: 'fixture-turn' } },
            { timestamp: '2026-10-06T01:00:03Z', type: 'event_msg', payload: { type: 'task_started', turn_id: 'live-turn' } },
            { timestamp: '2026-10-06T01:00:04Z', type: 'response_item', payload: { type: 'message', role: 'user', content: [{type: 'input_text', text: 'Windows open writer live request'}] } }
          ];
          fs.writeSync(writer, appendedRows.map(row => JSON.stringify(row) + '\n').join(''));
          const progress = Buffer.from(JSON.stringify({ timestamp: '2026-10-06T01:00:05Z', type: 'response_item', payload: {
            type: 'message', id: 'live-progress', role: 'assistant', phase: 'commentary',
            content: [{ type: 'output_text', text: 'Windows host live SSE evidence · 한글 실시간 메시지' }] } }) + '\r\n');
          const utf8Start = progress.indexOf(Buffer.from('한'));
          // Publish a partial UTF-8 character while leaving the writer open; complete it after a poll.
          fs.writeSync(writer, progress.subarray(0, utf8Start + 1)); fs.fsyncSync(writer);
          fs.utimesSync(rollout, frozenTime, frozenTime);
          tailTimer = setTimeout(() => {
            fs.writeSync(writer, progress.subarray(utf8Start + 1)); fs.fsyncSync(writer);
            fs.utimesSync(rollout, frozenTime, frozenTime);
          }, 1500);
        }
        const frames = buffer.split('\n\n');
        buffer = frames.pop();
        for (const frame of frames) {
          const line = frame.split('\n').find(value => value.startsWith('data: '));
          if (!line) continue;
          const state = JSON.parse(line.slice(6));
          const requestNode = state.nodes.find(node => node.turn_id === 'live-turn');
          const progress = state.messages.filter(message => message.id === 'live-progress');
          if (requestNode?.title === 'Windows open writer live request' && progress.length === 1 &&
              progress[0].message === 'Windows host live SSE evidence · 한글 실시간 메시지') {
            clearTimeout(timer); request.destroy(); resolve(true);
          }
        }
      });
    });
    request.on('error', reject);
  });
  assert.equal(received, true);
  assert.equal(backend.child.pid, pid, 'Live delivery required a restart');
  assert.equal(writerOpen, true, 'Writer closed before live delivery');
  assert.equal(fs.statSync(rollout).mtimeMs, frozenTime.getTime());
  fs.closeSync(writer); writerOpen = false;
  await backend.stop(); assert.equal(backend.child, null);
  assert.equal(fs.existsSync(path.join(settings.dataDir, '.server.json')), false);
  assert.ok(!logs.join('\n').includes(endpoint.token));
  await backend.start(settings);
  assert.equal(backend.phase, 'ready'); assert.notEqual(backend.child.pid, pid);
  await backend.stop();
});
