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
}

static CONF: OnceLock<Conf> = OnceLock::new();

// ================= STATE =================

struct State {
    lcg: Lcg,
    waiting: HashMap<String, ()>,
}

static STATE: OnceLock<Mutex<State>> = OnceLock::new();

// ================= FUNCTION =================

struct Lambda;

impl EdgeFunction for Lambda {

    // --------------------------------------------------
    // HANDLE CAST
    // --------------------------------------------------
    fn handle_cast(_src: InstanceId, encoded_message: &[u8]) {

        let payload =
            core::str::from_utf8(encoded_message)
                .unwrap_or("");

        let conf = CONF.get().unwrap();
        let mut state =
            STATE.get().unwrap().lock().unwrap();

        // ==================================================
        // CASE 1 → EMPTY MESSAGE
        // ==================================================
        if payload.is_empty() {

            // ---- COMPUTATION ----
            fibonacci_n_th_element(conf.fibonacci_n);

            // ---- RANDOM KEY ----
            let k_id =
                (state.lcg.rand() * conf.key_space as f32)
                as u32;

            let key = format!("key{}", k_id);

            // ---- RANDOM OP ----
            let is_write =
                state.lcg.rand() < 0.5;

            if is_write {

                let value =
                    (state.lcg.rand() * 1000.0) as u32;

                let msg =
                    format!("write({}:{})", key, value);

                state.waiting.insert(key.clone(), ());

                cast("out", msg.as_bytes());

            } else {

                let msg =
                    format!("read({})", key);

                state.waiting.insert(key.clone(), ());

                cast("out", msg.as_bytes());
            }

            return;
        }

        // ==================================================
        // CASE 2 → REPLY RECEIVED
        // ==================================================
        if payload.starts_with("ack_write")
            || payload.starts_with("read_result")
        {
            if let Some(inner) =
                payload.split('(').nth(1)
                       .and_then(|s| s.strip_suffix(")"))
            {
                let key =
                    inner.split(':').next().unwrap();

                if state.waiting.contains_key(key) {

                    // ---- COMPUTATION ----
                    fibonacci_n_th_element(conf.fibonacci_n);

                    state.waiting.remove(key);
                }
            }
        }
    }

    // --------------------------------------------------
    fn handle_call(
        _src: InstanceId,
        _msg: &[u8]
    ) -> CallRet {
        CallRet::NoReply
    }

    // --------------------------------------------------
    fn handle_init(
        payload: Option<&[u8]>,
        _state: Option<&[u8]>
    ) {
        edgeless_function::init_logger();

        let args =
            edgeless_function::init_payload_to_args(payload);

        let fibonacci_n =
            args.get("fibonacci")
                .unwrap_or(&"200")
                .parse::<u64>()
                .unwrap_or(20);

        let key_space =
            args.get("key_space")
                .unwrap_or(&"100")
                .parse::<u32>()
                .unwrap_or(100);

        let _ = CONF.set(Conf {
            fibonacci_n,
            key_space,
        });

    
        let time_seed =
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos() as u32;

    let instance_mix =
        edgeless_function::instance_id().function_id as u32;

    let seed = time_seed ^ instance_mix;
        let _ = STATE.set(
            Mutex::new(State {
                lcg: Lcg::new(seed),
                waiting: HashMap::new(),
            })
        );

        // Trigger first event
        cast("self", b"");
    }

    fn handle_stop() {}
}

edgeless_function::export!(Lambda);

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

