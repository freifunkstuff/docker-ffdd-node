#!/usr/bin/env python3
"""Opt-in many-interface BMXD regression; isolated namespace, no real keys/peers."""
import argparse
import json
import subprocess

CAPS=('NET_ADMIN','NET_RAW','NET_BIND_SERVICE','SETUID','SETGID','CHOWN','DAC_OVERRIDE','KILL')
RUNNER=r'''
import subprocess,time,json
# No private keys, Internet routes or host interfaces in this namespace.
subprocess.run(['ip','link','add','bmxprimary','type','bridge'],check=True)
subprocess.run(['ip','addr','add','10.200.3.137/32','dev','bmxprimary'],check=True)
subprocess.run(['ip','link','set','bmxprimary','up'],check=True)
args=['bmxd','--no_fork','0','--runtime_dir','/tmp/bmxd-proof','--ogm_interval','100','-r','3','--dev=bmxprimary','/linklayer','0']
for number in range(141):
 name='proof'+str(number)
 subprocess.run(['ip','link','add',name,'type','dummy'],check=True)
 subprocess.run(['ip','addr','add','10.201.3.137/16','broadcast','10.255.255.255','dev',name],check=True)
 subprocess.run(['ip','link','set',name,'up'],check=True)
 args+=['--dev='+name,'/linklayer','1']
p=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
try:
 end=time.monotonic()+15
 while time.monotonic()<end and p.poll() is None:time.sleep(.1)
 alive=p.poll() is None
 print(json.dumps({'interfaces':142,'alive_after_15s':alive,'exit_code':p.poll()}),flush=True)
 if not alive:raise SystemExit(1)
finally:
 if p.poll() is None:p.terminate();p.wait(timeout=5)
'''

def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--image',required=True);args=parser.parse_args()
 command=['docker','run','--rm','-i','--network','none','--cap-drop','ALL']
 for cap in CAPS:command+=['--cap-add',cap]
 command+=['--security-opt','no-new-privileges','--entrypoint','python3',args.image,'-']
 result=subprocess.run(command,input=RUNNER,text=True,timeout=90)
 return result.returncode

if __name__=='__main__':raise SystemExit(main())
