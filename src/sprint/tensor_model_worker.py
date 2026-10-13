"""Public tensor service running alongside untrusted model code in its sandbox.

This process receives no labels, protected mounts, operator state or credentials.
Its outputs are untrusted and must be validated by the protected evaluator.
"""

import argparse
import json
import importlib
import time
from pathlib import Path

model_package = importlib.import_module(
    "sprint.tensor_model_package" if __package__ else "tensor_model_package"
)


def serve(package, channel, vocabulary_size, device="cuda"):
    import torch

    channel = Path(channel)
    channel.mkdir(parents=True, exist_ok=True)
    directory = model_package.unpack_model(package, channel / "model")
    predictor = model_package.load_predictor(directory, device)
    (channel / "ready").write_text("ready")
    handled = set()
    while True:
        for request_path in sorted(channel.glob("request-*.json")):
            if request_path.name in handled:
                continue
            handled.add(request_path.name)
            request = json.loads(request_path.read_text())
            rows = request["input_ids"]
            maximum = max(map(len, rows))
            inputs = torch.zeros((len(rows), maximum), dtype=torch.long, device=device)
            mask = torch.zeros_like(inputs)
            # Left padding keeps the answer position at the last column.
            for index, row in enumerate(rows):
                inputs[index, -len(row) :] = torch.tensor(row, device=device)
                mask[index, -len(row) :] = 1
            logits = model_package.predict_logits(
                predictor, inputs, mask, vocabulary_size
            )
            values = logits[:, request["output_token_ids"]].cpu().tolist()
            output = channel / request_path.name.replace("request-", "response-", 1)
            temporary = output.with_suffix(".tmp")
            temporary.write_text(json.dumps(values, allow_nan=False))
            temporary.replace(output)
        time.sleep(0.05)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--vocabulary-size", type=int, required=True)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    args = parser.parse_args()
    serve(args.package, args.channel, args.vocabulary_size, args.device)
