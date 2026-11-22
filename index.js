/**
 * dsh-plugin-cnki-lit
 *
 * 把 cnki-lit（上财机构权限的知网 CLI）包装成 DSH 工具。
 * 每次调用 = 一个 `python cnki_lit.py <cmd> ... --json` 子进程，stdout 作为结构化数据返回。
 *
 * @module dsh-plugin-cnki-lit
 */
import { spawn } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));

export const name = 'cnki-lit';
export const inject = ['tools'];

const OUT_SCHEMA = { type: 'object', additionalProperties: true };
const FIELD = ['SU', 'TI', 'KY', 'AB', 'AU', 'LY'];

const FIELD_HINT = '检索字段：SU=主题（默认）、TI=篇名、KY=关键词、AB=摘要、AU=作者、LY=来源。';

export function apply(ctx, config) {
  const settings = {
    python: (config?.python || '').trim() || 'python',
    script: (config?.script || '').trim() || join(HERE, 'cnki_lit.py'),
    dir: (config?.dir || '').trim(),
    searchTimeout: config?.searchTimeout ?? 60000,
    downloadTimeout: config?.downloadTimeout ?? 180000,
    collectTimeout: config?.collectTimeout ?? 900000,
  };
  for (const def of buildTools(settings)) {
    ctx.effect(() => ctx.tools.register(def));
  }
}

function buildTools(settings) {
  return [
    {
      name: 'cnki_auth',
      description:
        '检查/建立 CNKI 机构访问会话。action=status 看会话是否有效；login 用上财门户 TGC 走 CARSI 换票；trust 弹出浏览器过一次滑块（一次性，建立信任态）。返回 session_ok 为 false 时先 login，再 trust。',
      parameters: {
        action: { type: 'string', enum: ['status', 'login', 'trust'], description: '默认 status。' },
      },
      timeoutMs: 300000,
      output: { schema: OUT_SCHEMA, render: (_a, v) => textContent(renderAuth(v)) },
      execute: async (args) => {
        const a = args.action || 'status';
        const argv = ['auth', a === 'status' ? '--status' : a === 'login' ? '--login' : '--trust'];
        argv.push('--json');
        return exec_(settings, 'cnki_auth', argv, 290000);
      },
    },
    {
      name: 'cnki_search',
      description:
        `检索中国知网（走学校机构权限）。返回题名/作者/来源/日期/数据库/详情链接。单次最多 50 条，${FIELD_HINT}`,
      parameters: {
        keyword: { type: 'string', description: '检索词。' },
        field: { type: 'string', enum: FIELD, description: FIELD_HINT },
        limit: { type: 'integer', description: '返回条数，默认 20，上限 50。' },
        page: { type: 'integer', description: '页码，默认 1。翻页可能触发验证码。' },
      },
      timeoutMs: settings.searchTimeout,
      output: { schema: OUT_SCHEMA, render: (_a, v) => textContent(renderSearch(v)) },
      execute: async (args) => {
        const argv = ['search', args.keyword];
        if (args.field) argv.push(`--field=${args.field}`);
        if (args.limit) argv.push(`--limit=${args.limit}`);
        if (args.page) argv.push(`--page=${args.page}`);
        argv.push('--json');
        return exec_(settings, 'cnki_search', argv, settings.searchTimeout);
      },
    },
    {
      name: 'cnki_detail',
      description: '取单篇知网文献的摘要、关键词、作者、来源与 PDF 下载链接。url 来自 cnki_search 返回的 url 字段。',
      parameters: { url: { type: 'string', description: 'kcms2/article/abstract 详情页链接。' } },
      timeoutMs: settings.searchTimeout,
      output: { schema: OUT_SCHEMA, render: (_a, v) => textContent(renderDetail(v)) },
      execute: async (args) => exec_(settings, 'cnki_detail', ['detail', args.url, '--json'], settings.searchTimeout),
    },
    {
      name: 'cnki_download',
      description:
        '下载论文原文 PDF（机构订阅允许的文献）。内部用浏览器点击「PDF下载」并拦截下载事件；无权限或需登录时返回 NOT_PDF。',
      parameters: {
        url: { type: 'string', description: '详情页链接。' },
        out: { type: 'string', description: '保存文件或目录；目录时用知网建议的文件名。' },
      },
      timeoutMs: settings.downloadTimeout,
      output: { schema: OUT_SCHEMA, render: (_a, v) => textContent(renderDownload(v)) },
      execute: async (args) =>
        exec_(settings, 'cnki_download', ['download', args.url, '-o', args.out || '.'], settings.downloadTimeout, true),
    },
    {
      name: 'cnki_collect',
      description:
        '批量采集：按关键词检索 → 逐条取摘要 → 下载 PDF → 生成 RIS 文件，整包可直接导入 Zotero。适合「把这批文献归档」的请求。',
      parameters: {
        keyword: { type: 'string', description: '检索词。' },
        limit: { type: 'integer', description: '采集条数，默认 10。条数越多越慢。' },
        field: { type: 'string', enum: FIELD, description: FIELD_HINT },
        outdir: { type: 'string', description: '输出目录，默认 ~/cnki-import。' },
        no_pdf: { type: 'boolean', description: 'true 时只导出题录与摘要，不下载 PDF。' },
      },
      timeoutMs: settings.collectTimeout,
      output: { schema: OUT_SCHEMA, render: (_a, v) => textContent(renderCollect(v)) },
      execute: async (args) => {
        const argv = ['collect', args.keyword, `--limit=${args.limit ?? 10}`];
        if (args.field) argv.push(`--field=${args.field}`);
        if (args.outdir) argv.push('--outdir', args.outdir);
        if (args.no_pdf) argv.push('--no-pdf');
        return exec_(settings, 'cnki_collect', argv, settings.collectTimeout, true);
      },
    },
    {
      name: 'cnki_review',
      description:
        '文献综述素材包：按关键词检索并批量抓取摘要/关键词，输出 JSON 供据此撰写综述。总结与行文由模型完成，工具只负责取料。',
      parameters: {
        keyword: { type: 'string', description: '综述主题检索词。' },
        limit: { type: 'integer', description: '纳入文献数，默认 15。' },
        field: { type: 'string', enum: FIELD, description: FIELD_HINT },
      },
      timeoutMs: settings.collectTimeout,
      output: { schema: OUT_SCHEMA, render: (_a, v) => textContent(renderReview(v)) },
      execute: async (args) => {
        const argv = ['review', args.keyword, `--limit=${args.limit ?? 15}`];
        if (args.field) argv.push(`--field=${args.field}`);
        return exec_(settings, 'cnki_review', argv, settings.collectTimeout, true);
      },
    },
  ];
}

/* ---------------------------------------------------------------- exec */

function exec_(settings, tool, argv, timeoutMs, needsJson) {
  return new Promise((resolve) => {
    const proc = spawn(settings.python, [settings.script, ...argv], {
      cwd: settings.dir || undefined,
      env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
      windowsHide: true,
    });
    let stdout = '';
    let stderr = '';
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      proc.kill();
    }, timeoutMs);
    proc.stdout.on('data', (d) => (stdout += d.toString('utf8')));
    proc.stderr.on('data', (d) => (stderr += d.toString('utf8')));
    proc.on('error', (err) => {
      clearTimeout(timer);
      resolve({ ok: false, tool, code: 'CLI_NOT_FOUND', message: String(err.message || err) });
    });
    proc.on('close', (code) => {
      clearTimeout(timer);
      if (timedOut) return resolve({ ok: false, tool, code: 'TIMEOUT', message: `超过 ${Math.round(timeoutMs / 1000)} 秒未完成` });
      const text = stdout.trim();
      if (!text) return resolve({ ok: false, tool, code: 'EMPTY', message: (stderr || '').slice(-400) || `退出码 ${code}` });
      try {
        const data = JSON.parse(text);
        if (data && data.ok === false) return resolve({ ok: false, tool, ...data });
        return resolve({ ok: true, tool, data });
      } catch {
        return resolve({ ok: false, tool, code: 'PARSE_ERROR', message: text.slice(-300) });
      }
    });
  });
}

/* ---------------------------------------------------------------- render */

const line = (s) => String(s ?? '').replace(/\s+/g, ' ').trim();
const textContent = (text) => [{ type: 'text', text }];
const unwrap = (v) => (v && typeof v === 'object' && 'data' in v ? (v.data ?? {}) : (v ?? {}));
const failText = (v) => '❌ ' + ((v && v.message) || '调用失败') + (v && v.code ? '（' + v.code + '）' : '');

function renderAuth(v) {
  if (v && v.ok === false) return failText(v);
  const d = unwrap(v);
  if (d.state) return `信任态已保存：${d.state}（${d.cookies ?? 0} 个 Cookie）`;
  return `会话${d.alive ? '有效' : '无效'}｜探测页：${line(d.probe) || '-'}｜关键 Cookie：${(d.key || []).join(', ') || '无'}`;
}

function renderSearch(v) {
  if (v && v.ok === false) return failText(v);
  const d = unwrap(v);
  const items = d.items || [];
  const body = items
    .map((it, i) => `${i + 1}. ${line(it.title)}\n    ${line(it.authors)} ｜ ${line(it.source)} ｜ ${line(it.date)}\n    ${it.url}`)
    .join('\n');
  return `共 ${d.total ?? '?'} 条，返回 ${items.length} 条\n${body}`;
}

function renderDetail(v) {
  if (v && v.ok === false) return failText(v);
  const d = unwrap(v);
  return `${line(d.title)}\n\n${line(d.abstract).slice(0, 600)}\n\n关键词：${(d.keywords || []).join(' / ')}`;
}

function renderDownload(v) {
  if (v && v.ok === false) return failText(v);
  const d = unwrap(v);
  return `已保存：${d.path ?? ''}（${d.bytes ?? 0} 字节）`;
}

function renderCollect(v) {
  if (v && v.ok === false) return failText(v);
  const d = unwrap(v);
  const tip = '导入 Zotero：文件 → 导入 → 选择该 RIS，勾选「连同 PDF 一起导入」。';
  return `已采集 ${d.count ?? 0} 条（PDF ${d.pdf ?? 0} 份）\n目录：${d.outdir ?? ''}\nRIS：${d.ris ?? ''}\n${tip}`;
}

function renderReview(v) {
  if (v && v.ok === false) return failText(v);
  const d = unwrap(v);
  const items = d.items || [];
  return `主题「${d.keyword}」共 ${d.total ?? '?'} 条，已取 ${items.length} 篇摘要：\n` +
    items.map((it, i) => `${i + 1}. ${line(it.title)}（${line(it.source)}，${line(it.date)}）\n    ${it.url}`).join('\n');
}