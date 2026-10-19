#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
Digital Twin of ONE machine. Replays that machine's telemetry at a compressed
cadence, issues a periodic condition query, and escalates (root cause / action
decision) when the dataset's own error and failure records indicate an event.

Data: Microsoft Azure Predictive Maintenance dataset
      PdM_telemetry.csv : machineID, datetime, volt, rotate, pressure, vibration
                          (hourly, 100 machines, one year)
      PdM_errors.csv    : machineID, datetime, errorID  
      PdM_failures.csv  : machineID, datetime, failure
Timing: telemetry cadence, not chat think-time. The dataset is hourly and is
replayed with a compression factor (--time-compression): one simulated hour
takes 3600/compression seconds. Queries are issued every --query-every-hours
simulated hours, plus immediately on a recorded event. A Digital Twin has no
"think time"; using a chat arrival model here would be the wrong family.

Horizon (defines MAKESPAN): the agent processes telemetry from --from-hour for
--horizon-hours simulated hours, or until the machine's first recorded failure
with --until-failure. Makespan is then "time to complete the inspection
campaign for this machine" — a defensible quantity rather than an arbitrary
round count.
"""

import csv
import datetime as dt
import os
import random
import statistics
# when no agent class is used, the default answer spec is a short, generic instruction.
DEFAULT_ANSWER_SPEC = "Answer in at most 3 sentences."

    """Rough token estimate (~4 chars/token). Bytes remain the exact metric;
    this is carried alongside because context limits are token-based."""
def estimate_tokens(text):
    return max(1, len(text) // 4)

    """Raised instead of silently fabricating data."""
class SyntheticDataRefused(SystemExit):

class AgentClass:
    def __init__(self, name, window_hours, answer_spec):
        self.name = name
        self.window_hours = window_hours
        self.answer_spec = answer_spec

    def describe(self):
        return {"class": self.name,
                "window_hours": self.window_hours,
                "answer_spec": self.answer_spec}

    def __repr__(self):
        return f"AgentClass({self.name}, window={self.window_hours}h)"


MOUSE = AgentClass(
    name="mouse",
    window_hours=2,
    answer_spec="Answer in one sentence of at most 20 words.",
)

ELEPHANT = AgentClass(
    name="elephant",
    window_hours=24,
    answer_spec="Answer in at most 8 sentences, giving your reasoning.",
)

AGENT_CLASSES = {"mouse": MOUSE, "elephant": ELEPHANT}

    """Draw a class for one agent. Uses the session's seeded RNG, so the same
    --seed reproduces the same mice/elephant assignment."""
def assign_agent_class(rng, elephant_frac=0.5):
    return ELEPHANT if rng.random() < elephant_frac else MOUSE


# ==========================================================================
# timing models
# ==========================================================================
class ChatTiming:
    """Think-time between rounds (Gamma, bursty) — human/agent-paced work."""

    def __init__(self, regime="human", rng=None, mean_think=4.0, cv=1.5):
        self.regime = regime
        self.rng = rng or random.Random(0)
        self.mean_think = mean_think
        self.cv = cv

    def next_gap(self):
        if self.regime == "closed_loop":
            return 0.0
        k = 1.0 / (self.cv ** 2)              # CV = 1/sqrt(k)
        return self.rng.gammavariate(k, self.mean_think / k)

    def describe(self):
        return {"family": "gamma" if self.regime == "human" else "closed_loop",
                "mean_think_s": self.mean_think if self.regime == "human" else 0.0,
                "cv": self.cv if self.regime == "human" else None}


class TelemetryCadence:
    """Machine-driven arrivals: simulated hours replayed at a compression factor."""

    def __init__(self, compression=600.0, query_every_hours=6):
        self.compression = compression
        self.query_every_hours = query_every_hours

    def gap_for_hours(self, hours):
        return (hours * 3600.0) / self.compression

    def describe(self):
        return {"family": "telemetry_cadence",
                "compression": self.compression,
                "query_every_hours": self.query_every_hours,
                "seconds_per_query": self.gap_for_hours(self.query_every_hours)}

# Predictive maintenance digital twin
class PredictiveMaintenanceDT:

    name = "pdm_digital_twin"

    # {len} is the agent class's answer_spec: it is what separates a mouse's
    # round from an elephant's on the decode side.
    Q_PERIODIC = ("condition",
                  "Telemetry for machine {mid}, hours {h0}-{h1}:\n{w}\n"
                  "Assess the condition of this machine. {len}")
    Q_ON_ERROR = ("rootcause",
                  "Machine {mid} logged {evt} at hour {h1}.\n"
                  "Telemetry for hours {h0}-{h1}:\n{w}\n"
                  "State the most probable failing component and your confidence. {len}")
    Q_ON_FAILURE = ("decision",
                    "Machine {mid} reported failure '{evt}' at hour {h1}.\n"
                    "Telemetry for hours {h0}-{h1}:\n{w}\n"
                    "Should the machine be stopped, serviced, or kept running? "
                    "Give one recommendation and one reason. {len}")
    # instrumentation: probes conversational state rather than operator behaviour
    Q_TREND = ("trend",
               "Compared with the telemetry you assessed earlier for machine "
               "{mid}, is the condition improving or deteriorating? {len}")
    Q_RECALL = ("recall_probe",
                "Which sensor did you first identify as abnormal for machine "
                "{mid}, and does it still concern you? {len}")

    def __init__(self, data_dir=".", machine_id=1, window_hours=6,
                 from_hour=0, horizon_hours=120, until_failure=False,
                 query_every_hours=6, time_compression=600.0,
                 trend_every=4, recall_every=0, allow_synthetic=False,
                 agent_class=None, rng=None):
        self.rng = rng or random.Random(0)
        self.machine_id = machine_id
        # The agent class overrides the window: one of the two levers that
        # separate a mouse's round duration from an elephant's.
        self.agent_class = agent_class
        if agent_class is not None:
            window_hours = agent_class.window_hours
        self.window_hours = window_hours
        self.from_hour = from_hour
        self.horizon_hours = horizon_hours
        self.until_failure = until_failure
        self.trend_every = trend_every          # 0 = never
        self.recall_every = recall_every        # 0 = never
        self.cadence = TelemetryCadence(time_compression, query_every_hours)

        self.readings, self.events, self.synthetic, self.source = self._load(
            data_dir, machine_id, allow_synthetic)

        self.end_hour = self.from_hour + self.horizon_hours
        self.failure_hour = None
        if self.until_failure:
            for h, kind, _ in self.events:
                if kind == "failure" and h > self.from_hour:
                    self.failure_hour = h
                    self.end_hour = h
                    break
            if self.failure_hour is None:
                raise SystemExit(
                    f"--until-failure: no failure recorded for machine "
                    f"{machine_id} after hour {self.from_hour}")
        self.end_hour = min(self.end_hour, len(self.readings))

        self.meta = {
            "usecase": self.name,
            "machine_id": machine_id,
            "data_source": self.source,
            "synthetic": self.synthetic,
            "n_readings": len(self.readings),
            "n_events": len(self.events),
            "from_hour": self.from_hour,
            "end_hour": self.end_hour,
            "until_failure": self.until_failure,
            "failure_hour": self.failure_hour,
            "window_hours": window_hours,
            "agent_class": (agent_class.describe() if agent_class else
                            {"class": "uniform", "window_hours": window_hours,
                             "answer_spec": DEFAULT_ANSWER_SPEC}),
            "timing": self.cadence.describe(),
            "instrumentation_queries": {"trend_every": trend_every,
                                        "recall_every": recall_every},
        }

    # ---------------- data ----------------
    def _load(self, data_dir, mid, allow_synthetic):
        tpath = os.path.join(data_dir, "PdM_telemetry.csv")
        if os.path.exists(tpath):
            readings = self._read_telemetry(tpath, mid)
            if not readings:
                raise SystemExit(f"{tpath} has no rows for machineID={mid}")
            events = self._read_events(data_dir, mid, readings)
            return readings, events, False, os.path.abspath(tpath)

        if not allow_synthetic:
            raise SyntheticDataRefused(
                f"\nNo real dataset found at {tpath}.\n"
                "This driver will not silently fabricate telemetry.\n"
                "  * pass --allow-synthetic to run a PIPELINE TEST; results are\n"
                "    then tagged synthetic=True and must not be reported.\n")
        readings, events = self._synthesize()
        return readings, events, True, "synthetic (Azure schema) PIPELINE TEST ONLY"

    def _read_telemetry(self, path, mid):
        rows = []
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                try:
                    if int(r["machineID"]) != mid:
                        continue
                    rows.append((r.get("datetime", ""),
                                 float(r["volt"]), float(r["rotate"]),
                                 float(r["pressure"]), float(r["vibration"])))
                except (KeyError, ValueError):
                    continue
        rows.sort(key=lambda x: x[0])
        return [r[1:] for r in rows]

    def _read_events(self, data_dir, mid, readings):
        """Map error/failure records onto hour indices from the machine's start."""
        events = []
        base = None
        tpath = os.path.join(data_dir, "PdM_telemetry.csv")
        with open(tpath, newline="") as f:
            for r in csv.DictReader(f):
                if int(r.get("machineID", -1)) == mid:
                    base = self._parse_dt(r.get("datetime", ""))
                    break
        if base is None:
            return events

        for fname, kind, col in (("PdM_errors.csv", "error", "errorID"),
                                 ("PdM_failures.csv", "failure", "failure")):
            p = os.path.join(data_dir, fname)
            if not os.path.exists(p):
                continue
            with open(p, newline="") as f:
                for r in csv.DictReader(f):
                    try:
                        if int(r["machineID"]) != mid:
                            continue
                        t = self._parse_dt(r["datetime"])
                        if t is None:
                            continue
                        hour = int((t - base).total_seconds() // 3600)
                        if 0 <= hour < len(readings):
                            events.append((hour, kind, r.get(col, kind)))
                    except (KeyError, ValueError):
                        continue
        events.sort()
        return events

    @staticmethod
    def _parse_dt(s):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y %I:%M:%S %p",
                    "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y %H:%M"):
            try:
                return dt.datetime.strptime(s.strip(), fmt)
            except (ValueError, AttributeError):
                continue
        return None

    def _synthesize(self):
        """PIPELINE TEST ONLY — never for reported results."""
        n = self.from_hour + self.horizon_hours + self.window_hours + 10
        out = []
        for i in range(n):
            d = i / max(n - 1, 1)
            out.append((self.rng.gauss(170.0, 12.0),
                        self.rng.gauss(450.0, 45.0),
                        self.rng.gauss(100.0, 9.0) + 12.0 * d,
                        self.rng.gauss(40.0, 4.0) + 9.0 * d))
        events = [(int(n * 0.45), "error", "error1"),
                  (int(n * 0.80), "failure", "comp1")]
        return out, events

    # ---------------- rendering ----------------
    def _window(self, h1):
        h0 = max(0, h1 - self.window_hours)
        w = self.readings[h0:h1]
        if not w:
            return h0, h1, "  (no readings)"
        lines = ["  volt   rotate  pressure  vibration"]
        for (v, r, p, vb) in w:
            lines.append(f"  {v:6.1f} {r:7.1f} {p:8.1f} {vb:9.1f}")
        vib = [x[3] for x in w]
        lines.append(f"  (vibration mean {statistics.fmean(vib):.1f}, "
                     f"max {max(vib):.1f})")
        return h0, h1, "\n".join(lines)

    # ---------------- driver interface ----------------
    def tasks(self):
        ev_by_hour = {}
        for h, kind, label in self.events:
            if self.from_hour <= h <= self.end_hour:
                ev_by_hour.setdefault(h, (kind, label))

        idx = 0
        scheduled = 0
        prev_hour = self.from_hour
        hour = self.from_hour + self.cadence.query_every_hours

        while hour <= self.end_hour:
            evt = None
            for h in range(prev_hour + 1, hour + 1):
                if h in ev_by_hour:
                    evt = (h,) + ev_by_hour[h]
                    break

            if evt and evt[1] == "failure":
                intent, template = self.Q_ON_FAILURE
            elif evt and evt[1] == "error":
                intent, template = self.Q_ON_ERROR
            else:
                intent, template = self.Q_PERIODIC

            yield {"idx": idx, "intent": intent, "template": template,
                   "hour": hour, "prev_hour": prev_hour,
                   "event": evt[2] if evt else "",
                   "sim_hours_since_prev": hour - prev_hour,
                   "instrumentation": False,
                   "last_of_doc": False}
            idx += 1
            scheduled += 1

            if self.trend_every and scheduled % self.trend_every == 0:
                i2, t2 = self.Q_TREND
                yield {"idx": idx, "intent": i2, "template": t2, "hour": hour,
                       "prev_hour": hour, "event": "",
                       "sim_hours_since_prev": 0, "instrumentation": True,
                       "last_of_doc": False}
                idx += 1
            if self.recall_every and scheduled % self.recall_every == 0:
                i3, t3 = self.Q_RECALL
                yield {"idx": idx, "intent": i3, "template": t3, "hour": hour,
                       "prev_hour": hour, "event": "",
                       "sim_hours_since_prev": 0, "instrumentation": True,
                       "last_of_doc": False}
                idx += 1

            prev_hour = hour
            hour += self.cadence.query_every_hours

    def render(self, task):
        answer_spec = (self.agent_class.answer_spec if self.agent_class
                       else DEFAULT_ANSWER_SPEC)
        if task["intent"] in ("trend", "recall_probe"):
            return task["template"].format(mid=self.machine_id, len=answer_spec)
        h0, h1, w = self._window(task["hour"])
        return task["template"].format(mid=self.machine_id, w=w, h0=h0, h1=h1,
                                       evt=task["event"], len=answer_spec)

    def think_time(self, task=None):
        hours = self.cadence.query_every_hours
        if task is not None:
            hours = max(task.get("sim_hours_since_prev", 0), 0) or hours
        return self.cadence.gap_for_hours(hours)

    def is_unit_end(self, task):
        return task["hour"] >= self.end_hour

    def unit_pause(self):
        return 0.0


REGISTRY = {"pdm": PredictiveMaintenanceDT}

def build(name, args, rng, session=0, agent_class=None):
    if name == "pdm":
        return PredictiveMaintenanceDT(
            agent_class=agent_class,
            data_dir=args.data_dir,
            # each concurrent session is a DIFFERENT machine's Digital Twin,
            # so --sessions N models N distinct entities
            machine_id=args.machine_id + session,
            window_hours=getattr(args, "window_hours", 6),
            from_hour=getattr(args, "from_hour", 0),
            horizon_hours=getattr(args, "horizon_hours", 120),
            until_failure=getattr(args, "until_failure", False),
            query_every_hours=getattr(args, "query_every_hours", 6),
            time_compression=getattr(args, "time_compression", 600.0),
            trend_every=getattr(args, "trend_every", 4),
            recall_every=getattr(args, "recall_every", 0),
            allow_synthetic=getattr(args, "allow_synthetic", False),
            rng=rng)
    raise SystemExit(f"unknown usecase: {name} (have: {list(REGISTRY)})")
