#!/usr/bin/env python3
import csv, glob, os, collections, statistics

HERE = os.path.dirname(os.path.abspath(__file__))
TAIL = 25.0

def path(run, kind): return os.path.join(HERE, f"{run}_{kind}")

def window(run):
    ts = [float(r["t_send_unix"]) for r in csv.DictReader(open(path(run,"rounds.csv")))
          if r.get("t_send_unix")]
    return (min(ts), max(ts)+TAIL) if ts else None

def energy_j(run):
    w = window(run)
    if not w: return None
    lo, hi = w
    rows = collections.defaultdict(list)
    for r in csv.DictReader(open(path(run,"nodes.csv"))):
        try: t,p = int(r["unix_ts"]), int(r["active_power_mw"])
        except (ValueError,KeyError): continue
        if p>=0 and lo<=t<=hi: rows[r["node"]].append((t,p))
    tot=0.0
    for v in rows.values():
        v.sort()
        for (t0,p0),(t1,_) in zip(v,v[1:]):
            if 0<t1-t0<30: tot += (p0/1000.0)*(t1-t0)
    return tot

def tokens(run):
    """prompt+answer tokens summed over every completed round."""
    tot=0
    for r in csv.DictReader(open(path(run,"rounds.csv"))):
        try:
            tot += int(r.get("prompt_tokens_est") or 0) + int(r.get("answer_tokens_est") or 0)
        except ValueError: pass
    return tot

jpt = collections.defaultdict(list)
etot = collections.defaultdict(list)
for f in sorted(glob.glob(os.path.join(HERE,"load_*_summary.json"))):
    run = os.path.basename(f).replace("_summary.json","")
    if not (os.path.exists(path(run,"rounds.csv")) and os.path.exists(path(run,"nodes.csv"))): continue
    parts = run.split("_")
    if len(parts)!=4: continue
    _,pol,n,rep = parts
    e = energy_j(run); tk = tokens(run)
    if not e or not tk: continue
    key=(pol,int(n[1:]))
    jpt[key].append(e/tk)
    etot[key].append(e/1000)

print(f"{'policy':<12}{'N':>4}{'runs':>5}{'J/token':>12}{'total kJ':>12}")
print("-"*45)
for k in sorted(jpt, key=lambda x:(x[1],x[0])):
    j=jpt[k]; e=etot[k]
    sd=statistics.stdev(j) if len(j)>1 else 0
    print(f"{k[0]:<12}{k[1]:>4}{len(j):>5}{statistics.fmean(j):>9.2f} ±{sd:<3.2f}{statistics.fmean(e):>10.1f}")

# relative to the best stock policy per N
print("\nIntelligent J/token vs best stock policy:")
for n in sorted({k[1] for k in jpt}):
    stock={k[0]:statistics.fmean(jpt[k]) for k in jpt if k[1]==n and k[0]!="Intelligent"}
    intel=[statistics.fmean(jpt[k]) for k in jpt if k==("Intelligent",n)]
    if stock and intel:
        best=min(stock.values())
        d=(intel[0]-best)/best*100
        print(f"  N={n:<3} Intelligent {intel[0]:.2f}  best stock {best:.2f}  -> {d:+.0f}%")
