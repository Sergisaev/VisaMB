// Run against the local HTML or a downloaded deployed copy via MOBILE_TEST_HTML.
// Requires Playwright and Chromium; MOBILE_TEST_ENGINE=webkit selects WebKit.
const { chromium, webkit } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

const html = fs.readFileSync(process.env.MOBILE_TEST_HTML || path.join(__dirname, '..', 'index.html'));
const engine = process.env.MOBILE_TEST_ENGINE || 'chromium';
const countries = ['schengen', 'cyprus', 'japan', 'usa', 'canada'];
const sizes = [[320,568],[360,640],[375,667],[390,844],[430,932],[600,900],
  [767,900],[768,1024],[820,1180],[844,390],[1024,768],[1440,900]];

async function noOverflow(page, label) {
  const measurements = await page.evaluate(() => ({width:innerWidth, content:document.documentElement.scrollWidth}));
  assert.ok(measurements.content <= measurements.width, `${label}: horizontal overflow ${JSON.stringify(measurements)}`);
}

async function closeDialog(page, selector) {
  await page.locator(`${selector} .dialog-close`).click();
  await page.waitForFunction(() => !document.documentElement.classList.contains('modal-open'));
}

(async () => {
  const server = http.createServer((request, response) => {
    response.writeHead(200, {'Content-Type':'text/html; charset=utf-8'});
    response.end(html);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    const options = {headless:true};
    if (process.env.MOBILE_TEST_BROWSER) {options.executablePath=process.env.MOBILE_TEST_BROWSER;}
    else if (engine === 'chromium' && fs.existsSync('/usr/bin/chromium')) {
      options.executablePath = '/usr/bin/chromium';
      options.args = ['--no-sandbox'];
    }
    browser = await (engine === 'webkit' ? webkit : chromium).launch(options);
    const context = await browser.newContext({isMobile:true,hasTouch:true,reducedMotion:'reduce',viewport:{width:390,height:844}});
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/`, {waitUntil:'networkidle'});
    await page.evaluate(() => document.fonts.ready);
    const telegramLinks=await page.locator('[data-telegram]').evaluateAll(links=>links.map(link=>({href:link.href,target:link.target,rel:link.rel})));
    assert.equal(telegramLinks.length,5);
    for(const link of telegramLinks){
      assert.equal(link.href,'https://t.me/VisaMB_consult_bot');
      assert.equal(link.target,'_blank');
      assert.ok(link.rel.includes('noopener'));
    }
    await context.route('https://t.me/**',route=>route.fulfill({status:200,contentType:'text/html',body:'Telegram navigation test'}));
    const popupEvent=page.waitForEvent('popup');
    await page.locator('.hero [data-telegram]').click();
    const popup=await popupEvent;
    await popup.waitForLoadState();
    assert.equal(popup.url(),'https://t.me/VisaMB_consult_bot');
    assert.equal(await page.locator('#telegram-dialog').evaluate(dialog=>dialog.open),false);
    await popup.close();

    for (const [width,height] of sizes) {
      await page.setViewportSize({width,height});
      for (const country of countries) {
        await page.locator(`#tab-${country}`).click();
        await noOverflow(page, `${width}×${height}, ${country}`);
      }
      if (width <= 767) {
        const smallTargets = await page.evaluate(() => [...document.querySelectorAll('a,button,summary')]
          .filter(element => {
            const box=element.getBoundingClientRect();
            return box.width && box.height && (box.width<43.9 || box.height<43.9);
          }).map(element=>element.getAttribute('aria-label') || element.textContent.trim()));
        assert.deepEqual(smallTargets, [], `${width}px: undersized touch controls`);
        const headingFits = await page.locator('.hero-opening').evaluate(element => {
          const range=document.createRange();range.selectNodeContents(element);
          return range.getClientRects().length===1 && element.getBoundingClientRect().right <= innerWidth;
        });
        assert.ok(headingFits, `${width}px: hero heading breaks awkwardly`);
      }
      if (width <= 767) {
        const circles = await page.locator('.messenger-options .messenger-circle').evaluateAll(elements => elements.map(element=>{
          const rect=element.getBoundingClientRect();return rect.top+rect.height/2;
        }));
        assert.ok(Math.max(...circles)-Math.min(...circles)<1, `${width}px: messenger icons are misaligned`);
        const labels = await page.locator('.messenger-options strong').evaluateAll(elements=>elements.map(element=>{
          const rect=element.getBoundingClientRect();return rect.top+rect.height/2;
        }));
        assert.ok(Math.max(...labels)-Math.min(...labels)<1, `${width}px: messenger labels are misaligned`);
      }
    }
    console.log(`${engine}: 60 country/viewport combinations, touch targets, heading and messenger alignment passed`);

    await page.setViewportSize({width:390,height:844});
    await page.locator('.menu-toggle').click();
    assert.equal(await page.locator('.menu-toggle use').getAttribute('href'),'#i-close');
    await page.locator('#navigation a[href="#about"]').click();
    assert.equal(await page.locator('.menu-toggle').getAttribute('aria-expanded'),'false');
    assert.equal(await page.locator('.menu-toggle use').getAttribute('href'),'#i-menu');
    assert.equal(await page.evaluate(()=>document.activeElement.id),'about');
    const anchorPosition=await page.evaluate(()=>({section:document.getElementById('about').getBoundingClientRect().top,header:document.querySelector('.header').getBoundingClientRect().bottom}));
    assert.ok(anchorPosition.section>=anchorPosition.header, 'Sticky header obscures the destination section');
    await page.locator('.menu-toggle').click();
    await page.locator('.header .brand').click();
    assert.equal(await page.locator('.menu-toggle').getAttribute('aria-expanded'),'false');
    await page.locator('.menu-toggle').click();
    await page.setViewportSize({width:768,height:1024});
    await page.waitForFunction(()=>document.querySelector('.menu-toggle').getAttribute('aria-expanded')==='false');
    console.log(`${engine}: mobile menu, anchor focus and orientation change passed`);

    // Opening a dialog from the page footer must not jump back to the top after closing.
    await page.setViewportSize({width:390,height:844});
    const contactLink=page.locator('.telegram-card [data-messenger="whatsapp"]');
    await contactLink.scrollIntoViewIfNeeded();
    const scrollBefore=await page.evaluate(()=>scrollY);
    assert.ok(scrollBefore>1000);
    await contactLink.click();
    assert.ok(await page.evaluate(()=>document.documentElement.classList.contains('modal-open')));
    assert.equal(await page.evaluate(()=>getComputedStyle(document.body).position),'fixed');
    await closeDialog(page,'#messenger-dialog');
    assert.ok(Math.abs(await page.evaluate(()=>scrollY)-scrollBefore)<2,'Dialog close lost the reading position');

    await page.locator('.alternative-contact>summary').click();
    await page.locator('#name').fill('MOBILE_TEST_VISITOR');
    await page.locator('#contact-value').fill('mobile-test@example.invalid');
    await page.locator('#destination').selectOption('usa');
    await page.locator('#message').fill('Вопрос о поездке. '.repeat(50));
    await page.locator('#request-form button[type=submit]').click();
    assert.equal(await page.locator('#request-dialog').evaluate(element=>element.open),true);
    const preview=await page.locator('#request-preview').inputValue();
    assert.ok(preview.includes('США — платная консультация'));
    assert.ok(await page.locator('#request-preview').evaluate(element=>parseFloat(getComputedStyle(element).fontSize)>=16));
    await noOverflow(page,'Open request dialog');
    await closeDialog(page,'#request-dialog');
    console.log(`${engine}: scroll restoration, optional mobile form and readable request preview passed`);

    // A phone in landscape or with a short visual viewport must retain a reachable close button.
    for (const [width,height] of [[320,480],[568,320],[844,390]]) {
      await page.setViewportSize({width,height});
      await page.locator('#privacy-button').click();
      await page.locator('#privacy-dialog').evaluate(element=>{element.scrollTop=element.scrollHeight;});
      const control=await page.locator('#privacy-dialog .dialog-close').evaluate(element=>{
        const box=element.getBoundingClientRect(),dialog=element.closest('dialog').getBoundingClientRect();
        return {width:box.width,height:box.height,top:box.top,bottom:box.bottom,dialogTop:dialog.top,dialogBottom:dialog.bottom};
      });
      assert.ok(control.width>=44 && control.height>=44);
      assert.ok(control.top>=control.dialogTop && control.bottom<=control.dialogBottom, `Close button scrolled out of view: ${JSON.stringify(control)}`);
      await noOverflow(page,'Scrolled short-screen dialog');
      await closeDialog(page,'#privacy-dialog');
    }
    assert.deepEqual(errors,[],'Browser JavaScript errors');
    console.log(`${engine}: short-screen dialog scrolling passed; no JavaScript errors`);
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve=>server.close(resolve));
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
