import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import {once} from 'node:events';
import {test} from 'node:test';
import {pathToFileURL} from 'node:url';

const proxyPath = process.env.PROXY_MODULE ?? new URL('../../src/https_egress_proxy.mjs', import.meta.url).href;
const proxy = await import(proxyPath.startsWith('/') ? pathToFileURL(proxyPath).href : proxyPath);
const hosts = ['registry.npmjs.org'];
const lookup = async () => [{address: '8.8.8.8', family: 4}];

async function listening(server) {
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return server.address().port;
}

async function withProxy(t, options = {}) {
  const server = proxy.createProxy({hosts, lookup, ...options});
  const port = await listening(server);
  t.after(async () => server.stop());
  return port;
}

function response(port, {method = 'GET', path = '/healthz', headers = {}} = {}) {
  return new Promise((resolve, reject) => {
    http.request({host: '127.0.0.1', port, method, path, headers, agent: false}, res => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', chunk => { body += chunk; });
      res.on('end', () => resolve({status: res.statusCode, headers: res.headers, body}));
    }).on('error', reject).end();
  });
}

function rawConnect(port, authority, body = '') {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection({host: '127.0.0.1', port});
    let received = '';
    let sent = false;
    const timer = setTimeout(() => { socket.destroy(); reject(Error('proxy reply timeout')); }, 4000);
    socket.on('connect', () => socket.write('CONNECT ' + authority + ' HTTP/1.1\r\nHost: ' + authority + '\r\n\r\n'));
    socket.on('data', chunk => {
      received += chunk.toString();
      if (body && !sent && received.startsWith('HTTP/1.1 200') && received.includes('\r\n\r\n')) {
        sent = true;
        socket.write(body);
      }
      if ((!body && received.includes('\r\n\r\n')) ||
          (body && received.includes(body) && received.includes('\r\n\r\n'))) {
        clearTimeout(timer);
        socket.destroy();
        resolve(received);
      }
    });
    socket.on('error', error => { clearTimeout(timer); reject(error); });
  });
}

test('rejects unknown hosts, non-HTTPS ports and malformed destinations', () => {
  const list = proxy.allowedHosts(hosts);
  assert.equal(proxy.connectTarget('REGISTRY.NPMJS.ORG:443', list), 'registry.npmjs.org');
  assert.throws(() => proxy.connectTarget('other.example:443', list), /not allowed/);
  assert.throws(() => proxy.connectTarget('registry.npmjs.org:80', list), /port 443/);
  assert.throws(() => proxy.connectTarget('registry.npmjs.org:443/evil', list), /Invalid/);
  assert.throws(() => proxy.connectTarget('127.0.0.1:443', list), /Invalid/);
  assert.throws(() => proxy.httpTarget('http://registry.npmjs.org:8080/data', list), /Unsupported/);
  assert.throws(() => proxy.allowedHosts(''), /allowlist/);
});

test('only public IPv4 DNS answers are admitted', async () => {
  for (const address of ['127.0.0.1', '10.1.1.1', '192.168.1.1', '169.254.1.1',
    '100.64.0.1', '203.0.113.1', '224.0.0.1']) {
    assert.equal(proxy.publicIPv4(address), false, address);
  }
  assert.equal(proxy.publicIPv4('8.8.8.8'), true);
  await assert.rejects(proxy.addressesFor('registry.npmjs.org', async () => [
    {address: '8.8.8.8', family: 4}, {address: '127.0.0.1', family: 4},
  ]), /not public/);
});

test('health works, unrelated routes and methods do not forward', async t => {
  let calls = 0;
  const port = await withProxy(t, {dial: () => { calls++; throw Error('not expected'); }});
  assert.equal((await response(port)).status, 204);
  assert.equal((await response(port, {path: '/internal'})).status, 400);
  assert.equal((await response(port, {method: 'POST', path: 'http://registry.npmjs.org/'})).status, 405);
  assert.equal(calls, 0);
});

test('CONNECT forwards opaque bytes to the admitted fixed authority', async t => {
  const backend = net.createServer(socket => socket.on('data', data => socket.write(data)));
  const upstreamPort = await listening(backend);
  t.after(() => new Promise(resolve => backend.close(resolve)));
  const port = await withProxy(t, {dial: () => net.connect({host: '127.0.0.1', port: upstreamPort})});
  const text = await rawConnect(port, 'registry.npmjs.org:443', 'request-through-tunnel');
  assert.match(text, /^HTTP\/1\.1 200 Connection Established/);
  assert.ok(text.includes('request-through-tunnel'));
});

test('policy rejects before any dial and returns fixed HTTP errors', async t => {
  let dials = 0;
  const port = await withProxy(t, {dial: () => { dials++; throw Error('must not connect'); }});
  assert.match(await rawConnect(port, 'blocked.example:443'), /^HTTP\/1\.1 403/);
  assert.equal(dials, 0);
  assert.match(await rawConnect(port, 'registry.npmjs.org:443'), /^HTTP\/1\.1 502/);
  assert.equal(dials, 1);
});

test('private DNS answer rejects CONNECT before a connection is attempted', async t => {
  let called = false;
  const port = await withProxy(t, {lookup: async () => [{address: '127.0.0.1', family: 4}],
    dial: () => { called = true; throw Error('blocked'); }});
  assert.match(await rawConnect(port, 'registry.npmjs.org:443'), /^HTTP\/1\.1 403/);
  assert.equal(called, false);
});

test('HTTP absolute-form GET forwards path and strips proxy credentials', async t => {
  let headers;
  const backend = http.createServer((req, res) => {
    headers = req.headers;
    res.writeHead(200, {'content-type': 'text/plain'});
    res.end(req.url);
  });
  const upstreamPort = await listening(backend);
  t.after(() => new Promise(resolve => backend.close(resolve)));
  const port = await withProxy(t, {dial: () => net.connect({host: '127.0.0.1', port: upstreamPort})});
  const result = await response(port, {path: 'http://registry.npmjs.org/some/package?version=1',
    headers: {'Proxy-Authorization': 'Basic secret', 'X-Test': 'fixture'}});
  assert.equal(result.status, 200);
  assert.equal(result.body, '/some/package?version=1');
  assert.equal(headers.host, 'registry.npmjs.org');
  assert.equal(headers['proxy-authorization'], undefined);
  assert.equal(headers['x-test'], 'fixture');
});

test('shutdown retires its listener without touching other services', async t => {
  const port = await withProxy(t);
  const backend = net.createServer();
  const upstreamPort = await listening(backend);
  t.after(() => new Promise(resolve => backend.close(resolve)));
  // An unrelated listener remains available while the proxy shuts down.
  assert.ok(upstreamPort > 0);
  const server = proxy.createProxy({hosts, lookup});
  const anotherPort = await listening(server);
  await server.stop();
  await assert.rejects(response(anotherPort), /ECONNREFUSED/);
  assert.equal((await response(port)).status, 204);
});

test('CONNECT preserves a response after the client half-closes its write side', async t => {
  const backend = net.createServer({allowHalfOpen: true}, socket => {
    let body = '';
    socket.on('data', bytes => { body += bytes; });
    socket.on('end', () => {
      socket.end('upstream-replied:' + body);
    });
  });
  const backendPort = await listening(backend);
  t.after(() => new Promise(resolve => backend.close(resolve)));
  const port = await withProxy(t, {dial: () => net.connect({host:'127.0.0.1',port:backendPort})});
  const client = new net.Socket({allowHalfOpen:true});
  t.after(() => client.destroy());
  client.connect(port, '127.0.0.1');
  await once(client, 'connect');
  client.write('CONNECT registry.npmjs.org:443 HTTP/1.1\r\nHost: registry.npmjs.org\r\n\r\n');
  let reply='';
  const completed = new Promise((resolve,reject)=>{
    const alarm=setTimeout(()=>reject(new Error('half-close response deadline')), 1200);
    client.on('data', chunk=>{
      reply+=chunk;
      if(reply.includes('200 Connection Established')&&!reply.includes('upstream-replied:')) {
        client.end('hello-after-CONNECT');
      }
    });
    client.on('end',()=>{clearTimeout(alarm);resolve();});
  });
  await completed;
  assert.ok(reply.includes('upstream-replied:hello-after-CONNECT'), reply);
});
