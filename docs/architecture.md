# How the app fits together

The menu and the command line do the same work. The menu collects values and
calls `cli.main()` with ordinary command arguments, so fixes to validation,
scheduling or playlist updates apply to both interfaces.

## Following an update

1. `cli.py` loads the local settings, takes the state lock and checks whether
   there's anything to do. It obtains Google credentials through `auth.py`,
   creates the quota-counting `YouTube` transport, and enables `Classifier` if
   the saved mode calls for it.
2. `schedule.py` supplies a snapshot of the due topics in the saved rotation.
   All mode ignores due dates but still skips disabled topics. Stable calendar
   slots spread repeat updates out; a topic stays due if it doesn't finish.
3. `sync.py` checks the signed-in account against the owner saved in the database.
   It recovers unresolved writes or periodically reconciles a playlist before
   relying on its local item records.
4. The app obtains the account-scoped subscription list and reads selected channels'
   upload feeds. It collects recent video IDs across channels and requests their
   details in groups of up to 50.
5. Age, title and minimum-duration filters narrow the candidates. In LLM mode,
   `llm.py` classifies only uncached eligible videos. Existing below-minimum
   entries are retained until expiry. Invalid model output stops the update;
   it does not turn the desired playlist into an empty list.
6. The app compares the complete desired set with saved playlist items, then
   applies only additions, removals and necessary playlist title/privacy changes.
   Each returned item ID is saved immediately. Only a fully completed update
   records a new success time and next scheduled date.
7. Automatic updates suspend each topic after a bounded turn and move to the next
   unfinished topic. The allowance is `remaining_units // (50 * active_topics)`,
   clamped to one through five mutations. All writes count, including creation and
   renaming. Reads are still charged by the gateway and can exhaust the budget
   before the estimated allowance is used. A topic's generator retains the
   discovery/classification plan for later rounds without repeating those calls.
   Additions and removals alternate. `channel_order.py` chooses insertions using
   weighted channel slots, selecting the newest eligible video from that channel. New
   entries use saved publication dates to choose an insertion position without
   reordering existing items. Completion is recorded before yielding the final
   mutation, not deferred until another round. Failed topics leave this run's
   queue; quota exhaustion stops the whole run. Manual updates drain one generator
   directly, while previews walk each full plan without mutations.

## Channel turns inside topic turns

`channel_order.addition_turns()` is a pure planner over eligible new videos after
age/title/duration/model filtering. It groups videos by channel, sorts each group
newest-first, and walks a deterministic weighted cycle of selected channels.
High/medium/low weights are 3/2/1 per channel. Layering the slots visits every
channel once before giving higher priorities their extra slots. Channels without
eligible backlog are skipped, so no request is spent on an empty turn.

Each yielded video has a small rotation token: the cycle's hash and the next
slot index. The full cycle is reconstructed from selected channel IDs and their
per-topic priorities. A changed selection or priority invalidates the hash and
starts a new cycle; unchanged settings resume across process restarts. The topic
columns `channel_priorities` and `channel_rotation` are additive migrations with
empty defaults, so existing channels start at medium.

Sync writes the insertion's intended rotation token into the pending intent,
then records the returned item ID, new rotation and cleared intent in the same
SQLite transaction. Merely planning or previewing never changes the saved slot.
An uncertain insertion retains its token in pending state. Reconciliation grants
that turn only if the video is found remotely; unapplied writes leave the slot
unchanged. Cleanup doesn't consume insertion slots, and channel priorities don't
reorder existing playlist entries or alter the model cache key.

## Where quota goes

List calls generally cost one unit per page/request. Playlist creation, editing,
item insertion and item deletion each cost 50 units. See Google's
[quota table](https://developers.google.com/youtube/v3/determine_quota_cost) for
current costs. The app doesn't move every existing item into a new order on each
run, or clear and refill playlists: those writes would quickly use the allowance.

The discovery path makes several savings without carrying stale feeds into the
next run:

- Upload-playlist IDs supplied by channels.list are cached for seven days. They
  are addresses, not feed contents: uploads remain live. A cached address returning
  404 is looked up again; unchanged addresses are not blindly retried, and changed
  ones stop that topic before content changes so a later command rebuilds safely.
- Channel lookups and video metadata use batches of up to 50 IDs. Video batches
  span channels, so quiet channels don't each need their own metadata request.
- Publication dates in upload-feed entries let us skip metadata requests for
  already-expired videos. Missing dates are resolved rather than guessed.
- Upload pages are shared across topics within a run. A later topic with a longer
  age window extends pagination from the pages already fetched.
- Reconciliation uses publication dates returned in playlist items, falling back
  to saved dates or batched metadata only for previously unknown dates.
- Video details, including successful responses that omit unavailable IDs, are
  shared within a run. Reconciliation and discovery can use the same details.
- Subscription membership is cached for 24 hours by default and shared across
  topics. Explicit refresh and a configurable lifetime control when additions or
  removals of subscriptions are noticed. The ownership check still runs every
  command so a different Google account cannot use saved playlist IDs or cache.

For example, 40 channels with one recent upload each used to require 83 reads:
three setup reads, 40 feed reads and 40 video-detail reads. The batched path uses
44: the same setup and feeds, with one video-detail request. This is an example
from the fake-server tests, not a promise that every account will save that much.
Writes still dominate when a playlist needs a lot of additions or removals.

HTTP batching or smaller response fields can reduce transport overhead, but they
don't turn multiple API calls into one quota charge. The savings above avoid
requests or use the API's multi-ID reads instead.

## Cache lifetimes and failure boundaries

Upload-page snapshots and video details are shared only inside one `Sync`
instance. The next command checks them again, so new uploads, changed metadata
and previously unavailable videos remain visible. Only subscription membership
has a persistent cache: owner channel ID, completion timestamp and complete
channel list. Missing, expired, malformed, future-dated or foreign-account
snapshots trigger a full refresh. A failed refresh preserves the old cache on
disk but doesn't use it as stale fallback. The default lifetime is 24 hours;
zero disables reuse and explicit force-refresh bypasses it. YouTube has no
subscription delta endpoint, so each refresh still reads the whole list.

The separate upload_playlist_ids cache stores only channel IDs, verified API
upload-playlist addresses and lookup timestamps for seven days. It doesn't store
video contents or allow a failed feed lookup to masquerade as an empty feed.

LLM decisions are different: they persist in SQLite because the input metadata
and topic definition are part of their cache key. Endpoint, model, output limit
and prompt version are also included. Changes to the supplied inputs trigger a
new classification. The model client is adapted from Iris under `vendor/iris/`;
it handles transport, while `llm.py` handles application-specific validation,
limits and caching.

`state.py` keeps the durable topic definitions, playlist/item IDs, quota ledger
and pending-write records. There are two levels of protection:

- The file lock prevents two commands from updating the same database at once.
  It is held across network calls as well as local changes.
- SQLite transactions group the saved result of one remote write with clearing
  its pending intent. Intent is committed *before* sending the request. If a
  reply is lost, the next run reconciles instead of blindly inserting a duplicate.

All discovery pages must succeed before content-based removal begins. Quota
pauses preserve completed work. A partial update does not earn a new next-due
date, and the cursor gives other topics a turn on the following auto run.

## Supporting modules

`topics.py` validates local edits and supplies text-mode suggestions from the
subscription export. `subscriptions.py` produces that human-facing export;
updates use their own bounded, account-verified membership cache, so the human
export doesn't control its refresh lifetime. `videos.py` parses durations for admission.

`api.py` charges an aggregate Pacific-day quota ledger and endpoint breakdown in
one local transaction before dispatch. The api_usage table records calls and
estimated units per resource/method, including failed attempts. CLI status shows
these measurements, explicitly excluding uninstrumented historical requests.

`terminal.py` handles editable prompts, Escape cancellation and terminal-only
progress. Its heartbeat thread prints elapsed time; it never touches SQLite or
makes requests. `diagnostics.py` writes bounded private logs without request
bodies or authentication headers.

`playlists.py` is the separate one-video setup proof. Its JSON receipt and the
explicit subscription export predate the managed topic workflow and don't charge
the managed daily ledger. Leave quota headroom if you use those commands or other
apps against the same Google project.

## Checking changes

Run `python -m unittest discover -s tests -v`. Tests use fake YouTube services and
mock model transports, so they don't need credentials or spend quota. The sync
suite exercises partial writes, lost replies, account ownership, scheduling,
filters and idempotence. `test_quota_efficiency.py` checks actual request counts,
cross-channel batching and reuse across different age windows. Terminal tests
feed real key sequences into the line editor to check Escape and arrow editing.

## Local web management and capacity planning

`web.py` serves packaged `web_assets` on IPv4 loopback only. Page/state reads never
make remote calls. Topic mutations use the shared validation, `limits.prepare_limits`
and state lock. Background preview/update/discovery operations are allowlisted
child CLI processes with the launching paths/environment. Two reader threads drain
stdout/stderr to avoid pipe deadlocks; bounded job logs are polled by the UI.
Only one web job runs at a time, and the existing cross-process file lock still
protects it from terminal commands. Closing the server terminates its child;
uncertain writes retain the usual pending intent for recovery.

API requests require a per-launch token and an exact loopback Host; foreign
Origins are rejected. Only three fixed static assets are served. Credential
files, arbitrary commands and arbitrary filesystem paths aren't HTTP resources.
Personal metadata is rendered as text nodes. Dark CSS and plain functional copy
are defaults. This is a local management server, not a public deployment setup.

`limits.py` distinguishes unlimited (`max_videos IS NULL`) from a finite slot
allocation. Explicit total changes divide slots evenly; per-channel edits update
the sum. Zero channel slots exclude that channel. Adding/removing channels
preserves retained allocations and adjusts the total. Topic maximum ages can be
overridden per channel through `channel_max_age_days`, with inheritance when absent.

For a capped topic, `Sync.limited_videos` walks feed IDs lazily in 50-ID windows,
resolves metadata, applies ordinary eligibility and model decisions in bounded
batches, then stops when that channel's allocation is full. Thus long age windows
need not force full historical scans of busy channels. Unknown/unmatched videos
may still require more pages. All selected channels must complete discovery before
content changes. The desired set contains only each channel's newest eligible
slots. Cleanup runs before insertions for finite plans to avoid adding beyond
capacity during a quota pause; unlimited plans retain alternating cleanup/adds.

`forecast.py` is a pure local simulation over schedules, current items and an
explicit matching-upload-rate assumption. It models replacements, expirations,
creation and approximate reads over the selected period without consuming quota or updating
success dates. It is not a measured publishing model, and assumes earlier updates
complete. UI labels explain these assumptions. Item channel IDs are an additive
migration: new insertions save them, reconciliation uses returned owner IDs, and
ordinary updates attribute legacy entries using metadata already fetched.

## Redistributing schedules and budget carry-over

`rebalance.py` precomputes each enabled topic's forecast costs for alternative
calendar phases. Daily topics keep their dates; pending/partial updates preserve
their first due time. A bounded coordinate search over at least56days (or the
automatic visible horizon, whichever is longer) minimises peak daily cost, then
sum of squared daily costs. Equal-cost arrangements stay put. A final combined
forecast includes shared read costs before accepting the plan. Slot metadata and
next dates commit atomically under the state lock. Success timestamps, quota,
rotation cursors, disabled topics and remote playlists are not modified.

`slot_due` is the shared pure phase calculator used by both actual completion
scheduling and forecasting, including month-end clamping. The automatic graph
span covers two longest-frequency cycles plus the first scheduled date, at least
28days and bounded to366. Explicit spans use the same forecast engine.

Forecast demand and execution capacity are separate views. `budget_carryover`
queues the demand stream against the local ceiling, accounts for recorded usage
and a server-exhausted flag today, and funds reads/50-unit mutations in whole
operations. It returns requested/recorded/additional/carried amounts without
claiming exact completed video counts. This is deliberately an aggregate scenario:
real deferred updates can merge, expire, or require fresh discovery, so queued
work may be overstated. The UI exposes this limitation and the matching-upload
rate assumption. The web endpoint defaults to budget view; the planner's default
remains demand so scheduling optimisation cannot hide overload by clipping bars.


## Channel-first organisation

The separate `/organiser` page uses the same protected local state API, with
`/api/channel` providing up to five newest known videos for one channel and
`/api/assignment` changing a single membership. Membership writes acquire the
state lock, read the latest topic and call `edit_topic`, preserving unrelated
settings, existing capacity policy and pending-write protection. There is no
client-side full-topic overwrite during assignment. Paused topics count as
categories and new organiser topics default to paused.

`channel_videos` is a bounded per-channel display cache. Normal sync details
requests persist the five newest observed videos, including title, description
and publication date. This adds no YouTube requests and is never used as fresh
sync discovery. The detail endpoint merges the cache with attributed playlist
entries; incomplete history is labelled in the UI. Video thumbnails use validated
video IDs and the fixed YouTube image CDN, allowed explicitly by CSP. No remote
URLs from channel descriptions become executable markup or image sources.


The organiser now refreshes the selected channel's public Atom feed when absent
or at least24hours old, with a manual refresh button bypassing that cache. The
protected POST /api/channel-fetch route uses only a fixed YouTube feed URL, no
credentials, bounded2MB reads,15second network timeouts, no redirects or DTDs.
It validates channel/video IDs, stores five newest entries and a fetch timestamp,
and preserves prior history on failure. The browser backs off failed automatic
attempts for5minutes and never bulk-fetches subscriptions. No API fallback spends
quota silently. Videos appear before topic choices. Actual playlist sync remains
API-based because short feed history lacks reliable duration/availability and
cannot safely stand in for complete older discovery or reconciliation.


Optional `single_topic_channels` preference (default false) controls only organiser
assignment. On a checked assignment, all other memberships are removed in the same database
transaction as the chosen assignment, including when the chosen topic already contains the channel.
`edit_topic(commit=False)` is restricted to channel-only edits and participates in
the caller's transaction; validation/pending failures roll back both topics.
Existing overlaps are resolved only when a topic is explicitly chosen. Preferences persist via
normal settings endpoint/CLI (`--single-topic-channels on|off`).


`organiser-voice.js` owns optional browser SpeechRecognition sessions, using right
Control or right Command as push-to-talk. Release stops audio and opens a native
modal with an editable transcript and local partial/fuzzy topic matches. A click
calls the normal assignment route; recognition alone never writes. Sessions bind
to the selected channel, invalidate late callbacks on cancellation/typing/selection,
and abort on blur, hidden page, Escape or a 20-second listening limit. A bounded
final-result wait keeps typing usable even if the browser service stalls. Browser
speech may be remote; no audio/transcript is persisted or sent to the local API.
