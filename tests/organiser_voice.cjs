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
      window.topics = ['Photography', 'Mechanical engineering', 'Engineering one', 'Engineering two', 'Chess'].map((name, index) => ({name, id:index + 1}));
      window.voice = new TopicVoice({getChannel:() => window.channel, getTitle:() => 'Demo channel', getChannelInfo:() => ({title:'Demo channel', description:'Photo tips and camera settings'}), getTopics:() => window.topics, getVoiceMatches:() => window.learned || [], isBusy:() => window.busy, assign:(topic, phrase) => { window.saves.push(topic.name); window.lastPhrase = phrase; }});
    });
    async function press(code, type = 'keydown') {
      await page.evaluate(({code, type}) => document.dispatchEvent(new KeyboardEvent(type, {code, key:code === 'MetaRight' ? 'Meta' : 'Control', bubbles:true})), {code, type});
    }
    async function transcript(text, end = true) {
      await page.evaluate(({text, end}) => { window.rec.onresult({results:[[{transcript:text}]]}); if (end) window.rec.onend(); }, {text, end});
    }
    // Suggestions are local, ignore generic filler and weight title above description.
    assert.deepEqual(await page.evaluate(() => channelTopics(window.topics, {title:'Chess lessons', description:'Photography and chess videos'}).map(t => t.name)), ['Chess', 'Photography']);
    assert.deepEqual(await page.evaluate(() => channelTopics(window.topics, {title:'My channel', description:'Welcome to my channel about new videos. Subscribe for more content.'}).map(t => t.name)), []);
    assert.deepEqual(await page.evaluate(() => channelTopics([{name:'Cameras', description:'Photography and lenses'}], {title:'Photo lessons'}).map(t => t.name)), ['Cameras']);
    assert.deepEqual(await page.evaluate(() => channelTopics([{name:'Échecs'}], {title:'Échecs lessons'}).map(t => t.name)), ['Échecs']);
    // Open on keydown with clickable channel suggestions, even before speech.
    await press('ControlRight');
    assert.equal(await page.locator('dialog').evaluate(el => el.open), true);
    assert.equal(await page.locator('#voice-results button').textContent(), 'Photography');
    await page.getByRole('button', {name:'Photography', exact:true}).click();
    assert.equal(await page.evaluate(() => window.rec.aborted), true);
    await press('ControlRight', 'keyup');
    assert.equal(await page.locator('dialog').evaluate(el => el.open), false);
    assert.deepEqual(await page.evaluate(() => window.saves), ['Photography']);
    await page.evaluate(() => { window.saves = []; });
    // A quick silent tap retains the shortlist when recognition ends with no words.
    await press('ControlRight'); await press('ControlRight', 'keyup');
    await page.evaluate(() => window.rec.onend());
    assert.equal(await page.locator('dialog').evaluate(el => el.open), true);
    assert.equal(await page.locator('#voice-results button').textContent(), 'Photography');
    await page.getByRole('button', {name:'Cancel', exact:true}).click();
    // Voice still refines the immediately visible suggestions.
    await press('ControlRight'); await transcript('photo');
    assert.equal(await page.locator('dialog').evaluate(el => el.open), true);
    await press('ControlRight', 'keyup');
    assert.equal(await page.locator('dialog').evaluate(el => el.open), true);
    await page.getByRole('button', {name:'Photography', exact:true}).click();
    assert.deepEqual(await page.evaluate(() => window.saves), ['Photography']);
    assert.equal(await page.locator('dialog').evaluate(el => el.open), false);
    // Right Command, partial/fuzzy matches, immediate choices even before end.
    await press('MetaRight'); await transcript('engineering', false); await press('MetaRight', 'keyup');
    assert.equal(await page.locator('#voice-results button').count(), 4);
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
    // Escape or a backdrop click while held aborts; release cannot reopen it.
    await press('ControlRight');
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(() => window.rec.aborted), true);
    await press('ControlRight', 'keyup');
    assert.equal(await page.locator('dialog').evaluate(el => el.open), false);
    await press('MetaRight');
    const bounds = await page.locator('dialog').boundingBox();
    await page.mouse.click(bounds.x + 4, bounds.y + 4); // Dialog padding is not backdrop.
    assert.equal(await page.locator('dialog').evaluate(el => el.open), true);
    await page.mouse.click(5, 5);
    assert.equal(await page.locator('dialog').evaluate(el => el.open), false);
    assert.equal(await page.evaluate(() => window.rec.aborted), true);
    await page.evaluate(() => window.rec.onresult({results:[[{transcript:'Chess'}]]}));
    await press('MetaRight', 'keyup');
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
    // Empty/whitespace/punctuation recognitions must not be attached to a choice.
    await page.evaluate(() => { window.busy = false; });
    for (const empty of ['', '   ', '...!?']) {
      await press('ControlRight'); await transcript(empty); await press('ControlRight', 'keyup');
      await page.getByRole('button', {name:'Photography', exact:true}).click();
      assert.equal(await page.evaluate(() => window.lastPhrase), '');
    }
    // Teach a misheard phrase after typing a correction; preserve original speech.
    await page.evaluate(() => {
      window.busy = false;
      window.topics.push({id:6, name:'Synth'});
    });
    await press('ControlRight'); await transcript('since'); await press('ControlRight', 'keyup');
    await page.locator('#voice-query').fill('synth');
    await page.getByRole('button', {name:'Synth', exact:true}).click();
    assert.equal(await page.evaluate(() => window.lastPhrase), 'since');
    assert.deepEqual(await page.evaluate(() => learnedTopics(window.topics, 'Since!', [{phrase:'since',topic_id:6,uses:2},{phrase:'since',topic_id:1,uses:1}]).map(t => t.name)), ['Synth', 'Photography']);
    await page.evaluate(() => { window.learned = [{phrase:'since',topic_id:6,uses:1},{phrase:'since',topic_id:999,uses:9}]; });
    await press('MetaRight'); await transcript('since'); await press('MetaRight', 'keyup');
    assert.equal(await page.locator('#voice-results button').first().textContent(), 'Synth');
    await page.keyboard.press('Escape');
    await page.getByRole('button', {name:'Find a topic', exact:true}).click();
    await page.locator('#voice-query').fill('chess');
    await page.getByRole('button', {name:'Chess', exact:true}).click();
    assert.equal(await page.evaluate(() => window.lastPhrase), '');
    // Exercise the real organiser script and its assignment request using isolated fixtures.
    const cid = 'UC' + 'a'.repeat(22);
    const snapshot = {settings:{single_topic_channels:true}, channels:[{channel_id:cid, title:'Demo channel', description:'Photo lessons and camera settings'}], voice_matches:[], topics:[{id:1, name:'Photography', channels:[], enabled:false}, {id:2, name:'Cameras', channels:[], enabled:false}, {id:3, name:'Synth', channels:[], enabled:false}]};
    let assignment = null;
    await page.route('http://127.0.0.1:18765/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/state') return route.fulfill({json:snapshot});
      if (url.pathname === '/api/channel') return route.fulfill({json:{videos:[], feed_fetched_at:new Date().toISOString()}});
      if (url.pathname === '/api/assignment') {
        assignment = route.request().postDataJSON();
        snapshot.topics.find(t => t.name === assignment.topic).channels = [cid];
        if (assignment.voice_phrase) snapshot.voice_matches.push({phrase:assignment.voice_phrase, topic_id:3, uses:1});
        return route.fulfill({json:{ok:true}});
      }
      const file = url.pathname === '/organiser' ? 'organiser.html' : url.pathname.slice(1);
      return route.fulfill({body:fs.readFileSync(assets + file, 'utf8'), contentType:file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html'});
    });
    await page.addInitScript(() => {
      window.SpeechRecognition = class {
        constructor() { window.rec = this; }
        start() { this.onstart(); }
        stop() { this.stopped = true; }
        abort() { this.aborted = true; }
      };
    });
    await page.goto('http://127.0.0.1:18765/organiser');
    await page.locator('#channels button').click();
    await page.locator('#hide-assigned').check();
    await page.getByRole('button', {name:'Find a topic', exact:true}).click();
    assert.equal(await page.locator('#voice-query').inputValue(), '');
    await page.screenshot({path:'agent_docs/topic-channel-suggestions.png'});
    await page.getByRole('button', {name:'Photography', exact:true}).click();
    await page.getByText('Assignment saved.', {exact:true}).waitFor();
    assert.deepEqual(assignment, {channel_id:cid, topic:'Photography', assigned:true});
    assert.equal(await page.locator('dialog').evaluate(el => el.open), false);
    assert.equal(await page.locator('#channels button').count(), 0);
    // The actual organiser carries voice_phrase through the normal assignment route.
    await page.locator('#hide-assigned').uncheck();
    await page.locator('#channels button').click();
    await press('ControlRight'); await transcript('since'); await press('ControlRight', 'keyup');
    await page.locator('#voice-query').fill('synth');
    await page.getByRole('button', {name:'Synth', exact:true}).click();
    await page.getByText('Assignment saved.', {exact:true}).waitFor();
    assert.deepEqual(assignment, {channel_id:cid, topic:'Synth', assigned:true, voice_phrase:'since'});
    await page.reload();
    await page.locator('#channels button').click();
    await press('MetaRight'); await transcript('since'); await press('MetaRight', 'keyup');
    assert.equal(await page.locator('#voice-results button').first().textContent(), 'Synth');
    await page.keyboard.press('Escape');
    assert.deepEqual(errors, []);
    console.log('Topic finder: immediate opening, channel suggestions, voice/text filtering, one-click saving, Escape/backdrop dismissal, stale callbacks, permission errors, busy guards and learned speech matches across reloads passed.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
