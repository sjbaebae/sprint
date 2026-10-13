"""Model packages preserve architecture freedom without changing the tensor ABI."""

import io
import json
import tarfile

import pytest
import torch

from sprint.tensor_model_package import load_predictor, predict_logits, unpack_model


def test_custom_model_package_roundtrip(tmp_path):
    archive = tmp_path / "model.tar"
    files = {
        "model.json": json.dumps(
            {"format": "tensor-model-v1", "entrypoint": "model.py"}
        ),
        "model.py": "import torch\ndef load_model(directory, device):\n"
        "    return lambda input_ids, attention_mask: torch.ones((input_ids.shape[0], 7), device=device)\n",
    }
    with tarfile.open(archive, "w") as bundle:
        for name, text in files.items():
            data = text.encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            bundle.addfile(member, io.BytesIO(data))
    directory = unpack_model(archive, tmp_path / "unpacked")
    predictor = load_predictor(directory, "cpu")
    inputs = torch.ones((2, 3), dtype=torch.long)
    assert predict_logits(predictor, inputs, inputs, 7).shape == (2, 7)
    with pytest.raises(ValueError, match="finite floating"):
        predict_logits(predictor, inputs, inputs, 8)


def test_package_links_cannot_escape(tmp_path):
    archive = tmp_path / "model.tar"
    with tarfile.open(archive, "w") as bundle:
        member = tarfile.TarInfo("model.py")
        member.type = tarfile.SYMTYPE
        member.linkname = "/private/labels"
        bundle.addfile(member)
    destination = tmp_path / "unpacked"
    with pytest.raises(ValueError, match="package path"):
        unpack_model(archive, destination)
    assert not destination.exists()
