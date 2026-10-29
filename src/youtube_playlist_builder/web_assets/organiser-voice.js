/* Browser speech is optional. Topic matching and assignments stay local. */
'use strict';
/** Fold punctuation and accents so spoken phrases match ordinary topic names. */
function topicWords(text) {
  return text.normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
}
/** Measure small transcription errors without an extra library or model call. */
function topicDistance(a, b) {
  let row = Array.from({length:b.length + 1}, (_, i) => i);
  for (let i = 0; i < a.length; i++) {
    const next = [i + 1];
    for (let j = 0; j < b.length; j++) next.push(Math.min(next[j] + 1, row[j + 1] + 1, row[j] + (a[i] !== b[j])));
    row = next;
  }
  return row[b.length];
}
/** Rank exact/partial names first, then plausible word-level transcription errors. */
function matchingTopics(topics, phrase) {
  const query = topicWords(phrase).slice(0, 120);
  if (!query) return [];
  return topics.map(topic => {
    const name = topicWords(topic.name);
    let score = name === query ? 0 : name.includes(query) ? 1 : Infinity;
    if (!Number.isFinite(score)) {
      const words = name.split(' ');
      const costs = query.split(' ').map(q => Math.min(...[name, ...words].map(w => {
        if (w.includes(q)) return 0;
        const distance = topicDistance(q, w);
        return q.length >= 4 && distance <= Math.max(1, Math.floor(q.length * .3)) ? distance / Math.max(q.length, w.length) : Infinity;
      })));
      if (costs.every(Number.isFinite)) score = 2 + costs.reduce((a, b) => a + b, 0);
    }
    return {topic, score};
  }).filter(item => Number.isFinite(item.score)).sort((a, b) => a.score - b.score || a.topic.name.localeCompare(b.topic.name)).map(item => item.topic);
}
/** Own one short push-to-talk session, tied to the channel visible at its start. */
class TopicVoice {
  constructor(options) {
    this.options = options;
    this.dialog = document.getElementById('voice-dialog');
    this.query = document.getElementById('voice-query');
    this.status = document.getElementById('voice-status');
    this.results = document.getElementById('voice-results');
    this.Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    this.held = null;
    this.recognition = null;
    this.channel = null;
    this.timer = null;
    this.query.maxLength = 120;
    this.query.oninput = () => { this.abort(); this.draw(); };
    document.getElementById('voice-open').onclick = () => this.open(true);
    document.getElementById('voice-close').onclick = () => this.cancel();
    this.dialog.addEventListener('cancel', event => { event.preventDefault(); this.cancel(); });
    this.dialog.addEventListener('close', () => this.abort());
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape') { this.cancel(); return; }
      if (!['ControlRight', 'MetaRight'].includes(event.code) || event.repeat || this.held || event.altKey || event.shiftKey) return;
      // Leave ordinary text editing and Control shortcuts alone.
      if (event.target !== this.query && event.target?.matches?.('input, textarea, select, [contenteditable="true"]')) return;
      this.held = event.code;
      if (this.open()) { event.preventDefault(); this.listen(); }
    });
    document.addEventListener('keyup', event => {
      if (event.code !== this.held) return;
      this.held = null;
      this.stop();
    });
    window.addEventListener('blur', () => { this.held = null; this.cancel(); });
    document.addEventListener('visibilitychange', () => { if (document.hidden) { this.held = null; this.cancel(); } });
    if (!this.Recognition) document.getElementById('voice-hint').textContent = 'Speech recognition is unavailable here. Use the topic finder to type a category, or try Chrome with microphone access.';
    this.defaultHint = document.getElementById('voice-hint').textContent;
  }
  /** Open a modal bound to a specific channel; never assign on recognition alone. */
  open(show = false) {
    if (!this.options.getChannel() || this.options.isBusy()) return false;
    this.abort();
    if (!show && this.dialog.open) this.dialog.close();
    this.channel = this.options.getChannel();
    this.query.value = '';
    document.getElementById('voice-channel').textContent = 'For: ' + this.options.getTitle();
    this.say('Type a topic, or hold right Control or right Command to speak.');
    this.draw();
    if (show) { this.show(); this.query.focus(); }
    return true;
  }
  /** Show choices only after key release; the captured channel stays fixed. */
  show() {
    if (!this.channel || this.channel !== this.options.getChannel()) return;
    if (!this.dialog.open) this.dialog.showModal();
    this.query.focus(); this.query.select();
  }
  /** Share microphone feedback outside the modal while the key is held. */
  say(message) {
    this.status.textContent = message;
    document.getElementById('voice-hint').textContent = message;
  }
  /** Start only after an explicit key press or click, with a bounded listening time. */
  listen() {
    if (!this.Recognition) { this.say('Speech recognition is unavailable in this browser. Type a topic below, or try Chrome.'); return; }
    const rec = new this.Recognition();
    this.recognition = rec;
    rec.lang = 'en-GB'; rec.continuous = false; rec.interimResults = true; rec.maxAlternatives = 1;
    this.say('Starting microphone… Allow microphone access if asked.');
    rec.onstart = () => {
      if (this.recognition !== rec) return;
      if (!this.held) { this.stop(); return; }
      this.say('Listening… Release right Control or right Command when finished.');
    };
    rec.onresult = event => {
      if (this.recognition !== rec || this.channel !== this.options.getChannel()) return;
      this.query.value = Array.from(event.results).map(result => result[0].transcript).join(' ').slice(0, 120);
      this.draw();
    };
    rec.onerror = event => {
      if (this.recognition !== rec) return;
      const messages = {'not-allowed':'Microphone permission was denied. Allow access in your browser, or type below.', 'service-not-allowed':'The browser speech service is unavailable. Try Chrome, or type below.', 'audio-capture':'No microphone is available. Check your input device, or type below.', 'network':'The browser speech service could not connect. Try again, or type below.', 'no-speech':'No speech heard. Hold right Control or right Command to try again, or type below.'};
      this.abort();
      this.say(messages[event.error] || 'Speech recognition stopped. Try again, or type below.');
      if (!this.held) this.show();
      this.draw();
    };
    rec.onend = () => {
      if (this.recognition !== rec) return;
      clearTimeout(this.timer); this.recognition = null;
      this.say(this.query.value.trim() ? 'Choose a topic below. You can edit the words if needed.' : 'No speech heard. Try again, or type a topic below.');
      if (!this.held) this.show();
      this.draw();
    };
    try { rec.start(); this.timer = setTimeout(() => { this.abort(); this.say('Listening stopped after 20 seconds. Release the key to choose or type a topic.'); this.draw(); }, 20000); }
    catch (_) { this.abort(); this.say('Could not start speech recognition. Try again, or type below.'); }
    this.draw();
  }
  /** Stop audio on release and allow a short time for the final transcript. */
  stop() {
    this.show();
    this.draw();
    if (!this.recognition) return;
    clearTimeout(this.timer);
    this.say('Finishing recognition…');
    try { this.recognition.stop(); } catch (_) { this.abort(); this.draw(); }
    this.timer = setTimeout(() => {
      this.abort(); this.say('Choose a topic, or edit the words below.'); this.draw();
    }, 8000);
  }
  /** Invalidate callbacks before aborting so late results cannot change a new session. */
  abort() {
    clearTimeout(this.timer);
    const rec = this.recognition; this.recognition = null;
    if (rec) { try { rec.abort(); } catch (_) { /* Already disconnected. */ } }
  }
  /** Dismiss without writing; selection changes and loss of focus also cancel. */
  cancel() { this.abort(); if (this.dialog.open) this.dialog.close(); this.channel = null; document.getElementById('voice-hint').textContent = this.defaultHint; }
  /** Show text-only suggestions, with an explicit click required for every save. */
  draw() {
    this.results.replaceChildren();
    const matches = matchingTopics(this.options.getTopics(), this.query.value);
    for (const topic of matches) {
      const button = document.createElement('button');
      button.type = 'button'; button.textContent = topic.name;
      button.disabled = !!this.held;
      button.onclick = () => {
        if (this.held || this.options.isBusy() || this.channel !== this.options.getChannel()) return;
        this.cancel(); this.options.assign(topic);
      };
      this.results.append(button);
    }
    if (!matches.length) {
      const message = document.createElement('p');
      message.textContent = this.query.value.trim() ? 'No matching topics. Try fewer words, edit the spelling, or create a topic on the left.' : 'Say or type part of an existing topic name.';
      this.results.append(message);
    }
  }
}
