"""Normal Dreamina account login and independently verified account settings.

Only the website's login controls submit credentials. Settings use the observed
get_settings/update_settings contract; this module never opens the generator's
confirmation dialog or calls a generation endpoint.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from typing import Any, Callable

from .browser import BrowserError, CDPBrowser, account_proxy_url


class AutomationError(BrowserError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


_UI_LOCK = threading.RLock()
_SETTINGS_GUARD = threading.Lock()
_SETTINGS_LOCKS: dict[tuple[int, int], threading.RLock] = {}

_IDENTITY = r"""(async()=>{
 const matches=performance.getEntriesByType('resource').map(x=>x.name).filter(value=>{
   try{const u=new URL(value);return u.origin===location.origin&&u.pathname==='/passport/web/account/info/'&&
     !!u.searchParams.get('sdk_version')&&!!u.searchParams.get('aid');}
   catch(_){return false;}});
 // AU, for example, uses SDK aid=513641. A regional guess or our own
 // incomplete probe must not replace the site's real authenticated request.
 const url=matches.at(-1);if(!url)return {transport_error:true};
 const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),8000);
 try{const response=await fetch(url,{credentials:'include',signal:controller.signal});
   if(!response.ok)return {authenticated:false};
   const value=await response.json(),data=value?.data||{};
   const id=data.user_id_str||data.user_id;
   if(!id||String(id)==='0'||data.error_code)return {authenticated:false};
   return {authenticated:true,user_id:String(id),email:typeof data.email==='string'?data.email:''};
 }catch(_){return {transport_error:true};}finally{clearTimeout(timer);}
})()"""

_QUESTIONNAIRE = r"""const questionnaire=()=>{
 const expected=['Art professional','Designer','TV and film industry professional',
   'Digital marketing and e-commerce professional','Social media content creator','Tech professional','Other (please specify)'];
 const roots=[...document.querySelectorAll('[class*="user-portrait-dialog-"]')].filter(visible);
 return roots.find(root=>{
   const title=root.querySelector('[class*="title-"]'),description=root.querySelector('[class*="desc-"]');
   const options=[...root.querySelectorAll('[class*="option-text-"]')].map(text);
   const close=[...root.querySelectorAll('[class*="close-btn-"][role=button][aria-label="Close"]')].filter(visible);
   return text(title)==='What role best describes you?'&&text(description)==="We'll use this to personalize your experience."&&
     options.length===expected.length&&expected.every(label=>options.filter(x=>x===label).length===1)&&close.length===1;
 });
};"""

_PROVIDER = r"""const provider=()=>{
 const roots=[...document.querySelectorAll('.lv_new_sign_in_panel_wide_new_base_page.modal')].filter(visible);
 if(roots.length!==1)return null;
 const root=roots[0],wrapper=root.closest('.lv_new_sign_in_panel_wide-1-col-wrapper');
 if(!wrapper||!visible(wrapper)||[...root.querySelectorAll('input')].some(visible))return null;
 const expected=['Continue with Google','Continue with TikTok','Continue with Facebook','Continue with email','Continue with CapCut Mobile'];
 const choices=[...root.querySelectorAll('.lv_new_third_part_sign_in_expand-button')].filter(visible);
 const policies=[...root.querySelectorAll('.lv_new_sign_in_panel_wide-policy a.policy')].filter(visible).map(text);
 if(choices.length!==expected.length||!expected.every(label=>choices.filter(e=>text(e)===label).length===1)||
   policies.length!==2||!policies.includes('Terms of Service')||!policies.includes('Privacy Policy'))return null;
 const dialogs=[...document.querySelectorAll('[role=dialog],dialog,.lv-modal-wrapper')].filter(visible);
 if(dialogs.some(e=>!e.contains(root)&&!root.contains(e)))return null;
 return {root,email:choices.find(e=>text(e)==='Continue with email')};
};"""

_PROMOTION = r"""const promotion=()=>{
 const roots=[...document.querySelectorAll('.lv-modal-wrapper.wrapper-u1VKtB')].filter(visible);
 if(roots.length!==1)return null;
 const root=roots[0],body=text(root);
 if(!body.includes('Save up to 50% on plans to unlock Seedance.')||
   !body.includes('Seedance 2.5 at 720p from A$0.12/sec.')||
   !body.includes('Upgrade for up to 500 free Seedance 2.5 videos + 6,000 free GPT Image 2.5'))return null;
 const panels=[...root.querySelectorAll('.lv-modal.lv-modal-simple.lv-modal-closable')].filter(visible);
 const buttons=[...root.querySelectorAll('button')].filter(visible);
 const close=[...root.querySelectorAll('.lv-modal-close-icon')].filter(visible)
   .filter(e=>e.querySelectorAll('svg[role=button][aria-label="Close"]').length===1);
 if(panels.length!==1||buttons.length!==1||text(buttons[0])!=='Get offer'||close.length!==1||
   [...root.querySelectorAll('input,select,textarea')].some(visible))return null;
 const dialogs=[...document.querySelectorAll('[role=dialog],dialog,.lv-modal-wrapper')].filter(visible);
 if(dialogs.some(e=>!e.contains(root)&&!root.contains(e)))return null;
 return {root,close:close[0]};
};"""

_VIEW = r"""(() => {
 const visible=e=>{if(!e)return false;if(e.checkVisibility&&!e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))return false;
   const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>2&&r.height>2&&r.bottom>0&&r.right>0&&
   r.top<innerHeight&&r.left<innerWidth&&s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity)!==0;};
 const text=e=>(e?.innerText||e?.textContent||'').trim().replace(/\s+/g,' ');
 __QUESTIONNAIRE__
 __PROVIDER__
 __PROMOTION__
 const controls=root=>[...root.querySelectorAll('button,[role=button]')].filter(visible);
 const buttons=controls(document),headings=[...document.querySelectorAll('h1,h2,h3,[role=heading]')].filter(visible);
 const inputs=[...document.querySelectorAll('input')].filter(visible);
 const challenge=[...document.querySelectorAll('iframe[src*="captcha"],iframe[src*="arkoselabs"],[data-testid*="captcha"],[id*="captcha_container"]')].some(e=>{const r=e.getBoundingClientRect();
   return visible(e)&&r.width>60&&r.height>30;});
 const dialogs=[...document.querySelectorAll('[role=dialog],dialog,.lv-modal-wrapper')].filter(visible);
 const modalText=dialogs.map(text).join(' ');
 if(challenge||/verify (?:that )?you are human|complete (?:the )?security check|security verification/i.test(modalText))
   return {stage:'challenge',reason:'challenge_required'};
 if(inputs.some(e=>e.autocomplete==='one-time-code'||/verification.?code|otp|verify.?code/i.test(e.name+' '+e.placeholder))||
   /enter (?:the )?(?:verification|security) code|check your (?:email|inbox)|two.factor authentication/i.test(modalText))
   return {stage:'verification',reason:'verification_required'};
 if(promotion())return {stage:'promotion'};
 const heading=label=>headings.find(e=>text(e)===label);
 const scope=(title,selector)=>{let e=heading(title);for(let n=0;e&&n<8;n++,e=e.parentElement){
   if(e!==document.body&&e.querySelector(selector)&&controls(e).some(b=>text(b)==='Continue'))return e;}
   return null;};
 const password=inputs.filter(e=>e.type==='password'&&e.name==='password'&&e.autocomplete==='current-password');
 if(password.length){const root=scope('Welcome back','input[name="password"]');
   if(!root)return {stage:'unknown',reason:'login_required'};
   if(dialogs.some(e=>!e.contains(root)&&!root.contains(e)))return {stage:'unknown',reason:'setup_required'};
   const email=[...root.querySelectorAll('input[type=email][name=username]')].filter(visible);
   const next=controls(root).filter(e=>text(e)==='Continue');
   const errors=[...root.querySelectorAll('[role=alert],[class*=error],[class*=Error]')].filter(visible).map(text).join(' ');
   if(/incorrect|invalid (?:email|password)|wrong password|too many attempts|try again later/i.test(errors))
     return {stage:'failed',reason:'login_failed'};
   return {stage:email.length===1&&next.length===1?'password':'unknown',disabled:next[0]?.disabled===true};}
 const email=inputs.filter(e=>e.type==='email'&&e.name==='username'&&e.autocomplete==='username');
 if(email.length){const root=scope('Continue with email','input[name=username]');
   if(root&&dialogs.some(e=>!e.contains(root)&&!root.contains(e)))return {stage:'unknown',reason:'setup_required'};
   const next=root?controls(root).filter(e=>text(e)==='Continue'):[];
   return {stage:root&&email.length===1&&next.length===1?'email':'unknown',disabled:next[0]?.disabled===true};}
 if(provider())return {stage:'provider'};
 if(heading('Sign in to Dreamina')&&buttons.some(e=>text(e)==='Continue with email')&&
   dialogs.every(e=>e.contains(heading('Sign in to Dreamina'))))return {stage:'provider'};
 const survey=questionnaire();
 if(survey&&dialogs.every(e=>e.contains(survey)||survey.contains(e)))return {stage:'questionnaire'};
 const watermark=[...document.querySelectorAll('[class*="watermark-setting-modal-"]')].filter(visible)
   .find(e=>text(e).includes('Remove AI watermarks')&&text(e).includes('Save preference'));
 if(watermark&&dialogs.every(e=>e.contains(watermark)||watermark.contains(e)))return {stage:'watermark'};
 if(dialogs.length)return {stage:'unknown',reason:'setup_required'};
 if(buttons.some(e=>text(e)==='Sign in'||text(e)==='Sign in Free credits'||text(e)==='Sign in to start creating'))
   return {stage:'signed_out'};
 return {stage:'waiting'};
})()""".replace('__QUESTIONNAIRE__', _QUESTIONNAIRE).replace('__PROVIDER__', _PROVIDER).replace('__PROMOTION__', _PROMOTION)

# Revalidate an allowlisted control immediately before an ordinary DOM click.
_CLICK = r"""(() => {
 const wanted=__ACTION__;
 const visible=e=>{if(e.checkVisibility&&!e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))return false;
   const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>2&&r.height>2&&r.bottom>0&&r.right>0&&
   r.top<innerHeight&&r.left<innerWidth&&s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity)!==0;};
 const text=e=>(e?.innerText||e?.textContent||'').trim().replace(/\s+/g,' ');
 __QUESTIONNAIRE__
 __PROVIDER__
 __PROMOTION__
 const buttons=root=>[...root.querySelectorAll('button,[role=button]')].filter(visible);
 let options=[];
 if(wanted==='provider'){
   const normal=provider();
   options=normal?[normal.email]:buttons(document).filter(e=>text(e)==='Continue with email');
 }
 if(wanted==='promotion'){
   const known=promotion();if(!known)return false;options=[known.close];
 }
 if(wanted==='questionnaire'){
   const root=questionnaire(),dialogs=[...document.querySelectorAll('[role=dialog],dialog,.lv-modal-wrapper')].filter(visible);
   if(!root||dialogs.some(e=>!e.contains(root)&&!root.contains(e)))return false;
   options=[...root.querySelectorAll('[class*="close-btn-"][role=button][aria-label="Close"]')].filter(visible);
 }
 if(wanted==='email'||wanted==='password'){
   const title=wanted==='email'?'Continue with email':'Welcome back';
   let root=[...document.querySelectorAll('h1,h2,h3,[role=heading]')].filter(visible).find(e=>text(e)===title);
   for(let n=0;root&&n<8;n++,root=root.parentElement){
     if(root===document.body)break;
     const field=root.querySelector(wanted==='email'?'input[type=email][name=username]':'input[type=password][name=password]');
     const next=buttons(root).filter(e=>text(e)==='Continue');
     if(field&&next.length===1){options=next;break;}
   }
 }
 if(!options.length||(['email','password','provider'].includes(wanted)&&options.length!==1))return false;
 const target=options[0];if(target.disabled||target.getAttribute('aria-disabled')==='true')return false;
 target.click();return true;
})()""".replace('__QUESTIONNAIRE__', _QUESTIONNAIRE).replace('__PROVIDER__', _PROVIDER).replace('__PROMOTION__', _PROMOTION)

# The two sidebar chat/canvas login prompts have different handlers. Use the
# observed top login trigger, and click its Sign in label with browser mouse
# events rather than accepting any matching sidebar text or tooltip wrapper.
_OPEN_LOGIN_TARGET = r"""(() => {
 const visible=e=>{if(!e)return false;if(e.checkVisibility&&!e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))return false;
   const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>2&&r.height>2&&r.bottom>0&&r.right>0&&
   r.top<innerHeight&&r.left<innerWidth&&s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity)!==0;};
 const text=e=>(e?.innerText||e?.textContent||'').trim().replace(/\s+/g,' ');
 if([...document.querySelectorAll('[role=dialog],dialog,.lv-modal-wrapper')].some(visible))return null;
 const wrappers=[...document.querySelectorAll('[role=button][class*="login-button-wrapper-"]')].filter(visible);
 const targets=wrappers.flatMap(wrapper=>[...wrapper.querySelectorAll('[class*="login-button-"]')]
   .filter(e=>visible(e)&&text(e)==='Sign in').map(label=>({wrapper,label})));
 if(targets.length!==1)return null;
 const {wrapper,label}=targets[0],r=label.getBoundingClientRect();
 const x=(Math.max(0,r.left)+Math.min(innerWidth,r.right))/2,y=(Math.max(0,r.top)+Math.min(innerHeight,r.bottom))/2;
 const hit=document.elementFromPoint(x,y);
 if(!hit||!wrapper.contains(hit))return null;
 return {x,y};
})()"""

_WATERMARK = r"""(() => {
 const action=__ACTION__;
 const visible=e=>{if(!e)return false;if(e.checkVisibility&&!e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))return false;
   const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>2&&r.height>2&&r.bottom>0&&r.right>0&&
   r.top<innerHeight&&r.left<innerWidth&&s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity)!==0;};
 const text=e=>(e?.innerText||e?.textContent||'').trim().replace(/\s+/g,' ');
 const controls=root=>[...root.querySelectorAll('button,[role=button],[role=menuitem]')].filter(visible);
 const roots=[...document.querySelectorAll('[class*="watermark-setting-modal-"]')].filter(visible);
 const root=roots.find(e=>text(e).includes('Remove AI watermarks')&&text(e).includes('Save preference')&&
   text(e).includes('Applies to future generated content and exports.'));
 const dialogs=[...document.querySelectorAll('[role=dialog],dialog,.lv-modal-wrapper')].filter(visible);
 if(dialogs.some(e=>!root||(!e.contains(root)&&!root.contains(e))))return {stage:'blocked'};
 if(root){
   const row=root.querySelector('[class*="switch-row-"]');
   const switches=row?[...row.querySelectorAll('button,[role=switch],[role=checkbox],input[type=checkbox]')].filter(visible):[];
   const save=controls(root).filter(e=>text(e)==='Save preference');
   if(switches.length!==1||save.length!==1)return {stage:'unknown'};
   const toggle=switches[0],aria=toggle.getAttribute('aria-checked');
   let checked=aria==='true'?true:aria==='false'?false:null;
   if(checked===null&&toggle.tagName==='INPUT')checked=toggle.checked;
   if(checked===null&&/(?:^|\s|-)switch(?:\s|-)/.test(toggle.className||''))
     checked=[...toggle.classList].some(k=>/(?:^|-)checked(?:-|$)/.test(k));
   if(checked===null)return {stage:'unknown'};
   if(action==='toggle'&&checked===false&&!toggle.disabled){toggle.click();return {stage:'toggled'};}
   if(action==='save'&&checked===true&&!save[0].disabled&&save[0].getAttribute('aria-disabled')!=='true'){
     save[0].click();return {stage:'saved'};}
   return {stage:'modal',checked};
 }
 const entries=controls(document).filter(e=>text(e)==='Remove watermarks');
 if(entries.length===1){if(action==='open_setting'){entries[0].click();return {stage:'opened'};}return {stage:'menu'};}
 const avatars=[...document.querySelectorAll('[class*="top-actions-logged-in-"] [class*="avatar-action-shell-"] [class*="avatar-container-"]')]
   .filter(visible);
 if(avatars.length===1){if(action==='open_avatar'){avatars[0].click();return {stage:'opened'};}return {stage:'avatar'};}
 return {stage:'waiting'};
})()"""


class AccountAutomation:
    def __init__(self, account: dict[str, Any], settings: dict[str, Any] | None = None,
                 *, browser: CDPBrowser | None = None, allow_initial_identity: bool = False) -> None:
        account_proxy_url(account)
        self.account = account
        self.browser = browser or CDPBrowser(int(account.get('cdp_port') or 0), timeout=15)
        self.expected_email = str(account.get('login_email') or '').strip().casefold()
        self.allow_initial_identity = allow_initial_identity is True
        self._password_submitted = False
        self.verified_user_id = ''
        settings = settings or {}
        self.timeout = min(300.0, max(15.0, float(settings.get('auto_login_timeout_seconds') or 180)))
        key = (int(account.get('id') or 0), int(account.get('cdp_port') or 0))
        with _SETTINGS_GUARD:
            self._settings_lock = _SETTINGS_LOCKS.setdefault(key, threading.RLock())

    def _identity(self) -> dict[str, Any] | None:
        value = self.browser.evaluate(_IDENTITY, await_promise=True)
        if not isinstance(value, dict) or value.get('transport_error'):
            raise AutomationError('login_required', 'Dreamina identity read could not complete')
        if value.get('authenticated') is not True or not value.get('user_id'):
            return None
        user_id = value['user_id']
        if (not isinstance(user_id, str) or not user_id or user_id == '0' or len(user_id) > 256
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in user_id)):
            raise AutomationError('login_required', 'Dreamina signed-in identity does not match this account')
        email = str(value.get('email') or '').strip().casefold()
        masked = '*' in email
        mask = re.fullmatch(r'([^*@\s])\*{3}([^*\s]+)', email) if masked else None
        mask_matches = bool(mask and self.expected_email and mask[2].count('@') == 1
                            and not mask[2].startswith('@')
                            and self.expected_email.startswith(mask[1])
                            and self.expected_email.endswith(mask[2])
                            and len(self.expected_email) > len(mask[1]) + len(mask[2]))
        if self.expected_email and ((masked and not mask_matches) or (not masked and email != self.expected_email)):
            raise AutomationError('login_required', 'Dreamina signed-in identity does not match this account')
        bound_id = str(self.account.get('login_user_id') or self.verified_user_id or '')
        expected_id = str(self.account.get('user_id') or '')
        if ((bound_id and user_id != bound_id) or (expected_id and user_id != expected_id)
                or (masked and self.expected_email and not bound_id
                    and not (self._password_submitted or self.allow_initial_identity))):
            raise AutomationError('login_required', 'Dreamina signed-in identity does not match this account')
        self.verified_user_id = user_id
        return {'user_id': user_id, 'email': email}

    def _settings(self) -> dict[str, Any]:
        try:
            value = self.browser.post('/mweb/v1/get_settings', {})
        except BrowserError as error:
            raise AutomationError('setup_required', 'Dreamina account settings could not be verified') from error
        settings = value.get('user_custom_settings') if isinstance(value, dict) else None
        if not isinstance(settings, dict):
            raise AutomationError('setup_required', 'Dreamina account settings response is incomplete')
        return {field: settings.get(field) for field in
                ('close_watermark', 'close_watermark_popup_tip', 'aigc_compliance_confirmed')}

    @staticmethod
    def _ready(identity: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
        return {'identity_verified': True, 'watermark_ready': settings.get('close_watermark') is True,
                'agreement_ready': settings.get('aigc_compliance_confirmed') is True,
                'identity': identity, 'settings': settings}

    def read_ready(self) -> dict[str, Any]:
        """Read identity and settings only; never mutate a preference or click UI."""
        view = self.browser.evaluate(_VIEW)
        if isinstance(view, dict) and view.get('stage') == 'challenge':
            raise AutomationError('challenge_required', 'Dreamina login requires a human security challenge')
        if isinstance(view, dict) and view.get('stage') == 'verification':
            raise AutomationError('verification_required', 'Dreamina login requires email or security verification')
        identity = self._identity()
        if identity is None:
            raise AutomationError('login_required', 'Dreamina login session has not been verified')
        return self._ready(identity, self._settings())

    def ensure_ready(self) -> dict[str, Any]:
        """Confirm the two authorized preferences and verify the saved state."""
        with self._settings_lock:
            self._dismiss_promotion()
            evidence = self.read_ready()
            if not evidence['watermark_ready']:
                # The website has a region/freemium/membership gate. Its normal
                # Save handler must decide eligibility; a raw preference write
                # must never bypass that gate, even if the endpoint accepts it.
                self._enable_watermark()
                evidence = self.read_ready()
            if not evidence['agreement_ready']:
                try:
                    self.browser.post('/mweb/v1/update_settings', {
                        'custom_settings': {'aigc_compliance_confirmed': True}})
                except BrowserError as error:
                    raise AutomationError('setup_required', 'Dreamina account settings were not confirmed') from error
                evidence = self.read_ready()
            if not evidence['watermark_ready'] or not evidence['agreement_ready']:
                raise AutomationError('setup_required', 'Dreamina account settings were not confirmed')
            return evidence

    def _dismiss_promotion(self) -> bool:
        """Close only the recorded optional offer, never a subscription flow."""
        view = self.browser.evaluate(_VIEW)
        if not isinstance(view, dict) or view.get('stage') != 'promotion':
            return False
        if not self._action('promotion', '', ''):
            raise AutomationError('setup_required', 'Dreamina optional offer could not be closed')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = self.browser.evaluate(_VIEW)
            if isinstance(current, dict) and current.get('stage') != 'promotion':
                stage = current.get('stage')
                if stage == 'challenge':
                    raise AutomationError('challenge_required', 'Dreamina login requires a human security challenge')
                if stage == 'verification':
                    raise AutomationError('verification_required', 'Dreamina login requires email or security verification')
                if stage != 'unknown':
                    return True
                # The known modal's fading wrapper briefly outlives its
                # content. Wait without interacting with any unknown UI.
            time.sleep(.2)
        raise AutomationError('setup_required', 'Dreamina optional offer could not be closed')

    def _watermark_action(self, action: str = 'view') -> dict[str, Any]:
        if action not in {'view', 'open_avatar', 'open_setting', 'toggle', 'save'}:
            raise ValueError('unsupported watermark UI action')
        with _UI_LOCK:
            self.browser.command('Page.bringToFront')
            value = self.browser.evaluate(_WATERMARK.replace('__ACTION__', json.dumps(action)))
        return value if isinstance(value, dict) else {'stage': 'unknown'}

    def _enable_watermark(self) -> None:
        deadline = time.monotonic() + min(30, self.timeout)
        actions: set[str] = set()
        while time.monotonic() < deadline:
            current = self._watermark_action()
            stage = current.get('stage')
            if stage in {'blocked', 'unknown'}:
                if self._dismiss_promotion():
                    continue
                raise AutomationError('setup_required', 'Dreamina watermark setting requires manual review or membership')
            if stage == 'avatar' and 'open_avatar' not in actions:
                self._watermark_action('open_avatar')
                actions.add('open_avatar')
            elif stage == 'menu' and 'open_setting' not in actions:
                self._watermark_action('open_setting')
                actions.add('open_setting')
            elif stage == 'modal':
                if current.get('checked') is False and 'toggle' not in actions:
                    self._watermark_action('toggle')
                    actions.add('toggle')
                elif current.get('checked') is True and 'save' not in actions:
                    self._watermark_action('save')
                    actions.add('save')
            if 'save' in actions and self._settings().get('close_watermark') is True:
                return
            time.sleep(.3)
        raise AutomationError('setup_required', 'Dreamina watermark preference was not confirmed')

    def _view(self) -> dict[str, Any]:
        with _UI_LOCK:
            self.browser.command('Page.bringToFront')
            value = self.browser.evaluate(_VIEW)
        if not isinstance(value, dict):
            raise AutomationError('login_required', 'Dreamina login page is not ready')
        return value

    def _fill(self, selector: str, value: str) -> bool:
        # insertText delivers normal browser input events; neither return nor
        # persist the password, DOM field values, or arbitrary exception text.
        focus = r"""(() => {const list=[...document.querySelectorAll(__SELECTOR__)].filter(e=>{
          if(e.checkVisibility&&!e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))return false;
          const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>2&&r.height>2&&
            s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity)!==0;});
          if(list.length!==1)return false;list[0].focus();list[0].select();return true;})()"""
        if self.browser.evaluate(focus.replace('__SELECTOR__', json.dumps(selector))) is not True:
            return False
        self.browser.command('Input.insertText', {'text': value})
        check = '(() => {const e=document.querySelector(' + json.dumps(selector) + ');return !!e&&e.value===' + json.dumps(value) + ';})()'
        return self.browser.evaluate(check) is True

    def _action(self, stage: str, email: str, password: str) -> bool:
        if stage not in {'signed_out', 'provider', 'email', 'password', 'questionnaire', 'promotion'}:
            return False
        with _UI_LOCK:
            self.browser.command('Page.bringToFront')
            latest = self.browser.evaluate(_VIEW)
            if not isinstance(latest, dict) or latest.get('stage') != stage:
                return False
            if stage == 'signed_out':
                target = self.browser.evaluate(_OPEN_LOGIN_TARGET)
                if not isinstance(target, dict):
                    return False
                coordinates = {key: target.get(key) for key in ('x', 'y')}
                if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                           and math.isfinite(value) and value >= 0 for value in coordinates.values()):
                    return False
                self.browser.command('Input.dispatchMouseEvent', {'type': 'mouseMoved', **coordinates,
                                                                 'button': 'none', 'buttons': 0})
                try:
                    self.browser.command('Input.dispatchMouseEvent', {'type': 'mousePressed', **coordinates,
                                                                     'button': 'left', 'buttons': 1, 'clickCount': 1})
                finally:
                    self.browser.command('Input.dispatchMouseEvent', {'type': 'mouseReleased', **coordinates,
                                                                     'button': 'left', 'buttons': 0, 'clickCount': 1})
                return True
            if stage in {'email', 'password'}:
                if not self._fill('input[type="email"][name="username"]', email):
                    return False
            if stage == 'password':
                if not self._fill('input[type="password"][name="password"]', password):
                    return False
            return self.browser.evaluate(_CLICK.replace('__ACTION__', json.dumps(stage))) is True

    def run(self, email: str, password: str,
            on_stage: Callable[[str], Any] | None = None) -> dict[str, Any]:
        # A cold page can fill the default 250-resource buffer before the
        # signed-in SDK account/info request. Keep one CDP session attached
        # so current and replacement documents retain the genuine URL.
        with self.browser.resource_timing_capture():
            return self._run_login(email, password, on_stage)

    def _run_login(self, email: str, password: str,
                   on_stage: Callable[[str], Any] | None = None) -> dict[str, Any]:
        email = str(email).strip()
        if not email or not password:
            raise AutomationError('login_required', 'Dreamina email and password are required')
        self.expected_email = email.casefold()
        self._password_submitted = False
        callback = on_stage or (lambda _stage: None)
        deadline = time.monotonic() + self.timeout
        actions: set[str] = set()
        # Only opening the login UI and choosing its email provider are safe
        # navigation. A cold page may ignore the first event while its SDK
        # loads; retry those two stages at most three times, eight seconds
        # apart. Never retry either credential-bearing Continue action.
        navigation = {stage: {'count': 0, 'at': -float('inf')} for stage in ('signed_out', 'provider')}
        identity_checked_at = -float('inf')
        submitted_at: float | None = None
        optional_closed_at: dict[str, float] = {}
        unknown_since: float | None = None
        callback('login')
        while time.monotonic() < deadline:
            now = time.monotonic()
            try:
                view = self._view()
            except BrowserError:
                # A successful normal login may navigate or replace its tab.
                # Keep waiting for the same profile; never replay a password.
                time.sleep(.3)
                continue
            stage = str(view.get('stage') or 'waiting')
            if stage == 'challenge':
                raise AutomationError('challenge_required', 'Dreamina login requires a human security challenge')
            if stage == 'verification':
                raise AutomationError('verification_required', 'Dreamina login requires email or security verification')
            if stage == 'failed':
                raise AutomationError('login_failed', 'Dreamina did not accept the login')
            if stage == 'unknown':
                if unknown_since is None:
                    unknown_since = time.monotonic()
                if time.monotonic() - unknown_since < 30:
                    # Login and optional-dialog animations can leave an
                    # unrecognized wrapper briefly. Only wait: never click,
                    # fill, or verify identity through an unknown panel.
                    time.sleep(.2)
                    continue
                raise AutomationError(str(view.get('reason') or 'setup_required'), 'Dreamina requires manual page review')
            unknown_since = None
            if stage in {'questionnaire', 'promotion'}:
                if stage not in actions and self._action(stage, email, password):
                    actions.add(stage)
                    optional_closed_at[stage] = time.monotonic()
                if stage in optional_closed_at and time.monotonic() - optional_closed_at[stage] > 5:
                    raise AutomationError('setup_required', 'Dreamina optional dialog could not be closed')
                time.sleep(.3)
                continue
            if now - identity_checked_at >= 2:
                identity_checked_at = now
                try:
                    identity = self._identity()
                except AutomationError as error:
                    if 'does not match' in str(error):
                        raise
                    identity = None
                except BrowserError:
                    identity = None
                if identity is not None:
                    callback('settings')
                    evidence = self.ensure_ready()
                    callback('verifying')
                    return evidence
            if stage in navigation:
                if actions & {'email', 'password'}:
                    raise AutomationError('login_failed', 'Dreamina login returned to its start after credentials were submitted')
                attempt = navigation[stage]
                # A false result only checked readiness; no navigation was
                # sent. Once three real/uncertain actions are spent, wait
                # for their delayed result until the overall deadline.
                if attempt['count'] < 3 and now - attempt['at'] >= 8:
                    callback('login')
                    attempt['at'] = now
                    try:
                        if self._action(stage, email, password):
                            attempt['count'] += 1
                    except BrowserError:
                        # An uncertain navigation cannot create a video or
                        # send credentials; the next fresh stage decides.
                        attempt['count'] += 1
            elif stage in {'email', 'password'} and stage not in actions:
                callback(stage if stage in {'email', 'password'} else 'login')
                if self._action(stage, email, password):
                    actions.add(stage)
                    if stage == 'password':
                        self._password_submitted = True
                        submitted_at = time.monotonic()
            if submitted_at is not None and time.monotonic() - submitted_at > 45:
                raise AutomationError('login_failed', 'Dreamina login result was not confirmed')
            time.sleep(.3)
        raise AutomationError('login_required', 'Dreamina login did not finish before the deadline')
