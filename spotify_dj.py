#!/usr/bin/env python3
"""setlisted — a deterministic Spotify CLI that still works after the 2026 API changes.

Playback, discovery mixes that don't repeat week to week, and long event sets sequenced
as an arc. One dependency (requests). Your credentials stay yours: resolution order is
environment -> ~/.spotify-dj.env -> an optional secrets backend, so nothing is written to
a config file and no third-party OAuth broker sits in the path.

Two API realities shape the whole design:
  * Nov 2024 removed /recommendations, /audio-features and /related-artists, so discovery
    is search-based and sequencing works from metadata alone.
  * Feb 2026 renamed /playlists/{id}/tracks -> /items and /users/{id}/playlists ->
    /me/playlists, capped /search limit at 10, and stripped `popularity` from responses.
    Removed endpoints answer 403, NOT 404 -- see the README before blaming your scopes.

Playback control requires Spotify Premium.
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import sys
import time
import urllib.parse

import requests

hermes_secrets = None
try:
    sys.path.insert(0, os.environ.get("HERMES_AGENT_DIR", "/opt/hermes-agent"))
    import hermes_secrets  # type: ignore  # noqa: E402
except Exception:
    hermes_secrets = None

SECRET_PATH = os.environ.get("SPOTIFY_SECRET_PATH", "/Spotify")
TOKEN_CACHE = os.environ.get("SPOTIFY_TOKEN_CACHE",
                             os.path.join(os.path.expanduser("~"), ".spotify-dj-token.json"))
ENV_FILE = os.environ.get("SPOTIFY_ENV_FILE",
                          os.path.join(os.path.expanduser("~"), ".spotify-dj.env"))
API = "https://api.spotify.com/v1"
ACCOUNTS = "https://accounts.spotify.com"
TIMEOUT = 20

SCOPES = " ".join([
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "playlist-modify-private",
    "playlist-modify-public",
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-top-read",
    "user-library-read",
    "user-read-private",
])


class DJError(RuntimeError):
    """Never carries a secret value."""


@functools.lru_cache(maxsize=1)
def _env_file() -> dict:
    out = {}
    try:
        with open(ENV_FILE) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def _cred(name: str, default=None) -> str:
    """environment -> .env file -> optional secrets backend -> default."""
    val = os.getenv(name) or _env_file().get(name)
    if val:
        return val
    if hermes_secrets is not None:
        try:
            return hermes_secrets.get(name, path=SECRET_PATH, default=default)
        except Exception:
            pass
    if default is not None:
        return default
    raise DJError(f"{name} is not set. Put it in the environment or {ENV_FILE} (see README).")


def _redirect_uri() -> str:
    return _cred("SPOTIFY_REDIRECT_URI", default="http://127.0.0.1:8888/callback")


# ---------------------------------------------------------------- auth

def auth_url() -> str:
    q = urllib.parse.urlencode({
        "client_id": _cred("SPOTIFY_CLIENT_ID"),
        "response_type": "code",
        "redirect_uri": _redirect_uri(),
        "scope": SCOPES,
        "show_dialog": "true",
    })
    return f"{ACCOUNTS}/authorize?{q}"


def auth_exchange(code: str) -> str:
    """Trade the one-time ?code= for a refresh token. Run once, by hand."""
    if "code=" in code:  # tolerate a pasted full redirect URL
        code = urllib.parse.parse_qs(urllib.parse.urlparse(code).query)["code"][0]
    r = requests.post(
        f"{ACCOUNTS}/api/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _redirect_uri(),
        },
        auth=(_cred("SPOTIFY_CLIENT_ID"), _cred("SPOTIFY_CLIENT_SECRET")),
        timeout=TIMEOUT,
    )
    if not r.ok:
        raise DJError(f"token exchange failed: HTTP {r.status_code} {r.text[:200]}")
    tok = r.json().get("refresh_token")
    if not tok:
        raise DJError("no refresh_token in response (was the code already used?)")
    return tok


def _access_token() -> str:
    # The cache MUST record the scopes the token was minted with. Without that,
    # re-consenting with new scopes leaves the old token being served for up to
    # an hour, and Spotify answers 403 "Insufficient client scope" — which looks
    # exactly like an API restriction. That cost real debugging time on
    # 2026-08-28. If the cached scopes no longer cover SCOPES, re-mint.
    try:
        with open(TOKEN_CACHE) as fh:
            c = json.load(fh)
        cached = set((c.get("scope") or "").split())
        if c.get("expires_at", 0) > time.time() + 60 and not (set(SCOPES.split()) - cached):
            return c["access_token"]
    except Exception:
        pass
    r = requests.post(
        f"{ACCOUNTS}/api/token",
        data={"grant_type": "refresh_token", "refresh_token": _cred("SPOTIFY_REFRESH_TOKEN")},
        auth=(_cred("SPOTIFY_CLIENT_ID"), _cred("SPOTIFY_CLIENT_SECRET")),
        timeout=TIMEOUT,
    )
    if not r.ok:
        raise DJError(f"refresh failed: HTTP {r.status_code} — re-run auth-url/auth-exchange")
    d = r.json()
    tok = d["access_token"]
    try:
        old = os.umask(0o077)
        with open(TOKEN_CACHE, "w") as fh:
            json.dump({"access_token": tok,
                       "scope": d.get("scope", ""),
                       "expires_at": time.time() + d.get("expires_in", 3600)}, fh)
        os.umask(old)
    except Exception:
        pass
    return tok


def _call(method: str, path: str, **kw):
    r = requests.request(
        method, f"{API}{path}",
        headers={"Authorization": f"Bearer {_access_token()}"},
        timeout=TIMEOUT, **kw,
    )
    if r.status_code == 204:
        return {}
    if r.status_code == 403:
        raise DJError(
            f"403 Forbidden on {path}. NOTE: Spotify returns 403 (not 404) for "
            "endpoints REMOVED in the Feb 2026 rename — check the path first: "
            "/playlists/*/tracks -> /items, /users/{id}/playlists -> /me/playlists. "
            "Otherwise: the account is not Premium (playback), or the endpoint is "
            "gone at Dev Mode tier (/artists/*/top-tracks, /recommendations)."
        )
    if r.status_code == 404:
        raise DJError("404 — no active device. Open Spotify somewhere, then retry")
    if not r.ok:
        raise DJError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json() if r.text else {}


# ---------------------------------------------------------------- helpers

def _pick_device(name: str | None):
    devs = _call("GET", "/me/player/devices").get("devices", [])
    if not devs:
        raise DJError("no Spotify Connect devices visible — open Spotify on a speaker/phone")
    if not name:
        act = [d for d in devs if d.get("is_active")]
        return (act or devs)[0]
    for d in devs:
        if name.lower() in d["name"].lower():
            return d
    raise DJError(f"no device matching {name!r}. Seen: " + ", ".join(d["name"] for d in devs))


PAGE = 10  # hard cap for apps in Development Mode; asking for 11+ is a 400 "Invalid limit"


def _search_tracks(q: str, limit: int = 10):
    """Paginate in PAGE-sized pages — the API rejects limit>10 for this app."""
    out, seen, offset = [], set(), 0
    while len(out) < limit and offset < 200:
        d = _call("GET", "/search",
                  params={"q": q, "type": "track", "limit": PAGE, "offset": offset})
        items = d.get("tracks", {}).get("items", [])
        if not items:
            break
        for t in items:
            if t["uri"] not in seen:
                seen.add(t["uri"])
                out.append(t)
        offset += PAGE
    return out[:limit]


def _fmt(t) -> str:
    return f"{t['name']} — {', '.join(a['name'] for a in t['artists'])}"


# ---------------------------------------------------------------- commands

def cmd_devices(a):
    for d in _call("GET", "/me/player/devices").get("devices", []):
        print(f"{'*' if d.get('is_active') else ' '} {d['name']:<28} {d['type']:<12} vol={d.get('volume_percent')}")


def cmd_status(a):
    d = _call("GET", "/me/player")
    if not d:
        print("nothing playing")
        return
    t = d.get("item")
    print(f"{'playing' if d.get('is_playing') else 'paused'}: {_fmt(t) if t else '?'}")
    print(f"device: {d.get('device', {}).get('name')}")


def cmd_play(a):
    dev = _pick_device(a.device)
    body = {}
    if a.query:
        hits = _search_tracks(a.query, limit=a.limit)
        if not hits:
            raise DJError(f"nothing found for {a.query!r}")
        body["uris"] = [t["uri"] for t in hits]
        print(f"queued {len(hits)} track(s), starting: {_fmt(hits[0])}")
    _call("PUT", "/me/player/play", params={"device_id": dev["id"]}, json=body or None)
    print(f"-> {dev['name']}")


def cmd_pause(a):
    _call("PUT", "/me/player/pause")
    print("paused")


def cmd_next(a):
    _call("POST", "/me/player/next")
    print("skipped")


def cmd_prev(a):
    _call("POST", "/me/player/previous")
    print("back")


def cmd_volume(a):
    _call("PUT", "/me/player/volume", params={"volume_percent": max(0, min(100, a.percent))})
    print(f"volume {a.percent}%")


def cmd_search(a):
    for t in _search_tracks(a.query, limit=a.limit):
        print(f"{_fmt(t)}   [{t['uri']}]")


def cmd_queue(a):
    hits = _search_tracks(a.query, limit=1)
    if not hits:
        raise DJError(f"nothing found for {a.query!r}")
    _call("POST", "/me/player/queue", params={"uri": hits[0]["uri"]})
    print(f"queued: {_fmt(hits[0])}")


def cmd_top(a):
    d = _call("GET", "/me/top/tracks", params={"limit": a.limit, "time_range": a.range})
    for i, t in enumerate(d.get("items", []), 1):
        print(f"{i:>2}. {_fmt(t)}")


def cmd_make_playlist(a):
    """Deterministic build: seed -> matching tracks + that artist's top tracks."""
    seed_tracks, seen = [], set()
    for t in _search_tracks(a.seed, limit=min(a.limit, 50)):
        if t["uri"] not in seen:
            seen.add(t["uri"])
            seed_tracks.append(t)
    # Best-effort enrichment. /artists/*/top-tracks 403s for apps created after
    # Nov 2024, so a failure here must not sink the playlist.
    try:
        art = _call("GET", "/search", params={"q": a.seed, "type": "artist", "limit": 1})
        items = art.get("artists", {}).get("items", [])
        if items:
            top = _call("GET", f"/artists/{items[0]['id']}/top-tracks",
                        params={"market": a.market})
            for t in top.get("tracks", []):
                if t["uri"] not in seen and len(seed_tracks) < a.limit:
                    seen.add(t["uri"])
                    seed_tracks.append(t)
    except DJError:
        pass  # search-only playlist; still a valid mix
    seed_tracks = seed_tracks[: a.limit]
    if not seed_tracks:
        raise DJError(f"no tracks found for seed {a.seed!r}")
    # NB: /users/{id}/playlists 403s for this app; /me/playlists is the one
    # that works. Don't "fix" this back to the documented user-scoped path.
    pl = _call("POST", "/me/playlists",
               json={"name": a.name, "public": False,
                     "description": f"Hermes DJ — seed: {a.seed}"})
    # Feb 2026: /playlists/{id}/tracks was REMOVED and renamed to /items.
    # Spotify returns 403 (not 404) for removed endpoints, which reads exactly
    # like a permissions problem and is not. Same rename family as
    # POST /users/{id}/playlists -> POST /me/playlists above.
    _call("POST", f"/playlists/{pl['id']}/items",
          json={"uris": [t["uri"] for t in seed_tracks]})
    print(f"created {a.name!r} with {len(seed_tracks)} tracks")
    print(pl.get("external_urls", {}).get("spotify", ""))



# ---------------------------------------------------------------- discovery
# This account's listening history is heavily mixed with children's and sleep
# content (CoComelon, Tumble Tots, nursery rhymes, "Jazz for Babies", ocean
# sounds). Top-artists/top-tracks are therefore NOT usable raw as a taste
# signal -- filter first or the "discovery" mix comes back as nursery rhymes.
KIDS_DEFAULT_SENTINEL = "__default__"

# Filler that pollutes genre searches: SEO "type beats", karaoke and soundalike
# uploads. These are not the genre, they are content farmed against its name.
JUNK_MARKERS = (
    "type beat", "karaoke", "made famous by", "tribute to", "in the style of",
    "originally performed", "cover version", "backing track", "instrumental version",
)

KIDS_MARKERS = (
    "nursery", "lullab", "baby", "babies", "toddler", "cocomelon", "tumble tots",
    "super simple", "kids", "kidz", "children", "rhymes", "looloo", "sleep",
    "ocean sounds", "white noise", "relaxing", "meditation", "minidisco",
    "playtime", "sing along", "sing-along", "wheels on the bus", "frozen",
    "peppa", "bluey", "disney junior", "twinkle", "nap ", "bedtime",
    # Specific acts seen in THIS library's top-artists that the generic
    # markers above don't catch. Add to this list, don't broaden the markers --
    # broad terms wrongly excluded real artists (Sanchez is reggae, Steve
    # Dawson is a real guitarist, "Birdland" is jazz).
    "vicky arlidge", "ms raina", "martin and rose", "minidisco",
    "tumble tots", "looloo", "loo loo",
)


def _norm_title(name: str) -> str:
    """Strip版 suffixes so title-collision checks actually fire.

    "Lover's Rock - Remastered" by The Clash slipped into a lovers-rock block
    because the apostrophe and the " - Remastered" suffix meant it never
    matched the query string.
    """
    n = name.lower()
    for cut in (" - ", " (feat", " (with", " (remaster", " (live", " (radio"):
        i = n.find(cut)
        if i > 0:
            n = n[:i]
    return "".join(c for c in n if c.isalnum() or c == " ").strip()


def _is_kiddie(*parts) -> bool:
    blob = " ".join(p for p in parts if p).lower()
    return any(m in blob for m in KIDS_MARKERS)


def _liked_uris(cap: int = 600) -> set:
    """Everything already in the library — the exclusion set for discovery."""
    out, offset = set(), 0
    while offset < cap:
        d = _call("GET", "/me/tracks", params={"limit": 50, "offset": offset})
        items = d.get("items", [])
        if not items:
            break
        for it in items:
            tr = it.get("track") or {}
            if tr.get("uri"):
                out.add(tr["uri"])
        offset += 50
    return out


def _taste_seeds(limit: int = 12) -> list:
    """Real artists this listener actually likes, kids' content removed."""
    seeds, seen = [], set()
    for rng in ("short_term", "medium_term"):
        try:
            d = _call("GET", "/me/top/artists", params={"limit": 30, "time_range": rng})
        except DJError:
            continue
        for art in d.get("items", []):
            n = art.get("name", "")
            if n and n.lower() not in seen and not _is_kiddie(n):
                seen.add(n.lower())
                seeds.append(n)
    for it in _call("GET", "/me/tracks", params={"limit": 50}).get("items", []):
        tr = it.get("track") or {}
        for art in tr.get("artists", []):
            n = art.get("name", "")
            if n and n.lower() not in seen and not _is_kiddie(n, tr.get("name")):
                seen.add(n.lower())
                seeds.append(n)
    return seeds[:limit]


def _previous_mix_uris(prefix: str = "Hermes DJ", cap: int = 25) -> set:
    """Tracks already served in earlier Hermes DJ mixes.

    Without this the weekly job is deterministic and regenerates the SAME
    playlist every Monday — the searches don't change and the library
    exclusion only grows when tracks are liked. Caught 2026-08-28 by running
    the job twice and getting identical output.
    """
    out = set()
    try:
        pls = _call("GET", "/me/playlists", params={"limit": 50}).get("items", [])
    except DJError:
        return out
    mine = [p for p in pls if (p.get("name") or "").startswith(prefix)][:cap]
    for p in mine:
        try:
            d = _call("GET", f"/playlists/{p['id']}/items", params={"limit": 100})
        except DJError:
            continue
        for it in d.get("items", []):
            tr = it.get("item") or it.get("track") or {}
            if tr.get("uri"):
                out.add(tr["uri"])
    return out



# ---------------------------------------------------------------- sequencing
# Ordering a set properly would use tempo/energy/key from /audio-features --
# that endpoint is 403 at this tier, and `popularity` comes back None too. All
# that's left is the genre that found the track, its release date, duration and
# artist. So flow is built structurally: play genres in BLOCKS along an arc
# instead of alternating every track (round-robin picks well but sequences
# terribly -- Biggie into Luther Vandross into Burna Boy).
# A DEFAULT arc, not a required one. _sequence() is genre-agnostic: it orders
# blocks by whatever order the caller supplies, so the arc is just "the order
# you listed your genres in". This list is only the fallback for weekly-mix.
FLOW_ARC = [
    "classic soul", "neo soul", "rnb", "jazz rap",
    "90s hip hop", "boom bap", "g funk", "uk rap", "afrobeats",
]


def _arc_rank(genre: str, order: list) -> int:
    """Position of a genre in the supplied arc; unknown genres sort to the end."""
    g = (genre or "").lower()
    for i, name in enumerate(order):
        n = name.lower()
        if n == g or n in g or g in n:
            return i
    return len(order)


def _sequence(tracks: list, order: list | None = None) -> list:
    """Sequence tracks into blocks along an arc.

    Deliberately knows nothing about music genres. Each track carries a `_genre`
    tag (any string -- genre, mood, decade, energy label, whatever the caller
    used to find it). Blocks are emitted in `order`, so the caller controls the
    arc simply by the order it lists its tags in. Falls back to FLOW_ARC.

    Three rules, all derivable from metadata alone -- there is no tempo, energy
    or key data on the public API since Nov 2024:
      1. group into blocks, don't alternate  (alternating every track is a
         genre shuffle, not a set)
      2. chronological within a block         (reads as a run through an era)
      3. no same-artist back to back          (within the block, so the swap
         cannot fragment the arc)

    Where 2 and 3 conflict, 3 wins -- see _dedupe_artists.
    """
    order = order or FLOW_ARC
    blocks: dict = {}
    for t in tracks:
        blocks.setdefault(t.get("_genre", ""), []).append(t)

    out = []
    for g in sorted(blocks, key=lambda x: _arc_rank(x, order)):
        blk = blocks[g]
        # Chronological within a block reads as a deliberate run through the era
        # rather than a shuffle.
        blk.sort(key=lambda t: (t.get("album", {}) or {}).get("release_date", "") or "")
        out.extend(_dedupe_artists(blk))

    return out


def _dedupe_artists(blk: list) -> list:
    """Push same-artist runs apart WITHIN one block.

    Doing this globally swapped tracks ACROSS block boundaries and fragmented
    the arc into neo soul -> rnb -> neo soul, undoing the grouping it exists to
    protect. Keep it inside the block.

    Precedence: separating the artist beats strict chronology. Hearing the same
    voice twice in a row is the thing a listener notices; a track landing a year
    out of sequence is not. A forward swap is tried first because it disturbs
    the chronology least -- but at the TAIL of a block there is nothing ahead to
    swap with, so the track is moved BACKWARDS instead. Without that fallback a
    block ending in two tracks by one artist kept them adjacent.

    Some blocks cannot be fixed at all (three tracks, one artist). Best effort,
    and it says so rather than pretending.
    """
    def name(t):
        return t["artists"][0]["name"]

    for i in range(1, len(blk)):
        if name(blk[i]) != name(blk[i - 1]):
            continue
        for j in range(i + 1, len(blk)):           # forward: cheapest fix
            if name(blk[j]) != name(blk[i - 1]):
                blk[i], blk[j] = blk[j], blk[i]
                break
        else:                                       # tail of the block
            # Nothing ahead to swap with, so move the EARLIER of the pair back,
            # to the LATEST position that separates them. Latest, not earliest:
            # it displaces one track by the smallest distance that works
            # instead of flinging it to the front of the block.
            who = name(blk[i])
            for p in range(i - 2, -1, -1):
                if name(blk[p]) != who and (p == 0 or name(blk[p - 1]) != who):
                    blk.insert(p, blk.pop(i - 1))
                    break
    return blk


# ---------------------------------------------------------------- any playlist
# Everything above builds a playlist AND sequences it. The interesting half is
# the sequencing, and it works on a playlist this tool didn't build: give it
# something you already have and it re-orders it into blocks along an arc.
#
# The only metadata the public API still gives us since Nov 2024 is release
# date and the artist's genre list. That is enough for a defensible arc and
# nothing more -- no tempo, no key, no energy. Don't promise beatmatching.

def _resolve_playlist(ref: str) -> dict:
    """Accept a playlist id, an open.spotify.com URL, a spotify: URI, or a name."""
    m = re.search(r"playlist[:/]([A-Za-z0-9]{22})", ref or "")
    pid = m.group(1) if m else (ref if re.fullmatch(r"[A-Za-z0-9]{22}", ref or "") else None)
    if pid:
        return _call("GET", f"/playlists/{pid}")

    # Name match against the user's own playlists. /me/playlists is the working
    # path; /users/{id}/playlists is one of the Feb 2026 removals.
    offset, hits = 0, []
    while offset < 500:
        d = _call("GET", "/me/playlists", params={"limit": 50, "offset": offset})
        items = d.get("items") or []
        if not items:
            break
        hits += [p for p in items if p and ref.lower() in (p.get("name") or "").lower()]
        offset += 50
    if not hits:
        raise DJError(f"no playlist of yours matches {ref!r}")
    if len(hits) > 1:
        names = ", ".join(repr(p["name"]) for p in hits[:6])
        raise DJError(f"{len(hits)} playlists match {ref!r}: {names} — be more specific")
    return _call("GET", f"/playlists/{hits[0]['id']}")


def _playlist_tracks(pid: str) -> list:
    """Page through a playlist. NB /items, not /tracks (Feb 2026 rename)."""
    out, offset = [], 0
    while True:
        d = _call("GET", f"/playlists/{pid}/items",
                  params={"limit": 50, "offset": offset,
                          "additional_types": "track"})
        items = d.get("items") or []
        if not items:
            break
        for it in items:
            t = (it or {}).get("track") or {}
            # Local files and podcast episodes have no uri we can re-add.
            if t.get("type") == "track" and t.get("uri", "").startswith("spotify:track:"):
                out.append(t)
        offset += 50
        if not d.get("next"):
            break
    return out


def _artist_genre_map(tracks: list) -> dict:
    """artist id -> first genre string. Batched 50 at a time (API max)."""
    ids = []
    for t in tracks:
        a = (t.get("artists") or [{}])[0]
        if a.get("id") and a["id"] not in ids:
            ids.append(a["id"])
    out = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        try:
            d = _call("GET", "/artists", params={"ids": ",".join(chunk)})
        except DJError:
            continue          # a batch failing must not sink the sequence
        for art in d.get("artists") or []:
            if art:
                gs = art.get("genres") or []
                out[art["id"]] = gs[0] if gs else ""
    return out


def _decade(t: dict) -> str:
    d = ((t.get("album") or {}).get("release_date") or "")[:4]
    return f"{d[:3]}0s" if d.isdigit() else ""


def tag_tracks(tracks: list, mode: str = "artist-genre", gmap: dict | None = None) -> list:
    """Attach the `_genre` block tag _sequence() groups on.

    The tag is just a string. Genre is the default because it is what the API
    gives us, but decade works on exactly the same machinery -- which is the
    point of keeping _sequence() tag-agnostic.
    """
    gmap = gmap or {}
    for t in tracks:
        if mode == "decade":
            t["_genre"] = _decade(t) or "unknown"
        else:
            aid = ((t.get("artists") or [{}])[0]).get("id")
            t["_genre"] = gmap.get(aid) or "unknown"
    return tracks


def auto_arc(tracks: list) -> list:
    """Derive an arc when the caller didn't supply one.

    Blocks are ordered by median release year, oldest first, so the set reads
    as a run forward through time. It is a real, explainable rule rather than a
    hidden taste model -- and `--order` overrides it whenever you disagree.
    Ties break on block size (bigger blocks first) so the set opens with weight.
    """
    stats: dict = {}
    for t in tracks:
        y = ((t.get("album") or {}).get("release_date") or "")[:4]
        stats.setdefault(t.get("_genre", ""), []).append(int(y) if y.isdigit() else 0)
    def key(g):
        yrs = sorted(v for v in stats[g] if v)
        med = yrs[len(yrs) // 2] if yrs else 9999
        return (med, -len(stats[g]))
    return sorted(stats, key=key)


def cmd_sequence(a):
    pl = _resolve_playlist(a.playlist)
    tracks = _playlist_tracks(pl["id"])
    if not tracks:
        raise DJError(f"{pl['name']!r} has no playable tracks")

    gmap = _artist_genre_map(tracks) if a.tag_by == "artist-genre" else {}
    tag_tracks(tracks, mode=a.tag_by, gmap=gmap)

    order = [g.strip() for g in a.order.split(",") if g.strip()] if a.order else auto_arc(tracks)
    picked = _sequence(tracks, order=order)

    print(f"{pl['name']!r} — {len(tracks)} tracks, arc: " + " -> ".join(order))
    if a.dry_run or a.verbose:
        last = None
        for i, t in enumerate(picked, 1):
            if t["_genre"] != last:
                last = t["_genre"]
                print(f"\n  [{last}]")
            print(f"   {i:>3}. {_fmt(t)}")
    if a.dry_run:
        print("\ndry run — nothing written")
        return

    uris = [t["uri"] for t in picked]
    if a.into:
        dest = _call("POST", "/me/playlists",
                     json={"name": a.into, "public": False,
                           "description": f"sequenced from {pl['name']}"})
        target = dest["id"]
    else:
        target = pl["id"]
        # PUT replaces the whole list in one shot, so the playlist is never
        # left half-empty the way delete-then-add would leave it.
        _call("PUT", f"/playlists/{target}/items", json={"uris": uris[:100]})
        uris = uris[100:]
    for i in range(0, len(uris), 100):
        _call("POST", f"/playlists/{target}/items", json={"uris": uris[i:i + 100]})

    where = a.into or pl["name"]
    print(f"\nsequenced {len(picked)} tracks into {where!r}")


def cmd_weekly_mix(a):
    """New-music mix from stated genres + real (de-kiddified) taste seeds.

    /recommendations is GONE (404) and `year:`/`genre:` search filters return 0
    results at this tier, so discovery is built from plain search over genre
    terms and seed-artist names, minus everything already in the library.
    """
    kids = a.audience == "kids"
    # The same kids/adult split serves both mixes: for "me" the markers are an
    # EXCLUDE list, for "kids" the search terms do the selecting and we drop
    # anything Spotify flags explicit. Two playlists, one filter, no overlap.
    if kids and a.genres == KIDS_DEFAULT_SENTINEL:
        genres = ["nursery rhymes", "kids songs", "childrens music",
                  "sing along songs", "toddler songs"]
    elif a.genres == KIDS_DEFAULT_SENTINEL:
        # Weighted to stated taste: 90s hip hop / boom bap / jazz rap lead.
        # Deliberately NOT "east coast hip hop" or "90s rap classics" — both
        # returned modern Jay-Z/Drake rather than the era.
        genres = ["90s hip hop", "boom bap", "jazz rap", "g funk",
                  "rnb", "neo soul", "afrobeats", "classic soul", "uk rap"]
    else:
        genres = [g.strip() for g in a.genres.split(",") if g.strip()]
    seeds = [] if (a.no_taste or kids) else _taste_seeds()
    if seeds:
        print("taste seeds:", ", ".join(seeds))
    known = _liked_uris() if not a.allow_known else set()
    top_uris = {t["uri"] for t in _call("GET", "/me/top/tracks",
                                        params={"limit": 50}).get("items", [])}
    known |= top_uris
    if not a.allow_repeats:
        prev = _previous_mix_uris()
        known |= prev
        if prev:
            print(f"excluding {len(prev)} tracks from earlier Hermes DJ mixes")

    picked, seen = [], set()

    def _ok(t):
        uri = t["uri"]
        if uri in known or uri in seen:
            return False
        names = " ".join(ar["name"] for ar in t["artists"])
        if kids:
            return not t.get("explicit")   # never explicit in the children's mix
        return not _is_kiddie(names, t["name"])

    # Round-robin across the genres so every one is represented. Filling
    # sequentially meant the first 2-3 genres consumed the whole limit and the
    # rest never appeared at all.
    def _title_noise(track_name: str, query: str) -> bool:
        """Searching "boom bap" returns tracks literally TITLED "Boom Bap".

        Those are name collisions, not examples of the genre (Doechii's
        "BOOM BAP", Silica Gel's "NEO SOUL"). Drop a hit whose title is
        basically just the query.
        """
        n = _norm_title(track_name)
        q = "".join(c for c in query.lower() if c.isalnum() or c == " ").strip()
        if any(j in track_name.lower() for j in JUNK_MARKERS):
            return True
        # "Classic" matched by the query "classic soul" — the title is a strict
        # prefix of the query, which is the same collision in reverse.
        if q.startswith(n + " ") and len(q) - len(n) < 9:
            return True
        return n == q or (n.startswith(q + " ") and len(n) - len(q) < 6)

    pools = []
    for g in genres:
        try:
            pool = []
            for t in _search_tracks(g, limit=14):
                if _ok(t) and not _title_noise(t["name"], g):
                    t["_genre"] = g
                    pool.append(t)
            pools.append(pool)
        except DJError:
            pools.append([])
    depth = 0
    while len(picked) < a.limit and any(len(pl) > depth for pl in pools):
        for pl in pools:
            if len(picked) >= a.limit:
                break
            if len(pl) > depth and _ok(pl[depth]):
                seen.add(pl[depth]["uri"])
                picked.append(pl[depth])
        depth += 1

    # Top up from seed-artist and modifier queries if the genres ran dry.
    if len(picked) < a.limit:
        extra = [f"{g} {sd}" for sd in seeds[:6] for g in genres[:3]]
        extra += [f"{g} {y}" for g in genres for y in ("essentials", "classics", "mix")]
        for q in extra:
            if len(picked) >= a.limit:
                break
            for t in _search_tracks(q, limit=10):
                if len(picked) >= a.limit:
                    break
                if _ok(t):
                    t["_genre"] = q.split()[0] if q else ""
                    seen.add(t["uri"])
                    picked.append(t)

    if not picked:
        raise DJError("no new tracks found — try different --genres")

    if not a.no_flow:
        picked = _sequence(picked, order=genres)

    pl = _call("POST", "/me/playlists",
               json={"name": a.name, "public": False,
                     "description": ("Hermes DJ weekly — kids mix, explicit filtered"
                                     if kids else
                                     f"Hermes DJ weekly — {', '.join(genres)} — new to you")})
    _call("POST", f"/playlists/{pl['id']}/items", json={"uris": [t["uri"] for t in picked]})
    print(f"created {a.name!r} with {len(picked)} tracks "
          + ("(kids, explicit filtered)" if kids else "(all new to your library)"))
    for t in picked[:10]:
        print("   -", _fmt(t))
    print(pl.get("external_urls", {}).get("spotify", ""))



# ---------------------------------------------------------------- party set
# A party set is NOT a discovery mix and the rules invert:
#   * familiarity is a FEATURE -- do not exclude the user's library or previous
#     mixes; people want to hear songs they know.
#   * clean is non-negotiable (family event), so explicit is dropped outright.
#   * length is measured in HOURS of audio, not track count.
# Arc runs arrival -> celebration -> build -> lift -> peak.
PARTY_ARC = [
    "soul classics", "motown", "lovers rock", "roots reggae",   # arrival, mellow
    "gospel celebration", "afro gospel",                        # celebration moment
    "rnb classics", "highlife", "hiplife",                      # build
    "afrobeats", "azonto", "amapiano",                          # lift
    "reggae classics", "dancehall classics", "classic dancehall",  # peak, dancing
]


def cmd_party_set(a):
    genres = ([g.strip() for g in a.genres.split(",") if g.strip()]
              if a.genres != KIDS_DEFAULT_SENTINEL else list(PARTY_ARC))
    target_ms = int(a.hours * 3600 * 1000)
    per_block = target_ms / max(len(genres), 1)

    seen, blocks, total = set(), {}, 0
    for g in genres:
        got_ms, keep = 0, []
        for t in _search_tracks(g, limit=a.depth):
            if got_ms >= per_block:
                break
            uri = t["uri"]
            names = " ".join(ar["name"] for ar in t["artists"])
            if uri in seen or t.get("explicit"):
                continue          # explicit never enters a family set
            if _is_kiddie(names, t["name"]):
                continue
            dur = t.get("duration_ms", 0)
            # Long worship medleys and DJ mixes stall a party. The gospel
            # searches returned ~10-minute tracks (5 of them filled 53 min).
            if dur > a.max_track_min * 60 * 1000 or dur < 90 * 1000:
                continue
            # Don't blow the block budget with one last long track.
            if got_ms and got_ms + dur > per_block * 1.15:
                continue
            n = _norm_title(t["name"])
            q = "".join(c for c in g.lower() if c.isalnum() or c == " ").strip()
            if any(j in t["name"].lower() for j in JUNK_MARKERS) or n == q:
                continue
            if a.exclude_artists and any(
                    x.strip().lower() in names.lower()
                    for x in a.exclude_artists.split(",") if x.strip()):
                continue
            seen.add(uri)
            t["_genre"] = g
            keep.append(t)
            got_ms += dur
        blocks[g] = keep
        total += got_ms
        print(f"  {g:<20} {len(keep):>3} tracks  {got_ms//60000:>3} min")

    picked = _sequence([t for g in genres for t in blocks.get(g, [])], order=genres)
    if not picked:
        raise DJError("no tracks found — try different --genres")

    pl = _call("POST", "/me/playlists",
               json={"name": a.name, "public": False,
                     "description": (f"Hermes DJ — {a.hours}h clean party set. "
                                     "No explicit tracks.")})
    for i in range(0, len(picked), 90):   # API takes 100 per call; stay under
        _call("POST", f"/playlists/{pl['id']}/items",
              json={"uris": [t["uri"] for t in picked[i:i + 90]]})
    hrs, mins = divmod(total // 60000, 60)
    print(f"\ncreated {a.name!r}: {len(picked)} tracks, {hrs}h {mins}m, all clean")
    print(pl.get("external_urls", {}).get("spotify", ""))



def _known_artists() -> set:
    """Artists already in the listener's world — top artists plus recent likes."""
    out = set()
    for rng in ("short_term", "medium_term", "long_term"):
        try:
            for a in _call("GET", "/me/top/artists",
                           params={"limit": 50, "time_range": rng}).get("items", []):
                out.add(a["name"].lower())
        except DJError:
            pass
    off = 0
    while off < 400:
        try:
            d = _call("GET", "/me/tracks", params={"limit": 50, "offset": off})
        except DJError:
            break
        items = d.get("items", [])
        if not items:
            break
        for it in items:
            for a in (it.get("track") or {}).get("artists", []):
                out.add(a["name"].lower())
        off += 50
    return out


def cmd_new_artists(a):
    """Recent releases by artists NOT already in the library.

    'New' can't come from popularity (null at this tier) or a new-releases
    endpoint (gone), so it's derived from album.release_date plus absence from
    the listener's own top-artists/liked set.
    """
    genres = ([g.strip() for g in a.genres.split(",") if g.strip()]
              if a.genres != KIDS_DEFAULT_SENTINEL
              else ["rnb", "neo soul", "afrobeats", "uk rap", "amapiano",
                    "hiplife", "jazz rap", "dancehall"])
    known = _known_artists()
    print(f"{len(known)} artists already in your world — excluding them")

    picked, seen = [], set()
    for g in genres:
        got = 0
        for t in _search_tracks(g, limit=a.depth):
            if got >= a.per_genre:
                break
            rel = (t.get("album", {}) or {}).get("release_date", "") or ""
            if rel[:4] < str(a.since):
                continue
            names = [ar["name"] for ar in t["artists"]]
            if any(n.lower() in known for n in names):
                continue
            if t["uri"] in seen or _is_kiddie(" ".join(names), t["name"]):
                continue
            if any(j in t["name"].lower() for j in JUNK_MARKERS):
                continue
            if _norm_title(t["name"]) == g.lower():
                continue
            t["_genre"] = g
            seen.add(t["uri"])
            picked.append(t)
            got += 1
        print(f"  {g:<14} {got} new")
    if not picked:
        raise DJError(f"nothing found released since {a.since} by unknown artists")

    picked = _sequence(picked, order=genres)
    pl = _call("POST", "/me/playlists",
               json={"name": a.name, "public": False,
                     "description": f"Artists new to you, released {a.since}+"})
    _call("POST", f"/playlists/{pl['id']}/items",
          json={"uris": [t["uri"] for t in picked]})
    print(f"\ncreated {a.name!r} with {len(picked)} tracks by artists new to you")
    for t in picked[:12]:
        rel = (t.get("album", {}) or {}).get("release_date", "")[:4]
        print(f"   {rel}  {_fmt(t)}")
    print(pl.get("external_urls", {}).get("spotify", ""))


def cmd_auth_url(a):
    print(auth_url())


def cmd_auth_exchange(a):
    print(auth_exchange(a.code))


def main() -> int:
    p = argparse.ArgumentParser(prog="spotify-dj", description="Hermes DJ — Spotify control")
    s = p.add_subparsers(dest="cmd", required=True)

    s.add_parser("devices").set_defaults(fn=cmd_devices)
    s.add_parser("status").set_defaults(fn=cmd_status)
    s.add_parser("pause").set_defaults(fn=cmd_pause)
    s.add_parser("next").set_defaults(fn=cmd_next)
    s.add_parser("prev").set_defaults(fn=cmd_prev)
    s.add_parser("auth-url").set_defaults(fn=cmd_auth_url)

    x = s.add_parser("auth-exchange"); x.add_argument("code"); x.set_defaults(fn=cmd_auth_exchange)
    x = s.add_parser("play"); x.add_argument("query", nargs="?"); x.add_argument("--device")
    x.add_argument("--limit", type=int, default=20); x.set_defaults(fn=cmd_play)
    x = s.add_parser("volume"); x.add_argument("percent", type=int); x.set_defaults(fn=cmd_volume)
    x = s.add_parser("search"); x.add_argument("query"); x.add_argument("--limit", type=int, default=10)
    x.set_defaults(fn=cmd_search)
    x = s.add_parser("queue"); x.add_argument("query"); x.set_defaults(fn=cmd_queue)
    x = s.add_parser("top"); x.add_argument("--limit", type=int, default=10)
    x.add_argument("--range", default="medium_term",
                   choices=["short_term", "medium_term", "long_term"]); x.set_defaults(fn=cmd_top)
    x = s.add_parser("weekly-mix"); x.add_argument("name", nargs="?", default="Hermes DJ — Weekly Mix")
    x.add_argument("--genres", default=KIDS_DEFAULT_SENTINEL)
    x.add_argument("--audience", choices=["me", "kids"], default="me")
    x.add_argument("--limit", type=int, default=30)
    x.add_argument("--allow-known", action="store_true", help="don't exclude library tracks")
    x.add_argument("--no-taste", action="store_true", help="genres only, ignore listening history")
    x.add_argument("--no-flow", action="store_true",
                   help="skip DJ sequencing; leave in discovery order")
    x.add_argument("--allow-repeats", action="store_true",
                   help="don't exclude tracks from earlier Hermes DJ mixes")
    x.set_defaults(fn=cmd_weekly_mix)
    x = s.add_parser("new-artists"); x.add_argument("name", nargs="?", default="Hermes DJ — New Artists")
    x.add_argument("--genres", default=KIDS_DEFAULT_SENTINEL)
    x.add_argument("--since", type=int, default=2025)
    x.add_argument("--per-genre", type=int, default=5)
    x.add_argument("--depth", type=int, default=60)
    x.set_defaults(fn=cmd_new_artists)
    x = s.add_parser("party-set"); x.add_argument("name")
    x.add_argument("--hours", type=float, default=6.0)
    x.add_argument("--genres", default=KIDS_DEFAULT_SENTINEL)
    x.add_argument("--depth", type=int, default=40,
                   help="how deep to page each genre search")
    x.add_argument("--exclude-artists", default="",
                   help="comma-separated artists to keep out of the set")
    x.add_argument("--max-track-min", type=float, default=7.0,
                   help="skip tracks longer than this (worship medleys, DJ mixes)")
    x.set_defaults(fn=cmd_party_set)
    x = s.add_parser("sequence", help="re-order any playlist you own into an arc")
    x.add_argument("playlist", help="playlist name, id, or open.spotify.com URL")
    x.add_argument("--order", help="comma-separated block order; default is derived by era")
    x.add_argument("--tag-by", choices=["artist-genre", "decade"], default="artist-genre",
                   dest="tag_by", help="what to group blocks on")
    x.add_argument("--into", help="write to a NEW playlist instead of re-ordering in place")
    x.add_argument("--dry-run", action="store_true", help="print the order, change nothing")
    x.add_argument("--verbose", action="store_true")
    x.set_defaults(fn=cmd_sequence)
    x = s.add_parser("make-playlist"); x.add_argument("name"); x.add_argument("--seed", required=True)
    x.add_argument("--limit", type=int, default=30); x.add_argument("--market", default="GB")
    x.set_defaults(fn=cmd_make_playlist)

    a = p.parse_args()
    try:
        a.fn(a)
    except (DJError, hermes_secrets.SecretError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
