use edgeless_function::*;
use log::info;
use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};

static STORE: OnceLock<Mutex<HashMap<String, String>>> =
    OnceLock::new();

const DELAY_MS: u64 = 100;

struct Mem;

impl EdgeFunction for Mem {
    fn handle_cast(src: InstanceId, msg: &[u8]) {
        let raw = match core::str::from_utf8(msg) {
            Ok(v) => v.trim(),
            Err(_) => {
                info!("[MEM][ERROR] invalid UTF-8");
                return;
            }
        };

        info!("[MEM][RECV] raw={}", raw);

        // ==================================================
        // FIRST ARRIVAL FROM LAMBDA
        // Delay request by 100 ms and preserve original sender.
        // ==================================================
        if raw.starts_with("write(") || raw.starts_with("read(") {
            let node_hex = bytes_to_hex(&src.node_id);
            let comp_hex = bytes_to_hex(&src.component_id);

            let delayed_msg = format!(
                "delayed|{}|{}|{}",
                raw,
                node_hex,
                comp_hex
            );

            info!(
                "[MEM][DELAY] scheduling self-call in {} ms",
                DELAY_MS
            );

            delayed_cast(
                DELAY_MS,
                "self",
                delayed_msg.as_bytes(),
            );

            return;
        }

        // ==================================================
        // SECOND ARRIVAL FROM SELF
        // Format:
        // delayed|<original_message>|<node_hex>|<component_hex>
        // ==================================================
        if !raw.starts_with("delayed|") {
            info!("[MEM][UNKNOWN] raw={}", raw);
            return;
        }

        let rest = &raw["delayed|".len()..];
        let parts: Vec<&str> = rest.rsplitn(3, '|').collect();

        if parts.len() != 3 {
            info!("[MEM][ERROR] malformed delayed message");
            return;
        }

        let component_hex = parts[0];
        let node_hex = parts[1];
        let original_msg = parts[2];

        let node_id = match hex_to_bytes_16(node_hex) {
            Some(v) => v,
            None => {
                info!("[MEM][ERROR] invalid node_id");
                return;
            }
        };

        let component_id = match hex_to_bytes_16(component_hex) {
            Some(v) => v,
            None => {
                info!("[MEM][ERROR] invalid component_id");
                return;
            }
        };

        let original_src = InstanceId {
            node_id,
            component_id,
        };

        // ==================================================
        // PARSE ORIGINAL COMMAND
        // ==================================================
        let mut split = original_msg.splitn(2, '|');
        let command = split.next().unwrap_or("");
        let payload_fragment = split.next().unwrap_or("");

        info!(
            "[MEM][PARSE] command={} payload_size={}",
            command,
            payload_fragment.len()
        );

        let store =
            STORE.get_or_init(|| Mutex::new(HashMap::new()));

        // ==================================================
        // WRITE
        // ==================================================
        if let Some(inner) = command
            .strip_prefix("write(")
            .and_then(|s| s.strip_suffix(")"))
        {
            let parts: Vec<&str> = inner.split(':').collect();

            if parts.len() == 3 {
                let key = parts[0];
                let value = parts[1];
                let tx_id = parts[2];

                let mut map = store.lock().unwrap();

                map.insert(
                    key.to_string(),
                    payload_fragment.to_string(),
                );

                info!(
                    "[MEM][WRITE] key={} value={} tx_id={}",
                    key,
                    value,
                    tx_id
                );

                let reply =
                    format!("ack_write({}:{})", key, tx_id);

                // Reply to original lambda
                cast_raw(original_src, reply.as_bytes());
            }

        // ==================================================
        // READ
        // ==================================================
        } else if let Some(inner) = command
            .strip_prefix("read(")
            .and_then(|s| s.strip_suffix(")"))
        {
            let parts: Vec<&str> = inner.split(':').collect();

            if parts.len() == 2 {
                let key = parts[0];
                let tx_id = parts[1];

                let mut map = store.lock().unwrap();

                let (value, status) =
                    if let Some(v) = map.get(key) {
                        (v.clone(), "hit")
                    } else {
                        let generated =
                            generate_payload(
                                payload_fragment.len()
                            );

                        map.insert(
                            key.to_string(),
                            generated.clone(),
                        );

                        info!(
                            "[MEM][MISS] key={} generated_size={}",
                            key,
                            generated.len()
                        );

                        (generated, "miss")
                    };

                info!(
                    "[MEM][READ] key={} size={} status={} tx_id={}",
                    key,
                    value.len(),
                    status,
                    tx_id
                );

                let reply = format!(
                    "read_result({}:{}:{})",
                    key,
                    status,
                    tx_id
                );

                // Reply to original lambda
                cast_raw(original_src, reply.as_bytes());
            }

        } else {
            info!("[MEM][UNKNOWN] raw={}", original_msg);
        }
    }

    fn handle_call(
        _src: InstanceId,
        _msg: &[u8],
    ) -> CallRet {
        CallRet::NoReply
    }

    fn handle_init(
        _init_message: Option<&[u8]>,
        _state: Option<&[u8]>,
    ) {
        edgeless_function::init_logger();

        STORE.get_or_init(|| Mutex::new(HashMap::new()));

        info!(
            "[MEM][INIT] version=FINAL_V3 delay={}ms",
            DELAY_MS
        );
    }

    fn handle_stop() {
        info!("[MEM][STOP]");
    }
}

edgeless_function::export!(Mem);

fn generate_payload(size: usize) -> String {
    "S".repeat(size)
}

// ==========================================================
// Helpers
// ==========================================================

fn bytes_to_hex(bytes: &[u8; 16]) -> String {
    let mut s = String::with_capacity(32);
    for b in bytes {
        s.push_str(&format!("{:02x}", b));
    }
    s
}

fn hex_to_bytes_16(hex: &str) -> Option<[u8; 16]> {
    if hex.len() != 32 {
        return None;
    }

    let mut out = [0u8; 16];

    for i in 0..16 {
        let part = &hex[i * 2..i * 2 + 2];
        out[i] = u8::from_str_radix(part, 16).ok()?;
    }

    Some(out)
}
