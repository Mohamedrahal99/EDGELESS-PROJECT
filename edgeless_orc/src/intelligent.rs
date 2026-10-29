//! Intelligent or CAP 
//! # Mathematical formulation
//!
//! ## Notation
//! * `N`: candidate nodes; node `n` has relative capacity `c_n` (1 = reference
//!   node), static power `P_n` and agent set `A_n`.
//! * Agent `j` is a closed-loop client with reference service time `S_j` and
//!   think time `Z_j`. It alternates between one request to the inference
//!   slot and a think period.
//! * `a` is the arriving agent.
//!
//! ## Agent service time (`AgentProfile::effective_service`)
//!
//! With growth rate `g` and age `t`, the conversation grows by one exchange
//! per completed round:
//!
//! ```text
//!   m(t)  = floor( t / (S + Z) )
//!   S(t)  = S * (1 + g * m(t))
//! ```
//!
//! ## Node model (`predict_on`): approximate MVA, single-slot FCFS queue
//!
//! Service time on node `n` and the residual-service factor for coefficient
//! of variation `cv`:
//!
//! ```text
//!   s_j = S_j / c_n              rho = (1 + cv^2) / 2
//! ```
//!
//! For each agent `j` with response time `R_j` (initialised to `s_j`):
//!
//! ```text
//!   X_j = 1 / (Z_j + R_j)                    throughput
//!   U_j = X_j * s_j                          utilisation
//!   Q_j = X_j * R_j                          mean number in system
//!
//!   W_j = sum_{i != j} [ U_i * s_i * rho  +  max(Q_i - U_i, 0) * s_i ]
//!         (residual service of the request in service
//!          + service of the requests queued behind it)
//!
//!   R_j <- 1/2 * R_j + 1/2 * (s_j + W_j)     damped fixed point
//! ```
//!
//! iterated until `max_j |dR_j| < 1e-9` or `max_iterations` is reached.
//!
//! Node metrics:
//!
//! ```text
//!   C_j       = R_j + Z_j                    cycle time of agent j
//!   Cmax(n)   = max_j C_j                    largest cycle time on the node
//!   sigma(n)  = sum_j R_j / S_j              sum of slowdowns, versus the
//!                                            reference node running alone
//!   U(n)      = sum_j s_j / (Z_j + R_j)      utilisation of the slot
//! ```
//!
//! ## Placement rule (`choose`, "CAP")
//!
//! For each feasible node `n` (`|A_n| < max_agents_n`), let `A_n + a` be the
//! node population with the arrival. The predicted global makespan is
//!
//! ```text
//!   G_n = max( Cmax_others , Cmax(A_n + a) , max_{m != n} Cmax(A_m) )
//! ```
//!
//! where `Cmax_others` is the largest cycle time on non-candidate nodes.
//! Nodes within the makespan tolerance `epsilon` are admissible:
//!
//! ```text
//!   G*  = min_n G_n
//!   Adm = { n : G_n <= G* * (1 + epsilon) }
//! ```
//!
//! Among admissible nodes the arrival minimises the increase in total
//! slowdown,
//!
//! ```text
//!   n* = argmin_{n in Adm}  [ sigma(A_n + a) - sigma(A_n) ]
//! ```
//!
//! with ties (within `1e-9`) broken by lower static power `P_n`, then fewer
//! agents `|A_n|`, then lower node id.
//!
//! ## Baselines (same feasibility filter and tie-breaks, different cost)
//!
//! ```text
//!   WLL (choose_wll):  n* = argmin_n ( sum_{j in A_n} S_j + S_a ) / c_n
//!   CLL (choose_cll):  n* = argmin_n ( |A_n| + 1 ) / c_n
//! CLL is WLL with every class cost set to 1; WLL is CAP without the
//! queueing model.

use std::collections::HashMap;

/// What a workflow declares about one agent, per round, when running alone.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct AgentProfile {
    /// Mean service time of one round on an otherwise idle node (s)
    pub service_s: f64,
    /// Time between receiving an answer and sending the next request (s)
    pub think_s: f64,
    /// Fractional growth of the service time per round, as the conversation
    /// state grows. 0 disables the age term.
    pub growth_per_round: f64,
}

impl AgentProfile {
    /// Service time expected from an agent that has been running for
    /// `age_s` seconds: its conversation, and its prefill cost, has grown
    /// by one exchange per completed round.
    pub fn effective_service(&self, age_s: f64) -> f64 {
        let cycle = (self.service_s + self.think_s).max(1e-9);
        let rounds = (age_s.max(0.0) / cycle).floor();
        self.service_s * (1.0 + self.growth_per_round.max(0.0) * rounds)
    }

    pub fn agent(&self, age_s: f64) -> Agent {
        Agent { service_s: self.effective_service(age_s).max(1e-6), think_s: self.think_s.max(0.0) }
    }
}

/// An agent as the queueing model sees it.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Agent {
    pub service_s: f64,
    pub think_s: f64,
}

/// Policy parameters. Defaults are the values calibrated on the testbed; each
/// can be overridden through the environment of the orchestrator process (see
/// [`Params::from_env`]), e.g. in a systemd override.
#[derive(Debug, Clone)]
pub struct Params {
    /// Declared agent classes, looked up from the `agent_class` annotation.
    pub classes: HashMap<String, AgentProfile>,
    /// Makespan tolerance of the fairness criterion epsilon(0.10).
    pub epsilon: f64,
    /// Coefficient of variation of the service time, for the residual term.
    pub service_cv: f64,
    /// Resource class type of the inference server.
    pub llm_class_type: String,
    /// Static power assumed for nodes without a `static_power_mw=` label.
    pub default_static_power_mw: f64,
    /// Fixed-point iterations of the approximate MVA.
    pub max_iterations: usize,
}

impl Default for Params {
    fn default() -> Self {
        let mut classes = HashMap::new();
        // Service time of one round on a node of capacity 1.0 (a full-speed
        // Orin), mean over a 12-round conversation, from the single-agent
        // calibration runs. Think time is the driver's fixed 18 s.
        classes.insert("mouse".to_string(), AgentProfile { service_s: 0.95, think_s: 18.0, growth_per_round: 0.0 });
        classes.insert("elephant".to_string(), AgentProfile { service_s: 5.44, think_s: 18.0, growth_per_round: 0.0 });
        Self {
            classes,
            epsilon: 0.10,
            service_cv: 0.30,
            llm_class_type: "ollama".to_string(),
            default_static_power_mw: 10_000.0,
            max_iterations: 200,
        }
    }
}

impl Params {
    /// Defaults, overridden by:
    ///
    /// * `EDGELESS__CLASSES`   `name=service:think:growth,...`
    ///   e.g. `mouse=0.95:18:0,elephant=5.44:18:0`
    /// * `EDGELESS_INTELLIGENT_EPSILON`   e.g. `0.10`
    /// * `EDGELESS_INTELLIGENT_CV`        e.g. `0.30`
    /// * `EDGELESS_INTELLIGENT_LLM_CLASS` e.g. `ollama`
    /// * `EDGELESS_INTELLIGENT_STATIC_POWER_MW` e.g. `10000`
    pub fn from_env() -> Self {
        let mut p = Params::default();
        if let Ok(v) = std::env::var("EDGELESS_INTELLIGENT_CLASSES") {
            let parsed = parse_classes(&v);
            if !parsed.is_empty() {
                p.classes = parsed;
            }
        }
        if let Some(v) = env_f64("EDGELESS_INTELLIGENT_EPSILON") {
            p.epsilon = v.max(0.0);
        }
        if let Some(v) = env_f64("EDGELESS_INTELLIGENT_CV") {
            p.service_cv = v.max(0.0);
        }
        if let Ok(v) = std::env::var("EDGELESS_INTELLIGENT_LLM_CLASS") {
            if !v.is_empty() {
                p.llm_class_type = v;
            }
        }
        if let Some(v) = env_f64("EDGELESS_INTELLIGENT_STATIC_POWER_MW") {
            p.default_static_power_mw = v.max(0.0);
        }
        p
    }

    /// Profile declared by a component's annotations (functions) or
    /// configuration (resources). `None` when the component declares nothing,
    /// in which case it is not treated as an agent.
    /// `agent_service_s` / `agent_think_s` / `agent_growth` override the class
    /// values, so a workflow can declare its own measured cost directly.
    pub fn profile_from(&self, map: &HashMap<String, String>) -> Option<AgentProfile> {
        let base = map.get("agent_class").and_then(|c| self.classes.get(c)).copied();
        let service = map.get("agent_service_s").and_then(|v| v.parse::<f64>().ok());
        if base.is_none() && service.is_none() {
            return None;
        }
        let mut prof = base.unwrap_or(AgentProfile { service_s: 1.0, think_s: 0.0, growth_per_round: 0.0 });
        if let Some(s) = service {
            prof.service_s = s;
        }
        if let Some(z) = map.get("agent_think_s").and_then(|v| v.parse::<f64>().ok()) {
            prof.think_s = z;
        }
        if let Some(g) = map.get("agent_growth").and_then(|v| v.parse::<f64>().ok()) {
            prof.growth_per_round = g;
        }
        if prof.service_s > 0.0 { Some(prof) } else { None }
    }
}

fn env_f64(key: &str) -> Option<f64> {
    std::env::var(key).ok().and_then(|v| v.trim().parse::<f64>().ok())
}

/// Parse `name=service:think:growth,...`; malformed entries are skipped.
pub fn parse_classes(spec: &str) -> HashMap<String, AgentProfile> {
    let mut out = HashMap::new();
    for entry in spec.split(',') {
        let mut kv = entry.splitn(2, '=');
        let (name, vals) = match (kv.next(), kv.next()) {
            (Some(n), Some(v)) if !n.trim().is_empty() => (n.trim(), v),
            _ => continue,
        };
        let f: Vec<f64> = vals.split(':').filter_map(|x| x.trim().parse::<f64>().ok()).collect();
        if f.is_empty() || f[0] <= 0.0 {
            continue;
        }
        out.insert(
            name.to_string(),
            AgentProfile {
                service_s: f[0],
                think_s: *f.get(1).unwrap_or(&0.0),
                growth_per_round: *f.get(2).unwrap_or(&0.0),
            },
        );
    }
    out
}

/// Parse a node label of the form `key=value` into a number.
pub fn label_f64(labels: &[String], key: &str) -> Option<f64> {
    let prefix = format!("{}=", key);
    labels.iter().find_map(|l| l.strip_prefix(prefix.as_str()).and_then(|v| v.trim().parse::<f64>().ok()))
}

// prediction: approximate MVA of one single-slot FCFS node

#[derive(Debug, Clone, PartialEq)]
pub struct Prediction {
    /// Predicted response time of each agent, same order as the input (s).
    pub response_s: Vec<f64>,
    /// Largest predicted cycle time (response + think) on the node (s).
    pub max_cycle_s: f64,
    /// Sum over the agents of response / service.
    pub slowdown_sum: f64,
    /// Predicted utilisation of the inference slot.
    pub utilisation: f64,
}

impl Prediction {
    fn empty() -> Self {
        Self { response_s: vec![], max_cycle_s: 0.0, slowdown_sum: 0.0, utilisation: 0.0 }
    }
}

pub fn predict_on(agents: &[Agent], capacity: f64, cv: f64, max_iterations: usize) -> Prediction {
    let k = agents.len();
    if k == 0 {
        return Prediction::empty();
    }
    let cap = capacity.max(1e-6);
    let svc: Vec<f64> = agents.iter().map(|a| a.service_s / cap).collect();
    let resid = (1.0 + cv * cv) / 2.0;
    let mut r: Vec<f64> = svc.clone();
    let mut u = vec![0.0; k];
    let mut q = vec![0.0; k];
    for _ in 0..max_iterations.max(1) {
        for j in 0..k {
            let x = 1.0 / (agents[j].think_s + r[j]);
            u[j] = x * svc[j];
            q[j] = x * r[j];
        }
        let mut delta: f64 = 0.0;
        for j in 0..k {
            let mut wait = 0.0;
            for i in 0..k {
                if i != j {
                    let queued = (q[i] - u[i]).max(0.0);
                    wait += u[i] * svc[i] * resid + queued * svc[i];
                }
            }
            let target = svc[j] + wait;
            let next = 0.5 * r[j] + 0.5 * target; // damped
            delta = delta.max((next - r[j]).abs());
            r[j] = next;
        }
        if delta < 1e-9 {
            break;
        }
    }
    let mut max_cycle: f64 = 0.0;
    let mut slow = 0.0;
    let mut util = 0.0;
    for j in 0..k {
        max_cycle = max_cycle.max(r[j] + agents[j].think_s);
        slow += r[j] / agents[j].service_s;
        util += svc[j] / (agents[j].think_s + r[j]);
    }
    Prediction { response_s: r, max_cycle_s: max_cycle, slowdown_sum: slow, utilisation: util }
}

/// Predict on a reference-capacity node (capacity = 1.0).
pub fn predict(agents: &[Agent], cv: f64, max_iterations: usize) -> Prediction {
    predict_on(agents, 1.0, cv, max_iterations)
}

// decision
/// One node the arriving agent may be placed on.
#[derive(Debug, Clone)]
pub struct Candidate<K> {
    pub node: K,
    /// Agents already anchored on the node.
    pub agents: Vec<Agent>,
    /// Measured static power (mW); lower is preferred on ties.
    pub static_power_mw: f64,
    /// Relative compute capacity of the node, 1.0 = the reference node. 
    pub capacity: f64,
    /// Optional cap on the number of agents that can be placed on the node
    pub max_agents: Option<usize>,
}

/// The outcome of a decision, kept for logging and evaluation.
#[derive(Debug, Clone, PartialEq)]
pub struct Decision<K> {
    pub node: K,
    /// Predicted global maximum cycle time with the chosen placement (s).
    pub predicted_max_cycle_s: f64,
    /// Best predicted global maximum cycle time over all candidates (s).
    pub best_max_cycle_s: f64,
    /// Increase in the sum of slowdowns on the chosen node.
    pub delta_slowdown: f64,
    /// Predicted slowdown of the arriving agent.
    pub arriving_slowdown: f64,
    /// Candidates within the makespan tolerance.
    pub admissible: usize,
    pub considered: usize,
}

const TIE: f64 = 1e-9;

/// Choose the node for `arriving`
/// `others_max_cycle_s` is the largest predicted cycle time on nodes that are
/// not candidates (e.g. excluded by a sticky constraint); pass 0 when every
/// node hosting agents is a candidate.
/// Weighted least-loaded, capacity-aware (WLL-cap): the strongest simple
/// baseline for this workload, and the ablation that isolates what the queueing
/// model adds.
/// It uses the same two declared inputs as [`choose`] --- the class service
/// time and the node's measured capacity --- but no queueing model: a node's
/// load is the sum of the service times of the agents on it, divided by its
/// capacity, and the arriving agent goes where that load would be smallest.
/// Equivalently it minimises `(sum_j s_j + s_a) / c_n`, i.e. the node's
/// projected busy time per round. Ties fall to static power, then agent count,
/// then node id, exactly as in [`choose`], so the two policies differ only in
/// the cost function.
/// Complexity is `O(|candidates|)` per decision, against `O(|candidates| k^2 I)`
/// for the MVA-based rule.
pub fn choose_wll<K: Copy + Ord>(
    candidates: &[Candidate<K>],
    arriving: Agent,
    p: &Params,
) -> Option<Decision<K>> {
    struct Eval<K> {
        node: K,
        load: f64,
        power: f64,
        count: usize,
    }
    let mut evals: Vec<Eval<K>> = Vec::with_capacity(candidates.len());
    for c in candidates {
        if let Some(cap) = c.max_agents {
            if c.agents.len() >= cap {
                continue;
            }
        }
        let cap = c.capacity.max(1e-6);
        let load: f64 =
            (c.agents.iter().map(|a| a.service_s).sum::<f64>() + arriving.service_s) / cap;
        evals.push(Eval { node: c.node, load, power: c.static_power_mw, count: c.agents.len() });
    }
    if evals.is_empty() {
        return None;
    }
    evals.sort_by(|a, b| {
        cmp_f(a.load, b.load)
            .then(cmp_f(a.power, b.power))
            .then(a.count.cmp(&b.count))
            .then(a.node.cmp(&b.node))
    });
    let w = &evals[0];
    // Report the same fields as `choose` so both policies log identically; the
    // predicted cycle is computed after the fact, for the record only.
    let chosen = candidates.iter().find(|c| c.node == w.node)?;
    let mut with = chosen.agents.clone();
    with.push(arriving);
    let after = predict_on(&with, chosen.capacity, p.service_cv, p.max_iterations);
    let before = predict_on(&chosen.agents, chosen.capacity, p.service_cv, p.max_iterations);
    Some(Decision {
        node: w.node,
        predicted_max_cycle_s: after.max_cycle_s,
        best_max_cycle_s: after.max_cycle_s,
        delta_slowdown: after.slowdown_sum - before.slowdown_sum,
        arriving_slowdown: after.response_s.last().copied().unwrap_or(arriving.service_s)
            / arriving.service_s,
        admissible: evals.len(),
        considered: evals.len(),
    })
}

/// Rule of the count-based load-balancing baselines.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CountRule {
    Plain,
    /// CLL: the node with the smallest `(agents + 1) / capacity`, i.e. the
    /// agent count the node would hold after the arrival, normalised by its
    /// measured capacity. Knows capacity, but not the class.
    CapacityNormalised,
}

/// They use the same feasibility filter (`max_agents`) and the same
/// tie-breaks as [`choose_wll`] and [`choose`] (static power, then agent count,
/// then node id), so the policies differ only in the cost function:
///
/// * CLL minimises `(|A_n| + 1) / c_n`
/// * WLL minimises `(sum_j S_k(j) + S_k(a)) / c_n`
///
/// CLL is WLL with every class cost set to 1: the arriving agent's class is
/// never read. Complexity is `O(|candidates|)` per decision.
pub fn choose_count<K: Copy + Ord>(
    candidates: &[Candidate<K>],
    arriving: Agent,
    p: &Params,
    rule: CountRule,
) -> Option<Decision<K>> {
    struct Eval<K> {
        node: K,
        load: f64,
        power: f64,
        count: usize,
    }
    let mut evals: Vec<Eval<K>> = Vec::with_capacity(candidates.len());
    for c in candidates {
        if let Some(cap) = c.max_agents {
            if c.agents.len() >= cap {
                continue;
            }
        }
        let load = match rule {
            CountRule::Plain => c.agents.len() as f64,
            CountRule::CapacityNormalised => (c.agents.len() as f64 + 1.0) / c.capacity.max(1e-6),
        };
        evals.push(Eval { node: c.node, load, power: c.static_power_mw, count: c.agents.len() });
    }
    if evals.is_empty() {
        return None;
    }
    evals.sort_by(|a, b| {
        cmp_f(a.load, b.load)
            .then(cmp_f(a.power, b.power))
            .then(a.count.cmp(&b.count))
            .then(a.node.cmp(&b.node))
    });
    let w = &evals[0];
    // Same record as `choose_wll`: the prediction is computed after the fact,
    // for logging only, and never influences the decision.
    let chosen = candidates.iter().find(|c| c.node == w.node)?;
    let mut with = chosen.agents.clone();
    with.push(arriving);
    let after = predict_on(&with, chosen.capacity, p.service_cv, p.max_iterations);
    let before = predict_on(&chosen.agents, chosen.capacity, p.service_cv, p.max_iterations);
    Some(Decision {
        node: w.node,
        predicted_max_cycle_s: after.max_cycle_s,
        best_max_cycle_s: after.max_cycle_s,
        delta_slowdown: after.slowdown_sum - before.slowdown_sum,
        arriving_slowdown: after.response_s.last().copied().unwrap_or(arriving.service_s) / arriving.service_s,
        admissible: evals.len(),
        considered: evals.len(),
    })
}

/// CLL: least agents per unit of measured capacity, not class-aware.
pub fn choose_cll<K: Copy + Ord>(candidates: &[Candidate<K>], arriving: Agent, p: &Params) -> Option<Decision<K>> {
    choose_count(candidates, arriving, p, CountRule::CapacityNormalised)
}

pub fn choose<K: Copy + Ord>(
    candidates: &[Candidate<K>],
    arriving: Agent,
    others_max_cycle_s: f64,
    p: &Params,
) -> Option<Decision<K>> {
    // Solve every candidate without the arriving agent, on its own capacity.
    let before: Vec<Prediction> = candidates
        .iter()
        .map(|c| predict_on(&c.agents, c.capacity, p.service_cv, p.max_iterations))
        .collect();

    struct Eval<K> {
        node: K,
        global: f64,
        dslow: f64,
        own_slow: f64,
        power: f64,
        count: usize,
    }
    let mut evals: Vec<Eval<K>> = Vec::with_capacity(candidates.len());
    for (idx, c) in candidates.iter().enumerate() {
        if let Some(cap) = c.max_agents {
            if c.agents.len() >= cap {
                continue;
            }
        }
        let mut with = c.agents.clone();
        with.push(arriving);
        let after = predict_on(&with, c.capacity, p.service_cv, p.max_iterations);
        let mut others = others_max_cycle_s;
        for (jdx, b) in before.iter().enumerate() {
            if jdx != idx {
                others = others.max(b.max_cycle_s);
            }
        }
        let own_slow = after.response_s.last().copied().unwrap_or(arriving.service_s) / arriving.service_s;
        evals.push(Eval {
            node: c.node,
            global: others.max(after.max_cycle_s),
            dslow: after.slowdown_sum - before[idx].slowdown_sum,
            own_slow,
            power: c.static_power_mw,
            count: c.agents.len(),
        });
    }
    if evals.is_empty() {
        return None;
    }
    let best = evals.iter().map(|e| e.global).fold(f64::INFINITY, f64::min);
    let bound = best * (1.0 + p.epsilon.max(0.0)) + TIE;
    let considered = evals.len();
    let mut adm: Vec<Eval<K>> = evals.into_iter().filter(|e| e.global <= bound).collect();
    let admissible = adm.len();
    adm.sort_by(|a, b| {
        cmp_f(a.dslow, b.dslow)
            .then(cmp_f(a.power, b.power))
            .then(a.count.cmp(&b.count))
            .then(a.node.cmp(&b.node))
    });
    let w = &adm[0];
    Some(Decision {
        node: w.node,
        predicted_max_cycle_s: w.global,
        best_max_cycle_s: best,
        delta_slowdown: w.dslow,
        arriving_slowdown: w.own_slow,
        admissible,
        considered,
    })
}

fn cmp_f(a: f64, b: f64) -> std::cmp::Ordering {
    if (a - b).abs() <= TIE {
        std::cmp::Ordering::Equal
    } else if a < b {
        std::cmp::Ordering::Less
    } else {
        std::cmp::Ordering::Greater
    }
}
