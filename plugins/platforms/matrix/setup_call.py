"""One-time native call-device enrolment: hermes --run-module ...setup_call."""
import argparse
import getpass
import json
import os
from pathlib import Path
import subprocess


def main():
    from dotenv import dotenv_values, set_key
    from hermes_constants import get_hermes_home
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--create-first-identity',action='store_true',help='create cross-signing only if the server has no existing identity')
    parser.add_argument('--configure-chat',action='store_true',help='save the local recovery key and require chat encryption')
    args=parser.parse_args()
    home=Path(get_hermes_home());values=dotenv_values(home/'.env')
    def setting(name):return values.get(name) or os.environ.get(name,'')
    cfg={'homeserver':setting('MATRIX_HOMESERVER'),'user_id':setting('MATRIX_USER_ID'),
         'allowed_user':setting('MATRIX_LIVE_ALLOWED_USER') or setting('MATRIX_ALLOWED_USERS').split(',')[0],
         'store':str(home/'matrix-call/device'),'create_identity_if_missing':args.create_first_identity}
    if not cfg['homeserver'] or not cfg['user_id'] or not cfg['allowed_user']:
        parser.error('configure MATRIX_HOMESERVER, MATRIX_USER_ID and MATRIX_ALLOWED_USERS first')
    store=Path(cfg['store']);binary=home/'matrix-call/bin/hermes-matrix-call'
    password=setting('MATRIX_PASSWORD')
    recovery=setting('MATRIX_RECOVERY_KEY')
    if not password and not (store/'session.json').exists():
        password=getpass.getpass('Hermes Matrix password: ')
    if (store/'recovery.key').exists():recovery=(store/'recovery.key').read_text().strip()
    request=json.dumps({'config':cfg,'password':password,'recovery_key':recovery})
    names={'HOME','PATH','LANG','LC_ALL','TZ','SSL_CERT_FILE','SSL_CERT_DIR','HTTPS_PROXY','HTTP_PROXY','NO_PROXY','LD_LIBRARY_PATH'}
    result=subprocess.run([str(binary),'enrol'],input=request,text=True,capture_output=True,
                          env={k:v for k,v in os.environ.items() if k in names},umask=0o077,timeout=120)
    if result.returncode:
        raise SystemExit('Call device enrolment did not finish. Existing cross-signing keys are preserved; verify this device or supply its recovery key.')
    public=json.loads(result.stdout)
    if not public.get('verified'):raise SystemExit('Call device is not verified')
    if args.configure_chat:
        if (store/'recovery.key').exists():recovery=(store/'recovery.key').read_text().strip()
        if not recovery:raise SystemExit('Device enrolled; chat verification still needs MATRIX_RECOVERY_KEY')
        env_file=home/'.env';env_file.touch(mode=0o600);env_file.chmod(0o600)
        set_key(str(env_file),'MATRIX_RECOVERY_KEY',recovery)
        set_key(str(env_file),'MATRIX_E2EE_MODE','required')
    print(json.dumps(public))
    return 0


if __name__=='__main__':raise SystemExit(main())
