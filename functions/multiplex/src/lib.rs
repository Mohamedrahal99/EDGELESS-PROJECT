// SPDX-License-Identifier: MIT

pub use edgeless_function::*;

struct MultiplexFunction;

struct Conf {
    outputs: Vec<String>,
}


static CONF: std::sync::OnceLock<Conf> = std::sync::OnceLock::new();

impl EdgeFunction for MultiplexFunction {

    
    fn handle_cast(_src: InstanceId, encoded_message: &[u8]) {
        let outputs = &CONF.get().unwrap().outputs;

        if outputs.is_empty() {
            return;
        }

        for alias in outputs {
            cast(alias, encoded_message);
        } 
    }

    
    fn handle_call(_src: InstanceId, _encoded_message: &[u8]) -> CallRet {
        CallRet::Err
    }

    fn handle_init(
        payload: Option<&[u8]>,
        _serialized_state: Option<&[u8]>
    ) {
        let arguments = edgeless_function::init_payload_to_args(payload);

        let num_outputs = arguments
            .get("num_outputs")
            .unwrap_or(&"0")
            .parse::<usize>()
            .unwrap_or(0);

        let mut outputs = vec![];

        for i in 1..=num_outputs {
            outputs.push(format!("out{}", i));
        }

        let _ = CONF.set(Conf { outputs });
    }

    fn handle_stop() {}
}

edgeless_function::export!(MultiplexFunction);