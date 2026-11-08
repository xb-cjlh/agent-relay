"""Direct image uploads use real Java/MySQL and a separate OSS HTTP origin."""
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit,parse_qs,unquote
from scripts.devtools import Client
from oss_support import claims,put,upload_image,image_redirect
from urllib.request import urlopen
import test_accounts as accounts
PNG=accounts.PNG
from test_relay import TOKEN
from auth_support import BrowserClient

class ImageUploadsCase(unittest.TestCase):
    setUp=accounts.AccountsCase.setUp
    tearDown=accounts.AccountsCase.tearDown
    denied=accounts.AccountsCase.denied

    def test_signed_upload_requires_confirmation_and_complete_is_idempotent(self):
        c=Client(self.url,TOKEN)
        ticket=c.request('POST','/api/images/uploads',claims(PNG))
        query=parse_qs(urlsplit(ticket['url']).query)
        self.assertEqual('OSS4-HMAC-SHA256',query['x-oss-signature-version'][0])
        self.assertIn('content-length',query['x-oss-additional-headers'][0])
        self.assertEqual('image/png',ticket['headers']['Content-Type'])
        self.assertEqual(claims(PNG)['md5'],ticket['headers']['Content-MD5'])
        key=unquote(urlsplit(ticket['url']).path);image_id=key.split('/')[-1]
        self.assertIn('/relay-test/users/fixture/images/',key)
        self.denied(404,c,'GET','/api/images/'+image_id)
        self.denied(404,c,'GET','/api/images/'+image_id+'/url')
        complete='/api/images/uploads/'+ticket['uploadId']+'/complete'
        self.denied(409,c,'POST',complete,{})
        self.assertFalse(any(r['method']=='GET' for r in self.oss.requests))
        put(ticket,PNG)
        with self.assertRaises(HTTPError) as caught:put(ticket,PNG)
        self.assertEqual(409,caught.exception.code)
        image=c.request('POST',complete,{})
        before=len(self.oss.requests)
        self.assertEqual(image,c.request('POST',complete,{}))
        self.assertEqual(before,len(self.oss.requests))
        self.assertFalse(any(r['method']=='GET' for r in self.oss.requests))
        before=len(self.oss.requests)
        url=image_redirect(c,image['id'])
        self.assertEqual(before,len(self.oss.requests)) # Signing never reads OSS, even with HEAD.
        grant=c.request('GET','/api/images/'+image['id']+'/url')
        self.assertEqual(before,len(self.oss.requests))
        self.assertTrue(grant['url'].startswith(self.oss.url+'/'))
        anonymous=BrowserClient(self.url)
        self.denied(401,anonymous,'GET','/api/images/'+image['id']+'/url')
        self.denied(401,anonymous,'GET','/api/images/'+image['id'])
        query=parse_qs(urlsplit(url).query)
        self.assertEqual('OSS4-HMAC-SHA256',query['x-oss-signature-version'][0])
        self.assertLessEqual(int(query['x-oss-expires'][0]),300)
        self.assertEqual('private, max-age=300',query['response-cache-control'][0])
        self.assertNotIn('response-content-type',query) # OSS uses the immutable uploaded MIME.
        with urlopen(url) as response:self.assertEqual(PNG,response.read())
        self.denied(404,c,'POST','/api/images',{'name':'old.png','data':'not-used'})

    def test_md5_head_claims_and_download_bytes_are_checked(self):
        c=Client(self.url,TOKEN)
        ticket=c.request('POST','/api/images/uploads',claims(PNG))
        with self.assertRaises(HTTPError) as caught:put(ticket,PNG[:-1]+b'x')
        self.assertEqual(400,caught.exception.code)
        put(ticket,PNG)
        key=unquote(urlsplit(ticket['url']).path)
        item=self.oss.objects[key]
        original=item['headers']['x-oss-meta-sha256']
        item['headers']['x-oss-meta-sha256']='0'*64
        complete='/api/images/uploads/'+ticket['uploadId']+'/complete'
        self.denied(409,c,'POST',complete,{})
        item['headers']['x-oss-meta-sha256']=original
        image=c.request('POST',complete,{})
        device=accounts.AccountsCase.device(self,c)
        agent=c.request('POST','/api/agents',{'device':device['id'],'name':'image-check','kind':'codex','cwd':'/tmp'})
        c.request('POST','/api/jobs',{'kind':'prompt','agent':agent['id'],'images':[image['id']]})
        dev=Client(self.url,device['token'])
        item['data']=PNG[:-1]+b'x'
        self.denied(409,dev,'GET','/api/device/images/'+image['id'])
        malformed=upload_image(c,b'<html>not an image</html>')
        c.request('POST','/api/jobs',{'kind':'prompt','agent':agent['id'],'images':[malformed['id']]})
        self.denied(400,dev,'GET','/api/device/images/'+malformed['id'])

    def test_owner_expiry_and_pending_reservation_limits(self):
        c=Client(self.url,TOKEN)
        ticket=c.request('POST','/api/images/uploads',claims(PNG))
        other=BrowserClient(self.url);other.register(self.mail,'other@example.com')
        complete='/api/images/uploads/'+ticket['uploadId']+'/complete'
        self.denied(404,other,'POST',complete,{})
        put(ticket,PNG)
        self.server.database.execute('UPDATE image_uploads SET expires_at=0 WHERE id=?',(ticket['uploadId'],))
        self.denied(410,c,'POST',complete,{})
        for _ in range(20):c.request('POST','/api/images/uploads',claims(PNG))
        self.denied(429,c,'POST','/api/images/uploads',claims(PNG))
        self.assertFalse(self.oss.objects) # Only the expired owner's object was removed.
