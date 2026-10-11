import os
import secrets
import subprocess
from pathlib import Path
import cgir

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
env = Path('/etc/rpz-ir-sensor-api.env')
repo = Path('/home/ops/rpz-ir-sensor-api')
subprocess.run(['install', '-m', '644', str(repo / '99-rpz-ir-sensor.rules'), '/etc/udev/rules.d/99-rpz-ir-sensor.rules'], check=True)
subprocess.run(['udevadm', 'control', '--reload-rules'], check=True)
subprocess.run(['udevadm', 'trigger', '--subsystem-match=lirc'], check=True)
subprocess.run(['udevadm', 'settle'], check=True)
if not Path('/dev/rpz-ir-tx').exists() or not Path('/dev/rpz-ir-rx').exists():
    raise RuntimeError('Stable IR device symlinks missing')
if not env.exists():
    env.write_text(f'RPZ_API_KEY={secrets.token_urlsafe(32)}\nRPZ_DB=/var/lib/rpz-ir-sensor-api/codes.sqlite3\nRPZ_BME280_ADDRESS=0x76\nRPZ_IR_TX=/dev/rpz-ir-tx\nRPZ_IR_RX=/dev/rpz-ir-rx\n')
    env.chmod(0o600)
    import pwd
    user = pwd.getpwnam('ops')
    os.chown(env, user.pw_uid, user.pw_gid)
else:
    lines = [line for line in env.read_text().splitlines() if not line.startswith(('RPZ_IR_TX=', 'RPZ_IR_RX='))]
    env.write_text('\n'.join(lines) + '\nRPZ_IR_TX=/dev/rpz-ir-tx\nRPZ_IR_RX=/dev/rpz-ir-rx\n')
print('IR roles:', roles)
subprocess.run(['install', '-d', '-o', 'ops', '-g', 'ops', '-m', '700', '/var/lib/rpz-ir-sensor-api'], check=True)
subprocess.run(['install', '-m', '644', '/home/ops/rpz-ir-sensor-api/rpz-ir-sensor-api.service', '/etc/systemd/system/rpz-ir-sensor-api.service'], check=True)
subprocess.run(['systemctl', 'daemon-reload'], check=True)
subprocess.run(['systemctl', 'enable', '--now', 'rpz-ir-sensor-api'], check=True)
subprocess.run(['systemctl', 'restart', 'rpz-ir-sensor-api'], check=True)
