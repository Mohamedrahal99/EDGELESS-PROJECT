use edgeless_function::*;
use edgeless_function::lcg::Lcg;
use std::collections::HashSet;
use std::sync::{Mutex, OnceLock};

struct Conf {
    fibonacci_n: u64,
    key_space: u32,
    write_prob: f32,
}

static CONF: OnceLock<Conf> = OnceLock::new();

struct State {
    lcg: Option<Lcg>,
    waiting: HashSet<u64>,
    payload: String,
    tx_counter: u64,
    instance_id: u32,
    tx_seq: u64,
}

static STATE: OnceLock<Mutex<State>> = OnceLock::new();

struct Lambda;

impl EdgeFunction for Lambda {

    fn handle_cast(src: InstanceId, encoded_message: &[u8]) {

        let payload = match core::str::from_utf8(encoded_message) {
            Ok(p) => p,
            Err(_) => {
                log::info!("[LAMBDA][ERROR] Invalid UTF-8 payload");
                return;
            }
        };

        let conf = match CONF.get() {
            Some(c) => c,
            None => return,
        };

        let mut state = match STATE.get() {
            Some(s) => s.lock().unwrap(),
            None => return,
        };

        // -------- INIT --------
        if state.lcg.is_none() {
            let seed =
                hash_16_bytes(src.node_id) ^
                hash_16_bytes(src.component_id);

            state.instance_id = seed;

            log::info!(
                "[LAMBDA][INIT] instance_id={}",
                state.instance_id
            );

            state.lcg = Some(Lcg::new(seed));
        }

        // ==================================================
        // RESPONSE RECEIVED
        // ==================================================
        if payload.starts_with("ack_write") || payload.starts_with("read_result") {

            let tx_id = payload
                .split('(')
                .nth(1)
                .and_then(|s| s.strip_suffix(")"))
                .and_then(|inner| inner.split(':').last())
                .and_then(|id| id.parse::<u64>().ok())
                .unwrap_or(0);

            if state.waiting.remove(&tx_id) {
		telemetry_log(5, "waiting_size", &state.waiting.len().to_string());
                state.tx_counter += 1;
		/*
                log::info!(
                    "[LAMBDA][TX_COMPLETE] inst={} tx={} in_flight={}",
                    state.instance_id,
                    tx_id,
                    state.waiting.len()
                );
		*/
                // -------- SAVE STATE --------
                state.payload = "X".repeat(state.payload.len());
		/*
                log::info!(
                    "[LAMBDA][TX_SAVED] inst={} tx={} payload_size={} total_tx={}",
                    state.instance_id,
                    tx_id,
                    state.payload.len(),
                    state.tx_counter
                );
		*/
                // -------- LATENCY END --------
                telemetry_log(5, "tend", &tx_id.to_string());

            } else {
                log::info!(
                    "[LAMBDA][ORPHAN] inst={} tx_id={}",
                    state.instance_id,
                    tx_id
                );
            }

            return;
        }

        // ==================================================
        // NEW REQUEST
        // ==================================================
        if payload.is_empty() {

            // -------- TX ID --------
            state.tx_seq += 1;
            let tx_id = state.tx_seq;

            // -------- LATENCY BEGIN --------
            telemetry_log(5, "tbegin", &tx_id.to_string());

            // -------- CPU BEGIN (FIXED POSITION) --------
           // telemetry_log(5, "cpu_begin", &tx_id.to_string());

            // -------- CPU WORK --------
            let fib = fibonacci_n_th_element(conf.fibonacci_n);
	   
            log::info!(
                "[LAMBDA][FIB] n={} digits={}",
                conf.fibonacci_n,
                fib.to_string().len()
            );
            // -------- RANDOM --------
            let (is_write, k_id, value_opt) = {
                let lcg = state.lcg.as_mut().unwrap();

                let is_write = if conf.write_prob == 1.0 {
                    true
                } else if conf.write_prob == 0.0 {
                    false
                } else {
                    lcg.rand() < conf.write_prob
                };

                let k_id = (lcg.rand() * conf.key_space as f32) as u32;

                let value = if is_write {
                    Some((lcg.rand() * 1000.0) as u32)
                } else {
                    None
                };

                (is_write, k_id, value)
            };

            let key = format!("key{}", k_id);

            state.waiting.insert(tx_id);
	    telemetry_log(5, "waiting_size", &state.waiting.len().to_string());
            log::info!(
                "[LAMBDA][TX_START] inst={} tx={} key={}",
                state.instance_id,
                tx_id,
                key
            );

            let msg = if is_write {

                let value = value_opt.unwrap();

                log::info!(
                    "[LAMBDA][WRITE] inst={} key={} value={} tx={}",
                    state.instance_id,
                    key,
                    value,
                    tx_id
                );

                format!("write({}:{}:{})|{}", key, value, tx_id, state.payload)

            } else {

                log::info!(
                    "[LAMBDA][READ] inst={} key={} tx={}",
                    state.instance_id,
                    key,
                    tx_id
                );

                format!("read({}:{})|{}", key, tx_id, state.payload)
            };

            cast("out", msg.as_bytes());
        }
    }

    fn handle_call(
        _src: InstanceId,
        _msg: &[u8]
    ) -> CallRet {
        CallRet::NoReply
    }

    fn handle_init(
        payload: Option<&[u8]>,
        _state: Option<&[u8]>
    ) {
        edgeless_function::init_logger();

        let args = edgeless_function::init_payload_to_args(payload);

        let _ = CONF.set(Conf {
            fibonacci_n: args.get("fibonacci").unwrap_or(&"1").parse().unwrap_or(1),
            key_space: args.get("key_space").unwrap_or(&"1000").parse().unwrap_or(1000),
            write_prob: 0.5,
        });

        let _ = STATE.set(Mutex::new(State {
            lcg: None,
            waiting: HashSet::new(),
            payload: "X".repeat(10485760),
            tx_counter: 0,
            instance_id: 0,
            tx_seq: 0,
        }));

        log::info!("[LAMBDA][INIT] ready");

        // self-trigger loop (kept intentionally)
        cast("self", b"");
    }

    fn handle_stop() {}
}

edgeless_function::export!(Lambda);

// ==================================================
// HELPERS
// ==================================================

fn hash_16_bytes(bytes: [u8; 16]) -> u32 {
    let mut hash: u32 = 2166136261;
    for b in bytes {
        hash ^= b as u32;
        hash = hash.wrapping_mul(16777619);
    }
    hash
}

fn fibonacci_n_th_element(n: u64) -> num_bigint::BigUint {
    let mut f0 = num_bigint::BigUint::ZERO;
    let mut f1 = num_bigint::BigUint::from(1_u64);

    for i in 0..n {
        if i < 10 {
           log::info!("[FIB][STEP] i={} value={}", i, f0);
        }
        let f2 = f0 + &f1;
        f0 = f1;
        f1 = f2;
    }

    f0
}

