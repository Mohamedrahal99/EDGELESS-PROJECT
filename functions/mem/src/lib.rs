use edgeless_function::*;
use log::info;
use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};

/// Global in-memory store (thread-safe)
static STORE: OnceLock<Mutex<HashMap<String, String>>> =
    OnceLock::new();

struct Mem;

impl EdgeFunction for Mem {

    // --------------------------------------------------
    // Main logic: handle CAST messages
    // --------------------------------------------------
    fn handle_cast(src: InstanceId, msg: &[u8]) {

        let payload = match core::str::from_utf8(msg) {
            Ok(v) => v.trim(),
            Err(_) => {
                info!("MEM → Invalid UTF-8 message");
                return;
            }
        };

        let store =
            STORE.get_or_init(|| Mutex::new(HashMap::new()));

        // =========================
        // WRITE: write(k:v)
        // =========================
        if payload.starts_with("write(")
            && payload.ends_with(")")
        {
            let inner = &payload[6..payload.len() - 1];
            let mut parts = inner.split(':');

            if let (Some(k), Some(v)) =
                (parts.next(), parts.next())
            {
                let mut map = store.lock().unwrap();
                map.insert(k.to_string(), v.to_string());

                info!("MEM WRITE → {} = {}", k, v);

                // Reply to caller
                let reply =
                    format!("ack_write({}:{})", k, v);

                cast_raw(src, reply.as_bytes());
            }

        // =========================
        // READ: read(k)
        // =========================
        } else if payload.starts_with("read(")
            && payload.ends_with(")")
        {
            let key = &payload[5..payload.len() - 1];

            let map = store.lock().unwrap();

            let value = map
                .get(key)
                .cloned()
                .unwrap_or_else(|| "NULL".to_string());

            info!("MEM READ → {} = {}", key, value);

            let reply =
                format!("read_result({}:{})", key, value);

            cast_raw(src, reply.as_bytes());

        // =========================
        // UNKNOWN
        // =========================
        } else {
            info!("MEM → Unknown command: {}", payload);
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
        _init_message: Option<&[u8]>,
        _state: Option<&[u8]>
    ) {
        edgeless_function::init_logger();
        STORE.get_or_init(|| Mutex::new(HashMap::new()));
        info!("MEM initialized");
    }

    // --------------------------------------------------
    fn handle_stop() {
        info!("MEM stopped");
    }
}

edgeless_function::export!(Mem);