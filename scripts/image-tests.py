#!/usr/bin/env python3
"""Exercise a Madsonic image on the builder with Docker and dummy credentials.

Usage: image-tests.py IMAGE

Scenarios (each with its own volume, labelled and removed afterwards):
  fresh         new install with a password containing & # + % and a space:
                one installed callback after setup, the account works over the
                API and the web login, admin:admin is refused, no secret in logs
  restart       the same container restarted: no setup, no callback
  empty         MAD_PASSWORD empty: exits 1 before starting Madsonic, no callback
  unverified    MAD_USERNAME=admin, so admin:admin cannot be disabled: setup
                verification fails, exit 1, no callback, no configured marker
  upgrade       the current published image installs, the customer changes the
                password, then this image starts on the same volume: no setup,
                the changed password still works, one callback; then a restart
Callbacks go to a local mock API; api.cylo.net and api.cylo.io resolve to
127.0.0.1 inside every container, so nothing reaches the real API.
"""
import http.cookiejar
import http.server
import importlib.util
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid

spec = importlib.util.spec_from_file_location('registry_check', Path(__file__).with_name('registry-check.py'))
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)

if len(sys.argv) != 2:
    raise SystemExit('Usage: image-tests.py IMAGE')
image = sys.argv[1]
run_id = 'madsonic-gate-' + uuid.uuid4().hex[:10]
label = 'appbox.release-gate=' + run_id
containers = []
volumes = []
hidden = []
callbacks = []


def hide(value):
    hidden.append(value)
    hidden.append(value.encode().hex())
    return value


def redact(text):
    for value in hidden:
        text = text.replace(value, '[redacted]')
    return text


def docker(*args, check=True, timeout=1800):
    result = subprocess.run(['docker', *args], capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError('docker ' + args[0] + ' failed: ' + redact(result.stderr.decode(errors='replace')[-1500:]))
    return result


class Callback(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        target = urllib.parse.urlsplit(self.path)
        match = re.fullmatch(r'/v1/apps/installed/(\d+)', target.path)
        callbacks.append({
            'instance': match.group(1) if match else target.path,
            'query_token': urllib.parse.parse_qs(target.query).get('callback_token', [None])[0],
            'header_token': self.headers.get('X-Cylo-Callback-Token'),
        })
        self.send_response(200 if match else 404)
        self.end_headers()

    def log_message(self, *args):
        pass


gateway = json.loads(docker('network', 'inspect', 'bridge').stdout)[0]['IPAM']['Config'][0]['Gateway']
server = http.server.ThreadingHTTPServer((gateway, 0), Callback)
threading.Thread(target=server.serve_forever, daemon=True).start()
callback_base = 'http://host.docker.internal:' + str(server.server_port)


def volume(suffix):
    name = run_id + '-' + suffix
    docker('volume', 'create', '--label', label, name)
    volumes.append(name)
    return name


def install_env(instance, username, password, email, query_token=None, header_token=None):
    env = {
        'MAD_USERNAME': username,
        'MAD_PASSWORD': password,
        'MAD_EMAIL': email,
        'MAD_URL': 'https://madsonic.example.test',
        'INSTANCE_ID': instance + ('?callback_token=' + query_token if query_token else ''),
        'APPBOX_CALLBACK_BASE': callback_base,
    }
    if header_token:
        env['CALLBACK_TOKEN'] = header_token
    return env


def launch(suffix, selected_image, config_volume, env):
    name = run_id + '-' + suffix
    args = ['create', '--platform', 'linux/amd64', '--name', name, '--label', label, '--init',
            '--add-host', 'host.docker.internal:host-gateway',
            '--add-host', 'api.cylo.net:127.0.0.1', '--add-host', 'api.cylo.io:127.0.0.1',
            '-p', '127.0.0.1::80', '-v', config_volume + ':/config']
    for key, value in env.items():
        args += ['-e', key + '=' + value]
    # Track the name first: a slow create can time out here and still finish.
    containers.append(name)
    docker(*args, selected_image)
    docker('start', name)
    return name


def logs(name, since=None):
    args = ['logs'] + (['--since', since] if since else []) + [name]
    result = subprocess.run(['docker', *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
    return result.stdout.decode(errors='replace')


def evidence(name):
    return '\n--- last log lines of ' + name + ' ---\n' + redact('\n'.join(logs(name).splitlines()[-40:]))


def running(name):
    return docker('inspect', '-f', '{{.State.Running}}', name).stdout.decode().strip() == 'true'


def started_at(name):
    return docker('inspect', '-f', '{{.State.StartedAt}}', name).stdout.decode().strip()


def base_url(name):
    return 'http://' + docker('port', name, '80/tcp').stdout.decode().splitlines()[0].strip()


def api(name, view, user, password, encode=True, **params):
    query = {'u': user, 'p': ('enc:' + password.encode().hex()) if encode else password,
             'v': '2.6.0', 'c': 'cylo', 'f': 'json', **params}
    url = base_url(name) + '/rest2/' + view + '.view?' + urllib.parse.urlencode(query)
    with urllib.request.urlopen(url, timeout=20) as response:
        body = json.loads(response.read())['madsonic-response']
    return body['status'] if body['status'] == 'ok' else 'failed ' + str(body.get('error', {}).get('code'))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def web_login(name, user, password):
    opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.open(base_url(name) + '/login.view', timeout=20).read()
    data = urllib.parse.urlencode({'j_username': user, 'j_password': password}).encode()
    try:
        opener.open(base_url(name) + '/j_spring_security_check', data=data, timeout=20).read()
        return 'no redirect'
    except urllib.error.HTTPError as error:
        return str(error.code) + ' ' + urllib.parse.urlsplit(error.headers.get('Location', '')).path


def wait_api(name, timeout=1800):
    deadline = time.time() + timeout
    last = ''
    while time.time() < deadline:
        if not running(name):
            raise RuntimeError(name + ' stopped while waiting for the API' + evidence(name))
        try:
            api(name, 'ping', 'admin', 'admin')
            return
        except Exception as error:
            last = type(error).__name__
            time.sleep(3)
    raise RuntimeError(name + ' API not ready: ' + last + evidence(name))


def wait_exit(name, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not running(name):
            return int(docker('inspect', '-f', '{{.State.ExitCode}}', name).stdout.decode().strip())
        time.sleep(2)
    raise RuntimeError(name + ' did not exit' + evidence(name))


def callbacks_for(instance):
    return [c for c in callbacks if c['instance'] == instance]


def wait_callback(name, instance, timeout=1800):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if callbacks_for(instance):
            return
        if not running(name):
            raise RuntimeError(name + ' stopped before its callback' + evidence(name))
        time.sleep(2)
    raise RuntimeError(name + ' sent no installed callback' + evidence(name))


def volume_files(name):
    """Appbox markers present in /config of container NAME (running or stopped)."""
    found = set()
    for marker in ('.appbox_configured', '.appbox_install_pending'):
        if docker('cp', name + ':/config/' + marker, '-', check=False).returncode == 0:
            found.add(marker)
    return found


def check_no_secrets(name):
    text = logs(name)
    text += docker('exec', name, 'cat', '/config/madsonic.log', check=False).stdout.decode(errors='replace')
    leaked = [i for i, value in enumerate(hidden) if value in text]
    assert not leaked, 'secret value(s) in logs of ' + name


def check_runtime(name):
    pid = docker('exec', name, 'pgrep', 'java').stdout.decode().split()[0]
    status = docker('exec', name, 'cat', '/proc/' + pid + '/status').stdout.decode()
    assert re.search(r'^Uid:\s+1000\s+1000\s+1000\s+1000$', status, re.M), 'Madsonic is not running as UID 1000'


try:
    details = json.loads(docker('image', 'inspect', image).stdout)[0]
    assert details['Architecture'] == 'amd64' and details['Os'] == 'linux'
    assert details['Config']['Cmd'] == ['/sbin/tini', '--', 'start.sh'], details['Config']['Cmd']
    script = docker('run', '--rm', '--network', 'none', '--entrypoint', 'sh', image, '-c',
                    'sh -n /usr/local/bin/start.sh && cat /usr/local/bin/start.sh').stdout
    assert b'set -x' not in script.replace(b'Do not add `set -x`', b'')
    print('PASS static: linux/amd64, unchanged command, start.sh parses, no set -x', flush=True)

    # The current image takes longest; start it first.
    legacy_password = hide('Legacy' + secrets.token_hex(6))
    legacy_volume = volume('upgrade')
    legacy_env = install_env('900000731', 'legacyuser', legacy_password, 'legacy@example.com')
    baseline = launch('baseline', registry.CURRENT, legacy_volume, legacy_env)

    empty_volume = volume('empty')
    empty = launch('empty', image, empty_volume,
                   install_env('900000732', 'emptyuser', '', 'empty@example.com', hide(secrets.token_urlsafe(16))))
    assert wait_exit(empty, 900) == 1, 'empty MAD_PASSWORD did not exit 1'
    assert 'MAD_USERNAME and MAD_PASSWORD must be set' in logs(empty)
    assert 'STARTING JAVA' not in logs(empty)
    assert volume_files(empty) == set()
    print('PASS empty: exits 1 before starting Madsonic; nothing marked', flush=True)

    unverified_volume = volume('unverified')
    unverified = launch('unverified', image, unverified_volume,
                        install_env('900000733', 'admin', hide('Unverified' + secrets.token_hex(6)),
                                    'unverified@example.com', hide(secrets.token_urlsafe(16))))

    fresh_password = hide('Gate&#+% ' + secrets.token_hex(6) + ' =?;')
    fresh_query = hide(secrets.token_urlsafe(18))
    fresh_header = hide(secrets.token_urlsafe(18))
    fresh_volume = volume('fresh')
    fresh = launch('fresh', image, fresh_volume,
                   install_env('900000734', 'releasegate', fresh_password, 'gate+test@example.com',
                               fresh_query, fresh_header))

    assert wait_exit(unverified, 1800) == 1, 'unverified setup did not exit 1'
    assert 'the default admin login is still accepted' in logs(unverified), evidence(unverified)
    assert not callbacks_for('900000733'), 'callback sent after failed verification'
    assert volume_files(unverified) == {'.appbox_install_pending'}
    check_no_secrets(unverified)
    print('PASS unverified: admin:admin still accepted, exit 1, no callback, no configured marker', flush=True)

    wait_callback(fresh, '900000734')
    wait_api(fresh)
    sent = callbacks_for('900000734')
    assert len(sent) == 1, sent
    assert sent[0]['query_token'] == fresh_query and sent[0]['header_token'] == fresh_header
    assert api(fresh, 'ping', 'releasegate', fresh_password) == 'ok'
    assert api(fresh, 'ping', 'releasegate', fresh_password, encode=False) == 'ok'
    assert api(fresh, 'ping', 'releasegate', fresh_password.split('&')[0]) == 'failed 40'
    assert api(fresh, 'ping', 'admin', 'admin') == 'failed 40'
    assert web_login(fresh, 'releasegate', fresh_password) == '302 /index.view'
    assert web_login(fresh, 'admin', 'admin') != '302 /index.view'
    user = urllib.request.urlopen(base_url(fresh) + '/rest2/getUser.view?' + urllib.parse.urlencode({
        'u': 'releasegate', 'p': 'enc:' + fresh_password.encode().hex(), 'v': '2.6.0', 'c': 'cylo', 'f': 'json',
        'username': 'releasegate'}), timeout=20)
    user = json.loads(user.read())['madsonic-response']['user']
    assert user['adminRole'] is True and user['email'] == 'gate+test@example.com', user
    assert 'Madsonic setup verified' in logs(fresh)
    assert volume_files(fresh) == {'.appbox_configured'}
    check_runtime(fresh)
    check_no_secrets(fresh)
    print('PASS fresh: special-character password works (API and web), admin:admin refused, '
          'one authenticated callback after verification, no secrets in logs', flush=True)

    docker('restart', '--time', '30', fresh)
    since = started_at(fresh)
    wait_api(fresh)
    time.sleep(20)
    after = logs(fresh, since)
    assert 'Madsonic is already set up.' in after and 'ADDING NEW ADMIN USER' not in after, redact(after[-3000:])
    assert len(callbacks_for('900000734')) == 1, 'restart sent another callback'
    assert api(fresh, 'ping', 'releasegate', fresh_password) == 'ok'
    assert api(fresh, 'ping', 'admin', 'admin') == 'failed 40'
    check_runtime(fresh)
    check_no_secrets(fresh)
    print('PASS restart: no setup, no callback, account unchanged', flush=True)
    docker('stop', '--time', '30', fresh)

    deadline = time.time() + 2400
    while logs(baseline).count('STARTING JAVA') < 2:
        if time.time() > deadline or not running(baseline):
            raise RuntimeError('current image did not finish its setup' + evidence(baseline))
        time.sleep(5)
    wait_api(baseline)
    assert api(baseline, 'ping', 'legacyuser', legacy_password) == 'ok'
    assert api(baseline, 'ping', 'admin', 'admin') == 'failed 40'
    changed_password = hide('Changed' + secrets.token_hex(6))
    assert api(baseline, 'changePassword', 'legacyuser', legacy_password,
               username='legacyuser', password='enc:' + changed_password.encode().hex()) == 'ok'
    assert api(baseline, 'ping', 'legacyuser', changed_password) == 'ok'
    docker('stop', '--time', '30', baseline)
    assert volume_files(baseline) == set()

    upgraded = launch('upgraded', image, legacy_volume, legacy_env)
    wait_callback(upgraded, '900000731', timeout=900)
    wait_api(upgraded)
    up_logs = logs(upgraded)
    assert 'Existing Madsonic database found; keeping its users.' in up_logs
    assert 'ADDING NEW ADMIN USER' not in up_logs
    assert len(callbacks_for('900000731')) == 1
    assert api(upgraded, 'ping', 'legacyuser', changed_password) == 'ok'
    assert api(upgraded, 'ping', 'legacyuser', legacy_password) == 'failed 40'
    assert api(upgraded, 'ping', 'admin', 'admin') == 'failed 40'
    assert web_login(upgraded, 'legacyuser', changed_password) == '302 /index.view'
    assert volume_files(upgraded) == {'.appbox_configured'}
    check_runtime(upgraded)
    check_no_secrets(upgraded)
    docker('restart', '--time', '30', upgraded)
    since = started_at(upgraded)
    wait_api(upgraded)
    time.sleep(20)
    assert 'Madsonic is already set up.' in logs(upgraded, since)
    assert len(callbacks_for('900000731')) == 1, 'restart after upgrade sent another callback'
    assert api(upgraded, 'ping', 'legacyuser', changed_password) == 'ok'
    print('PASS upgrade from the current image: users and the changed password kept, no setup, '
          'one callback, restart quiet', flush=True)
    print('PASS image tests', flush=True)
except Exception as error:
    where = traceback.extract_tb(error.__traceback__)[-1]
    print('FAIL image tests (line ' + str(where.lineno) + ': ' + redact(where.line or '') + '): '
          + type(error).__name__ + ': ' + redact(str(error)), flush=True)
    raise SystemExit(1)
finally:
    for name in reversed(containers):
        try:
            inspected = docker('inspect', '--format', '{{index .Config.Labels "appbox.release-gate"}}', name, check=False)
            if inspected.returncode and b'No such' in inspected.stderr:
                continue
            found = inspected.stdout.decode().strip()
            if found != run_id:
                raise RuntimeError('Container cleanup identity mismatch')
            docker('rm', '-f', name)
        except Exception as error:
            print('Cleanup requires attention:', name, type(error).__name__, flush=True)
    for name in volumes:
        try:
            found = docker('volume', 'inspect', '--format', '{{index .Labels "appbox.release-gate"}}', name).stdout.decode().strip()
            if found != run_id:
                raise RuntimeError('Volume cleanup identity mismatch')
            docker('volume', 'rm', name)
        except Exception as error:
            print('Cleanup requires attention:', name, type(error).__name__, flush=True)
    server.shutdown()
