#!/usr/bin/env python3
"""Fetch timed lyrics for every video in data/videos.json -> public/lyrics/<ytid>.json

Sources, in order:
 1. The video's own YouTube caption tracks via yt-dlp (aligned to the video
    timeline, which is what clips are cut on). Manual tracks only: auto (asr)
    tracks are misaligned enough that Mitchell would rather have none. Plain
    HTTP to the timedtext endpoint stopped working in 2026 (empty bodies),
    hence yt-dlp.
 2. lrclib.net line-synced lyrics for the studio track, used only when the
    video length matches the track length within 3 s (so offset ~ 0). Music
    videos with intros fail that test and get no lyrics rather than wrong ones.

Output schema:
  {"source": "youtube"|"lrclib", "lang": str|None, "kind": "manual"|"asr"|"line",
   "lines": [{"t": sec, "d": sec, "text": str, "words": [{"t": sec, "w": str}] | None}]}
Videos with nothing usable get {"source": None, "lines": []}.
Usage: python3 scripts/fetch_lyrics.py [--force]
"""
import json, re, sys, time, pathlib, subprocess, shutil, tempfile, urllib.request, urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE.parent / "data"
VIDEOS = json.loads((DATA / "videos.json").read_text())
OUT = HERE.parent / "public" / "lyrics"
UA = {"User-Agent": "interval-trainer/0.1 (personal ear-training project)"}

def song_for_video():
    m = {}
    for p in DATA.glob("raw/*.json"):
        d = json.loads(p.read_text()); j = json.loads(d.get("jsonData") or "{}")
        yt = (j.get("youtube") or {}).get("id") or d.get("youTubeID") or ""
        mm = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", yt) or re.fullmatch(r"([A-Za-z0-9_-]{11})", yt)
        if mm:
            m.setdefault(mm.group(1), (d["artist"], d["song"]))
    return m

def via_ytdlp(yt_id):
    if not shutil.which("yt-dlp"):
        return None
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run(["yt-dlp", "--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", "en.*,ja.*,es.*,ko.*",
                            "--sub-format", "json3", "-o", f"{td}/%(id)s", f"https://www.youtube.com/watch?v={yt_id}"],
                           capture_output=True, text=True, timeout=180)
        files = sorted(pathlib.Path(td).glob("*.json3"))
        if not files:
            return None
        # manual tracks are named <id>.<lang>.json3; auto ones the same, so detect asr by per-word segs
        best = None
        for f in files:
            data = json.loads(f.read_text())
            lines = []
            for ev in data.get("events", []):
                segs = [s for s in ev.get("segs", []) if s.get("utf8", "").strip()]
                if not segs:
                    continue
                t0 = ev.get("tStartMs", 0) / 1000
                text = "".join(s["utf8"] for s in segs).replace("\n", " ").strip()
                words = [{"t": round(t0 + s.get("tOffsetMs", 0) / 1000, 3), "w": s["utf8"].strip()} for s in segs] if len(segs) > 1 else None
                lines.append({"t": round(t0, 3), "d": round(ev.get("dDurationMs", 0) / 1000, 3), "text": text, "words": words})
            if not lines:
                continue
            raw_lang = f.name.split(".")[-2]            # e.g. "en", "en-orig" (auto, original language), "en-en-<vssId>"
            kind = "asr" if ("orig" in raw_lang or sum(1 for l in lines if l["words"]) > len(lines) / 2) else "manual"
            if kind == "asr":
                continue  # Mitchell (2026-09-02): auto-generated captions are worse than no lyrics
            lang = raw_lang.split("-")[0]
            cand = {"source": "youtube", "lang": lang, "kind": kind, "lines": lines}
            # prefer manual over asr; among equals prefer the first (original language listed first)
            if best is None or (best["kind"] == "asr" and kind == "manual"):
                best = cand
        return best

def parse_lrc(text):
    lines = []
    for m in re.finditer(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)", text):
        t = int(m.group(1)) * 60 + float(m.group(2)); s = m.group(3).strip()
        if s:
            lines.append({"t": round(t, 3), "text": s})
    for i, l in enumerate(lines):
        l["d"] = round((lines[i + 1]["t"] - l["t"]) if i + 1 < len(lines) else 4.0, 3); l["words"] = None
    return lines

def via_lrclib(yt_id, artist, title):
    q = urllib.parse.urlencode({"artist_name": artist, "track_name": title})
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://lrclib.net/api/get?{q}", headers=UA), timeout=20) as r:
            d = json.loads(r.read())
    except Exception:
        return None
    if not d.get("syncedLyrics"):
        return None
    vlen, tlen = VIDEOS.get(yt_id), d.get("duration")
    if not vlen or not tlen or abs(vlen - tlen) > 3:
        return {"source": None, "lines": [], "note": f"lrclib has synced lyrics but video {vlen}s vs track {tlen}s: offset unknown"}
    return {"source": "lrclib", "lang": None, "kind": "line", "lines": parse_lrc(d["syncedLyrics"])}

def main(force=False):
    OUT.mkdir(parents=True, exist_ok=True)
    songs = song_for_video()
    for yt_id in sorted(VIDEOS):
        out = OUT / f"{yt_id}.json"
        if out.exists() and not force and json.loads(out.read_text()).get("lines"):
            continue
        res = via_ytdlp(yt_id)
        if not res or not res["lines"]:
            a, t = songs.get(yt_id, ("", ""))
            res = via_lrclib(yt_id, a, t) or {"source": None, "lines": []}
        out.write_text(json.dumps(res, ensure_ascii=False))
        extra = f" ({res['note']})" if res.get("note") else ""
        print(f"{yt_id} {songs.get(yt_id, ('?', '?'))[1][:28]:28s} {res.get('source')} {res.get('kind')} {res.get('lang')} {len(res['lines'])} lines{extra}")
        time.sleep(1)

if __name__ == "__main__":
    main("--force" in sys.argv)
