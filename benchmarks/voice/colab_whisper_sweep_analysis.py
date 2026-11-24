"""Figures to quote from colab_whisper_sweep.py output: per-language error rate with a 95% bootstrap interval. Standard library only.

Run in Colab after the sweep (or anywhere with the CSV files):  %run colab_whisper_sweep_analysis.py [folder]   (default: S_OUT or
/content/drive/MyDrive/whisper_sweep). The newest file of each language is used.
Spaced languages: word error rate from the harness columns. Languages without word spaces (ja zh yue th my km lo): character error
rate recomputed here after Unicode NFKC, lower case, and removing spaces and punctuation from both sides (the harness CER counts the
spaces FLEURS puts between Chinese characters). Latin letters in a reference that the model leaves out still count as errors: the
count of clips whose reference has Latin words is printed. Intervals: 2000 resamples of clips, seed 1234 (the pilot's method); the
clips are consecutive, so the intervals are probably too narrow. This normalisation is not the harness's normalizer version 1.
It prints proposed stt.csv rows and writes nothing: check them against ragleap_voice.coverage before committing any.
"""
import csv, glob, os, pathlib, random, re, sys, unicodedata

OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else
                   os.environ.get("S_OUT", "/content/drive/MyDrive/whisper_sweep"))
CHAR = {"ja", "zh", "yue", "th", "my", "km", "lo"}
B = 2000

def squash(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    return "".join(ch for ch in s if not ch.isspace() and unicodedata.category(ch)[0] not in "PZ")

def ed(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]

def interval(pairs):
    rnd = random.Random(1234); vals = []
    for _ in range(B):
        s = [pairs[rnd.randrange(len(pairs))] for _ in pairs]
        vals.append(100.0 * sum(e for e, n in s) / max(1, sum(n for e, n in s)))
    vals.sort()
    return vals[int(0.025 * B)], vals[int(0.975 * B)]

files = {}
for p in sorted(glob.glob(str(OUT / "*whisper-large-v3-sweep-*.csv"))):
    m = re.search(r"sweep-([a-z]+)_\d{8}T", os.path.basename(p))
    if m: files[m.group(1)] = p  # sorted by name, so the newest file of a language wins
print("folder:", OUT, "| languages found:", len(files))
results = []
for code, p in sorted(files.items()):
    rows = list(csv.DictReader(open(p, encoding="utf-8")))
    good = [r for r in rows if not (r.get("error") or "").strip()]
    if not good: print("%s: no usable rows in %s" % (code, os.path.basename(p))); continue
    note = ""
    if code in CHAR:
        pairs, worst = [], []
        for r in good:
            a, b = squash(r["reference"]), squash(r["hypothesis"])
            e = ed(a, b); pairs.append((e, len(a)))
            d = next((i for i in range(min(len(a), len(b))) if a[i] != b[i]), min(len(a), len(b)))
            worst.append((e / max(1, len(a)), r["id"], len(a), len(b), a[d:d + 40], b[d:d + 40]))
        metric = "CER*"
        harness = 100.0 * sum(int(r["char_errors"]) for r in good) / max(1, sum(int(r["ref_chars"]) for r in good))
        latin = sum(1 for r in good if re.search(r"[A-Za-z]{3,}", r["reference"]))
        note = "harness CER %.1f%%; reference has Latin words in %d clips" % (harness, latin)
    else:
        pairs = [(int(r["word_errors"]), int(r["ref_words"])) for r in good]
        metric = "WER"
    value = 100.0 * sum(e for e, n in pairs) / max(1, sum(n for e, n in pairs))
    lo, hi = interval(pairs)
    results.append(dict(code=code, metric=metric, value=value, lo=lo, hi=hi, n=len(good), failed=len(rows) - len(good), note=note))
    if code in CHAR:
        for w in sorted(worst, reverse=True)[:2]:
            print("   %s worst %s (%.0f%%): ref %d chars, output %d chars; from the first difference: ref %r | out %r" %
                  (code, w[1], 100 * w[0], w[2], w[3], w[4], w[5]))
print()
print("%-5s %-5s %7s   %-16s %4s %6s  %s" % ("lang", "metric", "value", "95% interval", "n", "failed", "note"))
for r in sorted(results, key=lambda r: (r["metric"] != "WER", r["value"])):
    print("%-5s %-5s %6.1f%%  [%5.1f, %5.1f] %4d %6d  %s" % (r["code"], r["metric"], r["value"], r["lo"], r["hi"], r["n"], r["failed"], r["note"]))
print("\n* CER after NFKC, lower case, and removing spaces and punctuation from both sides.")
print("\nproposed stt.csv rows (review against ragleap_voice.coverage; nothing is written):")
w = csv.writer(sys.stdout, lineterminator="\n")
w.writerow("language,engine,tier,metric,value,ci_low,ci_high,test_set,n_clips,normalizer_version,raw_committed,evidence,contamination,weights_licence,notes,champion".split(","))
for r in results:
    w.writerow([r["code"], "whisper-large-v3", 2, "CER" if r["metric"] == "CER*" else "WER", "%.1f" % r["value"], "%.1f" % r["lo"], "%.1f" % r["hi"],
                "google/fleurs test, first %d clips" % r["n"], r["n"], "1" if r["metric"] == "WER" else "squash", "no",
                "benchmarks/voice/colab_whisper_sweep.py; benchmarks/voice/colab_whisper_sweep_analysis.py", "", "MIT (model card)",
                ("CER after removing spaces and punctuation (own normalisation, not normalizer 1); " + r["note"]) if r["metric"] == "CER*" else "", ""])
