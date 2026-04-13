use edgeless_function::*;
use log::info;
use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};

static STORE: OnceLock<Mutex<HashMap<String, String>>> =
    OnceLock::new();

struct Mem;

impl EdgeFunction for Mem {

    fn handle_cast(src: InstanceId, msg: &[u8]) {

        let raw = match core::str::from_utf8(msg) {
            Ok(v) => v.trim(),
            Err(_) => {
                info!("MEM → Invalid UTF-8");
                return;
            }
        };

        // 🔥 DEBUG (very important)
        info!("MEM RAW = {:?}", raw);

        // 🔥 split command | payload
        let mut split = raw.splitn(2, '|');
        let command = split.next().unwrap_or("");
        let payload_fragment = split.next().unwrap_or("");

        info!("MEM COMMAND = {:?}", command);
        info!("MEM PAYLOAD SIZE = {}", payload_fragment.len());

        let store =
            STORE.get_or_init(|| Mutex::new(HashMap::new()));

        // =========================
        // WRITE: write(k:v)|payload
        // =========================
        if let Some(inner) = command
            .strip_prefix("write(")
            .and_then(|s| s.strip_suffix(")"))
        {
            let mut parts = inner.splitn(2, ':');

            if let (Some(k), Some(v)) =
                (parts.next(), parts.next())
            {
                let mut map = store.lock().unwrap();

                map.insert(k.to_string(), payload_fragment.to_string());

                info!(
                    "MEM WRITE → key={} value={} payload_size={}",
                    k,
                    v,
                    payload_fragment.len()
                );

                let reply = format!("ack_write({}:{})", k, v);
                cast_raw(src, reply.as_bytes());
            } else {
                info!("MEM WRITE → malformed inner: {:?}", inner);
            }

        // =========================
        // READ: read(k)|payload
        // =========================
        } else if let Some(key) = command
            .strip_prefix("read(")
            .and_then(|s| s.strip_suffix(")"))
        {
            let mut map = store.lock().unwrap();

            let value = if let Some(v) = map.get(key) {
                v.clone()
            } else {
                let generated = generate_payload(payload_fragment.len());

                map.insert(key.to_string(), generated.clone());

                info!(
                    "MEM MISS → key={} generated_size={}",
                    key,
                    generated.len()
                );

                generated
            };

            info!(
                "MEM READ → key={} stored_size={}",
                key,
                value.len()
            );

            let reply =
                format!("read_result({}:{})", key, value);

            cast_raw(src, reply.as_bytes());

        // =========================
        // UNKNOWN
        // =========================
        } else {
            info!("MEM → Unknown command: {:?}", raw);
        }
    }

    fn handle_call(
        _src: InstanceId,
        _msg: &[u8]
    ) -> CallRet {
        CallRet::NoReply
    }

    fn handle_init(
        _init_message: Option<&[u8]>,
        _state: Option<&[u8]>
    ) {
        edgeless_function::init_logger();

        STORE.get_or_init(|| Mutex::new(HashMap::new()));

        // 🔥 version marker (super important)
        info!("MEM VERSION = FINAL_V1");

        info!("MEM initialized");
    }

    fn handle_stop() {
        info!("MEM stopped");
    }
}

edgeless_function::export!(Mem);

fn generate_payload(size: usize) -> String {
    "S".repeat(size)
}