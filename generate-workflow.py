import json
import sys

n = int(sys.argv[1])

functions = []

# writer

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
"init_payload": "period=500"
}
})

# multiplex

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
"init_payload": f"num_outputs={n}"
}
})

# lambda functions

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
"init_payload": "fibonacci=50,key_space=100"
}
})

# mem

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
"annotations": {}
})

workflow = {
"functions": functions,
"resources": [],
"annotations": {}
}

with open("workflow.json", "w") as f:
    json.dump(workflow, f, indent=2)

print(f"Generated workflow with {n} lambda functions")


