use edgeless_function::*;
use edgeless_function::lcg::Lcg;
use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};

//
// ================= CONFIG =================
//

struct Conf {
    fibonacci_n: u64,
    key_space: u32,
    state_size: usize,
}

static CONF: OnceLock<Conf> = OnceLock::new();

//
// ================= STATE =================
//

struct State {
    lcg: Option<Lcg>,
    waiting: HashMap<String, ()>,
    payload: String,
}

static STATE: OnceLock<Mutex<State>> = OnceLock::new();

//
// ================= FUNCTION =================
//

struct Lambda;

impl EdgeFunction for Lambda {

    fn handle_cast(src: InstanceId, encoded_message: &[u8]) {

        let payload =
            core::str::from_utf8(encoded_message)
                .unwrap_or("");

        let conf = CONF.get().unwrap();
        let mut state =
            STATE.get().unwrap().lock().unwrap();

        // -------- INIT LCG --------
        if state.lcg.is_none() {
            let node_hash = hash_16_bytes(src.node_id);
            let comp_hash = hash_16_bytes(src.component_id);

            let instance_seed = node_hash ^ comp_hash;

            log::info!("Initialized unique seed = {}", instance_seed);

            state.lcg = Some(Lcg::new(instance_seed));
        }

        // ================= EMPTY MESSAGE =================
        if payload.is_empty() {

            fibonacci_n_th_element(conf.fibonacci_n);
            // -------- PHASE 1: READ STATE --------
            let payload_len = state.payload.len();
            let slice_size = std::cmp::min(conf.state_size, payload_len);

            // -------- PHASE 2: RANDOM (ONLY LCG) --------
            let (is_write, k_id, start) = {
                let lcg = state.lcg.as_mut().unwrap();

                let rand_val = lcg.rand();

                let random_write_prob = if rand_val < 0.33 {
                    0.0
                } else if rand_val < 0.66 {
                    0.5
                } else {
                    1.0
                };

                let is_write = if random_write_prob == 1.0 {
                    true
                } else if random_write_prob == 0.0 {
                    false
                } else {
                    lcg.rand() < 0.5
                };

                let k_id =
                    (lcg.rand() * conf.key_space as f32) as u32;

                let start = if payload_len > slice_size {
                    (lcg.rand() * (payload_len - slice_size) as f32) as usize
                } else {
                    0
                };

                (is_write, k_id, start)
            };

            log::info!("Final decision: is_write = {}", is_write);

            let key = format!("key{}", k_id);

            // -------- PHASE 3: PAYLOAD ACCESS --------
            let end = start + slice_size;

            let payload_fragment =
                state.payload.get(start..end).unwrap_or("").to_string();

            log::info!("Payload fragment size = {}", payload_fragment.len());
            let before_len = state.payload.len();
            let before_preview = state.payload.chars().take(20).collect::<String>();
            // -------- PHASE 4: BUILD MESSAGE --------
            let msg = if is_write {

                let value = {
                    let lcg = state.lcg.as_mut().unwrap();
                    (lcg.rand() * 1000.0) as u32
                };

                // -------- BEFORE STATE --------
                let before_len = state.payload.len();
                let before_head = state.payload.chars().take(10).collect::<String>();
                let before_tail = state.payload
                    .chars()
                    .rev()
                    .take(10)
                    .collect::<String>()
                    .chars()
                    .rev()
                    .collect::<String>();

                // -------- MUTATE STATE --------
                state.payload.push_str(&value.to_string());

                // prevent unbounded growth
                if state.payload.len() > 10_000 {
                    let drain_len = 1000.min(state.payload.len());
                    state.payload.drain(0..drain_len);
                }

                // -------- AFTER STATE --------
                let after_len = state.payload.len();
                let after_head = state.payload.chars().take(10).collect::<String>();
                let after_tail = state.payload
                    .chars()
                    .rev()
                    .take(10)
                    .collect::<String>()
                    .chars()
                    .rev()
                    .collect::<String>();

                // -------- LOG --------
                log::info!(
                    "STATE UPDATE → len: {} → {} | head: {} → {} | tail: {} → {}",
                    before_len,
                    after_len,
                    before_head,
                    after_head,
                    before_tail,
                    after_tail
                );

                log::info!("WRITE value = {}", value);

                format!("write({}:{})|{}", key, value, payload_fragment)

            } else {

                log::info!("READ operation");

                format!("read({})|{}", key, payload_fragment)
            };

            log::info!("Sending message = {}", msg);
            
            cast("out", msg.as_bytes());

            return;
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

        let args =
            edgeless_function::init_payload_to_args(payload);

        let fibonacci_n =
            args.get("fibonacci")
                .unwrap_or(&"20")
                .parse::<u64>()
                .unwrap_or(20);

        let key_space =
            args.get("key_space")
                .unwrap_or(&"100")
                .parse::<u32>()
                .unwrap_or(100);

        let state_size =
            args.get("state_size")
                .unwrap_or(&"256")
                .parse::<usize>()
                .unwrap_or(256);

        let payload_size =
            args.get("payload_size")
                .unwrap_or(&"1024")
                .parse::<usize>()
                .unwrap_or(1024);

        let _ = CONF.set(Conf {
            fibonacci_n,
            key_space,
            state_size,
        });

        let initial_payload = "X".repeat(payload_size);

        let _ = STATE.set(
            Mutex::new(State {
                lcg: None,
                waiting: HashMap::new(),
                payload: initial_payload,
            })
        );

        log::info!("Lambda initialized");

        cast("self", b"");
    }

    fn handle_stop() {}
}

edgeless_function::export!(Lambda);

//
// ================= HELPERS =================
//

fn hash_16_bytes(bytes: [u8; 16]) -> u32 {
    let mut hash: u32 = 2166136261;

    for b in bytes {
        hash ^= b as u32;
        hash = hash.wrapping_mul(16777619);
    }

    hash
}

//
// ================= COMPUTATION =================
//

fn fibonacci_n_th_element(n: u64) -> num_bigint::BigUint {
    let mut f0 = num_bigint::BigUint::ZERO;
    let mut f1 = num_bigint::BigUint::from(1_u64);

    for _ in 0..n {
        let f2 = f0 + &f1;
        f0 = f1;
        f1 = f2;
    }

    f0
}