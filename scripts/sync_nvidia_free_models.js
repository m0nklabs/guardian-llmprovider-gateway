#!/usr/bin/env node
/**
 * sync_nvidia_free_models.js
 *
 * Refreshes the NVIDIA free-model allowlist in config/providers.overrides.yaml
 * (providers.nvidia.catalog_allowlist).
 *
 * NVIDIA's hosted /v1/models lists every model (102) regardless of what the free
 * token can reach, and gives no free/price marker. The authoritative source for
 * "what is free" is build.nvidia.com's catalog filtered by the "Free Endpoint"
 * switch, which maps to the query:
 *
 *   ?filters=nimType:nim_type_preview&pageSize=96&orderBy=weightPopular:DESC
 *
 * That URL renders ALL free models on a single page (verified 2026-08-22: 58).
 * build.nvidia.com is behind an AWS WAF challenge, so a plain HTTP GET (curl) is
 * blocked — the WAF serves a challenge page that runs JavaScript. This script
 * therefore drives a real Chrome via the Chrome DevTools Protocol (CDP): it lets
 * the challenge JS run, accepts the cookie banner, waits for the model list to
 * render, extracts the model card links, and rewrites the allowlist.
 *
 * Optional extra allowlisted ids (e.g. moonshotai/kimi-k3, which the API serves
 * even though the page does not yet mark it free) are kept across refreshes.
 *
 * Usage:
 *   node scripts/sync_nvidia_free_models.js [--dry-run] [--keep kimi-k3,moonshotai/kimi-k3] [--extra model1,model2]
 *
 * Requires: google-chrome (or chromium) on PATH, and Node >= 22 (global WebSocket).
 * The profile dir used for the WAF session cookie is stored under
 * data/nvidia-sync-chrome-profile/ so the challenge is solved once.
 */
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

const REPO = path.resolve(__dirname, '..');
const OVERRIDES_FILE = path.join(REPO, 'config', 'providers.overrides.yaml');
const PROFILE_DIR = path.join(REPO, 'data', 'nvidia-sync-chrome-profile');
const CDP_PORT = 0; // 0 -> let the OS pick; we discover it from the log

const TARGET_URL =
  'https://build.nvidia.com/models?filters=nimType%3Anim_type_preview&pageSize=96&orderBy=weightPopular%3ADESC';

// `free`/`not-free` filter marker rendered on each free model card.
const FREE_BADGE_RE = /Free Endpoint/i;

function whichChrome() {
  const { execSync } = require('node:child_process');
  for (const name of ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser']) {
    try {
      const p = execSync(`command -v ${name}`).toString().trim();
      if (p) return p;
    } catch {}
  }
  return null;
}

/** Sleep helper. */
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Minimal CDP client over the Chrome DevTools Protocol using a raw WebSocket. */
class CDP {
  constructor(wsUrl, onClose) {
    this.ws = new WebSocket(wsUrl);
    this.id = 0;
    this.pending = new Map();
    this.onClose = onClose;
    this.ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(msg.error.message));
        else resolve(msg.result);
      }
    };
    this.ws.onclose = () => this.onClose && this.onClose();
  }
  static connect(wsUrl, onClose) {
    return new Promise((resolve, reject) => {
      const c = new CDP(wsUrl, onClose);
      c.ws.onopen = () => resolve(c);
      c.ws.onerror = (e) => reject(e);
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  close() { try { this.ws.close(); } catch {} }
}

async function launchChrome() {
  const chrome = whichChrome();
  if (!chrome) throw new Error('No chrome/chromium found on PATH');
  fs.mkdirSync(PROFILE_DIR, { recursive: true });

  const logPath = path.join(PROFILE_DIR, 'cdp-port.log');
  try { fs.unlinkSync(logPath); } catch {}

  const args = [
    '--headless=new',
    '--no-sandbox',
    '--disable-gpu',
    '--disable-dev-shm-usage',
    '--no-first-run',
    '--no-default-browser-check',
    '--remote-debugging-pipe', // avoid fixed port races
    `--user-data-dir=${PROFILE_DIR}`,
  ];
  // Use --remote-debugging-address/port on a random free port and read it from stderr.
  args[args.indexOf('--remote-debugging-pipe')] = '--remote-debugging-port=0';
  args.push('about:blank');

  const child = spawn(chrome, args, { stdio: ['ignore', 'ignore', 'pipe'] });
  // Chrome prints: DevTools listening on ws://127.0.0.1:<port>/devtools/browser/<id>
  const wsUrl = await new Promise((resolve, reject) => {
    let buf = '';
    const to = setTimeout(() => { try { child.kill(); } catch {} reject(new Error('Chrome CDP startup timeout')); }, 20000);
    child.stderr.on('data', (d) => {
      buf += d.toString();
      const m = buf.match(/DevTools listening on (ws:\/\/\S+)/);
      if (m) { clearTimeout(to); resolve(m[1]); }
    });
    child.on('error', (e) => { clearTimeout(to); reject(e); });
    child.on('exit', (code) => { clearTimeout(to); reject(new Error(`Chrome exited early (${code})`)); });
  });
  return { child, wsUrl };
}

/** Extract free model slugs ({publisher}/{model}) from the rendered page. */
function extractFreeModels() {
  const out = [];
  const seen = new Set();
  const skipHosts = new Set(['models', 'explore', 'skills', 'blueprints', 'pricing', 'catalog', 'docs', 'gpuid']);
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.getAttribute('href') || '';
    const m = href.match(/^\/([\w.+-]+)\/([\w.+-]+)$/);
    if (!m) continue;
    if (skipHosts.has(m[1])) continue;
    // Only count cards that actually render a "Free Endpoint" badge. The model
    // heading link may be a child of the card; walk up to the list item.
    let node = a;
    let isFree = false;
    for (let i = 0; i < 6 && node; i++) {
      if (FREE_BADGE_RE.test(node.textContent || '')) { isFree = true; break; }
      node = node.parentElement;
    }
    if (!isFree) continue; // only the Free-gated list should match, but be strict
    if (!seen.has(href)) { seen.add(href); out.push(href.replace(/^\//, '')); }
  }
  return out;
}

async function main() {
  const dryRun = process.argv.includes('--dry-run');
  const extraKeep = [];

  // Parse --extra=... / --keep=...
  for (const arg of process.argv) {
    if (arg.startsWith('--extra=')) {
      for (const s of arg.slice(8).split(',')) if (s.trim()) extraKeep.push(s.trim());
    }
  }

  // Load current allowlist so we can preserve entries (e.g. kimi-k3) not on the page.
  let existing = [];
  try {
    const raw = fs.readFileSync(OVERRIDES_FILE, 'utf8');
    const m = raw.match(/catalog_allowlist:\s*((?:\n\s*-[^\n]*)+)/);
    if (m) {
      existing = [...m[1].matchAll(/-\s*([^\s#]+)/g)].map((x) => x[1]);
    }
  } catch {}

  console.log(`Launching Chrome (headless) to fetch NVIDIA free list… (profile: ${PROFILE_DIR})`);
  const { child, wsUrl } = await launchChrome();

  let pageWsUrl;
  try {
    // Open a fresh tab at the target URL via the HTTP /json/new endpoint, which
    // returns the page's own webSocketDebuggerUrl (the route proven to work).
    const port = new URL(wsUrl).port;
    const created = await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(TARGET_URL)}`, {
      method: 'PUT',
    }).then((r) => r.json());
    pageWsUrl = created.webSocketDebuggerUrl;
  } catch (e) {
    console.error('Target setup error:', e.message);
    child.kill();
    process.exit(1);
  }

  let cdp;
  try {
    cdp = await CDP.connect(pageWsUrl, () => {});
    await sleep(1000);
    // The /json/new tab may still be on about:blank; navigate explicitly and
    // enable Page events so the SPA (and WAF challenge JS) actually runs.
    try {
      await cdp.send('Page.enable');
      await cdp.send('Page.navigate', { url: TARGET_URL });
    } catch {}
    await sleep(4000);
    // Help the WAF challenge / cookie banner along.
    try {
      await cdp.send('Runtime.evaluate', {
        expression: `(() => {
          const btn = [...document.querySelectorAll('button')].find(b => /Accept All/i.test(b.textContent||''));
          if (btn) { btn.click(); return 'accept-clicked'; }
          return 'no-banner';
        })()`,
      });
    } catch {}
    // Wait for the model list to render (poll the count label / card links).
    let freeModels = [];
    for (let attempt = 0; attempt < 12; attempt++) {
      await sleep(2500);
      try {
        const res = await cdp.send('Runtime.evaluate', {
          expression: `(${extractFreeModels.toString()})()`,
          returnByValue: true,
        });
        const list = res?.result?.value;
        if (Array.isArray(list) && list.length > 0) freeModels = list;
        // Stop once we've reached the authoritative count (58) or a stable set.
        if (freeModels.length >= 40) break;
      } catch {}
    }
    cdp.close();

    // Preserve explicit extras + existing entries not in the fresh list.
    const fresh = new Set(freeModels);
    const keep = new Set(extraKeep);
    for (const e of existing) if (!fresh.has(e)) keep.add(e);
    const merged = [...new Set([...freeModels, ...keep])].sort();

    console.log(`Fresh free from page: ${freeModels.length}; kept extras: ${[...keep].length}; total allowlist: ${merged.length}`);
    if (dryRun) {
      console.log('[dry-run] would write allowlist:');
      for (const m of merged) console.log('  ', m);
      child.kill();
      process.exit(0);
    }

    // Write the overrides file, preserving the openrouter.catalog_url block.
    const yamlLines = [];
    yamlLines.push('providers:');
    yamlLines.push('  openrouter:');
    yamlLines.push('    catalog_url: /models/user');
    yamlLines.push('  nvidia:');
    yamlLines.push('    # AWS WAF-gated NVIDIA free catalog (build.nvidia.com filtered by');
    yamlLines.push('    # nimType:nim_type_preview). Auto-refreshed by scripts/sync_nvidia_free_models.js.');
    yamlLines.push('    catalog_allowlist:');
    for (const m of merged) yamlLines.push(`      - ${m}`);
    fs.writeFileSync(OVERRIDES_FILE, yamlLines.join('\n') + '\n');
    console.log(`Wrote ${OVERRIDES_FILE}`);
  } finally {
    child.kill();
  }
}

main().catch((e) => {
  console.error('sync_nvidia_free_models failed:', e);
  process.exitCode = 1;
});
