"""Loopback-only push provider: independently verifies VAPID and decrypts RFC 8291."""
import base64
import hashlib
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def b64(value):return base64.urlsafe_b64encode(value).rstrip(b'=').decode()
def un64(value):return base64.urlsafe_b64decode(value+'='*((4-len(value)%4)%4))
def point(key):return key.public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
def mac(key,value):return hmac.new(key,value,hashlib.sha256).digest()
def expand(key,info,size):return mac(key,info+b'\x01')[:size]

class PushFixture:
    def __init__(self,root):
        self.vapid=ec.generate_private_key(ec.SECP256R1());self.client=ec.generate_private_key(ec.SECP256R1());self.auth=os.urandom(16)
        self.public=b64(point(self.vapid.public_key()));self.messages=[];self.errors=[];self.status=201
        self.file=root/'web-push.json'
        self.file.write_text(json.dumps({'publicKey':self.public,'privateKey':b64(self.vapid.private_bytes(serialization.Encoding.DER,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))}));self.file.chmod(0o600)
        fixture=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                try:
                    assert self.headers['Content-Encoding']=='aes128gcm'
                    fields=dict(value.strip().split('=',1) for value in self.headers['Authorization'].removeprefix('vapid ').split(','))
                    assert fields['k']==fixture.public
                    header,claims,signature=fields['t'].split('.')
                    assert json.loads(un64(header))['alg']=='ES256'
                    claims=json.loads(un64(claims));assert claims['aud']==fixture.origin
                    assert time.time()<claims['exp']<time.time()+86401
                    assert claims['sub'].startswith('https://')
                    signature=un64(signature);assert len(signature)==64
                    fixture.vapid.public_key().verify(utils.encode_dss_signature(int.from_bytes(signature[:32],'big'),int.from_bytes(signature[32:],'big')),'.'.join(fields['t'].split('.')[:2]).encode(),ec.ECDSA(hashes.SHA256()))
                    data=self.rfile.read(int(self.headers['Content-Length']));salt=data[:16];assert int.from_bytes(data[16:20],'big')==4096
                    assert data[20]==65
                    sender=data[21:86];public=ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(),sender)
                    secret=fixture.client.exchange(ec.ECDH(),public)
                    ikm=expand(mac(fixture.auth,secret),b'WebPush: info\0'+point(fixture.client.public_key())+sender,32)
                    prk=mac(salt,ikm);key=expand(prk,b'Content-Encoding: aes128gcm\0',16);nonce=expand(prk,b'Content-Encoding: nonce\0',12)
                    plain=AESGCM(key).decrypt(nonce,data[86:],None);assert plain[-1]==2
                    fixture.messages.append(json.loads(plain[:-1]));self.send_response(fixture.status);self.end_headers()
                except Exception as error:
                    fixture.errors.append(type(error).__name__);self.send_response(400);self.end_headers()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.origin='http://127.0.0.1:'+str(self.server.server_port)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def environment(self):return {'RELAY_WEB_PUSH_KEY_FILE':str(self.file),'RELAY_WEB_PUSH_TEST_ORIGIN':self.origin,'RELAY_WEB_PUSH_DISPATCH_INTERVAL_MS':'150','RELAY_WEB_PUSH_DISPATCH_INITIAL_MS':'150'}
    def subscription(self,path='/push'):return {'endpoint':self.origin+path,'keys':{'p256dh':b64(point(self.client.public_key())),'auth':b64(self.auth)}}
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
