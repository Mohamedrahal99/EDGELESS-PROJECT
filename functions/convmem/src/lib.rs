// SPDX-License-Identifier: MIT
//
// ConversationMem — in-platform stateful conversation memory 

use edgeless_function::*;
use std::cell::RefCell;


/// Turn delimiters. Deliberately unusual so the model does not reproduce them
/// by accident and continue the dialogue on its own.
const OPERATOR_TAG: &str = "### REQUEST";
const ASSISTANT_TAG: &str = "### RESPONSE";

/// Default, workload-neutral system preamble. Override per workflow with the
/// init-payload key `system=...` when a specific role is wanted.
const DEFAULT_SYSTEM_PREAMBLE: &str = "\
You are a helpful assistant. Answer ONLY the final request, concisely.
Do not write additional requests, do not continue the dialogue, and do not
invent information that was not provided.
";

/// Prefix the DRIVER puts on every prompt. Payloads carrying it are prompts;
/// everything else arriving by cast is an LLM answer. 
const PROMPT_MARKER: &str = "###PROMPT";

/// Self-cast payload used by the LLM watchdog, followed by the turn number.
const WATCHDOG_MARKER: &str = "###WATCHDOG";

/// Prefix of the payload posted to the driver when a round fails, so the
/// timeout is propagated out of the platform instead of hanging silently.
const ERROR_MARKER: &str = "###ERROR";

/// Default watchdog delay (ms) before a round is declared failed. Override per
/// workflow with the init-payload key `llm_timeout_ms=...`.
const DEFAULT_LLM_TIMEOUT_MS: u64 = 180_000;

/// Markers that indicate the model started fabricating further turns. Anything
/// from the first occurrence onwards is discarded before the reply is stored.
const FABRICATION_MARKERS: [&str; 8] = [
    OPERATOR_TAG,
    ASSISTANT_TAG,
    "\nuser:",
    "\nUser:",
    "\nassistant:",
    "\nAssistant:",
    "\nOPERATOR:",
    "\nRESPONSE:",
];

// configuration (from the workflow's init-payload)
/// Context policy: what the LLM is shown each turn.
///   Full      -> system preamble + the entire transcript (conversational
///                tasks: the LLM's memory IS this history; ctx grows with sigma)
#[derive(PartialEq)]
enum ContextPolicy {
    Full,
    Stateless,
}

struct Config {
    system: String,
    context: ContextPolicy,
    llm_timeout_ms: u64,
}

impl Config {
    fn new() -> Self {
        Config {
            system: DEFAULT_SYSTEM_PREAMBLE.to_string(),
            context: ContextPolicy::Full,
            llm_timeout_ms: DEFAULT_LLM_TIMEOUT_MS,
        }
    }
}

thread_local! {
    static CONFIG: RefCell<Config> = RefCell::new(Config::new());
}


struct State {
    /// (role, text); role is "user" or "assistant".
    history: Vec<(String, String)>,
    /// True while a prompt is out at the LLM. This is what distinguishes an
    /// incoming user prompt from an incoming LLM response.
    awaiting: bool,
    /// sigma: total bytes of stored conversation. Grows monotonically.
    bytes: u64,
    /// Completed turns (prompt + answer).
    turns: u64,
    /// Rounds abandoned because the LLM never answered.
    failed: u64,
}

impl State {
    fn new() -> Self {
        State { history: Vec::new(), awaiting: false, bytes: 0, turns: 0, failed: 0 }
    }

    fn serialize(&self) -> Vec<u8> {
        let mut s = format!("{} {} {} {}\n",
                            self.awaiting as u8, self.bytes, self.turns, self.failed);
        for (role, text) in &self.history {
            let clean = text.replace('\n', " ").replace('\t', " ");
            s.push_str(role);
            s.push('\t');
            s.push_str(&clean);
            s.push('\n');
        }
        s.into_bytes()
    }

    fn deserialize(bytes: &[u8]) -> Option<State> {
        let s = core::str::from_utf8(bytes).ok()?;
        let mut lines = s.lines();
        let mut header = lines.next()?.split_whitespace();
        let awaiting = header.next()?.parse::<u8>().ok()? != 0;
        let b = header.next()?.parse::<u64>().ok()?;
        let t = header.next()?.parse::<u64>().ok()?;
        // `failed` was added later; tolerate snapshots written without it
        let fl = header.next().and_then(|x| x.parse::<u64>().ok()).unwrap_or(0);
        let mut history = Vec::new();
        for line in lines {
            if let Some((role, text)) = line.split_once('\t') {
                history.push((role.to_string(), text.to_string()));
            }
        }
        Some(State { history, awaiting, bytes: b, turns: t, failed: fl })
    }

    /// Build the prompt sent to the LLM according to the context policy.
    ///
    /// Full: system preamble + the ENTIRE transcript. The LLM is stateless, so
    /// this function is the conversation's memory — the prompt grows every
    /// turn, which is the state growth this project studies.
    ///
    fn assemble_context(&self) -> String {
        let (system, stateless) = CONFIG.with(|c| {
            let c = c.borrow();
            (c.system.clone(), c.context == ContextPolicy::Stateless)
        });

        let mut ctx = system;
        ctx.push('\n');

        if stateless {
            if let Some((_, text)) = self.history.iter().rev().find(|(r, _)| r == "user") {
                ctx.push_str(OPERATOR_TAG);
                ctx.push('\n');
                ctx.push_str(text);
                ctx.push_str("\n\n");
            }
        } else {
            for (role, text) in &self.history {
                ctx.push_str(if role == "user" { OPERATOR_TAG } else { ASSISTANT_TAG });
                ctx.push('\n');
                ctx.push_str(text);
                ctx.push_str("\n\n");
            }
        }

        ctx.push_str(ASSISTANT_TAG);
        ctx.push('\n');
        ctx
    }
}

fn truncate_at_fabricated_turn(text: &str) -> String {
    let mut end = text.len();
    for marker in FABRICATION_MARKERS {
        if let Some(pos) = text.find(marker) {
            if pos < end {
                end = pos;
            }
        }
    }
    text[..end].trim().to_string()
}

thread_local! {
    static STATE: RefCell<State> = RefCell::new(State::new());
}

struct ConversationMem;

impl EdgeFunction for ConversationMem {
    fn handle_cast(_src: InstanceId, message: &[u8]) {
        let raw = match core::str::from_utf8(message) {
            Ok(t) => t.trim().to_string(),
            Err(_) => {
                log::warn!("conversation_mem: non-utf8 message ignored");
                return;
            }
        };
        // ---------------- watchdog ----------------
        // Sent by delayed_cast when a prompt goes out. If the turn
        // it names has still not completed, the LLM never answered: clear the
        // awaiting flag and propagate the failure to the driver.
        if let Some(rest) = raw.strip_prefix(WATCHDOG_MARKER) {
            let turn: u64 = rest.trim().parse().unwrap_or(0);
            let failure: Option<String> = STATE.with(|st| {
                let mut st = st.borrow_mut();
                if st.awaiting && st.turns + 1 == turn {
                    st.awaiting = false;
                    st.failed += 1;
                    // drop the unanswered prompt so the transcript stays coherent
                    if let Some((role, text)) = st.history.pop() {
                        if role == "user" {
                            st.bytes = st.bytes.saturating_sub(text.len() as u64);
                        } else {
                            st.history.push((role, text));
                        }
                    }
                    log::warn!(
                        "conversation_mem: LLM timeout on turn {} ({} failed so far)",
                        turn, st.failed
                    );
                    telemetry_log(5, "llm_timeout", &turn.to_string());
                    telemetry_log(5, "failed_rounds", &st.failed.to_string());
                    let snapshot = st.serialize();
                    sync(&snapshot);
                    Some(format!("{} llm_timeout turn={}", ERROR_MARKER, turn))
                } else {
                    None                      // the round done
                }
            });
            if let Some(err) = failure {
                cast("post", err.as_bytes());
            }
            return;
        }

        // ---------------- prompt/answer ----------------
        // A payload carrying PROMPT_MARKER is a request from the driver; any
        // other payload is an answer from the LLM. Deciding structurally (not
        // from the awaiting flag alone) means a lost answer cannot cause the
        // next prompt to be stored as a response.
        let is_prompt = raw.starts_with(PROMPT_MARKER);
        let text = if is_prompt {
            raw[PROMPT_MARKER.len()..].trim().to_string()
        } else {
            raw
        };

        enum Action {
            Ask(String, u64, u64),   // context, turn number, timeout ms
            Deliver(String),
            Nothing,
        }

        let action = STATE.with(|st| {
            let mut st = st.borrow_mut();

            if is_prompt {
                if st.awaiting {
                    // a previous answer never arrived and the watchdog has not
                    // fired yet: abandon that round rather than corrupt state
                    st.failed += 1;
                    st.awaiting = false;
                    if let Some((role, prev)) = st.history.pop() {
                        if role == "user" {
                            st.bytes = st.bytes.saturating_sub(prev.len() as u64);
                        } else {
                            st.history.push((role, prev));
                        }
                    }
                    log::warn!("conversation_mem: new prompt while awaiting; \
                                previous round abandoned");
                    telemetry_log(5, "round_abandoned", &st.turns.to_string());
                }

                st.bytes += text.len() as u64;
                st.history.push(("user".to_string(), text.clone()));
                let ctx = st.assemble_context();
                let ctx_len = ctx.len();
                st.awaiting = true;
                let turn = st.turns + 1;

                log::info!(
                    "conversation_mem: prompt -> llm (turn {}, ctx {} B, sigma {} B)",
                    turn, ctx_len, st.bytes
                );
                telemetry_log(5, "round_begin", &turn.to_string());
                telemetry_log(5, "prompt_bytes", &text.len().to_string());
                telemetry_log(5, "ctx_bytes", &ctx_len.to_string());

                let tmo = CONFIG.with(|c| c.borrow().llm_timeout_ms);
                Action::Ask(ctx, turn, tmo)
            } else {
                if !st.awaiting {
                    // an answer with no outstanding prompt: a late reply to a
                    // round already declared failed.
                    log::warn!("conversation_mem: unsolicited answer ({} B) dropped",
                               text.len());
                    telemetry_log(5, "late_answer_dropped", &text.len().to_string());
                    return Action::Nothing;
                }

                // The ollama resource reports a failure by sending an ERROR
                // payload through the normal reply path. we Treat it as a failed
                // round:
                if text.starts_with(ERROR_MARKER) {
                    st.awaiting = false;
                    st.failed += 1;
                    if let Some((role, prev)) = st.history.pop() {
                        if role == "user" {
                            st.bytes = st.bytes.saturating_sub(prev.len() as u64);
                        } else {
                            st.history.push((role, prev));
                        }
                    }
                    log::warn!("conversation_mem: LLM reported failure: {}", text);
                    telemetry_log(5, "llm_error", &text);
                    telemetry_log(5, "failed_rounds", &st.failed.to_string());
                    let snapshot = st.serialize();
                    sync(&snapshot);
                    return Action::Deliver(text.clone());
                }

                let answer = truncate_at_fabricated_turn(&text);
                let trimmed = text.len().saturating_sub(answer.len());
                if trimmed > 0 {
                    log::warn!(
                        "conversation_mem: trimmed {} B of fabricated continuation",
                        trimmed
                    );
                    telemetry_log(5, "trimmed_bytes", &trimmed.to_string());
                }

                st.bytes += answer.len() as u64;
                st.turns += 1;
                st.awaiting = false;
                st.history.push(("assistant".to_string(), answer.clone()));

                log::info!(
                    "conversation_mem: answer, turn {} done (sigma {} B, {} entries)",
                    st.turns, st.bytes, st.history.len()
                );
                telemetry_log(5, "round_end", &st.turns.to_string());
                telemetry_log(5, "answer_bytes", &answer.len().to_string());
                telemetry_log(5, "sigma_bytes", &st.bytes.to_string());

                let snapshot = st.serialize();
                sync(&snapshot);

                Action::Deliver(answer)
            }
        });
        match action {
            Action::Ask(ctx, turn, tmo) => {
                cast("llm", ctx.as_bytes());
                // arm the watchdog for this turn
                delayed_cast(tmo, "self", format!("{} {}", WATCHDOG_MARKER, turn).as_bytes());
            }
            Action::Deliver(answer) => {
                cast("post", answer.as_bytes());
                log::info!("conversation_mem: answer ({} B) -> poster", answer.len());
            }
            Action::Nothing => {}
        }
    }

    fn handle_call(_src: InstanceId, _message: &[u8]) -> CallRet {
        CallRet::NoReply
    }

    fn handle_init(init_message: Option<&[u8]>, serialized_state: Option<&[u8]>) {
        edgeless_function::init_logger();
        //   "init-payload": "system=You are an industrial maintenance assistant."
        if let Some(payload) = init_message.and_then(|p| core::str::from_utf8(p).ok()) {
            let args = edgeless_function::parse_init_payload(payload);
            if let Some(sys) = args.get("system") {
                if !sys.is_empty() {
                    CONFIG.with(|c| c.borrow_mut().system = sys.to_string());
                    log::info!("conversation_mem: custom system preamble set");
                }
            }
            if let Some(t) = args.get("llm_timeout_ms").and_then(|v| v.parse::<u64>().ok()) {
                CONFIG.with(|c| c.borrow_mut().llm_timeout_ms = t);
                log::info!("conversation_mem: llm timeout = {} ms", t);
            }
            match args.get("context").map(|s| &s[..]) {
                Some("stateless") => {
                    CONFIG.with(|c| c.borrow_mut().context = ContextPolicy::Stateless);
                    log::info!("conversation_mem: context policy = stateless");
                }
                Some("full") | None => {
                    log::info!("conversation_mem: context policy = full");
                }
                Some(other) => {
                    log::warn!(
                        "conversation_mem: unknown context policy '{}', using full",
                        other
                    );
                }
            }
        }

        if let Some(bytes) = serialized_state {
            if let Some(prev) = State::deserialize(bytes) {
                let entries = prev.history.len();
                let sigma = prev.bytes;
                let turns = prev.turns;
                STATE.with(|st| *st.borrow_mut() = prev);
                log::info!(
                    "conversation_mem: rehydrated {} entries, {} turns, sigma {} B",
                    entries, turns, sigma
                );
                telemetry_log(5, "rehydrated_entries", &entries.to_string());
                telemetry_log(5, "rehydrated_bytes", &sigma.to_string());
                return;
            }
            log::warn!("conversation_mem: state present but could not be parsed");
        }
        log::info!("conversation_mem: fresh start");
    }

    fn handle_stop() {
        STATE.with(|st| {
            let st = st.borrow();
            log::info!(
                "conversation_mem: stopping after {} turns, sigma {} B",
                st.turns, st.bytes
            );
            let snapshot = st.serialize();
            sync(&snapshot);
        });
    }
}

edgeless_function::export!(ConversationMem);
