# spotify-dj

A small, dependency-light Spotify CLI that **plays music, builds discovery mixes, and
sequences them like a set** — written against the post-February-2026 Web API, where a
lot of the tutorials you'll find are now wrong.

No SDK. One dependency (`requests`). Your credentials stay yours — no hosted OAuth broker.

```bash
# playback
spotify_dj.py play "boom bap" --device Kitchen
spotify_dj.py devices | status | pause | next | prev | volume 40 | queue "Four Tet"

# a weekly discovery mix that never repeats itself
spotify_dj.py weekly-mix "Week of 28 Aug" --audience me   --limit 30
spotify_dj.py weekly-mix "Kids — 28 Aug"  --audience kids --limit 25

# a 6-hour clean set for an actual event, sequenced as an arc
spotify_dj.py party-set "Baptism" --hours 6 --exclude-artists "Artist A, Artist B"

# artists you don't already listen to, released recently
spotify_dj.py new-artists --since 2025 --per-genre 5
```

## Why this exists

Every Spotify skill/wrapper I tried was unusable on a headless Linux box: macOS-only
(Keychain), tied to another CLI, or proxying my OAuth tokens through a third-party
broker. And most of them still call endpoints Spotify removed in Feb 2026.

## Two separate Spotify API events — don't conflate them

**27 Nov 2024** removed the data this kind of tool was built on, for every app without a
pre-existing quota extension: `/recommendations`, `/audio-features`, `/audio-analysis`,
`/related-artists`, `/browse/featured-playlists`, and 30-second preview URLs.
*This* is when playlist sequencing on the public API died — not 2026.

**Feb 2026** was a rename-and-trim migration (enforced 11 Feb for new integrations,
9 Mar for existing ones):

| Removed / renamed | Use instead |
|---|---|
| `POST/PUT/GET/DELETE /playlists/{id}/tracks` | `/playlists/{id}/items` |
| `POST /users/{user_id}/playlists` | `POST /me/playlists` |
| library ops on `/me/tracks`, `/me/albums` … | `PUT\|DELETE /me/library` (takes URIs) |

It also stripped `popularity`, `available_markets`, `followers` and `external_ids` from
responses, cut `/search` `limit` from max 50 → **10** (default 20 → 5), started requiring
Premium on the registering account, and dropped dev-mode test users from 25 → 5.

### The trap that costs you an afternoon

**Spotify returns `403 Forbidden`, not `404`, for endpoints it removed.** A renamed path is
indistinguishable from a permissions error, so you go hunting through scopes. Two dialects
tell them apart:

- `{"message": "Insufficient client scope"}` → a real scope problem.
- `{"message": "Forbidden"}` (bare) → the endpoint is gone. Check the path.

The bare `Forbidden` arrives *before* body validation — an empty body or a malformed URI
returns 403 rather than 400, which is how you can prove it's the route and not your request.

**Extended Quota Mode is not a workaround.** It's gated at 250k monthly active users, so
for an individual it's closed. You don't need it for playback or playlist writes.

## Where this sits in the ecosystem

Being straight about it: **the playback CLI part is commoditised** — there are several
maintained Spotify CLIs and two popular MCP servers. **Search-based discovery is crowded**,
mostly with abandoned repos. The client libraries migrated quickly (spotipy 2.26.0 shipped
before the deadline); what rotted is the application layer above them, where most
audio-feature-dependent sequencers are dead — [playlistjockey](https://github.com/robalberse/playlistjockey)
says so in its own README.

Three things here I couldn't find prior art for on the public API:

1. **Metadata-only flow sequencing.** Genre blocks along an arc, chronological within a
   block, artist-adjacency avoidance. It is *weaker* than real BPM/energy sequencing — it
   is what's left when that data is gone, not a replacement for it.
2. **Anti-SEO junk filtering for genre search** — title-equals-genre, `type beat`, karaoke
   and soundalike uploads. This matters more since Feb 2026: with `limit` capped at 10, one
   junk result costs 10% of your candidate pool.
3. **Cross-mix de-duplication** so a weekly job doesn't rebuild the same playlist, plus the
   shared-family-account audience split.

If you run the Spotify desktop client, [sort-play](https://github.com/hoeci/sort-play)
(Spicetify) reaches real BPM/energy data from inside the client and is a strictly better
tool. This one is for **headless** use — a server, a cron job, no desktop client.

## Setup

1. Create an app at <https://developer.spotify.com/dashboard>, tick **Web API**, and set a
   redirect URI of `http://127.0.0.1:8888/callback`.
2. Put the credentials in `~/.spotify-dj.env`:
   ```
   SPOTIFY_CLIENT_ID=...
   SPOTIFY_CLIENT_SECRET=...
   ```
3. Authorise once — the browser will land on a dead `127.0.0.1` page, which is expected.
   Copy the whole URL from the address bar:
   ```bash
   ./spotify_dj.py auth-url          # open this, approve
   ./spotify_dj.py auth-exchange "http://127.0.0.1:8888/callback?code=..."
   ```
   Add the printed refresh token to the same file as `SPOTIFY_REFRESH_TOKEN=`.

Playback control requires **Spotify Premium**. Search and playlist building do not.

Resolution order is environment → `~/.spotify-dj.env` → optional secrets backend, so you
can wire it to a vault by dropping in a `hermes_secrets.py` and setting `HERMES_AGENT_DIR`.

## Discovery, without a recommendations API

`/recommendations` is gone, and `year:` / `genre:` search filters return **zero results** at
this tier — so era and style have to come from the search phrasing alone. `weekly-mix`:

- pulls taste seeds from your top artists and recent likes,
- searches a genre list (round-robin, so every genre is represented rather than the first
  two eating the whole limit),
- excludes your liked library, your top tracks, and **everything from previous mixes** —
  without that last one the searches are deterministic and you rebuild the same playlist
  every week,
- filters junk: tracks literally *titled* the genre (`"BOOM BAP"` for `boom bap`), plus
  `type beat` / karaoke / soundalike uploads that farm genre keywords.

## Sequencing: making it flow

Ordering a set properly wants tempo and energy from `/audio-features` — which is 403, with
`popularity` null too. What's left is the genre that found each track, its release date,
duration and artist. So flow is structural:

- **Genre blocks, not alternation.** Round-robin picks well but sequences terribly —
  Biggie into Luther Vandross into Burna Boy. Blocks are ordered along an arc:
  `classic soul → neo soul → rnb → jazz rap → 90s hip hop → boom bap → g funk → uk rap → afrobeats`
  (warm up → bridge → peak → finish).
- **Chronological within a block**, so it reads as a run through an era.
- **No same-artist back to back** — de-duplicated *inside* each block, because doing it
  globally swaps tracks across boundaries and fragments the arc.

Set your own arc with `FLOW_ARC`, or `--no-flow` to keep discovery order.

## The `--audience` split

One marker list (`KIDS_MARKERS`) drives both mixes: it **excludes** for `--audience me` and
**selects** for `--audience kids`, where explicit tracks are also dropped. This exists
because a shared family account makes `/me/top/*` useless raw — nursery rhymes and sleep
sounds crowd out the real signal.

Add specific act names to that list rather than broadening the markers: broad terms
wrongly caught a reggae artist and a guitarist.

## Building a set for an event

`party-set` targets **hours of audio**, not a track count, and inverts the discovery rules:
familiarity is a feature, so it does *not* exclude your library — people want songs they
know. Explicit tracks are dropped outright, and so are tracks over `--max-track-min`
(default 7), because worship medleys and DJ mixes stall a room. The default arc runs
arrival → celebration → build → lift → peak across soul, motown, lovers rock, roots reggae,
gospel, rnb, highlife, hiplife, afrobeats, azonto, amapiano, reggae and dancehall.

Genre coverage here leans Afro-Caribbean because that's what it was built for; the arc and
the genre list are both plain lists you can replace.

## Running it weekly

```bash
30 8 * * 1  /path/to/weekly-mix.sh
```

## Licence

MIT.
