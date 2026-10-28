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
4. Known expired items can be removed directly using their saved playlist-item
   IDs. The app then reads the current subscriptions and the selected channels'
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

## Where quota goes

List calls generally cost one unit per page/request. Playlist creation, editing,
item insertion and item deletion each cost 50 units. See Google's
[quota table](https://developers.google.com/youtube/v3/determine_quota_cost) for
current costs. The app doesn't move every existing item into a new order on each
run, or clear and refill playlists: those writes would quickly use the allowance.

The discovery path makes several savings without carrying stale feeds into the
next run:

- Channel lookups and video metadata use batches of up to 50 IDs. Video batches
  span channels, so quiet channels don't each need their own metadata request.
- Publication dates in upload-feed entries let us skip metadata requests for
  already-expired videos. Missing dates are resolved rather than guessed.
- Upload pages are shared across topics within a run. A later topic with a longer
  age window extends pagination from the pages already fetched.
- Video details, including successful responses that omit unavailable IDs, are
  shared within a run. Reconciliation and discovery can use the same details.
- The subscription list is fetched once per run. The ownership check is retained
  so a different Google account cannot accidentally use saved playlist IDs.

For example, 40 channels with one recent upload each used to require 83 reads:
three setup reads, 40 feed reads and 40 video-detail reads. The batched path uses
44: the same setup and feeds, with one video-detail request. This is an example
from the fake-server tests, not a promise that every account will save that much.
Writes still dominate when a playlist needs a lot of additions or removals.

HTTP batching or smaller response fields can reduce transport overhead, but they
don't turn multiple API calls into one quota charge. The savings above avoid
requests or use the API's multi-ID reads instead.

## Cache lifetimes and failure boundaries

YouTube subscriptions, upload-page snapshots and video details are shared only
inside one `Sync` instance. The next command checks them again. This keeps new
uploads, unsubscribes, changed metadata and previously unavailable videos from
being hidden by long-lived discovery caches.

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
updates independently refresh subscriptions so an old export doesn't dictate
what you're currently subscribed to. `videos.py` parses durations for admission.

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
