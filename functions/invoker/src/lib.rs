use edgeless_function::*;
use log::info;

static mut COUNTER: u64 = 0;

struct Invoker;

impl EdgeFunction for Invoker {

    fn handle_cast(_src: InstanceId, msg: &[u8]) {

        let payload =
            core::str::from_utf8(msg).unwrap_or("");

        // ----------------------------
        // SELF LOOP
        // ----------------------------
        if payload == "tick" {

            send_write();

            delayed_cast(1000, "self", b"tick");
            return;
        }

        // ----------------------------
        // Reply from MEM
        // ----------------------------
        info!("Writer received → {}", payload);

        if payload.starts_with("ack_write") {

            if let Some(inner) =
                payload.strip_prefix("ack_write(")
                       .and_then(|s| s.strip_suffix(")"))
            {
                let key =
                    inner.split(':').next().unwrap();

                let read_msg =
                    format!("read({})", key);

                info!("Writer sending → {}", read_msg);

                cast("out", read_msg.as_bytes());
            }
        }
    }

    fn handle_init(
        _init_message: Option<&[u8]>,
        _state: Option<&[u8]>
    ) {
        edgeless_function::init_logger();

        info!("Writer initialized");

        cast("self", b"tick");
    }

    fn handle_call(
        _src: InstanceId,
        _msg: &[u8]
    ) -> CallRet {
        CallRet::NoReply
    }

    fn handle_stop() {
        info!("Writer stopped");
    }
}

// ------------------------------------------------------
// Helper
// ------------------------------------------------------
fn send_write() {

    let count;
    let value;

    unsafe {
        COUNTER += 30;
        count = COUNTER;
        value = COUNTER;
    }

    let payload =
        format!("write(key{}:{})", count, value);

    info!("Writer sending → {}", payload);

    // ✅ USE "out" alias, not "mem"
    cast("out", payload.as_bytes());
}

edgeless_function::export!(Invoker);