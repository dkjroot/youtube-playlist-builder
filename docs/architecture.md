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
