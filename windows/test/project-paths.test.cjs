const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../../src/monitor/index.html'), 'utf8');
const implementation = html.match(/^    function selectedAppPath\(\).*$/m)[0];
function selected(value, status = 'ready') {
  const context = vm.createContext({state: {app_context: {status, selected_project: {path: value}}}});
  return vm.runInContext(implementation + '; selectedAppPath()', context);
}
test('authoritative local app selection supports Windows drives, UNC and Korean paths', () => {
  for (const value of ['C:\\개발\\프로젝트', 'D:/Work/codex', '\\\\server\\share\\project', '/Users/test/project']) {
    assert.equal(selected(value), value);
  }
});
test('relative and unavailable or remote app selection never autofills a project', () => {
  for (const value of ['C:relative', 'relative/project', 'ssh://server/project']) assert.equal(selected(value), null);
  for (const status of ['unavailable', 'remote', 'multiple_roots', 'invalid_path']) {
    assert.equal(selected('C:\\Work\\project', status), null);
  }
});
