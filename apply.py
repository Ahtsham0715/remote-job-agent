"""Conservative Lever adapter: exact answers only; unsupported forms are queued."""
import json
import re
from urllib.parse import urlparse

ALLOWED = {'jobs.lever.co', 'jobs.eu.lever.co'}

def allowed_url(url):
    parsed = urlparse(url)
    return parsed.scheme == 'https' and parsed.hostname in ALLOWED and not parsed.username and not parsed.password

def answer_for(name, label, profile):
    common = {'name': profile['name'], 'email': profile['email'], 'phone': profile['phone'],
              'urls[LinkedIn]': profile.get('linkedin', ''), 'urls[GitHub]': profile.get('github', ''),
              'urls[Portfolio]': profile.get('website', '')}
    # Exact field-name overrides make sponsorship and sensitive answers explicit.
    return profile.get('answers', {}).get(name, profile.get('answers', {}).get(label, common.get(name)))

def apply_lever(job, profile, resume, folder):
    if not allowed_url(job['url']):
        return 'blocked', 'Unsupported application host; use the job link manually'
    clicked = False
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            # Untrusted job pages may not navigate the browser to arbitrary sites.
            def route(request_route):
                request = request_route.request
                if request.is_navigation_request() and not allowed_url(request.url):
                    request_route.abort()
                else:
                    request_route.continue_()
            page.route('**/*', route)
            page.goto(job['url'], wait_until='domcontentloaded', timeout=45000)
            if not allowed_url(page.url):
                return 'blocked', 'Unexpected redirect'
            if not page.locator('input[type=file]').count():
                link = page.get_by_role('link', name=re.compile(r'apply for this job', re.I))
                if link.count() == 1: link.click()
            upload = page.locator('input[type=file]')
            if upload.count() != 1:
                return 'blocked', 'Unrecognized resume upload form'
            upload.set_input_files(str(resume))
            fields = page.locator('input,select,textarea')
            unknown = []
            for i in range(fields.count()):
                element = fields.nth(i)
                meta = element.evaluate('''e => ({name:e.name, type:e.type, tag:e.tagName,
                    required:e.required || e.getAttribute('aria-required') === 'true',
                    label:(e.labels && e.labels[0] ? e.labels[0].innerText : e.getAttribute('aria-label')) || ''})''')
                if meta['type'] in ('hidden', 'file', 'submit', 'button') or not element.is_visible():
                    continue
                name, label = meta['name'], meta['label'].strip()
                answer = answer_for(name, label, profile)
                if answer is None or answer == '':
                    if meta['required']: unknown.append(label or name or 'unlabeled field')
                    continue
                if meta['type'] == 'checkbox':
                    if not isinstance(answer, bool): unknown.append(label or name); continue
                    element.set_checked(answer)
                elif meta['type'] == 'radio':
                    if str(element.get_attribute('value')) == str(answer): element.check()
                elif meta['tag'] == 'SELECT':
                    element.select_option(label=str(answer))
                else:
                    element.fill(str(answer))
            invalid = page.locator('form').evaluate_all('''forms => forms.flatMap(f => Array.from(f.elements).filter(e => e.willValidate && !e.checkValidity()).map(e => e.name || e.id || 'unknown'))''')
            if unknown or invalid:
                (folder / 'missing-answers.json').write_text(json.dumps(sorted(set(unknown + invalid)), indent=2))
                page.screenshot(path=str(folder / 'blocked.png'), full_page=True)
                return 'blocked', 'Missing answers: ' + ', '.join(sorted(set(unknown + invalid)))
            if page.locator('iframe[src*="recaptcha"],iframe[src*="hcaptcha"],input[name="captcha"]').count():
                return 'blocked', 'CAPTCHA requires manual completion'
            button = page.get_by_role('button', name=re.compile(r'^submit application$', re.I))
            if button.count() != 1: return 'blocked', 'Unrecognized submit control'
            page.screenshot(path=str(folder / 'before-submit.png'), full_page=True)
            clicked = True
            button.click(timeout=15000)
            confirmation = page.get_by_text(re.compile(r'(application (has been |was )?(submitted|received)|thank you for applying)', re.I))
            try:
                confirmation.first.wait_for(state='visible', timeout=20000)
            except Exception:
                page.screenshot(path=str(folder / 'uncertain.png'), full_page=True)
                return 'uncertain', 'Submit clicked; confirmation absent. Verify manually before retry.'
            (folder / 'receipt.json').write_text(json.dumps({'url': page.url, 'confirmation': confirmation.first.inner_text()}, indent=2))
            page.screenshot(path=str(folder / 'receipt.png'), full_page=True)
            browser.close()
            return 'submitted', 'Visible application confirmation saved'
    except Exception as e:
        return ('uncertain' if clicked else 'blocked'), type(e).__name__ + ': ' + str(e)[:300]
