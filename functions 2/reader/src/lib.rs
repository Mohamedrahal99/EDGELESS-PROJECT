use edgeless_function::*;
use log::info;

struct Reader;

impl EdgeFunction for Reader {

    fn handle_cast(_src: InstanceId, message: &[u8]) {

        if let Ok(msg) = core::str::from_utf8(message) {
            info!("Reader received → {}", msg);
        }
    }

    fn handle_call(
        _src: InstanceId,
        _message: &[u8]
    ) -> CallRet {
        CallRet::NoReply
    }

    fn handle_init(
        init_message: Option<&[u8]>,
        _serialized_state: Option<&[u8]>
    ) {
        edgeless_function::init_logger();

        if let Some(msg) = init_message {
            if let Ok(id) = core::str::from_utf8(msg) {
                info!("Reader initialized: {}", id);
            }
        } else {
            info!("Reader initialized");
        }
    }

    fn handle_stop() {
        info!("Reader stopped");
    }
}

edgeless_function::export!(Reader);
