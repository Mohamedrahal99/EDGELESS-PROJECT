#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
driver.py — EDGELESS agent driver.

Drives an LLM conversation through an EDGELESS workflow:
  * POSTs each prompt to the http-ingress          (http://INGRESS/?wf_id=<SID>)
  * exposes an HTTP server that receives the answer back from the platform
    (http-poster POSTs to  http://THIS_HOST:PORT/answer/<tag>)
  * collects application metrics: per-round latency and MAKESPAN.

One driver process models ONE entity (e.g. the Digital Twin of a machine) and
owns ONE workflow = ONE session. Run several processes (or --sessions N) for an
aggregate.

Typical use:
    # single session
    ./driver.py --usecase pdm --rounds 20 --timing human
"""

import argparse
import csv
import json
import os
import queue
import random
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import usecases


def _flat(s, limit):
    """Collapse whitespace and truncate, so the CSV stays one row per round."""
    if not s:
        return ""
    return " ".join(s.split())[:limit]


# ==========================================================================
# answer receiver: the HTTP server the platform posts answers back to
# ==========================================================================
ANSWER_QUEUES = {}          # tag -> queue.Queue
ANSWER_LOCK = threading.Lock()


class AnswerHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode("utf-8", "replace")
        recv_t = time.perf_counter()
        # path is /answer/<tag>  (each workflow's poster has its own URL)
        m = re.match(r"^/answer/([^/]+)/?$", self.path)
        tag = m.group(1) if m else "default"
        with ANSWER_LOCK:
            q = ANSWER_QUEUES.get(tag)
        if q is not None:
            q.put((recv_t, body))
        else:
            print(f"[warn] answer for unknown tag '{tag}' ({n} B)", flush=True)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"edgeless agent driver: answer endpoint alive\n")

    def log_message(self, *a):
        pass


def start_answer_server(port):
    srv = ThreadingHTTPServer(("0.0.0.0", port), AnswerHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv

# ==========================================================================
# workflow control (edgeless_cli)
# ==========================================================================
def render_workflow(template_path, out_path, answer_url, model, agent_class=None):
    """Write a per-session workflow whose poster points back at us.

    When the session has an agent class
    """
    with open(template_path) as f:
        wf = json.load(f)
    cls = agent_class.name if agent_class is not None else None

    for fn in wf.get("functions", []):
        if cls:
            fn.setdefault("annotations", {})["agent_class"] = cls
    for r in wf.get("resources", []):
        if r.get("class_type") in ("http-poster", "http-egress"):
            r.setdefault("configurations", {})["url"] = answer_url
        if r.get("class_type") == "ollama" and model:
            r.setdefault("configurations", {})["model"] = model
        if cls:
            r.setdefault("configurations", {})["agent_class"] = cls

    with open(out_path, "w") as f:
        json.dump(wf, f, indent=2)
    return out_path


def workflow_start(cli, wf_path):
    out = subprocess.run([cli, "workflow", "start", wf_path],
                         capture_output=True, text=True)
    sid = out.stdout.strip().splitlines()[-1].strip() if out.stdout.strip() else ""
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", sid):
        raise SystemExit(f"workflow start failed:\nstdout={out.stdout}\nstderr={out.stderr}")
    return sid


def workflow_stop(cli, sid):
    subprocess.run([cli, "workflow", "stop", sid],
                   capture_output=True, text=True)


# ==========================================================================
# one session
# ==========================================================================
def run_session(args, tag, uc, ingress, sid, rounds_writer):
    q = queue.Queue()
    with ANSWER_LOCK:
        ANSWER_QUEUES[tag] = q

    url = f"http://{ingress}/?wf_id={sid}"
    records = []
    first_send = None
    last_recv = None

    for task in uc.tasks():
        if args.rounds and task["idx"] >= args.rounds:
            break
        prompt = uc.render(task)
        # PROMPT_MARKER lets ConversationMem distinguish a request from an LLM
        # answer structurally, so a lost answer cannot desynchronise the session
        data = ("###PROMPT\n" + prompt).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Host": args.host_header, "Content-Type": "text/plain"})

        attempts = 0
        max_attempts = 1 + (args.max_retries if args.on_timeout == "retry" else 0)
        answer, t_recv, timed_out, platform_error = "", None, False, ""

        while attempts < max_attempts:
            attempts += 1
            t_send = time.perf_counter()
            if first_send is None:
                first_send = t_send
            try:
                with urllib.request.urlopen(req, timeout=args.post_timeout) as r:
                    r.read()
                posted = True
            except Exception as e:                              # noqa: BLE001
                print(f"[{tag}] POST failed on round {task['idx']}: {e}", flush=True)
                posted = False

            answer, t_recv, timed_out, platform_error = "", None, False, ""
            if posted:
                try:
                    t_recv, answer = q.get(timeout=args.answer_timeout)
                    # the function propagates a failed round as ###ERROR ...
                    if answer.startswith("###ERROR"):
                        platform_error = answer.strip()
                        answer, t_recv, timed_out = "", None, True
                        print(f"[{tag}] round {task['idx']}: platform reported "
                              f"{platform_error}", flush=True)
                except queue.Empty:
                    timed_out = True
                    print(f"[{tag}] round {task['idx']}: TIMEOUT after "
                          f"{args.answer_timeout}s (attempt {attempts}/{max_attempts})",
                          flush=True)
            else:
                timed_out = True

            if not timed_out:
                break
            if attempts < max_attempts:
                # drain any late answer so it cannot be mistaken for the retry's
                while True:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break
                gap = random.uniform(0.5 * args.retry_backoff,
                                     1.5 * args.retry_backoff)
                print(f"[{tag}] retrying round {task['idx']} in {gap:.1f}s",
                      flush=True)
                time.sleep(gap)

        if timed_out:
            # A late answer may still arrive after we gave up. Discard it, or the
            # NEXT round pops a stale response: that yields a negative latency and
            # misattributes every following row in this session.
            drained, deadline = 0, time.perf_counter() + 2.0
            while time.perf_counter() < deadline:
                try:
                    q.get(timeout=0.2)
                    drained += 1
                    deadline = time.perf_counter() + 2.0
                except queue.Empty:
                    break
            if drained:
                print(f"[{tag}] drained {drained} late answer(s)", flush=True)

        latency = (t_recv - t_send) if t_recv else None
        if latency is not None and latency < 0:
            # belt and braces: a stale answer can never enter the results
            print(f"[{tag}] round {task['idx']}: negative latency, stale answer "
                  f"discarded", flush=True)
            latency, answer, timed_out = None, "", True
        if t_recv:
            last_recv = t_recv

        rec = {
            "session": tag,
            "wf_id": sid,
            "round": task["idx"],
            "intent": task.get("intent", task.get("doc", "")),
            "prompt_bytes": len(data),
            "answer_bytes": len(answer),
            "latency_s": round(latency, 4) if latency else "",
            "timed_out": int(timed_out),
            "attempts": attempts,
            "error": platform_error,
            "t_send_unix": round(time.time() - (time.perf_counter() - t_send), 3),
            # instrumentation queries probe conversational state rather than
            # represent operator behaviour: excluded from headline results
            "agent_class": uc.meta.get("agent_class", {}).get("class", "uniform"),
            "instrumentation": int(task.get("instrumentation", False)),
            "sim_hour": task.get("hour", ""),
            "event": task.get("event", ""),
            "prompt_tokens_est": usecases.estimate_tokens(prompt),
            "answer_tokens_est": usecases.estimate_tokens(answer) if answer else 0,
            # the actual texts, so responses can be inspected without the platform log
            "prompt": _flat(prompt, args.text_chars),
            "answer": _flat(answer, args.text_chars),
        }
        records.append(rec)
        rounds_writer(rec)

        # full, untruncated answer as its own file (easy to read / quote)
        if answer and args.save_answers:
            adir = os.path.join(args.out_dir, "answers")
            os.makedirs(adir, exist_ok=True)
            with open(os.path.join(adir, f"{tag}_r{task['idx']:03d}.txt"), "w") as af:
                af.write(f"# intent: {rec['intent']}\n"
                         f"# latency_s: {rec['latency_s']}\n"
                         f"# prompt_bytes: {rec['prompt_bytes']}\n\n"
                         f"--- PROMPT ---\n{prompt}\n\n--- ANSWER ---\n{answer}\n")

        lat_s = f"{latency:.2f}s" if latency else "  --  "
        print(f"[{tag}] round {task['idx']:>3} {rec['intent']:<18} "
              f"lat={lat_s} in={rec['prompt_bytes']}B out={rec['answer_bytes']}B",
              flush=True)
        if args.show_answers and answer:
            snippet = " ".join(answer.split())[:args.show_chars]
            print(f"       > {snippet}{'...' if len(answer) > args.show_chars else ''}",
                  flush=True)

        if timed_out and args.on_timeout == "abort":
            print(f"[{tag}] aborting session after round {task['idx']} "
                  f"({attempts} attempts)", flush=True)
            break

        # inter-round timing
        gap = uc.unit_pause() if uc.is_unit_end(task) else uc.think_time(task)
        if gap > 0:
            time.sleep(gap)

    makespan = (last_recv - first_send) if (first_send and last_recv) else None
    lats = [r["latency_s"] for r in records if r["latency_s"] != ""]
    work_lats = [r["latency_s"] for r in records
                 if r["latency_s"] != "" and not r["instrumentation"]]
    summary = {
        "session": tag,
        "wf_id": sid,
        "agent_class": uc.meta.get("agent_class", {}).get("class", "uniform"),
        "rounds": len(records),
        "completed": len(lats),
        "timeouts": sum(r["timed_out"] for r in records),
        "retried_rounds": sum(1 for r in records if r["attempts"] > 1),
        "aborted": bool(records and records[-1]["timed_out"]
                        and args.on_timeout == "abort"),
        "makespan_s": round(makespan, 3) if makespan else None,
        "latency_mean_s": round(statistics.fmean(lats), 3) if lats else None,
        "latency_p50_s": round(statistics.median(lats), 3) if lats else None,
        "latency_p95_s": (round(sorted(lats)[int(0.95 * (len(lats) - 1))], 3)
                          if lats else None),
        "latency_min_s": round(min(lats), 3) if lats else None,
        "latency_max_s": round(max(lats), 3) if lats else None,
        "workload_only_latency_mean_s": (round(statistics.fmean(work_lats), 3)
                                         if work_lats else None),
        "workload_only_rounds": len(work_lats),
        "synthetic": bool(uc.meta.get("synthetic", False)),
        "usecase_meta": uc.meta,
    }
    return summary

def main():
    p = argparse.ArgumentParser(description="EDGELESS agent driver")
    # what to run
    p.add_argument("--usecase", default="pdm", choices=list(usecases.REGISTRY))
    p.add_argument("--rounds", type=int, default=0,
                   help="optional hard cap on rounds (0 = use the use case's own "
                        "horizon: pdm -> --horizon-hours/--until-failure, "
                        "summarizer -> the whole corpus)")
    p.add_argument("--sessions", type=int, default=1,
                   help="concurrent sessions (each = its own workflow)")
    p.add_argument("--seed", type=int, default=42)
    # timing model
    p.add_argument("--timing", default="human", choices=["human", "closed_loop"])
    p.add_argument("--think-mean", type=float, default=4.0)
    p.add_argument("--think-cv", type=float, default=1.5)
    # data
    p.add_argument("--data-dir", default=".")
    p.add_argument("--agent-classes", action="store_true",
                   help="assign each session a class at random (mice = short "
                        "rounds, elephants = long rounds); without this flag "
                        "every session uses the uniform workload as before")
    p.add_argument("--elephant-frac", type=float, default=0.5,
                   help="probability that a session is an elephant (default 0.5)")
    p.add_argument("--alternate-classes", action="store_true",
                   help="assign classes alternately (mouse, elephant, ...) so "
                        "every run has an identical, balanced composition")
    p.add_argument("--allow-synthetic", action="store_true",
                   help="PIPELINE TEST ONLY: run without the real dataset; "
                        "results are tagged synthetic=True and must not be reported")
    # --- predictive-maintenance digital twin ---
    p.add_argument("--machine-id", type=int, default=1)
    p.add_argument("--window-hours", type=int, default=6,
                   help="hours of telemetry included in each query")
    p.add_argument("--from-hour", type=int, default=0)
    p.add_argument("--horizon-hours", type=int, default=120,
                   help="simulated hours to cover (defines the makespan horizon)")
    p.add_argument("--until-failure", action="store_true",
                   help="run until the machine's first recorded failure instead")
    p.add_argument("--query-every-hours", type=int, default=6,
                   help="simulated hours between scheduled queries")
    p.add_argument("--time-compression", type=float, default=600.0,
                   help="simulated seconds per real second (600 = 1 sim hour / 6 s)")
    p.add_argument("--trend-every", type=int, default=4,
                   help="instrumentation: trend query every N scheduled queries (0=off)")
    p.add_argument("--recall-every", type=int, default=0,
                   help="instrumentation: memory-recall probe every N queries (0=off)")
    # platform
    p.add_argument("--ingress", default="127.0.0.1:7007")
    p.add_argument("--host-header", default="demo.edgeless.com")
    p.add_argument("--answer-host", default="127.0.0.1",
                   help="address the PLATFORM should post answers to")
    p.add_argument("--answer-port", type=int, default=9000)
    p.add_argument("--cli", default="target/release/edgeless_cli")
    p.add_argument("--workflow", default="workflows/step1_poster_workflow.json",
                   help="template; its poster/egress url is rewritten per session")
    p.add_argument("--model", default="", help="override the ollama model")
    p.add_argument("--no-workflow-control", action="store_true",
                   help="do not start/stop workflows; use --wf-id instead")
    p.add_argument("--wf-id", default="", help="use an already-running workflow")
    # timeouts / output
    p.add_argument("--post-timeout", type=float, default=30.0)
    p.add_argument("--answer-timeout", type=float, default=180.0,
                   help="how long to wait for the platform to return an answer")
    # --- failure policy (ollama timeouts) ---
    p.add_argument("--on-timeout", default="retry",
                   choices=["retry", "skip", "abort"],
                   help="retry: try the round again; skip: record and continue; "
                        "abort: stop the session and record the error")
    p.add_argument("--max-retries", type=int, default=2,
                   help="attempts after the first failure, when --on-timeout=retry")
    p.add_argument("--retry-backoff", type=float, default=10.0,
                   help="mean seconds before a retry (randomised in [0.5x, 1.5x])")
    p.add_argument("--out-dir", default="results")
    p.add_argument("--show-answers", action="store_true", default=True,
                   help="print a snippet of each answer as it arrives (default on)")
    p.add_argument("--quiet-answers", dest="show_answers", action="store_false",
                   help="do not print answer snippets")
    p.add_argument("--show-chars", type=int, default=160,
                   help="characters of the answer to print per round")
    p.add_argument("--text-chars", type=int, default=4000,
                   help="max characters of prompt/answer stored in the CSV")
    p.add_argument("--save-answers", action="store_true", default=True,
                   help="also write each full answer to <out-dir>/answers/ (default on)")
    p.add_argument("--no-save-answers", dest="save_answers", action="store_false")
    p.add_argument("--run-id", default="")
    args = p.parse_args()

    run_id = args.run_id or f"{args.usecase}_{args.timing}_{time.strftime('%Y%m%d-%H%M%S')}"
    os.makedirs(args.out_dir, exist_ok=True)
    rounds_path = os.path.join(args.out_dir, f"{run_id}_rounds.csv")
    summary_path = os.path.join(args.out_dir, f"{run_id}_summary.json")

    srv = start_answer_server(args.answer_port)
    print(f"answer server on :{args.answer_port}  (platform posts to "
          f"http://{args.answer_host}:{args.answer_port}/answer/<tag>)", flush=True)

    fields = ["session", "wf_id", "agent_class", "round", "intent", "instrumentation",
              "sim_hour", "event", "prompt_bytes", "answer_bytes",
              "prompt_tokens_est", "answer_tokens_est",
              "latency_s", "timed_out", "attempts", "error",
              "t_send_unix", "prompt", "answer"]
    fcsv = open(rounds_path, "w", newline="")
    writer = csv.DictWriter(fcsv, fieldnames=fields)
    writer.writeheader()
    wlock = threading.Lock()

    def write_row(rec):
        with wlock:
            writer.writerow(rec)
            fcsv.flush()

    sessions, summaries, threads = [], [], []
    tmpdir = os.path.join(args.out_dir, f".{run_id}_wf")
    os.makedirs(tmpdir, exist_ok=True)

    try:
        for i in range(args.sessions):
            tag = f"s{i}"
            rng = random.Random(args.seed + i)
            # Each agent is assigned to a category. With --alternate-classes the
            # split is deterministic (mouse, elephant, mouse, ...)
            if args.agent_classes and args.alternate_classes:
                n_eleph = round(args.elephant_frac * args.sessions)
                is_eleph = (i * n_eleph) % max(args.sessions, 1) < n_eleph
                agent_class = usecases.ELEPHANT if is_eleph else usecases.MOUSE
            elif args.agent_classes:
                agent_class = usecases.assign_agent_class(rng, args.elephant_frac)
            else:
                agent_class = None
            uc = usecases.build(args.usecase, args, rng, session=i,
                                agent_class=agent_class)
            answer_url = f"http://{args.answer_host}:{args.answer_port}/answer/{tag}"

            if args.no_workflow_control:
                sid = args.wf_id
                if not sid:
                    raise SystemExit("--no-workflow-control requires --wf-id")
                print(f"[{tag}] using running workflow {sid}; ensure its "
                      f"poster/egress url is {answer_url}", flush=True)
            else:
                wf_path = render_workflow(args.workflow,
                                          os.path.join(tmpdir, f"wf_{tag}.json"),
                                          answer_url, args.model, agent_class)
                sid = workflow_start(args.cli, wf_path)
                cls = uc.meta.get("agent_class", {}).get("class", "uniform")
                print(f"[{tag}] workflow {sid} started  [{cls}]", flush=True)
            sessions.append((tag, sid, uc))

        if any(u.meta.get("synthetic") for (_, _, u) in sessions):
            print("\n" + "!" * 72)
            print("!! SYNTHETIC DATA — PIPELINE TEST ONLY. Results are tagged")
            print("!! synthetic=True and must NOT be reported as measurements.")
            print("!" * 72 + "\n", flush=True)

        time.sleep(15)  # let the instances settle

        for (tag, sid, uc) in sessions:
            t = threading.Thread(
                target=lambda tg=tag, s=sid, u=uc:
                    summaries.append(run_session(args, tg, u, args.ingress, s, write_row)),
                daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

    finally:
        if not args.no_workflow_control:
            for (_, sid, _) in sessions:
                workflow_stop(args.cli, sid)
        fcsv.close()
        srv.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)

    agg_makespan = max([s["makespan_s"] for s in summaries if s["makespan_s"]],
                       default=None)
    out = {
        "run_id": run_id,
        "config": vars(args),
        "aggregate_makespan_s": agg_makespan,
        "sessions": summaries,
    }
    with open(summary_path, "w") as f:
        json.dump(out, f, indent=2)

    print("\n================ RESULT ================")
    # Per-class breakdown: with --agent-classes this is the point of the run,
    # since mice and elephants are expected to have very different round times.
    by_class = {}
    for s_ in summaries:
        by_class.setdefault(s_.get("agent_class", "uniform"), []).append(s_)
    if len(by_class) > 1:
        for cls in sorted(by_class):
            rows = by_class[cls]
            p50s = [r["latency_p50_s"] for r in rows if r.get("latency_p50_s")]
            mks = [r["makespan_s"] for r in rows if r.get("makespan_s")]
            if p50s and mks:
                print(f"  {cls:<9} sessions={len(rows):<3} "
                      f"mean p50={statistics.fmean(p50s):6.2f}s  "
                      f"mean makespan={statistics.fmean(mks):7.1f}s")
            else:
                print(f"  {cls:<9} sessions={len(rows)}")
        print()
    for s in summaries:
        print(f"  {s['session']}: makespan={s['makespan_s']}s  "
              f"rounds={s['completed']}/{s['rounds']}  "
              f"p50={s['latency_p50_s']}s p95={s['latency_p95_s']}s  "
              f"timeouts={s['timeouts']}")
    print(f"  aggregate makespan: {agg_makespan}s")
    print(f"\n  rounds : {rounds_path}\n  summary: {summary_path}")


if __name__ == "__main__":
    sys.exit(main())
