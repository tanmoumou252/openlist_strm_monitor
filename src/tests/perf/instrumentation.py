import os,time,threading
from collections import defaultdict
from contextlib import contextmanager
ENABLED=os.getenv("PERF_DIAGNOSTICS")=="1"; lock=threading.Lock(); calls=defaultdict(int); ns=defaultdict(int)
@contextmanager
def timed(name):
    if not ENABLED: yield; return
    t=time.perf_counter_ns()
    try: yield
    finally:
        with lock: calls[name]+=1; ns[name]+=time.perf_counter_ns()-t
def snapshot():
    with lock: return {k:{"calls":calls[k],"seconds":ns[k]/1e9} for k in sorted(set(calls)|set(ns))}
