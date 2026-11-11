"""Private loopback OSS protocol fixture; no real cloud credentials or objects."""
import base64
from email.utils import formatdate
import hashlib
import http.server
import json
import threading
from urllib.parse import urlsplit,unquote,parse_qs
from urllib.error import HTTPError
import urllib.request
from urllib.request import Request,urlopen

class OssFixture:
    def __init__(self):
        self.objects={};self.requests=[]
        fixture=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def record(self):
                key=unquote(urlsplit(self.path).path)
                fixture.requests.append({'method':self.command,'key':key,'headers':dict(self.headers)})
                return key
            def cors(self):
                origin=self.headers.get('Origin')
                if origin:self.send_header('Access-Control-Allow-Origin',origin)
                self.send_header('Access-Control-Allow-Methods','PUT, GET, HEAD')
                self.send_header('Access-Control-Allow-Headers',self.headers.get('Access-Control-Request-Headers','*'))
                self.send_header('Vary','Origin')
            def error(self,status,code):
                data=('<Error><Code>'+code+'</Code><Message>Fixture error</Message><RequestId>relay-fixture</RequestId></Error>').encode()
                self.send_response(status);self.cors();self.send_header('Content-Type','application/xml');self.send_header('x-oss-error-code',code);self.send_header('x-oss-request-id','relay-fixture')
                self.send_header('Content-Length',str(len(data)));self.end_headers()
                if self.command!='HEAD':self.wfile.write(data)
            def do_OPTIONS(self):
                self.record();self.send_response(204);self.cors();self.end_headers()
            def do_PUT(self):
                key=self.record();data=self.rfile.read(int(self.headers.get('Content-Length','0')))
                if self.headers.get('Content-MD5')!=base64.b64encode(hashlib.md5(data).digest()).decode():self.error(400,'InvalidDigest');return
                if key in fixture.objects and self.headers.get('x-oss-forbid-overwrite')=='true':self.error(409,'FileAlreadyExists');return
                fixture.objects[key]={'data':data,'headers':{k.lower():v for k,v in self.headers.items() if k.lower().startswith('x-oss-meta-') or k.lower()=='content-type'}}
                self.send_response(200);self.cors();self.send_header('ETag','"'+hashlib.md5(data).hexdigest()+'"');self.send_header('Content-Length','0');self.end_headers()
            def get(self):
                key=self.record();item=fixture.objects.get(key)
                if item is None:self.error(404,'NoSuchKey');return
                override=parse_qs(urlsplit(self.path).query)
                if 'response-content-type' in override:self.error(400,'InvalidRequest');return
                self.send_response(200);self.cors()
                for name,value in item['headers'].items():self.send_header(name,value)
                if 'response-content-disposition' in override:self.send_header('Content-Disposition',override['response-content-disposition'][0])
                if 'response-cache-control' in override:self.send_header('Cache-Control',override['response-cache-control'][0])
                self.send_header('Content-Length',str(len(item['data'])));self.send_header('ETag','"'+hashlib.md5(item['data']).hexdigest()+'"');self.send_header('Last-Modified',formatdate(usegmt=True));self.end_headers()
                if self.command!='HEAD':self.wfile.write(item['data'])
            do_GET=get
            do_HEAD=get
            def do_DELETE(self):
                key=self.record();fixture.objects.pop(key,None);self.send_response(204);self.end_headers()
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)
    def environment(self):
        return {'RELAY_OSS_ENDPOINT':self.url,'RELAY_OSS_UPLOAD_ENDPOINT':self.url,'RELAY_OSS_REGION':'cn-hangzhou','RELAY_OSS_BUCKET':'fixture','RELAY_OSS_ACCESS_KEY_ID':'fixture-access-key','RELAY_OSS_ACCESS_KEY_SECRET':'fixture-secret','RELAY_OSS_PREFIX':'relay-test/'}
    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)

def claims(data,name='fixture.png',mime='image/png'):
    return {'name':name,'mime':mime,'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'md5':base64.b64encode(hashlib.md5(data).digest()).decode()}

def put(ticket,data):
    with urlopen(Request(ticket['url'],data=data,method='PUT',headers=ticket['headers']),timeout=15) as response:
        assert response.status==200

def upload_image(client,data,name='fixture.png',mime='image/png'):
    ticket=client.request('POST','/api/images/uploads',claims(data,name,mime))
    put(ticket,data)
    return client.request('POST','/api/images/uploads/'+ticket['uploadId']+'/complete',{})

def image_redirect(client,image_id):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,req,fp,code,msg,headers,newurl):return None
    handlers=[NoRedirect()]
    headers={}
    if hasattr(client,'jar'):handlers.append(urllib.request.HTTPCookieProcessor(client.jar))
    else:headers['Authorization']='Bearer '+client.token
    try:
        urllib.request.build_opener(*handlers).open(Request(client.server+'/api/images/'+image_id,headers=headers),timeout=15)
    except HTTPError as response:
        assert response.code==302
        assert response.read()==b''
        assert 'no-store' in response.headers['Cache-Control']
        return response.headers['Location']
    raise AssertionError('Expected an authenticated redirect to OSS')
