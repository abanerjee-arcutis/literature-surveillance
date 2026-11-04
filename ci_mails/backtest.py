"""Score the L1 pool and the emailed articles with the L3 reasons (L3_INTENTS and L3_PUSH_DOWN in surveillance.py),
and compare with the priority rule the daily job used before them.

    python backtest.py [--list-misses] [--list-false] [--gaps]

--list-misses  emailed articles scoring below 60
--list-false   pool articles not emailed that score 60+ (over-ranked, or articles the reviewer may have missed)
--gaps         emailed articles the L1 terms don't find, by their best reason (what L1 is missing)
"""
import collections
import os
import sys

from common import ROOT, funding_text, load, study, sv

WINDOW = ("2025-12-20", "2026-10-10")   # an unemailed article dated outside the emails' months can't count against them

emails, pool, flagged = load()
lit = [a for a in emails if not a.get("not_literature")]
term_cat = {t["term"]: t["product_category"] for t in sv.clean_terms(sv.read_csv(os.path.join(ROOT, "terms.csv")))}
rules = sv.read_csv(os.path.join(ROOT, "exclusions.csv"))


def score(r):
    """(L3 score, ids of the reasons matched, best first, ids of the push-down rules fired), as the daily job scores
    the article without L3 keywords from Settings (none are saved)."""
    s, hits, pushed = sv.l3_score(sv.DEFAULT_L3_RULES, r["title"], r.get("abstract"), study(r), funding_text(r))
    return s, [h["id"] for h in hits], [p["id"] for p in pushed]


def old_priority(r):
    """The priority the daily job gave before the L3 score: High, Medium, Check or Low."""
    cats = {term_cat.get(t, "Class") for t in r.get("matched_terms") or []}
    product = "Roflumilast" if "Roflumilast" in cats else "Competitor" if "Competitor" in cats else "Class"
    study_type = study(r)
    if sv.apply_rules(rules, r["title"], r.get("abstract")):
        return "Low"
    if not r.get("abstract"):
        return "Check"
    if study_type == "Preclinical":
        return "Medium" if product == "Roflumilast" else "Low"
    if product == "Roflumilast" or (product == "Competitor" and study_type in ("Phase 3", "Phase 2", "RCT", "Real-world", "Meta-analysis")):
        return "High"
    return "Low" if study_type in ("Letter", "Editorial") else "Medium"


def auc(pos, neg):
    """Chance that an emailed article outranks an unemailed one (ties count half)."""
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


rows = []
for k, r in pool.items():
    a = flagged.get(k)
    if not a and r.get("pub_date") and not (WINDOW[0] <= r["pub_date"] <= WINDOW[1]):
        continue
    s, m, pen = score(r)
    rows.append(dict(key=k, title=r["title"], score=s, intents=m, pen=pen,
                     flagged=bool(a), tier=a["tier"] if a else None, cur=old_priority(r)))
P = [x for x in rows if x["flagged"]]
N = [x for x in rows if not x["flagged"]]
cur_rank = {"High": 3, "Medium": 2, "Check": 1, "Low": 0}
print(f"L1 pool {len(rows)}: emailed {len(P)} (main {sum(x['tier'] == 'main' for x in P)}, "
      f"fyi {sum(x['tier'] == 'fyi' for x in P)}), not emailed {len(N)}")
print(f"ranking AUC: L3 score {auc([x['score'] for x in P], [x['score'] for x in N]):.3f}, "
      f"old priority {auc([cur_rank[x['cur']] for x in P], [cur_rank[x['cur']] for x in N]):.3f}")
for name, key in (("L3 score", lambda x: x["score"]), ("old", lambda x: cur_rank[x["cur"]])):
    ranked = sorted(rows, key=lambda x: -key(x))
    for k in (50, 100, 200, 300):
        f = sum(x["flagged"] for x in ranked[:k])
        print(f"  {name:8s} top {k:3d}: {f:3d} emailed ({f / len(P):.0%} of emailed)")
bands = [band for _, band in sv.L3_BANDS]
print("\nL3 priority (L1 pool):")
for b in bands:
    xs = [x for x in rows if sv.l3_band(x["score"]) == b]
    f = sum(x["flagged"] for x in xs)
    print(f"  {b:12s} {len(xs):4d} articles, {f:3d} emailed ({f / max(1, len(xs)):.0%}); "
          f"main {sum(x['tier'] == 'main' for x in xs)}, fyi {sum(x['tier'] == 'fyi' for x in xs)}")
print("\nold priority (L1 pool):")
for b in ("High", "Medium", "Check", "Low"):
    xs = [x for x in rows if x["cur"] == b]
    f = sum(x["flagged"] for x in xs)
    print(f"  {b:12s} {len(xs):4d} articles, {f:3d} emailed ({f / max(1, len(xs)):.0%})")
print("\nper reason (L1 pool): matched, emailed among matched, share of all emailed it catches")
for it in sv.L3_INTENTS:
    xs = [x for x in rows if it["intent_id"] in x["intents"]]
    f = sum(x["flagged"] for x in xs)
    print(f"  {it['intent_id']:20s} w{it['weight']:3d}  {len(xs):4d}  {f:3d} ({f / max(1, len(xs)):.0%})  {f / len(P):.0%}")
# every emailed article, inside the pool or not, scored on what a future run would see (title + abstract)
allpos = [(*score(a["rec"]), a) for a in lit]
print(f"\nall {len(lit)} emailed articles: " + ", ".join(f"{b} {sum(1 for s, *_ in allpos if sv.l3_band(s) == b)}" for b in bands))
print("  main tier: " + ", ".join(f"{b} {sum(1 for s, *_, a in allpos if a['tier'] == 'main' and sv.l3_band(s) == b)}" for b in bands))
if "--list-misses" in sys.argv:
    print("\nemailed articles scoring below 60:")
    for s, m, pen, a in sorted(allpos, key=lambda x: x[0]):
        if s < 60:
            print(f"  {s:3d} {a['tier']:4s} {'L1' if a.get('pool_key') else '--'} {m} {pen} | {a['rec']['title'][:110]}")
if "--list-false" in sys.argv:
    print("\npool articles not emailed that score 60+:")
    for x in sorted(N, key=lambda x: -x["score"]):
        if x["score"] >= 60:
            print(f"  {x['score']:3d} {x['intents']} {x['pen']} | {x['title'][:120]}")
if "--gaps" in sys.argv:
    groups = collections.defaultdict(list)
    for s, m, pen, a in allpos:
        if not a.get("pool_key"):
            groups[m[0] if m else "(no reason)"].append((s, a))
    print(f"\nemailed articles the L1 terms don't find: {sum(len(v) for v in groups.values())}")
    for g, xs in sorted(groups.items(), key=lambda x: -len(x[1])):
        print(f"  {g}: {len(xs)}")
        for s, a in xs:
            print(f"     {a['tier']:4s} {s:3d} | {a['rec']['title'][:100]}")
