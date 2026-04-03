use edgeless_function::*;
use std::sync::OnceLock;

//
// ================= CONFIG =================
//

struct Conf {
    period_ms: u64,
}

static CONF: OnceLock<Conf> = OnceLock::new();


//
// ================= FUNCTION =================
//

struct Writer;

impl EdgeFunction for Writer {

    // --------------------------------------------------
    // HANDLE CAST
    // --------------------------------------------------
    fn handle_cast(_src: InstanceId, msg: &[u8]) {

        let payload =
            core::str::from_utf8(msg).unwrap_or("");

        // SELF LOOP TRIGGER
        if payload == "tick" {

            // Send EMPTY message
            cast("out", b"");

            // Schedule next tick
            let conf = CONF.get().unwrap();

            delayed_cast(
                conf.period_ms,
                "self",
                b"tick"
            );
        }
    }

    // --------------------------------------------------
    fn handle_init(
        payload: Option<&[u8]>,
        _state: Option<&[u8]>
    ) {
        edgeless_function::init_logger();

        // Parse init payload
        let args =
            edgeless_function::init_payload_to_args(payload);

        let period_ms =
            args.get("period")
                .unwrap_or(&"1000")
                .parse::<u64>()
                .unwrap_or(1000);

        let _ = CONF.set(Conf { period_ms });

        // Start periodic loop
        cast("self", b"tick");
    }

    // --------------------------------------------------
    fn handle_call(
        _src: InstanceId,
        _msg: &[u8]
    ) -> CallRet {
        CallRet::NoReply
    }

    fn handle_stop() {}
}

edgeless_function::export!(Writer);
