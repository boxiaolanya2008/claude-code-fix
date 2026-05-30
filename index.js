#!/usr/bin/env node

const { spawn } = require('child_process');
const URL = 'https://github.com/boxiaolanya2008/claude-code-fix';

// Cross-platform open browser
function openBrowser(url) {
  if (process.platform === 'win32') {
    // Windows: use cmd /c start
    spawn('cmd', ['/c', 'start', '', url], { detached: true, stdio: 'ignore' }).unref();
  } else if (process.platform === 'darwin') {
    spawn('open', [url], { detached: true, stdio: 'ignore' }).unref();
  } else {
    spawn('xdg-open', [url], { detached: true, stdio: 'ignore' }).unref();
  }
}

console.log('\n\x1b[36m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\x1b[0m');
console.log('\x1b[1;32m  Claude Code Fix Installed Successfully!\x1b[0m');
console.log('\x1b[36m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\x1b[0m\n');

console.log('\x1b[33m  Project URL:\x1b[0m', URL, '\n');

console.log('\x1b[35m  Usage:\x1b[0m');
console.log('    \x1b[32mnpm install -g claudecode-fix\x1b[0m');
console.log('    \x1b[32mclaude-code-fix-400\x1b[0m\n');

console.log('\x1b[31m  Next time, just run:\x1b[0m');
console.log('    \x1b[1;32mclaude-code-fix-400\x1b[0m\n');

console.log('\x1b[36m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\x1b[0m\n');

// Open browser
openBrowser(URL);
console.log('\x1b[32m  Browser opened automatically!\x1b[0m\n');