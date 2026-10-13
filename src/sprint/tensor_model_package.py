"""Model-package format for an isolated inference worker, never the grader.

Submitted Python is executable and untrusted. Call load_predictor only inside a
network-blocked worker with no protected data, credentials or writable shared
state. This module does not establish container isolation itself.
"""

import importlib.util
import json
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath

MAX_PACKAGE_BYTES = 4 * 1024**3
MAX_FILES = 1000


def unpack_model(archive, destination):
    """Extract a bounded regular-file package, refusing links and path escapes."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    try:
        total = 0
        files = 0
        with tarfile.open(archive, mode="r|*") as bundle:
            for member in bundle:
                files += 1
                if files > MAX_FILES:
                    raise ValueError("model package exceeds limits")
                relative = PurePosixPath(member.name)
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or not relative.parts
                    or member.issym()
                    or member.islnk()
                ):
                    raise ValueError("invalid package path")
                target = destination.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise ValueError("package must contain regular files only")
                total += member.size
                if total > MAX_PACKAGE_BYTES:
                    raise ValueError("model package exceeds limits")
                target.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    raise ValueError("missing package file")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
        with (destination / "model.json").open("rb") as stream:
            data = stream.read(65537)
        if len(data) > 65536:
            raise ValueError("model manifest exceeds 64 KiB")
        manifest = json.loads(data)
        if manifest != {"format": "tensor-model-v1", "entrypoint": "model.py"}:
            raise ValueError("invalid model package manifest")
        if not (destination / "model.py").is_file():
            raise ValueError("missing model entrypoint")
        return destination
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def load_predictor(directory, device):
    """Execute the package loader inside the isolated model worker only.

    model.py exports load_model(directory: Path, device: str), returning a callable
    taking input_ids and attention_mask and returning [batch, vocabulary] logits.
    The package owns architecture, weights and internal inference behavior.
    """
    directory = Path(directory)
    spec = importlib.util.spec_from_file_location(
        "submitted_tensor_model", directory / "model.py"
    )
    if spec is None or spec.loader is None:
        raise ValueError("cannot load model entrypoint")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(directory))
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    predictor = module.load_model(directory, device)
    if not callable(predictor):
        raise TypeError("load_model must return a callable")
    return predictor


def predict_logits(predictor, input_ids, attention_mask, vocabulary_size):
    """Check the public tensor ABI; architecture and inference code are unrestricted."""
    import torch

    with torch.inference_mode():
        logits = predictor(input_ids=input_ids, attention_mask=attention_mask)
    if (
        not isinstance(logits, torch.Tensor)
        or logits.shape != (input_ids.shape[0], vocabulary_size)
        or not logits.is_floating_point()
        or not torch.isfinite(logits).all().item()
    ):
        raise ValueError("model must return finite floating [batch, vocabulary] logits")
    return logits.detach().float()
