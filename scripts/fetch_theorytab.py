#!/usr/bin/env python3
"""Fetch Hooktheory Theorytab sections for a list of songs.

Usage: python3 scripts/fetch_theorytab.py artist/song [artist/song ...]
       python3 scripts/fetch_theorytab.py --list scripts/songs.txt

For each slug: render the Theorytab page through r.jina.ai (the page itself is a
JS shell and 403s plain fetches; ask for the HTML), pull every section tab's data-hash, and fetch
each section from the public Hookpad API, which returns the human transcription
(melody notes as scale degrees, chords, key, tempo, YouTube id + sync points).
Raw API responses land in data/raw/<artist>__<song>__<section>.json. Video
lengths (needed to turn sync fractions into seconds) go in data/videos.json.
"""
import json, re, sys, time, urllib.request, pathlib

HERE = pathlib.Path(__file__).resolve().parent
RAW = HERE.parent / "data" / "raw"
VIDEOS = HERE.parent / "data" / "videos.json"
UA = {"User-Agent": "Mozilla/5.0 (interval-trainer; personal ear-training project)"}

def get(url, timeout=60, html=False):
    req = urllib.request.Request(url, headers={**UA, **({"X-Return-Format": "html"} if html else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def is_404(page):
    """Theorytab's 404 page is titled '404 - Hooktheory' (and embeds a demo Hookpad song, so ids alone
    prove nothing). Judge by the title only: real pages carry the 404 template's text too."""
    m = re.search(r"<title>\s*([^<]*)</title>", page) or re.search(r"^Title:\s*(.*)$", page, re.M)
    return bool(m) and m.group(1).strip().startswith("404")

def section_hashes(page):
    """Hookpad ids of every section on a Theorytab page, in page order.
    2026-10: the rendered markdown only exposes one idOfSong (the first tab); the HTML has each
    section tab as <div id="tab-<hash>" data-hash="<hash>">. Fall back to idOfSong= for old renders."""
    ids = re.findall(r'data-hash="([A-Za-z0-9_-]+)"', page) or re.findall(r"idOfSong=([A-Za-z0-9_-]+)", page)
    return list(dict.fromkeys(ids))

def video_length(yt_id, cache):
    if yt_id in cache:
        return cache[yt_id]
    try:
        html = get(f"https://www.youtube.com/watch?v={yt_id}", timeout=30)
        m = re.search(r'"lengthSeconds":"(\d+)"', html)
        cache[yt_id] = int(m.group(1)) if m else None
    except Exception as e:
        print(f"    ! video length failed for {yt_id}: {e}")
        cache[yt_id] = None
    return cache[yt_id]

def fetch_song(slug, cache):
    artist, song = slug.strip("/").split("/", 1)
    print(f"== {slug}")
    page = get(f"https://r.jina.ai/https://www.hooktheory.com/theorytab/view/{artist}/{song}", html=True)
    if is_404(page):
        print("    ! 404 on Theorytab (the 404 page embeds a demo song, so don't trust idOfSong here)")
        return
    ids = section_hashes(page)
    if not ids:
        print("    ! no Hookpad sections found (wrong slug, or page not rendered)")
        return
    for sid in ids:
        data = json.loads(get(f"https://api.hooktheory.com/v1/songs/public/{sid}", timeout=30))
        section = (data.get("section") or "section").lower().replace(" ", "-")
        out = RAW / f"{artist}__{song}__{section}.json"
        data["_slug"] = slug
        data["_hookpadId"] = sid
        out.write_text(json.dumps(data, indent=1))
        j = json.loads(data.get("jsonData") or "{}")
        yt = (j.get("youtube") or {}).get("id") or data.get("youTubeID") or ""
        m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", yt) or re.fullmatch(r"([A-Za-z0-9_-]{11})", yt)
        yt_id = m.group(1) if m else None
        n_notes = sum(1 for n in j.get("notes", []) if not n.get("isRest"))
        length = video_length(yt_id, cache) if yt_id else None
        print(f"    {section:14s} notes={n_notes:3d} chords={len(j.get('chords', [])):2d} yt={yt_id} len={length}")
        time.sleep(0.5)

def main(argv):
    slugs = []
    if argv and argv[0] == "--list":
        slugs = [l.strip() for l in open(argv[1]) if l.strip() and not l.startswith("#")]
    else:
        slugs = argv
    RAW.mkdir(parents=True, exist_ok=True)
    cache = json.loads(VIDEOS.read_text()) if VIDEOS.exists() else {}
    for slug in slugs:
        try:
            fetch_song(slug, cache)
        except Exception as e:
            print(f"    ! failed: {e}")
        VIDEOS.write_text(json.dumps(cache, indent=1, sort_keys=True))
        time.sleep(1)

if __name__ == "__main__":
    main(sys.argv[1:])
