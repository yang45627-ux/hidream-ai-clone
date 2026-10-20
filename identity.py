"""Workspace accounts and expiring server-side sessions."""
import hashlib,hmac,secrets,time,re

def setup(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,username TEXT UNIQUE,salt TEXT,password_hash TEXT,disabled INTEGER DEFAULT 0,created TEXT);
    CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,account TEXT REFERENCES accounts(id),expires INTEGER);
    CREATE TABLE IF NOT EXISTS project_owners(project TEXT PRIMARY KEY REFERENCES projects(id),owner TEXT NOT NULL);''')

def create(c,d):
    import server as s
    name=s.required(d,'username');password=s.required(d,'password')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,64}',name):raise ValueError('账号使用3–64位字母、数字或._-')
    if len(password)<12:raise ValueError('独立账号密码至少12个字符')
    if c.execute('SELECT 1 FROM accounts WHERE username=?',(name,)).fetchone():raise ValueError('账号已存在')
    salt=secrets.token_hex(16);digest=hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex();i=s.uid()
    c.execute('INSERT INTO accounts VALUES(?,?,?,?,0,?)',(i,name,salt,digest,s.now()))
    return {'id':i,'username':name}

def login(c,name,password):
    account=c.execute('SELECT * FROM accounts WHERE username=? AND disabled=0',(name,)).fetchone()
    # Do comparable work for missing usernames to limit account enumeration.
    salt=bytes.fromhex(account['salt']) if account else b'\0'*16
    digest=hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1).hex()
    if not account or not hmac.compare_digest(digest,account['password_hash']):return None
    token=secrets.token_hex(32);hashed=hashlib.sha256(token.encode()).hexdigest()
    c.execute('DELETE FROM sessions WHERE expires<?',(int(time.time()),))
    c.execute('INSERT INTO sessions VALUES(?,?,?)',(hashed,account['id'],int(time.time())+86400))
    return token

def resolve(c,token):
    hashed=hashlib.sha256(token.encode()).hexdigest()
    row=c.execute('SELECT a.id,a.username FROM sessions s JOIN accounts a ON a.id=s.account WHERE s.id=? AND s.expires>? AND a.disabled=0',(hashed,int(time.time()))).fetchone()
    return dict(row) if row else None

def owns(c,owner,project):
    row=c.execute('SELECT owner FROM project_owners WHERE project=?',(project,)).fetchone()
    return (row['owner'] if row else 'root')==owner

def scoped(state,c,owner):
    ids={p['id'] for p in state['projects'] if owns(c,owner,p['id'])}
    for key,value in state.items():
        if isinstance(value,list) and key!='roles':
            state[key]=[r for r in value if isinstance(r,dict) and (r.get('project',r.get('id')) in ids)]
    return state
