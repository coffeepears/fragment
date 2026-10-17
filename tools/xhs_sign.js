#!/usr/bin/env node
/**
 * 小红书 Web 签名器（骨架 + 协议实现）
 * =====================================
 *
 * 干什么用的
 * ----------
 * 创作者平台（creator.xiaohongshu.com）的每个请求都要带 `x-s` / `x-t` 头，
 * 值是页面里那段混淆 JS（`window._webmsxyw`）算出来的。纯靠 Python 复刻这段算法
 * 既违反平台意愿、也极不稳定，所以本项目把它做成**可插拔的外部进程**。
 *
 * 本文件只负责**协议那一半**：stdin 收 JSON，调用签名实现，stdout 吐 JSON。
 * 真正的算法请放在同目录的 `xhs_sign_impl.js` 里（那个文件不会进版本库）。
 *
 * 协议
 * ----
 *   →  {"method":"POST","uri":"/web_api/sns/v2/note","payload":{...},"cookies":{"a1":"..."}}
 *   ←  {"x-s":"XYS_...","x-t":"1712345678901"}
 *
 * 用法
 * ----
 *   echo '{"method":"POST","uri":"/web_api/sns/v2/note","payload":{},"cookies":{"a1":"x"}}' \
 *     | node tools/xhs_sign.js
 *
 * 然后把这个命令填进「设置 → 账号 → 小红书 → 签名器命令」：
 *   node /home/you/gnome-poster/tools/xhs_sign.js
 *
 * 怎么搞到真正的算法
 * ------------------
 * 1. 浏览器登录 creator.xiaohongshu.com，F12 → Sources，搜 `_webmsxyw` 或 `XYS_`；
 * 2. 把那个函数所在的混淆文件整个存成 `tools/xhs_sign_impl.js`；
 * 3. 在文件末尾加：
 *
 *      module.exports = {
 *        sign(method, uri, payload, cookies) {
 *          // uri 是不带域名的路径 + query，payload 是请求体对象（GET 时是 {}）
 *          const x_t = Date.now().toString();
 *          const x_s = globalThis._webmsxyw(uri, payload)['X-s'];
 *          return { 'x-s': x_s, 'x-t': x_t };
 *        },
 *      };
 *
 * 4. `node tools/xhs_sign.js --selftest` 自检通过就能用了。
 *
 * 或者：别折腾签名，把「发布通道」改成 MCP，用一个本地的发布服务来干这活。
 *
 * ⚠️ 免责：只用来发布你自己账号的内容；请遵守小红书用户协议，不要做自动化刷量。
 */

'use strict';

const path = require('path');
const fs = require('fs');

const IMPL_PATH = path.join(__dirname, 'xhs_sign_impl.js');

const SAMPLE = {
  method: 'POST',
  uri: '/web_api/sns/v2/note',
  payload: { title: '自检', desc: '自检用的一条内容', type: 'normal' },
  cookies: { a1: 'selftest-a1', web_session: 'selftest-session' },
};

function loadImpl() {
  if (!fs.existsSync(IMPL_PATH)) {
    return null;
  }
  // eslint-disable-next-line global-require, import/no-dynamic-require
  const mod = require(IMPL_PATH);
  const impl = mod && mod.default ? mod.default : mod;
  if (!impl || typeof impl.sign !== 'function') {
    throw new Error(`${IMPL_PATH} 里没有导出 sign(method, uri, payload, cookies)`);
  }
  return impl;
}

function normalise(result) {
  if (!result || typeof result !== 'object') {
    throw new Error('签名实现必须返回一个对象');
  }
  // 容忍 {'X-s': ...} / {headers: {...}} 这类包装
  const inner = result.headers || result.data || result;
  const headers = {};
  for (const [key, value] of Object.entries(inner)) {
    if (typeof value === 'string' || typeof value === 'number') {
      headers[String(key).toLowerCase()] = String(value);
    }
  }
  if (!headers['x-s'] || !headers['x-t']) {
    throw new Error(`签名结果缺少 x-s / x-t，实际拿到：${JSON.stringify(headers)}`);
  }
  return headers;
}

function readStdin() {
  return new Promise((resolve, reject) => {
    let raw = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (chunk) => { raw += chunk; });
    process.stdin.on('end', () => resolve(raw));
    process.stdin.on('error', reject);
  });
}

async function main() {
  const args = process.argv.slice(2);

  if (args.includes('--help') || args.includes('-h')) {
    process.stdout.write(
      '用法: echo \'{"method","uri","payload","cookies"}\' | node xhs_sign.js\n' +
      '      node xhs_sign.js --selftest\n'
    );
    return 0;
  }

  const selftest = args.includes('--selftest');
  let impl;
  try {
    impl = loadImpl();
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    return 1;
  }

  if (impl === null) {
    process.stderr.write(
      `缺少签名实现：${IMPL_PATH}\n\n` +
      '这个文件需要你自己从浏览器里把小红书的签名 JS 抽出来（见本文件顶部注释），\n' +
      '或者改用 MCP 通道（设置 → 账号 → 小红书 → 发布通道）。\n'
    );
    return 2;
  }

  const request = selftest ? SAMPLE : JSON.parse((await readStdin()) || '{}');
  const method = String(request.method || 'POST').toUpperCase();
  const uri = String(request.uri || '/');
  const payload = request.payload && typeof request.payload === 'object' ? request.payload : {};
  const cookies = request.cookies && typeof request.cookies === 'object' ? request.cookies : {};

  const headers = normalise(await impl.sign(method, uri, payload, cookies));
  process.stdout.write(JSON.stringify(headers));

  if (selftest) {
    process.stderr.write(`\n自检通过：${JSON.stringify(headers)}\n`);
  }
  return 0;
}

main()
  .then((code) => process.exit(code))
  .catch((error) => {
    process.stderr.write(`签名失败：${error && error.stack ? error.stack : error}\n`);
    process.exit(1);
  });
