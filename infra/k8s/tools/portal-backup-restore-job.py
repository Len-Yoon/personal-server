#!/usr/bin/env python3
"""Render a disposable backup restore Job with no production PVC or API token."""
import argparse
import json
import re


def render(name: str, image: str) -> dict:
    if not re.fullmatch(r'portal-restore-check-[a-z0-9-]{1,32}', name):
        raise ValueError('unexpected Job name')
    if not re.fullmatch(r'(?:[a-z0-9./:_-]+)@sha256:[0-9a-f]{64}', image):
        raise ValueError('immutable backup image required')
    return {
        'apiVersion':'batch/v1','kind':'Job',
        'metadata':{'name':name,'namespace':'personal-server','labels':{'app.kubernetes.io/name':'portal-backup-restore-check'}},
        'spec':{'backoffLimit':0,'activeDeadlineSeconds':660,'ttlSecondsAfterFinished':600,
                'template':{'metadata':{'labels':{'app.kubernetes.io/name':'portal-backup-restore-check'}},'spec':{
                    'automountServiceAccountToken':False,'restartPolicy':'Never',
                    'securityContext':{'runAsNonRoot':True,'runAsUser':10001,'runAsGroup':10001,'fsGroup':10001,'seccompProfile':{'type':'RuntimeDefault'}},
                    'containers':[{'name':'restore','image':image,'imagePullPolicy':'Never',
                        'command':['python3','/opt/restore/portal-backup-restore-check.py','--evidence','/run/evidence/evidence','--work-dir','/work','--credential-dir','/run/backup-credentials'],
                        'securityContext':{'readOnlyRootFilesystem':True,'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']}},
                        'resources':{'requests':{'cpu':'100m','memory':'256Mi','ephemeral-storage':'1Gi'},'limits':{'cpu':'1','memory':'1Gi','ephemeral-storage':'11Gi'}},
                        'volumeMounts':[{'name':n,'mountPath':p,**({'readOnly':True} if ro else {})} for n,p,ro in (
                            ('work','/work',False),('tmp','/tmp',False),('code','/opt/restore',True),('evidence','/run/evidence',True),('credentials','/run/backup-credentials',True))]}],
                    'volumes':[{'name':'work','emptyDir':{'sizeLimit':'8Gi'}},{'name':'tmp','emptyDir':{'sizeLimit':'16Mi'}},
                        {'name':'code','configMap':{'name':name+'-code','defaultMode':292}},
                        {'name':'evidence','configMap':{'name':'portal-pvc-backup-evidence','defaultMode':292,'items':[{'key':'evidence','path':'evidence'}]}},
                        {'name':'credentials','secret':{'secretName':'portal-pvc-backup-runtime','defaultMode':288,'items':[{'key':key,'path':key} for key in ('rclone-config','rclone-config-passphrase','age-identity')]}}]
                }}}
    }


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name',required=True);parser.add_argument('--image',required=True)
    args=parser.parse_args()
    print(json.dumps(render(args.name,args.image)))

if __name__=='__main__':main()
