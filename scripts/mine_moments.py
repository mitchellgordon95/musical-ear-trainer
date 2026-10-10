#!/usr/bin/env python3
"""Mine interval moments out of fetched Theorytab sections -> public/clips.json.

A moment is two consecutive melody notes (no more than a short rest between
them). Its clip window is computed from Hookpad's YouTube sync points: the
section's first and last keyFrames map linearly onto syncStart/syncEnd fractions
of the video length: t(beat) = len * (s0 + (s1 - s0) * (beat - k0) / (k1 - k0)).
(Not endBeat: several tabs have trailing empty measures past the last keyframe.)
The derived tempo is cross-checked against the tab's nominal BPM; sections that
disagree by more than 4% are flagged timing="suspect".
Theorytab's own precision is roughly +/-0.2 s; the app pads accordingly.

Hooks (2026-10-02): each section's melody is also cut into phrases at rests of
a beat or more (3-8 notes each, first few phrases per section). A hook carries
its interval sequence with no rhythm; the app's Hooks tab drills
intervals -> name the song and song -> name the first interval.
"""
import json, re, pathlib, collections

HERE = pathlib.Path(__file__).resolve().parent
RAW = HERE.parent / "data" / "raw"
VIDEOS = json.loads((HERE.parent / "data" / "videos.json").read_text())
OUT = HERE.parent / "public" / "clips.json"          # what the app loads: sections + hooks (no two-note clips)
FULL = HERE.parent / "data" / "clips_full.json"       # everything, for verify_clips.py

SCALES = {
    "major": [0, 2, 4, 5, 7, 9, 11], "minor": [0, 2, 3, 5, 7, 8, 10],
    "dorian": [0, 2, 3, 5, 7, 9, 10], "phrygian": [0, 1, 3, 5, 7, 8, 10],
    "lydian": [0, 2, 4, 6, 7, 9, 11], "mixolydian": [0, 2, 4, 5, 7, 9, 10],
    "locrian": [0, 1, 3, 5, 6, 8, 10], "harmonicminor": [0, 2, 3, 5, 7, 8, 11],
    "phrygiandominant": [0, 1, 4, 5, 7, 8, 10],
}
PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
INTERVALS = {1: "m2", 2: "M2", 3: "m3", 4: "M3", 5: "P4", 6: "TT", 7: "P5",
             8: "m6", 9: "M6", 10: "m7", 11: "M7", 12: "P8"}
HOOK_MIN, HOOK_MAX, HOOK_PHRASES = 3, 8, 6   # notes per phrase; phrases kept per section

def pitch_class(tonic):
    pc = PC[tonic[0].upper()]
    for ch in tonic[1:]:
        pc += {"#": 1, "b": -1}.get(ch, 0)
    return pc % 12

def sd_to_midi(sd, octave, tonic, scale):
    m = re.fullmatch(r"([#b]*)(\d)", str(sd))
    acc, deg = m.group(1), int(m.group(2))
    steps = SCALES.get(scale.lower().replace(" ", ""), SCALES["major"])
    semis = steps[(deg - 1) % 7] + 12 * ((deg - 1) // 7)
    semis += acc.count("#") - acc.count("b")
    # Calibrated against Theorytab's printed "Melody Range": octave 0, degree 1 = tonic in MIDI octave 4.
    return 60 + pitch_class(tonic) + 12 * octave + semis

def midi_name(m):
    return f"{NAMES[m % 12]}{m // 12 - 1}"

def key_at(keys, beat):
    cur = keys[0]
    for k in keys:
        if k["beat"] <= beat:
            cur = k
    return cur

def mine(path):
    data = json.loads(path.read_text())
    j = json.loads(data["jsonData"])
    yt = j.get("youtube") or {}
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", yt.get("id") or "") or \
        re.fullmatch(r"([A-Za-z0-9_-]{11})", data.get("youTubeID") or "")
    if not m:
        return None, "no youtube id"
    yt_id = m.group(1)
    length = VIDEOS.get(yt_id)
    s0, s1 = yt.get("syncStart"), yt.get("syncEnd")
    kfs = sorted(k["beat"] for k in j.get("keyFrames") or [])
    k0, k1 = (kfs[0], kfs[-1]) if len(kfs) >= 2 else (1, j.get("endBeat") or 1)
    if not length or not s0 or not s1 or s1 <= s0 or k1 <= k0:
        return None, f"no usable sync (len={length} s0={s0} s1={s1} kf={k0}-{k1})"
    def t(beat):
        return length * (s0 + (s1 - s0) * (beat - k0) / (k1 - k0))
    sec_per_beat = t(2) - t(1)
    nominal_bpm = (j.get("tempos") or [{}])[0].get("bpm")
    derived_bpm = 60 / sec_per_beat
    timing = "ok"
    if nominal_bpm and abs(derived_bpm / nominal_bpm - 1) > 0.04:
        timing = "suspect"
    keys = j["keys"]
    notes = sorted((n for n in j["notes"] if not n.get("isRest")), key=lambda n: n["beat"])
    for n in notes:
        k = key_at(keys, n["beat"])
        n["midi"] = sd_to_midi(n["sd"], n["octave"], k["tonic"], k["scale"])
    song = {"artist": data["artist"], "title": data["song"], "section": data["section"],
            "slug": data.get("_slug"), "hookpadId": data.get("_hookpadId"), "youtubeId": yt_id,
            "videoLength": length, "bpm": round(derived_bpm, 1), "nominalBpm": nominal_bpm, "timing": timing,
            "key": f'{keys[0]["tonic"]} {keys[0]["scale"]}',
            "melodyRange": f"{midi_name(min(n['midi'] for n in notes))}-{midi_name(max(n['midi'] for n in notes))}" if notes else None}
    song["notes"] = [{"on": round(t(n["beat"]), 3), "off": round(t(n["beat"] + n["duration"]), 3),
                      "midi": n["midi"], "name": midi_name(n["midi"])} for n in notes]
    song["secPerBeat"] = round(sec_per_beat, 4)
    # Chord track (for longer "find the tonic" clips): onsets/offsets in seconds, root as a scale degree index.
    song["chords"] = [{"on": round(t(c["beat"]), 3), "off": round(t(c["beat"] + c["duration"]), 3), "root": c.get("root"), "type": c.get("type")}
                      for c in sorted(j.get("chords") or [], key=lambda c: c["beat"]) if not c.get("isRest")]
    # Hooks: phrases of the melody, split at rests of >= 1 beat, capped at HOOK_MAX notes
    # (the tail of an over-long phrase is dropped, not started as a new phrase).
    phrases, cur, overflow = [], [], False
    for i, n in enumerate(notes):
        if i:
            prev = notes[i - 1]
            if n["beat"] - (prev["beat"] + prev["duration"]) >= 1.0:
                if len(cur) >= HOOK_MIN:
                    phrases.append(cur)
                cur, overflow = [], False
            elif len(cur) >= HOOK_MAX:
                overflow = True
        if not overflow:
            cur.append(i)
    if len(cur) >= HOOK_MIN:
        phrases.append(cur)
    hooks = []
    for pi, idxs in enumerate(phrases[:HOOK_PHRASES]):
        ns = [notes[i] for i in idxs]
        ivs = []
        for a, b in zip(ns, ns[1:]):
            d = b["midi"] - a["midi"]
            label = "same" if d == 0 else INTERVALS[min(abs(d), 12)] + ("+" if abs(d) > 12 else "")
            ivs.append({"semis": d, "label": label, "dir": "up" if d > 0 else "down" if d < 0 else "same"})
        first = next((x for x in ivs if x["semis"]), None)
        k = key_at(keys, ns[0]["beat"])
        hooks.append({
            "id": f"{path.stem}#h{pi}", "sectionKey": path.stem, "phrase": pi, "noteIndex": idxs[0], "n": len(idxs),
            "start": round(t(ns[0]["beat"]), 3), "end": round(t(ns[-1]["beat"] + ns[-1]["duration"]), 3),
            "notes": [{"midi": n["midi"], "name": midi_name(n["midi"]), "sd": str(n["sd"])} for n in ns],
            "intervals": ivs, "firstInterval": first["label"] if first else None, "firstDir": first["dir"] if first else None,
            "firstSd": str(ns[0]["sd"]), "tonic": pitch_class(k["tonic"]), "key": f'{k["tonic"]} {k["scale"]}',   # Theorytab degrees are relative to the key at the hook
        })
    clips = []
    for i in range(len(notes) - 1):
        a, b = notes[i], notes[i + 1]
        gap = b["beat"] - (a["beat"] + a["duration"])
        if gap > 0.75 or gap < -0.01:
            continue
        semis = b["midi"] - a["midi"]
        if semis == 0 or abs(semis) > 12:
            continue
        a_on, b_on, b_off = t(a["beat"]), t(b["beat"]), t(b["beat"] + b["duration"])
        prev_gap = (a["beat"] - (notes[i - 1]["beat"] + notes[i - 1]["duration"])) * sec_per_beat if i > 0 else 9
        next_gap = ((notes[i + 2]["beat"] - (b["beat"] + b["duration"])) * sec_per_beat) if i + 2 < len(notes) else 9
        a_sec, b_sec = a["duration"] * sec_per_beat, b["duration"] * sec_per_beat
        # Clarity: longer notes and space around the pair make a cleaner clip.
        score = round(min(1.0, min(a_sec, b_sec) / 0.35) * 0.7 + min(1.0, min(prev_gap, next_gap) / 0.3) * 0.3, 2)
        clips.append({
            "id": f'{path.stem}#{i}', "sectionKey": path.stem, "noteIndex": i,
            "start": round(a_on, 3), "onsetA": round(a_on, 3), "onsetB": round(b_on, 3), "end": round(b_off, 3),
            "beat": a["beat"], "semitones": semis, "interval": INTERVALS[abs(semis)],
            "direction": "up" if semis > 0 else "down",
            "notes": [{"midi": a["midi"], "name": midi_name(a["midi"]), "sd": a["sd"], "dur": round(a_sec, 3)},
                      {"midi": b["midi"], "name": midi_name(b["midi"]), "sd": b["sd"], "dur": round(b_sec, 3)}],
            "score": score,
        })
    return {"section": song, "clips": clips, "hooks": hooks}, None

def canonical_names(sections):
    """Theorytab sections of one song are edited by different people and disagree on
    capitalization ("billy joel" vs "Billy Joel"). Pick, per slug, the variant with the
    most capitals so every section of a song shares one display name."""
    best = {}
    for s in sections.values():
        cur = best.get(s["slug"])
        cand = (s["artist"], s["title"])
        if cur is None or sum(ch.isupper() for ch in "".join(cand)) > sum(ch.isupper() for ch in "".join(cur)):
            best[s["slug"]] = cand
    for s in sections.values():
        s["artist"], s["title"] = best[s["slug"]]

def main():
    prom = json.loads((HERE.parent / "data" / "prominence.json").read_text()) if (HERE.parent / "data" / "prominence.json").exists() else {}
    unavailable = set(json.loads((HERE.parent / "data" / "unavailable.json").read_text())) if (HERE.parent / "data" / "unavailable.json").exists() else set()
    sections, all_clips, all_hooks, skipped = {}, [], [], []
    for p in sorted(RAW.glob("*.json")):
        res, why = mine(p)
        if res is None:
            skipped.append((p.name, why)); continue
        sections[p.stem] = res["section"]
        all_clips.extend(res["clips"])
        all_hooks.extend(res["hooks"])
    canonical_names(sections)
    psec, pclip = prom.get("sections", {}), prom.get("clips", {})
    shifted = 0
    for k, sec in sections.items():
        sec["unavailable"] = sec["youtubeId"] in unavailable   # yt-dlp said "Video unavailable": the embed is dead too
        v = psec.get(k)
        gain = (v["ratioAtBest"] - v["ratioAtZero"]) if v else 0.0
        at_edge = v is not None and abs(abs(v["offset"]) - 0.5) < 1e-6      # hit the search boundary: trust only a big gain
        off = v["offset"] if v and gain > 0.03 and (not at_edge or gain >= 0.2) else 0.0
        sec["offset"] = off                                     # audio-derived correction to the Theorytab sync (verify_clips.py)
        if off:
            shifted += 1
            for n in sec["notes"]:
                n["on"] = round(n["on"] + off, 3); n["off"] = round(n["off"] + off, 3)
            for c in sec.get("chords", []):
                c["on"] = round(c["on"] + off, 3); c["off"] = round(c["off"] + off, 3)
    for c in all_clips:
        off = sections[c["sectionKey"]]["offset"]
        if off:
            for f in ("start", "onsetA", "onsetB", "end"):
                c[f] = round(c[f] + off, 3)
        c.update(pclip.get(c["id"], {}))                          # prom/promA/promB/promOct
    for h in all_hooks:
        off = sections[h["sectionKey"]]["offset"]
        if off:
            h["start"] = round(h["start"] + off, 3); h["end"] = round(h["end"] + off, 3)
    scored = [c for c in all_clips if "prom" in c]
    print(f"prominence: {len(scored)}/{len(all_clips)} clips scored; {shifted} sections time-shifted; unavailable videos: {sorted(unavailable)}")
    OUT.parent.mkdir(exist_ok=True)
    today = __import__("datetime").date.today().isoformat()
    FULL.write_text(json.dumps({"generated": today, "sections": sections, "clips": all_clips, "hooks": all_hooks}, indent=0))
    OUT.write_text(json.dumps({"generated": today, "sections": sections, "hooks": all_hooks}, separators=(",", ":")))
    print(f"sections: {len(sections)}  clips: {len(all_clips)} (data/clips_full.json)  hooks: {len(all_hooks)} -> {OUT.relative_to(HERE.parent)} ({OUT.stat().st_size // 1024} KB)")
    first = collections.Counter(h["firstInterval"] for h in all_hooks if h["phrase"] == 0 and sections[h["sectionKey"]]["section"].startswith("Chorus"))
    print("chorus first-phrase hooks by first interval:", {k: first[k] for k in INTERVALS.values() if first[k]})
    for name, why in skipped:
        print(f"  skipped {name}: {why}")
    by_int = collections.Counter(c["interval"] for c in all_clips)
    good = collections.Counter(c["interval"] for c in all_clips if c["score"] >= 0.5)
    print("per interval (all / score>=0.5):", {k: f"{by_int[k]}/{good[k]}" for k in INTERVALS.values()})
    for k, s in sections.items():
        print(f"  {k:55s} {s['key']:12s} bpm {s['bpm']:6} (tab {s['nominalBpm']}) {s['timing']:8s} range {s['melodyRange']}")

if __name__ == "__main__":
    main()
