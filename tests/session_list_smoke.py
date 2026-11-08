"""Browser checks for activity order, calendar groups, and compact session metadata."""
import sys
import datetime
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Client,Server
from playwright.sync_api import sync_playwright,expect

TOKEN='sk-relay_'+'s'*43
with tempfile.TemporaryDirectory() as temp:
    root=Path(temp)
    server=Server(('127.0.0.1',0),root/'data',TOKEN)
    url='http://127.0.0.1:'+str(server.server_port)
    client=Client(url,TOKEN)
    try:
        with sync_playwright() as p:
            chrome=Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
            browser=p.chromium.launch(**({'executable_path':str(chrome)} if chrome.is_file() else {}))
            page=browser.new_page(viewport={'width':1440,'height':1000},timezone_id='America/New_York')
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.clock.install(time=datetime.datetime(2026,11,8,17,tzinfo=datetime.timezone.utc))
            page.goto(url)
            # Calendar dates cross the autumn DST change in this timezone.
            times=page.evaluate('''() => {
                const now=new Date();
                return [0,1,3,10,45].map(days=>days ? new Date(now.getFullYear(),now.getMonth(),now.getDate()-days,12).getTime()/1000 : Date.now()/1000-60);
            }''')
            devices=[]
            for name in ['work-computer-with-a-very-long-device-name','home-computer-with-a-very-long-device-name']:
                identity=client.request('POST','/api/register',{'name':name,'platform':'test',
                    'capabilities':{'codex':True,'claude':True,'access_scope':'user','home':str(root)}})
                devices.append((identity,Client(url,identity['token'])))
            for index,(identity,device) in enumerate(devices):
                sessions=[]
                for group,stamp in enumerate(times):
                    sid=f'native-{index}-{group}'
                    name='project-with-a-very-long-name-that-must-truncate'
                    sessions.append({'kind':'claude' if index else 'codex','session_id':sid,
                        'name':name+' · '+sid,'cwd':str(root/name),'busy':False,'updated_at':stamp-index,
                        'messages':[{'id':sid,'role':'user','content':'A preview that should remain on one line and not widen the sidebar','created_at':stamp-index}]})
                device.request('POST','/api/device/poll',{'sessions':sessions})
            state=client.request('GET','/api/state')
            expected=sorted(state['agents'],key=lambda a:(-a['last_activity'],a['id']))
            server.browser_login(page)
            expect(page.locator('#workspace')).to_be_visible()
            expect(page.locator('#session-list [data-agent]')).to_have_count(10)
            assert page.locator('#session-list [data-agent]').evaluate_all('(items)=>items.map(item=>item.dataset.agent)')==[a['id'] for a in expected]
            assert page.locator('.session-group-heading').all_text_contents()==['今天','昨天','近 7 天','近 30 天','更早']
            assert page.evaluate("activityGroup(new Date(2026,10,1,0).getTime()/1000,new Date(2026,10,7,0))")=='近 7 天'
            assert len(set(a['device_id'] for a in expected[:2]))==2
            expect(page.locator('.session-device')).to_have_count(10)
            expect(page.locator('.session-name').first).to_have_text('project-with-a-very-long-name-that-must-truncate')
            expect(page.locator('#session-list .agent-claude').first).to_have_text('Claude')
            assert 'native-' in page.locator('#session-list [data-agent]').first.get_attribute('title')
            assert page.locator('.session-name').first.evaluate('(el)=>el.scrollWidth>el.clientWidth')
            assert page.locator('.session-device').first.evaluate('(el)=>el.getBoundingClientRect().width<=72')
            assert page.locator('.session-device > span:last-child').first.evaluate('(el)=>el.scrollWidth>el.clientWidth')
            assert page.locator('#session-list').evaluate('(el)=>el.scrollWidth<=el.clientWidth')
            # A newly active conversation moves across devices and calendar groups.
            older=expected[-1]
            newer=page.evaluate('Date.now()/1000')+10
            latest=next(device for identity,device in devices if identity['id']==older['device_id'])
            latest.request('POST','/api/device/poll',{'sessions':[{'kind':older['kind'],'session_id':older['session_id'],
                'name':older['name'],'cwd':older['cwd'],'busy':False,'updated_at':newer}]})
            expect(page.locator('#session-list [data-agent]').first).to_have_attribute('data-agent',older['id'])
            page.get_by_label('切换设备').select_option(devices[0][0]['id'])
            expect(page.locator('#session-list [data-agent]')).to_have_count(5)
            expect(page.locator('.session-device')).to_have_count(0)
            page.get_by_label('切换设备').select_option('')
            page.locator('#search').fill(next(d['name'] for d in state['devices'] if d['id']==devices[1][0]['id']))
            expect(page.locator('#session-list [data-agent]')).to_have_count(5)
            page.locator('#search').fill('')
            page.set_viewport_size({'width':390,'height':844})
            page.locator('#open-sidebar').click()
            expect(page.get_by_label('切换设备')).to_be_in_viewport()
            assert page.locator('#session-list').evaluate('(el)=>el.scrollWidth<=el.clientWidth')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert not errors,errors
            browser.close()
        print('Session list passed: mixed device order, five calendar groups, live activity reordering, compact names/device/Agent, tooltips, search and mobile.')
    finally:
        server.close()
