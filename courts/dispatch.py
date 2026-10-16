"""Wake the private worker once after enqueue; polling never dispatches workflows."""
import re
import requests
from cloud.readiness import section


def enabled(config):
    return section(config,'courts').get('enabled') is True


def dispatch(config):
    settings=section(config,'courts')
    repo=settings.get('operations_repository','')
    token=settings.get('dispatch_token','')
    if not token or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo):
        return False
    try:
        with requests.Session() as session:
            session.trust_env=False
            response=session.post('https://api.github.com/repos/'+repo+'/actions/workflows/courts.yml/dispatches',
                headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json',
                         'X-GitHub-Api-Version':'2022-11-28'},json={'ref':'main'},timeout=(10,20))
            return response.status==204
    except requests.RequestException:
        return False
