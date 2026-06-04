import json
import sys

# Number of lambda functions
n = int(sys.argv[1])

functions = []

# ==========================================================
# WRITER (force on mohamed-1)
# ==========================================================
functions.append({
    "name": "writer",
    "class_specification": {
        "id": "writer",
        "function_type": "RUST_WASM",
        "version": "0.1",
        "code": "functions/writer/target/wasm32-unknown-unknown/release/writer.wasm",
        "outputs": ["out"]
    },
    "output_mapping": {
        "out": "multiplex"
    },
    "annotations": {
        "init_payload": "period=100",
        "label_match_all": "hostname=mohamed-1"
    }
})

# ==========================================================
# MULTIPLEX (force on mohamed-1)
# ==========================================================
outputs = []
mapping = {}

for i in range(n):
    out = f"out{i}"
    outputs.append(out)
    mapping[out] = f"lambda{i}"

functions.append({
    "name": "multiplex",
    "class_specification": {
        "id": "multiplex",
        "function_type": "RUST_WASM",
        "version": "0.1",
        "code": "functions/multiplex/target/wasm32-unknown-unknown/release/multiplex.wasm",
        "outputs": outputs
    },
    "output_mapping": mapping,
    "annotations": {
        "init_payload": f"num_outputs={n}",
        "label_match_all": "hostname=mohamed-1"
    }
})


# ==========================================================
# LAMBDA FUNCTIONS
# Deploy all lambda functions on rpi-02
# ==========================================================
for i in range(n):
    functions.append({
        "name": f"lambda{i}",
        "class_specification": {
            "id": "lambda",
            "function_type": "RUST_WASM",
            "version": "0.1",
            "code": "functions/lambda/target/wasm32-unknown-unknown/release/lambda.wasm",
            "outputs": ["out"]
        },
        "output_mapping": {
            "out": "mem"
        },
        "annotations": {
            "init_payload": "fibonacci=1,key_space=1000,write_prob=0.5",
            "label_match_all": "hostname=rpi-02"
        }
    })

# ==========================================================
# MEM (force on mohamed-1)
# ==========================================================
functions.append({
    "name": "mem",
    "class_specification": {
        "id": "mem",
        "function_type": "RUST_WASM",
        "version": "0.1",
        "code": "functions/mem/target/wasm32-unknown-unknown/release/mem.wasm",
        "outputs": []
    },
    "output_mapping": {},
    "annotations": {
        "label_match_all": "hostname=mohamed-1"
    }
})

# ==========================================================
# WORKFLOW
# ==========================================================
workflow = {
    "functions": functions,
    "resources": [],
    "annotations": {}
}

with open("workflow.json", "w") as f:
    json.dump(workflow, f, indent=2)

print(f"Generated workflow with {n} lambda functions")
