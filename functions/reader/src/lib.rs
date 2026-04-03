use edgeless_function::*;
use log::info;
use std::sync::OnceLock;

struct Reader;

static READER_ID: OnceLock<String> = OnceLock::new();

impl EdgeFunction for Reader {

    fn handle_init(
        init_message: Option<&[u8]>,
        _state: Option<&[u8]>
    ) {
        edgeless_function::init_logger();

        let id = if let Some(msg) = init_message {
            core::str::from_utf8(msg).unwrap_or("reader")
        } else {
            "reader"
        };

        let _ = READER_ID.set(id.to_string());

        info!("Reader initialized: {}", id);
    }

    fn handle_call(_src: InstanceId, _msg: &[u8]) -> CallRet {
        CallRet::NoReply
    }

    fn handle_cast(_src: InstanceId, msg: &[u8]) {

        let id = READER_ID
            .get()
            .map(|s| s.as_str())
            .unwrap_or("reader");

        if let Ok(text) = core::str::from_utf8(msg) {
            info!("{} received → {}", id, text);
        } else {
            info!("{} received binary data", id);
        }
    }

    fn handle_stop() {
        if let Some(id) = READER_ID.get() {
            info!("{} stopped", id);
        }
    }
}

edgeless_function::export!(Reader);