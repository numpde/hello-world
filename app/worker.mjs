/** Runs npm only inside the immutable, networkless Node toolchain container. */
import {execFileSync} from 'node:child_process';
import {copyFileSync, existsSync, mkdirSync, readdirSync} from 'node:fs';
import {join} from 'node:path';

function run(program, args, options = {}) {
  execFileSync(program, args, {
    stdio: 'inherit', timeout: 180000,
    env: {...process.env, HOME: '/tmp', npm_config_cache: '/tmp/npm-cache'},
    ...options,
  });
}

function dependencies() {
  const directory = '/tmp/lock-check';
  mkdirSync(directory, {recursive: true});
  copyFileSync('/stage/package.json', join(directory, 'package.json'));
  copyFileSync('/stage/package-lock.json', join(directory, 'package-lock.json'));
  // This example has no third-party dependencies. Still exercise npm's actual
  // native lock contract with acquisition impossible at the Docker network layer.
  run('npm', ['ci', '--offline', '--ignore-scripts', '--no-audit', '--no-fund'], {cwd: directory});
}

function installed() {
  const archives = readdirSync('/dist').filter(name => name.endsWith('.tgz'));
  if (archives.length !== 1) throw Error('Exactly one npm package artifact is required');
  run('npm', ['install', '--offline', '--ignore-scripts', '--no-audit', '--no-fund',
    '--no-package-lock', '--prefix', '/output', join('/dist', archives[0])]);
  const module = '/output/node_modules/hello-world-https-egress-proxy/src/https_egress_proxy.mjs';
  if (!existsSync(module)) throw Error('The installed package is missing its proxy entrypoint');
  execFileSync(process.execPath, ['--test', '/tests/application/proxy.test.mjs'], {
    stdio: 'inherit', timeout: 180000,
    env: {...process.env, PROXY_MODULE: module, HOME: '/tmp'},
  });
}

if (process.argv.length !== 3) throw Error('Choose dependencies or installed');
if (process.argv[2] === 'dependencies') dependencies();
else if (process.argv[2] === 'installed') installed();
else throw Error('Unknown contained npm operation');
