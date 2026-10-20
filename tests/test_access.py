import http.client,json,threading,unittest,tempfile
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
import server

class PublicAccess(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.old=server.DB;server.DB=Path(self.temp.name)/'access.db'
        self.token='test-only-random-access-token-123456'
        self.env=patch.dict('os.environ',{'APP_ACCESS_TOKEN':self.token,'APP_PUBLIC_ORIGIN':'https://venture.example'})
        self.env.start();self.http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.thread.join();self.env.stop();server.DB=self.old;self.temp.cleanup()
    def request(self,path,data=None,cookie=None,origin=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.http.server_port)
        headers={'Content-Type':'application/json'}
        if cookie:headers['Cookie']=cookie
        if origin:headers['Origin']=origin
        conn.request('POST' if data is not None else 'GET',path,json.dumps(data) if data is not None else None,headers)
        r=conn.getresponse();result=(r.status,dict(r.getheaders()),r.read());conn.close();return result
    def test_anonymous_health_only(self):
        self.assertEqual(self.request('/api/health')[0],200)
        self.assertEqual(self.request('/api/state')[0],401)
        status,_,body=self.request('/');self.assertEqual(status,200);self.assertIn('访问密码'.encode(),body)
        self.assertEqual(self.request('/api/projects',{'name':'禁止创建'})[0],401)
    def test_login_cookie_origin_and_logout(self):
        self.assertEqual(self.request('/api/login',{'password':'wrong'})[0],401)
        status,headers,_=self.request('/api/login',{'password':self.token},origin='https://venture.example');self.assertEqual(status,200)
        cookie=headers['Set-Cookie'];self.assertIn('HttpOnly',cookie);self.assertIn('Secure',cookie)
        self.assertEqual(self.request('/api/state',cookie=cookie)[0],200)
        self.assertEqual(self.request('/api/projects',{'name':'不允许'},cookie=cookie,origin='https://other.example')[0],403)
        self.assertIn('Max-Age=0',self.request('/api/logout',{},cookie=cookie,origin='https://venture.example')[1]['Set-Cookie'])
    def test_authenticated_mutation_behind_https_proxy(self):
        cookie='venture_access='+server.access_cookie(self.token)
        status,_,body=self.request('/api/projects',{'name':'测试','idea':'部署验证','budget':'1'},cookie=cookie,origin='https://venture.example')
        self.assertEqual(status,200);self.assertTrue(json.loads(body)['id'])
        _,_,body=self.request('/api/state',cookie=cookie);self.assertNotIn(self.token,body.decode());self.assertEqual(len(json.loads(body)['projects']),1)
    def test_unicode_wrong_password_returns_401(self):
        self.assertEqual(self.request('/api/login',{'password':'这是错误的中文密码🔐'})[0],401)
    def test_unicode_password_login_and_cookie(self):
        token='中文访问密码🔐-'+('安全配置'*6)
        with patch.dict('os.environ',{'APP_ACCESS_TOKEN':token}):
            status,headers,_=self.request('/api/login',{'password':token})
            self.assertEqual(status,200)
            cookie=headers['Set-Cookie']
            self.assertTrue(cookie.isascii());self.assertNotIn(token,cookie)
            self.assertEqual(self.request('/api/state',cookie=cookie)[0],200)
            self.assertEqual(self.request('/api/login',{'password':token+'错'})[0],401)
