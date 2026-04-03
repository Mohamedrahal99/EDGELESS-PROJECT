use edgeless_function::*;
use log::info;

static mut COUNTER: u32 = 0;

struct Writer;

impl EdgeFunction for Writer {

    fn handle_cast(_src: InstanceId, _message: &[u8]) {

        let writer_id = "writer";

        let value;
        let count;

        unsafe {
            COUNTER += 1;
            count = COUNTER;
            value = COUNTER;
        }

        let payload = format!("key{}:{}", count, value);

        info!("Writer sending → {}", payload);

        // Send to MEM function
        cast("mem", payload.as_bytes());

        // loop every second
        delayed_cast(1000, "self", b"tick");
    }

    fn handle_init(
        init_message: Option<&[u8]>,
        _serialized_state: Option<&[u8]>
    ) {
        edgeless_function::init_logger();

        if let Some(msg) = init_message {
            if let Ok(id) = core::str::from_utf8(msg) {
                info!("Writer initialized: {}", id);
            }
        }

        cast("self", b"start");
    }

    fn handle_call(_src: InstanceId, _message: &[u8]) -> CallRet {
        CallRet::NoReply
    }

    fn handle_stop() {
        info!("Writer stopped");
    }
}

edgeless_function::export!(Writer);
