use edgeless_function::*;
use std::collections::HashMap;
use std::sync::Mutex;

struct Mem;

// Safe global state
static STORE: Mutex<Option<HashMap<String, String>>> =
    Mutex::new(None);

impl EdgeFunction for Mem {

    // --------------------------
    // CAST → store key:value
    // --------------------------
    fn handle_cast(
        _src: InstanceId,
        message: &[u8],
    ) {
        let msg = match std::str::from_utf8(message) {
            Ok(v) => v,
            Err(_) => return,
        };

        // Expect key:value
        let mut parts = msg.splitn(2, ':');

        let key = match parts.next() {
            Some(k) => k.to_string(),
            None => return,
        };

        let value = match parts.next() {
            Some(v) => v.to_string(),
            None => return,
        };

        // Store safely
        let mut guard = STORE.lock().unwrap();
        if let Some(store) = guard.as_mut() {
            store.insert(key.clone(), value.clone());
        }

        log::info!("MEM SET {} → {}", key, value);

        // Forward value downstream
        cast("out", value.as_bytes());
    }

    // --------------------------
    // CALL → read key
    // --------------------------
    fn handle_call(
        _src: InstanceId,
        message: &[u8],
    ) -> CallRet {

        let key = match std::str::from_utf8(message) {
            Ok(v) => v,
            Err(_) => return CallRet::NoReply,
        };

        let guard = STORE.lock().unwrap();

        if let Some(store) = guard.as_ref() {
            if let Some(val) = store.get(key) {

                log::info!("MEM GET {} → {}", key, val);

                return CallRet::Reply(
                    edgeless_function::OwnedByteBuff::new_from_slice(
                        val.as_bytes()
                    )
                );
            }
        }

        CallRet::NoReply
    }

    // --------------------------
    // INIT
    // --------------------------
    fn handle_init(
        _init_message: Option<&[u8]>,
        _serialized_state: Option<&[u8]>,
    ) {
        edgeless_function::init_logger();

        let mut guard = STORE.lock().unwrap();
        *guard = Some(HashMap::new());

        log::info!("MEM initialized");
    }

    // --------------------------
    // STOP
    // --------------------------
    fn handle_stop() {
        log::info!("MEM stopped");
    }
}

edgeless_function::export!(Mem);
