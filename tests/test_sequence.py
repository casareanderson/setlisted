#!/usr/bin/env python3
"""Offline tests for the sequencer. No network, no Spotify credentials.

The sequencer is pure metadata, so it is testable without touching the API --
which matters, because the API has a daily quota you can exhaust.

    python3 tests/test_sequence.py
"""
import importlib.util, os, random
from itertools import groupby

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("dj", os.path.join(ROOT, "spotify_dj.py"))
dj = importlib.util.module_from_spec(spec); spec.loader.exec_module(dj)

def T(n, artist, aid, year):
    return {"uri": f"spotify:track:{n}", "name": n, "artists": [{"name": artist, "id": aid}],
            "album": {"release_date": year}, "type": "track"}

def check(out, label, strict_chrono=True):
    seen = []
    for t in out:
        if not seen or seen[-1] != t["_genre"]: seen.append(t["_genre"])
    assert len(seen) == len(set(seen)), f"{label}: blocks fragmented {seen}"
    adj = [i for i in range(1, len(out))
           if out[i]["artists"][0]["name"] == out[i-1]["artists"][0]["name"]
           and out[i]["_genre"] == out[i-1]["_genre"]]
    return adj

tracks = [T("Juicy","Biggie","a1","1994"), T("Big Poppa","Biggie","a1","1995"),
          T("93 Til","Souls","a2","1993"), T("Untitled","D'Angelo","a3","2000"),
          T("Brown Sugar","D'Angelo","a3","1995"), T("Ye","Burna Boy","a4","2018"),
          T("Superstar","Lauryn Hill","a5","1998")]
gmap = {"a1":"90s hip hop","a2":"90s hip hop","a3":"neo soul","a4":"afrobeats","a5":"neo soul"}
dj.tag_tracks(tracks,"artist-genre",gmap)
arc = dj.auto_arc(tracks); print("auto arc:", arc)
out = dj._sequence(tracks, order=arc)
last=None
for t in out:
    if t["_genre"]!=last: last=t["_genre"]; print(f"  [{last}]")
    print("   ", t["name"], "-", t["artists"][0]["name"], t["album"]["release_date"])
adj = check(out, "fixture")
assert not adj, f"adjacent same artist at {adj}"

# tail-pair case in isolation: block ending in two tracks by one artist
blk = [T("x","Souls","a2","1993"), T("Juicy","Biggie","a1","1994"), T("Big Poppa","Biggie","a1","1995")]
r = dj._dedupe_artists(list(blk))
assert [t["artists"][0]["name"] for t in r] != ["Souls","Biggie","Biggie"], "tail pair unfixed"
print("\ntail case ->", [t['name'] for t in r])

# unsatisfiable block (3 of 3 same artist) must not crash or drop tracks
blk = [T("a","Solo","z","1990"), T("b","Solo","z","1991"), T("c","Solo","z","1992")]
r = dj._dedupe_artists(list(blk))
assert sorted(t["name"] for t in r) == ["a","b","c"], "tracks lost"
print("unsatisfiable block ->", [t['name'] for t in r], "(no crash, nothing lost)")

# randomised: never lose or duplicate a track, never fragment a block
random.seed(7)
worst = 0
for n in range(300):
    ts = [T(f"t{i}", f"art{random.randint(0,3)}", f"id{random.randint(0,3)}",
            str(random.randint(1985,2024))) for i in range(random.randint(2,30))]
    for t in ts: t["_genre"] = random.choice(["g1","g2","g3"])
    out = dj._sequence(list(ts), order=["g1","g2","g3"])
    assert len(out) == len(ts), f"count changed {len(out)} vs {len(ts)}"
    assert sorted(id(x) for x in out) == sorted(id(x) for x in ts), "tracks swapped out"
    worst = max(worst, len(check(out, f"rand{n}")))
print(f"\n300 random playlists: no loss, no fragmentation; "
      f"worst-case unavoidable same-artist adjacencies in one set: {worst}")

# explicit --order beats the derived arc
out2 = dj._sequence(tracks, order=["afrobeats","neo soul","90s hip hop"])
assert out2[0]["_genre"] == "afrobeats"
# decade tagging rides the same machinery
dj.tag_tracks(tracks, "decade")
assert dj.auto_arc(tracks)[0] == "1990s"
print("explicit order + decade tagging: ok")
print("\nALL OFFLINE TESTS PASS")
