/* Run with Node and the optional Playwright development dependency. No live data. */
const {chromium} = require('playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless:true, channel:process.env.PLAYWRIGHT_CHANNEL || undefined});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const assets = 'src/youtube_playlist_builder/web_assets/';
    const html = fs.readFileSync(assets + 'organiser.html', 'utf8').replace(/<script[^>]*><\/script>/g, '').replace(/<link[^>]*>/g, '');
    await page.setContent(html);
    await page.addStyleTag({content:fs.readFileSync(assets + 'app.css', 'utf8') + fs.readFileSync(assets + 'organiser.css', 'utf8')});
    await page.evaluate(() => {
      window.saves = [];
      window.channel = 'first';
      window.busy = false;
      window.SpeechRecognition = class {
        constructor() { window.rec = this; }
        start() { this.onstart(); }
        stop() { this.stopped = true; }
        abort() { this.aborted = true; }
      };
    });
    await page.addScriptTag({content:fs.readFileSync(assets + 'organiser-voice.js', 'utf8')});
    await page.evaluate(() => {
      window.topics = ['Photography', 'Mechanical engineering', 'Engineering one', 'Engineering two', 'Chess'].map(name => ({name}));
      window.voice = new TopicVoice({getChannel:() => window.channel, getTitle:() => 'Demo channel', getTopics:() => window.topics, isBusy:() => window.busy, assign:topic => window.saves.push(topic.name)});
    });
    async function press(code, type = 'keydown') {
      await page.evaluate(({code, type}) => document.dispatchEvent(new KeyboardEvent(type, {code, key:code === 'MetaRight' ? 'Meta' : 'Control', bubbles:true})), {code, type});
    }
    async function transcript(text, end = true) {
      await page.evaluate(({text, end}) => { window.rec.onresult({results:[[{transcript:text}]]}); if (end) window.rec.onend(); }, {text, end});
    }
    // No dialog while held, then one click saves and dismisses.
    await press('ControlRight'); await transcript('photo');
    assert.equal(await page.locator('dialog').evaluate(el => el.open), false);
    await press('ControlRight', 'keyup');
    assert.equal(await page.locator('dialog').evaluate(el => el.open), true);
    await page.getByRole('button', {name:'Photography', exact:true}).click();
    assert.deepEqual(await page.evaluate(() => window.saves), ['Photography']);
    assert.equal(await page.locator('dialog').evaluate(el => el.open), false);
    // Right Command, partial/fuzzy matches, immediate choices even before end.
    await press('MetaRight'); await transcript('engineering', false); await press('MetaRight', 'keyup');
    assert.equal(await page.locator('#voice-results button').count(), 3);
    assert.equal(await page.evaluate(() => window.rec.stopped), true);
    await page.locator('#voice-query').fill('fotography');
    assert.equal(await page.evaluate(() => window.rec.aborted), true);
    assert.equal(await page.locator('#voice-results button').textContent(), 'Photography');
    await page.evaluate(() => window.rec.onresult({results:[[{transcript:'Chess'}]]}));
    assert.equal(await page.locator('#voice-query').inputValue(), 'fotography');
    await page.locator('#voice-query').fill('zzzzzzzz');
    assert.match(await page.locator('#voice-results').textContent(), /No matching/);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('dialog').evaluate(el => el.open), false);
    // Stale results after channel switch cannot mutate or assign.
    await press('ControlRight');
    await page.evaluate(() => { window.channel = 'second'; window.voice.cancel(); window.rec.onresult({results:[[{transcript:'Chess'}]]}); });
    await press('ControlRight', 'keyup');
    assert.equal(await page.locator('dialog').evaluate(el => el.open), false);
    // Text-only entry remains useful with no speech API, and errors keep typing available.
    await page.evaluate(() => { window.voice.Recognition = null; });
    await press('MetaRight'); await press('MetaRight', 'keyup');
    await page.locator('#voice-query').fill('chess');
    await page.getByRole('button', {name:'Chess', exact:true}).click();
    assert.deepEqual(await page.evaluate(() => window.saves), ['Photography', 'Chess']);
    await page.getByRole('button', {name:'Find a topic', exact:true}).click();
    await page.locator('#voice-query').fill('engineering');
    fs.mkdirSync('agent_docs', {recursive:true});
    await page.screenshot({path:'agent_docs/voice-topic-dialog.png'});
    await page.getByRole('button', {name:'Cancel', exact:true}).click();
    await page.evaluate(() => { window.voice.Recognition = window.SpeechRecognition; });
    await press('ControlRight');
    await page.evaluate(() => window.rec.onerror({error:'not-allowed'}));
    await press('ControlRight', 'keyup');
    assert.match(await page.locator('#voice-status').textContent(), /permission was denied/);
    assert.equal(await page.locator('dialog').evaluate(el => el.open), true);
    await page.keyboard.press('Escape');
    await page.evaluate(() => { window.busy = true; });
    await press('ControlRight'); await press('ControlRight', 'keyup');
    assert.equal(await page.locator('dialog').evaluate(el => el.open), false);
    // Exercise the real organiser script and its assignment request using isolated fixtures.
    const cid = 'UC' + 'a'.repeat(22);
    const snapshot = {settings:{single_topic_channels:true}, channels:[{channel_id:cid, title:'Demo channel', description:'Test channel'}], topics:[{name:'Photography', channels:[], enabled:false}]};
    let assignment = null;
    await page.route('http://127.0.0.1:18765/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/state') return route.fulfill({json:snapshot});
      if (url.pathname === '/api/channel') return route.fulfill({json:{videos:[], feed_fetched_at:new Date().toISOString()}});
      if (url.pathname === '/api/assignment') {
        assignment = route.request().postDataJSON();
        snapshot.topics[0].channels = [cid];
        return route.fulfill({json:{ok:true}});
      }
      const file = url.pathname === '/organiser' ? 'organiser.html' : url.pathname.slice(1);
      return route.fulfill({body:fs.readFileSync(assets + file, 'utf8'), contentType:file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html'});
    });
    await page.goto('http://127.0.0.1:18765/organiser');
    await page.locator('#channels button').click();
    await page.locator('#hide-assigned').check();
    await page.getByRole('button', {name:'Find a topic', exact:true}).click();
    await page.locator('#voice-query').fill('photo');
    await page.getByRole('button', {name:'Photography', exact:true}).click();
    await page.getByText('Assignment saved.', {exact:true}).waitFor();
    assert.deepEqual(assignment, {channel_id:cid, topic:'Photography', assigned:true});
    assert.equal(await page.locator('dialog').evaluate(el => el.open), false);
    assert.equal(await page.locator('#channels button').count(), 0);
    assert.deepEqual(errors, []);
    console.log('Voice UI: matching, key timing, right Command, typing, one-click save, late results, cancellation, permission errors, unsupported browser and busy guards passed.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
