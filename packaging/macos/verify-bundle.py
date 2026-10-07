#!/usr/bin/env python3
"""Verify the packaged service with disposable, non-user inputs outside the repo.

Usage: python3 packaging/macos/verify-bundle.py \
    'dist/Codex DAG.app/Contents/Resources/backend/codex-dag-backend' \
    --evidence .local/verification/bundle.json

The evidence contains check names/status only: no session token or source bodies.
This verifies the backend contract, not installed menu-bar/WebView interactions.
"""
import argparse
import hashlib
import http.client
import json
import os
import selectors
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit


class CheckFailure(RuntimeError):
    pass


def require(condition, description):
    if not condition:
        raise CheckFailure(description)


def digest_tree(directory):
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob('*') if p.is_file() and not p.is_symlink()}


class Service:
    def __init__(self, verifier, data, codex=None, port=0, parent=None):
        self.verifier, self.data = verifier, data
        self.log = tempfile.TemporaryFile(mode='w+b')
        command = verifier.command(data, codex) + ['serve', '--port', str(port)]
        if parent is not None:
            command += ['--parent-pid', str(parent)]
        self.process = subprocess.Popen(command, cwd=verifier.root, env=verifier.env,
                                        stdout=subprocess.PIPE, stderr=self.log)
        verifier.services.append(self)
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            require(bool(selector.select(15)), 'Service startup JSON timed out')
            line = self.process.stdout.readline()
            require(bool(line), 'Service exited before publishing startup JSON')
            self.info = json.loads(line)
        finally:
            selector.close()
        require(self.info.get('pid') == self.process.pid, 'Startup PID does not identify owned process')
        endpoint = urlsplit(self.info['url'])
        require(endpoint.scheme == 'http' and endpoint.hostname == '127.0.0.1',
                'Service address is not loopback HTTP')
        require(bool(self.info.get('token')), 'Startup authentication token is missing')
        self.host, self.port = endpoint.hostname, endpoint.port
        self.check_ready()

    def request(self, path, method='GET', body=None, auth=True, headers=None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        supplied = {'Authorization': 'Bearer ' + self.info['token']} if auth else {}
        supplied.update(headers or {})
        if body is not None:
            body = json.dumps(body).encode()
            supplied['Content-Type'] = 'application/json'
        try:
            connection.request(method, path, body=body, headers=supplied)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def json(self, path, method='GET', body=None):
        status, _, raw = self.request(path, method, body)
        require(status == 200, f'{method} {path} failed with HTTP {status}')
        return json.loads(raw)

    def check_ready(self):
        value = self.json('/api/health')
        require(value.get('status') == 'ready' and value.get('pid') == self.process.pid,
                'Authenticated health does not confirm owned service readiness')

    def stream(self, last_id=None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=6)
        headers = {'Authorization': 'Bearer ' + self.info['token']}
        if last_id is not None:
            headers['Last-Event-ID'] = str(last_id)
        connection.request('GET', '/api/stream', headers=headers)
        response = connection.getresponse()
        require(response.status == 200, 'Authenticated SSE did not start')
        require(response.getheader('Content-Type', '').startswith('text/event-stream'),
                'SSE content type is incorrect')
        return connection, response

    @staticmethod
    def frame(response):
        for _ in range(80):
            line = response.readline()
            require(bool(line), 'SSE closed before snapshot')
            if line.startswith(b'data: '):
                return json.loads(line[6:])
        raise CheckFailure('SSE snapshot frame was not received')

    def terminate(self):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        try:
            status = self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
            raise CheckFailure('Owned service ignored SIGTERM')
        require(status == 0, 'Owned service did not exit cleanly')
        require(not (self.data / '.server.json').exists(), 'Stopped service left .server.json')

    def log_text(self):
        self.log.seek(0)
        return self.log.read().decode('utf-8', errors='replace')


class Verifier:
    def __init__(self, binary, root):
        root = root.resolve()
        self.root, self.checks, self.services, self.writers = root, [], [], []
        self.home = root / 'home'; self.home.mkdir()
        self.tmp = root / 'tmp'; self.tmp.mkdir()
        # Exclude Homebrew, developer Python, PYTHONPATH and actual Codex env.
        self.env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin',
                    'TMPDIR': str(self.tmp), 'LANG': 'en_US.UTF-8',
                    'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'}
        copied = root / 'bundled-backend'
        shutil.copytree(binary.parent, copied, symlinks=True)
        self.binary = copied / binary.name
        self.resource_hashes = digest_tree(copied)

    def command(self, data, codex=None):
        command = [str(self.binary), '--data-dir', str(data)]
        if codex is not None:
            command += ['--codex-dir', str(codex)]
        return command

    def record(self, name, action):
        action()
        self.checks.append({'check': name, 'status': 'passed'})
        print(f'PASS {name}', flush=True)

    def cli(self, data, *arguments):
        value = subprocess.run(self.command(data) + list(arguments), cwd=self.root,
                               env=self.env, capture_output=True, timeout=10)
        require(value.returncode == 0, 'Bundled journal CLI failed')
        return value

    def refused(self, data, codex=None, port=0):
        value = subprocess.run(self.command(data, codex) + ['serve', '--port', str(port)],
                               cwd=self.root, env=self.env, capture_output=True, timeout=10)
        require(value.returncode != 0, 'Conflicting service unexpectedly started')
        require(not value.stdout.strip(), 'Rejected service published a usable startup address')

    def fixture(self, codex, project, session, live_wal=False):
        codex.mkdir(exist_ok=True)
        project.mkdir(exist_ok=True)
        folder = codex / 'sessions' / datetime.now().strftime('%Y/%m/%d')
        folder.mkdir(parents=True, exist_ok=True)
        created = datetime.now(timezone.utc) - timedelta(seconds=5)
        timestamp = created.isoformat(timespec='milliseconds')
        rollout = folder / ('rollout-' + session + '.jsonl')
        rows = [{'timestamp': timestamp, 'type': 'session_meta', 'payload': {
            'id': session, 'cwd': str(project), 'source': 'cli'}}]
        rollout.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        (codex / '.codex-global-state.json').write_text(json.dumps({
            'selected-project': {'type': 'local', 'projectId': 'fixture'},
            'local-projects': {'fixture': {'name': 'Verifier fixture', 'rootPaths': [str(project)]}}}))
        database = codex / 'state_5.sqlite'
        writer = sqlite3.connect(database)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT)')
        name = 'WAL-only fixture name' if live_wal else 'Closed-WAL fixture name'
        writer.execute('INSERT INTO threads VALUES (?,?)', (session, name))
        writer.commit()
        if live_wal:
            self.writers.append(writer)
        else:
            writer.close()
        return rollout

    @staticmethod
    def eventually(action, predicate, description):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = action()
            if predicate(result):
                return result
            time.sleep(.2)
        raise CheckFailure(description)

    def run(self):
        data = self.root / 'data-a'
        codex_a = self.root / 'codex-a'; codex_a.mkdir()
        codex_b = self.root / 'codex-b'; codex_b.mkdir()
        project_a, project_b = self.root / 'project-a', self.root / 'project-b'
        session_a = '11111111-1111-4111-8111-111111111111'
        session_b = '22222222-2222-4222-8222-222222222222'
        service = Service(self, data, codex_a)

        def fresh():
            state = service.json('/api/state')
            require(state.get('nodes') == [] and state.get('sessions') == [],
                    'Fresh installation fabricated graph/session records')
            require(not state.get('events') and not state.get('messages'),
                    'Fresh installation includes user/event history')
            require(state.get('live', {}).get('running_agents') == 0,
                    'Fresh installation fabricated active agents')
            settings = service.json('/api/settings')
            require(settings.get('codex_dir') == str(codex_a), 'Configured Codex directory was not applied')
            require(service.json('/api/projects').get('projects') == [], 'Fresh Codex fixture has projects')
            require((data / '.server.json').stat().st_mode & 0o777 == 0o600,
                    'Server token metadata is not owner-only')
        self.record('outside-repository restricted-PATH first run, health, state and settings', fresh)

        def access():
            for path in ('/', '/api/health', '/api/state', '/api/settings', '/api/projects', '/api/stream'):
                require(service.request(path, auth=False)[0] == 401, 'Unauthenticated read was accepted')
            for method in ('GET', 'POST'):
                path = '/api/state' if method == 'GET' else '/api/settings'
                body = None if method == 'GET' else {'codex_dir': str(codex_b)}
                require(service.request(path, method, body, headers={'Origin': 'https://external.invalid'})[0] == 403,
                        'Cross-origin authenticated request was accepted')
                require(service.request(path, method, body, headers={'Host': 'rebound.invalid'})[0] == 403,
                        'Untrusted Host was accepted')
            require(service.request('/api/settings', 'POST', {'codex_dir': str(codex_b)}, auth=False)[0] == 401,
                    'Unauthenticated settings change was accepted')
            status, headers, _ = service.request('/?token=' + service.info['token'], auth=False)
            require(status == 303 and headers.get('Location') == '/', 'WebView bootstrap did not remove query token')
            cookie = headers.get('Set-Cookie', '')
            require('HttpOnly' in cookie and 'SameSite=Strict' in cookie, 'Bootstrap cookie lacks access boundaries')
            require(service.request('/', auth=False, headers={'Cookie': cookie.split(';')[0]})[0] == 200,
                    'Cookie-authenticated WebView entry failed')
        self.record('authentication, WebView bootstrap, Origin and Host boundaries', access)

        def reconnect():
            last = None
            for _ in range(3):
                connection, response = service.stream(last)
                try:
                    frame = Service.frame(response)
                    require('revision' in frame, 'SSE snapshot lacks revision')
                    last = frame['revision']
                finally:
                    response.close(); connection.close()
            service.check_ready()
        self.record('SSE snapshot and repeated reconnect', reconnect)

        def conflicts():
            before = (data / '.server.json').read_bytes()
            self.refused(data, codex_a)
            require((data / '.server.json').read_bytes() == before, 'Duplicate replaced owner metadata')
            service.check_ready()
            with socket.socket() as owner:
                owner.bind(('127.0.0.1', 0)); owner.listen()
                self.refused(self.root / 'port-conflict', codex_a, owner.getsockname()[1])
                require(owner.fileno() >= 0 and owner.getsockname()[1] > 0, 'Port owner was disturbed')
            service.check_ready()
        self.record('duplicate data-dir and occupied port refuse without disturbing owner', conflicts)

        rollout_a = self.fixture(codex_a, project_a, session_a)
        rollout_b = self.fixture(codex_b, project_b, session_b, live_wal=True)
        input_a, input_b = digest_tree(codex_a), digest_tree(codex_b)

        def select():
            self.eventually(lambda: service.json('/api/projects'),
                lambda v: any(p['path'] == str(project_a) for p in v.get('projects', [])),
                'Collector did not discover temporary Codex project')
            service.json('/api/project', 'POST', {'path': str(project_a)})
            service.json('/api/session', 'POST', {'id': session_a})
            require(service.json('/api/settings').get('root_session_id') == session_a,
                    'Selected session was not persisted')
            require(service.json('/api/state').get('root_session_name') == 'Closed-WAL fixture name',
                    'Closed-WAL database name was not read')
            require(not (codex_a / 'state_5.sqlite-wal').exists() and not (codex_a / 'state_5.sqlite-shm').exists(),
                    'Read-only Codex reader created WAL/SHM files in closed-WAL source')
        self.record('temporary Codex source, project and root-session selection', select)

        history_body = 'Synthetic public bundle-verifier history'
        def live():
            connection, response = service.stream()
            try:
                before = Service.frame(response)['revision']
                self.cli(data, 'message', '--from-agent', '/root', '--to-agent', '/root/review',
                         '--message', history_body, '--kind', 'result',
                         '--session', session_a, '--project', str(project_a))
                newer = Service.frame(response)
                require(newer['revision'] > before, 'SSE failed to publish appended journal event')
                require(any(r.get('message') == history_body for r in newer.get('message_records', [])),
                        'Matching-session journal result missing from SSE')
            finally:
                response.close(); connection.close()
        self.record('live SSE journal update with original synthetic message record', live)
        ledger = (data / 'events.jsonl').read_bytes()

        def runtime():
            def append(kind, turn, report=None):
                payload = {'type': kind, 'turn_id': turn}
                if kind == 'task_started':
                    payload['root_turn_id'] = turn
                if report is not None:
                    payload['last_agent_message'] = report
                with rollout_a.open('a') as handle:
                    handle.write(json.dumps({'timestamp': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                                             'type': 'event_msg', 'payload': payload}) + '\n')
            append('task_started', 'bundle-turn-one')
            self.eventually(lambda: service.json('/api/state'),
                lambda v: v.get('live', {}).get('running_agents') == 1,
                'Runtime start did not change actual running-agent count')
            append('task_complete', 'bundle-turn-one', 'Synthetic verifier completed')
            complete = self.eventually(lambda: service.json('/api/state'),
                lambda v: v.get('live', {}).get('completed_agents') == 1,
                'Runtime completion did not stop actual running-agent count')
            require(complete['live']['running_agents'] == 0, 'Completed fixture is still counted running')
            require(any(e.get('type') == 'task_complete' and e.get('message') == 'Synthetic verifier completed'
                        for e in complete.get('events', [])), 'Execution log lacks completion body')
            require(any(r.get('message') == 'Synthetic verifier completed' for r in complete.get('message_records', [])),
                    'Collaboration lacks original completion result')
            old = next(n for n in complete['nodes'] if n.get('turn_id') == 'bundle-turn-one')
            append('task_started', 'bundle-turn-two')
            newer = self.eventually(lambda: service.json('/api/state'),
                lambda v: v.get('live', {}).get('running_agents') == 1,
                'New runtime request did not start actual running-agent count')
            require(next(n for n in newer['nodes'] if n['id'] == old['id'])['status'] == 'completed',
                    'New runtime request revived a completed request')
        self.record('runtime start/completion counts, readable results and immutable completed requests', runtime)
        input_a[str(rollout_a.relative_to(codex_a))] = hashlib.sha256(rollout_a.read_bytes()).hexdigest()

        def source_boundary():
            config_before = (data / 'config.json').read_bytes()
            status, _, _ = service.request('/api/settings', 'POST', {'codex_dir': str(data)})
            require(status == 400, 'Settings accepted Codex source overlapping writable app data')
            require((data / 'config.json').read_bytes() == config_before,
                    'Rejected source-overlap settings changed persisted config')
            source = self.root / 'rejected-codex-overlap'; source.mkdir()
            self.refused(source / 'app-data', source)
            require(not any(source.iterdir()), 'Rejected data/source startup wrote inside Codex source')
        self.record('data/Codex overlap rejected before source writes', source_boundary)

        def switch():
            service.json('/api/settings', 'POST', {'codex_dir': str(codex_b)})
            self.eventually(lambda: service.json('/api/projects'),
                lambda v: any(p['path'] == str(project_b) for p in v.get('projects', [])),
                'Changed Codex directory was not applied')
            require(not any(p['path'] == str(project_a) for p in service.json('/api/projects')['projects']),
                    'Old Codex source leaked through directory change')
            service.json('/api/project', 'POST', {'path': str(project_b)})
            service.json('/api/session', 'POST', {'id': session_b})
            selected = self.eventually(lambda: service.json('/api/state'),
                lambda v: v.get('root_session_name') == 'WAL-only fixture name',
                'Live-WAL-only displayed name was not read from selected source')
            require(not any(r.get('message') == history_body for r in service.json('/api/state').get('message_records', [])),
                    'Other-source/session journal leaked into selected source')
            require((data / 'events.jsonl').read_bytes() == ledger, 'Source change altered persisted history')
        self.record('Codex-directory change clears old source and preserves isolated history', switch)

        def stop():
            service.terminate()
            require('Traceback' not in service.log_text(), 'SSE disconnect caused backend traceback')
        self.record('SIGTERM owned process and metadata cleanup without SSE traceback', stop)

        restarted = Service(self, data)
        def persistence():
            settings = restarted.json('/api/settings')
            require(settings.get('codex_dir') == str(codex_b), 'Restart lost Codex source setting')
            require(settings.get('project_path') == str(project_b) and settings.get('root_session_id') == session_b,
                    'Restart lost project/session selection')
            require((data / 'events.jsonl').read_bytes() == ledger, 'Restart changed history bytes')
            restarted.json('/api/settings', 'POST', {'codex_dir': str(codex_a)})
            self.eventually(lambda: restarted.json('/api/projects'),
                lambda v: any(p['path'] == str(project_a) for p in v.get('projects', [])),
                'Restart could not reconnect original Codex source')
            restarted.json('/api/project', 'POST', {'path': str(project_a)})
            restarted.json('/api/session', 'POST', {'id': session_a})
            require(any(r.get('message') == history_body for r in restarted.json('/api/state').get('message_records', [])),
                    'Persisted original journal is missing after reconnecting original source/session')
            restarted.terminate()
        self.record('restart/settings/session/history persistence', persistence)

        def parent_death():
            parent = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                      cwd=self.root, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            child_data = self.root / 'parent-death-data'
            try:
                child = Service(self, child_data, codex_b, parent=parent.pid)
                parent.terminate(); parent.wait(timeout=5)
                require(child.process.wait(timeout=10) == 0, 'Parent death did not cleanly stop child')
                require(not (child_data / '.server.json').exists(), 'Parent death left service metadata')
            finally:
                if parent.poll() is None:
                    parent.terminate(); parent.wait(timeout=5)
        self.record('parent-death child cleanup', parent_death)

        def immutable_inputs():
            require(digest_tree(codex_a) == input_a and digest_tree(codex_b) == input_b,
                    'Service changed read-only Codex inputs')
            require(not (codex_a / 'state_5.sqlite-wal').exists() and not (codex_a / 'state_5.sqlite-shm').exists(),
                    'Read-only Codex reader created WAL/SHM files in closed-WAL source')
            require(digest_tree(self.binary.parent) == self.resource_hashes,
                    'Service wrote into copied bundle resources')
            require(not (self.home / '.codex').exists(), 'Service created or selected unintended default Codex source')
        self.record('Codex inputs and bundled resources unchanged', immutable_inputs)

    def cleanup(self):
        for service in self.services:
            if service.process.poll() is None:
                service.process.terminate()
                try:
                    service.process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    service.process.kill(); service.process.wait(timeout=3)
            service.log.close()
        for writer in self.writers:
            writer.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    binary = args.binary.expanduser().resolve()
    require(binary.is_file() and os.access(binary, os.X_OK), 'Pass the executable bundled backend')
    report = {'started_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'Copied packaged backend outside repository; disposable HOME/Codex/project/data; no native GUI assertion',
              'binary': str(binary), 'path': '/usr/bin:/bin', 'checks': [], 'status': 'failed'}
    verifier = None
    try:
        with tempfile.TemporaryDirectory(prefix='codex-dag-bundle-verify-') as temporary:
            verifier = Verifier(binary, Path(temporary))
            try:
                verifier.run()
                report['status'] = 'passed'
            finally:
                report['checks'] = verifier.checks
                verifier.cleanup()
    except Exception as exc:
        # Exceptions deliberately contain only check descriptions, never source/token JSON.
        report['failure'] = str(exc)
        print('FAIL ' + str(exc), file=sys.stderr)
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
