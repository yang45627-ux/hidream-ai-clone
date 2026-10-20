"""In-memory per-connection rate bounds; identifiers expire and are never logged."""
import hashlib,secrets,threading,time
_lock=threading.Lock();_seed=secrets.token_bytes(32);_buckets={}

def allow(peer,route,limit,now=None):
    now=time.monotonic() if now is None else now;window=int(now//60)
    key=(hashlib.sha256(_seed+peer.encode()).digest(),route,window)
    with _lock:
        if len(_buckets)>10000:
            for item in list(_buckets):
                if item[2]<window:_buckets.pop(item,None)
            if len(_buckets)>10000:return False
        count=_buckets.get(key,0)
        if count>=limit:return False
        _buckets[key]=count+1;return True
