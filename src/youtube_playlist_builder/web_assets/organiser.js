/* Channel-first organisation. All assignments are explicit local saves; browsing
   reads cached data only. Remote thumbnails load directly from YouTube's CDN. */
'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name=yt-local]').content;
let state, selected = null, filter = '', busy = false, detailRequest = 0;
const feedAttempts = new Map();
/** Build text-only DOM nodes so channel metadata never becomes HTML markup. */
function node(tag, text, cls) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (cls) element.className = cls;
  return element;
}
/** Call only the fixed local API, carrying its same-origin session token. */
async function api(path, body) {
  const response = await fetch(path, {method:body === undefined ? 'GET' : 'POST', headers:{'X-YT-Local':token,'Content-Type':'application/json'}, body:body === undefined ? undefined : JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw Error(data.error || 'Request failed');
  return data;
}
/** Report confirmed saves and failures without losing the current selection. */
function notice(text, error = false) { $('notice').textContent = text; $('notice').className = error ? 'error' : ''; }
/** Include paused topics in membership counts: they still categorise channels. */
function memberships(cid) { return state.topics.filter(t => t.channels.includes(cid)); }
/** Rebuild topic filters and the full searchable channel list from saved state. */
function render() {
  $('single-topic').checked = !!state.settings.single_topic_channels;
  const filters = $('topic-filters');
  filters.replaceChildren();
  for (const topic of [null, ...state.topics]) {
    const name = topic?.name || '';
    const count = topic ? state.channels.filter(c => topic.channels.includes(c.channel_id)).length : state.channels.length;
    const button = node('button', `${name || 'All channels'} (${count})`, filter === name ? 'selected' : '');
    button.onclick = () => { if (busy) return; filter = name; if (name) $('hide-assigned').checked = false; render(); };
    filters.append(button);
  }
  const query = $('search').value.trim().toLocaleLowerCase();
  const channels = state.channels.filter(c => {
    const topics = memberships(c.channel_id);
    return (!filter || topics.some(t => t.name === filter)) && (!$('hide-assigned').checked || !topics.length) &&
      `${c.title}\n${c.description || ''}`.toLocaleLowerCase().includes(query);
  }).sort((a,b) => a.title.localeCompare(b.title));
  const remaining = state.channels.filter(c => !memberships(c.channel_id).length).length;
  $('channel-count').textContent = `${channels.length} shown · ${remaining} uncategorised of ${state.channels.length}`;
  $('channels').replaceChildren(...channels.map(c => {
    const button = node('button', undefined, selected === c.channel_id ? 'selected' : '');
    button.append(node('strong', c.title), node('small', memberships(c.channel_id).map(t => t.name).join(', ') || 'Uncategorised'));
    button.onclick = () => { if (!busy) choose(c.channel_id); };
    return button;
  }));
  if (!channels.length) $('channels').append(node('p','No channels match these filters.'));
}
/** Save one membership at a time; the server merges it with fresh topic settings. */
async function assign(topic, checked) {
  if (busy) return;
  busy = true;
  $('memberships').querySelectorAll('input').forEach(input => input.disabled = true);
  notice('Saving assignment…');
  try {
    await api('/api/assignment', {channel_id:selected, topic:topic.name, assigned:checked});
    state = await api('/api/state');
    notice('Assignment saved.');
  } catch (error) { notice(error.message, true); }
  finally { busy = false; render(); renderMemberships(); }
}
/** Show independent checkboxes, allowing one channel in multiple topics if wanted. */
function renderMemberships() {
  const box = $('memberships');
  if (!box) return;
  box.replaceChildren();
  for (const topic of state.topics) {
    const label = node('label'), input = node('input');
    input.type = 'checkbox'; input.checked = topic.channels.includes(selected); input.disabled = busy;
    input.onchange = () => {
      if (busy) { input.checked = topic.channels.includes(selected); return; }
      assign(topic, input.checked);
    };
    label.append(input, node('span',topic.name), node('small',topic.enabled ? topic.frequency : 'Automatic updates off'));
    box.append(label);
  }
  if (state.settings.single_topic_channels && memberships(selected).length > 1) {
    box.append(node('p','Choose a topic to keep only that membership.', 'muted'));
  }
  if (!state.topics.length) box.append(node('p','Create a topic on the left to start categorising.'));
}
/** Show descriptions immediately; guard asynchronous details against rapid selection changes. */
async function choose(cid) {
  topicVoice.cancel();
  selected = cid;
  const request = ++detailRequest;
  render();
  const channel = state.channels.find(c => c.channel_id === cid), box = $('detail');
  // Reuse the global preference control when replacing the selected channel.
  $('preference-store').append($('single-topic-option'));
  box.replaceChildren(node('h2',channel.title));
  const link = node('a','Open channel on YouTube ↗');
  link.href = 'https://www.youtube.com/channel/' + encodeURIComponent(cid) + '/videos'; link.target = '_blank'; link.rel = 'noopener noreferrer';
  box.append(link, node('p', channel.description || 'No description saved for this channel.', 'channel-description'));
  box.append(node('h3','Recent saved videos'), node('p','Up to five saved videos; these may be incomplete or out of date. Selecting a channel refreshes its public feed at most once every 24 hours.','muted'));
  const fetchButton = node('button','Fetch recent videos');
  fetchButton.type = 'button';
  fetchButton.onclick = () => fetchRecent(cid, fetchButton);
  box.append(fetchButton, node('p','Uses YouTube’s public feed: no API quota. Refresh now to check for newer uploads.','muted'));
  const videos = node('div'), loading = node('p','Loading saved videos…');
  box.append(videos, loading);
  // Put the evidence before the category choices, including while details load.
  const assignments = node('div'); assignments.id = 'memberships';
  const topicHeading = node('div', undefined, 'assignment-heading');
  topicHeading.append(node('h3','Topics'), $('single-topic-option'));
  box.append(topicHeading, assignments); renderMemberships();
  try {
    const detail = await api('/api/channel?id=' + encodeURIComponent(cid));
    if (request !== detailRequest) return;
    loading.remove();
    if (detail.feed_fetched_at) videos.append(node('p','Public feed last fetched: ' + new Date(detail.feed_fetched_at).toLocaleString(), 'muted'));
    if (!detail.videos.length) videos.append(node('p','No videos saved for this channel yet. Use Fetch recent videos, or open the channel on YouTube above.'));
    for (const video of detail.videos) {
      const card = node('div',undefined,'video-card'), text = node('div'), title = node('a',video.title || video.video_id);
      title.href = 'https://www.youtube.com/watch?v=' + encodeURIComponent(video.video_id); title.target = '_blank'; title.rel = 'noopener noreferrer';
      if (/^[\w-]{11}$/.test(video.video_id)) {
        const image = node('img'); image.src = `https://i.ytimg.com/vi/${video.video_id}/mqdefault.jpg`; image.alt = ''; image.loading = 'lazy'; image.referrerPolicy = 'no-referrer';
        image.onerror = () => image.remove(); card.append(image);
      }
      text.append(title, node('small',video.published_at?.slice(0,10) || 'Publication date unknown'));
      if (video.description) text.title = video.description;
      card.append(text); videos.append(card);
    }
    // Only the selected channel is fetched, not the whole subscription list.
    // Failed feeds back off for five minutes instead of retrying on every click.
    const fetched = Date.parse(detail.feed_fetched_at || '');
    const attempted = feedAttempts.get(cid) || 0;
    if ((!Number.isFinite(fetched) || Date.now() - fetched >= 86400000) && Date.now() - attempted >= 300000) {
      await fetchRecent(cid, fetchButton, false);
    }
  } catch (error) { if (request === detailRequest) { loading.remove(); videos.append(node('p',error.message)); } }
}
/** Refresh one selected public feed, retaining cached details on failures. */
async function fetchRecent(cid, button, refresh = true) {
  if (busy) return;
  feedAttempts.set(cid, Date.now());
  busy = true; button.disabled = true; button.textContent = 'Fetching…';
  renderMemberships();
  try {
    await api('/api/channel-fetch', {channel_id:cid, refresh});
    notice('Recent videos saved. No API quota used.');
    await choose(cid);
  } catch (error) { notice(error.message,true); }
  finally { busy = false; button.disabled = false; button.textContent = 'Fetch recent videos'; renderMemberships(); }
}
/** Create paused categories by default; enabling playlist updates is a deliberate choice. */
$('new-topic').onsubmit = async event => {
  event.preventDefault();
  if (busy) return;
  busy = true;
  const button = event.currentTarget.querySelector('button'); button.disabled = true;
  try {
    const name = $('topic-name').value.trim();
    await api('/api/topic',{action:'add',name,fields:{channels:[],enabled:$('topic-enabled').checked,frequency:'weekly',max_age_days:30}});
    state = await api('/api/state'); $('topic-name').value = ''; notice(`Created ${name}.`); render(); renderMemberships();
  } catch (error) { notice(error.message,true); }
  finally {
    busy = false; button.disabled = false;
    // Creation redraws memberships while busy; re-enable them after saving ends.
    renderMemberships();
  }
};
/** Persist the organiser preference without moving any existing assignments. */
$('single-topic').onchange = async () => {
  const control = $('single-topic');
  if (busy) { control.checked = !!state.settings.single_topic_channels; return; }
  busy = true; control.disabled = true;
  try {
    await api('/api/settings', {single_topic_channels:control.checked});
    state = await api('/api/state');
    notice('Organiser preference saved. Existing assignments unchanged.');
  } catch (error) { control.checked = !!state.settings.single_topic_channels; notice(error.message,true); }
  finally { busy = false; control.disabled = false; render(); renderMemberships(); }
};
$('search').oninput = render;
$('hide-assigned').onchange = () => { if ($('hide-assigned').checked) filter = ''; render(); };
/** Load local data once; do not launch subscription refreshes or playlist jobs. */
async function start() {
  try { state = await api('/api/state'); render(); }
  catch (error) { notice(error.message,true); }
}
const topicVoice = new TopicVoice({
  getChannel: () => selected,
  getTitle: () => state?.channels.find(c => c.channel_id === selected)?.title || '',
  getTopics: () => state?.topics || [],
  isBusy: () => busy,
  assign: topic => assign(topic, true),
});
start();
