#!/usr/bin/env python3
"""Pull Mitchell's most-played Spotify tracks and find which are on Theorytab.

Why: the hook pool gets boring after a few rounds; the songs you actually play
most are the hooks you know best (Open Problem 10).

Setup, once (needs a Spotify Premium account on the app owner, Spotify's rule for
development-mode apps since 2026-02):
  1. https://developer.spotify.com/dashboard -> Create app. Name anything; API: Web API;
     Redirect URI exactly  http://127.0.0.1:8888/callback  (Spotify no longer accepts
     "localhost").
  2. Copy the Client ID into  ~/.config/interval-trainer/spotify.json  as
       {"client_id": "..."}      (chmod 600; tokens are cached in the same file; never in the repo)
Run, from code/:
  python3 scripts/spotify_top.py            # sign in (browser opens), fetch top tracks -> data/spotify_top.json
  python3 scripts/spotify_top.py --propose  # also check Theorytab for each -> data/spotify_candidates.txt
  python3 scripts/spotify_top.py --playlists --propose   # also every playlist's tracks (needs the playlist scopes; re-signs in once)
Then review data/spotify_candidates.txt, paste the slugs you want into scripts/songs.txt,
and run the usual pipeline (fetch_theorytab.py --list, verify_clips.py, mine_moments.py).

Spotify facts this relies on (checked 2026-10-03): GET /me/top/tracks, scope user-top-read,
time_range short_term (~4 weeks) / medium_term (~6 months) / long_term (~1 year+), limit <= 50,
Authorization Code with PKCE, loopback redirect. stdlib only.
"""
import base64, hashlib, http.server, json, os, pathlib, re, secrets, sys, threading, time, urllib.parse, urllib.request, webbrowser

HERE = pathlib.Path(__file__).resolve().parent
CODE = HERE.parent
CONF = pathlib.Path(os.environ.get("SPOTIFY_CONF", "~/.config/interval-trainer/spotify.json")).expanduser()
OUT = CODE / "data" / "spotify_top.json"
CAND = CODE / "data" / "spotify_candidates.txt"
SONGS = HERE / "songs.txt"
REDIRECT = "http://127.0.0.1:8888/callback"
SCOPE = "user-top-read playlist-read-private playlist-read-collaborative"
PLAYLISTS = CODE / "data" / "spotify_playlists.json"
UA = {"User-Agent": "Mozilla/5.0 (interval-trainer; personal ear-training project)"}


def load_conf():
    if not CONF.exists():
        sys.exit(f"missing {CONF}: create it with {{\"client_id\": \"...\"}} (see the docstring)")
    return json.loads(CONF.read_text())


def save_conf(c):
    CONF.parent.mkdir(parents=True, exist_ok=True)
    CONF.write_text(json.dumps(c, indent=1))
    os.chmod(CONF, 0o600)


def post_form(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), headers={**UA, "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def authorize(conf):
    """Authorization Code with PKCE; the browser lands on a one-shot local server."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(b"<p>Signed in. You can close this tab and go back to the terminal.</p>")
        def log_message(self, *a): pass

    srv = http.server.HTTPServer(("127.0.0.1", 8888), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": conf["client_id"], "response_type": "code", "redirect_uri": REDIRECT, "scope": SCOPE,
        "code_challenge_method": "S256", "code_challenge": challenge, "state": state})
    if "--no-browser" in sys.argv:
        print("open this to sign in to Spotify:\n  " + url, flush=True)
    else:
        print("opening the browser to sign in to Spotify…\n  (if it doesn't open: " + url + ")", flush=True)
        webbrowser.open(url)
    for _ in range(600):
        if got: break
        time.sleep(0.5)
    srv.server_close()
    if got.get("state") != state or "code" not in got:
        sys.exit(f"sign-in failed: {got.get('error') or 'no code returned'}")
    tok = post_form("https://accounts.spotify.com/api/token", {
        "client_id": conf["client_id"], "grant_type": "authorization_code", "code": got["code"],
        "redirect_uri": REDIRECT, "code_verifier": verifier})
    conf["token"] = {**tok, "obtained": int(time.time())}
    save_conf(conf)
    return conf


def token(conf):
    t = conf.get("token")
    if t and not set(SCOPE.split()) <= set((t.get("scope") or "").split()):
        print("cached token lacks the scopes now needed; signing in again"); t = None
    if t and time.time() < t["obtained"] + t.get("expires_in", 3600) - 60:
        return t["access_token"]
    if t and t.get("refresh_token"):
        try:
            new = post_form("https://accounts.spotify.com/api/token", {
                "client_id": conf["client_id"], "grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
            conf["token"] = {**t, **new, "obtained": int(time.time())}
            save_conf(conf)
            return conf["token"]["access_token"]
        except Exception as e:
            print(f"refresh failed ({e}); signing in again")
    return authorize(conf)["token"]["access_token"]


def api(path, tok, **params):
    url = "https://api.spotify.com/v1" + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={**UA, "Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def paged(path, tok, **params):
    items, offset = [], 0
    while True:
        d = api(path, tok, limit=50, offset=offset, **params)
        items += d.get("items", [])
        if not d.get("next") or len(d.get("items", [])) == 0:
            return items
        offset += 50


def fetch_playlists(conf):
    """All of Mitchell's playlists (owned and followed) with their tracks -> data/spotify_playlists.json.
    Spotify exposes no per-playlist play counts (nor does the privacy export), so order is Spotify's own."""
    tok = token(conf)
    conf["user_id"] = api("/me", tok).get("id"); save_conf(conf)
    out = {"fetched": time.strftime("%Y-%m-%d"), "playlists": []}
    for pl in paged("/me/playlists", tok):
        if not pl:
            continue
        tracks = []
        try:   # 2026-02 API: /playlists/{id}/items (the old /tracks path now 403s)
            items = paged(f"/playlists/{pl['id']}/items", tok, fields="items(item(name,artists(name),album(name),id),track(name,artists(name),album(name),id)),next")
        except urllib.error.HTTPError as e:
            print(f"  ! {pl['name']}: {e}"); items = []
        for it in items:
            t = (it or {}).get("item") or (it or {}).get("track") or {}
            if t.get("name") and t.get("artists"):
                tracks.append({"artist": t["artists"][0]["name"], "artists": [a["name"] for a in t["artists"]], "title": t["name"], "album": (t.get("album") or {}).get("name"), "id": t.get("id")})
        me = (pl.get("owner") or {}).get("id")
        out["playlists"].append({"id": pl["id"], "name": pl["name"], "owner": (pl.get("owner") or {}).get("display_name"), "ownerId": me, "collaborative": bool(pl.get("collaborative")), "tracks": tracks})
        print(f"  {pl['name']:40s} {len(tracks)} tracks")
        time.sleep(0.2)
    PLAYLISTS.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"-> {PLAYLISTS.relative_to(CODE)}: {len(out['playlists'])} playlists, {sum(len(p['tracks']) for p in out['playlists'])} tracks")
    return out


def fetch_top(conf):
    tok = token(conf)
    me = api("/me", tok)
    conf["user_id"] = me.get("id"); save_conf(conf)
    print(f"signed in as {me.get('display_name') or me.get('id')}")
    out = {"fetched": time.strftime("%Y-%m-%d"), "ranges": {}}
    for rng, pages in (("short_term", 1), ("medium_term", 1), ("long_term", 2)):
        items = []
        for page in range(pages):
            d = api("/me/top/tracks", tok, time_range=rng, limit=50, offset=50 * page)
            items += d.get("items", [])
            if not d.get("next"):
                break
        out["ranges"][rng] = [{"rank": i + 1, "artist": t["artists"][0]["name"], "artists": [a["name"] for a in t["artists"]],
                               "title": t["name"], "album": t["album"]["name"], "popularity": t.get("popularity"), "id": t["id"]}
                              for i, t in enumerate(items)]
        print(f"  {rng:12s} {len(items)} tracks")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"-> {OUT.relative_to(CODE)}")
    return out


# ---------- Theorytab ----------
def slug(s, keep_parens=False):
    """Theorytab-style slug. Default drops parentheticals ('(feat. X)'); keep_parens=True keeps them the
    way Theorytab does: 'I'm Not Okay (I Promise)' -> 'im-not-okay-(i-promise)'."""
    s = s.lower()
    if keep_parens:
        s = s.replace("&", "and").replace("'", "").replace("’", "")
        s = re.sub(r"^(.+?)\s+-\s+.*$", r"\1", s)
        s = re.sub(r"[^a-z0-9()]+", "-", s).strip("-")
        return re.sub(r"-?\(-?", "-(", re.sub(r"-?\)-?", ")-", s)).strip("-")
    s = re.sub(r"\s*[\(\[].*?[\)\]]", "", s)                    # (feat. X), [Remastered]
    s = re.sub(r"^(.+?)\s+-\s+.*$", r"\1", s)                    # 'Title - Remastered 2011', 'Title - From "Film"', 'Title - feat. X'
    s = s.replace("&", "and").replace("'", "").replace("’", "")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def section_ids(page):
    """Hookpad ids of the sections on a Theorytab page (HTML render): one <div data-hash=...> per section tab."""
    ids = re.findall(r'data-hash="([A-Za-z0-9_-]+)"', page) or re.findall(r"idOfSong=([A-Za-z0-9_-]+)", page)
    return list(dict.fromkeys(ids))


def render(url, html=True, tries=4):
    """Fetch through r.jina.ai; back off on 429 (it rate-limits bursts)."""
    for i in range(tries):
        try:
            req = urllib.request.Request(f"https://r.jina.ai/{url}", headers={**UA, **({"X-Return-Format": "html"} if html else {})})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tries - 1:
                time.sleep(15 * (i + 1)); continue
            raise
    return ""


def is_404(page):
    t = re.search(r"<title>\s*([^<]*)</title>", page) or re.search(r"^Title:\s*(.*)$", page, re.M)
    return bool(t) and t.group(1).strip().startswith("404")   # the 404 page embeds a demo Hookpad song; judge by title


def theorytab_has(slug_path, cache):
    if cache.get(slug_path, -1) >= 0:   # -1 = earlier network failure, retry
        return cache[slug_path]
    try:
        page = render(f"https://www.hooktheory.com/theorytab/view/{slug_path}")
        cache[slug_path] = 0 if is_404(page) else len(section_ids(page))
    except Exception as e:
        print(f"    ! {slug_path}: {e}")
        cache[slug_path] = -1
    time.sleep(1.5)
    return cache[slug_path]


ARTIST_ALIASES = {"ヨルシカ": "yorushika", "米津玄師": "kenshi-yonezu", "Cö shu Nie": "co-shu-nie", "李浩瑋 Howard Lee": "howard-lee"}


def artist_songs(artist_slug, cache):
    """Every song slug Theorytab lists for an artist (its artist page), for titles we can't slug ourselves."""
    key = f"artist:{artist_slug}"
    if key not in cache:
        try:
            page = render(f"https://www.hooktheory.com/theorytab/artist/{artist_slug}")
            cache[key] = [] if is_404(page) else sorted(set(re.findall(rf"theorytab/view/{re.escape(artist_slug)}/([a-z0-9()-]+)", page)))
        except Exception as e:
            print(f"    ! artist {artist_slug}: {e}"); cache[key] = []
        time.sleep(1.5)
    return cache[key]
    try:
        req = urllib.request.Request(f"https://r.jina.ai/https://www.hooktheory.com/theorytab/view/{slug_path}", headers={**UA, "X-Return-Format": "html"})
        with urllib.request.urlopen(req, timeout=60) as r:
            page = r.read().decode("utf-8", "replace")
        # the 404 page embeds a demo Hookpad song, so "has an idOfSong" is not "exists"
        t = re.search(r"<title>\s*([^<]*)</title>", page) or re.search(r"^Title:\s*(.*)$", page, re.M)
        if t and t.group(1).strip().startswith("404"):   # the 404 page embeds a demo Hookpad song; judge by title
            cache[slug_path] = 0
        else:
            cache[slug_path] = len(section_ids(page))
    except Exception as e:
        print(f"    ! {slug_path}: {e}")
        cache[slug_path] = -1
    time.sleep(1.5)
    return cache[slug_path]


def propose(top):
    have = {l.strip() for l in SONGS.read_text().splitlines() if l.strip() and not l.startswith("#")}
    cache_file = CODE / "data" / "theorytab_slug_cache.json"
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    # one row per distinct track, best (lowest) rank per range
    tracks = {}
    for rng, items in top["ranges"].items():
        for t in items:
            key = (t["artist"], t["title"])
            tracks.setdefault(key, {"artist": t["artist"], "title": t["title"], "ranks": {}, "playlists": []})["ranks"][rng] = t["rank"]
    if PLAYLISTS.exists():
        pls = json.loads(PLAYLISTS.read_text())["playlists"]
        me = json.loads(CONF.read_text()).get("user_id")
        use = [pl for pl in pls if "--all-playlists" in sys.argv or pl.get("collaborative") or (me and pl.get("ownerId") == me)]
        print(f"playlists in scope: {len(use)} of {len(pls)} ({'all' if '--all-playlists' in sys.argv else 'own + collaborative; --all-playlists for followed ones'})")
        for pl in use:
            for t in pl["tracks"]:
                key = (t["artist"], t["title"])
                row = tracks.setdefault(key, {"artist": t["artist"], "title": t["title"], "ranks": {}, "playlists": []})
                if pl["name"] not in row["playlists"]:
                    row["playlists"].append(pl["name"])
    rows = []
    for (artist, title), t in tracks.items():
        a_slug = ARTIST_ALIASES.get(artist) or slug(artist)
        if not a_slug or not slug(title):   # non-Latin title: Theorytab romanizes; list what it has for the artist instead
            have_songs = artist_songs(a_slug, cache) if a_slug else []
            cache_file.write_text(json.dumps(cache, indent=0, sort_keys=True))
            rows.append({**t, "slug": f"{a_slug or '?'}/?", "sections": 0, "status": "not found",
                         "note": f"artist has on Theorytab: {', '.join(have_songs)}" if have_songs else "artist not on Theorytab" if a_slug else "can't slug artist"})
            continue
        cands = [f"{a_slug}/{slug(title)}"]
        if slug(title, True) != slug(title):
            cands.append(f"{a_slug}/{slug(title, True)}")
        if a_slug.startswith("the-"):
            cands.append(f"{a_slug[4:]}/{slug(title)}")
        found = None
        for c in cands:
            n = theorytab_has(c, cache)
            cache_file.write_text(json.dumps(cache, indent=0, sort_keys=True))
            if n > 0:
                found = (c, n); break
        status = "in songs.txt" if found and found[0] in have else ("on theorytab" if found else "not found")
        note = ""
        if not found:   # maybe Theorytab has it under a longer/shorter title: match against the artist's list
            have_songs = artist_songs(a_slug, cache)
            cache_file.write_text(json.dumps(cache, indent=0, sort_keys=True))
            ts = slug(title)
            fuzzy = [x for x in have_songs if len(ts) >= 4 and (x.rstrip("-") == ts or x.startswith(ts + "-(") or x.startswith(ts + "-") or (len(x.rstrip("-")) >= 6 and ts.startswith(x.rstrip("-") + "-")))]
            fuzzy.sort(key=lambda x: ("remix" in x or "live" in x or "version" in x, len(x)))   # prefer the plain tab over remixes
            if fuzzy:
                n = theorytab_has(f"{a_slug}/{fuzzy[0]}", cache)
                cache_file.write_text(json.dumps(cache, indent=0, sort_keys=True))
                if n > 0:
                    found = (f"{a_slug}/{fuzzy[0]}", n); status = "in songs.txt" if found[0] in have else "on theorytab"; note = "matched by title prefix"
            if not found:
                note = f"artist has on Theorytab: {', '.join(have_songs)}" if have_songs else "artist not on Theorytab"
        rows.append({**t, "slug": found[0] if found else cands[0], "sections": found[1] if found else 0, "status": status, "note": note})
        print(f"  {status:13s} {rows[-1]['slug']:60s} {' '.join(f'{k[0]}{v}' for k, v in sorted(t['ranks'].items()))}", flush=True)
    rows.sort(key=lambda r: (r["status"] != "on theorytab", min(r["ranks"].values()) if r["ranks"] else 999, -len(r.get("playlists", []))))
    lines = [f"# Spotify top tracks vs Theorytab — {top['fetched']}. s=short (4 wk) m=medium (6 mo) l=long (~1 yr+) rank.",
             "# Paste 'on theorytab' slugs you know cold into scripts/songs.txt, then run the pipeline.", ""]
    for r in rows:
        ranks = " ".join(f"{k[0]}{v}" for k, v in sorted(r["ranks"].items()))
        pls = (" {" + "; ".join(r["playlists"]) + "}") if r.get("playlists") else ""
        lines.append(f"{r['status']:13s} {r['slug']:60s} {r['sections']:2d} sections  {ranks:16s} # {r['artist']} – {r['title']}{pls}" + (f"  [{r['note']}]" if r.get('note') else ""))
    CAND.write_text("\n".join(lines) + "\n")
    n_on = sum(r["status"] == "on theorytab" for r in rows)
    print(f"\n{len(rows)} distinct tracks: {n_on} on Theorytab and not yet in songs.txt, "
          f"{sum(r['status'] == 'in songs.txt' for r in rows)} already in, {sum(r['status'] == 'not found' for r in rows)} not found -> {CAND.relative_to(CODE)}")


def main(argv):
    if "--playlists" in argv:
        fetch_playlists(load_conf())
    if OUT.exists() and "--refetch" not in argv:
        top = json.loads(OUT.read_text())
        print(f"using cached {OUT.relative_to(CODE)} from {top['fetched']} (pass --refetch to pull again)")
    else:
        top = fetch_top(load_conf())
    if "--propose" in argv:
        propose(top)


if __name__ == "__main__":
    main(sys.argv[1:])
