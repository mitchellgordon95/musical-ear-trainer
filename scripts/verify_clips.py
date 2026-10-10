#!/usr/bin/env python3
"""Listen to the audio: (1) correct each section's timing, (2) score how clearly the
transcribed melody stands out -> data/prominence.json

Mitchell (2026-09-02): the bad clips are the ones where several lines harmonize and
"it's impossible to tell what the melodic line I'm supposed to be following is"
(Bohemian Rhapsody); the slider was also "hard to line up with the beats". Theorytab
can't see either; the audio can.

Per video (audio fetched locally with yt-dlp, never committed):
  * Constant-Q magnitude spectrogram, 1/3-semitone bins from C2, ~23 ms hops. Harmonic
    salience folds harmonics 1-4 (weights 0.8^(h-1)) onto each candidate fundamental.
Per section:
  * Offset search: slide the whole transcription -0.5..+0.5 s and keep the offset that
    maximizes mean salience at the transcribed pitches (Theorytab sync is ~±0.2 s).
    Applied by mine_moments.py when it helps by > 0.03.
Per transcribed note (middle 60% of its frames, at the corrected time):
  * dominance = S(target) / (S(target) + S(strongest competing pitch within ±9 st,
    excluding the target's own ±1.5 st and its octave)), 70th percentile over frames.
    ~0.9 = the line stands alone in its register; ~0.5 = another pitch is just as loud
    (close harmony, a chord tone, a doubled instrument).
Clip prom = min over its two notes. Also promOct (octave-agnostic target).

Run: .venv/bin/python scripts/verify_clips.py [--force]
"""
import json, pathlib, sys
import numpy as np, librosa, soundfile as sf

HERE = pathlib.Path(__file__).resolve().parent
CODE = HERE.parent
CLIPS = json.loads((CODE / "data" / "clips_full.json").read_text())   # written by mine_moments.py (the public file has no clips)
AUDIO = CODE / "data" / "audio"
OUT = CODE / "data" / "prominence.json"

SR, HOP, BPO = 22050, 512, 36
FMIN_MIDI = 36                        # C2
N_BINS = 5 * BPO                      # C2..C7
HARMONICS = [(h, 0.8 ** (h - 1)) for h in range(1, 5)]
OFFSETS = np.arange(-0.5, 0.501, 0.025)

def salience_map(y):
    C = np.abs(librosa.cqt(y, sr=SR, hop_length=HOP, fmin=librosa.midi_to_hz(FMIN_MIDI), n_bins=N_BINS + 2 * BPO, bins_per_octave=BPO))
    S = np.zeros((N_BINS, C.shape[1]), dtype=np.float32)
    for h, w in HARMONICS:
        shift = int(round(BPO * np.log2(h)))
        S += w * C[shift:shift + N_BINS]
    return S

def frames_for(S, on, off):
    d = off - on
    f0, f1 = int((on + 0.2 * d) * SR / HOP), int((off - 0.2 * d) * SR / HOP)
    f0, f1 = max(0, f0), min(S.shape[1], max(f1, f0 + 2))
    return S[:, f0:f1] if f1 > f0 else None

def band(frames, midi):
    b = (midi - FMIN_MIDI) * 3
    if b < 1 or b >= N_BINS - 1:
        return np.zeros(frames.shape[1], dtype=np.float32)
    return frames[b - 1:b + 2].max(axis=0)

def target_ratio(S, midi, on, off):
    """salience at the transcribed pitch / strongest pitch within ±9 st — used for the offset search"""
    fr = frames_for(S, on, off)
    if fr is None: return None
    b = (midi - FMIN_MIDI) * 3
    lo, hi = max(0, b - 27), min(N_BINS, b + 28)
    if lo >= hi: return None
    return float(np.median(band(fr, midi) / (fr[lo:hi].max(axis=0) + 1e-9)))

def dominance(S, midi, on, off, octave_agnostic=False):
    fr = frames_for(S, on, off)
    if fr is None: return None
    tgt = band(fr, midi)
    if octave_agnostic:
        tgt = np.maximum.reduce([tgt, band(fr, midi - 12), band(fr, midi + 12)])
    b = (midi - FMIN_MIDI) * 3
    mask = np.zeros(N_BINS, dtype=bool)
    mask[max(0, b - 27):min(N_BINS, b + 28)] = True
    for c in (b, b - 36, b + 36):                       # exclude the target and its octaves
        mask[max(0, c - 4):min(N_BINS, c + 5)] = False
    comp = fr[mask].max(axis=0) if mask.any() else np.zeros(fr.shape[1])
    return float(np.percentile(tgt / (tgt + comp + 1e-9), 70))

def best_offset(S, notes):
    scores = []
    for d in OFFSETS:
        r = [target_ratio(S, n["midi"], n["on"] + d, n["off"] + d) for n in notes]
        r = [x for x in r if x is not None]
        scores.append(np.mean(r) if r else 0)
    i = int(np.argmax(scores)); zero = int(np.argmin(np.abs(OFFSETS)))
    return float(OFFSETS[i]), float(scores[zero]), float(scores[i])

def main():
    result = json.loads(OUT.read_text()) if OUT.exists() and "--force" not in sys.argv else {}
    result.setdefault("sections", {}); result.setdefault("clips", {})
    by_video = {}
    for k, sec in CLIPS["sections"].items():
        by_video.setdefault(sec["youtubeId"], []).append(k)
    for yt, keys in sorted(by_video.items()):
        wav = AUDIO / f"{yt}.wav"
        if not wav.exists():
            print(f"{yt}: no audio"); continue
        if all(k in result["sections"] for k in keys):
            continue
        y, sr = sf.read(wav, dtype="float32")
        if y.ndim > 1: y = y.mean(axis=1)
        if sr != SR: y = librosa.resample(y, orig_sr=sr, target_sr=SR)
        S = salience_map(y)
        for k in keys:
            sec = CLIPS["sections"][k]
            notes = [n for n in sec["notes"]]
            # undo any offset mine_moments already applied so the search is from the raw sync
            raw = sec.get("offset", 0.0)
            notes = [{"midi": n["midi"], "on": n["on"] - raw, "off": n["off"] - raw} for n in notes]
            if not notes:
                continue
            off, base, best = best_offset(S, notes)
            result["sections"][k] = {"offset": round(off, 3), "ratioAtZero": round(base, 3), "ratioAtBest": round(best, 3)}
            use = off if best - base > 0.03 else 0.0
            proms = []
            for c in [c for c in CLIPS["clips"] if c["sectionKey"] == k]:
                a, b = c["notes"]
                onA, onB, end = c["onsetA"] - raw + use, c["onsetB"] - raw + use, c["end"] - raw + use
                pa, pb = dominance(S, a["midi"], onA, onB), dominance(S, b["midi"], onB, end)
                oa, ob = dominance(S, a["midi"], onA, onB, True), dominance(S, b["midi"], onB, end, True)
                if pa is None or pb is None: continue
                result["clips"][c["id"]] = {"prom": round(min(pa, pb), 3), "promA": round(pa, 3), "promB": round(pb, 3), "promOct": round(min(oa, ob), 3)}
                proms.append(min(pa, pb))
            print(f"{k:52s} offset {off:+.3f}s (ratio {base:.2f}->{best:.2f}{' applied' if use else ''})  clips={len(proms):3d} median prom={np.median(proms) if proms else 0:.2f}")
        OUT.write_text(json.dumps(result))
    print("wrote", OUT.relative_to(CODE), len(result["clips"]), "clips,", len(result["sections"]), "sections")

if __name__ == "__main__":
    main()
