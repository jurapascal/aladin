#!/usr/bin/env python3
# Použití: python3 tools/stabilita_soak.py <url z výpisu zkušebního hubu> <pid hubu> [sekund]
"""60 s souvislé zátěže platnými požadavky + měření paměti a vláken hubu."""
import sys, time, threading, json, urllib.parse, http.client, random, collections
URL=sys.argv[1]; PID=int(sys.argv[2]); DUR=int(sys.argv[3]) if len(sys.argv)>3 else 60
u=urllib.parse.urlsplit(URL); H,P=u.hostname,u.port; T=urllib.parse.parse_qs(u.query)["t"][0]
def mem():
    st=open(f"/proc/{PID}/status").read().splitlines()
    return int([l for l in st if l.startswith("VmRSS")][0].split()[1])//1024, int([l for l in st if l.startswith("Threads")][0].split()[1])
def call(m,path,body=None):
    c=http.client.HTTPConnection(H,P,timeout=10); t=time.time()
    try:
        c.request(m,f"/api/{path}{'&' if '?' in path else '?'}t={T}",body=None if body is None else json.dumps(body).encode(),
                  headers={"Content-Type":"application/json","Origin":f"http://{H}:{P}"})
        r=c.getresponse(); r.read(); return r.status,time.time()-t
    except Exception as e: return type(e).__name__,time.time()-t
    finally: c.close()
ops=[("GET","state",None),("GET","stav-radek?id=1",None),("GET","tab-context?id=1",None),("GET","soubory-hledani?id=1&q=app",None),
     ("POST","chat-hledej",{"q":"wedos"}),("POST","chat-hledej",{"q":"prilis ftp"}),("POST","chat-hledej-stav",{"job":1}),
     ("POST","fronta",{"tab":1,"id":"a","text":"x"}),("GET","chat-export?id=00000000-0000-4000-8000-000000000000",None),
     ("POST","rewind-prehled",{"chat":"x","uuid":"y"}),("POST","chat-hledej-zrus",{})]
res=collections.Counter(); slow=[]; maxdt=[0]; stop=time.time()+DUR; lk=threading.Lock()
def w():
    while time.time()<stop:
        m,p,b=random.choice(ops); st,dt=call(m,p,b)
        with lk:
            res[st if not isinstance(st,int) else (st//100)*100]+=1; maxdt[0]=max(maxdt[0],dt)
            if dt>5: slow.append((p[:30],round(dt,1)))
r0,t0=mem(); print(f"start: RSS {r0} MB, vláken {t0}")
th=[threading.Thread(target=w) for _ in range(16)]; [t.start() for t in th]
while time.time()<stop:
    time.sleep(15); r,t=mem(); print(f"  +{int(DUR-(stop-time.time()))} s: RSS {r} MB, vláken {t}", flush=True)
[t.join() for t in th]; time.sleep(5); r1,t1=mem()
print(f"požadavků {sum(res.values())}, stavy {dict(res)}, nejdelší {maxdt[0]:.2f} s, pomalých {len(slow)}")
print(f"konec: RSS {r1} MB (+{r1-r0}), vláken {t1} (+{t1-t0})")
st,_=call("GET","state"); print("hub žije:",st)
sys.exit(0 if st==200 and not slow and not any(k in res for k in (500,"RemoteDisconnected","ConnectionRefusedError","TimeoutError","timeout")) else 1)
