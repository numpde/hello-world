#!/usr/bin/env node
/** A small, fixed-destination acquisition proxy. No third-party dependencies. */
import dns from 'node:dns/promises';
import {realpathSync} from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import {pathToFileURL} from 'node:url';

const blocked = new net.BlockList();
for (const [address, bits] of [
  ['0.0.0.0', 8], ['10.0.0.0', 8], ['100.64.0.0', 10],
  ['127.0.0.0', 8], ['169.254.0.0', 16], ['172.16.0.0', 12],
  ['192.0.0.0', 24], ['192.0.2.0', 24], ['192.88.99.0', 24],
  ['192.168.0.0', 16], ['198.18.0.0', 15], ['198.51.100.0', 24],
  ['203.0.113.0', 24], ['224.0.0.0', 4], ['240.0.0.0', 4],
]) blocked.addSubnet(address, bits, 'ipv4');

const DENIED_HEADERS = new Set([
  'connection', 'proxy-connection', 'proxy-authorization',
  'proxy-authenticate', 'keep-alive', 'te', 'trailer',
  'transfer-encoding', 'upgrade', 'content-length', 'host',
]);
const DEFAULT_TIMEOUT_MS = 5000;
const MAX_TARGET_LENGTH = 261;

function endToEndHeaders(headers) {
  const nominated = new Set(String(headers.connection ?? '').split(',')
    .map(name => name.trim().toLowerCase()).filter(Boolean));
  return Object.fromEntries(Object.entries(headers).filter(([name]) =>
    !DENIED_HEADERS.has(name) && !nominated.has(name)));
}

export class Rejected extends Error {
  constructor(message, status = 403) {
    super(message);
    this.status = status;
  }
}

export function normalizeHost(value) {
  if (typeof value !== 'string' || !value || value.length > 253 ||
      net.isIP(value) || value.includes(':')) throw new Rejected('Invalid destination host', 400);
  const host = value.toLowerCase().replace(/\.$/, '');
  if (!host.split('.').every(part =>
    /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(part))) {
    throw new Rejected('Invalid destination host', 400);
  }
  return host;
}

export function allowedHosts(value) {
  const entries = Array.isArray(value) ? value : String(value ?? '').split(',');
  if (entries.length === 0 || entries.some(host => !String(host).trim())) {
    throw new Rejected('A nonempty exact-host allowlist is required', 400);
  }
  return new Set(entries.map(host => normalizeHost(host.trim())));
}

export function publicIPv4(address) {
  return net.isIP(address) === 4 && !blocked.check(address, 'ipv4');
}

function requireHost(host, list) {
  const admitted = normalizeHost(host);
  if (!list.has(admitted)) throw new Rejected('Destination not allowed');
  return admitted;
}

export function connectTarget(authority, list) {
  if (typeof authority !== 'string' || authority.length > MAX_TARGET_LENGTH ||
      !/^[a-zA-Z0-9.-]+:[0-9]+$/.test(authority)) {
    throw new Rejected('Invalid CONNECT authority', 400);
  }
  const [host, port] = authority.split(':');
  if (port !== '443') throw new Rejected('CONNECT requires port 443');
  return requireHost(host, list);
}

export function httpTarget(raw, list) {
  if (typeof raw !== 'string' || raw.length > 8192 || !/^http:\/\//i.test(raw)) {
    throw new Rejected('Expected an absolute HTTP URL', 400);
  }
  let target;
  try { target = new URL(raw); } catch { throw new Rejected('Invalid HTTP URL', 400); }
  if (target.protocol !== 'http:' || target.username || target.password ||
      target.hash || (target.port && target.port !== '80')) {
    throw new Rejected('Unsupported HTTP target', 400);
  }
  return {host: requireHost(target.hostname, list), path: target.pathname + target.search};
}

function deadline(promise, duration) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Deadline elapsed')), duration);
    Promise.resolve(promise).then(
      value => { clearTimeout(timer); resolve(value); },
      error => { clearTimeout(timer); reject(error); },
    );
  });
}

export async function addressesFor(host, lookup = dns.lookup, timeoutMs = DEFAULT_TIMEOUT_MS) {
  const answers = await deadline(lookup(host, {all: true, family: 4}), timeoutMs);
  if (!Array.isArray(answers) || answers.length === 0 ||
      answers.some(answer => answer.family !== 4 || !publicIPv4(answer.address))) {
    throw new Rejected('Destination DNS address is not public');
  }
  return [...new Set(answers.map(answer => answer.address))];
}

function connectOne(address, port, factory, duration) {
  return new Promise((resolve, reject) => {
    let settled = false;
    let socket;
    try { socket = factory(address, port); } catch (error) { reject(error); return; }
    const timer = setTimeout(() => socket.destroy(new Error('Connection timeout')), duration);
    const done = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      socket.off('connect', onConnect);
      socket.off('error', onError);
      if (error) { socket.destroy(); reject(error); } else resolve(socket);
    };
    const onConnect = () => done();
    const onError = error => done(error);
    socket.once('connect', onConnect);
    socket.once('error', onError);
    if (!socket.connecting && socket.readyState === 'open') done();
  });
}

export async function connectFirst(addresses, port, factory, timeoutMs) {
  const until = Date.now() + timeoutMs;
  let lastError;
  for (const address of addresses) {
    const remaining = until - Date.now();
    if (remaining <= 0) break;
    try { return await connectOne(address, port, factory, remaining); }
    catch (error) { lastError = error; }
  }
  throw lastError ?? new Error('Connection deadline elapsed');
}

function writeFailure(socket, code) {
  if (!socket.destroyed) socket.end(
    'HTTP/1.1 ' + code + ' ' + (code === 403 ? 'Forbidden' : code === 400 ? 'Bad Request' : 'Bad Gateway') + '\r\n' +
    'Connection: close\r\nContent-Length: 0\r\n\r\n',
  );
}

/** A caller can inject local transport fixtures; the production CLI never does. */
export function createProxy({hosts, lookup = dns.lookup,
  dial = (address, port) => net.connect({host: address, port}),
  connectTimeoutMs = DEFAULT_TIMEOUT_MS, idleTimeoutMs = 30000,
  maxConnections = 32} = {}) {
  const list = allowedHosts(hosts);
  if (![connectTimeoutMs, idleTimeoutMs, maxConnections].every(value =>
    Number.isSafeInteger(value) && value > 0)) throw new Error('Expected positive limits');
  const sockets = new Set();
  const server = http.createServer({maxHeaderSize: 16384}, async (request, response) => {
    if (request.method === 'GET' && request.url === '/healthz') {
      response.writeHead(204, {'connection': 'close'});
      response.end();
      return;
    }
    if (!['GET', 'HEAD'].includes(request.method)) {
      response.writeHead(405, {'connection': 'close'});
      response.end();
      return;
    }
    let target;
    try {
      target = httpTarget(request.url, list);
      if (request.headers['transfer-encoding'] || Number(request.headers['content-length'] ?? 0) > 0) {
        throw new Rejected('Acquisition GET/HEAD cannot carry a body', 400);
      }
      const addresses = await addressesFor(target.host, lookup, connectTimeoutMs);
      const headers = endToEndHeaders(request.headers);
      headers.host = target.host;
      headers.connection = 'close';
      // The chosen numeric address is used directly: no second DNS resolution.
      const agent = new http.Agent({keepAlive: false});
      agent.createConnection = () => dial(addresses[0], 80);
      const upstream = http.request({
        host: addresses[0], port: 80, method: request.method,
        path: target.path, headers, agent,
      }, backend => {
        if (!response.headersSent) {
          const downstreamHeaders = endToEndHeaders(backend.headers);
          response.writeHead(backend.statusCode, downstreamHeaders);
          backend.pipe(response);
        }
      });
      upstream.setTimeout(connectTimeoutMs, () => upstream.destroy(new Error('Upstream timeout')));
      upstream.on('error', () => {
        if (response.headersSent) response.destroy();
        else { response.writeHead(502, {'connection': 'close'}); response.end(); }
      });
      response.on('close', () => upstream.destroy());
      upstream.end();
    } catch (error) {
      if (response.headersSent) response.destroy();
      else { response.writeHead(error instanceof Rejected ? error.status : 502,
        {'connection': 'close'}); response.end(); }
    }
  });
  server.maxConnections = maxConnections;
  server.headersTimeout = connectTimeoutMs;
  server.requestTimeout = idleTimeoutMs;
  server.on('connection', socket => {
    sockets.add(socket);
    socket.on('close', () => sockets.delete(socket));
  });
  server.on('connect', async (request, client, head) => {
    let upstream;
    try {
      const host = connectTarget(request.url, list);
      const addresses = await addressesFor(host, lookup, connectTimeoutMs);
      upstream = await connectFirst(addresses, 443, dial, connectTimeoutMs);
      if (client.destroyed) { upstream.destroy(); return; }
      client.setTimeout(idleTimeoutMs, () => client.destroy());
      upstream.setTimeout(idleTimeoutMs, () => upstream.destroy());
      client.on('error', () => upstream.destroy());
      upstream.on('error', () => client.destroy());
      client.write('HTTP/1.1 200 Connection Established\r\n\r\n');
      if (head.length) upstream.write(head);
      client.pipe(upstream);
      upstream.pipe(client);
    } catch (error) {
      if (upstream) upstream.destroy();
      writeFailure(client, error instanceof Rejected ? error.status : 502);
    }
  });
  server.on('clientError', (_error, socket) => writeFailure(socket, 400));
  server.stop = () => new Promise(resolve => {
    server.close(() => resolve());
    for (const socket of sockets) socket.destroy();
  });
  return server;
}

export function configuration(environment = process.env) {
  const port = Number(environment.PROXY_PORT ?? '8080');
  if (!Number.isSafeInteger(port) || port < 1 || port > 65535) throw Error('PROXY_PORT must be 1..65535');
  const host = environment.PROXY_HOST ?? '127.0.0.1';
  if (!['127.0.0.1', '0.0.0.0'].includes(host)) throw Error('PROXY_HOST must be 127.0.0.1 or 0.0.0.0');
  return {host, port, hosts: environment.PROXY_ALLOWED_HOSTS};
}

async function main() {
  const {host, port, hosts} = configuration();
  const server = createProxy({hosts});
  server.listen(port, host, () => process.stdout.write('Proxy listening on ' + host + ':' + port + '\n'));
  process.on('SIGTERM', () => { server.stop().catch(() => { process.exitCode = 1; }); });
  process.on('SIGINT', () => { server.stop().catch(() => { process.exitCode = 1; }); });
}

if (process.argv[1] && import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href) {
  main().catch(error => {
    process.stderr.write('Proxy startup failed: ' + error.message + '\n');
    process.exitCode = 1;
  });
}
