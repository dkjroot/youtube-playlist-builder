/* Local management client. Topic changes stay local until an explicit update;
   all personal strings use DOM text nodes, never HTML interpolation. */
'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name=yt-local]').content;
let state, draft = null,
  creating = false,
  running = false,
  polling = null;
const node = (tag, text, cls) => {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
};
const number = id => Number($(id).value);
const notice = (text, error = false) => {
  $('notice').textContent = text;
  $('notice').className = error ? 'error' : '';
};
/** Use the per-launch token on the fixed local JSON API; expose errors in the page. */
async function api(path, body) {
  const r = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: {
      'X-YT-Local': token,
      'Content-Type': 'application/json'
    },
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  const data = await r.json();
  if (!r.ok) throw Error(data.error || 'Request failed');
  return data;
}
/** Resolve channel names from the local export, falling back to the stable ID. */
function title(cid) {
  return state.channels.find(c => c.channel_id === cid)?.title || cid;
}
/** Show the cached channel description on hover without making another API request. */
function channelDescription(cid) {
  return state.channels.find(c => c.channel_id === cid)?.description?.trim() || 'No channel description available.';
}
/** Format the draft capacity without confusing unlimited with zero allocated slots. */
function maximum() {
  return draft.max_videos === null ? 'Unlimited' : draft.max_videos;
}
/** Show local topic/queue/quota data; never initiate remote work on page load. */
function fillSummary() {
  const cards = [
    ['Topics', state.topics.length],
    ['Due now', state.queue.length],
    ['Tracked API units today',
      `${state.quota.used.toLocaleString()} / ${state.settings.daily_budget.toLocaleString()}`
    ]
  ];
  $('summary').replaceChildren(...cards.map(([label, value]) => {
    const e = node('div', undefined, 'metric');
    e.append(node('strong', value), node('span', label));
    return e;
  }));
}
/** Render topic navigation using text nodes so channel/topic metadata cannot inject HTML. */
function listTopics() {
  const box = $('topic-list');
  box.replaceChildren();
  for (const t of state.topics) {
    const b = node('button', undefined, 'topic' + (draft?.name === t.name && !creating ? ' selected' : ''));
    b.append(node('strong', t.name), node('small',
      `${t.enabled?'Enabled':'Paused'} · ${t.item_count} videos · ${t.max_videos??'unlimited'} capacity`));
    const due = !t.next_due || Date.parse(t.next_due) <= Date.now()
      ? 'Due now'
      : 'Next: ' + new Intl.DateTimeFormat('en-GB', {day:'numeric', month:'short', timeZone:state.settings.timezone}).format(new Date(t.next_due));
    b.append(node('small', due));
    b.onclick = () => openTopic(t);
    box.append(b);
  }
}
/** Keep save feedback visible; failures leave the draft orange and retryable. */
function saveStatus(status) {
  const button = $('save-topic');
  button.dataset.status = status;
  button.disabled = status === 'saving';
  button.textContent = status === 'saved' ? 'Saved' : status === 'saving' ? 'Saving…' : 'Save topic — unsaved changes';
}
/** Any form edit needs an explicit successful local save, including channel buttons. */
function markUnsaved() {
  if (draft) saveStatus('dirty');
}
/** Edit an independent draft; data is persisted only by Save topic. */
function openTopic(topic, isNew = false) {
  creating = isNew;
  draft = structuredClone(topic);
  draft.channel_limits ||= {};
  draft.channel_max_age_days ||= {};
  draft.channel_priorities ||= {};
  $('topic-form').hidden = false;
  $('editor-title').textContent = isNew ? 'New topic' : topic.name;
  $('name').value = topic.name;
  $('name').disabled = !isNew;
  $('playlist-name').value = topic.playlist_name || '';
  $('description').value = topic.description || '';
  $('frequency').value = topic.frequency;
  $('max-age').value = topic.max_age_days;
  $('enabled').checked = !!topic.enabled;
  $('limited').checked = topic.max_videos !== null;
  $('maximum').value = topic.max_videos ?? 40;
  $('includes').value = topic.include_words.join('\n');
  $('excludes').value = topic.exclude_words.join('\n');
  $('remove-topic').hidden = isNew;
  $('playlist-link').hidden = !topic.playlist_id;
  if (topic.playlist_id) $('playlist-link').href = 'https://www.youtube.com/playlist?list=' +
    encodeURIComponent(topic.playlist_id);
  rows();
  listTopics();
  saveStatus(isNew ? 'dirty' : 'saved');
}
/** An explicit overall-size edit resets all channel allocations to an even split. */
function allocate() {
  if (!draft) return;
  draft.max_videos = $('limited').checked ? Math.max(0, Math.trunc(number('maximum'))) : null;
  draft.channel_limits = {};
  if (draft.max_videos !== null) {
    const channels = [...draft.channels].sort(),
      n = channels.length;
    channels.forEach((cid, i) => draft.channel_limits[cid] = Math.floor(draft.max_videos / n) + (i < draft
      .max_videos % n ? 1 : 0));
  }
  rows();
}
/** An individual channel edit changes the total, preserving every other allocation. */
function recalc() {
  if (draft.max_videos !== null) {
    draft.max_videos = Object.values(draft.channel_limits).reduce((a, b) => a + b, 0);
    $('maximum').value = draft.max_videos;
  }
  info();
}
/** Keep the displayed total and limited/unlimited control states consistent. */
function info() {
  $('maximum').disabled = draft.max_videos === null;
  $('allocation-info').textContent =
    `${draft.channels.length} channels · ${maximum()} slots · ${draft.max_videos===null?'no size cap':'unused slots stay empty'}`;
}
/** Build allocation/priority/age controls; blank age overrides inherit the topic default. */
function rows() {
  const body = $('channel-rows');
  body.replaceChildren();
  for (const cid of draft.channels) {
    const tr = node('tr'),
      label = node('td', title(cid));
    label.title = channelDescription(cid);
    const priority = node('select');
    priority.setAttribute('aria-label', 'Priority for ' + title(cid));
    for (const level of ['high', 'medium', 'low']) {
      const o = node('option', level);
      o.value = level;
      priority.append(o);
    }
    priority.value = draft.channel_priorities[cid] || 'medium';
    priority.onchange = () => draft.channel_priorities[cid] = priority.value;
    const slots = node('input');
    slots.type = 'number';
    slots.min = 0;
    slots.value = draft.channel_limits[cid] ?? '';
    slots.disabled = draft.max_videos === null;
    slots.placeholder = '∞';
    slots.setAttribute('aria-label', 'Slots for ' + title(cid));
    slots.onchange = () => {
      draft.channel_limits[cid] = Math.max(0, Math.trunc(Number(slots.value)));
      recalc();
    };
    const age = node('input');
    age.type = 'number';
    age.min = 1;
    age.value = draft.channel_max_age_days[cid] ?? '';
    age.placeholder = $('max-age').value;
    age.title = 'Blank inherits the topic default';
    age.setAttribute('aria-label', 'Age override for ' + title(cid));
    age.onchange = () => {
      if (age.value === '') delete draft.channel_max_age_days[cid];
      else draft.channel_max_age_days[cid] = Math.max(1, Math.trunc(Number(age.value)));
    };
    const remove = node('button', '×');
    remove.type = 'button';
    remove.setAttribute('aria-label', 'Remove ' + title(cid));
    remove.onclick = () => {
      markUnsaved();
      draft.channels = draft.channels.filter(c => c !== cid);
      delete draft.channel_limits[cid];
      delete draft.channel_priorities[cid];
      delete draft.channel_max_age_days[cid];
      recalc();
      rows();
    };
    for (const control of [priority, slots, age, remove]) {
      const td = node('td');
      td.append(control);
      tr.append(td);
    }
    tr.prepend(label);
    body.append(tr);
  }
  info();
}
/** Add a channel locally, preserving current allocations and adding an average-sized share. */
function addChannel(cid) {
  if (!draft) return notice('Create or choose a topic first.', true);
  if (draft.channels.includes(cid)) return;
  const average = Math.max(1, Math.floor((draft.max_videos || 0) / Math.max(1, draft.channels.length)));
  markUnsaved();
  draft.channels.push(cid);
  if (draft.max_videos !== null) draft.channel_limits[cid] = average;
  recalc();
  rows();
}
/** Filter the local subscription export; drag and + actions only edit the current draft. */
function library() {
  const q = $('channel-search').value.toLocaleLowerCase();
  const box = $('channel-library');
  box.replaceChildren();
  const matches = state.channels.filter(c => (c.title + ' ' + (c.description || '')).toLocaleLowerCase()
    .includes(q));
  for (const c of matches.slice(0, 150)) {
    const row = node('div', undefined, 'channel-list-item');
    row.draggable = true;
    row.title = channelDescription(c.channel_id);
    row.ondragstart = e => e.dataTransfer.setData('text/plain', c.channel_id);
    const b = node('button', '+');
    b.title = 'Add ' + c.title;
    b.onclick = () => addChannel(c.channel_id);
    row.append(node('span', c.title), b);
    box.append(row);
  }
  if (matches.length > 150) box.append(node('p',
    `Showing 150 of ${matches.length}. Search to narrow the list.`));
  if (!state.channels.length) box.append(node('p', 'Refresh subscriptions to load your channels.'));
}
/** Refresh local management data and forecast after saves or completed jobs. */
async function load() {
  state = await api('/api/state');
  fillSummary();
  listTopics();
  library();
  $('frequency').replaceChildren(...state.frequencies.map(f => {
    const o = node('option', f);
    o.value = f;
    return o;
  }));
  if (draft) {
    const t = state.topics.find(t => t.name === draft.name);
    if (t && !creating) openTopic(t);
  } else if (state.topics.length) openTopic(state.topics[0]);
  await forecast();
}
/** Request a local-only planning scenario using the explicit expected matching-upload rate. */
async function forecast() {
  try {
    const f = await api('/api/forecast?rate=' + encodeURIComponent($('rate').value || 0) + '&mode=' + $('forecast-mode').value + '&days=' + $('forecast-days').value);
    $('chart').style.gridTemplateColumns = `repeat(${f.days.length}, minmax(35px, 1fr))`;
    $('chart').style.minWidth = (f.days.length * 42) + 'px';
    const budgetMode = f.mode === 'budget';
    // A capped spend bar hides an unsustainable queue. Stack what remains after
    // today's work above what was funded, using one scale for the entire period.
    const total = d => d.units + (budgetMode ? d.carried_units : 0);
    const scale = Math.max(f.budget, ...f.days.map(total), 1);
    const height = 150;
    $('chart').replaceChildren(...f.days.map((d, index) => {
      const cell = node('div', undefined, 'bar-cell'),
        bar = node('div', undefined, 'bar' + (!budgetMode && d.units > f.budget ? ' over' : ''));
      bar.style.height = Math.max(2, height * total(d) / scale) + 'px';
      if (budgetMode) {
        const queued = node('div', undefined, 'bar-queued');
        // The orange segment is the end-of-day queue, not newly requested work.
        queued.style.height = (total(d) ? 100 * d.carried_units / total(d) : 0) + '%';
        bar.append(queued);
      }
      const previousQueue = index ? f.days[index - 1].carried_units : 0;
      const delta = budgetMode ? d.carried_units - previousQueue : 0;
      cell.title = budgetMode
        ? `${d.date}: ${total(d).toLocaleString()} units including carried work; ${d.units.toLocaleString()} spent/estimated; ${d.requested_units.toLocaleString()} newly requested; ${d.carried_units.toLocaleString()} still queued (${delta >= 0 ? '+' : ''}${delta.toLocaleString()} since previous day)`
        : `${d.date}: ${d.topics} topics; ${d.adds} additions, ${d.removes} removals, ~${d.reads} reads`;
      cell.append(node('span', total(d).toLocaleString()), bar, node('small', d.date.slice(5)));
      return cell;
    }));
    const ceiling = node('div', undefined, 'budget-line');
    ceiling.style.bottom = (17 + height * f.budget / scale) + 'px';
    ceiling.title = 'Local daily ceiling: ' + f.budget.toLocaleString();
    $('chart').append(ceiling);
    $('forecast-detail').textContent =
      `Daily local ceiling: ${f.budget.toLocaleString()} units. Dashed line marks the ceiling. ` +
      (budgetMode
        ? 'Green: spent/estimated today. Orange: unfinished work carried to the next day. Full bar: today’s work plus the queue brought forward. '
        : 'Orange bars exceed the ceiling. ') +
      'Hover for daily figures and queue changes; scroll horizontally for the full period.';
    if (budgetMode) {
      const first = f.days[0].carried_units, last = f.days.at(-1).carried_units;
      const change = last - first;
      $('forecast-backlog').textContent =
        `Queue after today: ${first.toLocaleString()} units; after ${f.days.length} days: ${last.toLocaleString()} units ` +
        `(${change > 0 ? 'up' : change < 0 ? 'down' : 'unchanged'}${change ? ' ' + Math.abs(change).toLocaleString() : ''} from today). ` +
        `Today includes ${f.days[0].recorded_units.toLocaleString()} already recorded units.`;
    } else {
      $('forecast-backlog').textContent = 'Requested work before applying the daily budget.';
    }
    $('assumptions').textContent = f.assumptions;
  } catch (e) {
    notice(e.message, true);
  }
}
/** Validate and persist local topic settings; no playlist API operations occur here. */
async function save(e) {
  e.preventDefault();
  saveStatus('saving');
  try {
    const name = $('name').value.trim();
    const fields = {
      description: $('description').value,
      playlist_name: $('playlist-name').value.trim() || null,
      frequency: $('frequency').value,
      max_age_days: number('max-age'),
      enabled: $('enabled').checked,
      channels: draft.channels,
      channel_priorities: draft.channel_priorities,
      channel_max_age_days: draft.channel_max_age_days,
      include_words: $('includes').value.split('\n').filter(Boolean),
      exclude_words: $('excludes').value.split('\n').filter(Boolean)
    }; /* Only send the total when redistributed; explicit allocations are authoritative. */
    fields.max_videos = draft.max_videos;
    if (draft.max_videos !== null) fields.channel_limits = draft.channel_limits;
    await api('/api/topic', {
      action: creating ? 'add' : 'edit',
      name,
      fields
    });
    saveStatus('saved');
    creating = false;
    draft.name = name;
    notice('Saved locally. Preview or update when you’re ready.');
    await load();
  } catch (e) {
    saveStatus('dirty');
    notice(e.message, true);
  }
}
/** Launch an explicit CLI-backed network operation and start polling its bounded progress log. */
async function job(action) {
  if (running) return notice('Wait for the current operation to finish.', true);
  try {
    if (['preview', 'update', 'reconcile'].includes(action) && (!draft || creating)) throw Error(
      'Save and choose a topic first.');
    await api('/api/jobs', {
      action,
      name: draft?.name
    });
    running = true;
    $('job-status').textContent = 'Running…';
    notice('Operation started. Progress appears below.');
    polling = setInterval(poll, 700);
    await poll();
  } catch (e) {
    notice(e.message, true);
  }
}
/** Display worker progress, completion/pauses and topic suggestions without blocking editing. */
async function poll() {
  try {
    const j = await api('/api/job');
    if (!j) return;
    $('activity').textContent = j.lines.join('\n');
    $('activity').scrollTop = $('activity').scrollHeight;
    if (!j.running) {
      clearInterval(polling);
      running = false;
      $('job-status').textContent = j.code === 0 ? 'Completed' : j.code === 2 ? 'Paused: quota or budget' :
        'Finished with an error';
      notice($('job-status').textContent, j.code === 1);
      if (j.result) {
        $('suggestions').replaceChildren(...j.result.map(s => {
          const b = node('button', `${s.name} (${s.channels.length} channels)`);
          b.onclick = () => openTopic({
            name: s.name,
            channels: s.channels.map(c => c.channel_id),
            channel_priorities: {},
            channel_limits: {},
            channel_max_age_days: {},
            max_videos: null,
            frequency: 'daily',
            max_age_days: 7,
            enabled: true,
            include_words: [],
            exclude_words: [],
            description: ''
          }, true);
          return b;
        }));
      }
      await load();
    }
  } catch (e) {
    clearInterval(polling);
    running = false;
    notice(e.message, true);
  }
}
$('topic-form').onsubmit = save;
$('topic-form').addEventListener('input', markUnsaved);
$('topic-form').addEventListener('change', markUnsaved);
$('channel-search').oninput = library;
$('rate').onchange = forecast;
$('forecast-mode').onchange = forecast;
$('forecast-days').onchange = forecast;
$('limited').onchange = allocate;
$('maximum').onchange = allocate;
$('max-age').onchange = rows;
$('new-topic').onclick = () => openTopic({
  name: '',
  channels: [],
  channel_priorities: {},
  channel_limits: {},
  channel_max_age_days: {},
  max_videos: null,
  frequency: 'daily',
  max_age_days: 7,
  enabled: true,
  include_words: [],
  exclude_words: [],
  description: ''
}, true);
for (const [id, action] of [
    ['auto', 'auto'],
    ['all', 'all'],
    ['preview', 'preview'],
    ['update', 'update'],
    ['reconcile', 'reconcile'],
    ['refresh', 'refresh'],
    ['suggest', 'suggest']
  ]) $(id).onclick = () => job(action);
$('dropzone').ondragover = e => {
  e.preventDefault();
  $('dropzone').classList.add('over');
};
$('dropzone').ondragleave = () => $('dropzone').classList.remove('over');
$('dropzone').ondrop = e => {
  e.preventDefault();
  $('dropzone').classList.remove('over');
  const cid = e.dataTransfer.getData('text/plain');
  if (state.channels.some(c => c.channel_id === cid)) addChannel(cid);
};
$('remove-topic').onclick = async () => {
  if (!confirm('Remove this local topic? Its YouTube playlist will be kept.')) return;
  try {
    await api('/api/topic', {
      action: 'delete',
      name: draft.name
    });
    draft = null;
    $('topic-form').hidden = true;
    notice('Topic removed; its playlist is kept.');
    await load();
  } catch (e) {
    notice(e.message, true);
  }
};
$('settings-open').onclick = () => {
  for (const [id, key] of [
      ['daily-budget', 'daily_budget'],
      ['timezone', 'timezone'],
      ['mode', 'mode'],
      ['min-length', 'minimum_video_seconds'],
      ['cache-hours', 'subscription_cache_hours']
    ]) $(id).value = state.settings[key];
  $('settings').showModal();
};
$('settings-close').onclick = () => $('settings').close();
$('settings-form').onsubmit = async e => {
  e.preventDefault();
  try {
    await api('/api/settings', {
      daily_budget: number('daily-budget'),
      timezone: $('timezone').value,
      mode: $('mode').value,
      minimum_video_seconds: number('min-length'),
      subscription_cache_hours: number('cache-hours')
    });
    $('settings').close();
    notice('Settings saved.');
    await load();
  } catch (e) {
    notice(e.message, true);
  }
};
load().then(async () => {
  const j = await api('/api/job');
  if (j?.running) {
    running = true;
    polling = setInterval(poll, 700);
  }
}).catch(e => notice(e.message, true));

/** Explicitly rearrange saved dates; forecasts/quotas are read locally, never remote requests. */
$('redistribute').onclick = async () => {
  if (running) return notice('Wait for the current update to finish before redistributing.', true);
  const button = $('redistribute');
  button.disabled = true;
  button.textContent = 'Redistributing…';
  try {
    const result = await api('/api/redistribute', {rate: Number($('rate').value || 0)});
    notice(result.message);
    // Refresh scheduling data without discarding an unsaved topic form.
    state = await api('/api/state');
    fillSummary();
    listTopics();
    await forecast();
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = 'Redistribute schedules';
  }
};
