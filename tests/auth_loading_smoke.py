"""Browser regression: defer identity/state responses and observe the initial screen."""
import hashlib
from pathlib import Path
import secrets
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Server
from playwright.sync_api import expect,sync_playwright

def main():
 with tempfile.TemporaryDirectory() as temporary:
  server=Server(('127.0.0.1',0),Path(temporary)/'data','relay-auth-loading-fixture-token')
  base='http://127.0.0.1:'+str(server.server_port)
  cookie=secrets.token_urlsafe(32)
  server.database.execute("INSERT INTO auth_sessions(token_hash,user_id,expires_at) VALUES(?,'fixture',4102444800)",(hashlib.sha256(cookie.encode()).hexdigest(),))
  try:
   with sync_playwright() as p:
    for name,engine in [('Chromium',p.chromium),('WebKit',p.webkit)]:
     options={'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if name=='Chromium' else {}
     browser=engine.launch(**options)
     try:
      context=browser.new_context();page=context.new_page();errors=[]
      page.on('pageerror',lambda error:errors.append(str(error)))
      page.add_init_script('''
       // Keep the initial-load checks independent of background polling.
       window.setInterval=()=>0;
       window.loginWasVisible=false;
       const observe=()=>{
        const login=document.getElementById('login');
        if(login && !login.hidden && getComputedStyle(login).display!=='none')window.loginWasVisible=true;
       };
       new MutationObserver(observe).observe(document,{subtree:true,childList:true,attributes:true,attributeFilter:['hidden']});
      ''')
      identity=[];state=[]
      page.route('**/api/auth/session',lambda route:identity.append(route))
      page.route('**/api/state',lambda route:state.append(route))
      context.add_cookies([{'name':'relay_session','value':cookie,'url':base,'httpOnly':True,'sameSite':'Lax'}])
      page.goto(base,wait_until='domcontentloaded')
      expect(page.locator('#session-loading')).to_be_visible()
      page.wait_for_function('()=>document.readyState === "complete"')
      assert identity and not state
      expect(page.locator('#login')).not_to_be_visible()
      expect(page.locator('#workspace')).not_to_be_visible()
      identity.pop(0).continue_()
      # Identity is valid but loading the workspace is still pending.
      page.wait_for_function('()=>signedIn')
      expect(page.locator('#session-loading')).to_be_visible()
      expect(page.locator('#login')).not_to_be_visible()
      assert state
      state.pop(0).continue_()
      expect(page.locator('#workspace')).to_be_visible()
      assert not page.evaluate('window.loginWasVisible'),'Login flashed while the session was valid'

      # A reload with a valid cookie must remain neutral until both requests finish.
      page.reload(wait_until='domcontentloaded')
      expect(page.locator('#session-loading')).to_be_visible()
      page.wait_for_function('()=>document.readyState === "complete"')
      identity.pop(0).continue_()
      page.wait_for_function('()=>signedIn')
      state.pop(0).fulfill(status=503,content_type='application/json',body='{"error":"temporary fixture failure"}')
      expect(page.locator('#retry-session')).to_be_visible()
      expect(page.locator('#login')).not_to_be_visible()
      assert not page.evaluate('window.loginWasVisible')
      page.locator('#retry-session').click()
      expect(page.locator('#retry-session')).not_to_be_visible()
      assert identity
      identity.pop(0).continue_()
      page.wait_for_function('()=>signedIn')
      page.wait_for_timeout(50)
      assert state
      state.pop(0).continue_()
      expect(page.locator('#workspace')).to_be_visible()
      assert not page.evaluate('window.loginWasVisible')

      # A failed identity lookup is recoverable and does not pretend the cookie expired.
      page.reload(wait_until='domcontentloaded')
      page.wait_for_function('()=>document.readyState === "complete"')
      identity.pop(0).fulfill(status=503,content_type='application/json',body='{"error":"temporary fixture failure"}')
      expect(page.locator('#retry-session')).to_be_visible()
      expect(page.locator('#login')).not_to_be_visible()
      page.locator('#retry-session').click()
      identity.pop(0).continue_()
      page.wait_for_function('()=>signedIn')
      state.pop(0).continue_()
      expect(page.locator('#workspace')).to_be_visible()
      assert not page.evaluate('window.loginWasVisible')

      # Only a confirmed missing/expired session reveals the actual login form.
      context.clear_cookies()
      page.reload(wait_until='domcontentloaded')
      page.wait_for_function('()=>document.readyState === "complete"')
      expect(page.locator('#login')).not_to_be_visible()
      identity.pop(0).continue_()
      expect(page.locator('#login')).to_be_visible()
      expect(page.locator('#session-loading')).not_to_be_visible()
      expect(page.locator('#workspace')).not_to_be_visible()
      assert not errors,errors
      context.close()
      print(name+': delayed session/state, authenticated reload without login flash, failure/retry and expired cookie passed',flush=True)
     finally:browser.close()
  finally:server.close()

if __name__=='__main__':main()
