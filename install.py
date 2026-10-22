"""Install or update the service. Run from /opt/rpz-ir-sensor-api as the deploy user: sudo .venv/bin/python install.py"""
import grp
import os
import pwd
import secrets
import socket
import subprocess
from pathlib import Path

import cgir

SERVICE_USER = 'rpz-ir-sensor'
IR_GROUP = 'rpz-ir'
repo = Path('/opt/rpz-ir-sensor-api')
env = Path('/etc/rpz-ir-sensor-api.env')
config = Path('/etc/rpz-ir-sensor-api')
state = Path('/var/lib/rpz-ir-sensor-api')


def run(*args):
    subprocess.run(args, check=True)


if os.geteuid() != 0 or not os.environ.get('SUDO_USER'):
    raise RuntimeError('Run with sudo from the deploy user')
if Path(__file__).resolve().parent != repo:
    raise RuntimeError(f'The checkout must be at {repo}')
deploy = pwd.getpwnam(os.environ['SUDO_USER'])

roles = {}
for path in sorted(Path('/dev').glob('lirc*')):
    with path.open('rb', buffering=0) as device:
        features = cgir.Infrared.lirc_get_features(device)
        if cgir.Infrared.lirc_can_send_pulse(features):
            roles['TX'] = str(path)
        if cgir.Infrared.lirc_can_rec_mode2(features):
            roles['RX'] = str(path)
if len(roles) != 2:
    raise RuntimeError(f'IR devices missing: {roles}')
print('IR roles:', roles)

# A dedicated, non-login account and an IR-only group keep the API away from the
# deploy user's home and keep other video-group members away from the IR devices.
try:
    grp.getgrnam(IR_GROUP)
except KeyError:
    run('groupadd', '--system', IR_GROUP)
try:
    pwd.getpwnam(SERVICE_USER)
except KeyError:
    run('useradd', '--system', '--user-group', '--no-create-home', '--home-dir', '/nonexistent',
        '--shell', '/usr/sbin/nologin', SERVICE_USER)
service = pwd.getpwnam(SERVICE_USER)
# The checkout and its virtualenv (including the interpreter it links to) must be readable by the service.
subprocess.run(['runuser', '-u', SERVICE_USER, '--', str(repo / '.venv/bin/python'), '-c', 'import uvicorn, app'],
               cwd=repo, check=True)

run('install', '-m', '644', str(repo / '99-rpz-ir-sensor.rules'), '/etc/udev/rules.d/99-rpz-ir-sensor.rules')
run('udevadm', 'control', '--reload-rules')
run('udevadm', 'trigger', '--subsystem-match=lirc')
run('udevadm', 'settle')
for name in ('rpz-ir-tx', 'rpz-ir-rx'):
    device = Path('/dev') / name
    if not device.exists() or grp.getgrgid(device.stat().st_gid).gr_name != IR_GROUP:
        raise RuntimeError(f'{device} is missing or not owned by group {IR_GROUP}')

if env.exists():
    lines = [line for line in env.read_text().splitlines() if not line.startswith(('RPZ_IR_TX=', 'RPZ_IR_RX='))]
else:
    lines = [f'RPZ_API_KEY={secrets.token_urlsafe(32)}', f'RPZ_DB={state}/codes.sqlite3', 'RPZ_BME280_ADDRESS=0x76']
if not any(line.startswith('RPZ_READ_API_KEY=') for line in lines):
    lines.append(f'RPZ_READ_API_KEY={secrets.token_urlsafe(32)}')
env.write_text('\n'.join(lines + ['RPZ_IR_TX=/dev/rpz-ir-tx', 'RPZ_IR_RX=/dev/rpz-ir-rx']) + '\n')
# systemd reads this as root; the deploy user may read it for curl.
os.chown(env, 0, deploy.pw_gid)
env.chmod(0o640)

config.mkdir(mode=0o755, exist_ok=True)
key, cert = config / 'tls.key', config / 'tls.crt'
if not key.exists() or not cert.exists():
    host = socket.gethostname()
    ips = subprocess.run(['hostname', '-I'], capture_output=True, text=True, check=True).stdout.split()
    names = [f'DNS:{host}', f'DNS:{host}.local', 'DNS:localhost', 'IP:127.0.0.1', *(f'IP:{ip}' for ip in ips)]
    run('openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes', '-days', '3650',
        '-subj', f'/CN={host}', '-addext', 'subjectAltName=' + ','.join(names), '-keyout', str(key), '-out', str(cert))
os.chown(key, 0, service.pw_gid)
key.chmod(0o640)
cert.chmod(0o644)

state.mkdir(mode=0o700, exist_ok=True)
run('chown', '-R', f'{SERVICE_USER}:{SERVICE_USER}', str(state))

run('install', '-m', '644', str(repo / 'rpz-ir-sensor-api.service'), '/etc/systemd/system/rpz-ir-sensor-api.service')
run('systemctl', 'daemon-reload')
run('systemctl', 'enable', 'rpz-ir-sensor-api')
run('systemctl', 'restart', 'rpz-ir-sensor-api')
fingerprint = subprocess.run(['openssl', 'x509', '-in', str(cert), '-noout', '-fingerprint', '-sha256'],
                             capture_output=True, text=True, check=True).stdout.strip()
print('TLS certificate', fingerprint)
