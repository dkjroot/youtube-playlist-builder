# YouTube playlist builder

I subscribe to channels about quite a few different things. Some of them upload
so often that everything else gets buried in my subscriptions feed. I really 
think that the ability to categorise your subscriptions is a basic feature that
YouTube should support. Since they don't, this project is my attempt to make that
feed useful again.

The idea is to keep a private playlist for each subject, filled with recent
videos from channels I already follow. If I feel like watching mechanical
engineering videos, I should be able to open that playlist without wading
through a hundred chess videos first. Old videos will drop out as they pass the
chosen age limit.

It runs in Python on macOS or Linux and uses the YouTube Data API. You can manage
topics through a local web interface, terminal menu or command-line options, set their update frequency,
and keep a rolling private playlist for each one. Matching can use simple channel
selections and title filters, or an optional local or hosted language model.

**[Support my work on Buy Me a Coffee](https://buymeacoffee.com/dkjroot)**

### Free to use. Please help keep it that way.

I develop this project to be freely available to everyone, supported, if at all,
by voluntary contributions. I do not want other people to profit from my work
by selling copies, putting it behind a paywall, or building a paid application
or hosted service around it. I ask you to respect that intention: please keep
this work freely available, credit its origins, and direct people to
[my official GitHub repository](https://github.com/dkjroot/youtube-playlist-builder) for the software.

If you want to help fund my development, please contribute **directly to me**
through [Buy Me a Coffee](https://buymeacoffee.com/dkjroot). Paying a distributor
or hosting provider does not fund my work unless I explicitly say otherwise.
There is no obligation to contribute.

**This is a personal request, not an additional license restriction.** The
[GNU General Public License v3.0](LICENSE) permits commercial use and
redistribution, including forks, subject to its terms. Those legal permissions
remain unchanged by this request. See [licensing details](LICENSING.md) and
[third-party credits](THIRD_PARTY_NOTICES.md).

## Setup

### Install the Python project

Use Python 3.11 or later. Open a terminal in the project folder and run:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

The install creates the `yt-playlists` command inside your virtual environment.
The `-e` means it uses the source in this folder, so changes to the Python code
take effect without reinstalling. In a new terminal, run
`source .venv/bin/activate` again before using the commands below. The `.venv/`
folder is local to your computer and isn't committed to Git.

### Create a Google Cloud project

You need your own Google Cloud project so that requests use your quota.

1. Create or select a project in the [Google Cloud console](https://console.cloud.google.com/).
2. Go to **APIs & Services → Library**, search for **YouTube Data API v3**,
   and click **Enable**. Review Google's API terms when enabling it.
3. If you use the credential wizard and it asks whether this is an AI-powered
   agent or MCP integration, choose **No**. This Python app makes the API calls
   directly. If it asks which data you need, choose **User data**.

You don't need a separate API key for this project.

### Configure Google sign-in

Open **Google Auth Platform**. You can also get there through
**APIs & Services → OAuth consent screen**.

1. Under **Branding**, enter an app name, such as `My YouTube Playlist Builder`,
   and fill in the required support email and developer contact details.
2. Under **Audience**, choose **External** for a personal Google account and
   leave the publishing status as **Testing**.
3. On that same **Audience** page, find **Test users**, click **Add users**, and
   add the Google email address you'll use to sign in to YouTube. Save it.
   Being the project owner does **not** automatically make you a test user.
4. Under **Data Access → Add or remove scopes**, add these two scopes and save:

   | Scope | What we use it for |
   | --- | --- |
   | `https://www.googleapis.com/auth/youtube.readonly` | Read your subscriptions and video details. |
   | `https://www.googleapis.com/auth/youtube` | Create playlists and add or remove their videos. |

Adding both scopes now means you won't need to return to the console for the
playlist test. The script still requests read access first. Google asks for
your approval separately when you run a command that needs write access.
The management permission covers more than playlists; YouTube doesn't offer a
permission limited to editing playlists.

You don't need to publish the app or complete Google verification to use it
with your listed test account.

### Download the desktop OAuth credentials

Under **Google Auth Platform → Clients**, create an OAuth client with the
application type **Desktop app**. Give it a name, then download its JSON file.
If the credential wizard has already created a Desktop client for you, use
that client's download instead.

Save the downloaded file as `client_secret.json` in the project folder,
alongside `pyproject.toml`. This file identifies your app to Google. It isn't
the access token: the script saves that separately after you sign in.

### Sign in and check your subscriptions

```sh
yt-playlists auth
```

Your browser will open Google's sign-in page. Choose the account and YouTube
channel whose subscriptions you want to use. The command waits up to three
minutes for sign-in. Approve the requested read access, then return to your
terminal. A successful sign-in saves your token in `.local/token.json`.

Now check that the app can read your subscriptions:

```sh
yt-playlists subscriptions
yt-playlists subscriptions --output .local/subscriptions.json
```

The export contains each channel's ID, title, and description. It includes all
pages of subscriptions, rather than stopping at the first 50 channels.

### Try creating a playlist

Request permission to manage playlists:

```sh
yt-playlists auth --write-access
```

Approve the additional permission in Google's sign-in page. Then choose a video
and run:

```sh
yt-playlists create-test-playlist --video-id VIDEO_ID
```

Replace `VIDEO_ID` with the ID from a YouTube video's URL (the part after `v=`).
The command creates a private playlist called **YouTube playlist builder — test**,
adds that video, and checks that the playlist is private and contains exactly
that one video. It prints the playlist link and saves the playlist and item IDs
in `.local/test-playlist.json`.

This is a one-off test. Running it again stops when that receipt already exists,
so it won't accidentally create another playlist. If a request fails partway
through, keep the receipt: it records the last attempted step and any returned
IDs so we can check what happened before retrying.

### If sign-in doesn't work

If Google says the app hasn't completed verification and shows
**Error 403: access_denied**, check **Google Auth Platform → Audience → Test
users**. Add the exact Google account you chose during sign-in, save it, and
run `yt-playlists auth` again. You can leave the app in Testing.

If the browser sign-in times out, rerun the command and complete sign-in within
three minutes. If you accidentally chose a different account, remove
`.local/token.json` and sign in again with the account you want to use.

Refresh tokens for External apps in Testing
[expire after seven days](https://developers.google.com/identity/protocols/oauth2#expiration).
If the script says it cannot renew your sign-in, remove `.local/token.json`
and run `yt-playlists auth --write-access` again if you need playlist access,
or `yt-playlists auth` for read access alone.

Google's [desktop OAuth guide](https://developers.google.com/identity/protocols/oauth2/native-app)
has more detail about the Google side of the setup.

### Keep credentials and personal data local

The OAuth client file, saved sign-in, and `.local/` folder are ignored by Git.
Keep personal exports in `.local/` too. The saved sign-in contains a refresh
token, so don't share it. You can remove the local token to forget it on this
computer, or revoke access through your Google account to invalidate it.

If you keep your files elsewhere, put the global options before the command:

```sh
yt-playlists --client-secrets /path/to/client.json --token /path/to/token.json subscriptions
```

## Using topics and playlists

### Local web management

Start the browser interface from the project directory, with the same environment
and credentials you use for the command line:

```sh
source .venv/bin/activate
# If your model key is in .env, load it as you normally do before starting.
yt-playlists web
```

The app opens at `http://127.0.0.1:8765` and stays running in that terminal until
you press Ctrl-C. Use `web --no-browser` if you'd rather open the address yourself,
or `web --port 8766` if the normal port is busy. It runs locally, uses the same
SQLite database, and has a dark theme. No node build or separate frontend install
is needed. Global `--state`, credential and `--llm-config` options go before `web`
just as they do before other commands.

Choose a topic to edit its schedule, age limit, name, description and filters.
Drag channels from the searchable subscriptions list into the editor, or use
its + buttons. Hover a channel name to read its description from the local
subscription export. Each channel has a priority dropdown, slot allocation and
optional age override. Save commits local settings; it doesn't update YouTube. Use Preview,
Update now, Update due topics or Update all to request that work explicitly.
Activity shows progress and budget pauses. Opening the page spends no API quota.

Refresh loads a fresh subscription export for the channel list and invalidates
the managed subscription cache for the next update. Suggest from subscriptions
uses your selected matching mode and lets you choose a suggestion as the starting
point for a new topic. Topic removal keeps its YouTube playlist. Existing settings,
OAuth setup and model keys are shared with the CLI; the page doesn't display keys
or replace the initial Google sign-in workflow.

### Playlist sizes and channel allocations

The default is **unlimited**, preserving the original age-based behaviour. To
limit a topic, enable Limit playlist size in the web editor and enter a total.
That divides the slots evenly between assigned channels, with any remainder
distributed deterministically. There is no cross-cutting or “other videos” pool.
A small total may give some channels zero slots, which you can adjust individually.

Changing a channel's allocation recalculates the overall total without changing
its neighbours. Channels keep firm maxima: if one has four slots and only one
eligible video, its other three slots stay empty. Newer matches replace that
channel's oldest entries. Adding a channel to a capped topic preserves existing
allocations and gives it an average-sized share, increasing the total; removing
one removes its allocation. Explicitly changing the overall total redistributes
all channels evenly again. Uncheck Limit playlist size to restore unlimited.

These controls are also available from the CLI:

```sh
yt-playlists topics set engineering --max-videos 40
yt-playlists topics set engineering --channel-slots CHANNEL_ID=4
yt-playlists topics set engineering --max-videos 0
```

Replace `CHANNEL_ID` with an assigned channel ID. `--max-videos 0` restores
unlimited. Setting an individual channel to zero slots excludes its videos from
a capped topic. Changing limits makes the topic due, but doesn't immediately
modify the playlist. Preview first: reducing an existing playlist from hundreds
of entries still needs individual removals. Cleanup shares topic turns and can
span several quota days. For capped topics, obsolete entries are removed before
new additions, so a partially completed replacement doesn't add on top of a full
allocation. Unlimited topics keep the existing alternating add/remove workflow.

### Channel-specific maximum ages

The topic's maximum age is the default for all its channels. In the editor, leave
Age override blank to inherit it, or enter a different number of days for that
channel. For example, engineering can default to 7 days while a channel that
uploads rarely keeps videos eligible for 180 days:

```sh
yt-playlists topics set engineering --max-age 7
yt-playlists topics set engineering --channel-max-age CHANNEL_ID=180
```

Use `--channel-max-age CHANNEL_ID=0` to restore inheritance. Overrides work in
both capped and unlimited topics. This is the maximum **age** of a published
video, separate from the minimum video **length** setting measured in seconds.

A six-month window is cheap for a channel with a few uploads, but scanning six
months of a busy channel can mean many pages and model calls. Capped discovery
works incrementally: it stops scanning a channel once enough eligible matches
fill its allocation. In a test with 1,800 uploads and four slots, it used one feed
page, one video-details request and one model batch. Filters can still force a
deeper scan when few videos match. Unlimited topics continue scanning the whole
configured age window.

### Organising your subscriptions

Choose **Organise channels** at the top of the web interface to work through your
subscriptions channel by channel. Search by name or description, then select a
channel to read its description and see up to five recent saved videos. Video
titles link to YouTube, and thumbnails can help you recognise the channel. There
is also a link to the channel itself when you want a closer look.

Tick topics to add the channel to them, or untick them to remove it. Each change
saves immediately, with a confirmation at the top. Channels can belong to more
than one topic. **Hide categorised channels** hides anything already assigned,
including channels in paused topics, so you can work through what's left. Click
a topic on the left to browse its channels instead.

To find a topic quickly, hold **right Control** or **right Command**, say a word
or two, then release the key. A dialog appears with matching topics: “photo” can
find Photography, and “engineering” can find several engineering categories.
Click a topic to save the assignment and close the dialog. Nothing is assigned
just because you said it. The text box contains what the browser heard; edit it
to filter the suggestions straight away. **Find a topic** opens the same box for
typing without using the microphone. Escape cancels.

Voice uses your browser's speech recognition where available. Try Chrome and
allow microphone access when asked. The browser may send audio to its online
speech service; this app doesn't record or store it. Topic matching happens
locally, with partial names and small spelling errors supported. If speech isn't
available or doesn't hear you properly, you can still type in the dialog.

If you usually want each channel in just one topic, enable **One topic per
channel** beside the Topics heading above the channel’s topic choices. Choosing a topic then removes the channel from all other
topics and saves the move in one go. The preference is remembered and is off by
default. Turning it on doesn't rearrange existing assignments. If a channel is already
in several topics, choosing one keeps just that topic. This
mode applies to the organiser; the main editor and CLI still let you manage
memberships explicitly. You can also change the preference from the command line:

```sh
yt-playlists settings --single-topic-channels on
```

Use `off` to restore independent checkboxes. Moves use the usual allocation rules:
removing a channel removes its slots, and adding it to a capped topic gives it an
average-sized share. Playlist contents change on their next update.

You can create topics on this page too. Automatic updates are off by default,
which makes a topic useful for channels you want to keep organised without
building a playlist. For example, a “support only” topic can hold channels you
follow but don't usually watch. Use the main playlist manager to enable a topic
or change its frequency, video limits and other settings later. The existing
channel picker on that page still works as before.

Selecting a channel automatically reads its public YouTube feed when the saved
feed is missing or more than 24 hours old. It shows up to five recent videos,
with the description above them and the topic choices below. **Fetch recent
videos** refreshes immediately if you want to check again. Only channels you
select are fetched; it doesn't scan all your subscriptions in the background.
There is no quota saving from asking for one or two videos instead of five, so
there isn't a separate count setting.

Public feeds and thumbnail images use no YouTube Data API or model quota and
need no extra sign-in. They do need an internet connection, may include Shorts
or live videos, and can occasionally be unavailable or incomplete. A failed
fetch keeps existing saved videos; you can still open the channel directly on
YouTube. The page shows when the feed was last fetched. Normal updates also
contribute saved video details. Playlist updates still use the API: the public
feed's short history cannot reliably cover long age windows or confirm video
availability and duration. Local categorisation changes take effect on playlists
when you next run an update.

### Quota forecasts and schedule redistribution

The web interface offers two local forecast views:

- **Budget & carry-over** is the default. It applies the local daily ceiling,
  includes today's recorded usage, and carries estimated unfinished work into
  later days. Each bar stacks funded work in green and the remaining queue in
  orange, so growing excess rises above the daily ceiling. Hover to see the
  queue size and its change from the previous day. A confirmed Google quota
  exhaustion prevents further forecast
  spending today. The backlog figure shows the work still waiting at the end of
  the displayed period, even when every daily bar fits within the ceiling.
- **Requested work** shows estimated demand before the daily ceiling. It helps
  identify which scheduled days are overloaded; it assumes those scheduled
  updates finish rather than rolling their work into later dates.

Automatic period covers at least two cycles of the longest enabled frequency,
with a minimum of four weeks. Monthly topics generally extend it to about two
months. You can choose 10 days, four weeks, two months or three months instead.
Scroll the chart horizontally to see all days and hover for daily figures.

Both views use saved schedules, current playlist entries, allocations and age
limits. Adjust **assumed matching videos per channel per day** to explore busier
or quieter publishing. This rate is your assumption, not measured channel history.
Save topic edits to include them in the next calculation. The dashed line is the
local ceiling; orange demand bars exceed it. Calculating either view spends no
YouTube or model quota.

Budget carry-over is an aggregate work-queue estimate, not an exact simulation
of future playlist contents. Reads are approximate, and mutations are charged in
whole 50-unit operations. Delayed videos can expire or be superseded, and a real
resumed update may need fresh reads. The baseline demand also assumes successful
scheduled updates, so the carried queue can overestimate work that would merge
or disappear in practice. It doesn't know future uploads, Google’s actual quota
allowance or LLM prices. Legacy entries without channel attribution are spread
evenly for the estimate until attribution is known.

Use **Redistribute schedules** beside the chart to spread recurring work across
days. It uses the selected upload-rate assumption and estimates costs over at
least eight weeks (longer if needed), considering all active frequencies. It only
accepts arrangements that improve estimated peaks or spread, rather than moving
fortnightly work just past the visible chart. Topic cards show the next scheduled
date afterwards. The same operation is available from menu option19 or:

```sh
yt-playlists redistribute --rate 1
```

This changes saved calendar slots and next update dates, not the chosen update
frequencies, last successful update times or quota counters. Disabled topics and
daily due dates stay as they are. Pending and unfinished updates remain due;
their future repeats can still get better slots. A first-fill backlog, a large
cleanup or more total demand than your allowance can therefore still produce
spikes. Redistribution doesn't update YouTube or make API/model calls. It uses
saved topic settings; unsaved form changes aren't included or discarded.

Run `yt-playlists` for the interactive menu, or use the commands below in scripts.
While entering a value or choosing an option, press **Escape** to abandon that
action and return to the main menu. You don't need to press Enter afterwards.
Normal arrow-key editing still works. Escape cancels input, not an API operation
that has already started; completed operations aren't rolled back.

When running in a terminal, progress messages show which topic or channel is
being processed, model batches and cache use, and playlist additions/removals.
Requests that take a while print a reminder every ten seconds. Progress goes to
terminal stderr, so redirecting command output or using `--json` still gives clean
results. Restart an open menu after installing an update to pick up the changes.

Start by exporting your subscriptions, then ask for suggestions:

```sh
yt-playlists subscriptions --output .local/subscriptions.json
yt-playlists suggest-topics --details
yt-playlists topics add chess --suggested --max-age 7 --frequency daily
yt-playlists update chess --preview
yt-playlists update chess
```

Review the suggested channels before adding a topic. You can also pick channels
yourself with `channels QUERY`, then pass their IDs using repeated `--channel`
options. Suggestions use your own subscription export. Text mode has a built-in
list of subjects and recognisable channels; LLM mode can suggest other subjects
based on what you actually follow. Neither mode adds topics without you choosing
them. Refresh the export when your subscriptions change.

Each topic gets a private playlist called `sub_<topic>`. For example, chess gets
`sub_chess`. A channel can belong to several topics. In text mode, recent uploads
from the selected channels are included unless you set title filters. In LLM
mode, the model also checks whether each video belongs in the topic. It uses
metadata, not the video's audio or images. Both modes respect optional `--include`
and `--exclude` title phrases; these are case-insensitive substring matches.

```sh
yt-playlists topics list
yt-playlists topics show chess
yt-playlists topics set chess --playlist-name "Chess this week"
yt-playlists topics set chess --description "Chess instruction and analysis, including tournaments."
yt-playlists topics set chess --frequency every-2-days --max-age 10
yt-playlists topics set chess --add-channel CHANNEL_ID
yt-playlists topics set chess --remove-channel CHANNEL_ID
yt-playlists topics set chess --exclude shorts
yt-playlists topics set chess --clear-filters
yt-playlists topics disable chess
yt-playlists topics enable chess
yt-playlists topics remove chess
```

In the menu, choose **3. Edit a topic → Add channels** to search your
subscriptions and add channels without replacing the existing selection. The
**Channels** choice replaces the selection instead.

Removing a topic keeps its YouTube playlist. Re-adding it creates a new managed
playlist. `--channel` replaces the entire channel selection; `--add-channel` and
`--remove-channel` change individual entries. Repeating `--include` accepts any
of those phrases; repeating `--exclude` rejects any of them. `--default-name`
restores the normal playlist name.

### Channel priorities and fair turns

You can give each channel a **high**, **medium** or **low** priority within a
topic. All channels start at medium, including topics you've already set up.
In the menu, choose **3. Edit a topic → Channel priorities**, select a channel,
and choose its priority. The menu uses channel names from your subscription
export when available.

The equivalent commands are:

```sh
yt-playlists channels QUERY
yt-playlists topics set news --channel-priority CHANNEL_ID=high
yt-playlists topics set news --channel-priority ANOTHER_CHANNEL_ID=low
yt-playlists topics show news
```

Replace the example IDs with actual channel IDs from `channels` or `topics show`.
You can repeat `--channel-priority` in one command and use it when adding a topic.
Set a channel back to `medium` to restore the default. It must already belong to
the topic, or be added with `--add-channel` in the same edit. Removing a channel
also removes its priority override.

There are now two rotations: auto mode gives each due playlist a bounded turn,
and each playlist takes weighted turns between its channels for new additions.
High channels get three slots per cycle, medium two, and low one. A cycle with
one of each goes high, medium, low, high, medium, high. Equal-priority channels
each get one slot before repeating. Empty slots are skipped, so a quiet channel
doesn't waste quota and a busy one can use the spare capacity. These are relative
opportunities when eligible videos are available, not guaranteed daily counts.

The next slot is saved with each successful insertion. If quota runs out after
one channel's video, the following command starts at the next saved channel slot
rather than beginning at the same channel again. Priorities are separate for
each topic, and each channel's newest eligible video is selected first. The
playlist itself stays in date order; priority changes selection order rather
than moving videos already present.

Priority changes make a topic due and start a new channel cycle. They don't
remove existing videos or change which videos pass the topic filters. Previews
don't consume slots, failed insertions don't consume them, and reconciliation
restores the slot if an insertion succeeded but its reply was lost. The same
ordering works for manual updates, normal auto, and update-all mode, in text and
LLM modes. It needs no extra YouTube or model requests.

### Update schedules

The frequency choices are `daily`, `every-2-days`, `every-4-days`, `weekly`,
`biweekly` (every two weeks), and `monthly`. Set your scheduling timezone once:

```sh
yt-playlists settings --timezone Europe/London
yt-playlists auto
yt-playlists auto --all
yt-playlists status
```

Auto mode visits enabled topics that are due, starting after the last topic it
attempted. It works in rounds instead of filling one playlist before moving on.
Each turn allows between one and five playlist changes, estimated from the quota
left and the number of unfinished playlists. Creation and renaming count as
changes too, so they don't bypass the allowance. The estimate is recalculated
on each turn; YouTube reads still consume quota, and the hard budget always wins.

New videos take weighted turns between channels, newest first within each
channel, alternating with removals when cleanup is
needed. Each playlist gets a turn before an unfinished one gets another batch.
Completed changes stay saved if quota runs out, and the incomplete topic stays
due. The next run starts after the last attempted topic. If everything fits, all
due topics finish during the same run. Discovery and LLM plans are reused between
rounds, so taking another turn doesn't repeat those requests. A new command
refreshes discovery and resumes from the item IDs already saved.

The same rounds apply to `auto --all` and the menu's update-all option. Manual
`update NAME` concentrates on that one topic. Previews show the complete proposed
changes without advancing the queue. Fair turns improve the spread of work, but
there may still be too little quota for every playlist, and some have no new
matching videos.

Frequencies count calendar days after a
successful update, with an automatically assigned calendar slot to spread the
work out. Ten topics set to every two days will be split five-and-five across
alternating days. New playlists can be filled straight away; their first
follow-up may come sooner than the full interval to establish that stagger.
After that, on-time updates keep their assigned slot. Monthly slots are days of
the month, clamped when a month is shorter. A late or forced update returns to
the next assigned slot, which can also be sooner than the full interval.
Existing topics get slots automatically without postponing overdue or unfinished
work. Changing a frequency assigns a slot for the new interval.

Use `yt-playlists auto --all` (or `yt-playlists --auto --all`) to update every
enabled topic regardless of its due date. The menu has **16. Update all enabled
topics (ignore frequency)**. This keeps the saved rotation and respects both
YouTube and model budgets. Each successful update records its completion time
and schedules its next update in its assigned slot, so an “all” run doesn't
bunch everything together again. A partial update stays due. Running “all” again
visits every enabled topic, including ones completed on the previous run; normal
`auto` only visits due topics. Add `--preview` to either command to see the plan
without updating playlists or schedules.

A manual `update NAME` runs even when that topic is disabled or not due.

The program only runs when you start it. Auto mode chooses what to update; it
doesn't install a background scheduler. Old videos leave playlists on an update,
not automatically at midnight. A hosted model also needs this script running
somewhere, whether that's your laptop or a server.

### Minimum video length

If you don't want short videos, set a minimum duration for new additions:

```sh
yt-playlists settings --minimum-video-length 181
```

This accepts videos at least 181 seconds long and skips shorter videos, including
ordinary short videos as well as Shorts. Videos with missing or unreadable duration
are also skipped while the filter is enabled. Use `0` to turn the filter off (the
default for a new installation), or choose **17. Minimum video length** in the menu.
The setting applies across all topics in both text and LLM modes, including
previews and “update all”. Filtering happens before model classification, so
skipped videos don't cost model calls.

Existing entries below the threshold are kept until their normal age limit;
changing this setting doesn't clean them out. The new minimum takes effect on
the next update without changing topics' schedules. Duration is requested with
the video metadata we already fetch, so it adds no extra API requests.

This is a duration filter, not an exact Shorts detector. YouTube currently allows
[Shorts up to three minutes](https://support.google.com/youtube/answer/15424877),
but its public video API doesn't expose a Shorts flag. The API's
[duration metadata](https://developers.google.com/youtube/data/reference/rest/v3/videos#contentDetails.duration)
can also differ slightly from playback length, so the threshold applies to the
reported duration.

### Subscription channel cache and fresh uploads

Updates cache the list of **which channels you follow** for 24 hours in the local
database. This avoids reading every subscription page again when you run several
updates. It does **not** cache yesterday's videos: each command still checks
channel uploads and video details for the latest news, politics and other uploads.

YouTube doesn't provide a changed-since subscription feed, so refreshing the list
means reading all its pages again. New subscriptions and unsubscribes can take
up to the cache lifetime to be noticed. After changing who you follow, force a
fresh list on an update:

```sh
yt-playlists auto --refresh-subscriptions
yt-playlists auto --all --refresh-subscriptions
yt-playlists update news --refresh-subscriptions
```

Or choose **18. Subscription channel cache** in the menu and refresh on the next
update. The same option lets you adjust the lifetime. From the command line:

```sh
yt-playlists settings --subscription-cache-hours 24
yt-playlists settings --subscription-cache-hours 0
yt-playlists settings --refresh-subscriptions
```

Zero disables reuse between commands. The maximum lifetime is 720 hours; the
normal 24-hour default is a compromise between channel-list freshness and reads.
The cache belongs to the verified YouTube account and is replaced only after a
complete successful refresh. If refreshing fails, the update stops rather than
quietly using expired membership data. The standalone `subscriptions --output`
command always fetches a fresh export for topic suggestions; that export is
separate from this managed-update cache.

### API budgets and local records

Playlist and playlist-item IDs are saved in `.local/playlists.sqlite3`. Keep this
file: it lets the app remove old items directly and avoid adding duplicates.
Don't delete it to reset a budget. Normal updates don't read the managed playlist
first. They do reconcile after uncertain writes and roughly once a week, and you
can request a reconciliation after editing a playlist yourself:

```sh
yt-playlists reconcile chess
yt-playlists settings --daily-budget 9500
yt-playlists auto --budget 2000
yt-playlists auto --preview
```

The default local YouTube budget is 9,500 units per Pacific calendar day. Its
usage counter is saved in the database and shared by every run using that state
file. Restarting the script doesn't reset it. Each new quota day starts at
midnight Pacific time, independently of the timezone used for topic schedules.
Reads and writes made by topic updates are counted before dispatch. Subscription
exports and the one-off test command aren't included in that local ledger, nor
are other apps using your Google project, so the count is an estimate of our
tracked usage rather than a reading of Google's remaining allowance.

`settings --daily-budget` changes the saved **daily ceiling**, not the usage
already recorded. For example, if today's count is 10,000 units, changing the
ceiling to 10,500 gives another 500 units of local headroom. It doesn't reset our
counter or increase Google's quota. You can check the count, run an update, and
raise the ceiling a little if the local limit stops you first:

```sh
yt-playlists settings --daily-budget 10000
yt-playlists status
yt-playlists auto
yt-playlists settings --daily-budget 10500
yt-playlists auto
```

The messages distinguish a **local budget pause** from **YouTube daily quota
exhaustion**. A local pause means our estimated allowance ran out; Google hasn't
necessarily rejected a request. If Google does report daily quota exhaustion,
that fact is saved too, and further updates stop until the next Pacific quota
day. Raising the local ceiling after that won't make the script keep sending
requests that Google has already refused.

`auto --budget 2000` sets a separate **per-run allowance**. That run stops when
either its own allowance or the remaining daily allowance is used up. Spending
from a per-run allowance also counts towards the daily total; it isn't an extra
allocation. `auto --daily-budget NUMBER` overrides the daily ceiling for that
command only, while `settings --daily-budget NUMBER` saves it for future runs.

The local ceiling also feeds the estimate used to size round-robin turns. Setting
it much higher than Google's actual quota will tend to produce the maximum five
changes per playlist turn, even when the real allowance is nearly gone. The
five-change cap and saved rotation still apply, but using a realistic ceiling
helps the app spread the remaining work into smaller turns. There isn't currently
a separate batch-planning quota setting.

Adding or removing a playlist item costs 50 units; list requests generally cost
one unit per page. Google's [quota documentation](https://developers.google.com/youtube/v3/determine_quota_cost)
explains the individual costs. Only changed items are written, but a large first
fill may still need several days. Exit code `0` means success, `1` means an error,
and `2` means a budget or quota pause. Unattended updates won't open a sign-in
window; renew authentication with `auth --write-access` if needed.

A preview doesn't write to YouTube or advance the update queue. It still makes
YouTube reads and, in LLM mode, may make paid model calls. Those results are cached
for the real update.

### Where the remaining quota goes

`yt-playlists status` includes a breakdown of calls and estimated units by API
operation. This starts counting when the feature is installed; older usage stays
in the daily total without a retrospective breakdown. Failed dispatched attempts
are counted too. This is our local accounting, not a reading of Google's remaining
allowance, and the separate subscription export/setup proof aren't included.

The script also caches each channel's uploads-playlist **address** for seven days.
It still reads the feed at that address on every update command, so this doesn't
delay new videos. An invalid cached address triggers a fresh channel lookup;
if it has changed, that topic stops safely and can be retried with the new address.
Reconciliation uses video publication dates already present in playlist items or
known local records, requesting extra video metadata only when a date is unknown.

The expensive part is the lifetime of a video in a rolling playlist: adding it
costs 50 units, and eventually removing it costs another 50. A video that appears
in two topic playlists incurs those changes twice. With a 10,000-unit allowance,
that means fewer than about 100 new playlist entries per day on a sustained
basis after allowing for reads and playlist edits. Initial filling can add more
because those entries haven't reached their removal dates yet.

Longer retention postpones removals and avoids repeated initial fills, but it
doesn't remove the eventual deletion cost. To make a big further reduction we'd
have to include fewer videos, allow expired items to remain, or rebuild playlists
with new links. None of those changes happens automatically.

### Optional LLM setup

Text mode works without a model and is the default. To use a model, create
`.local/llm.json`. The client is adapted from Iris and uses the same compatible
chat API, supporting both local servers and hosted services. The familiar
`base_url`, `model`, `api_key`, `allow_remote`, `timeout`, and `reply_encoding`
settings are supported. Audio settings aren't used here, and classification uses
its own prompt with no conversation history.

There are copyable examples in `examples/llm.local.json` and
`examples/llm.gemini.json`. Copy the appropriate file to `.local/llm.json` and
edit it for your server or account. For a local server, start with:

```json
{
  "base_url": "http://127.0.0.1:5001/v1",
  "model": "local-model",
  "max_calls": 20,
  "max_daily_calls": 100,
  "max_tokens": 4096
}
```

Use the address and model name from your server. The example address is Iris's
usual KoboldCPP address; other compatible servers may use a different port.

For Google's Gemini API, use this configuration, replacing `YOUR_MODEL_ID` with
a compatible model available to your account:

```json
{
  "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
  "model": "YOUR_MODEL_ID",
  "allow_remote": true,
  "api_key_env": "GEMINI_API_KEY",
  "max_calls": 20,
  "max_daily_calls": 100,
  "max_tokens": 4096
}
```

Set `GEMINI_API_KEY` in the environment before running the command. This is a
separate credential from YouTube sign-in. See Google's
[compatible API guide](https://ai.google.dev/gemini-api/docs/openai) for key setup
and supported models. Check the billing account and credit eligibility for the
service you choose; this configuration uses the Gemini API, not Vertex AI.
For other compatible hosted services, change the URL, model and key variable.
Hosted endpoints require HTTPS and `allow_remote: true`. Without `api_key_env`,
the client reads `IRIS_API_KEY`, falling back to `api_key` in the local JSON file.
Environment variables take priority over a stored key. Keep keys out of tracked
files; `.local/` is already ignored.

Then enable LLM mode:

```sh
yt-playlists settings --mode llm
yt-playlists suggest-topics --details
yt-playlists topics add chess --suggested
yt-playlists update chess --preview
```

That one setting enables the model for **both topic discovery and video
matching**, including in the interactive menu. A topic's `--description` helps
explain what you mean. Matching considers recent videos from the channels you've
selected for that topic, not every video on YouTube. Changing modes makes existing
topics due again. Switch back with `yt-playlists settings --mode text`.

You can choose another config with the global `--llm-config PATH` option before
the command. The menu also has a mode selector. Suggestions can be reviewed with
`--details` or exported with `--json`.

### Keeping model costs down

Successful decisions are cached in the local database. Video decisions are saved
individually, so a new upload doesn't cause the rest of a playlist to be classified
again. Topic discovery saves batches of up to 40 channels, and video matching
sends batches of up to 20 uncached videos. Titles are capped at 300 characters and
descriptions at 1,200 characters before being sent. The cache covers that supplied
metadata, the topic name and description, endpoint, model, output limit and prompt
version. Changing those inputs causes a fresh classification. Changing a model
behind the same server model name won't invalidate the cache automatically.

The defaults allow at most 20 model requests per command and 100 per UTC day,
shared across commands using the same database. Failed requests count too, because
the provider may have billed them. There are no automatic model retries. When a
limit is reached, run again later; completed batches remain cached. These are
request limits, not a guaranteed pound amount: model prices and token usage vary.
Use the provider's own spending controls as well.

Hosted models receive the titles, descriptions, IDs and topic definitions needed
for classification. They never receive your YouTube login token. If a model fails
or returns invalid decisions, the app reports the problem rather than quietly
switching to text mode. Discovery and classification must finish before that
topic's playlist changes begin; a model failure doesn't trigger removals. An uncertain decision
keeps an existing video until its age limit, and doesn't add a new one.

The reused Iris client and its GPL-3.0-only licence are included under
`src/youtube_playlist_builder/vendor/iris/`.

## Runtime diagnostics

The app writes a local log to `.local/runtime.log` automatically. If you choose
another `--state` path, the log goes alongside that database. Restart an already
open menu after updating the code to pick up logging changes.

The log records the command, model and provider host, request sizes and settings,
timing, HTTP status, and selected provider error fields. It doesn't record request
headers, prompts, video descriptions, successful model replies or Google sign-in
tokens. Known key values and common credential patterns are redacted from provider
errors. Provider messages can still contain context echoed by the server, so
review a log before sharing it. Logs are owner-readable/writable, rotate at about
1 MB, and keep three backups. The default location is ignored by Git.

For Gemini, a missing key is caught before contacting the model. Environment
variables belong to the terminal that sets them: if you open a new terminal,
set `GEMINI_API_KEY` there before starting `yt-playlists`. A running menu cannot
pick up environment changes made in another terminal. You can instead store
`api_key` in the ignored `.local/llm.json` file if you want it to persist.

## Reading the code

The [code walkthrough](docs/architecture.md) explains the update workflow, which
modules own each part, how quota is saved, and why the recovery records matter.

## Running the checks

```sh
python -m unittest discover -s tests -v
```

These checks use simulated API responses and don't need Google credentials.

